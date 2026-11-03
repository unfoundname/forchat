"""read 工具：读文本文件，返回带行号的片段。"""
from __future__ import annotations

import os

from ..registry import tool

MAX_LINES = 300


@tool(
    description="读取本地文本文件内容，返回带行号的文本。"
                "回答关于某个文件的问题前，先用它把文件读进来，不要凭空猜测。",
    params={"path": "文件路径，相对当前工作目录，例如 main.py 或 forchat/agent.py"},
)
def read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return f"错误：文件不存在 -> {path}（相对路径基于 {os.getcwd()}）"
    except IsADirectoryError:
        return f"错误：{path} 是目录，请改用 list_dir"
    except OSError as exc:
        return f"错误：读取失败 {path}: {exc}"

    if not lines:
        return f"（空文件：{path}）"

    shown = lines[:MAX_LINES]
    body = "".join(f"{i:>4} | {line}" for i, line in enumerate(shown, 1))
    if len(lines) > MAX_LINES:
        body += f"\n...（共 {len(lines)} 行，只显示前 {MAX_LINES} 行）"
    return body
