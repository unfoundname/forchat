"""list_dir 工具：看目录结构，比盲猜路径可靠得多。"""
from __future__ import annotations

import os

from ..registry import tool

MAX_ENTRIES = 200


@tool(
    description="列出目录下的文件和子目录。不了解项目结构时先调用它。",
    params={"path": "目录路径，相对当前工作目录，默认当前目录"},
)
def list_dir(path: str = ".") -> str:
    if not os.path.isdir(path):
        return f"错误：不是目录 -> {path}（当前目录 {os.getcwd()}）"
    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        return f"错误：无法列出 {path}: {exc}"

    lines = []
    for entry in entries[:MAX_ENTRIES]:
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            lines.append(f"d  {entry}/")
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            lines.append(f"f  {entry}  ({size} B)")

    if len(entries) > MAX_ENTRIES:
        lines.append(f"...（共 {len(entries)} 项，只显示前 {MAX_ENTRIES} 项）")
    return "\n".join(lines) if lines else f"（空目录：{path}）"
