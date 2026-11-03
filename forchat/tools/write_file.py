"""write_file：真正把内容落到磁盘。有它才是"能干活"的引擎。

路径安全由 policy 在运行时层统一拦（越界、敏感文件），这里只做 IO，
并把常见的编码/权限错误变成可读回执。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool


@tool(
    description="写入文本文件（覆盖或新建）。相对路径按当前工作目录解析；"
                "需要新建目录时会自动创建。",
    params={
        "path": "目标文件路径，例如 src/app.py 或 notes/plan.md",
        "content": "完整文件内容（会整体覆盖，不是追加）",
    },
)
def write_file(path: str, content: str) -> str:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except IsADirectoryError:
        return f"错误：{path} 是目录"
    except OSError as exc:
        return f"错误：写入失败 {path}: {exc}"
    return f"已写入 {path}（{len(content)} 字符）"
