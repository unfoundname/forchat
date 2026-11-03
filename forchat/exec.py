"""受控进程出口：生产代码里所有进程创建都必须走这里。

统一处理：

- 环境：`scrub=True` 走 `env_scrub.clean_environ`，避免子进程看到密钥；
- ACL 沙箱：`sandbox=True` 且 `FORCHAT_SANDBOX=acl` 时，经 `forchat.sandbox_win` 启动；
- 取消：`worker_id` 登记到注册表，`cancel_worker` 一次性 kill；
- 限额：`memory_mb` 给进程挂 Job Object（Windows，best-effort）。

`tests/test_layering.py` 禁止其他模块直接调用 `subprocess.Popen/run/...`。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from .env_scrub import clean_environ

_LOCK = threading.Lock()
_REGISTRY: dict[str, set[subprocess.Popen]] = {}


def _wrap_sandbox(cmd: list[str], cwd: Path, env: dict[str, str] | None):
    workspace = ((env or {}).get("FORCHAT_SANDBOX_WORKSPACE")
                 or os.environ.get("FORCHAT_SANDBOX_WORKSPACE") or str(cwd))
    mode = ((env or {}).get("FORCHAT_SANDBOX_MODE")
            or os.environ.get("FORCHAT_SANDBOX_MODE", "workspace-write"))
    temp = Path(workspace) / ".forchat-sandbox-tmp"
    envv = dict(env or clean_environ())
    envv["TMP"] = str(temp)
    envv["TEMP"] = str(temp)
    envv["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return [
        sys.executable, "-m", "forchat.sandbox_win",
        "--workspace", workspace, "--mode", mode, "--", *cmd,
    ], envv


def _acl_enabled(env: dict[str, str] | None) -> bool:
    flag = ((env or {}).get("FORCHAT_SANDBOX") or os.environ.get("FORCHAT_SANDBOX"))
    return sys.platform == "win32" and flag == "acl"


def spawn(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    scrub: bool = False,
    sandbox: bool = False,
    worker_id: str | None = None,
    memory_mb: int | None = None,
    stdin=None,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text: bool = True,
    encoding: str = "utf-8",
    errors: str = "replace",
    bufsize: int = -1,
    creationflags: int = 0,
    preexec_fn=None,
) -> subprocess.Popen:
    envv = clean_environ() if scrub else (dict(env) if env is not None else None)
    if envv is None:
        envv = dict(os.environ)
    # 唯一出口的红利：所有子进程统一 UTF-8 模式，避免 Windows 控制台/代码页把中文变成
    # GBK 解码错误（pytest 不设 PYTHONUTF8 时，worker/远程 agent 会直接失败）。
    envv["PYTHONUTF8"] = "1"
    envv.setdefault("PYTHONIOENCODING", "utf-8")
    if sandbox and _acl_enabled(envv):
        cmd, envv = _wrap_sandbox(cmd, Path(cwd or os.getcwd()), envv)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=envv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        bufsize=bufsize,
        creationflags=creationflags,
        **({'preexec_fn': preexec_fn} if preexec_fn is not None else {}),
    )
    if worker_id:
        with _LOCK:
            _REGISTRY.setdefault(worker_id, set()).add(proc)
    if memory_mb and sys.platform == "win32":
        proc._forchat_job = _windows_memory_job(proc, memory_mb)  # type: ignore[attr-defined]
    return proc


def release(proc: subprocess.Popen) -> None:
    """注销进程并关掉它的 Job Object 句柄（运行结束后调用）。"""
    with _LOCK:
        for registry in _REGISTRY.values():
            registry.discard(proc)
    job = getattr(proc, "_forchat_job", None)
    if job:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(job)  # type: ignore[attr-defined]
        except Exception:                        # noqa: BLE001
            pass
        proc._forchat_job = None                 # type: ignore[attr-defined]


def kill_worker(worker_id: str) -> int:
    with _LOCK:
        procs = list(_REGISTRY.get(worker_id, ()))
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            killed += 1
    return killed


def run_simple(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    scrub: bool = False,
    timeout: float = 30,
) -> subprocess.CompletedProcess:
    """不登记的短命令（例如 icacls），供沙箱安装等内部使用。"""
    proc = spawn(cmd, cwd=cwd, env=env, scrub=scrub, sandbox=False)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")


def _windows_memory_job(proc: subprocess.Popen, memory_mb: int):
    """给进程挂一个 Job Object，限制 process memory。失败时静默返回 None。"""
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
    except Exception:                            # noqa: BLE001
        return None
