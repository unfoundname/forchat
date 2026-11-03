"""进程出口自检：环境脱敏、worker 取消 kill。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run

from forchat import exec as exec_mod


def test_scrub_env() -> None:
    code = "import os; print(os.environ.get('FORCHAT_TEST_SECRET', 'absent'))"
    env = dict(os.environ)
    env["FORCHAT_TEST_SECRET"] = "leak"
    proc = exec_mod.spawn([sys.executable, "-c", code], env=env, scrub=True)
    out, _ = proc.communicate(timeout=30)
    assert "absent" in out, out

    proc = exec_mod.spawn([sys.executable, "-c", code], env=env, scrub=False)
    out, _ = proc.communicate(timeout=30)
    assert "leak" in out, out


def test_kill_worker() -> None:
    proc = exec_mod.spawn(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        env=dict(os.environ), scrub=False, worker_id="w-exec",
    )
    assert exec_mod.kill_worker("w-exec") >= 1
    proc.wait(timeout=5)
    assert proc.poll() is not None


def test_child_env_is_utf8() -> None:
    """所有子进程统一 UTF-8 模式，避免 Windows 代码页导致中文解码失败。"""
    code = "import os; print(os.environ.get('PYTHONUTF8'), os.environ.get('PYTHONIOENCODING'))"
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    proc = exec_mod.spawn([sys.executable, "-c", code], env=env, scrub=False)
    out, _ = proc.communicate(timeout=30)
    assert "1" in out and "utf-8" in out, out


def main() -> int:
    checks = [
        ("环境脱敏", test_scrub_env),
        ("按 worker kill", test_kill_worker),
        ("子进程 UTF-8", test_child_env_is_utf8),
    ]
    return run("进程出口自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
