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



def main() -> int:
    checks = [
        ("policy 路径越界", test_policy_path_guard),
        ("policy 拒绝 cwd 越界", test_policy_denies_cwd_escape),
        ("policy 拦截写越界", test_policy_blocks_write_file_escape),
        ("运行时 guard 拒绝", test_runtime_guard_blocks_tool),
        ("deepseek provider 映射", test_deepseek_provider_maps_chunks),
        ("scope 授权范围", test_policy_scope_enforced),
        ("run_python 擦除密钥", test_run_python_scrubs_secrets),
        ("write_file 覆盖前备份", test_write_file_backs_up_overwrite),
    ]
    return run("策略与安全自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
