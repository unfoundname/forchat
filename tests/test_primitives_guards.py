"""原语层自检：运行时、配置、围栏、事件流、续跑、预算。

    python tests/test_primitives.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Replay, Runtime, WorkflowDef, WorkflowError
from forchat.primitives.store import EventLog
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_workflow_rejects_unknown_skill() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["skills"] = ["nope"]
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_skills={"code-review"})
    except WorkflowError as exc:
        assert "nope" in str(exc)
        return
    raise AssertionError("挂载不存在的技能应当报错")



def test_hook_child_status_surfaced() -> None:
    """子 worker 的 hook 派生的 worker 失败时，父 worker 必须看得见（否则 review 挂了被吞）。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["report"]
    raw["workers"]["coder"]["hooks"] = {
        "on_done": [{"spawn": "reviewer", "task": "审查", "wait": True}]
    }
    raw["workers"]["reviewer"]["tools"] = []
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    text_only = [
        {"choices": [{"delta": {"content": "reviewer 只说话不交报告"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "干活", "wait": True})]),
                tool_chunks([("report", {"summary": "收尾"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "coder 完成"})])],
            "reviewer": [text_only],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")

    spawn_result = next(
        event["result"] for event in runtime.log.query(type="tool_result") if event["tool"] == "spawn"
    )
    assert "reviewer" in spawn_result, spawn_result
    assert "failed" in spawn_result, spawn_result


def test_spawn_cannot_grant_beyond_scope() -> None:
    """父 worker 不能把超出自己范围的目录授权给子 worker。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["scope"] = [".worktrees/a"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([(
                    "spawn",
                    {"template": "coder", "task": "越权授权", "wait": True,
                     "paths": [".worktrees/b"]},
                )]),
                tool_chunks([("report", {"summary": "授权被拒后收尾"})]),
            ],
            "coder": [tool_chunks([("report", {"summary": "不该被创建"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("任务")

    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[0]["reason"] == "scope_grant", rejected
    assert len(runtime.workers) == 1, runtime.workers


def test_stop_worker_kills_running_child() -> None:
    """父 worker 可以叫停后台分支：子 worker 状态 killed，事件留痕。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "stop_worker", "wait_workers", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    long_stream = [
        {"choices": [{"delta": {"content": "x"}}]} for _ in range(200)
    ] + [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "慢任务", "wait": False})]),
                tool_chunks([("stop_worker", {"worker_id": "n002-coder"})]),
                tool_chunks([("wait_workers", {"timeout_seconds": 20})]),
                tool_chunks([("report", {"summary": "已叫停慢分支"})]),
            ],
            "coder": [long_stream],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder, delay=0.02), tmpdir())
    root = runtime.start("任务")

    coder = runtime.workers["n002-coder"]
    assert coder.status == "killed", coder.status
    assert runtime.log.query(type="worker_stop_requested")
    assert root.status == "done"
    summary = Replay(runtime.log).summary()
    assert "killed" in summary


def test_interrupted_worker_recovered_on_resume() -> None:
    """上次运行没写 worker_done 的 worker = 中断，续跑时要标记并可追溯到任务。"""
    from forchat.primitives.store import EventLog

    run_dir = tmpdir()
    log = EventLog(run_dir)
    log.append(
        "worker_created", worker_id="n001-root", template="root", depth=0,
        parent=None, task="上次没跑完的任务", scope=[],
    )
    # 故意不写 worker_done：模拟进程被杀

    workflow = WorkflowDef.load(MINIMAL)
    provider = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "续跑完成"})])]})
    )
    runtime = Runtime(workflow, provider, run_dir)

    assert runtime.workers["n001-root"].status == "interrupted"
    assert runtime.interrupted == ["n001-root"]

    runtime.start("继续")
    system = provider.seen[0][0]["content"]
    assert "上次运行中断" in system
    assert "上次没跑完的任务" in system


def test_event_log_rotation_and_integrity() -> None:
    """事件流超过阈值要滚动分段，read() 仍按顺序拼回全部事件。"""
    import os

    old = os.environ.get("FORCHAT_EVENT_LOG_MB")
    os.environ["FORCHAT_EVENT_LOG_MB"] = "0.004"
    try:
        run_dir = tmpdir()
        log = EventLog(run_dir)
        for index in range(300):
            log.append("noise", worker_id=f"w{index:03d}", payload="x" * 80)
        log.append("run_finished", status="done")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_EVENT_LOG_MB", None)
        else:
            os.environ["FORCHAT_EVENT_LOG_MB"] = old

    segments = sorted(run_dir.glob("events*.jsonl"))
    assert len(segments) >= 2, segments
    events = log.read()
    assert len(events) == 301, len(events)
    assert events[-1]["type"] == "run_finished"


def test_worker_retries_transient_error() -> None:
    """模板 retries=1：worker 遇到瞬时异常后自动重跑一次并成功。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["retries"] = 1
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, model, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("瞬时错误")
            yield from tool_chunks([("report", {"summary": "重试后成功"})])

    provider = Flaky()
    runtime = Runtime(workflow, provider, tmpdir())
    root = runtime.start("任务")

    assert provider.calls == 2
    assert runtime.log.query(type="worker_retry")
    assert root.status == "done"
    assert root.report.summary == "重试后成功"


def test_resume_worker_from_snapshot() -> None:
    """每轮快照 + --resume-worker 语义：从断点消息继续，而不是开新会话。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["max_rounds"] = 1
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    first = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("read_state", {})])]})
    )
    runtime = Runtime(workflow, first, run_dir)
    root = runtime.start("任务")

    assert root.status == "failed", root.status
    snapshot = run_dir / "workers" / "n001-root.jsonl"
    assert snapshot.is_file(), list((run_dir / "workers").iterdir())
    records = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]
    assert records and all(record["op"] in ("append", "reset") for record in records)
    # 第一条要带初始上下文，之后每轮只追加新增（通常 2 条）
    assert len(records[0]["messages"]) >= 2
    assert all(len(record["messages"]) <= 3 for record in records[1:]), records

    second = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "断点续跑完成"})])]})
    )
    resumed = Runtime(workflow, second, run_dir)
    worker = resumed.resume_worker("n001-root")

    assert worker.status == "done"
    assert worker.report.summary == "断点续跑完成"
    roles = [m["role"] for m in second.seen[0]]
    assert "assistant" in roles and "tool" in roles, roles
    assert resumed.log.query(type="worker_snapshot_loaded")



def main() -> int:
    checks = [
        ("workflow 拒绝未知技能", test_workflow_rejects_unknown_skill),
        ("test_hook_child_status_surfaced", test_hook_child_status_surfaced),
        ("越权授权被拒", test_spawn_cannot_grant_beyond_scope),
        ("stop_worker 叫停分支", test_stop_worker_kills_running_child),
        ("中断 worker 续跑可见", test_interrupted_worker_recovered_on_resume),
        ("事件流滚动与完整性", test_event_log_rotation_and_integrity),
        ("worker 级重试", test_worker_retries_transient_error),
        ("快照续跑单个 worker", test_resume_worker_from_snapshot),
    ]
    return run("原语围栏自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
