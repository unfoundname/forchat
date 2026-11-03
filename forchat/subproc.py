"""受限子进程：给 run_python / git 一个"更小"的运行环境。

四件事：

1. **脱敏环境**：走 `env_scrub.clean_environ`，子进程看不到 API_KEY；
2. **超时**：`subprocess.run(timeout=...)`，超时抛 `TimeoutExpired`；
3. **内存上限**（best-effort）：Windows 用 Job Object 的 process memory limit，
   POSIX 用 `setrlimit(RLIMIT_AS)`；超限时进程会被杀/抛 MemoryError；
4. **CPU 时间上限**（仅 POSIX）：`RLIMIT_CPU`。

注意：这不是沙箱。它限制资源，不隔离文件系统与网络。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from .env_scrub import clean_environ

MEMORY_MB = int(os.environ.get("FORCHAT_MEMORY_MB", "512"))


def run(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    memory_mb: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """执行命令并施加超时/内存上限。超时抛 subprocess.TimeoutExpired。"""
    limit_mb = MEMORY_MB if memory_mb is None else memory_mb
    envv = env or clean_environ()

    if sys.platform == "win32":
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=envv,
        )
        job = _windows_memory_job(proc, limit_mb)
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                raise
        finally:
            if job:
                ctypes.windll.kernel32.CloseHandle(job)   # type: ignore[attr-defined]
        return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")

    def _preexec() -> None:                     # pragma: no cover - Windows 走不到
        import resource

        if limit_mb:
            limit = limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        cpu = max(1, int(timeout))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=envv,
        preexec_fn=_preexec,                     # type: ignore[arg-type]
    )


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
