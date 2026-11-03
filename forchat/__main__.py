"""命令行入口：跑一个 workflow。

    python -m forchat "你的目标"                    # 用默认 workflow 包
    python -m forchat --workflow examples/workflows/minimal/workflow.json "目标"
    python -m forchat --tools                      # 列出能力工具（含 MCP）
    python -m forchat --skills                     # 列出技能
    python -m forchat --mcp                        # 连接 MCP 服务端

注意：这里只有"装配"，没有执行逻辑——执行只在 forchat/primitives/runtime.py 里有一份。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from . import registry, skills
from . import tools  # noqa: F401  (触发能力工具注册)
from .mcp_bridge import connect_from_config
from .primitives import Replay, Runtime, WorkflowDef, WorkflowError

DEFAULT_WORKFLOW = Path("examples/workflows/minimal/workflow.json")

USAGE = """用法:
  python -m forchat [目标]
  python -m forchat --workflow <path> [目标]
  python -m forchat --tools     列出能力工具（含 MCP）
  python -m forchat --skills    列出已加载技能
  python -m forchat --mcp       连接 mcp_servers.json 里的服务端
"""


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


def _run_workflow(path: Path, goal: str) -> int:
    from .providers.deepseek import DeepSeekProvider
    from .tool_bridge import build as build_tools, known_names

    connect_from_config()                      # 先连 MCP，它的工具才算"已有能力"
    try:
        workflow = WorkflowDef.load(path, known_tools=known_names())
    except WorkflowError as exc:
        print(f"workflow 配置有误：{exc}")
        return 2

    run_dir = Path(".runs") / f"{workflow.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    runtime = Runtime(
        workflow,
        DeepSeekProvider(),
        run_dir,
        external_tools=build_tools(),
    )
    print(f"[run] workflow={workflow.name} entry={workflow.entry} dir={run_dir}")

    try:
        root = runtime.start(goal)
    except KeyboardInterrupt:
        print("\n[abort] 被中断，事件流已落盘，可用 Replay 复查")
        return 130
    except RuntimeError as exc:
        print(f"[error] {exc}")
        return 2

    print("\n" + Replay(runtime.log).tree())
    report = root.report.to_dict() if root.report else {}
    print(f"\n[done] status={root.status} workers={len(runtime.workers)} 事件={len(runtime.log.read())}")
    print(f"[done] summary={report.get('summary', '')}")
    return 0 if root.status == "done" else 1


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return
    if args and args[0] == "--tools":
        _list_tools()
        return
    if args and args[0] == "--skills":
        _list_skills()
        return
    if args and args[0] == "--mcp":
        clients = connect_from_config()
        print(f"已连接 {len(clients)} 个服务端" if clients else "没有连上任何 MCP 服务端")
        return

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
