"""Windows ACL 写沙箱（对标 DSH）：受限令牌 + Low 完整性 + 能力 SID。

机制（与 DSH 的 windows-acl 档一致）：

- 按工作区路径派生能力 SID `S-1-4-x-y`，给工作区目录加一条 allow ACE；
- 用 `CreateRestrictedToken(WRITE_RESTRICTED|SANDBOX_INERT|DISABLE_MAX_PRIVILEGE)`
  造令牌，restricting SIDs = [logon SID, Everyone, 能力 SID]；
  （缺 logon SID / Everyone 时子进程 DLL 初始化会 0xC0000142）
- 把能力 SID 的全权 ACE 并进令牌默认 DACL，否则子进程建匿名管道会 EPERM；
- 令牌降到 Low 完整性（S-1-16-4096），并给授权目录打 Low 标签；
- `CreateProcessAsUser` 启动子进程，先放进 kill-on-close 的 Job Object 再 resume。

边界（诚实，和 DSH 的 partial 一致）：只限制**写**；读不受限、网络不隔离、
NTFS 硬链接可绕过、Everyone 有写权的对象（如 Public、NUL）仍可写、
被 AppContainer 包 SID ACL 过的树对 Low 令牌不可读。

任何一步失败都抛 `SandboxError`，绝不退化成普通进程。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import hashlib
import os
import subprocess
import sys
from pathlib import Path


class SandboxError(RuntimeError):
    """沙箱安装失败（fail-closed）。"""


_WIN = sys.platform == "win32"
if _WIN:                                       # pragma: no branch - Linux 上只导入不执行
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetCurrentProcess.restype = w.HANDLE
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p
    _k32.CreateJobObjectW.restype = w.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    _k32.SetInformationJobObject.restype = w.BOOL
    _k32.AssignProcessToJobObject.restype = w.BOOL
    _k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    _k32.TerminateProcess.restype = w.BOOL
    _k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    _k32.CloseHandle.argtypes = [w.HANDLE]
    _k32.CloseHandle.restype = w.BOOL
    _k32.ResumeThread.restype = w.DWORD
    _k32.ResumeThread.argtypes = [w.HANDLE]
    _k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    _k32.GetExitCodeProcess.restype = w.BOOL
    _k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    _adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    _adv.OpenProcessToken.restype = w.BOOL
    _adv.CreateRestrictedToken.restype = w.BOOL
    _adv.CreateProcessAsUserW.restype = w.BOOL
    _adv.GetTokenInformation.restype = w.BOOL
    _adv.SetTokenInformation.restype = w.BOOL
    _adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _adv.ConvertStringSidToSidW.restype = w.BOOL
    _adv.GetNamedSecurityInfoW.argtypes = [
        w.LPCWSTR, w.DWORD, w.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _adv.GetNamedSecurityInfoW.restype = w.DWORD
    _adv.SetNamedSecurityInfoW.argtypes = [
        w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _adv.SetNamedSecurityInfoW.restype = w.DWORD
    _adv.SetEntriesInAclW.restype = w.DWORD
    _adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", w.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _TRUSTEE(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", w.DWORD),
        ("TrusteeForm", w.DWORD), ("TrusteeType", w.DWORD), ("ptstrName", w.LPWSTR),
    ]


class _EXPLICIT_ACCESS(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", w.DWORD), ("grfAccessMode", w.DWORD),
        ("grfInheritance", w.DWORD), ("Trustee", _TRUSTEE),
    ]


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE), ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD),
    ]


class _JOBOBJECT_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
GRANT_ACCESS = 1
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 3
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0
FILE_ALL_ACCESS = 0x1F01FF
FILE_GENERIC_READ = 0x120089
FILE_TRAVERSE = 0x20
WRITE_RESTRICTED = 0x8
SANDBOX_INERT = 0x4
DISABLE_MAX_PRIVILEGE = 0x1
TOKEN_GROUPS_CLASS = 2
TOKEN_DEFAULT_DACL_CLASS = 6
TOKEN_INTEGRITY_LEVEL_CLASS = 25
SE_GROUP_LOGON_ID = 0xC0000000
CREATE_SUSPENDED = 0x4
STARTF_USESTDHANDLES = 0x100


def _check(ok, what: str) -> None:
    if not ok:
        raise SandboxError(f"{what} 失败（Win32 错误 {ctypes.get_last_error()}）")


def derive_sid(workspace: str) -> str:
    """按工作区路径派生能力 SID（30 位子权威，和 DSH 的 S-1-4-x-y 一致）。"""
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(workspace)).encode()).digest()
    a = int.from_bytes(digest[:4], "little") % (2**30 - 1) + 1
    b = int.from_bytes(digest[4:8], "little") % (2**30 - 1) + 1
    return f"S-1-4-{a}-{b}"


def _sid(text: str) -> ctypes.c_void_p:
    sid = ctypes.c_void_p()
    _check(_adv.ConvertStringSidToSidW(text, ctypes.byref(sid)), f"解析 SID {text}")
    return sid


def _token_sid(token, info_class: int) -> ctypes.c_void_p:
    needed = w.DWORD()
    _adv.GetTokenInformation(token, info_class, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise SandboxError("GetTokenInformation 查询大小失败")
    buf = ctypes.create_string_buffer(needed.value)
    _check(_adv.GetTokenInformation(token, info_class, buf, needed.value,
                                    ctypes.byref(needed)), "读取令牌信息")
    ptr = ctypes.c_void_p.from_buffer_copy(buf).value
    length = _adv.GetLengthSid(ctypes.c_void_p(ptr))
    dest = ctypes.create_string_buffer(length)
    _check(_adv.CopySid(length, dest, ctypes.c_void_p(ptr)), "复制 SID")
    return ctypes.cast(dest, ctypes.c_void_p)


def _logon_sid(token) -> ctypes.c_void_p:
    needed = w.DWORD()
    _adv.GetTokenInformation(token, TOKEN_GROUPS_CLASS, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value)
    _check(_adv.GetTokenInformation(token, TOKEN_GROUPS_CLASS, buf, needed.value,
                                    ctypes.byref(needed)), "读取令牌组")
    count = ctypes.c_uint32.from_buffer_copy(buf, 0).value
    for index in range(count):
        offset = 8 + index * 16
        ptr = ctypes.c_void_p.from_buffer_copy(buf, offset).value
        attrs = ctypes.c_uint32.from_buffer_copy(buf, offset + 8).value
        if attrs & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
            length = _adv.GetLengthSid(ctypes.c_void_p(ptr))
            dest = ctypes.create_string_buffer(length)
            _check(_adv.CopySid(length, dest, ctypes.c_void_p(ptr)), "复制 logon SID")
            return ctypes.cast(dest, ctypes.c_void_p)
    raise SandboxError("令牌里没有 logon SID，无法创建受限令牌")


def _merge_ace(path: str, sids: list[ctypes.c_void_p], mask: int = FILE_ALL_ACCESS) -> None:
    sd = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    err = _adv.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                     None, None, ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise SandboxError(f"读取 {path} 的 DACL 失败（{err}）")
    entries = (_EXPLICIT_ACCESS * len(sids))(*[
        _EXPLICIT_ACCESS(mask, GRANT_ACCESS, SUB_CONTAINERS_AND_OBJECTS_INHERIT,
                         _TRUSTEE(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN,
                                  ctypes.cast(sid, w.LPWSTR)))
        for sid in sids
    ])
    new_dacl = ctypes.c_void_p()
    err = _adv.SetEntriesInAclW(len(sids), entries, dacl, ctypes.byref(new_dacl))
    if err:
        raise SandboxError(f"合并 {path} 的 DACL 失败（{err}）")
    err = _adv.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                     None, None, new_dacl, None)
    if err:
        raise SandboxError(f"写回 {path} 的 DACL 失败（{err}）")
    if sd:
        _k32.LocalFree(sd)
    if new_dacl:
        _k32.LocalFree(new_dacl)


def _default_dacl_grant(token, sid) -> None:
    needed = w.DWORD()
    _adv.GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value)
    _check(_adv.GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, buf, needed.value,
                                    ctypes.byref(needed)), "读取默认 DACL")
    current = ctypes.c_void_p.from_buffer_copy(buf).value
    entry = _EXPLICIT_ACCESS(FILE_ALL_ACCESS, GRANT_ACCESS, 0,
                             _TRUSTEE(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_UNKNOWN,
                                      ctypes.cast(sid, w.LPWSTR)))
    new_dacl = ctypes.c_void_p()
    err = _adv.SetEntriesInAclW(1, ctypes.byref(entry), ctypes.c_void_p(current),
                                ctypes.byref(new_dacl))
    if err:
        raise SandboxError(f"合并令牌默认 DACL 失败（{err}）")
    info = (ctypes.c_void_p * 1)(new_dacl)
    _check(_adv.SetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, info,
                                    ctypes.sizeof(info)), "写回令牌默认 DACL")
    _k32.LocalFree(new_dacl)


def _set_low_integrity(token) -> None:
    low = _sid("S-1-16-4096")
    label = _TOKEN_MANDATORY_LABEL(_SID_AND_ATTRIBUTES(low, 0))
    _check(_adv.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL_CLASS,
                                    ctypes.byref(label), ctypes.sizeof(label)),
           "设置 Low 完整性")


def _label_low(path: str) -> None:
    from . import exec as exec_mod

    proc = exec_mod.run_simple(
        ["icacls", path, "/setintegritylevel", "(OI)(CI)L"],
        scrub=False, timeout=30,
    )
    if proc.returncode != 0:
        raise SandboxError(f"设置 {path} 的 Low 标签失败：{proc.stdout.strip()[:200]}")


def build_token(workspace: Path, mode: str):
    """创建受限令牌；mode=read-only 时不带能力 SID。"""
    token = w.HANDLE()
    _check(_adv.OpenProcessToken(_k32.GetCurrentProcess(), 0xF01FF, ctypes.byref(token)),
           "OpenProcessToken")
    cap = _sid(derive_sid(str(workspace)))
    everyone = _sid("S-1-1-0")
    logon = _logon_sid(token)
    restricting = [logon, everyone] + ([cap] if mode == "workspace-write" else [])

    sids = (_SID_AND_ATTRIBUTES * len(restricting))(
        *[_SID_AND_ATTRIBUTES(sid, 0) for sid in restricting]
    )
    new_token = w.HANDLE()
    flags = WRITE_RESTRICTED | SANDBOX_INERT | DISABLE_MAX_PRIVILEGE
    _check(_adv.CreateRestrictedToken(token, flags, 0, None, 0, None,
                                      len(restricting), sids, ctypes.byref(new_token)),
           "CreateRestrictedToken")
    _default_dacl_grant(new_token, cap if mode == "workspace-write" else everyone)
    _set_low_integrity(new_token)
    return new_token, cap


def spawn_confined(cmd: list[str], cwd: Path, mode: str) -> int:
    """在受限令牌下跑一个子进程；返回子进程退出码。"""
    if not _WIN:
        raise SandboxError("沙箱只支持 Windows")
    workspace = Path(cwd).resolve()
    temp = workspace / ".forchat-sandbox-tmp"
    temp.mkdir(parents=True, exist_ok=True)
    token, cap = build_token(workspace, mode)

    user = None
    try:
        base = w.HANDLE()
        _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0xF01FF, ctypes.byref(base))
        user = _token_sid(base, 1)
        _k32.CloseHandle(base)
    except SandboxError:
        user = None
    if mode == "workspace-write":
        # 能力 SID + 当前用户都显式授权（继承来的 ACE 在 SetNamedSecurityInfo 时会被剥掉）
        _merge_ace(str(workspace), [cap, user] if user else [cap])
        _merge_ace(str(temp), [cap, user] if user else [cap])
        _label_low(str(workspace))
        _label_low(str(temp))
    elif user:
        # read-only：显式补一条用户读 ACE（某些临时目录没有用户 ACE，Low 令牌读不了）
        _merge_ace(str(workspace), [user], FILE_GENERIC_READ | FILE_TRAVERSE)

    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise SandboxError("CreateJobObject 失败")
    info = _JOBOBJECT_EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _check(_k32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)),
        "SetInformationJobObject")
    _check(_k32.SetHandleInformation(job, 1, 1), "SetHandleInformation")

    import msvcrt

    si = _STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "winsta0\\default"
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = w.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno()))
    si.hStdOutput = w.HANDLE(msvcrt.get_osfhandle(sys.stdout.fileno()))
    si.hStdError = w.HANDLE(msvcrt.get_osfhandle(sys.stderr.fileno()))
    pi = _PROCESS_INFORMATION()
    command = subprocess.list2cmdline(cmd)
    creation = CREATE_SUSPENDED
    ok = _adv.CreateProcessAsUserW(
        token, None, ctypes.create_unicode_buffer(command), None, None, True,
        creation, None, str(cwd), ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise SandboxError(f"CreateProcessAsUser 失败（Win32 错误 {ctypes.get_last_error()}）")
    try:
        _check(_k32.AssignProcessToJobObject(job, pi.hProcess), "AssignProcessToJobObject")
        if _k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            raise SandboxError("ResumeThread 失败")
        _k32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
        code = w.DWORD()
        _k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        if pi.hProcess:
            _k32.CloseHandle(pi.hProcess)
        if pi.hThread:
            _k32.CloseHandle(pi.hThread)
        if job:
            _k32.CloseHandle(job)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        index = args.index("--")
    except ValueError:
        print("forchat-acl-run: 缺少 -- 命令分隔符", file=sys.stderr)
        return 127
    head, cmd = args[:index], args[index + 1:]
    if not cmd:
        print("forchat-acl-run: 没有要执行的命令", file=sys.stderr)
        return 127
    options = dict(zip(head[::2], head[1::2]))
    workspace = Path(options.get("--workspace", ".")).resolve()
    mode = options.get("--mode", "workspace-write")
    try:
        return spawn_confined(cmd, workspace, mode)
    except SandboxError as exc:
        print(f"forchat-acl-run: {exc}", file=sys.stderr)
        return 127
    except Exception as exc:                    # noqa: BLE001 - fail-closed
        print(f"forchat-acl-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
