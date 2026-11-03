"""受限子进程：给 run_python / git 一个"更小"的运行环境，并支持硬中断。

五件事：

1. **脱敏环境**：走 `env_scrub.clean_environ`，子进程看不到 API_KEY；
2. **超时**：超时抛 `TimeoutExpired`；
3. **内存上限**（best-effort）：Windows Job Object / POSIX `setrlimit`；
4. **CPU 时间上限**（仅 POSIX）：`RLIMIT_CPU`；
5. **可取消**：worker 被 stop 时，`cancel_worker(worker_id)` 直接 kill 该 worker 正在跑的子进程。

进程创建本身统一走 `forchat.exec.spawn`（唯一出口；ACL 沙箱也在那里挂）。
注意：这不是沙箱。它限制资源、支持杀子进程，不隔离文件系统与网络。
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import exec as exec_mod
from .env_scrub import clean_environ

MEMORY_MB = int(os.environ.get("FORCHAT_MEMORY_MB", "512"))

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "forchat_worker", default=None
)
_CANCELS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()


class Cancelled(RuntimeError):
    """worker 被停止，正在跑的子进程被 kill。"""


class SandboxUnavailable(RuntimeError):
    """沙箱安装失败（fail-closed）：命令没有在无约束环境下执行。"""


@contextlib.contextmanager
def worker_context(worker_id: str):
    with _LOCK:
        _CANCELS.setdefault(worker_id, threading.Event())
    token = _CURRENT.set(worker_id)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def cancel_worker(worker_id: str) -> int:
    """置取消位并 kill 该 worker 名下的所有子进程。返回 kill 掉的数量。"""
    with _LOCK:
        event = _CANCELS.setdefault(worker_id, threading.Event())
        event.set()
    return exec_mod.kill_worker(worker_id)


def run(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    memory_mb: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """执行命令并施加超时/内存上限/取消。超时抛 TimeoutExpired，取消抛 Cancelled。"""
    limit_mb = MEMORY_MB if memory_mb is None else memory_mb
    scrub = env is None
    envv = clean_environ() if scrub else dict(env or {})
    worker_id = _CURRENT.get()

    def _preexec() -> None:                     # pragma: no cover - Windows 走不到
        import resource

        if limit_mb:
            limit = limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))  # type: ignore[attr-defined]
        cpu = max(1, int(timeout))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))  # type: ignore[attr-defined]

    proc = exec_mod.spawn(
        cmd,
        cwd=cwd,
        env=envv,
        scrub=scrub,
        sandbox=True,
        worker_id=worker_id,
        memory_mb=limit_mb,
        preexec_fn=None if sys.platform == "win32" else _preexec,
    )
    cancel = _CANCELS.get(worker_id) if worker_id else None
    deadline = time.time() + timeout

    try:
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled("worker stopped")
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                out, err = proc.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            if cancel is not None and cancel.is_set():
                raise Cancelled("worker stopped")
            if proc.returncode == 127 and "forchat-acl-run:" in (err or ""):
                raise SandboxUnavailable((err or "").strip()[-600:])
            return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")
    finally:
        exec_mod.release(proc)
