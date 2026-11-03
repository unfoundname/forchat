"""edit_file：精确替换，不重写整个文件（省 token、少误伤）。

整段持有按路径的写锁（跨进程排队），替换后原子写回；
可选 `expected_sha256` / `expected_mtime` 做乐观并发校验。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool
from ._files import check_expected, file_digest, write_text
from ._locks import LockTimeout, path_lock


@tool(
    description="对已有文件做精确文本替换：old_string 必须在文件里出现且默认要求唯一。"
                "改一两行用它，不要用 write_file 整文件重写。",
    params={
        "path": "目标文件路径",
        "old_string": "要被替换的原文（带足够上下文，保证唯一）",
        "new_string": "替换后的文本（空字符串表示删除）",
        "replace_all": "为 true 时替换所有匹配，默认 false（要求唯一）",
        "expected_sha256": "可选：改前要求文件 sha256 等于它（空 = 不校验）",
        "expected_mtime": "可选：改前要求文件 mtime 等于它（0 = 不校验）",
    },
)
def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    expected_sha256: str = "",
    expected_mtime: float = 0.0,
) -> str:
    target = Path(path)
    if not target.is_file():
        return f"错误：文件不存在 -> {path}"
    if not old_string:
        return "错误：old_string 不能为空"

    try:
        with path_lock(target):
            problem = check_expected(target, expected_sha256, expected_mtime)
            if problem:
                return problem

            text = target.read_text(encoding="utf-8", errors="replace")
            count = text.count(old_string)
            if count == 0:
                return f"错误：在 {path} 里没找到 old_string（注意缩进与换行要完全一致）"
            if count > 1 and not replace_all:
                return (
                    f"错误：old_string 在 {path} 里出现了 {count} 次，"
                    "请提供更长的上下文，或设 replace_all=true"
                )

            new_text = text.replace(old_string, new_string) if replace_all else text.replace(
                old_string, new_string, 1
            )
            try:
                backup = write_text(target, new_text)
            except OSError as exc:
                return f"错误：写入失败 {path}: {exc}"
            digest = file_digest(target)
    except LockTimeout as exc:
        return f"错误：文件正被其他 worker 写入，请稍后重试（{exc}）"

    note = f"（旧内容已备份到 {backup}）" if backup else ""
    return (f"已编辑 {path}（替换 {count if replace_all else 1} 处，"
            f"{len(new_text) - len(text):+d} 字符，sha256={digest}）{note}")
