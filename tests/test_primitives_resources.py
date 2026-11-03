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



def test_context_trimmed_for_long_sessions() -> None:
    """长会话必须裁剪上下文：保留 system+任务+最近轮次，并留事件。"""
    import os

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    turns = [tool_chunks([("read_state", {})]) for _ in range(20)]
    turns.append(tool_chunks([("report", {"summary": "裁剪后完成"})]))
    provider = FakeProvider(TemplateResponder({"root": turns}))

    old = os.environ.get("FORCHAT_CONTEXT_MESSAGES")
    os.environ["FORCHAT_CONTEXT_MESSAGES"] = "10"
    try:
        runtime = Runtime(workflow, provider, tmpdir())
        root = runtime.start("任务")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_CONTEXT_MESSAGES", None)
        else:
            os.environ["FORCHAT_CONTEXT_MESSAGES"] = old

    assert root.status == "done", root.status
    compacted = runtime.log.query(type="context_compacted")
    assert compacted
    # 被裁掉的对话要归档 + 有确定性摘要，不是简单丢弃
    archive = runtime.dir / compacted[0]["archive"]
    assert archive.is_file()
    digest = str(archive.read_text(encoding="utf-8"))
    assert "read_state" in digest
    assert max(len(seen) for seen in provider.seen) <= 12

def test_thinking_logging_is_opt_in() -> None:
    """thinking 默认不落事件（1B token 级运行会把事件流写爆），要开才记。"""
    import os

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "想"}}]},
        {"choices": [{"delta": {"reasoning_content": "一下"}}]},
        {"choices": [{"delta": {"reasoning_content": "。"}}]},
        *tool_chunks([("report", {"summary": "ok"})]),
    ]

    def run_once() -> Runtime:
        provider = FakeProvider(TemplateResponder({"root": [chunks]}))
        runtime = Runtime(workflow, provider, tmpdir())
        runtime.start("任务")
        return runtime

    assert not run_once().log.query(type="thinking")

    old = os.environ.get("FORCHAT_LOG_THINKING")
    os.environ["FORCHAT_LOG_THINKING"] = "1"
    try:
        events = run_once().log.query(type="thinking")
        assert len(events) == 1, "整轮 thinking 要合并成一条事件"
        assert events[0]["text"] == "想一下。"
    finally:
        if old is None:
            os.environ.pop("FORCHAT_LOG_THINKING", None)
        else:
            os.environ["FORCHAT_LOG_THINKING"] = old

def test_resources_visible_to_worker() -> None:
    """read_state 要带资源余量，模型派活前能看见。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("read_state", {})]),
                    tool_chunks([("report", {"summary": "ok"})]),
                ]
            }
        )
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")
    results = [event.get("result", "") for event in runtime.log.query(type="tool_result")]
    assert any("剩余总预算" in item for item in results), results

def test_context_trim_tightens_when_budget_low() -> None:
    """整轮预算剩下 <20% 时，裁剪要收紧并标记 tight。"""
    import os

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"] = {"max_total_tokens": 200, "max_wall_seconds": 600}
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    turns = [tool_chunks([("read_state", {})]) for _ in range(20)]
    turns.append(tool_chunks([("report", {"summary": "ok"})]))
    provider = FakeProvider(TemplateResponder({"root": turns}))

    old = os.environ.get("FORCHAT_CONTEXT_MESSAGES")
    os.environ["FORCHAT_CONTEXT_MESSAGES"] = "6"
    try:
        runtime = Runtime(workflow, provider, tmpdir())
        runtime.start("任务")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_CONTEXT_MESSAGES", None)
        else:
            os.environ["FORCHAT_CONTEXT_MESSAGES"] = old

    trimmed = runtime.log.query(type="context_compacted")
    assert any(event.get("tight") for event in trimmed), trimmed

def test_queued_state_visible() -> None:
    """并发槽不够时，后派的 worker 状态是 queued，并留事件（前端要显示排队数）。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["limits"] = {"max_concurrent": 1, "max_workers": 8}
    raw["workers"]["root"]["tools"] = ["spawn", "wait_workers", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    slow = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(40)]
    slow.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    coder_turn = slow + tool_chunks([("report", {"summary": "子完成"})])

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "a", "wait": False}),
                             ("spawn", {"template": "coder", "task": "b", "wait": False})]),
                tool_chunks([("wait_workers", {"timeout_seconds": 30})]),
                tool_chunks([("report", {"summary": "根完成"})]),
            ],
            "coder": [coder_turn, coder_turn],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder, delay=0.01), tmpdir())
    runtime.start("任务")

    queued = runtime.log.query(type="worker_queued")
    assert len(queued) == 2, queued            # 两个异步分支都进过队列
    assert all("pending" in event for event in queued)
    assert all(event.get("max_concurrent") == 1 for event in queued)

def test_compact_uses_llm_summary_when_available() -> None:
    """compact 默认走 LLM 摘要；摘要器失败时退回确定性摘要。"""
    import os

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    turns = [tool_chunks([("read_state", {})]) for _ in range(20)]
    turns.append(tool_chunks([("report", {"summary": "ok"})]))
    old = os.environ.get("FORCHAT_CONTEXT_MESSAGES")
    os.environ["FORCHAT_CONTEXT_MESSAGES"] = "10"
    try:
        runtime = Runtime(
            workflow,
            FakeProvider(TemplateResponder({"root": turns})),
            tmpdir(),
            compact_summarizer=lambda dropped: "总结：查过 read_state，无未决事项",
        )
        runtime.start("任务")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_CONTEXT_MESSAGES", None)
        else:
            os.environ["FORCHAT_CONTEXT_MESSAGES"] = old

    compacted = runtime.log.query(type="context_compacted")
    assert compacted
    assert compacted[0]["summary_source"] == "llm"

    turns2 = [tool_chunks([("read_state", {})]) for _ in range(20)]
    turns2.append(tool_chunks([("report", {"summary": "ok"})]))
    os.environ["FORCHAT_CONTEXT_MESSAGES"] = "10"
    try:
        fallback = Runtime(
            workflow,
            FakeProvider(TemplateResponder({"root": turns2})),
            tmpdir(),
            compact_summarizer=lambda dropped: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        fallback.start("任务")
    finally:
        if old is None:
            os.environ.pop("FORCHAT_CONTEXT_MESSAGES", None)
        else:
            os.environ["FORCHAT_CONTEXT_MESSAGES"] = old

    assert fallback.log.query(type="context_compacted")[0]["summary_source"] == "digest"


def test_compact_archive_retention_configurable() -> None:
    """FORCHAT_COMPACT_KEEP=N：只留最近 N 份归档（默认 0 = 全留）。"""
    import os

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    turns = [tool_chunks([("read_state", {})]) for _ in range(24)]
    turns.append(tool_chunks([("report", {"summary": "ok"})]))
    old_messages = os.environ.get("FORCHAT_CONTEXT_MESSAGES")
    old_keep = os.environ.get("FORCHAT_COMPACT_KEEP")
    os.environ["FORCHAT_CONTEXT_MESSAGES"] = "6"
    os.environ["FORCHAT_COMPACT_KEEP"] = "2"
    run_dir = tmpdir()
    try:
        runtime = Runtime(workflow, FakeProvider(TemplateResponder({"root": turns})), run_dir)
        runtime.start("任务")
    finally:
        for key, old in (("FORCHAT_CONTEXT_MESSAGES", old_messages),
                         ("FORCHAT_COMPACT_KEEP", old_keep)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    files = sorted((run_dir / "compacted").glob("n001-root-*.json"))
    assert len(files) <= 2, files
    assert runtime.log.query(type="context_compacted")


def test_stuck_worker_detection() -> None:
    """心跳超时的 running/queued worker 会被 stuck_workers 标出来。"""
    import time

    from forchat.primitives.types import WorkerInstance

    workflow = WorkflowDef.load(MINIMAL)
    runtime = Runtime(workflow, FakeProvider(TemplateResponder({})), tmpdir())
    worker = WorkerInstance(id="n001-root", template="root", task="任务", status="running")
    runtime.workers[worker.id] = worker
    runtime._beats[worker.id] = time.time() - 100

    stuck = runtime.stuck_workers(threshold_seconds=30)
    assert stuck and stuck[0]["worker_id"] == "n001-root"
    assert stuck[0]["idle_seconds"] >= 30

    runtime._beat(worker.id)
    assert runtime.stuck_workers(threshold_seconds=30) == []

    worker.status = "done"
    assert runtime.stuck_workers(threshold_seconds=0.0) == []


def test_heartbeat_recorded_during_run() -> None:
    """正常跑一轮也会留下心跳，服务端据此判断卡死。"""
    workflow = WorkflowDef.load(MINIMAL)
    provider = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "ok"})])]})
    )
    runtime = Runtime(workflow, provider, tmpdir())
    runtime.start("任务")
    assert runtime._beats.get("n001-root") is not None


def main() -> int:
    checks = [
        ("长会话 compact 归档", test_context_trimmed_for_long_sessions),
        ("compact 优先 LLM 摘要", test_compact_uses_llm_summary_when_available),
        ("compact 归档保留配置", test_compact_archive_retention_configurable),
        ("stuck 检测", test_stuck_worker_detection),
        ("运行期心跳", test_heartbeat_recorded_during_run),
        ("thinking 日志默认关闭", test_thinking_logging_is_opt_in),
        ("资源余量可见", test_resources_visible_to_worker),
        ("预算低时裁剪收紧", test_context_trim_tightens_when_budget_low),
        ("排队状态可见", test_queued_state_visible),
    ]
    return run("原语资源/上下文自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
