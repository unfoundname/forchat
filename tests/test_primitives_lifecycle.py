"""原语生命周期自检：restart（父 worker 对自己直接子 worker）+ 每 worker 事实。"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_worker_facts_are_per_worker_and_persist() -> None:
    """事实是"每个 worker 实例一份"，落盘长期保存；别的实例读不到。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["write_fact", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "写事实"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("write_fact", {"name": "构建命令", "content": "pytest -q"})]),
                    tool_chunks([("report", {"summary": "子完成"})]),
                ],
            }
        )
    )
    runtime = Runtime(workflow, provider, run_dir)
    runtime.start("任务")

    assert runtime.load_facts("n002-coder") == {"构建命令": "pytest -q"}
    assert runtime.load_facts("n001-root") == {}
    assert (run_dir / "facts" / "n002-coder.json").is_file()

    reopened = Runtime(workflow, provider, run_dir)
    assert reopened.load_facts("n002-coder") == {"构建命令": "pytest -q"}


def test_restart_inherits_worker_facts() -> None:
    """restart 是父 worker 决定：新实例的系统提示里带该 worker 原有事实。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "restart", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["write_fact", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    inner = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "写事实"})]),
                    tool_chunks([("restart", {"worker_id": "n002-coder"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("write_fact", {"name": "构建命令", "content": "pytest -q"})]),
                    tool_chunks([("report", {"summary": "第一代"})]),
                    tool_chunks([("report", {"summary": "第二代"})]),
                ],
            }
        )
    )

    class Recording:
        def __init__(self, provider):
            self.provider = provider
            self.calls: list[list[dict]] = []

        def stream(self, model, messages, tools=None):
            self.calls.append(copy.deepcopy(messages))
            return self.provider.stream(model, messages, tools)

    runtime = Runtime(workflow, Recording(inner), tmpdir())
    runtime.start("任务")

    with_facts = [
        call for call in runtime.provider.calls
        if "你的事实" in str(call[0]["content"])
    ]
    assert with_facts, "restart 的 agent 系统提示里应当带事实"
    assert "pytest -q" in with_facts[-1][0]["content"]


def test_restart_only_from_parent() -> None:
    """restart 只能由父 worker 发起：同级 worker 会被拒。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "wait_workers", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["restart", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "a", "wait": False}),
                                 ("spawn", {"template": "coder", "task": "b", "wait": False})]),
                    tool_chunks([("wait_workers", {"timeout_seconds": 20})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("restart", {"worker_id": "n003-coder"})]),
                    tool_chunks([("report", {"summary": "a 完成"})]),
                    tool_chunks([("report", {"summary": "b 完成"})]),
                ],
            }
        )
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")

    results = [event.get("result", "") for event in runtime.log.query(type="tool_result")]
    assert any("只能由父 worker" in item for item in results), results


def main() -> int:
    checks = [
        ("每 worker 事实、长期保存", test_worker_facts_are_per_worker_and_persist),
        ("restart 继承事实", test_restart_inherits_worker_facts),
        ("restart 只能父发起", test_restart_only_from_parent),
    ]
    return run("原语生命周期自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
