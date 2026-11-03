"""write_file / edit_file 共用的落盘、备份与并发校验逻辑（避免两份实现）。

并发安全的两个要点：

1. **原子写**：先写同目录临时文件，再 `os.replace` 覆盖——读者永远看到旧内容或新内容，
   不会读到写了一半的文件；
2. **乐观并发**：调用方可以带 `expected_sha256` / `expected_mtime`，写前校验；
   不一致就拒绝，并回传当前值，让模型先重读再改。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

BACKUP_DIR = ".forchat-backups"


def file_digest(target: Path) -> str:
    return hashlib.sha256(target.read_bytes()).hexdigest()


def current_state(target: Path) -> dict[str, str | float | None]:
    if not target.is_file():
        return {"sha256": None, "mtime": None}
    stat = target.stat()
    return {"sha256": file_digest(target), "mtime": round(stat.st_mtime, 6)}


def check_expected(
    target: Path, expected_sha256: str = "", expected_mtime: float = 0.0
) -> str | None:
    """校验调用方声明的版本；返回错误文案（None = 通过）。"""
    if not expected_sha256 and not expected_mtime:
        return None
    if not target.is_file():
        return f"错误：文件不存在 {target}（不能用 expected_* 声明一个不存在的文件）"
    state = current_state(target)
    if expected_sha256 and state["sha256"] != expected_sha256:
        return (f"错误：文件已被别人修改（sha256 不匹配），请先 read 再改。"
                f"当前 sha256={state['sha256']}")
    if expected_mtime and abs(float(state["mtime"] or 0) - float(expected_mtime)) > 0.002:
        return (f"错误：文件已被别人修改（mtime 不匹配），请先 read 再改。"
                f"当前 mtime={state['mtime']}")
    return None


def backup_existing(target: Path, root: Path) -> str | None:
    """覆盖前把旧内容备份到 .forchat-backups/<相对路径>，返回备份相对路径。"""
    if not target.is_file():
        return None
    try:
        rel = target.resolve().relative_to(root)
    except ValueError:
        rel = Path(target.name)
    backup = root / BACKUP_DIR / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return str(backup.relative_to(root))


def write_text(target: Path, content: str) -> str | None:
    """原子写文件并返回"旧内容备份路径"（没有旧文件则为 None）。"""
    root = Path.cwd().resolve()
    backup = backup_existing(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return backup
