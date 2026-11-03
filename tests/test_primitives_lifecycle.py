"""原语生命周期自检：worker 自重启（同实例、清空对话、事实保留）+ 每 worker 事实。"""
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


def test_restart_is_self_and_reuses_instance() -> None:
    """restart 是 worker 自己重启自己：不新增 worker，清空对话，事实保留。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["write_fact", "restart", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    inner = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "写事实"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("write_fact", {"name": "构建命令", "content": "pytest -q"})]),
                    tool_chunks([("restart", {})]),
                    tool_chunks([("report", {"summary": "重启后完成"})]),
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

    coder = runtime.workers["n002-coder"]
    assert coder.status == "done"
    assert coder.report.summary == "重启后完成"
    assert len(runtime.workers) == 2                      # 没有新增 worker
    assert runtime.log.query(type="worker_restarted")

    coder_calls = [
        call for call in runtime.provider.calls
        if any("模板 coder" in str(message) for message in call)
    ]
    after_restart = coder_calls[-1]
    assert len(after_restart) == 2                        # system + task，旧对话没了
    assert "pytest -q" in after_restart[0]["content"]     # 事实保留


def test_restart_can_replace_task() -> None:
    """restart(task=...) 用新任务重开同一个 worker。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["restart", "report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("spawn", {"template": "coder", "task": "旧任务"})]),
                    tool_chunks([("report", {"summary": "根完成"})]),
                ],
                "coder": [
                    tool_chunks([("restart", {"task": "新任务"})]),
                    tool_chunks([("report", {"summary": "新任务完成"})]),
                ],
            }
        )
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")

    coder = runtime.workers["n002-coder"]
    assert coder.task == "新任务"
    assert coder.report.summary == "新任务完成"


def main() -> int:
    checks = [
        ("每 worker 事实、长期保存", test_worker_facts_are_per_worker_and_persist),
        ("restart 同实例自重启", test_restart_is_self_and_reuses_instance),
        ("restart 可换任务", test_restart_can_replace_task),
    ]
    return run("原语生命周期自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
