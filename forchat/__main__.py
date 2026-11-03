"""命令行入口：跑一个 workflow。

    python -m forchat "你的目标"                    # 用默认 workflow 包
    python -m forchat --workflow examples/workflows/minimal/workflow.json "目标"
    python -m forchat --tools                      # 列出能力工具（含 MCP）
    python -m forchat --skills                     # 列出技能
    python -m forchat --mcp                        # 连接 MCP 服务端

注意：这里只有"装配"，没有执行逻辑——执行只在 forchat/primitives/runtime.py 里有一份。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from . import registry, skills
from . import tools  # noqa: F401  (触发能力工具注册)
from .mcp_bridge import close_all, connect_from_config
from .primitives import EventLog, Replay, Runtime, WorkflowDef, WorkflowError

DEFAULT_WORKFLOW = Path("examples/workflows/minimal/workflow.json")

USAGE = """用法:
  python -m forchat [目标]
  python -m forchat --workflow <path> [目标]
  python -m forchat --tools     列出能力工具（含 MCP）
  python -m forchat --skills    列出已加载技能
  python -m forchat --mcp       连接 mcp_servers.json 里的服务端
  python -m forchat --inspect <run-dir>   复查一次历史运行
  python -m forchat --runs      列出历史运行（状态/worker 数/token/耗时）
  python -m forchat --check <workflow>   只校验 workflow 配置，不跑模型
  python -m forchat --resume <run-dir> [目标]   在同一个运行目录上继续推进
"""


def _list_runs(base: Path | None = None, limit: int = 20) -> int:
    """列出历史运行：状态、worker 数、token、耗时。不需要 API key。"""
    root = Path(base or ".runs")
    if not root.is_dir():
        print(f"（没有 {root} 目录）")
        return 0

    runs = [p for p in root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()]
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not runs:
        print(f"（{root} 下没有运行记录）")
        return 0

    print(f"{'run':38} {'status':8} {'workers':>7} {'tokens':>16} {'耗时(s)':>8}")
    for run_dir in runs[:limit]:
        log = EventLog(run_dir)
        finished = log.query(type="run_finished")
        last = finished[-1] if finished else None
        if last is None:
            print(f"{run_dir.name:38} {'running':8} {'-':>7} {'-':>16} {'-':>8}")
            continue
        limit_tokens = last.get("budget_limit") or "-"
        tokens = f"{last.get('total_tokens', '-')}/{limit_tokens}"
        print(
            f"{run_dir.name:38} {str(last.get('status', '-')):8} "
            f"{last.get('workers', '-'):>7} {tokens:>16} {last.get('elapsed', '-'):>8}"
        )
    if len(runs) > limit:
        print(f"...（共 {len(runs)} 次运行，只显示最近 {limit} 次）")
    return 0


def _check(path: Path) -> int:
    from .skills import load_skills
    from .tool_bridge import known_names

    connect_from_config()          # 让校验看得到 MCP 工具；失败不影响本地工具
    try:
        workflow = WorkflowDef.load(
            path, known_tools=known_names(), known_skills=set(load_skills())
        )
    except WorkflowError as exc:
        print(f"[check] 失败：{exc}")
        return 2
    finally:
        close_all()

    print(f"[check] ok  name={workflow.name} version={workflow.version} entry={workflow.entry}")
    print(f"[check] limits={workflow.limits}")
    for name, tpl in workflow.workers.items():
        spawn = ", ".join(tpl.can_spawn) if tpl.can_spawn else "（叶子）"
        rounds = tpl.max_rounds if tpl.max_rounds is not None else "不限"
        print(
            f"  {name:12} spawn-> {spawn:20} tools={len(tpl.tools):2} "
            f"skills={tpl.skills or '[]'} max_rounds={rounds}"
        )
    return 0


def _inspect(run_dir: Path) -> int:
    if not (run_dir / "events.jsonl").is_file():
        print(f"没有事件流：{run_dir}")
        return 2
    replay = Replay(EventLog(run_dir))
    print(replay.tree())
    print()
    print(replay.summary())
    print("\n最近事件：")
    print("\n".join(replay.review().splitlines()[-25:]))
    return 0


def _list_tools() -> None:
    connect_from_config()
    for name in registry.names():
        item = registry.REGISTRY[name]
        origin = "mcp" if item.raw_caller is not None else "local"
        params = ", ".join(item.parameters.get("properties", {})) or "-"
        print(f"{origin:5} {name:28} ({params})  {item.description.splitlines()[0][:60]}")


def _list_skills() -> None:
    found = skills.load_skills()
    if not found:
        print(f"（{skills.skills_dir()} 下没有技能）")
    for name, skill in found.items():
        print(f"{name:12} {skill.description}\n{'':12} {skill.path}")


def _assemble(workflow: WorkflowDef, run_dir: Path) -> Runtime:
    from .policy import Policy
    from .providers.deepseek import DeepSeekProvider
    from .providers.retry import RetryProvider
    from .tool_bridge import build as build_tools, build_skill_loader

    attempts = int(os.environ.get("FORCHAT_RETRIES", 2))
    provider = DeepSeekProvider()
    if attempts > 1:
        provider = RetryProvider(provider, attempts=attempts)

    return Runtime(
        workflow,
        provider,
        run_dir,
        external_tools=build_tools(),
        skill_loader=build_skill_loader(),
        guard=Policy(Path.cwd()).check,
    )


def _execute(workflow: WorkflowDef, run_dir: Path, goal: str, label: str = "run") -> int:
    runtime = _assemble(workflow, run_dir)
    print(f"[{label}] workflow={workflow.name} entry={workflow.entry} dir={run_dir}")
    if runtime.interrupted:
        names = ", ".join(runtime.interrupted[:5])
        more = f" 等 {len(runtime.interrupted)} 个" if len(runtime.interrupted) > 5 else ""
        print(f"[{label}] 上次中断的 worker：{names}{more}（会在根提示里列出，交给模型重新派发）")

    try:
        root = runtime.start(goal)
    except KeyboardInterrupt:
        print("\n[abort] 被中断，事件流已落盘，可用 Replay 复查")
        return 130
    except RuntimeError as exc:
        print(f"[error] {exc}")
        return 2
    finally:
        close_all()          # MCP 子进程由装配层负责收尾，不留给 atexit

    print("\n" + Replay(runtime.log).tree())
    report = root.report.to_dict() if root.report else {}
    print(f"\n[done] status={root.status} workers={len(runtime.workers)} 事件={len(runtime.log.read())}")
    print(f"[done] summary={report.get('summary', '')}")
    return 0 if root.status == "done" else 1


def _run_workflow(path: Path, goal: str) -> int:
    from .skills import load_skills
    from .tool_bridge import known_names

    connect_from_config()                      # 先连 MCP，它的工具才算"已有能力"
    try:
        workflow = WorkflowDef.load(
            path,
            known_tools=known_names(),
            known_skills=set(load_skills()),
        )
    except WorkflowError as exc:
        print(f"workflow 配置有误：{exc}")
        return 2

    run_dir = Path(".runs") / f"{workflow.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    # 把整个 workflow 包（workflow.json + prompts/ + 脚本）快照进运行目录：
    # --resume / --inspect 只依赖运行目录，不依赖原始路径。
    for item in path.parent.iterdir():
        target = run_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copy2(item, target)
    return _execute(workflow, run_dir, goal)


def _resume(run_dir: Path, goal: str) -> int:
    from .skills import load_skills
    from .tool_bridge import known_names

    snapshot = run_dir / "workflow.json"
    if not snapshot.is_file():
        print(f"运行目录里没有 workflow.json 快照：{run_dir}")
        return 2

    connect_from_config()
    try:
        workflow = WorkflowDef.load(
            snapshot, known_tools=known_names(), known_skills=set(load_skills())
        )
    except WorkflowError as exc:
        print(f"workflow 快照有误：{exc}")
        return 2
    return _execute(workflow, run_dir, goal, label="resume")


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return
    if args and args[0] == "--tools":
        _list_tools()
        close_all()
        return
    if args and args[0] == "--skills":
        _list_skills()
        return
    if args and args[0] == "--mcp":
        clients = connect_from_config()
        print(f"已连接 {len(clients)} 个服务端" if clients else "没有连上任何 MCP 服务端")
        close_all()
        return
    if args and args[0] == "--runs":
        raise SystemExit(_list_runs())
    if args and args[0] == "--inspect":
        if len(args) < 2:
            print("--inspect 需要一个运行目录，例如 .runs/minimal-20261103-130000")
            return
        raise SystemExit(_inspect(Path(args[1])))
    if args and args[0] == "--check":
        if len(args) < 2:
            print("--check 需要一个 workflow 路径")
            return
        raise SystemExit(_check(Path(args[1])))
    if args and args[0] == "--resume":
        if len(args) < 2:
            print("--resume 需要一个运行目录")
            return
        goal = " ".join(args[2:]) or "继续推进该项目：先 read_state 看历史 plan 与笔记"
        raise SystemExit(_resume(Path(args[1]), goal))

    workflow_path = DEFAULT_WORKFLOW
    if args[:1] == ["--workflow"]:
        if len(args) < 2:
            print("--workflow 需要一个路径")
            return
        workflow_path = Path(args[1])
        args = args[2:]

    goal = " ".join(args) or "跑通一次最小运行：派一个执行 worker，完成后自动派生 review"
    raise SystemExit(_run_workflow(workflow_path, goal))


if __name__ == "__main__":
    main()
