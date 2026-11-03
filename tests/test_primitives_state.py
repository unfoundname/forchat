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


def test_budget_is_soft_accounting() -> None:
    """预算是记账不是闸：token 预算超了也继续跑，只记 budget_exceeded。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["budget"] = {"tokens": 1, "wall_seconds": 600}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {"root": [tool_chunks([("read_state", {})]), tool_chunks([("report", {"summary": "超额也完成"})])]}
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")

    assert root.status == "done"
    assert root.report.summary == "超额也完成"
    assert runtime.log.query(type="budget_exceeded"), "超软预算应当留事件"
    assert not runtime.log.query(type="budget_exhausted")


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


def test_total_budget_stops_new_workers_only() -> None:
    """整轮 token 超了只拒绝新派生，正在跑的 worker 继续收尾（记账不中断）。"""
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
                tool_chunks([("report", {"summary": "跑完收尾"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    root = runtime.start("任务")

    assert runtime.log.query(type="run_budget_exceeded"), "应当记录总预算超出"
    finished = runtime.log.query(type="run_finished")[-1]
    assert finished["status"] == "done", finished
    assert finished["total_tokens"] >= finished["budget_limit"], finished
    assert root.status == "done"
    assert root.report.summary == "跑完收尾"

    # 预算超了以后，新派生必须被拒
    assert runtime._create_worker("coder", "太晚了", None, 1) is None
    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[-1]["reason"] == "total_budget"



def test_auto_resume_restores_tree_from_snapshots() -> None:
    """崩溃后 --resume：根从快照续跑，已完成的分支不重跑。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report", "read_state"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    first = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "子任务", "wait": False})]),
                tool_chunks([("report", {"summary": "原来就完成"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
        }
    )
    original = Runtime(workflow, FakeProvider(first), run_dir)
    original.start("根任务")
    assert original.workers["n001-root"].status == "done"
    assert original.workers["n002-coder"].status == "done"

    # 模拟进程崩溃：抹掉根的结束事件，但保留每轮快照
    events_path = run_dir / "events.jsonl"
    kept = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("worker_id") == "n001-root" and event.get("type") in ("worker_done", "report"):
            continue
        kept.append(line)
    events_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    second = TemplateResponder(
        {"root": [tool_chunks([("report", {"summary": "恢复后完成"})])]}
    )
    resumed = Runtime(workflow, FakeProvider(second), run_dir)
    assert resumed.interrupted == ["n001-root"], resumed.interrupted

    root = resumed.auto_resume("继续")
    assert root is not None and root.id == "n001-root"
    assert root.status == "done"
    assert root.report.summary == "恢复后完成"
    assert resumed.log.query(type="auto_resume")
    # 已完成的分支没有被重新创建
    created = [
        event for event in resumed.log.read()
        if event.get("type") == "worker_created" and event.get("worker_id") == "n002-coder"
    ]
    assert len(created) == 1


def test_replay_graph_exposes_mesh() -> None:
    """给前端的运行图：节点带状态/模板/摘要，边是父子派生关系。"""
    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "子任务"})]),
                tool_chunks([("report", {"summary": "根完成"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
            "reviewer": [tool_chunks([("report", {"summary": "审完"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")

    graph = Replay(runtime.log).graph()
    ids = {node["id"] for node in graph["nodes"]}
    assert "n001-root" in ids and len(ids) >= 2
    assert any(edge["from"] == "n001-root" for edge in graph["edges"])
    root_node = next(node for node in graph["nodes"] if node["id"] == "n001-root")
    assert root_node["template"] == "root"
    assert root_node["status"] == "done"
    assert root_node["summary"] == "根完成"


def main() -> int:
    checks = [
        ("并发=1 时串行", test_runtime_serial_when_concurrency_one),
        ("续跑恢复历史 worker", test_resume_restores_workers_and_seq),
        ("附件自动进报告", test_artifact_indexed_into_report),
        ("崩溃后自动恢复整棵树", test_auto_resume_restores_tree_from_snapshots),
        ("运行图（节点+边）", test_replay_graph_exposes_mesh),
        ("provider 异常只失败单个 worker", test_provider_error_fails_only_worker),
        ("summary 合计行", test_summary_has_totals),
        ("预算只记账不中断", test_budget_is_soft_accounting),
        ("截断响应被记录", test_truncated_response_is_recorded),
        ("wait_workers 汇合并行分支", test_wait_workers_joins_async_branches),
        ("总预算只拦新派生", test_total_budget_stops_new_workers_only),
    ]
    return run("原语状态/预算自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
