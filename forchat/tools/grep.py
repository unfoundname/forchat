"""grep：按正则搜索项目文本，返回 路径:行号: 内容。"""
from __future__ import annotations

import re
from pathlib import Path

from ..registry import tool
from ._scan import iter_files


@tool(
    description="在项目里按正则搜索文本，返回 路径:行号: 内容。"
                "找符号/调用点/报错文案时先用它，不要靠猜路径逐个 read。",
    params={
        "pattern": "Python 正则，例如 'def spawn' 或 'TODO|FIXME'",
        "path": "搜索起点目录，默认当前目录",
        "include": "只搜匹配的文件名 glob，例如 '*.py'（默认全部）",
        "max_results": "最多返回多少条，默认 100",
    },
)
def grep(
    pattern: str,
    path: str = ".",
    include: str = "",
    max_results: int = 100,
) -> str:
    base = Path(path)
    if not base.is_dir():
        return f"错误：目录不存在 -> {base}"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"错误：不是合法正则：{exc}"

    limit = max(1, int(max_results))
    hits: list[str] = []
    scanned = 0
    for file in iter_files(base, include or "**/*"):
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                target = file.relative_to(Path.cwd()) if file.is_absolute() else file
                hits.append(f"{target}:{number}: {line.strip()[:200]}")
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    if not hits:
        return f"（扫描 {scanned} 个文件，无匹配）"
    header = f"（扫描 {scanned} 个文件，命中 {len(hits)} 条{'，已截断' if len(hits) >= limit else ''}）"
    return header + "\n" + "\n".join(hits)
