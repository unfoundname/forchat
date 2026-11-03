"""write_file：真正把内容落到磁盘。有它才是"能干活"的引擎。

路径安全由 policy 在运行时层统一拦（越界、敏感文件），这里只做 IO，
并把常见的编码/权限错误变成可读回执。

覆盖已有文件前会自动留一份备份（`.forchat-backups/<相对路径>`），
这样"写错/删错"至少还能捞回来——我们没有沙箱，回滚点是最后的保险。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool
from ._files import write_text


@tool(
    description="写入文本文件（覆盖或新建）。相对路径按当前工作目录解析；"
                "需要新建目录时会自动创建；覆盖已有文件前会自动备份到 .forchat-backups/。"
                "小改动请优先用 edit_file，不要整文件重写。",
    params={
        "path": "目标文件路径，例如 src/app.py 或 notes/plan.md",
        "content": "完整文件内容（会整体覆盖，不是追加）",
    },
)
def write_file(path: str, content: str) -> str:
    target = Path(path)
    try:
        backup = write_text(target, content)
    except IsADirectoryError:
        return f"错误：{path} 是目录"
    except OSError as exc:
        return f"错误：写入失败 {path}: {exc}"
    note = f"（旧内容已备份到 {backup}）" if backup else ""
    return f"已写入 {path}（{len(content)} 字符）{note}"
