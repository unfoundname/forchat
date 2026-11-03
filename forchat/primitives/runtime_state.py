"""StateMixin：续跑恢复、plan 状态、执行快照、运行指标。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .types import Report, WorkerInstance


class StateMixin:

    def _restore(self) -> None:
        """同一 run 目录再次打开时，恢复历史 worker / 报告 / 序号。

        续跑语义：**继续推进这个项目**，而不是复活死掉的 worker。
        历史 worker 只读；新 worker 从 n<seq+1> 开始编号，事件追加在同一条流里。
        """
        events = self.log.read()
        if not events:
            return

        for event in events:
            kind = event.get("type")
            wid = event.get("worker_id")
            if kind == "worker_created" and wid and wid not in self.workers:
                self.workers[wid] = WorkerInstance(
                    id=wid,
                    template=str(event.get("template", "?")),
                    task=str(event.get("task", "")),
                    parent_id=event.get("parent"),
                    depth=int(event.get("depth", 0)),
                )
                if event.get("parent") is None and self._root_id is None:
                    self._root_id = wid
            elif kind == "worker_done" and wid in self.workers:
                self.workers[wid].status = event.get("status", "failed")
            elif kind == "report" and wid in self.workers:
                data = event.get("report") or {}
                self.workers[wid].report = Report(
                    worker_id=wid,
                    status=str(data.get("status", "done")),
                    summary=str(data.get("summary", "")),
                    artifacts=[str(a) for a in data.get("artifacts") or []],
                    decisions=[str(d) for d in data.get("decisions") or []],
                    unresolved=[str(u) for u in data.get("unresolved") or []],
                    evidence=[str(e) for e in data.get("evidence") or []],
                    budget=data.get("budget") or {},
                )

        for wid in self.workers:
            digits = wid[1:].split("-", 1)[0]
            if digits.isdigit():
                self._seq = max(self._seq, int(digits))

        # 没有 worker_done 的 worker = 上次进程崩了/被杀时还在跑，标记 interrupted 交给新根处理
        for wid, item in self.workers.items():
            if item.status == "pending" and wid not in {e.get("worker_id") for e in events
                                                        if e.get("type") == "worker_done"}:
                item.status = "interrupted"
                self._interrupted.append(wid)

        self._restored = list(self.workers)
        self.log.append(
            "run_resumed",
            workers=len(self.workers),
            next_seq=self._seq + 1,
            interrupted=len(self._interrupted),
        )
    # ------------------------------------------------------------------ 状态
    @property
    def interrupted(self) -> list[str]:
        """上次运行中断（没有 worker_done）的 worker id。"""
        return list(self._interrupted)

    def auto_resume(self, task: str | None = None) -> WorkerInstance | None:
        """崩溃恢复整棵树：先恢复中断的子 worker（深→浅），再恢复根。

        - 根有快照且中断 → 直接续跑根（保住它自己的对话与未完成的决策）；
        - 根没中断 → 起一个新根，历史 worker 作为只读上下文。
        """
        if self.run_deadline is None:
            self.run_deadline = time.time() + self.max_wall_seconds

        root = self.workers.get(self._root_id) if self._root_id else None
        targets = [
            item for item in self.workers.values()
            if item.status == "interrupted" and self._worker_state_path(item.id).is_file()
        ]
        targets.sort(key=lambda item: -item.depth)      # 深的先恢复，父的后续轮次才能看到子报告

        resumed: list[str] = []
        for item in targets:
            if root is not None and item.id == root.id:
                continue
            try:
                self.resume_worker(item.id)
                resumed.append(item.id)
            except KeyError:
                continue

        self.log.append("auto_resume", resumed=resumed, root=self._root_id,
                        interrupted=len(self._interrupted))
        if root is not None and root.status == "interrupted":
            return self.resume_worker(root.id)
        return self.start(task or "继续推进该项目：先 read_state 看历史 plan 与笔记")
    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
    def metrics(self) -> dict[str, Any]:
        """给服务端 /metrics 用的运行时快照（不是持久状态）。"""
        counts: dict[str, int] = {}
        for item in self.workers.values():
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "workers": counts,
            "tokens": self.total_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
            "elapsed": round(time.time() - self.started_at, 1),
            "budget_tripped": self._budget_tripped,
        }
    # ------------------------------------------------------------------ 执行快照
    def _worker_state_path(self, worker_id: str) -> Path:
        return self.dir / "workers" / f"{worker_id}.json"
    def _save_worker_state(
        self, worker: WorkerInstance, messages: list[dict], round_no: int, reported: bool
    ) -> None:
        """每轮落一次快照：messages + 轮次 + 是否已 report。崩溃后可以接着跑。"""
        path = self._worker_state_path(worker.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "worker_id": worker.id,
            "template": worker.template,
            "task": worker.task,
            "round": round_no,
            "reported": reported,
            "messages": messages,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
    def resume_worker(self, worker_id: str) -> WorkerInstance:
        """从快照继续一个已经中断/失败的 worker（同一 run 目录、追加事件）。"""
        path = self._worker_state_path(worker_id)
        if not path.is_file():
            raise KeyError(f"没有 {worker_id} 的执行快照：{path}")
        worker = self.workers.get(worker_id)
        if worker is None:
            raise KeyError(f"这个 run 里没有 worker {worker_id}")
        state = json.loads(path.read_text(encoding="utf-8"))
        if self.run_deadline is None:
            self.run_deadline = time.time() + self.max_wall_seconds
        self.log.append("worker_resumed", worker_id=worker_id, round=state.get("round", 0))
        self._run_worker(worker, resume=state, run_hooks=False)
        return worker
