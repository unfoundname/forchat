"""运行目录的保留策略：按"最近 N 次"和"最老 N 天"清理 `.runs/`。

默认 dry-run，只有显式 `--yes` 才真的删除——删运行记录不可逆。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path


def plan_prune(
    runs_root: Path, keep_last: int = 20, older_than_days: float | None = None
) -> list[Path]:
    """返回应删除的运行目录列表（不执行删除）。"""
    if not runs_root.is_dir():
        return []
    runs = [p for p in runs_root.iterdir() if p.is_dir()]
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    victims: list[Path] = []
    if keep_last >= 0:
        victims.extend(runs[keep_last:])
    if older_than_days is not None:
        cutoff = time.time() - older_than_days * 86400
        victims.extend(p for p in runs if p.stat().st_mtime < cutoff)
    return sorted(set(victims))


def prune(
    runs_root: Path,
    keep_last: int = 20,
    older_than_days: float | None = None,
    dry_run: bool = True,
) -> tuple[list[Path], int]:
    """删除计划中的运行目录，返回 (被删列表, 释放字节数)。"""
    victims = plan_prune(runs_root, keep_last, older_than_days)
    freed = 0
    for path in victims:
        freed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    return victims, freed
