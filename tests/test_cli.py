"""CLI 自检：--check / --inspect 的进程级行为与退出码。

    python tests/test_cli.py
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks


def test_check_cli_exit_codes() -> None:
    """--check 不需要 key：好配置退出 0，坏配置退出 2。"""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    good = root / "examples" / "workflows" / "minimal" / "workflow.json"
    result = subprocess.run(
        [sys.executable, "-m", "forchat", "--check", str(good)],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "[check] ok" in result.stdout

    bad = subprocess.run(
        [sys.executable, "-m", "forchat", "--check", str(root / "nope.json")],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert bad.returncode == 2, (bad.stdout or "") + (bad.stderr or "")
    assert "[check] 失败" in bad.stdout


def test_inspect_prints_tree_budget_and_recent() -> None:
    """--inspect：树 + 每个 worker 一行 + 预算行 + 最近事件。"""
    from forchat.__main__ import _inspect

    workflow = WorkflowDef.load(MINIMAL)
    responder = TemplateResponder({"root": [tool_chunks([("report", {"summary": "完成"})])]})
    run_dir = tmpdir()
    Runtime(workflow, FakeProvider(responder), run_dir).start("任务")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _inspect(run_dir)
    output = buffer.getvalue()

    assert code == 0, output
    assert "n001-root" in output
    assert "运行: status=" in output
    assert "合计:" in output


def test_inspect_missing_dir_returns_2() -> None:
    from forchat.__main__ import _inspect

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _inspect(tmpdir() / "does-not-exist")
    assert code == 2
    assert "没有事件流" in buffer.getvalue()


def test_runs_lists_history() -> None:
    """--runs：列出历史运行的状态/worker 数/token/耗时。"""
    from forchat.__main__ import _list_runs
    from forchat.primitives.store import EventLog

    base = tmpdir() / ".runs"
    done = base / "run-a"
    EventLog(done).append(
        "run_finished", status="done", workers=3, total_tokens=1234,
        budget_limit=5000, elapsed=12.5,
    )
    (base / "empty").mkdir()

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _list_runs(base)
    output = buffer.getvalue()

    assert code == 0
    assert "run-a" in output and "done" in output
    assert "1234/5000" in output, output


def test_runs_missing_dir() -> None:
    from forchat.__main__ import _list_runs

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _list_runs(tmpdir() / "no-runs")
    assert code == 0
    assert "没有" in buffer.getvalue()


def main() -> int:
    checks = [
        ("--check 进程级退出码", test_check_cli_exit_codes),
        ("--inspect 输出树/预算/最近事件", test_inspect_prints_tree_budget_and_recent),
        ("--inspect 缺失目录返回 2", test_inspect_missing_dir_returns_2),
        ("--runs 列出历史", test_runs_lists_history),
        ("--runs 缺失目录", test_runs_missing_dir),
    ]
    return run("CLI 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
