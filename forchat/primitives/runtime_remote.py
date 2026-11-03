"""RemoteMixin：叶子 worker 的执行选择（远程队列 / 子进程 / 原地线程）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .types import Report, WorkerInstance


class RemoteMixin:

    def _run_child_worker(self, worker: WorkerInstance) -> None:
        """叶子 worker：远程队列 > 子进程 > 原地线程。"""
        tpl = self.wf.workers[worker.template]
        if not tpl.can_spawn and self.remote_executor is not None:
            self._run_remote_worker(worker)
            return
        if self.process_workers and not tpl.can_spawn:
            self._run_process_worker(worker)
            return
        self._run_worker(worker)

    def _worker_payload(self, worker: WorkerInstance) -> dict[str, Any]:
        return {
            "workflow": self.wf.to_dict(),
            "base_dir": str(self.wf.base_dir),
            "run_dir": str(self.dir),
            "model": self.model,
            "facts_dir": str(getattr(self, "facts_dir", "")),
            "worker": {
                "id": worker.id,
                "template": worker.template,
                "task": worker.task,
                "parent_id": worker.parent_id,
                "depth": worker.depth,
                "scope": list(worker.scope),
                "facts_owner": worker.facts_owner,
            },
        }

    def _mark_killed(self, worker: WorkerInstance) -> None:
        worker.status = "killed"
        worker.report = Report(worker_id=worker.id, status="failed",
                               summary="（被硬中断：子进程已 kill）")
        self.log.append("worker_hard_killed", worker_id=worker.id)

    def _apply_remote_result(self, worker: WorkerInstance, data: dict) -> None:
        report = data.get("report") or {}
        worker.report = Report(
            worker_id=worker.id,
            status=str(report.get("status", "done")),
            summary=str(report.get("summary", "")),
            artifacts=[str(a) for a in report.get("artifacts") or []],
            decisions=[str(d) for d in report.get("decisions") or []],
            unresolved=[str(u) for u in report.get("unresolved") or []],
            evidence=[str(e) for e in report.get("evidence") or []],
            budget=report.get("budget") or {},
        )
        worker.status = str(data.get("status") or "failed")
        tokens = int(data.get("tokens") or 0)
        if tokens:
            self._charge_total(tokens, data.get("cost_usd"))
        self._run_hooks(worker, "on_done" if worker.status == "done" else "on_fail")

    def _run_remote_worker(self, worker: WorkerInstance) -> None:
        """把叶子 worker 投递到共享队列，等远程 agent 回传结果。"""
        if self.remote_executor is None:
            raise RuntimeError("remote_executor 未注入")
        worker.status = "running"
        self._run_hooks(worker, "on_start")
        stop_event = self._stop_events.setdefault(worker.id, threading.Event())
        data = self.remote_executor(self._worker_payload(worker), stop_event.is_set)
        if stop_event.is_set() or str(data.get("status")) == "killed":
            self._mark_killed(worker)
            return
        self._apply_remote_result(worker, data)

    def _run_process_worker(self, worker: WorkerInstance) -> None:
        """在子进程里跑完一个叶子 worker 的完整循环；stop 时 kill 子进程。"""
        worker.status = "running"
        self._run_hooks(worker, "on_start")   # 父进程侧跑 hook（子进程里禁用）
        payload = self._worker_payload(worker)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        if self.spawn_process is None:
            raise RuntimeError("进程 worker 需要装配层注入 spawn_process（forchat.exec.spawn）")
        proc = self.spawn_process(
            [sys.executable, "-m", "forchat.worker_proc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            scrub=False,
        )
        assert proc.stdin
        proc.stdin.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        proc.stdin.flush()

        stop_event = self._stop_events.setdefault(worker.id, threading.Event())
        killed = False
        while True:
            if stop_event.is_set():
                proc.kill()
                killed = True
                break
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue

        stderr = proc.stderr.read() if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()

        result_path = self.dir / "workers" / f"{worker.id}.result.json"
        data: dict[str, Any] | None = None
        if result_path.is_file():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None

        if killed or stop_event.is_set():
            self._mark_killed(worker)
            return
        if data is None:
            worker.status = "failed"
            worker.report = Report(worker_id=worker.id, status="failed",
                                   summary=f"（worker 子进程异常退出 rc={proc.returncode}）")
            self.log.append("worker_error", worker_id=worker.id,
                            error=f"worker_proc rc={proc.returncode} {stderr[-300:]}")
            return
        self._apply_remote_result(worker, data)
