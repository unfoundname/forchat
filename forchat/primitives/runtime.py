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


def _is_inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


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
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "授权给子 worker 的目录（相对项目根），例如 [\".worktrees/a\"]；"
                                       "省略则继承你的范围。只能授予你自己范围内的目录。",
                    },
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
    "write_artifact": {
        "type": "function",
        "function": {
            "name": "write_artifact",
            "description": "把交付附件（补丁、日志片段、说明）写进本次运行目录，返回相对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "附件名，例如 patch.diff"},
                    "content": {"type": "string", "description": "附件内容"},
                },
                "required": ["name", "content"],
            },
        },
    },
    "read_worker_report": {
        "type": "function",
        "function": {
            "name": "read_worker_report",
            "description": "读取某个 worker 的报告与预算。长运行里回看子 worker 的细节时用它，"
                           "不要去翻整段事件流。",
            "parameters": {
                "type": "object",
                "properties": {"worker_id": {"type": "string", "description": "子 worker 的 id"}},
                "required": ["worker_id"],
            },
        },
    },
    "wait_workers": {
        "type": "function",
        "function": {
            "name": "wait_workers",
            "description": "等待异步派出的子 worker 完成（spawn(wait=false) 之后用它汇合）。"
                           "不传 worker_ids 时等待本 worker 的所有直接子 worker。",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要等的 worker id 列表；省略表示全部直接子 worker",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "最多等多久，默认 300 秒",
                    },
                },
            },
        },
    },
    "stop_worker": {
        "type": "function",
        "function": {
            "name": "stop_worker",
            "description": "请求停止一个仍在运行的 worker（协作式：它在下一个分片/下一轮边界退出）。",
            "parameters": {
                "type": "object",
                "properties": {"worker_id": {"type": "string"}},
                "required": ["worker_id"],
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
        guard: Callable[[str, str], str | None] | None = None,
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
        self.total_tokens = 0
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
        self._restore()

    # ------------------------------------------------------------------ 续跑
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

        self._restored = list(self.workers)
        self.log.append(
            "run_resumed",
            workers=len(self.workers),
            next_seq=self._seq + 1,
        )

    # ------------------------------------------------------------------ 状态
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

    def _deadline_exceeded(self) -> bool:
        return self.run_deadline is not None and time.time() > self.run_deadline

    def _charge_total(self, tokens: int) -> None:
        if tokens <= 0:
            return
        with self._lock:
            self.total_tokens += tokens
            if self.total_tokens >= self.max_total_tokens:
                self._budget_tripped = True

    # ------------------------------------------------------------------ 入口
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
            final_status = status if status != "done" else ("done" if root.status == "done" else "failed")
            self.log.append(
                "run_finished",
                status=final_status,
                workers=len(self.workers),
                total_tokens=self.total_tokens,
                budget_limit=self.max_total_tokens,
                scheduler=self.scheduler.stats(),
                elapsed=round(time.time() - self.started_at, 3),
            )
        return root

    # ------------------------------------------------------------------ worker
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
                                 ("tokens", "wall_seconds")}),
                scope=chosen_scope,
            )
            self.workers[worker_id] = worker
        self.log.append("worker_created", worker_id=worker_id, template=template, depth=depth,
                        parent=parent.id if parent else None, task=task, scope=chosen_scope)
        return worker

    def _run_worker(self, worker: WorkerInstance) -> None:
        tpl = self.wf.workers[worker.template]
        worker.status = "running"
        started = time.time()
        self.log.append("worker_started", worker_id=worker.id, template=worker.template, depth=worker.depth)
        self._run_hooks(worker, "on_start")

        messages = self._initial_messages(worker, tpl)
        reason = "stop"
        last_charge = started
        last_text = ""
        reported = False
        rnd = 0

        # 默认不限轮数：协调型 worker 跑几十上百轮是正常的，真正的闸是预算与墙钟。
        # 想给单个模板设上限，就在 workflow.json 里写 max_rounds。
        while True:
            if tpl.max_rounds is not None and rnd >= tpl.max_rounds:
                reason = "max_rounds"
                break
            stop_event = self._stop_events.get(worker.id)
            if stop_event is not None and stop_event.is_set():
                reason = "stopped"
                self.log.append("worker_stop_requested", worker_id=worker.id, round=rnd)
                break
            rnd += 1
            # 墙钟要按轮次记账，否则 wall_seconds 只有在 worker 结束后才生效
            now = time.time()
            worker.budget.charge(seconds=now - last_charge)
            last_charge = now

            if self._deadline_exceeded():
                reason = "wall_clock"
                self.log.append("run_expired", worker_id=worker.id,
                                limit=self.max_wall_seconds)
                break
            if worker.budget.exhausted():
                reason = "budget"
                self.log.append("budget_exhausted", worker_id=worker.id, budget=worker.budget.snapshot())
                break
            if self._budget_tripped:
                reason = "total_budget"
                self.log.append("run_budget_exhausted", worker_id=worker.id,
                                total_tokens=self.total_tokens, limit=self.max_total_tokens)
                break

            try:
                text, calls, usage = self._stream_round(worker, tpl, messages, rnd)
            except Exception as exc:                     # noqa: BLE001
                # 模型/网络层炸了：只让这一个 worker 失败，不掀翻整次运行
                reason = "error"
                self.log.append("worker_error", worker_id=worker.id,
                                error=f"{type(exc).__name__}: {exc}")
                last_text = last_text or f"（异常：{type(exc).__name__}: {exc}）"
                break
            worker.budget.charge(tokens=usage)
            self._charge_total(usage)
            last_text = text

            stop_event = self._stop_events.get(worker.id)
            if stop_event is not None and stop_event.is_set():
                reason = "stopped"
                break

            if not calls:
                reason = "stop"
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
                try:
                    result = self._execute_tool(worker, tpl, call, messages)
                except Exception as exc:                 # noqa: BLE001
                    self.log.append("tool_error", worker_id=worker.id, tool=call["name"],
                                    error=f"{type(exc).__name__}: {exc}")
                    result = f"错误：{type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

            if worker.report is not None:
                # report 是显式的完成信号：本轮结束，不再请求下一轮
                reported = True
                reason = "stop"
                break

        worker.budget.charge(seconds=time.time() - last_charge)
        self._finish(worker, reason, reported=reported, last_text=last_text)

    def _finish(self, worker: WorkerInstance, reason: str, reported: bool, last_text: str = "") -> None:
        if worker.report is None:
            worker.report = self._fallback_report(worker, last_text)
        if reason == "stopped":
            status = "killed"
        elif reported:
            status = "done"
        else:
            status = "failed"
        # 只有真的调用过 report 才算完成；跑满轮次/超预算/只说话不交报告都不算
        worker.report.status = "done" if status == "done" else "failed"
        worker.report.budget = worker.budget.snapshot()
        worker.status = status
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

        stop_event = self._stop_events.setdefault(worker.id, threading.Event())

        for chunk in self.provider.stream(self.model, messages, self._tool_specs(tpl)):
            if stop_event.is_set():
                # 协作式取消：只能做到"下一个分片到达时停"，在途网络请求没法硬杀
                self.log.append("worker_stop_requested", worker_id=worker.id, round=rnd)
                break
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
                if choice["finish_reason"] == "length":
                    # 被 max_tokens 截断：工具参数可能缺尾巴，调用方应重试而不是硬执行
                    self.log.append("response_truncated", worker_id=worker.id, round=rnd)

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

        if self.guard is not None:
            reason = self.guard(name, call["arguments"] or "{}", worker.scope)
            if reason:
                self.log.append("tool_denied", worker_id=worker.id, tool=name, reason=reason)
                return f"错误：策略拒绝执行 {name}：{reason}"

        handler = {
            "spawn": self._tool_spawn,
            "report": self._tool_report,
            "read_state": self._tool_read_state,
            "write_note": self._tool_write_note,
            "write_artifact": self._tool_write_artifact,
            "read_worker_report": self._tool_read_worker_report,
            "wait_workers": self._tool_wait_workers,
            "stop_worker": self._tool_stop_worker,
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
        raw_paths = args.get("paths") or []

        if template not in tpl.can_spawn:
            self.log.append("spawn_rejected", worker_id=worker.id, reason="not_allowed",
                            template=template, allowed=tpl.can_spawn)
            return f"错误：{tpl.name} 不能派 {template}，允许的是 {tpl.can_spawn or '无'}"

        granted: list[str] | None = None
        if raw_paths:
            granted = [str(item) for item in raw_paths]
            project = Path.cwd().resolve()
            parent_roots = [(project / part).resolve() for part in worker.scope] or [project]
            for part in granted:
                target = (project / part).resolve()
                if not _is_inside(target, project) or not any(
                    _is_inside(target, base) for base in parent_roots
                ):
                    self.log.append("spawn_rejected", worker_id=worker.id, reason="scope_grant",
                                    template=template, requested=granted)
                    return f"错误：不能把 {part} 授权给子 worker——超出你自己的范围 {worker.scope or ['项目根']}"

        child = self._create_worker(template, task, parent=worker, depth=worker.depth + 1,
                                    scope=granted)
        if child is None:
            return "错误：子 worker被资源或深度限制拒绝"

        if wait:
            self._run_worker(child)          # 内联执行，不占并发槽
            summary = (child.report.summary if child.report else "")[:400]
            # 子 worker 的 hook 可能又派了 worker（如 review），它们的成败父 worker 必须看得见，
            # 否则"review 超时/失败"会被静默吞掉。
            with self._lock:
                grandchildren = [w for w in self.workers.values() if w.parent_id == child.id]
            extra = ""
            if grandchildren:
                extra = "\n子 worker 的派生：" + "; ".join(
                    f"{w.id}({w.template}) {w.status}" for w in grandchildren
                )
            return f"子 worker {child.id} 已完成，status={child.status}；report: {summary}{extra}"
        self._submit(child)
        return f"子 worker {child.id} 已在后台启动；用 wait_workers 汇合"

    def _submit(self, child: WorkerInstance) -> None:
        future = self.scheduler.submit(self._run_worker, child)
        self._futures[child.id] = future

    def _tool_wait_workers(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        ids = [str(i) for i in args.get("worker_ids") or []]
        timeout = float(args.get("timeout_seconds") or 300)
        with self._lock:
            if not ids:
                ids = [wid for wid, w in self.workers.items()
                       if w.parent_id == worker.id and wid in self._futures]
            futures = {wid: self._futures[wid] for wid in ids if wid in self._futures}

        if not futures:
            return "没有需要等待的异步子 worker"

        deadline = time.time() + timeout
        pending = dict(futures)
        while pending and time.time() < deadline:
            for wid, future in list(pending.items()):
                if future.done():
                    pending.pop(wid)
                    continue
            if pending:
                time.sleep(0.05)

        lines = []
        for wid in ids:
            target = self.workers.get(wid)
            if target is None:
                lines.append(f"- {wid}: 不存在")
            elif wid in pending:
                lines.append(f"- {wid}: 仍在运行（timeout={timeout}s）")
            else:
                summary = (target.report.summary if target.report else "")[:160]
                lines.append(f"- {wid}: {target.status} — {summary or '（无报告）'}")
        return "\n".join(lines)

    def _tool_stop_worker(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        target_id = str(args.get("worker_id") or "")
        target = self.workers.get(target_id)
        if target is None:
            return f"错误：没有 worker {target_id}"
        if target.status in ("done", "failed", "killed"):
            return f"worker {target_id} 已结束（{target.status}）"

        event = self._stop_events.setdefault(target_id, threading.Event())
        event.set()
        self.log.append("worker_stop_requested", worker_id=target_id, by=worker.id)
        return f"已请求停止 {target_id}（协作式：下一分片/下一轮退出，不会立刻杀掉）"

    def _tool_report(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        declared = [str(a) for a in args.get("artifacts") or []]
        # 自己 write_artifact 过的文件自动进索引，模型不用再手抄一遍路径
        merged = list(dict.fromkeys([*worker.artifacts, *declared]))
        worker.report = Report(
            worker_id=worker.id,
            status="done",
            summary=str(args.get("summary", "")),
            artifacts=merged,
            decisions=[str(d) for d in args.get("decisions") or []],
            unresolved=[str(u) for u in args.get("unresolved") or []],
            evidence=[str(e) for e in args.get("evidence") or []],
        )
        self._run_hooks(worker, "on_report")
        return "report 已记录"

    def _tool_read_state(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        key = args.get("key")
        if key:
            return json.dumps(self.state.get(str(key), None), ensure_ascii=False)
        return json.dumps(self.state, ensure_ascii=False)

    def _tool_write_note(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        key = str(args.get("key"))
        if not key or key == "None":
            return "错误：write_note 需要 key"
        self.state[key] = args.get("value")
        self._save_state()
        return "已写入"

    def _tool_write_artifact(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "错误：write_artifact 需要 name"
        path = self.log.artifact(f"{worker.id}-{name}", str(args.get("content", "")))
        worker.artifacts.append(path)
        return f"附件已写入 {path}"

    def _tool_read_worker_report(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        target_id = str(args.get("worker_id") or "")
        target = self.workers.get(target_id)
        if target is None:
            return f"错误：没有 worker {target_id}"
        if target.report is None:
            return f"worker {target_id} 还没有报告（status={target.status}）"
        data = target.report.to_dict()
        data["status"] = target.status
        data["template"] = target.template
        return json.dumps(data, ensure_ascii=False)

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
                    self._submit(child)
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

        # 续跑：把历史 worker 的结论直接摆在新的根 worker 面前，别让它以为从零开始
        if worker.parent_id is None and self._restored:
            lines = []
            for wid in self._restored[-12:]:
                prev = self.workers[wid]
                summary = (prev.report.summary if prev.report else "")[:200]
                lines.append(f"- {wid} [{prev.template}] {prev.status}: {summary or '（无报告）'}")
            system += (
                "\n--- 历史运行（同一 run 目录的上一轮）---\n"
                + "\n".join(lines)
                + "\n需要全文时用 read_worker_report(worker_id)；plan 与笔记用 read_state 读。\n"
            )

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
