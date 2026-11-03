"""Hook：生命周期挂载点。两类，语义完全不同：

- action hook：执行一个确定性动作（快照、通知），返回字符串，不影响worker成败；
- spawn hook：派生一个新的 worker worker（"完成后自动派 review"就是它）。

两者都可以配 sync。sync=False 的 action 丢进调度器后台执行，sync=False 的 spawn 直接提交线程池。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

HOOK_WHEN = ("on_start", "on_report", "on_done", "on_fail")


@dataclass
class HookContext:
    runtime: Any            # 避免循环 import，运行时对象在调用时才用到
    worker_id: str
    state: dict[str, Any]


def _snapshot(ctx: HookContext) -> str:
    log = ctx.runtime.log
    path = log.artifact(f"state-{ctx.worker_id}.json", json.dumps(ctx.state, ensure_ascii=False, indent=2))
    return f"snapshot -> {path}"


def _notify(ctx: HookContext) -> str:
    return f"notify {ctx.worker_id}"


ACTION_HOOKS: dict[str, Callable[[HookContext], str]] = {
    "snapshot": _snapshot,
    "notify": _notify,
}


def is_spawn(entry: dict) -> bool:
    return "spawn" in entry


def validate_entry(entry: dict, templates: set[str]) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"hook 条目必须是对象，收到 {entry!r}")
    if ("action" in entry) == ("spawn" in entry):
        raise ValueError(f"hook 条目必须且只能有 action 或 spawn 之一：{entry!r}")
    if "action" in entry and entry["action"] not in ACTION_HOOKS:
        raise ValueError(f"未知 action hook：{entry['action']}，可用：{sorted(ACTION_HOOKS)}")
    if "spawn" in entry and entry["spawn"] not in templates:
        raise ValueError(f"hook 要派生的模板不存在：{entry['spawn']}")
