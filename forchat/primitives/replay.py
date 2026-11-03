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
    "budget_exhausted", "run_finished",
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
