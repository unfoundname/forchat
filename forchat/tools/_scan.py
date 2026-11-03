"""grep / glob 共用的文件遍历与忽略规则（避免两份实现）。"""
from __future__ import annotations

from pathlib import Path

IGNORED_PARTS = {
    ".git", "__pycache__", ".runs", ".worktrees", "node_modules",
    ".venv", "venv", ".forchat-backups", ".pytest_cache",
}
MAX_BYTES = 2_000_000


def iter_files(base: Path, pattern: str = "**/*"):
    """按 glob 遍历文件，跳过版本库/缓存/运行目录与超大文件。"""
    for path in base.glob(pattern):
        if not path.is_file():
            continue
        if IGNORED_PARTS & set(path.parts):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        yield path
