"""受限子进程自检：超时、脱敏环境、内存上限（尽力而为）。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat import subproc


def test_timeout_raises() -> None:
    work = tmpdir()
    script = work / "sleep.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    started = time.time()
    try:
        subproc.run([sys.executable, str(script)], cwd=work, timeout=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("应当在超时后抛 TimeoutExpired")
    assert time.time() - started < 4


def test_env_is_scrubbed() -> None:
    work = tmpdir()
    script = work / "env.py"
    script.write_text(
        "import os\nprint(os.environ.get('DEEPSEEK_API_KEY', 'missing'))\n", encoding="utf-8"
    )
    old = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "should-not-leak"
    try:
        proc = subproc.run([sys.executable, str(script)], cwd=work, timeout=10)
    finally:
        if old is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = old
    assert proc.returncode == 0, proc.stderr
    assert "missing" in proc.stdout


def test_memory_limit_best_effort() -> None:
    """申请远超上限的内存：进程应失败（被 Job Object 杀或抛 MemoryError）。"""
    work = tmpdir()
    script = work / "hog.py"
    script.write_text(
        "data = bytearray(800 * 1024 * 1024)\nprint(len(data))\n", encoding="utf-8"
    )
    proc = subproc.run([sys.executable, str(script)], cwd=work, timeout=30, memory_mb=128)
    assert proc.returncode != 0 or "MemoryError" in (proc.stderr or ""), (
        proc.returncode,
        (proc.stderr or "")[:200],
    )


def test_cancel_kills_running_tool_process() -> None:
    """worker 被 stop 时，正在跑的工具子进程被直接 kill。"""
    import threading

    work = tmpdir()
    script = work / "sleep.py"
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

    outcome: dict = {}

    def runner() -> None:
        from forchat import subproc as sub

        with sub.worker_context("w1"):
            try:
                sub.run([sys.executable, str(script)], cwd=work, timeout=30)
            except sub.Cancelled:
                outcome["cancelled"] = True

    thread = threading.Thread(target=runner)
    thread.start()
    time.sleep(0.4)
    killed = subproc.cancel_worker("w1")
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert outcome.get("cancelled") is True
    assert killed >= 1


def test_force_stop_calls_process_canceller() -> None:
    """Runtime.force_stop 要把取消传给工具子进程。"""
    from _support import MINIMAL, tmpdir

    from forchat.primitives import Runtime, WorkflowDef
    from forchat.primitives.types import WorkerInstance
    from forchat.providers import FakeProvider

    calls: list[str] = []
    workflow = WorkflowDef.load(MINIMAL)
    runtime = Runtime(workflow, FakeProvider([]), tmpdir(), cancel_processes=calls.append)
    worker = WorkerInstance(id="n001-root", template="root", task="t", status="running")
    runtime.workers[worker.id] = worker

    runtime.force_stop(worker.id)
    assert calls == [worker.id]


def main() -> int:
    checks = [
        ("超时", test_timeout_raises),
        ("环境脱敏", test_env_is_scrubbed),
        ("内存上限（best-effort）", test_memory_limit_best_effort),
        ("取消 kill 工具子进程", test_cancel_kills_running_tool_process),
        ("force_stop 联动取消", test_force_stop_calls_process_canceller),
    ]
    return run("受限子进程自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
