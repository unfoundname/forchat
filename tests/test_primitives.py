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

from forchat.primitives import Replay, Runtime, WorkflowDef, WorkflowError
from forchat.primitives.store import EventLog
from forchat.primitives.types import Budget
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_budget() -> None:
    budget = Budget(tokens=10, wall_seconds=5)
    budget.charge(tokens=9)
    assert not budget.exhausted()
    budget.charge(tokens=2)
    assert budget.exhausted()
    assert budget.snapshot()["tokens"] == [11, 10]
    # 默认预算要按"真实项目"量级，不是 200k
    assert Budget().tokens == 20_000_000
    assert Budget().wall_seconds == 14400.0


def test_eventlog() -> None:
    log = EventLog(tmpdir())
    log.append("worker_started", worker_id="n001", template="root")
    log.append("tool_call", worker_id="n001", tool="spawn", arguments={"template": "coder"})
    log.append("tool_call", worker_id="n002", tool="report")

    assert len(log.read()) == 3
    assert len(log.query(type="tool_call")) == 2
    assert len(log.query(type="tool_call", worker_id="n002")) == 1
    path = log.artifact("note.txt", "hello")
    assert (log.root / path).read_text(encoding="utf-8") == "hello"


def test_workflow_rejects_unknown_tool() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "teleport"]
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    except WorkflowError as exc:
        assert "teleport" in str(exc)
        return
    raise AssertionError("未知工具应当报错")


def test_workflow_rejects_bad_spawn_target() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["can_spawn"] = ["ghost"]
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    except WorkflowError as exc:
        assert "ghost" in str(exc)
        return
    raise AssertionError("不存在的模板应当报错")


def test_workflow_rejects_missing_role_file() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["role_file"] = "prompts/nope.md"
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    except WorkflowError as exc:
        assert "role_file" in str(exc)
        return
    raise AssertionError("缺失 role_file 应当报错")


# ---------------------------------------------------------------- 运行时
def _responder_root_only(**extra) -> TemplateResponder:
    return TemplateResponder(
        {
            "root": [tool_chunks([("report", {"summary": "完成", **extra})])],
            "coder": [tool_chunks([("report", {"summary": "子任务完成"})])],
            "reviewer": [tool_chunks([("report", {"summary": "审查通过"})])],
        }
    )


def test_workflow_rejects_unknown_top_key() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["unknown_section"] = {}
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    except WorkflowError as exc:
        assert "unknown_section" in str(exc)
        return
    raise AssertionError("未知顶层字段应当报错")


def test_workflow_rejects_bad_max_rounds() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["max_rounds"] = 0
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)
    except WorkflowError as exc:
        assert "max_rounds" in str(exc)
        return
    raise AssertionError("max_rounds=0 应当报错")

def test_replay_review_and_tree() -> None:
    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder(
        {
            "root": [tool_chunks([("report", {"summary": "完成"})])],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
            "reviewer": [tool_chunks([("report", {"summary": "审查通过"})])],
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir())
    runtime.start("根任务")
    replay = Replay(runtime.log)
    text = replay.review()
    assert "report" in text and "n001-root" in text
    assert replay.tree().startswith("n001-root")


def test_replay_summary() -> None:
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
    assert "n001-root" in summary
    assert "root" in summary and "done" in summary


def test_skill_mounted_into_prompt() -> None:
    """技能是配置挂载：正文由装配层注入，原语层只拿到 name -> body。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["skills"] = ["demo"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["tools"] = ["report"]
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_skills={"demo"})

    provider = FakeProvider(
        TemplateResponder({"root": [tool_chunks([("report", {"summary": "ok"})])]})
    )
    runtime = Runtime(workflow, provider, tmpdir(), skill_loader=lambda name: f"BODY-{name}")
    runtime.start("任务")

    system = provider.seen[0][0]["content"]
    assert "已挂载技能" in system and "BODY-demo" in system, system


def test_wall_clock_fence() -> None:
    """整轮运行的墙钟上限：超时后不再接受新的派生。"""
    workflow = WorkflowDef.load(MINIMAL)
    runtime = Runtime(
        workflow,
        FakeProvider(TemplateResponder({"root": [tool_chunks([("report", {"summary": "x"})])]})),
        tmpdir(),
    )
    runtime.run_deadline = time.time() - 1
    assert runtime._create_worker("coder", "太晚了", None, 1) is None
    rejected = runtime.log.query(type="spawn_rejected")
    assert rejected and rejected[0]["reason"] == "wall_clock"
    runtime.scheduler.shutdown()



def test_eventlog_cross_process_append() -> None:
    """两个进程同时写同一个 run 目录：文件锁保证不互相插行。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    work = tmpdir()
    script = work / "writer.py"
    script.write_text(
        "import sys\n"
        "from forchat.primitives.store import EventLog\n"
        "log = EventLog(sys.argv[1])\n"
        "for index in range(40):\n"
        "    log.append('noise', writer=sys.argv[2], index=index)\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    run_dir = work / "run"
    procs = [
        subprocess.Popen([sys.executable, str(script), str(run_dir), name], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for name in ("a", "b")
    ]
    for proc in procs:
        proc.wait(timeout=60)
        assert proc.returncode == 0, proc.stderr.read() if proc.stderr else b""

    events = EventLog(run_dir).read()
    assert len(events) == 80, len(events)
    assert {event.get("writer") for event in events} == {"a", "b"}


def test_tool_events_split_from_main_log() -> None:
    """工具全文进 tools.jsonl；events.jsonl 只留截断摘要（不灌大文本）。"""
    from forchat.primitives.store import EventLog

    run_dir = tmpdir()
    log = EventLog(run_dir)
    big = "x" * 2000
    log.append("tool_result", worker_id="w1", tool="read", result=big)

    events = [json.loads(line) for line in
              (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    tools = [json.loads(line) for line in
             (run_dir / "tools.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1 and len(tools) == 1
    assert len(events[0]["result"]) == 500
    assert len(tools[0]["result"]) == 2000
    assert tools[0]["worker_id"] == "w1"


def test_tools_log_rotates() -> None:
    """tools.jsonl 超过阈值也要滚动，不能无限涨。"""
    import os

    from forchat.primitives.store import EventLog

    old = os.environ.get("FORCHAT_EVENT_LOG_MB")
    os.environ["FORCHAT_EVENT_LOG_MB"] = "0.004"
    try:
        run_dir = tmpdir()
        log = EventLog(run_dir)
        for _ in range(40):
            log.append("tool_result", worker_id="w", tool="t", result="x" * 300)
    finally:
        if old is None:
            os.environ.pop("FORCHAT_EVENT_LOG_MB", None)
        else:
            os.environ["FORCHAT_EVENT_LOG_MB"] = old

    assert list(run_dir.glob("tools.*.jsonl")), list(run_dir.iterdir())
    assert (run_dir / "tools.jsonl").is_file()


def main() -> int:
    checks = [
        ("budget 记账与判废", test_budget),
        ("事件流跨进程追加", test_eventlog_cross_process_append),
        ("工具事件分离存储", test_tool_events_split_from_main_log),
        ("工具日志滚动", test_tools_log_rotates),
        ("eventlog 追加/查询/附件", test_eventlog),
        ("workflow 拒绝未知工具", test_workflow_rejects_unknown_tool),
        ("workflow 拒绝不存在的派生目标", test_workflow_rejects_bad_spawn_target),
        ("workflow 拒绝缺失 role_file", test_workflow_rejects_missing_role_file),
        ("workflow 拒绝未知顶层字段", test_workflow_rejects_unknown_top_key),
        ("workflow 拒绝 max_rounds=0", test_workflow_rejects_bad_max_rounds),
        ("replay：review 与 tree", test_replay_review_and_tree),
        ("replay summary", test_replay_summary),
        ("技能挂载进系统提示", test_skill_mounted_into_prompt),
        ("围栏：整轮墙钟", test_wall_clock_fence),
    ]
    return run("原语自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
