"""write_file：真正把内容落到磁盘。有它才是"能干活"的引擎。

路径安全由 policy 在运行时层统一拦（越界、敏感文件），这里只做 IO，
并把常见的编码/权限错误变成可读回执。

覆盖已有文件前会自动备份到 `.forchat-backups/<相对路径>`；
写入用"临时文件 + os.replace"保证原子性；可选 `expected_sha256` / `expected_mtime`
做乐观并发校验（文件被别人改过就拒绝，让模型先重读）。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool
from ._files import check_expected, file_digest, write_text


@tool(
    description="写入文本文件（覆盖或新建）。原子写 + 覆盖前自动备份；"
                "可带 expected_sha256/expected_mtime 做乐观并发校验（不匹配就拒绝）。"
                "小改动请优先用 edit_file。",
    params={
        "path": "目标文件路径，例如 src/app.py 或 notes/plan.md",
        "content": "完整文件内容（会整体覆盖，不是追加）",
        "expected_sha256": "可选：写前要求文件当前内容的 sha256 等于它（空 = 不校验）",
        "expected_mtime": "可选：写前要求文件 mtime 等于它（0 = 不校验）",
    },
)
def write_file(
    path: str,
    content: str,
    expected_sha256: str = "",
    expected_mtime: float = 0.0,
) -> str:
    target = Path(path)
    problem = check_expected(target, expected_sha256, expected_mtime)
    if problem:
        return problem
    try:
        backup = write_text(target, content)
    except IsADirectoryError:
        return f"错误：{path} 是目录"
    except OSError as exc:
        return f"错误：写入失败 {path}: {exc}"
    note = f"（旧内容已备份到 {backup}）" if backup else ""
    return f"已写入 {path}（{len(content)} 字符，sha256={file_digest(target)}）{note}"
