"""受限子进程：给 run_python / git 一个"更小"的运行环境，并支持硬中断。

五件事：

1. **脱敏环境**：走 `env_scrub.clean_environ`，子进程看不到 API_KEY；
2. **超时**：超时抛 `TimeoutExpired`；
3. **内存上限**（best-effort）：Windows Job Object / POSIX `setrlimit`；
4. **CPU 时间上限**（仅 POSIX）：`RLIMIT_CPU`；
5. **可取消**：worker 被 stop 时，`cancel_worker(worker_id)` 直接 kill 该 worker 正在跑的子进程。

注意：这不是沙箱。它限制资源、支持杀子进程，不隔离文件系统与网络。
"""
from __future__ import annotations

import contextlib
import contextvars
import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .env_scrub import clean_environ

MEMORY_MB = int(os.environ.get("FORCHAT_MEMORY_MB", "512"))

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "forchat_worker", default=None
)
_CANCELS: dict[str, threading.Event] = {}
_PROCS: dict[str, set[subprocess.Popen]] = {}
_LOCK = threading.Lock()


class Cancelled(RuntimeError):
    """worker 被停止，正在跑的子进程被 kill。"""


@contextlib.contextmanager
def worker_context(worker_id: str):
    """在执行工具的线程里标出"当前是哪个 worker"，方便取消时定位子进程。"""
    with _LOCK:
        _CANCELS.setdefault(worker_id, threading.Event())
        _PROCS.setdefault(worker_id, set())
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
        procs = list(_PROCS.get(worker_id, ()))
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            killed += 1
    return killed


def run(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    memory_mb: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """执行命令并施加超时/内存上限/取消。超时抛 TimeoutExpired，取消抛 Cancelled。"""
    limit_mb = MEMORY_MB if memory_mb is None else memory_mb
    envv = env or clean_environ()
    worker_id = _CURRENT.get()

    def _preexec() -> None:                     # pragma: no cover - Windows 走不到
        import resource

        if limit_mb:
            limit = limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        cpu = max(1, int(timeout))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _preexec

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=envv,
        **kwargs,
    )
    job = _windows_memory_job(proc, limit_mb) if sys.platform == "win32" else None
    if worker_id:
        with _LOCK:
            _PROCS.setdefault(worker_id, set()).add(proc)
    cancel = _CANCELS.get(worker_id) if worker_id else None
    deadline = time.time() + timeout

    try:
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                out, err = proc.communicate()
                raise Cancelled("worker stopped")
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                out, err = proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                out, err = proc.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
            if cancel is not None and cancel.is_set():
                raise Cancelled("worker stopped")
            return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")
    finally:
        if worker_id:
            with _LOCK:
                _PROCS.get(worker_id, set()).discard(proc)
        if job:
            ctypes.windll.kernel32.CloseHandle(job)   # type: ignore[attr-defined]


def _windows_memory_job(proc: subprocess.Popen, memory_mb: int | None):
    """给进程挂一个 Job Object，限制 process memory。失败时静默返回 None（不阻断执行）。"""
    if not memory_mb:
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JobObjectExtendedLimitInformation = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        try:
            assigned = kernel32.AssignProcessToJobObject(job, handle)
        finally:
            kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:                            # noqa: BLE001 - 尽力而为，失败就不限制
        return None
