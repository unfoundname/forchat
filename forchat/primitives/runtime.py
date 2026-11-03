"""Runtime：把原语组装成一次运行。它只做"执行"，不含任何业务。

一个 Runtime = 一个 run 目录 + 一棵 worker 树 + 一个执行循环。
具体实现按主题拆在同目录的 mixin 里，这里只保留状态与装配：

    runtime_state.py   续跑恢复、plan、执行快照、metrics
    runtime_prompt.py  初始消息与兜底报告
    runtime_tools.py   工具分发与运行时工具
    runtime_worker.py  单个 worker 的会话循环（唯一执行循环）
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .runtime_prompt import PromptMixin
from .runtime_state import StateMixin
from .runtime_tools import ToolsMixin
from .runtime_worker import WorkerMixin
from .scheduler import Scheduler
from .store import EventLog
from .types import Budget, WorkerInstance
from .workflow import WorkflowDef


class Runtime(StateMixin, PromptMixin, ToolsMixin, WorkerMixin):

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
    ) -> None:
        self.wf = workflow
        self.provider = provider
        self.model = model
        # 可选的请求执行器：把模型调用放进子进程（真正可 kill）；None = 进程内
        self.stream_factory = stream_factory
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
        self.max_total_cost_usd = workflow.limits.get("max_total_cost_usd")
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self._budget_tripped = False
        self._total_budget_logged = False
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
        self._restore()
    def _deadline_exceeded(self) -> bool:
        return self.run_deadline is not None and time.time() > self.run_deadline

    def _charge_total(self, tokens: int, cost_usd: float | None = None) -> None:
        with self._lock:
            if tokens > 0:
                self.total_tokens += tokens
                if self.total_tokens >= self.max_total_tokens:
                    self._budget_tripped = True
            if cost_usd:
                self.total_cost_usd += cost_usd
                if self.max_total_cost_usd is not None and self.total_cost_usd >= float(
                    self.max_total_cost_usd
                ):
                    self._budget_tripped = True

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

    def start(self, task: str, template: str | None = None) -> WorkerInstance | None:
        self.run_deadline = time.time() + self.max_wall_seconds
        root = self._create_worker(template or self.wf.entry, task, parent=None, depth=0)
        if root is None:
            raise RuntimeError("根 worker 创建失败，检查 workflow 配置")

        status = "done"
        try:
            self._run_worker(root)
        except BaseException:
            status = "aborted"
            raise
        finally:
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
