"""Runtime：把原语组装成一次运行。它只做"执行"，不含任何业务。

worker与 agent 1:1：一个 WorkerInstance 跑一个会话，会话里模型通过工具与世界交互：
    spawn      派子 worker（受 can_spawn / 深度 / worker 数约束）
    report     提交结构化交付物
    read_state 读运行状态（plan 等），防止上下文遗忘
    write_note 写状态（plan 可随时改，改的是文件不是对话）
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import hooks as hooks_mod
from .hooks import HookContext
from .scheduler import Scheduler
from .store import EventLog
from .types import Budget, WorkerInstance, Report
from .workflow import WorkerTemplate, WorkflowDef

TOOL_SPECS: dict[str, dict] = {
    "spawn": {
        "type": "function",
        "function": {
            "name": "spawn",
            "description": "派一个子 worker 执行子任务。只能派 can_spawn 里允许的模板。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "worker 模板名"},
                    "task": {"type": "string", "description": "交给它的完整任务描述"},
                    "wait": {"type": "boolean", "description": "是否等它跑完再继续，默认 true"},
                },
                "required": ["template", "task"],
            },
        },
    },
    "report": {
        "type": "function",
        "function": {
            "name": "report",
            "description": "提交本次工作的结构化交付物。这是唯一的完成信号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "unresolved": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
        },
    },
    "read_state": {
        "type": "function",
        "function": {
            "name": "read_state",
            "description": "读取当前运行状态（plan、笔记）。key 省略时返回全部。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
            },
        },
    },
    "write_note": {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "写一条状态/笔记，供自己和子 worker 后续读取。用于防止上下文遗忘。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
    },
}


class Runtime:
    def __init__(
        self,
        workflow: WorkflowDef,
        provider: Any,
        run_dir: Path | str,
        model: str = "fake",
        max_concurrent: int | None = None,
        external_tools: dict[str, tuple[dict, Any]] | None = None,
        skill_loader: Callable[[str], str | None] | None = None,
    ) -> None:
        self.wf = workflow
        self.provider = provider
        self.model = model
        # name -> (openai spec, caller(raw_arguments_json))
        # 由装配层（tool_bridge）注入；原语层不认识 registry，也不认识任何工具实现
        self.external = dict(external_tools or {})
        # skill 名 -> 正文；同样由装配层注入，原语层不认识 SKILL.md 的格式
        self.skill_loader = skill_loader
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(self.dir)

        self.max_depth = int(workflow.limits.get("max_depth", 3))
        self.max_workers = int(workflow.limits.get("max_workers", 16))
        self.scheduler = Scheduler(
            max_concurrent or int(workflow.limits.get("max_concurrent", 2))
        )

        self.state: dict[str, Any] = {}
        self.workers: dict[str, WorkerInstance] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self.started_at = time.time()

    # ------------------------------------------------------------------ 入口
    def start(self, task: str, template: str | None = None) -> WorkerInstance | None:
        root = self._create_worker(template or self.wf.entry, task, parent=None, depth=0)
        if root is None:
            raise RuntimeError("根 worker创建失败，检查 workflow 配置")

        status = "done"
        try:
            self._run_worker(root)
        except BaseException:
            status = "aborted"
            raise
        finally:
            # 无论成功还是异常，都要收掉线程池并留下"运行结束"事件，否则事件流读不出结尾
            self.scheduler.shutdown()
            self.log.append(
                "run_finished",
                status=status,
                workers=len(self.workers),
                scheduler=self.scheduler.stats(),
                elapsed=round(time.time() - self.started_at, 3),
            )
        return root

    # ------------------------------------------------------------------ worker
    def _create_worker(self, template: str, task: str, parent: WorkerInstance | None, depth: int) -> WorkerInstance | None:
        with self._lock:
            if template not in self.wf.workers:
                self.log.append("spawn_rejected", reason="unknown_template", template=template,
                                parent=parent.id if parent else None)
                return None
            if depth > self.max_depth:
                self.log.append("spawn_rejected", reason="max_depth", template=template,
                                parent=parent.id if parent else None, depth=depth)
                return None
            if len(self.workers) >= self.max_workers:
                self.log.append("spawn_rejected", reason="max_workers", template=template,
                                parent=parent.id if parent else None, limit=self.max_workers)
                return None

            self._seq += 1
            worker_id = f"n{self._seq:03d}-{template}"
            tpl = self.wf.workers[template]
            worker = WorkerInstance(
                id=worker_id,
                template=template,
                task=task,
                parent_id=parent.id if parent else None,
                depth=depth,
                budget=Budget(**{k: getattr(tpl.budget, k) for k in
                                 ("tokens", "wall_seconds", "cost_usd")}),
            )
            self.workers[worker_id] = worker
        self.log.append("worker_created", worker_id=worker_id, template=template, depth=depth,
                        parent=parent.id if parent else None, task=task)
        return worker

    def _run_worker(self, worker: WorkerInstance) -> None:
        tpl = self.wf.workers[worker.template]
        worker.status = "running"
        started = time.time()
        self.log.append("worker_started", worker_id=worker.id, template=worker.template, depth=worker.depth)
        self._run_hooks(worker, "on_start")

        messages = self._initial_messages(worker, tpl)
        reason = "max_rounds"

        for rnd in range(1, tpl.max_rounds + 1):
            if worker.budget.exhausted():
                reason = "budget"
                self.log.append("budget_exhausted", worker_id=worker.id, budget=worker.budget.snapshot())
                break

            text, calls, usage = self._stream_round(worker, tpl, messages, rnd)
            worker.budget.charge(tokens=usage)

            if not calls:
                reason = "stop"
                if worker.report is None:
                    worker.report = self._fallback_report(worker, text)
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in calls
                    ],
                }
            )
            for call in calls:
                result = self._execute_tool(worker, tpl, call, messages)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

            if worker.report is not None:
                # report 是显式的完成信号：本轮结束，不再请求下一轮
                reason = "stop"
                break

        worker.budget.charge(seconds=time.time() - started)
        self._finish(worker, reason)

    def _finish(self, worker: WorkerInstance, reason: str) -> None:
        if worker.report is None:
            worker.report = self._fallback_report(worker, "")
        worker.report.status = "done" if reason in ("stop", "max_rounds") else "failed"
        worker.report.cost = worker.budget.snapshot()
        worker.status = "done" if worker.report.status == "done" else "failed"
        self.log.append(
            "worker_done",
            worker_id=worker.id,
            status=worker.status,
            reason=reason,
            budget=worker.budget.snapshot(),
        )
        self.log.append("report", worker_id=worker.id, report=worker.report.to_dict())
        self._run_hooks(worker, "on_done" if worker.status == "done" else "on_fail")

    # ------------------------------------------------------------------ 一轮对话
    def _stream_round(
        self, worker: WorkerInstance, tpl: WorkerTemplate, messages: list[dict], rnd: int
    ) -> tuple[str, list[dict], int]:
        self.log.append("round_start", worker_id=worker.id, round=rnd)
        content: list[str] = []
        pending: dict[int, dict] = {}
        usage = 0

        for chunk in self.provider.stream(self.model, messages, self._tool_specs(tpl)):
            usage += int(chunk.get("usage", {}).get("completion_tokens", 0))
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                self.log.append("thinking_delta", worker_id=worker.id,
                                text=delta["reasoning_content"])

            for tc in delta.get("tool_calls") or []:
                slot = pending.setdefault(int(tc.get("index", 0)),
                                          {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

            if choice.get("finish_reason"):
                self.log.append("round_finish", worker_id=worker.id, round=rnd,
                                finish_reason=choice["finish_reason"])

        text = "".join(content)
        if text:
            self.log.append("assistant_message", worker_id=worker.id, round=rnd, text=text)
        calls = [pending[i] for i in sorted(pending)]
        if usage == 0:
            usage = max(1, (len(text) + sum(len(c["arguments"]) for c in calls)) // 4)
        return text, calls, usage

    # ------------------------------------------------------------------ 工具
    def _execute_tool(
        self, worker: WorkerInstance, tpl: WorkerTemplate, call: dict, messages: list[dict]
    ) -> str:
        name = call["name"]
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            self.log.append("tool_error", worker_id=worker.id, tool=name, error=str(exc))
            return f"错误：参数不是合法 JSON（{exc}）"

        self.log.append("tool_call", worker_id=worker.id, tool=name, arguments=args)

        if name not in tpl.tools:
            self.log.append("tool_rejected", worker_id=worker.id, tool=name, reason="not_in_whitelist")
            return f"错误：模板 {tpl.name} 没有授权使用工具 {name}"

        handler = {
            "spawn": self._tool_spawn,
            "report": self._tool_report,
            "read_state": self._tool_read_state,
            "write_note": self._tool_write_note,
        }.get(name)
        if handler is None:
            if name not in self.external:
                return f"错误：工具 {name} 没有实现"
            result = self.external[name][1](call["arguments"] or "{}")
        else:
            result = handler(worker, tpl, args)
        self.log.append("tool_result", worker_id=worker.id, tool=name, result=result[:500])
        return result

    def _tool_spawn(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        template = str(args.get("template", ""))
        task = str(args.get("task", ""))
        wait = bool(args.get("wait", True))

        if template not in tpl.can_spawn:
            self.log.append("spawn_rejected", worker_id=worker.id, reason="not_allowed",
                            template=template, allowed=tpl.can_spawn)
            return f"错误：{tpl.name} 不能派 {template}，允许的是 {tpl.can_spawn or '无'}"

        child = self._create_worker(template, task, parent=worker, depth=worker.depth + 1)
        if child is None:
            return "错误：子 worker被资源或深度限制拒绝"

        if wait:
            self._run_worker(child)          # 内联执行，不占并发槽
            return f"子 worker {child.id} 已完成，status={child.status}"
        self.scheduler.submit(self._run_worker, child)
        return f"子 worker {child.id} 已在后台启动"

    def _tool_report(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        worker.report = Report(
            worker_id=worker.id,
            status="done",
            summary=str(args.get("summary", "")),
            artifacts=[str(a) for a in args.get("artifacts") or []],
            decisions=[str(d) for d in args.get("decisions") or []],
            unresolved=[str(u) for u in args.get("unresolved") or []],
            evidence=[str(e) for e in args.get("evidence") or []],
        )
        return "report 已记录"

    def _tool_read_state(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        key = args.get("key")
        if key:
            return json.dumps(self.state.get(str(key), None), ensure_ascii=False)
        return json.dumps(self.state, ensure_ascii=False)

    def _tool_write_note(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        self.state[str(args.get("key"))] = args.get("value")
        return "已写入"

    # ------------------------------------------------------------------ hooks
    def _run_hooks(self, worker: WorkerInstance, when: str) -> None:
        tpl = self.wf.workers[worker.template]
        for entry in tpl.hooks.get(when, []):
            if hooks_mod.is_spawn(entry):
                template = entry["spawn"]
                task = str(entry.get("task", "review {worker_id}")).format(
                    worker_id=worker.id, template=tpl.name
                )
                child = self._create_worker(template, task, parent=worker, depth=worker.depth)
                if child is None:
                    continue
                self.log.append("hook_spawn", worker_id=worker.id, hook=when,
                                child=child.id, template=template)
                if entry.get("wait", False):
                    self._run_worker(child)
                else:
                    self.scheduler.submit(self._run_worker, child)
                continue

            action = hooks_mod.ACTION_HOOKS[entry["action"]]
            ctx = HookContext(runtime=self, worker_id=worker.id, state=self.state)
            if entry.get("sync", True):
                result = action(ctx)
                self.log.append("hook_action", worker_id=worker.id, hook=when,
                                action=entry["action"], result=result)
            else:
                self.scheduler.submit(self._hook_async, worker.id, when, action, ctx)

    def _hook_async(self, worker_id: str, when: str, action, ctx: HookContext) -> None:
        result = action(ctx)
        self.log.append("hook_action", worker_id=worker_id, hook=when, action=action.__name__,
                        result=result, async_=True)

    # ------------------------------------------------------------------ 辅助
    def _initial_messages(self, worker: WorkerInstance, tpl: WorkerTemplate) -> list[dict]:
        tool_names = ", ".join(tpl.tools) or "（无）"
        system = (
            f"{tpl.role}\n\n"
            f"--- 运行时约定 ---\n"
            f"你的 worker id 是 {worker.id}，模板 {tpl.name}，深度 {worker.depth}。\n"
            f"可用工具：{tool_names}。\n"
            f"需要子 worker 时用 spawn；完成时用 report 提交交付物（这是唯一的完成信号）。\n"
            f"不确定上下文时用 read_state 读当前 plan 和笔记，不要靠记忆。\n"
        )

        # 技能：由装配层注入 loader，原语层不认识技能的存储格式
        mounted: list[str] = []
        if self.skill_loader is not None:
            for name in tpl.skills:
                body = self.skill_loader(name)
                if body:
                    mounted.append(f"## 技能：{name}\n{body}")
        if mounted:
            system += "\n--- 已挂载技能 ---\n" + "\n\n".join(mounted) + "\n"

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": worker.task},
        ]

    def _tool_specs(self, tpl: WorkerTemplate) -> list[dict]:
        specs: list[dict] = []
        for name in tpl.tools:
            if name in TOOL_SPECS:
                specs.append(TOOL_SPECS[name])
            elif name in self.external:
                specs.append(self.external[name][0])
        return specs

    def _fallback_report(self, worker: WorkerInstance, text: str) -> Report:
        return Report(
            worker_id=worker.id,
            status="done",
            summary=(text or "").strip()[:2000] or "（无输出，未调用 report）",
        )
