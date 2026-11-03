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


def plan_compacted_cleanup(
    runs_root: Path, keep_latest: int = 3, older_than_days: float | None = None
) -> list[Path]:
    """compact 归档的清理计划：每个 worker 只留最近 keep_latest 份。

    给前端"归档管理"面板用：先拿计划，再由用户确认执行。
    """
    if not runs_root.is_dir():
        return []
    victims: list[Path] = []
    for folder in runs_root.glob("*/compacted"):
        groups: dict[str, list[Path]] = {}
        for path in sorted(folder.glob("*.json")):
            key = path.name.rsplit("-", 2)[0]     # n002-coder-001.json -> n002-coder
            groups.setdefault(key, []).append(path)
        for files in groups.values():
            if keep_latest > 0:
                victims.extend(files[:-keep_latest])
            else:
                victims.extend(files)
    if older_than_days is not None:
        cutoff = time.time() - older_than_days * 86400
        victims.extend(
            p for p in runs_root.glob("*/compacted/*.json") if p.stat().st_mtime < cutoff
        )
    return sorted(set(victims))


def cleanup_compacted(
    runs_root: Path,
    keep_latest: int = 3,
    older_than_days: float | None = None,
    dry_run: bool = True,
) -> tuple[list[Path], int]:
    """删除计划中的 compact 归档，返回 (被删列表, 释放字节数)。"""
    victims = plan_compacted_cleanup(runs_root, keep_latest, older_than_days)
    freed = 0
    for path in victims:
        try:
            freed += path.stat().st_size
        except OSError:
            continue
        if not dry_run:
            path.unlink(missing_ok=True)
    return victims, freed
