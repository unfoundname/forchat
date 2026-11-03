"""git：受限的版本控制工具。

真实项目离不开分支/提交/worktree，但把裸 shell 交给模型太危险。
所以这里只放行一个**子命令白名单**，其余一律拒绝并给出可用列表。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..registry import tool

ALLOWED = {
    "status", "diff", "log", "show", "add", "commit", "branch",
    "checkout", "switch", "worktree", "rev-parse", "ls-files", "stash",
}
TIMEOUT = 60


@tool(
    description="在项目仓库里执行受限的 git 命令（白名单子命令）。"
                "用于建分支、加 worktree、提交、看 diff。不要用它跑任意 shell。",
    params={
        "args": "git 子命令与参数，例如 \"status --short\"、\"worktree add ../wt-a -b feat-a\"",
    },
)
def git(args: str) -> str:
    parts = [part for part in args.split() if part]
    if not parts:
        return f"错误：需要参数，可用子命令：{', '.join(sorted(ALLOWED))}"
    if parts[0] not in ALLOWED:
        return f"错误：子命令 {parts[0]} 不在白名单，可用：{', '.join(sorted(ALLOWED))}"

    try:
        proc = subprocess.run(
            ["git", *parts],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"错误：git {parts[0]} 超时（>{TIMEOUT}s）"
    except OSError as exc:
        return f"错误：无法执行 git：{exc}"

    out = (proc.stdout or "").strip()
    if proc.stderr and proc.stderr.strip():
        out += "\n[stderr]\n" + proc.stderr.strip()
    out = out or "（无输出）"
    if len(out) > 4000:
        out = out[:4000] + "\n...（已截断）"
    if proc.returncode:
        out = f"[exit code {proc.returncode}]\n{out}"
    return out
