"""原语层自检：运行时、配置、围栏、事件流、续跑、预算。

    python tests/test_primitives.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_fork_and_restart_agents() -> None:
    """fork 继承快照对话；restart 是全新会话。"""
    import copy

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "wait_workers", "fork", "restart", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    inner = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "子任务"})]),
                    tool_chunks([("fork", {"worker_id": "n002-coder"})]),
                    tool_chunks([("restart", {"worker_id": "n002-coder"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("report", {"summary": "原始结论"})]),
                    tool_chunks([("report", {"summary": "fork 结论"})]),
                    tool_chunks([("report", {"summary": "restart 结论"})]),
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

    provider = Recording(inner)
    runtime = Runtime(workflow, provider, tmpdir())
    root = runtime.start("任务")

    assert root.status == "done"
    assert runtime.log.query(type="worker_forked")
    assert runtime.log.query(type="worker_restarted")

    coder_calls = [
        call for call in provider.calls
        if any("模板 coder" in str(message) for message in call)
    ]
    assert len(coder_calls) >= 3, [len(call) for call in coder_calls]

    fork_messages = coder_calls[1]
    assert any("原始结论" in str(message) for message in fork_messages)

    restart_messages = coder_calls[2]
    assert not any("原始结论" in str(message) for message in restart_messages)

def test_worker_facts_are_per_worker_and_persist() -> None:
    """事实是"每个 worker 一份"，落盘长期保存；别的 worker 读不到。"""
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

    # 新的 Runtime（模拟下次打开）仍读得到：长期存储
    reopened = Runtime(workflow, provider, run_dir)
    assert reopened.load_facts("n002-coder") == {"构建命令": "pytest -q"}

def test_restart_inherits_worker_facts() -> None:
    """restart 是同一个逻辑 worker：新 agent 的系统提示里带原有事实。"""
    import copy

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

def test_fork_copies_facts_to_new_branch() -> None:
    """fork 会复制源 worker 的事实；之后两条分支各写各的。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "fork", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["write_fact", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "写事实"})]),
                    tool_chunks([("fork", {"worker_id": "n002-coder"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("write_fact", {"name": "构建命令", "content": "pytest -q"})]),
                    tool_chunks([("report", {"summary": "第一代"})]),
                    tool_chunks([("report", {"summary": "fork 分支"})]),
                ],
            }
        )
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")

    assert runtime.load_facts("n002-coder") == {"构建命令": "pytest -q"}
    assert runtime.load_facts("n003-coder") == {"构建命令": "pytest -q"}

def main() -> int:
    checks = [
        ("fork / restart agent", test_fork_and_restart_agents),
        ("每 worker 事实、长期保存", test_worker_facts_are_per_worker_and_persist),
        ("restart 继承事实", test_restart_inherits_worker_facts),
        ("fork 复制事实", test_fork_copies_facts_to_new_branch),
    ]
    return run("原语生命周期自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
