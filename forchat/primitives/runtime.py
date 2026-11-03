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
    ) -> None:
        self.wf = workflow
        self.provider = provider
        self.model = model
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
            float(workflow.limits.get("max_wall_seconds", 3600)),
            float(workflow.limits.get("max_total_seconds", 3600)),
        )
        self.max_total_tokens = int(workflow.limits.get("max_total_tokens", 1_000_000))
        self.max_total_cost_usd = workflow.limits.get("max_total_cost_usd")
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self._budget_tripped = False
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
        self.log.append("worker_created", worker_id=worker_id, template=template, depth=depth,
                        parent=parent.id if parent else None, task=task, scope=chosen_scope)
        return worker
