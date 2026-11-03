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

def test_runtime_spawns_and_reports() -> None:
    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "写 hello", "wait": True})]),
                tool_chunks([("report", {"summary": "总完成"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "hello ok"})])],
            "reviewer": [tool_chunks([("report", {"summary": "review ok"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("上级任务")

    assert root.status == "done"
    assert len(runtime.workers) == 3, f"期望 3 个worker，实际 {runtime.workers}"
    assert runtime.workers["n001-root"].report.summary == "总完成"
    assert any(k.startswith("n003-reviewer") for k in runtime.workers)
    types = [event["type"] for event in runtime.log.read()]
    assert "spawn_rejected" not in types
    assert "worker_done" in types and types[-1] == "run_finished"

def test_leaf_cannot_spawn() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["coder"]["can_spawn"] = []
    raw["workers"]["coder"]["tools"] = ["spawn", "report"]
    raw["workers"]["coder"]["max_rounds"] = 2
    raw["workers"]["coder"]["hooks"] = {}          # 排除 on_done 的合法派生，只留越权那一次
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "试图继续派活", "wait": True})]),
                tool_chunks([("report", {"summary": "总完成"})]),
            ],
            "coder": [
                tool_chunks([("spawn", {"template": "reviewer", "task": "越权", "wait": True})]),
                tool_chunks([("report", {"summary": "只能自己收尾"})]),
            ],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("根任务")

    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[0]["reason"] == "not_allowed"
    assert len(runtime.workers) == 2, f"越权派生不应产生worker：{list(runtime.workers)}"

def test_max_depth_fence() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"]["max_depth"] = 0
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "太深了", "wait": True})]),
                tool_chunks([("report", {"summary": "被深度限制"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("根任务")
    assert runtime.log.query(type="spawn_rejected")[0]["reason"] == "max_depth"
    assert len(runtime.workers) == 1

def test_max_workers_fence() -> None:
    """worker 数量上限：超出的派生被拒，不会把资源吃光。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"]["max_workers"] = 2
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "A", "wait": True})]),
                tool_chunks([("spawn", {"template": "coder", "task": "B", "wait": True})]),
                tool_chunks([("report", {"summary": "只成功派了一个"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")

    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[0]["reason"] == "max_workers", rejected
    assert len(runtime.workers) == 2, runtime.workers

def test_state_persisted_and_reloaded() -> None:
    """plan/笔记落盘：同一个 run 目录重新打开能读回。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["write_note", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("write_note", {"key": "plan", "value": "三步走"})]),
                tool_chunks([("report", {"summary": "写完了"})]),
            ]
        }
    )
    Runtime(workflow, FakeProvider(responder), run_dir).start("写下 plan")

    state_file = Path(run_dir) / "state.json"
    assert state_file.is_file()
    assert json.loads(state_file.read_text(encoding="utf-8"))["plan"] == "三步走"

    reloaded = Runtime(workflow, FakeProvider([]), run_dir)
    assert reloaded.state["plan"] == "三步走"

def test_no_report_means_failed() -> None:
    """只说话不交报告 = 未完成（不是 done）。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    text_only = [
        {"choices": [{"delta": {"content": "我做完了，但没调用 report"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    runtime = Runtime(workflow, FakeProvider([text_only]), tmpdir())
    root = runtime.start("任务")
    assert root.status == "failed"
    assert "report" in (root.report.summary or "")

def test_on_report_hook() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {"on_report": [{"action": "notify"}]}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder({"root": [tool_chunks([("report", {"summary": "ok"})])]})
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")
    hooks = [e for e in runtime.log.query(type="hook_action") if e.get("hook") == "on_report"]
    assert hooks, runtime.log.query(type="hook_action")

def test_on_start_and_on_fail_hooks() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {
        "on_start": [{"action": "notify"}],
        "on_fail": [{"action": "notify"}],
    }
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    text_only = [
        {"choices": [{"delta": {"content": "没有调用 report"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    runtime = Runtime(workflow, FakeProvider([text_only]), tmpdir())
    root = runtime.start("任务")

    assert root.status == "failed"
    hooks = [event.get("hook") for event in runtime.log.query(type="hook_action")]
    assert "on_start" in hooks, hooks
    assert "on_fail" in hooks, hooks

def test_unlimited_rounds_by_default() -> None:
    """不写 max_rounds = 不限轮数；模型可以连续多轮读状态，最后交报告。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"].pop("max_rounds", None)
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("read_state", {})]),
                tool_chunks([("read_state", {})]),
                tool_chunks([("read_state", {})]),
                tool_chunks([("report", {"summary": "多轮后交付"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")
    assert root.status == "done"
    assert root.report.summary == "多轮后交付"

def test_inline_role_template() -> None:
    """模板不写 role_file，直接内联 role 字符串也要能跑。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    root = raw["workers"]["root"]
    root.pop("role_file", None)
    root["role"] = "你是内联角色，收到任务后直接 report。"
    root["can_spawn"] = []
    root["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "ok"})])]})
    )
    Runtime(workflow, provider, tmpdir()).start("任务")
    system = provider.seen[0][0]["content"]
    assert "你是内联角色" in system

def test_read_worker_report() -> None:
    """父 worker 能直接读子 worker 的报告，不用翻事件流。"""
    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "子任务", "wait": True})]),
                tool_chunks([("report", {"summary": "总完成"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "coder 完成", "unresolved": ["x"]})])],
            "reviewer": [tool_chunks([("report", {"summary": "审查通过"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")

    data = json.loads(
        runtime._tool_read_worker_report(root, workflow.workers["root"], {"worker_id": "n002-coder"})
    )
    assert data["summary"] == "coder 完成"
    assert data["status"] == "done"
    assert data["unresolved"] == ["x"]

def test_scheduler_concurrency_limit() -> None:
    from forchat.primitives.scheduler import Scheduler

    scheduler = Scheduler(max_concurrent=1)
    order: list[str] = []

    def job(index: int) -> None:
        order.append(f"start{index}")
        time.sleep(0.03)
        order.append(f"end{index}")

    futures = [scheduler.submit(job, index) for index in range(3)]
    for future in futures:
        future.result()
    scheduler.shutdown()

    assert order == ["start0", "end0", "start1", "end1", "start2", "end2"], order
    assert scheduler.stats()["peak_running"] == 1

def test_parallel_spawn_and_log_integrity() -> None:
    """wait=False 的两个分支真并行；并行写事件流不能写出半行。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    coder_reply = tool_chunks([("report", {"summary": "分支完成"})])
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks(
                    [
                        ("spawn", {"template": "coder", "task": "A", "wait": False}),
                        ("spawn", {"template": "coder", "task": "B", "wait": False}),
                    ]
                ),
                tool_chunks([("report", {"summary": "两个分支已启动"})]),
            ],
            "coder": [coder_reply, coder_reply],
        }
    )
    run_dir = tmpdir()
    runtime = Runtime(
        workflow, FakeProvider(responder, delay=0.02), run_dir, max_concurrent=2
    )
    runtime.start("并行任务")

    coders = [w for w in runtime.workers.values() if w.template == "coder"]
    assert len(coders) == 2, runtime.workers
    assert all(w.status == "done" for w in coders)
    assert runtime.scheduler.stats()["peak_running"] == 2, runtime.scheduler.stats()

    lines = (Path(run_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip():
            json.loads(line)          # 半行会在这里抛 JSONDecodeError

def test_contracts_frozen_and_injected() -> None:
    """契约冻结：写一次后拒绝覆盖；replace=true 升版本；新 agent 提示里可见。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["write_contract", "read_contracts", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("write_contract", {"key": "GET /api/videos", "content": "返回 {items: [...]}"})]),
                    tool_chunks([("write_contract", {"key": "GET /api/videos", "content": "改掉"})]),
                    tool_chunks([("write_contract", {"key": "GET /api/videos", "content": "v2", "replace": True})]),
                    tool_chunks([("report", {"summary": "契约完成"})]),
                ]
            }
        )
    )
    run_dir = tmpdir()
    runtime = Runtime(workflow, provider, run_dir)
    runtime.start("任务")

    rejected = [e for e in runtime.log.query(type="tool_result") if "已冻结" in str(e.get("result"))]
    assert rejected, "第二次写入应当被拒"
    written = runtime.log.query(type="contract_written")
    assert [e["version"] for e in written] == [1, 2]
    assert runtime.state["contracts"]["GET /api/videos"]["version"] == 2

    second = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "新任务"})])]})
    )
    resumed = Runtime(workflow, second, run_dir)
    resumed.start("继续")
    system = second.seen[0][0]["content"] if second.seen else ""
    assert "冻结契约" in system and "GET /api/videos" in system


def main() -> int:
    checks = [
        ("运行时：派生 + 报告 + hook", test_runtime_spawns_and_reports),
        ("围栏：叶子不能派生", test_leaf_cannot_spawn),
        ("围栏：最大深度", test_max_depth_fence),
        ("围栏：worker 数量上限", test_max_workers_fence),
        ("状态落盘与重载", test_state_persisted_and_reloaded),
        ("未调 report = 失败", test_no_report_means_failed),
        ("on_report hook", test_on_report_hook),
        ("hook：on_start / on_fail", test_on_start_and_on_fail_hooks),
        ("默认不限轮数", test_unlimited_rounds_by_default),
        ("内联 role 模板", test_inline_role_template),
        ("父 worker 读子报告", test_read_worker_report),
        ("调度器并发上限", test_scheduler_concurrency_limit),
        ("并行派生与日志完整性", test_parallel_spawn_and_log_integrity),
        ("契约冻结与注入", test_contracts_frozen_and_injected),
    ]
    return run("原语流程自检", checks)

if __name__ == "__main__":
    raise SystemExit(main())
