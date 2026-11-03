"""Scheduler：准入控制。并发槽、状态计数、等待。

关键选择：**等待型子 worker在父 worker线程内联执行**（不占槽），只有"不等"的分支才进线程池。
否则并发=1 时，父 worker占着唯一槽等子 worker，直接死锁。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class Scheduler:
    def __init__(self, max_concurrent: int = 2) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrent, thread_name_prefix="worker")
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.submitted = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            self.submitted += 1
        return self._pool.submit(self._wrap, fn, args, kwargs)

    def _wrap(self, fn: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self.running -= 1

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    def stats(self) -> dict[str, int]:
        return {
            "max_concurrent": self.max_concurrent,
            "submitted": self.submitted,
            "peak_running": self.peak,
        }
