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
    "budget_exhausted", "run_finished", "tool_denied", "worker_error", "run_resumed",
    "response_truncated", "run_budget_exhausted", "worker_stop_requested",
    "context_compacted", "worker_restarted", "fact_written", "worker_queued",
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
        total_tokens = 0
        total_seconds = 0.0
        statuses: dict[str, int] = {}
        for wid, row in workers.items():
            lines.append(
                f"{wid:18} {row['template']:10} {row['status']:6} {row['tokens']!s:>7} "
                f"{row['seconds']!s:>7}  {row['summary']}"
            )
            if isinstance(row["tokens"], int):
                total_tokens += row["tokens"]
            if isinstance(row["seconds"], (int, float)):
                total_seconds += float(row["seconds"])
            if row["status"] != "-":
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1

        detail = " ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        lines.append(
            f"合计: workers={len(workers)} tokens={total_tokens} "
            f"seconds={total_seconds:.1f} {detail}".rstrip()
        )

        denied = len(self.log.query(type="tool_denied"))
        rejected = len(self.log.query(type="spawn_rejected"))
        errored = len(self.log.query(type="worker_error"))
        if denied or rejected or errored:
            lines.append(f"护栏: tool_denied={denied} spawn_rejected={rejected} worker_error={errored}")

        finished = self.log.query(type="run_finished")
        if finished:
            event = finished[-1]
            limit = event.get("budget_limit")
            used = event.get("total_tokens", total_tokens)
            if limit:
                lines.append(
                    f"运行: status={event.get('status')} tokens={used}/{limit} "
                    f"({used / limit:.0%}) 用时={event.get('elapsed')}s"
                )
            else:
                lines.append(
                    f"运行: status={event.get('status')} tokens={used} 用时={event.get('elapsed')}s"
                )
            if "total_cost_usd" in event:
                lines.append(f"成本: ${event.get('total_cost_usd'):.4f}（按价目表估算）")
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

    def graph(self) -> dict[str, Any]:
        """给前端/可视化用的运行图：节点 + 父子边（不带时序细节）。

        nodes: id / template / depth / status / task 摘要 / tokens / cost
        edges: {from, to}——"谁派了谁"
        """
        done_status = {e["worker_id"]: e.get("status", "failed")
                       for e in self.log.query(type="worker_done")}
        reports = {e["worker_id"]: (e.get("report") or {})
                   for e in self.log.query(type="report")}
        finished = bool(self.log.query(type="run_finished"))

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for event in self.log.query(type="worker_created"):
            wid = event["worker_id"]
            report = reports.get(wid) or {}
            budget = report.get("budget") or {}
            status = done_status.get(wid)
            if status is None:
                status = "interrupted" if finished else "running"
            nodes.append(
                {
                    "id": wid,
                    "template": event.get("template"),
                    "depth": int(event.get("depth", 0)),
                    "parent": event.get("parent"),
                    "status": status,
                    "task": str(event.get("task", ""))[:120],
                    "summary": str(report.get("summary", ""))[:200],
                    "tokens": (budget.get("tokens") or [0, 0])[0],
                    "cost_usd": report.get("cost_usd"),
                }
            )
            if event.get("parent"):
                edges.append({"from": event["parent"], "to": wid})
        return {"nodes": nodes, "edges": edges}

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
        if kind == "worker_error":
            return f"[{wid}] 异常：{event.get('error')}"
        if kind == "response_truncated":
            return f"[{wid}] 响应被 max_tokens 截断（第 {event.get('round')} 轮）"
        if kind == "run_budget_exhausted":
            return f"[{wid}] 总预算用尽：{event.get('total_tokens')}/{event.get('limit')}"
        if kind == "worker_stop_requested":
            by = event.get("by")
            extra = f"（由 {by} 请求）" if by else ""
            return f"[{wid}] 收到停止请求{extra}"
        if kind == "run_resumed":
            return f"续跑：历史 workers={event.get('workers')} 下一个序号={event.get('next_seq')}"
        if kind == "context_compacted":
            return (f"[{wid}] 上下文 compact：裁掉 {event.get('dropped')} 条，"
                    f"归档 {event.get('archive')}")
        if kind == "worker_restarted":
            return f"[{wid}] 自重启（清空对话，事实保留）"
        if kind == "worker_queued":
            return f"[{wid}] 排队（pending={event.get('pending')}）"
        if kind == "fact_written":
            return f"[{wid}] 写事实 {event.get('name')}"
        if kind == "run_finished":
            return f"运行结束：workers={event.get('workers')} 用时={event.get('elapsed')}s"
        return f"[{wid}] {kind}"
