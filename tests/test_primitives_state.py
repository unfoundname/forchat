"""原语层自检：运行时、配置、围栏、事件流、续跑、预算。

    python tests/test_primitives.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Replay, Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_runtime_serial_when_concurrency_one() -> None:
    """max_concurrent=1：两个异步分支必须串行（峰值 1）。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"]["max_concurrent"] = 1
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
                tool_chunks([("report", {"summary": "两个分支已排队"})]),
            ],
            "coder": [coder_reply, coder_reply],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder, delay=0.01), tmpdir())
    runtime.start("串行任务")

    assert runtime.scheduler.stats()["peak_running"] == 1, runtime.scheduler.stats()
    coders = [w for w in runtime.workers.values() if w.template == "coder"]
    assert len(coders) == 2 and all(w.status == "done" for w in coders)


def test_resume_restores_workers_and_seq() -> None:
    """同一 run 目录再打开：历史 worker / 报告可读，序号接着涨。"""
    workflow = WorkflowDef.load(MINIMAL)
    run_dir = tmpdir()

    first = TemplateResponder({"root": [tool_chunks([("report", {"summary": "第一轮"})])]})
    Runtime(workflow, FakeProvider(first), run_dir).start("第一轮")

    second = TemplateResponder({"root": [tool_chunks([("report", {"summary": "第二轮"})])]})
    provider2 = FakeProvider(second)
    resumed = Runtime(workflow, provider2, run_dir)
    assert "n001-root" in resumed.workers
    assert resumed.workers["n001-root"].report.summary == "第一轮"
    assert any(event["type"] == "run_resumed" for event in resumed.log.read())

    root2 = resumed.start("第二轮")
    assert root2.id.startswith("n002-"), root2.id
    system_prompt = provider2.seen[0][0]["content"]
    assert "历史运行" in system_prompt and "第一轮" in system_prompt
    assert root2.report.summary == "第二轮"


def test_artifact_indexed_into_report() -> None:
    """write_artifact 的产出自动进 report.artifacts，模型不用手抄路径。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["write_artifact", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("write_artifact", {"name": "note.txt", "content": "hello"})]),
                tool_chunks([("report", {"summary": "已交付"})]),
            ]
        }
    )
    run_dir = tmpdir()
    runtime = Runtime(workflow, FakeProvider(responder), run_dir)
    root = runtime.start("写附件")

    assert root.report.artifacts, root.report.to_dict()
    artifact = Path(run_dir) / root.report.artifacts[0]
    assert artifact.is_file()
    assert artifact.read_text(encoding="utf-8") == "hello"


def test_provider_error_fails_only_worker() -> None:
    """模型/网络层抛异常：worker 标记 failed，整次运行不崩。"""
    workflow = WorkflowDef.load(MINIMAL)

    class Boom:
        name = "boom"

        def stream(self, *args, **kwargs):
            raise RuntimeError("网络炸了")
            yield  # pragma: no cover

    runtime = Runtime(workflow, Boom(), tmpdir())
    root = runtime.start("任务")

    assert root.status == "failed"
    errors = runtime.log.query(type="worker_error")
    assert errors and "网络炸了" in errors[0]["error"]


def test_summary_has_totals() -> None:
    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder(
        {
            "root": [tool_chunks([("report", {"summary": "完成"})])],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
            "reviewer": [tool_chunks([("report", {"summary": "审查通过"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")
    summary = Replay(runtime.log).summary()
    assert "合计:" in summary
    assert "运行: status=" in summary
    assert "workers=1" in summary


def test_budget_fence() -> None:
    """token 预算极小：一轮后判废，事件里有 budget_exhausted。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["budget"] = {"tokens": 1, "wall_seconds": 600}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {"root": [tool_chunks([("read_state", {})]), tool_chunks([("report", {"summary": "不该到这"})])]}
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")

    assert root.status == "failed"
    events = runtime.log.query(type="budget_exhausted")
    assert events, "应当记录 budget_exhausted"
    assert runtime.workers[root.id].report.summary != "不该到这"


def test_truncated_response_is_recorded() -> None:
    """finish_reason=length：记 response_truncated，不把半截参数当完整调用。"""
    workflow = WorkflowDef.load(MINIMAL)
    chunks = [
        {"choices": [{"delta": {"content": "写到一半"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    runtime = Runtime(workflow, FakeProvider([chunks]), tmpdir())
    root = runtime.start("任务")

    assert runtime.log.query(type="response_truncated")
    assert root.status == "failed"
    assert "写到一半" in root.report.summary


def test_wait_workers_joins_async_branches() -> None:
    """spawn(wait=false) 之后用 wait_workers 汇合：root 的报告必须晚于所有子 worker 完成。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "wait_workers", "report"]
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
                tool_chunks([("wait_workers", {"timeout_seconds": 30})]),
                tool_chunks([("report", {"summary": "两个都完成"})]),
            ],
            "coder": [coder_reply, coder_reply],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder, delay=0.02), tmpdir())
    root = runtime.start("等待两个分支")

    coders = [w for w in runtime.workers.values() if w.template == "coder"]
    assert len(coders) == 2 and all(w.status == "done" for w in coders)

    events = runtime.log.read()
    report_index = max(
        i for i, e in enumerate(events)
        if e["type"] == "report" and e.get("worker_id") == root.id
    )
    done_indexes = [i for i, e in enumerate(events) if e["type"] == "worker_done"]
    assert done_indexes and max(done_indexes) < report_index


def test_total_budget_caps_whole_run() -> None:
    """整次运行的总 token 闸：超了停止本轮并拒绝新的派生。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"]["max_total_tokens"] = 30
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("read_state", {})]),
                tool_chunks([("read_state", {})]),
                tool_chunks([("report", {"summary": "不该到这"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")

    assert runtime.log.query(type="run_budget_exhausted"), "应当记录总预算用尽"
    finished = runtime.log.query(type="run_finished")[-1]
    assert finished["status"] == "failed", finished
    assert finished["total_tokens"] >= finished["budget_limit"], finished
    assert root.status == "failed"
    assert root.report.summary != "不该到这"

    # 预算用尽后，新派生必须被拒
    assert runtime._create_worker("coder", "太晚了", None, 1) is None
    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[-1]["reason"] == "total_budget"



def main() -> int:
    checks = [
        ("并发=1 时串行", test_runtime_serial_when_concurrency_one),
        ("续跑恢复历史 worker", test_resume_restores_workers_and_seq),
        ("附件自动进报告", test_artifact_indexed_into_report),
        ("provider 异常只失败单个 worker", test_provider_error_fails_only_worker),
        ("summary 合计行", test_summary_has_totals),
        ("预算围栏", test_budget_fence),
        ("截断响应被记录", test_truncated_response_is_recorded),
        ("wait_workers 汇合并行分支", test_wait_workers_joins_async_branches),
        ("总预算闸", test_total_budget_caps_whole_run),
    ]
    return run("原语状态/预算自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
