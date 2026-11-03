"""写锁自检：同进程排队、超时报错、跨进程互斥。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat import registry, tools  # noqa: F401
from forchat.tools._locks import path_lock


def _write(path: Path, content: str, **extra) -> str:
    payload = {"path": str(path), "content": content, **extra}
    return registry.execute("write_file", json.dumps(payload))


def test_write_waits_for_lock() -> None:
    work = tmpdir()
    target = work / "a.txt"
    out: list[str] = []
    with path_lock(target):
        thread = threading.Thread(target=lambda: out.append(_write(target, "x")))
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive(), "锁被占用时 write_file 应当等待"
        assert not target.exists(), "等锁期间不能写入"
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert target.read_text(encoding="utf-8") == "x"


def test_lock_timeout_returns_error() -> None:
    work = tmpdir()
    target = work / "b.txt"
    old = os.environ.get("FORCHAT_WRITE_LOCK_TIMEOUT")
    os.environ["FORCHAT_WRITE_LOCK_TIMEOUT"] = "0.15"
    try:
        with path_lock(target):
            out = _write(target, "x")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_WRITE_LOCK_TIMEOUT", None)
        else:
            os.environ["FORCHAT_WRITE_LOCK_TIMEOUT"] = old
    assert "请稍后重试" in out, out
    assert not target.exists()


def test_cross_process_lock() -> None:
    work = tmpdir()
    target = work / "c.txt"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from forchat.tools._locks import path_lock\n"
        "with path_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(1.0)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(target)],
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert child.stdout
    assert child.stdout.readline().strip() == "locked"
    started = time.time()
    out = _write(target, "x")
    elapsed = time.time() - started
    child.wait(timeout=10)
    assert "已写入" in out, out
    assert elapsed >= 0.6, elapsed


def main() -> int:
    checks = [
        ("同进程等待锁", test_write_waits_for_lock),
        ("等锁超时报错", test_lock_timeout_returns_error),
        ("跨进程互斥", test_cross_process_lock),
    ]
    return run("写锁自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
