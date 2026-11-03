"""依赖图生成器：纯标准库 ast，无第三方依赖。

用法：
    python graph.py [源码根目录]      # 默认 .
    python graph.py . --max-nodes 20

输出：模块统计、内部依赖边、循环依赖、Mermaid 图。
由 skills/code-graph/SKILL.md 说明何时调用；也可以直接命令行跑。
"""
from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "site-packages",
}
DEFAULT_MAX_NODES = 40


def collect_modules(root: str) -> dict[str, str]:
    """dotted 模块名 -> 文件路径。"""
    modules: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root)
            parts = rel[:-3].split(os.sep)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            name = ".".join(parts) if parts else "(root)"
            modules[name] = path
    return modules


def _resolve(candidate: str, modules: dict[str, str]) -> str | None:
    """把 import 字符串对到项目内模块：先精确，再按后缀（兼容 src/ 布局）。"""
    if not candidate:
        return None
    if candidate in modules:
        return candidate
    suffix = "." + candidate
    hits = [name for name in modules if name.endswith(suffix)]
    return sorted(hits, key=len)[0] if len(hits) == 1 else None


def parse_imports(path: str) -> list[tuple[str, int, int, bool]]:
    """返回 [(原始目标, 行号, 相对层级, 是否只是别名猜测)]。

    第四项 soft=True 表示"这可能是 from X import y 里的子模块 y"，
    猜错了不算外部依赖，否则 registry/tool 这类名字会污染统计。
    """
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read(), filename=path)
    except SyntaxError as exc:
        return [("<语法错误: %s>" % exc.msg, exc.lineno or 0, 0, False)]

    found: list[tuple[str, int, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno, 0, False))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.append((base, node.lineno, node.level or 0, False))
            for alias in node.names:                     # from X import y 可能是子模块
                if alias.name != "*":
                    found.append((f"{base}.{alias.name}" if base else alias.name,
                                  node.lineno, node.level or 0, True))
    return found


def build_edges(modules: dict[str, str]) -> tuple[dict[str, set[str]], dict[str, int], set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    where: dict[str, int] = {}
    external: set[str] = set()

    for name, path in modules.items():
        package = name.split(".")[:-1]
        for raw, lineno, level, soft in parse_imports(path):
            if level:                                    # 相对导入
                up = level - 1
                base = package[:-up] if up else package
                candidates = [".".join(base + [raw])] if raw else [".".join(base)]
            else:
                candidates = [raw]

            target = next((t for c in candidates if (t := _resolve(c, modules))), None)
            if target is None:
                top = raw.split(".")[0]
                if top and not top.startswith(".") and not soft:
                    external.add(top)
                continue
            if target != name:
                edges[name].add(target)
                where.setdefault(f"{name}->{target}", lineno)
    return edges, where, external


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    sys.setrecursionlimit(10000)
    color: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            state = color.get(nxt, 0)
            if state == 0:
                dfs(nxt)
            elif state == 1:
                path = stack[stack.index(nxt):] + [nxt]
                if path not in cycles:
                    cycles.append(path)
        stack.pop()
        color[node] = 2

    for node in sorted(edges):
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def mermaid(edges: dict[str, set[str]], max_nodes: int) -> str:
    nodes = sorted({n for n in edges} | {v for vs in edges.values() for v in vs})[:max_nodes]
    keep = set(nodes)
    lines = ["graph LR"]
    for src in sorted(edges):
        if src not in keep:
            continue
        for dst in sorted(edges[src]):
            if dst in keep:
                lines.append(f"  {src.replace('.', '_')}[{src}] --> {dst.replace('.', '_')}[{dst}]")
    if len(keep) < len({n for n in edges} | {v for vs in edges.values() for v in vs}):
        lines.append(f"  %% 节点超过 {max_nodes} 个，已截断")
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    max_nodes = DEFAULT_MAX_NODES
    for flag in argv[1:]:
        if flag.startswith("--max-nodes="):
            max_nodes = int(flag.split("=", 1)[1])

    if not os.path.isdir(root):
        print(f"错误：不是目录 -> {root}")
        return 2

    modules = collect_modules(root)
    if not modules:
        print(f"在 {root} 下没找到 .py 文件")
        return 1

    edges, where, external = build_edges(modules)
    cycles = find_cycles(edges)
    edge_count = sum(len(v) for v in edges.values())

    print(f"源码根: {root}")
    print(f"模块数: {len(modules)}    内部依赖边: {edge_count}")
    print(f"外部依赖: {', '.join(sorted(external)) if external else '（无）'}")

    print("\n依赖明细:")
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            print(f"  {src} -> {dst}   (line {where.get(f'{src}->{dst}', 0)})")

    print("\n循环依赖:")
    if cycles:
        for path in cycles:
            print("  " + " -> ".join(path))
    else:
        print("  无")

    print("\n" + mermaid(edges, max_nodes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
