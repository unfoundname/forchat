"""read 工具：读文本文件，返回带行号的片段（支持 offset/limit）。"""
from __future__ import annotations

import os

from ..registry import tool

DEFAULT_LIMIT = 300


@tool(
    description="读取本地文本文件内容，返回带行号的文本。"
                "大文件用 offset/limit 分段读（offset 从 1 开始）。"
                "回答关于某个文件的问题前，先用它把文件读进来，不要凭空猜测。",
    params={
        "path": "文件路径，相对当前工作目录，例如 main.py 或 forchat/agent.py",
        "offset": "起始行号（1 开始），默认 1",
        "limit": "最多读多少行，默认 300",
    },
)
def read(path: str, offset: int = 1, limit: int = DEFAULT_LIMIT) -> str:
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

    start = max(1, int(offset))
    count = max(1, int(limit))
    end = min(len(lines), start - 1 + count)
    shown = lines[start - 1:end]
    body = "".join(f"{i:>4} | {line}" for i, line in enumerate(shown, start))
    if end < len(lines):
        body += f"\n...（共 {len(lines)} 行，本次显示 {start}-{end}；用 offset/limit 继续读）"
    return body
