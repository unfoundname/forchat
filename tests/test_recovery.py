"""崩溃/半写状态自检：原子写在 kill 下不产生半截目标文件；残行可恢复。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.primitives.store import EventLog
from forchat.providers import FakeProvider

ROOT = Path(__file__).resolve().parents[1]


def test_atomic_write_survives_kill() -> None:
    """子进程反复原子写，随机时刻 kill：目标文件只能是完整 A 或完整 B。"""
    work = tmpdir()
    target = work / "atomic.txt"
    child = work / "writer.py"
    child.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from forchat.tools._files import write_text\n"
        "from pathlib import Path\n"
        f"target = Path(r'{target}')\n"
        "i = 0\n"
        "while True:\n"
        "    write_text(target, ('A' if i % 2 == 0 else 'B') * 200000)\n"
        "    i += 1\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([sys.executable, str(child)], cwd=ROOT)
    time.sleep(1.0)
    proc.kill()
    proc.wait(timeout=10)

    if target.exists():
        content = target.read_text(encoding="utf-8")
        assert content in ("A" * 200000, "B" * 200000), "目标文件出现了半截内容"


def test_partial_event_log_and_state_are_tolerated() -> None:
    run_dir = tmpdir()
    good = json.dumps({"type": "worker_created", "worker_id": "w1",
                       "template": "root", "task": "t"}, ensure_ascii=False)
    (run_dir / "events.jsonl").write_text(
        good + "\n" + '{"type": "worker_st', encoding="utf-8"
    )
    events = EventLog(run_dir).read()
    assert [e.get("type") for e in events] == ["worker_created"]

    (run_dir / "state.json").write_text('{"plan": "半截', encoding="utf-8")
    workflow = WorkflowDef.load(MINIMAL)
    runtime = Runtime(workflow, FakeProvider([]), run_dir)
    assert runtime.state == {} or isinstance(runtime.state, dict)


def main() -> int:
    return run("崩溃/半写状态自检", [
        ("原子写在 kill 下无半截", test_atomic_write_survives_kill),
        ("残行/残 state 可恢复", test_partial_event_log_and_state_are_tolerated),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
