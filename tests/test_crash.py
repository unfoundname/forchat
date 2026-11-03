"""崩溃注入：worker 子进程写到一半被 kill，事件流/恢复仍可用。"""
from __future__ import annotations

import json
import os
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


def test_worker_killed_mid_run_is_recoverable() -> None:
    workflow = WorkflowDef.load(MINIMAL)
    run_dir = tmpdir()
    log = EventLog(run_dir)
    log.append("worker_created", worker_id="crash-w", template="root",
               task="慢任务", parent=None, depth=0)

    chunks = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(200)]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    script = tmpdir() / "slow.json"
    script.write_text(json.dumps([chunks]), encoding="utf-8")

    payload = {
        "workflow": workflow.to_dict(),
        "base_dir": str(MINIMAL.parent),
        "run_dir": str(run_dir),
        "model": "fake",
        "facts_dir": "",
        "worker": {
            "id": "crash-w", "template": "root", "task": "慢任务",
            "parent_id": None, "depth": 0, "scope": [], "facts_owner": "",
        },
    }
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["FORCHAT_CHILD_SCRIPT"] = str(script)
    env["FORCHAT_CHILD_DELAY"] = "0.2"
    proc = subprocess.Popen(
        [sys.executable, "-m", "forchat.worker_proc"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
        env=env,
    )
    assert proc.stdin
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    time.sleep(0.9)
    proc.kill()
    proc.wait(timeout=10)

    # 事件流：最多允许最后一行是写到一半的残行，其余必须可解析
    lines = [line for line in (run_dir / "events.jsonl").read_text(
        encoding="utf-8", errors="replace").splitlines() if line.strip()]
    bad = 0
    for index, line in enumerate(lines):
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            assert index == len(lines) - 1, f"中间行损坏：{index}"
    assert bad <= 1

    events = EventLog(run_dir).read()
    assert events, "崩溃后事件流应仍可读"
    assert not [e for e in events if e.get("type") == "run_finished"]

    runtime = Runtime(workflow, FakeProvider([]), run_dir)
    assert runtime.workers["crash-w"].status == "interrupted"
    assert "crash-w" in runtime._interrupted


def main() -> int:
    return run("崩溃注入自检", [
        ("worker 半途被杀可恢复", test_worker_killed_mid_run_is_recoverable),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
