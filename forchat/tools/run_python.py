"""run_python 工具：把确定性工作交给脚本，模型只负责编排。

这是「技能带资源」的关键一环：SKILL.md 讲思路，scripts/ 里的脚本干重活，
模型用一个通用执行工具把它跑起来 —— 比让模型自己读一堆文件再心算可靠得多。
"""
from __future__ import annotations

import os
import subprocess
import sys

from ..registry import tool

TIMEOUT = 30          # 秒
MAX_OUTPUT = 8000     # 字符，防止一次把上下文灌爆


@tool(
    description="用当前 Python 解释器执行一个本地 .py 脚本，返回 stdout/stderr。"
                "技能目录下 scripts/ 里的脚本用它来跑，例如依赖图分析。",
    params={
        "script": "脚本路径，相对当前工作目录，例如 skills/code-graph/scripts/graph.py",
        "args": "传给脚本的参数，空格分隔，可为空，例如 \". --max-nodes=20\"",
    },
)
def run_python(script: str, args: str = "") -> str:
    if not os.path.isfile(script):
        return f"错误：脚本不存在 -> {script}（当前目录 {os.getcwd()}）"

    cmd = [sys.executable, script, *[item for item in args.split() if item]]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"错误：执行超时（>{TIMEOUT}s），可能是脚本在等输入或死循环"
    except OSError as exc:
        return f"错误：无法执行 {script}: {exc}"

    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr
    out = out.strip() or "（无输出）"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n...（输出共 {len(out)} 字符，已截断）"
    if proc.returncode:
        out = f"[exit code {proc.returncode}]\n{out}"
    return out
