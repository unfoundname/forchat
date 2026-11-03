"""整 worker 进程化自检：叶子 worker 在子进程里跑，stop 能直接 kill。"""
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


def _script_file(chunks_per_call: list[list[dict]]) -> Path:
    path = tmpdir() / "child_script.json"
    path.write_text(json.dumps(chunks_per_call, ensure_ascii=False), encoding="utf-8")
    os.environ["FORCHAT_CHILD_SCRIPT"] = str(path)
    return path


def test_leaf_worker_runs_in_subprocess() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["read", "report"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(
        raw, base_dir=MINIMAL.parent, known_tools={"read"}
    )

    _script_file([
        tool_chunks([("read", {"path": "README.md", "limit": 3})]),
        tool_chunks([("report", {"summary": "子进程完成"})]),
    ])
    parent = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "子任务"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ]
            }
        )
    )
    run_dir = tmpdir()
    try:
        runtime = Runtime(workflow, parent, run_dir, process_workers=True)
        root = runtime.start("任务")
    finally:
        os.environ.pop("FORCHAT_CHILD_SCRIPT", None)

    coder = runtime.workers["n002-coder"]
    detail = runtime.log.query(type="worker_error")
    assert coder.status == "done", (coder.status, coder.report, detail)
    assert coder.report.summary == "子进程完成"
    assert (run_dir / "workers" / "n002-coder.result.json").is_file()
    assert root.status == "done"
    # 子进程写的事件也在同一条流里
    assert runtime.log.query(type="worker_done", worker_id="n002-coder")


def test_process_worker_can_be_hard_killed() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "wait_workers", "stop_worker", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["report"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    long_stream = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(300)]
    long_stream.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    _script_file([long_stream])
    os.environ["FORCHAT_CHILD_DELAY"] = "0.05"
    parent = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([
                        ("spawn", {"template": "coder", "task": "长任务", "wait": False}),
                        ("stop_worker", {"worker_id": "n002-coder"}),
                    ]),
                    tool_chunks([("wait_workers", {"timeout_seconds": 20})]),
                    tool_chunks([("report", {"summary": "已叫停"})]),
                ]
            }
        )
    )
    run_dir = tmpdir()
    started = time.time()
    try:
        runtime = Runtime(workflow, parent, run_dir, process_workers=True)
        runtime.start("任务")
    finally:
        os.environ.pop("FORCHAT_CHILD_SCRIPT", None)
        os.environ.pop("FORCHAT_CHILD_DELAY", None)

    elapsed = time.time() - started
    coder = runtime.workers["n002-coder"]
    assert coder.status == "killed", coder.status
    assert elapsed < 8, elapsed


def main() -> int:
    checks = [
        ("叶子 worker 子进程执行", test_leaf_worker_runs_in_subprocess),
        ("worker 子进程硬杀", test_process_worker_can_be_hard_killed),
    ]
    return run("worker 进程化自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
