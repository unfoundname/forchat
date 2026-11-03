"""原语层自检：每个原语一条测试，不联网、不需要 key。

    python tests/test_primitives.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forchat.primitives import Replay, Runtime, WorkflowDef, WorkflowError
from forchat.primitives.store import EventLog
from forchat.primitives.types import Budget
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks

HERE = Path(__file__).resolve().parent
MINIMAL = HERE.parent / "examples" / "workflows" / "minimal" / "workflow.json"

PASSED: list[str] = []


def check(name: str, fn) -> None:
    fn()
    PASSED.append(name)
    print(f"  ok  {name}")


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="forchat-test-"))


# ---------------------------------------------------------------- 原语测试
def test_budget() -> None:
    budget = Budget(tokens=10, wall_seconds=5)
    budget.charge(tokens=9)
    assert not budget.exhausted()
    budget.charge(tokens=2)
    assert budget.exhausted()
    assert budget.snapshot()["tokens"] == [11, 10]


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


def test_external_tools_bridge() -> None:
    """装配层注入的外部工具：spec 进 tools 列表，调用走 raw caller。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["echo", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_tools={"echo"})

    external = {
        "echo": (
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "回显",
                    "parameters": {"type": "object",
                                   "properties": {"text": {"type": "string"}}},
                },
            },
            lambda raw_args: f"echo:{raw_args}",
        )
    }
    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("echo", {"text": "hi"})]),
                tool_chunks([("report", {"summary": "外部工具已调用"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir(), external_tools=external)
    runtime.start("用外部工具")
    results = runtime.log.query(type="tool_result")
    assert results and results[0]["result"] == 'echo:{"text": "hi"}', results


def test_registry_bridge_read() -> None:
    """真实能力工具（registry 里的 read）经 tool_bridge 接进运行时。"""
    from forchat import tools  # noqa: F401  触发工具注册
    from forchat.tool_bridge import build, known_names

    target = tmpdir() / "note.txt"
    target.write_text("hello from disk\n", encoding="utf-8")

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_tools=known_names())

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("read", {"path": str(target)})]),
                tool_chunks([("report", {"summary": "读到了"})]),
            ]
        }
    )
    runtime = Runtime(workflow, FakeProvider(responder), tmpdir(), external_tools=build())
    runtime.start("读文件")
    results = runtime.log.query(type="tool_result")
    assert any("hello from disk" in event["result"] for event in results), results


def test_workflow_rejects_unknown_skill() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["skills"] = ["nope"]
    try:
        WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_skills={"code-review"})
    except WorkflowError as exc:
        assert "nope" in str(exc)
        return
    raise AssertionError("挂载不存在的技能应当报错")


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


def test_policy_path_guard() -> None:
    from forchat.policy import Policy

    root = tmpdir()
    (root / "ok.txt").write_text("x", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "a.py").write_text("print(1)", encoding="utf-8")
    policy = Policy(root)

    assert policy.check("read", json.dumps({"path": "ok.txt"})) is None
    assert "越界" in (policy.check("read", json.dumps({"path": "../x.txt"})) or "")
    assert policy.check("read", json.dumps({"path": ".env"})) is not None
    assert policy.check("run_python", json.dumps({"script": "ok.py"})) is not None
    assert policy.check("run_python", json.dumps({"script": "skills/demo/a.py"})) is None


def test_runtime_guard_blocks_tool() -> None:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["write_note", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("write_note", {"key": "k", "value": "v"})]),
                tool_chunks([("report", {"summary": "ok"})]),
            ]
        }
    )
    runtime = Runtime(
        workflow,
        FakeProvider(responder),
        tmpdir(),
        guard=lambda name, args: "测试拒绝" if name == "write_note" else None,
    )
    root = runtime.start("任务")

    denied = runtime.log.query(type="tool_denied")
    assert denied and "测试拒绝" in denied[0]["reason"]
    assert root.status == "done"


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


def main() -> int:
    print("原语自检：")
    checks = [
        ("budget 记账与判废", test_budget),
        ("eventlog 追加/查询/附件", test_eventlog),
        ("workflow 拒绝未知工具", test_workflow_rejects_unknown_tool),
        ("workflow 拒绝不存在的派生目标", test_workflow_rejects_bad_spawn_target),
        ("workflow 拒绝缺失 role_file", test_workflow_rejects_missing_role_file),
        ("运行时：派生 + 报告 + hook", test_runtime_spawns_and_reports),
        ("围栏：叶子不能派生", test_leaf_cannot_spawn),
        ("围栏：最大深度", test_max_depth_fence),
        ("replay：review 与 tree", test_replay_review_and_tree),
        ("能力工具桥接：外部工具注入", test_external_tools_bridge),
        ("能力工具桥接：registry 的 read", test_registry_bridge_read),
        ("workflow 拒绝未知技能", test_workflow_rejects_unknown_skill),
        ("技能挂载进系统提示", test_skill_mounted_into_prompt),
        ("围栏：整轮墙钟", test_wall_clock_fence),
        ("状态落盘与重载", test_state_persisted_and_reloaded),
        ("未调 report = 失败", test_no_report_means_failed),
        ("on_report hook", test_on_report_hook),
        ("默认不限轮数", test_unlimited_rounds_by_default),
        ("父 worker 读子报告", test_read_worker_report),
        ("policy 路径护栏", test_policy_path_guard),
        ("运行时执行护栏", test_runtime_guard_blocks_tool),
        ("调度器并发上限", test_scheduler_concurrency_limit),
        ("replay summary", test_replay_summary),
    ]
    failed = 0
    for name, fn in checks:
        try:
            check(name, fn)
        except Exception as exc:                      # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")

    print(f"\n{len(PASSED)}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
