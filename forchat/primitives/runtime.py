"""Runtime：把原语组装成一次运行。它只做"执行"，不含任何业务。

一个 Runtime = 一个 run 目录 + 一棵 worker 树 + 一个执行循环。
具体实现按主题拆在同目录的 mixin 里，这里只保留状态与装配：

    runtime_state.py   续跑恢复、plan、执行快照、metrics
    runtime_prompt.py  初始消息与兜底报告
    runtime_tools.py   工具分发与运行时工具
    runtime_worker.py  单个 worker 的会话循环（唯一执行循环）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .runtime_compact import CompactMixin
from .runtime_prompt import PromptMixin
from .runtime_state import StateMixin
from .runtime_tools import ToolsMixin
from .runtime_worker import WorkerMixin
from .scheduler import Scheduler
from .store import EventLog
from .types import Budget, Report, WorkerInstance
from .workflow import WorkflowDef


class Runtime(StateMixin, PromptMixin, ToolsMixin, CompactMixin, WorkerMixin):

    def __init__(
        self,
        workflow: WorkflowDef,
        provider: Any,
        run_dir: Path | str,
        model: str = "fake",
        max_concurrent: int | None = None,
        external_tools: dict[str, tuple[dict, Any]] | None = None,
        skill_loader: Callable[[str], str | None] | None = None,
        guard: Callable[[str, str], str | None] | None = None,
        pricing: Any | None = None,
        facts_dir: Path | str | None = None,
        compact_summarizer: Callable[[list[dict]], str] | None = None,
        stream_factory: Callable[..., Any] | None = None,
        worker_context: Callable[[str], Any] | None = None,
        cancel_processes: Callable[[str], Any] | None = None,
        process_workers: bool = False,
        spawn_process: Callable[..., Any] | None = None,
        restore: bool = True,
    ) -> None:
        self.wf = workflow
        self.provider = provider
        self.model = model
        # 可选的请求执行器：把模型调用放进子进程（真正可 kill）；None = 进程内
        self.stream_factory = stream_factory
        # 由装配层注入：标记"当前 worker"以便取消时 kill 它的子进程
        self.worker_context = worker_context
        self.cancel_processes = cancel_processes
        # 进程 worker 的启动函数（装配层传 forchat.exec.spawn；原语层不认识具体实现）
        self.spawn_process = spawn_process
        # 叶子 worker 是否跑在独立子进程里（真硬中断）；子进程自己不再嵌套
        self.process_workers = process_workers
        self.hooks_enabled = True
        # compact 摘要器：默认用 provider 自己调一次模型；测试/宿主可注入替换
        self.compact_summarizer = compact_summarizer
        # 每个 worker 一份事实文件；默认落在 run 目录，可用 FORCHAT_FACTS_DIR 指到长期存储
        self.facts_dir = Path(facts_dir) if facts_dir else (Path(run_dir) / "facts")
        # name -> (openai spec, caller(raw_arguments_json))
        # 由装配层（tool_bridge）注入；原语层不认识 registry，也不认识任何工具实现
        self.external = dict(external_tools or {})
        # skill 名 -> 正文；同样由装配层注入，原语层不认识 SKILL.md 的格式
        self.skill_loader = skill_loader
        # 执行前护栏：(tool, raw_arguments) -> None 放行 / str 拒绝原因
        self.guard = guard
        # 价目表：只要求有 .cost(model, prompt_tokens, completion_tokens) 方法
        self.pricing = pricing
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(self.dir)

        self.max_depth = int(workflow.limits.get("max_depth", 3))
        self.max_workers = int(workflow.limits.get("max_workers", 16))
        # 整次运行的总闸：per-worker 预算管单个节点，这里管整棵树
        self.max_wall_seconds = min(
            float(workflow.limits.get("max_wall_seconds", 604800)),
            float(workflow.limits.get("max_total_seconds", 604800)),
        )
        self.max_total_tokens = int(workflow.limits.get("max_total_tokens", 1_000_000_000))
        self.max_total_hard_tokens = int(workflow.limits.get("max_total_hard_tokens", 0))
        self.max_total_cost_usd = workflow.limits.get("max_total_cost_usd")
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self._budget_tripped = False
        self._total_budget_logged = False
        self._hard_stopped = False
        self._hard_workers: set[str] = set()
        self.hard_stop_count = 0
        self.run_deadline: float | None = None
        self.scheduler = Scheduler(
            max_concurrent or int(workflow.limits.get("max_concurrent", 2))
        )

        # plan / 笔记：落在磁盘上，重启或续跑时读回来；不靠对话记忆
        self.state_path = self.dir / "state.json"
        self.state: dict[str, Any] = self._load_state()
        self.workers: dict[str, WorkerInstance] = {}
        self._lock = threading.RLock()
        self._futures: dict[str, Any] = {}          # worker_id -> Future（异步分支的 join 用）
        self._stop_events: dict[str, Any] = {}      # worker_id -> threading.Event（协作式取消）
        self._seq = 0
        self.started_at = time.time()
        self._restored: list[str] = []
        self._interrupted: list[str] = []
        self._root_id: str | None = None
        self._beats: dict[str, float] = {}          # worker_id -> 最后一次心跳时间
        self._stuck_reported: set[str] = set()
        if restore:
            self._restore()

    def _run_hooks(self, worker: WorkerInstance, when: str) -> None:
        if not self.hooks_enabled:
            return
        super()._run_hooks(worker, when)
    def _deadline_exceeded(self) -> bool:
        return self.run_deadline is not None and time.time() > self.run_deadline

    def _charge_total(self, tokens: int, cost_usd: float | None = None) -> None:
        with self._lock:
            if tokens > 0:
                self.total_tokens += tokens
                if self.total_tokens >= self.max_total_tokens:
                    self._budget_tripped = True
                if (self.max_total_hard_tokens and not self._hard_stopped
                        and self.total_tokens >= self.max_total_hard_tokens):
                    self._hard_stopped = True
                    self.hard_stop_count += 1
                    self.log.append("budget_hard_stop", scope="run",
                                    spent=self.total_tokens,
                                    limit=self.max_total_hard_tokens)
                    for worker_id in list(self.workers):
                        self.force_stop(worker_id)
            if cost_usd:
                self.total_cost_usd += cost_usd
                if self.max_total_cost_usd is not None and self.total_cost_usd >= float(
                    self.max_total_cost_usd
                ):
                    self._budget_tripped = True

    def _hard_worker_limit(self, tpl) -> int:
        env = os.environ.get("FORCHAT_HARD_TOKENS")
        try:
            env_limit = int(env) if env else 0
        except ValueError:
            env_limit = 0
        return env_limit or int(getattr(tpl.budget, "hard_tokens", 0) or 0)

    def _hard_stop_worker(self, worker: WorkerInstance, scope: str, limit: int) -> None:
        if worker.id in self._hard_workers:
            return
        self._hard_workers.add(worker.id)
        self.hard_stop_count += 1
        self.log.append("budget_hard_stop", worker_id=worker.id, scope=scope,
                        spent=worker.budget.spent_tokens, limit=limit)
        self.force_stop(worker.id)

    # ------------------------------------------------------------------ 宿主侧干预
    def force_stop(self, worker_id: str) -> bool:
        """宿主强制叫停一个 worker：置取消位 + 取消未启动的 future。

        线程仍在网络请求里时无法硬杀（Python 线程不可终止），但请求超时会兜住；
        这里保证 worker 在下一个分片/轮次边界退出并记 killed。
        """
        worker = self.workers.get(worker_id)
        if worker is None or worker.status in ("done", "failed", "killed", "interrupted"):
            return False
        event = self._stop_events.setdefault(worker_id, threading.Event())
        event.set()
        if self.cancel_processes is not None:
            self.cancel_processes(worker_id)
        future = self._futures.get(worker_id)
        if future is not None:
            future.cancel()
        self.log.append("worker_force_stopped", worker_id=worker_id)
        return True

    def replace_worker(self, worker_id: str, task: str | None = None) -> WorkerInstance | None:
        """宿主把跑偏/过长的 worker 停掉，用同一模板/范围换一个新任务重开（新实例）。"""
        old = self.workers.get(worker_id)
        if old is None:
            return None
        self.force_stop(worker_id)
        child = self._create_worker(
            old.template,
            task or old.task,
            parent=self.workers.get(old.parent_id) if old.parent_id else None,
            depth=old.depth,
            scope=old.scope or None,
        )
        if child is None:
            return None
        child.facts_owner = old.facts_owner or old.id
        self.log.append("worker_replaced", old=worker_id, new=child.id,
                        task=(task or old.task)[:120])
        self._submit(child)
        return child

    def _run_child_worker(self, worker: WorkerInstance) -> None:
        """叶子 worker 若开了进程模式就丢进子进程跑，否则原地跑线程。"""
        tpl = self.wf.workers[worker.template]
        if self.process_workers and not tpl.can_spawn:
            self._run_process_worker(worker)
            return
        self._run_worker(worker)

    def _run_process_worker(self, worker: WorkerInstance) -> None:
        """在子进程里跑完一个叶子 worker 的完整循环；stop 时 kill 子进程。"""
        worker.status = "running"
        self._run_hooks(worker, "on_start")   # 父进程侧跑 hook（子进程里禁用）
        payload = {
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
            worker.status = "killed"
            worker.report = Report(worker_id=worker.id, status="failed",
                                   summary="（被硬中断：子进程已 kill）")
            self.log.append("worker_hard_killed", worker_id=worker.id, pid=proc.pid)
            return
        if data is None:
            worker.status = "failed"
            worker.report = Report(worker_id=worker.id, status="failed",
                                   summary=f"（worker 子进程异常退出 rc={proc.returncode}）")
            self.log.append("worker_error", worker_id=worker.id,
                            error=f"worker_proc rc={proc.returncode} {stderr[-300:]}")
            return

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

    def start(self, task: str, template: str | None = None) -> WorkerInstance | None:
        self.run_deadline = time.time() + self.max_wall_seconds
        root = self._create_worker(template or self.wf.entry, task, parent=None, depth=0)
        if root is None:
            raise RuntimeError("根 worker 创建失败，检查 workflow 配置")

        watchdog = self._start_watchdog()
        status = "done"
        try:
            self._run_worker(root)
        except BaseException:
            status = "aborted"
            raise
        finally:
            watchdog.set()
            # 无论成功还是异常，都要收掉线程池并留下"运行结束"事件，否则事件流读不出结尾
            self.scheduler.shutdown()
            final_status = status
            if status == "done":
                final_status = "done" if root.status == "done" else (
                    "killed" if root.status == "killed" else "failed"
                )
            self.log.append(
                "run_finished",
                status=final_status,
                workers=len(self.workers),
                total_tokens=self.total_tokens,
                budget_exceeded=self._budget_tripped or any(
                    w.budget.soft_logged for w in self.workers.values()
                ),
                budget_limit=self.max_total_tokens,
                budget_hard_stops=self.hard_stop_count,
                **({"total_cost_usd": round(self.total_cost_usd, 6)}
                   if self.pricing is not None else {}),
                scheduler=self.scheduler.stats(),
                elapsed=round(time.time() - self.started_at, 3),
            )
        return root
    def _create_worker(
        self,
        template: str,
        task: str,
        parent: WorkerInstance | None,
        depth: int,
        scope: list[str] | None = None,
    ) -> WorkerInstance | None:
        with self._lock:
            if template not in self.wf.workers:
                self.log.append("spawn_rejected", reason="unknown_template", template=template,
                                parent=parent.id if parent else None)
                return None
            if self._deadline_exceeded():
                self.log.append("spawn_rejected", reason="wall_clock", template=template,
                                parent=parent.id if parent else None,
                                limit=self.max_wall_seconds)
                return None
            if depth > self.max_depth:
                self.log.append("spawn_rejected", reason="max_depth", template=template,
                                parent=parent.id if parent else None, depth=depth)
                return None
            if len(self.workers) >= self.max_workers:
                self.log.append("spawn_rejected", reason="max_workers", template=template,
                                parent=parent.id if parent else None, limit=self.max_workers)
                return None
            if self._budget_tripped:
                self.log.append("spawn_rejected", reason="total_budget", template=template,
                                parent=parent.id if parent else None,
                                total_tokens=self.total_tokens, limit=self.max_total_tokens)
                return None

            self._seq += 1
            worker_id = f"n{self._seq:03d}-{template}"
            tpl = self.wf.workers[template]
            parent_scope = list(parent.scope) if parent else []
            if scope is not None:
                chosen_scope = list(scope)
            elif tpl.scope:
                chosen_scope = list(tpl.scope)
            else:
                chosen_scope = parent_scope
            worker = WorkerInstance(
                id=worker_id,
                template=template,
                task=task,
                parent_id=parent.id if parent else None,
                depth=depth,
                budget=Budget(**{k: getattr(tpl.budget, k) for k in
                                 ("tokens", "wall_seconds", "max_cost_usd")}),
                scope=chosen_scope,
            )
            self.workers[worker_id] = worker
            self._beats[worker_id] = time.time()
        self.log.append("worker_created", worker_id=worker_id, template=template, depth=depth,
                        parent=parent.id if parent else None, task=task, scope=chosen_scope)
        return worker

    # ------------------------------------------------------------------ 心跳
    def _beat(self, worker_id: str) -> None:
        with self._lock:
            self._beats[worker_id] = time.time()

    def stuck_workers(self, threshold_seconds: float = 300.0) -> list[dict[str, Any]]:
        """超过 threshold 没有心跳的 running/queued worker——服务端和前端用它发现卡死。"""
        now = time.time()
        with self._lock:
            result: list[dict[str, Any]] = []
            for wid, item in self.workers.items():
                if item.status not in ("running", "queued"):
                    continue
                beat = self._beats.get(wid)
                idle = None if beat is None else round(now - beat, 1)
                if idle is None or idle >= threshold_seconds:
                    result.append({"worker_id": wid, "status": item.status,
                                   "idle_seconds": idle})
            return result

    def _start_watchdog(self) -> threading.Event:
        """后台看门狗：发现长时间没心跳的 worker → 记事件；action=stop 时直接停。"""
        stop = threading.Event()
        try:
            threshold = float(os.environ.get("FORCHAT_STUCK_SECONDS", "600"))
        except ValueError:
            threshold = 600.0
        if threshold <= 0:
            return stop

        def _watch() -> None:
            interval = max(0.05, min(30.0, threshold / 4))
            while not stop.wait(interval):
                for item in self.stuck_workers(threshold):
                    worker_id = item["worker_id"]
                    if item.get("status") != "running" or worker_id in self._stuck_reported:
                        continue
                    self._stuck_reported.add(worker_id)
                    action = os.environ.get("FORCHAT_STUCK_ACTION", "log")
                    self.log.append("stuck_detected", worker_id=worker_id,
                                    idle_seconds=item.get("idle_seconds"), action=action)
                    if action == "stop":
                        self.force_stop(worker_id)

        threading.Thread(target=_watch, name="forchat-watchdog", daemon=True).start()
        return stop

    # ------------------------------------------------------------------ 资源感知
    def resources(self) -> dict[str, Any]:
        """当前运行还剩多少资源。派 worker 前后都该看一眼，而不是把它跑死再发现。"""
        with self._lock:
            remaining = max(0, self.max_total_tokens - self.total_tokens)
            return {
                "tokens": {
                    "used": self.total_tokens,
                    "limit": self.max_total_tokens,
                    "remaining": remaining,
                },
                "cost_usd": {
                    "used": round(self.total_cost_usd, 6),
                    "limit": self.max_total_cost_usd,
                },
                "wall_seconds": {"limit": self.max_wall_seconds, "deadline": self.run_deadline},
                "workers": {"created": len(self.workers), "limit": self.max_workers},
                "queue": {
                    "waiting": self.scheduler.stats()["pending"],
                    "concurrency": self.scheduler.max_concurrent,
                },
                "concurrency": self.scheduler.stats(),
            }
