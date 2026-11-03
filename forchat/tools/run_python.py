"""run_python 工具：把确定性工作交给脚本，模型只负责编排。

这是「技能带资源」的关键一环：SKILL.md 讲思路，scripts/ 里的脚本干重活，
模型用一个通用执行工具把它跑起来 —— 比让模型自己读一堆文件再心算可靠得多。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .. import subproc
from ..registry import tool

TIMEOUT = 30          # 秒（可用 FORCHAT_SCRIPT_TIMEOUT 覆盖）
MAX_OUTPUT = 8000     # 字符，防止一次把上下文灌爆


@tool(
    description="用当前 Python 解释器执行一个本地 .py 脚本，返回 stdout/stderr。"
                "技能脚本、项目自测都走它。",
    params={
        "script": "脚本路径；相对路径按 cwd 解析，例如 skills/code-graph/scripts/graph.py",
        "args": "传给脚本的参数，空格分隔，可为空，例如 \". --max-nodes=20\"",
        "cwd": "在哪个目录执行（worktree 内跑测试时用），默认当前目录",
    },
)
def run_python(script: str, args: str = "", cwd: str = "") -> str:
    work_dir = Path(cwd).resolve() if cwd else Path.cwd()
    if not work_dir.is_dir():
        return f"错误：目录不存在 -> {work_dir}"

    target = Path(script)
    resolved = target if target.is_absolute() else (work_dir / target)
    if not resolved.is_file():
        return f"错误：脚本不存在 -> {resolved}（cwd={work_dir}）"

    cmd = [sys.executable, str(resolved), *[item for item in args.split() if item]]
    timeout = float(os.environ.get("FORCHAT_SCRIPT_TIMEOUT", TIMEOUT))
    try:
        proc = subproc.run(cmd, cwd=work_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"错误：执行超时（>{timeout:g}s），可能是脚本在等输入或死循环"
    except subproc.Cancelled:
        return "错误：已取消（worker 被停止）"
    except OSError as exc:
        return f"错误：无法执行 {resolved}: {exc}"

    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    out = out.strip() or "（无输出）"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n...（输出共 {len(out)} 字符，已截断）"
    if proc.returncode:
        out = f"[exit code {proc.returncode}]\n{out}"
    return out
