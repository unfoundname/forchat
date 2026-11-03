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
    ]
    return run("工具与安全自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
