"""并发安全自检：原子写 + 乐观并发（expected_sha256 / expected_mtime）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat import registry, tools  # noqa: F401


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, content: str, **extra) -> str:
    payload = {"path": str(path), "content": content, **extra}
    return registry.execute("write_file", json.dumps(payload))


def _edit(path: Path, old: str, new: str, **extra) -> str:
    payload = {"path": str(path), "old_string": old, "new_string": new, **extra}
    return registry.execute("edit_file", json.dumps(payload))


def test_write_file_is_atomic_and_returns_hash() -> None:
    work = tmpdir()
    target = work / "note.txt"
    out = _write(target, "v1")
    assert target.read_text(encoding="utf-8") == "v1"
    assert _digest(target) in out
    assert not list(work.glob("*.tmp")), "临时文件必须清理干净"


def test_write_file_rejects_stale_hash() -> None:
    work = tmpdir()
    target = work / "note.txt"
    _write(target, "v1")
    stale = _digest(target)
    _write(target, "v2")                       # 别人改过了

    out = _write(target, "v3", expected_sha256=stale)
    assert "已被别人修改" in out, out
    assert target.read_text(encoding="utf-8") == "v2", "冲突时不能覆盖"


def test_edit_file_checks_hash() -> None:
    work = tmpdir()
    target = work / "app.py"
    _write(target, "value = 1\n")
    stale = _digest(target)
    _write(target, "value = 2\n")

    out = _edit(target, "value = 2", "value = 3", expected_sha256=stale)
    assert "已被别人修改" in out, out
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    current = _digest(target)
    out = _edit(target, "value = 2", "value = 3", expected_sha256=current)
    assert "已编辑" in out, out
    assert target.read_text(encoding="utf-8") == "value = 3\n"


def test_edit_file_checks_mtime() -> None:
    import os
    import time

    work = tmpdir()
    target = work / "m.txt"
    _write(target, "a")
    old = time.time() - 100
    os.utime(target, (old, old))           # 把 mtime 拨到过去，避免同毫秒写入
    mtime = target.stat().st_mtime
    _write(target, "b")
    out = _edit(target, "b", "c", expected_mtime=mtime)
    assert "已被别人修改" in out, out


def main() -> int:
    checks = [
        ("原子写并回传 hash", test_write_file_is_atomic_and_returns_hash),
        ("旧 hash 被拒绝", test_write_file_rejects_stale_hash),
        ("edit 校验 hash", test_edit_file_checks_hash),
        ("edit 校验 mtime", test_edit_file_checks_mtime),
    ]
    return run("并发安全自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
