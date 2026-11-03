"""Replay：从事件流重建一次运行（只读，不调模型，不重放）。

- review()：人读的流水账
- tree()：worker 派生树
"""
from __future__ import annotations

from typing import Any

from .store import EventLog

_INTERESTING = {
    "worker_created", "worker_started", "assistant_message", "tool_call", "tool_result",
    "spawn_rejected", "hook_spawn", "hook_action", "report", "worker_done",
    "budget_exhausted", "run_finished", "tool_denied",
}


class Replay:
    def __init__(self, log: EventLog) -> None:
        self.log = log

    def review(self, worker_id: str | None = None, limit: int = 400) -> str:
        lines: list[str] = []
        for event in self.log.read():
            kind = event.get("type", "")
            if kind not in _INTERESTING:
                continue
            if worker_id and event.get("worker_id") != worker_id:
                continue
            lines.append(self._format(event))
            if len(lines) >= limit:
                lines.append("...（截断）")
                break
        return "\n".join(lines)

    def summary(self) -> str:
        """每个 worker 一行：模板 / 状态 / tokens / 秒 / 报告摘要。"""
        workers: dict[str, dict[str, Any]] = {}
        for event in self.log.read():
            kind = event.get("type")
            wid = event.get("worker_id")
            if not wid:
                continue
            row = workers.setdefault(
                wid, {"template": "?", "status": "-", "tokens": "-", "seconds": "-", "summary": ""}
            )
            if kind == "worker_started":
                row["template"] = event.get("template", "?")
            elif kind == "worker_done":
                row["status"] = event.get("status", "-")
                budget = event.get("budget") or {}
                spent, _ = (budget.get("tokens") or ["?", "?"])[:2]
                seconds = (budget.get("seconds") or ["?", "?"])[0]
                row["tokens"], row["seconds"] = spent, seconds
            elif kind == "report":
                row["summary"] = str((event.get("report") or {}).get("summary", ""))[:70]

        lines = [f"{'worker':18} {'template':10} {'status':6} {'tokens':>7} {'sec':>7}  summary"]
        for wid, row in workers.items():
            lines.append(
                f"{wid:18} {row['template']:10} {row['status']:6} {row['tokens']!s:>7} "
                f"{row['seconds']!s:>7}  {row['summary']}"
            )
        return "\n".join(lines)

    def tree(self) -> str:
        children: dict[str | None, list[str]] = {}
        labels: dict[str, str] = {}
        for event in self.log.query(type="worker_created"):
            wid, parent = event["worker_id"], event.get("parent")
            labels[wid] = f"{wid} [{event.get('template')}] d={event.get('depth')}"
            children.setdefault(parent, []).append(wid)

        lines: list[str] = []

        def walk(pid: str | None, prefix: str) -> None:
            for cid in children.get(pid, []):
                lines.append(f"{prefix}{labels[cid]}")
                walk(cid, prefix + "  ")

        walk(None, "")
        return "\n".join(lines)

    @staticmethod
    def _format(event: dict[str, Any]) -> str:
        kind = event.get("type")
        wid = event.get("worker_id", "-")
        if kind == "assistant_message":
            text = str(event.get("text", "")).replace("\n", " ")
            return f"[{wid}] {text[:160]}"
        if kind == "tool_call":
            args = str(event.get("arguments"))[:140]
            return f"[{wid}] tool {event.get('tool')} {args}"
        if kind == "tool_result":
            return f"[{wid}]   -> {str(event.get('result'))[:140]}"
        if kind == "report":
            report = event.get("report") or {}
            return f"[{wid}] report: {str(report.get('summary'))[:160]}"
        if kind == "spawn_rejected":
            return f"[{wid}] spawn 被拒：{event.get('reason')} {event.get('template', '')}"
        if kind == "hook_spawn":
            return f"[{wid}] hook {event.get('hook')} 派生 {event.get('child')}"
        if kind == "worker_done":
            return f"[{wid}] 结束 status={event.get('status')} reason={event.get('reason')}"
        if kind == "run_finished":
            return f"运行结束：workers={event.get('workers')} 用时={event.get('elapsed')}s"
        return f"[{wid}] {kind}"
