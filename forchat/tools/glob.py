"""glob：按通配符列出文件路径（找文件用，不要逐层 list_dir）。"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool
from ._scan import iter_files


@tool(
    description="按 glob 模式列出文件路径，例如 '**/*.py' 或 'examples/**/*.json'。"
                "不确定文件在哪时先用它，再用 read/grep。",
    params={
        "pattern": "glob 模式，例如 '**/*.py'",
        "path": "搜索起点目录，默认当前目录",
        "max_results": "最多返回多少条，默认 200",
    },
)
def glob(pattern: str, path: str = ".", max_results: int = 200) -> str:
    base = Path(path)
    if not base.is_dir():
        return f"错误：目录不存在 -> {base}"

    limit = max(1, int(max_results))
    root = Path.cwd()
    matches: list[str] = []
    for file in iter_files(base, pattern):
        try:
            matches.append(str(file.relative_to(root)))
        except ValueError:
            matches.append(str(file))
        if len(matches) >= limit:
            break

    if not matches:
        return f"（没有匹配 {pattern} 的文件）"
    header = f"（命中 {len(matches)} 个文件{'，已截断' if len(matches) >= limit else ''}）"
    return header + "\n" + "\n".join(sorted(matches))
