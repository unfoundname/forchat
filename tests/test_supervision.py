"""监督策略自检：超时先标记 suspect / 视察 / 人工重派，而不是自动重启。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks
from forchat.remote_queue import RemoteQueue


def _workflow(tools: list[str], can_spawn: list[str]) -> WorkflowDef:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = tools
    raw["workers"]["root"]["can_spawn"] = can_spawn
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["report"]
    raw["workers"]["coder"]["hooks"] = {}
    return WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)


def test_stale_lease_marks_suspect_not_requeue() -> None:
    root = tmpdir() / "remote"
    queue = RemoteQueue(root)
    queue.submit("t1", {"worker": {"id": "t1"}})
    leased = queue.lease("agent-a")
    assert leased is not None

    lease_path = root / "leases" / "t1.agent-a.json"
    old = time.time() - 3600
    os.utime(lease_path, (old, old))
    marked = queue.mark_suspects(timeout=60)

    assert marked == ["t1"]
    assert not queue.task_path("t1").is_file(), "不能自动重派"
    assert queue.suspects()[0]["task_id"] == "t1"
    assert queue.requeue("t1") is True, "视察后显式重派"
    assert queue.task_path("t1").is_file()
    assert not queue.suspects()


def test_inspect_worker_reports_running_child() -> None:
    workflow = _workflow(
        ["spawn", "inspect_worker", "wait_workers", "report"], ["coder"]
    )
    slow = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(40)]
    slow.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "慢任务",
                                             "wait": False})]),
                    tool_chunks([("inspect_worker", {"worker_id": "n002-coder"})]),
                    tool_chunks([("wait_workers", {"timeout_seconds": 10})]),
                    tool_chunks([("report", {"summary": "完成"})]),
                ],
                "coder": [slow],
            }
        ),
        delay=0.05,
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")

    inspect_results = [
        e for e in runtime.log.query(type="tool_result")
        if e.get("tool") == "inspect_worker"
    ]
    assert inspect_results, "应当调用过 inspect_worker"
    assert "n002-coder" in inspect_results[0]["result"]
    assert "task:" in inspect_results[0]["result"]


def test_watchdog_review_does_not_stop_by_default() -> None:
    workflow = _workflow(["read_state", "report"], [])
    stream = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(10)]
    stream.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    provider = FakeProvider(TemplateResponder({"root": [stream]}), delay=0.3)
    old_seconds = os.environ.get("FORCHAT_STUCK_SECONDS")
    old_action = os.environ.get("FORCHAT_STUCK_ACTION")
    os.environ["FORCHAT_STUCK_SECONDS"] = "0.2"
    os.environ.pop("FORCHAT_STUCK_ACTION", None)
    try:
        runtime = Runtime(workflow, provider, tmpdir())
        root = runtime.start("任务")
    finally:
        for key, old in (("FORCHAT_STUCK_SECONDS", old_seconds),
                         ("FORCHAT_STUCK_ACTION", old_action)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    events = runtime.log.query(type="stuck_detected")
    assert events and events[0]["action"] == "review", events
    assert not runtime.log.query(type="worker_hard_killed")
    assert root.status in ("failed", "done")


def main() -> int:
    checks = [
        ("超时只标 suspect", test_stale_lease_marks_suspect_not_requeue),
        ("inspect_worker 视察", test_inspect_worker_reports_running_child),
        ("看门狗默认不 stop", test_watchdog_review_does_not_stop_by_default),
    ]
    return run("监督策略自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
