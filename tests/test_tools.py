"""能力工具与安全自检：registry 工具、policy、provider 映射、工具桥。

    python tests/test_tools.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef, WorkflowError
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


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
    # run_python：项目根内的任意 .py 都允许（worktree 里的测试也要能跑）
    assert policy.check("run_python", json.dumps({"script": "ok.py"})) is None
    assert policy.check("run_python", json.dumps({"script": "skills/demo/a.py"})) is None
    assert policy.check("run_python", json.dumps({"script": "../escape.py"})) is not None
    assert policy.check("run_python", json.dumps({"script": "skills/demo/a.txt"})) is not None


def test_policy_denies_cwd_escape() -> None:
    from forchat.policy import Policy

    root = tmpdir()
    policy = Policy(root)
    denied = policy.check("run_python", json.dumps({"script": "x.py", "cwd": "../"}))
    assert denied and "工作目录越界" in denied, denied


def test_policy_blocks_write_file_escape() -> None:
    """运行时护栏对 write_file 同样生效：越界写被拒且文件不产生。"""
    from forchat import registry, tools  # noqa: F401  触发注册
    from forchat.policy import Policy
    from forchat.tool_bridge import build

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["write_file", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent, known_tools={"write_file"})

    root_dir = tmpdir()
    escape = root_dir.parent / "escape.txt"
    if escape.is_file():
        escape.unlink()

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("write_file", {"path": str(escape), "content": "x"})]),
                tool_chunks([("report", {"summary": "被拒后收尾"})]),
            ]
        }
    )
    runtime = Runtime(
        workflow,
        FakeProvider(responder),
        root_dir,
        external_tools=build(),
        guard=Policy(root_dir).check,
    )
    runtime.start("写越界文件")

    denied = runtime.log.query(type="tool_denied")
    assert denied and "越界" in denied[0]["reason"], denied
    assert not escape.is_file()


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
        guard=lambda name, args, scope: "测试拒绝" if name == "write_note" else None,
    )
    root = runtime.start("任务")

    denied = runtime.log.query(type="tool_denied")
    assert denied and "测试拒绝" in denied[0]["reason"]
    assert root.status == "done"

    from forchat.primitives import Replay

    summary = Replay(runtime.log).summary()
    assert "护栏:" in summary and "tool_denied=1" in summary, summary


def test_deepseek_provider_maps_chunks() -> None:
    """真 provider 的映射：chunk -> dict、usage 开关、tools 透传。"""
    from types import SimpleNamespace

    from forchat.providers.deepseek import DeepSeekProvider

    captured: dict = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            chunk = SimpleNamespace(
                model_dump=lambda exclude_none=True: {
                    "choices": [{"delta": {"content": "hi"}}]
                }
            )
            return iter([chunk])

    provider = DeepSeekProvider(api_key="test-key", thinking=True)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    chunks = list(
        provider.stream("deepseek-flash", [{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    )
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert captured["tools"] == [{"type": "function"}]
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


def test_write_file_tool() -> None:
    from forchat import registry, tools  # noqa: F401  触发注册

    target = tmpdir() / "deep" / "note.txt"
    result = registry.execute("write_file", json.dumps({"path": str(target), "content": "内容"}))
    assert "已写入" in result, result
    assert target.read_text(encoding="utf-8") == "内容"


def test_git_tool_whitelist() -> None:
    from forchat import registry, tools  # noqa: F401

    denied = registry.execute("git", json.dumps({"args": "rm -rf ."}))
    assert "不在白名单" in denied, denied

    merged = registry.execute("git", json.dumps({"args": "merge"}))
    assert "不在白名单" not in merged, merged

    status = registry.execute("git", json.dumps({"args": "status --short"}))
    assert "不在白名单" not in status, status


def test_run_python_cwd() -> None:
    """run_python 支持 cwd：worktree 里跑测试靠它。"""
    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    script = work / "where.py"
    script.write_text(
        "import os\nprint(os.getcwd())\nassert __file__.endswith('where.py')\n",
        encoding="utf-8",
    )
    result = registry.execute(
        "run_python", json.dumps({"script": "where.py", "cwd": str(work)})
    )
    assert "exit code" not in result, result
    assert work.name in result, result



def test_policy_scope_enforced() -> None:
    """scope 非空时，只有授权目录能改；只读仍限项目根；git 路径也受 scope 约束。"""
    from forchat.policy import Policy

    root = tmpdir()
    policy = Policy(root)
    scope = [".worktrees/a"]

    assert policy.check("write_file", json.dumps({"path": ".worktrees/a/x.py", "content": "x"}), scope) is None
    denied = policy.check("write_file", json.dumps({"path": ".worktrees/b/x.py", "content": "x"}), scope)
    assert denied and "授权范围" in denied, denied

    # 只读工具不受 scope 限制（仍限项目根）——读别人的 worktree 不产生副作用
    assert policy.check("read", json.dumps({"path": ".worktrees/b/x.py"}), scope) is None

    denied_git = policy.check(
        "git", json.dumps({"args": "worktree add .worktrees/b -b feat-b"}), scope
    )
    assert denied_git and "授权范围" in denied_git, denied_git

    # --opt=value 形式的绕过也要被拦（例如 --git-dir=/tmp/x）
    for sneaky in (
        "status --git-dir=../outside",
        "status --work-tree=/tmp/elsewhere",
    ):
        blocked = policy.check("git", json.dumps({"args": sneaky}), scope)
        assert blocked, sneaky


def test_run_python_scrubs_secrets() -> None:
    """run_python 的子进程不能继承 API_KEY 之类的密钥，普通变量照常传递。"""
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    script = work / "show_env.py"
    script.write_text(
        "import os\n"
        "print('KEY=' + str(os.environ.get('DEEPSEEK_API_KEY', 'missing')))\n"
        "print('PLAIN=' + str(os.environ.get('FORCHAT_TEST_PLAIN', 'missing')))\n",
        encoding="utf-8",
    )
    old_key = os.environ.get("DEEPSEEK_API_KEY")
    old_plain = os.environ.get("FORCHAT_TEST_PLAIN")
    os.environ["DEEPSEEK_API_KEY"] = "secret-should-not-leak"
    os.environ["FORCHAT_TEST_PLAIN"] = "visible"
    try:
        out = registry.execute("run_python", json.dumps({"script": "show_env.py", "cwd": str(work)}))
    finally:
        for key, old in (("DEEPSEEK_API_KEY", old_key), ("FORCHAT_TEST_PLAIN", old_plain)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    assert "KEY=missing" in out, out
    assert "PLAIN=visible" in out, out


def test_write_file_backs_up_overwrite() -> None:
    """覆盖已有文件前留备份，旧内容可找回。"""
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    (work / "note.txt").write_text("旧内容", encoding="utf-8")
    old_cwd = os.getcwd()
    os.chdir(work)
    try:
        result = registry.execute(
            "write_file", json.dumps({"path": "note.txt", "content": "新内容"})
        )
    finally:
        os.chdir(old_cwd)

    assert "已写入" in result, result
    backup = work / ".forchat-backups" / "note.txt"
    assert backup.is_file(), list(work.iterdir())
    assert backup.read_text(encoding="utf-8") == "旧内容"


def test_retry_provider_retries_before_first_chunk() -> None:
    from forchat.providers.retry import RetryProvider

    calls = {"n": 0}

    class Flaky:
        name = "flaky"

        def stream(self, model, messages, tools=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("模拟瞬时超时")
            yield {"choices": [{"delta": {"content": "ok"}}]}

    provider = RetryProvider(Flaky(), attempts=2, backoff=0, transient=(TimeoutError,))
    chunks = list(provider.stream("m", [], None))
    assert calls["n"] == 2
    assert chunks and chunks[0]["choices"][0]["delta"]["content"] == "ok"


def test_retry_provider_does_not_retry_midstream() -> None:
    """已经吐出分片后再失败不能重试，否则内容会重复。"""
    from forchat.providers.retry import RetryProvider

    calls = {"n": 0}

    class MidFail:
        name = "mid"

        def stream(self, model, messages, tools=None):
            calls["n"] += 1
            yield {"choices": [{"delta": {"content": "half"}}]}
            raise TimeoutError("流到一半断了")

    provider = RetryProvider(MidFail(), attempts=3, backoff=0, transient=(TimeoutError,))
    got = []
    try:
        for chunk in provider.stream("m", [], None):
            got.append(chunk)
    except TimeoutError:
        pass
    else:
        raise AssertionError("应当把异常抛出来")

    assert len(got) == 1
    assert calls["n"] == 1


def test_retry_provider_exhausts_attempts() -> None:
    from forchat.providers.retry import RetryProvider

    calls = {"n": 0}

    class AlwaysDown:
        name = "down"

        def stream(self, model, messages, tools=None):
            calls["n"] += 1
            raise ConnectionError("连不上")
            yield  # pragma: no cover

    provider = RetryProvider(AlwaysDown(), attempts=3, backoff=0, transient=(ConnectionError,))
    try:
        list(provider.stream("m", [], None))
    except ConnectionError:
        pass
    else:
        raise AssertionError("重试耗尽后应当抛出原异常")
    assert calls["n"] == 3


def test_pricing_math_and_unknown() -> None:
    from forchat.pricing import Price, Pricing

    pricing = Pricing({"m": Price(input_per_m=1.0, output_per_m=2.0)})
    assert pricing.cost("m", 1_000_000, 0) == 1.0
    assert pricing.cost("m", 0, 1_000_000) == 2.0
    assert pricing.cost("m", 1_000_000, 1_000_000) == 3.0
    assert pricing.cost("unknown-model", 10, 10) is None


def test_runtime_report_includes_cost() -> None:
    from forchat.pricing import Price, Pricing

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder({"root": [tool_chunks([("report", {"summary": "ok"})])]})
    runtime = Runtime(
        workflow,
        FakeProvider(responder),
        tmpdir(),
        pricing=Pricing({"fake": Price(input_per_m=1.0, output_per_m=2.0)}),
    )
    root = runtime.start("任务")

    assert root.report.cost_usd is not None
    assert root.report.cost_usd > 0
    finished = runtime.log.query(type="run_finished")[-1]
    assert finished["total_cost_usd"] > 0


def test_budget_cost_fence() -> None:
    from forchat.pricing import Price, Pricing

    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["read_state", "report"]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["budget"] = {
        "tokens": 20000, "wall_seconds": 600, "max_cost_usd": 0.00001,
    }
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    responder = TemplateResponder(
        {
            "root": [
                tool_chunks([("read_state", {})]),
                tool_chunks([("report", {"summary": "不该到这"})]),
            ]
        }
    )
    runtime = Runtime(
        workflow,
        FakeProvider(responder),
        tmpdir(),
        pricing=Pricing({"fake": Price(input_per_m=1.0, output_per_m=100.0)}),
    )
    root = runtime.start("任务")

    assert runtime.log.query(type="budget_exhausted"), "成本超限应当判废"
    assert root.status == "failed"


def test_run_python_timeout_env() -> None:
    """FORCHAT_SCRIPT_TIMEOUT 生效：超时返回错误回执，不阻塞。"""
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    (work / "sleep.py").write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    old = os.environ.get("FORCHAT_SCRIPT_TIMEOUT")
    os.environ["FORCHAT_SCRIPT_TIMEOUT"] = "1"
    try:
        out = registry.execute("run_python", json.dumps({"script": "sleep.py", "cwd": str(work)}))
    finally:
        if old is None:
            os.environ.pop("FORCHAT_SCRIPT_TIMEOUT", None)
        else:
            os.environ["FORCHAT_SCRIPT_TIMEOUT"] = old
    assert "超时" in out, out


def main() -> int:
    checks = [
        ("能力工具桥接：外部工具注入", test_external_tools_bridge),
        ("能力工具桥接：registry 的 read", test_registry_bridge_read),
        ("policy 路径护栏", test_policy_path_guard),
        ("护栏拒绝 cwd 越界", test_policy_denies_cwd_escape),
        ("护栏拦截越界写入", test_policy_blocks_write_file_escape),
        ("运行时执行护栏", test_runtime_guard_blocks_tool),
        ("deepseek provider 映射", test_deepseek_provider_maps_chunks),
        ("write_file 工具", test_write_file_tool),
        ("git 白名单", test_git_tool_whitelist),
        ("run_python 支持 cwd", test_run_python_cwd),
        ("scope 授权范围", test_policy_scope_enforced),
        ("run_python 擦除密钥", test_run_python_scrubs_secrets),
        ("write_file 覆盖前备份", test_write_file_backs_up_overwrite),
        ("RetryProvider：首片前重试", test_retry_provider_retries_before_first_chunk),
        ("RetryProvider：流中不重试", test_retry_provider_does_not_retry_midstream),
        ("RetryProvider：重试耗尽", test_retry_provider_exhausts_attempts),
        ("价目表换算", test_pricing_math_and_unknown),
        ("运行时报告含成本", test_runtime_report_includes_cost),
        ("成本预算闸", test_budget_cost_fence),
        ("run_python 超时环境变量", test_run_python_timeout_env),
    ]
    return run("工具与安全自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
