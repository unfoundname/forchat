"""Windows ACL 沙箱自检（非 Windows 直接跳过）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat import subproc, tools  # noqa: F401

WINDOWS = sys.platform == "win32"

CHILD = """import pathlib
import sys

work = pathlib.Path(sys.argv[1])
outside = pathlib.Path(sys.argv[2])
try:
    (work / "in.txt").write_text("x", encoding="utf-8")
    print("IN-OK")
except OSError as exc:
    print("IN-FAIL", exc)
try:
    (outside / "out.txt").write_text("x", encoding="utf-8")
    print("OUT-OK")
except OSError as exc:
    print("OUT-FAIL", exc)
"""


def _run_child(work: Path, outside: Path, *, mode: str = "workspace-write",
               sandbox: bool = True, cwd: Path | None = None,
               workspace_override: Path | None = None):
    script = (work if work.is_dir() else tmpdir()) / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ)
    if sandbox:
        env["FORCHAT_SANDBOX"] = "acl"
        env["FORCHAT_SANDBOX_WORKSPACE"] = str(workspace_override or work)
        env["FORCHAT_SANDBOX_MODE"] = mode
    else:
        env.pop("FORCHAT_SANDBOX", None)
    return subproc.run(
        [sys.executable, str(script), str(work), str(outside)],
        cwd=cwd or work, timeout=60, env=env,
    )


def test_workspace_write_allowed_outside_denied() -> None:
    work, outside = tmpdir(), tmpdir()
    proc = _run_child(work, outside)
    assert "IN-OK" in proc.stdout, proc.stdout + proc.stderr
    assert (work / "in.txt").is_file()
    assert not (outside / "out.txt").exists(), "工作区外写入必须被拒绝"
    assert "OUT-FAIL" in proc.stdout


def test_read_only_denies_workspace_write() -> None:
    work, outside = tmpdir(), tmpdir()
    proc = _run_child(work, outside, mode="read-only")
    assert not (work / "in.txt").exists(), "read-only 下工作区也不能写"
    assert "IN-FAIL" in proc.stdout, proc.stdout + proc.stderr


def test_fail_closed_when_grant_fails() -> None:
    bad = tmpdir() / "not-a-dir"
    bad.write_text("x", encoding="utf-8")
    work = tmpdir()
    try:
        _run_child(work, tmpdir(), cwd=work, workspace_override=bad)
    except subproc.SandboxUnavailable as exc:
        assert "forchat-acl-run" in str(exc)
    else:
        raise AssertionError("沙箱启动失败时必须 fail-closed，不能退化成普通进程")


def test_sandbox_off_runs_normally() -> None:
    work, outside = tmpdir(), tmpdir()
    _run_child(work, outside, sandbox=False)
    assert (work / "in.txt").is_file()
    assert (outside / "out.txt").is_file(), "关闭沙箱时不限制写入"


def main() -> int:
    if not WINDOWS:
        print("跳过：ACL 沙箱仅支持 Windows")
        return 0
    checks = [
        ("工作区可写/外部拒绝", test_workspace_write_allowed_outside_denied),
        ("read-only 全拒", test_read_only_denies_workspace_write),
        ("授权失败 fail-closed", test_fail_closed_when_grant_fails),
        ("关闭沙箱不受限", test_sandbox_off_runs_normally),
    ]
    return run("Windows ACL 沙箱自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
