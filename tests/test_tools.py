"""能力工具与安全自检：registry 工具、policy、provider 映射、工具桥。

    python tests/test_tools.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
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


def test_budget_cost_is_soft() -> None:
    """成本预算同样是记账：超了继续跑，留 budget_exceeded 事件。"""
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
                tool_chunks([("report", {"summary": "成本超了也完成"})]),
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

    assert runtime.log.query(type="budget_exceeded"), "超预算应当留事件"
    assert root.status == "done"
    assert root.report.summary == "成本超了也完成"


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



def test_read_supports_offset_and_limit() -> None:
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    (work / "big.txt").write_text(
        "\n".join(f"line {i}" for i in range(1, 501)), encoding="utf-8"
    )
    old = os.getcwd()
    os.chdir(work)
    try:
        out = registry.execute(
            "read", json.dumps({"path": "big.txt", "offset": 400, "limit": 5})
        )
    finally:
        os.chdir(old)
    assert " 400 | line 400" in out, out
    assert "line 404" in out and "line 405" not in out
    assert "共 500 行" in out


def test_edit_file_unique_and_replace_all() -> None:
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    (work / "a.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    old = os.getcwd()
    os.chdir(work)
    try:
        ambiguous = registry.execute(
            "edit_file", json.dumps({"path": "a.txt", "old_string": "alpha", "new_string": "x"})
        )
        assert "出现" in ambiguous and "replace_all" in ambiguous, ambiguous

        every = registry.execute(
            "edit_file",
            json.dumps({"path": "a.txt", "old_string": "alpha", "new_string": "X", "replace_all": True}),
        )
        assert "已编辑" in every, every

        single = registry.execute(
            "edit_file",
            json.dumps({"path": "a.txt", "old_string": "beta", "new_string": "BETA"}),
        )
        assert "已编辑" in single, single
    finally:
        os.chdir(old)
    assert (work / "a.txt").read_text(encoding="utf-8") == "X\nBETA\nX\n"


def test_grep_and_glob() -> None:
    import os

    from forchat import registry, tools  # noqa: F401

    work = tmpdir()
    (work / "a.py").write_text("x = 1  # TODO fix\n", encoding="utf-8")
    (work / "b.md").write_text("TODO docs\n", encoding="utf-8")
    (work / "sub").mkdir()
    (work / "sub" / "c.py").write_text("print('ok')\n", encoding="utf-8")

    old = os.getcwd()
    os.chdir(work)
    try:
        hit = registry.execute("grep", json.dumps({"pattern": "TODO", "include": "*.py"}))
        listing = registry.execute("glob", json.dumps({"pattern": "**/*.py"}))
    finally:
        os.chdir(old)

    assert "a.py:1" in hit, hit
    assert "b.md" not in hit, hit
    assert "a.py" in listing and "c.py" in listing, listing
    assert "b.md" not in listing, listing


def main() -> int:
    checks = [
        ("外部工具 schema 桥接", test_external_tools_bridge),
        ("registry 桥接 read", test_registry_bridge_read),
        ("write_file", test_write_file_tool),
        ("git 白名单", test_git_tool_whitelist),
        ("run_python cwd", test_run_python_cwd),
        ("RetryProvider：首片前重试", test_retry_provider_retries_before_first_chunk),
        ("RetryProvider：流中不重试", test_retry_provider_does_not_retry_midstream),
        ("RetryProvider：重试耗尽", test_retry_provider_exhausts_attempts),
        ("价目表换算", test_pricing_math_and_unknown),
        ("运行时报告含成本", test_runtime_report_includes_cost),
        ("成本预算只记账", test_budget_cost_is_soft),
        ("run_python 超时环境变量", test_run_python_timeout_env),
        ("read offset/limit", test_read_supports_offset_and_limit),
        ("edit_file 唯一/全替换", test_edit_file_unique_and_replace_all),
        ("grep 与 glob", test_grep_and_glob),
    ]
    return run("工具自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
