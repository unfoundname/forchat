"""CLI 自检：--check / --inspect 的进程级行为与退出码。

    python tests/test_cli.py
"""
from __future__ import annotations

import io
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


def test_prune_dry_run_then_delete() -> None:
    """--prune 默认只给计划，--yes 才真删。"""
    from forchat.runs import prune

    base = tmpdir()
    for name in ("a", "b", "c"):
        run_dir = base / name
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text("x" * 100, encoding="utf-8")

    victims, freed = prune(base, keep_last=1, dry_run=True)
    assert len(victims) == 2, victims
    assert freed > 0
    assert all(path.is_dir() for path in victims)

    removed, _ = prune(base, keep_last=1, dry_run=False)
    assert all(not path.exists() for path in removed)
    assert len(list(base.iterdir())) == 1


def test_compacted_cleanup_plan() -> None:
    """compact 归档清理：默认 dry-run，按 worker 保留最近 N 份。"""
    from forchat.runs import cleanup_compacted, plan_compacted_cleanup

    base = tmpdir()
    run_dir = base / "run-a" / "compacted"
    run_dir.mkdir(parents=True)
    for index in (1, 2, 3):
        (run_dir / f"n002-coder-{index:03d}.json").write_text("{}", encoding="utf-8")

    planned = plan_compacted_cleanup(base, keep_latest=1)
    assert len(planned) == 2, planned

    victims, freed = cleanup_compacted(base, keep_latest=1, dry_run=True)
    assert len(victims) == 2 and freed > 0
    assert all(path.is_file() for path in victims)

    cleanup_compacted(base, keep_latest=1, dry_run=False)
    assert len(list(run_dir.iterdir())) == 1


def test_init_scaffold_and_check() -> None:
    """forchat --init 生成包，紧接着 --check 能通过。"""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    target = tmpdir() / "hello-wf"
    result = subprocess.run(
        [sys.executable, "-m", "forchat", "--init", str(target)],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "workflow.json").is_file()
    assert (target / "prompts" / "root.md").is_file()

    check = subprocess.run(
        [sys.executable, "-m", "forchat", "--check", str(target / "workflow.json")],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_demo_runs_without_key() -> None:
    """forchat --demo 一条命令跑出端到端假 provider 运行。"""
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("DEEPSEEK_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "forchat", "--demo"],
        cwd=root, capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status=done" in result.stdout


def test_all_example_workflows_load() -> None:
    """所有示例 workflow 都必须能通过 --check（防示例漂移）。"""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    for workflow in sorted((root / "examples" / "workflows").glob("*/workflow.json")):
        result = subprocess.run(
            [sys.executable, "-m", "forchat", "--check", str(workflow)],
            cwd=root, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 0, f"{workflow}: {result.stdout}{result.stderr}"


def main() -> int:
    checks = [
        ("--check 进程级退出码", test_check_cli_exit_codes),
        ("--inspect 输出树/预算/最近事件", test_inspect_prints_tree_budget_and_recent),
        ("--inspect 缺失目录返回 2", test_inspect_missing_dir_returns_2),
        ("--runs 列出历史", test_runs_lists_history),
        ("--runs 缺失目录", test_runs_missing_dir),
        ("--prune 计划与删除", test_prune_dry_run_then_delete),
        ("compact 归档清理计划", test_compacted_cleanup_plan),
        ("--init 生成并 --check", test_init_scaffold_and_check),
        ("--demo 一键跑通", test_demo_runs_without_key),
        ("所有示例 workflow 可加载", test_all_example_workflows_load),
    ]
    return run("CLI 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
