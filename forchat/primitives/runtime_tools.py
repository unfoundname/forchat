"""ToolsMixin：工具分发与运行时工具实现（spawn/report/wait/stop/state/artifact）。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .runtime_specs import TOOL_SPECS
from .types import Report, WorkerInstance
from .workflow import WorkerTemplate


def _is_inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


class ToolsMixin:

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
    def stop_all(self) -> int:
        """请求停止所有仍在运行的 worker（服务端取消端点用）。返回受影响的 worker 数。"""
        count = 0
        for wid, item in self.workers.items():
            if item.status in ("pending", "running"):
                self._stop_events.setdefault(wid, threading.Event()).set()
                count += 1
        if count:
            self.log.append("stop_all_requested", workers=count)
        return count
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
    def _tool_specs(self, tpl: WorkerTemplate) -> list[dict]:
        specs: list[dict] = []
        for name in tpl.tools:
            if name in TOOL_SPECS:
                specs.append(TOOL_SPECS[name])
            elif name in self.external:
                specs.append(self.external[name][0])
        return specs
