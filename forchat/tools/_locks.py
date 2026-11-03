"""按文件路径的跨进程写锁（advisory）。

`write_file` / `edit_file` 在"校验 → 读 → 写"整段持有该锁，两个 worker 同时改同一文件时
会排队而不是互相覆盖。锁是**建议锁**：`run_python` 里的脚本不会自动遵守。

实现：每个路径一个 `.forchat-locks/<sha256>.lock` 文件，Windows 用 `msvcrt.locking`，
POSIX 用 `fcntl.flock`；等待超时抛 `LockTimeout`。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import time
from pathlib import Path

LOCK_DIR = ".forchat-locks"


class LockTimeout(RuntimeError):
    """等锁超时。"""


def _lock_path(target: Path) -> Path:
    key = os.path.normcase(str(target.resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    folder = Path.cwd().resolve() / LOCK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}.lock"


def _try_lock(handle) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def path_lock(target: Path):
    """持有某个文件路径的写锁；超时抛 LockTimeout。"""
    timeout = float(os.environ.get("FORCHAT_WRITE_LOCK_TIMEOUT", "5"))
    lock_path = _lock_path(target)
    handle = lock_path.open("a+b")
    if handle.tell() == 0 and lock_path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.time() + timeout
    acquired = False
    try:
        while True:
            if _try_lock(handle):
                acquired = True
                break
            if time.time() >= deadline:
                raise LockTimeout(f"{timeout:g}s 内没等到 {target} 的写锁")
            time.sleep(0.05)
        yield
    finally:
        if acquired:
            _unlock(handle)
        handle.close()
