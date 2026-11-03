"""write_file / edit_file 共用的落盘与备份逻辑（避免两份实现）。"""
from __future__ import annotations

from pathlib import Path

BACKUP_DIR = ".forchat-backups"


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
    """写文件并返回"旧内容备份路径"（没有旧文件则为 None）。"""
    root = Path.cwd().resolve()
    backup = backup_existing(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return backup
