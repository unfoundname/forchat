"""page_outline：导出网页的语义结构（landmark/标题层级），用于布局结构对比。"""
from __future__ import annotations

import shutil
import tempfile
from html.parser import HTMLParser
from urllib.parse import urlparse

from .. import exec as exec_mod
from ..registry import tool
from .page_screenshot import _browser
from .web_fetch import blocked_host

LANDMARKS = {"header", "nav", "main", "aside", "footer", "section", "form", "table"}
HEADINGS = {"h1", "h2", "h3"}
WATCH = LANDMARKS | HEADINGS


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.stack: list[tuple[str, int, list[str]]] = []
        self.lines: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        if tag in WATCH:
            self.stack.append((tag, self.depth, []))
        elif self.stack:
            self.stack[-1][2].append("")

    def handle_data(self, data):
        if self.stack:
            self.stack[-1][2].append(data)

    def handle_endtag(self, tag):
        if self.stack:
            name, depth, texts = self.stack[-1]
            if name == tag:
                text = " ".join(" ".join(texts).split())
                kind = "heading" if name in HEADINGS else "landmark"
                self.lines.append(f"{'  ' * min(depth, 8)}{kind}:{name}"
                                  + (f" | {text[:80]}" if text else ""))
                self.stack.pop()
        self.depth = max(0, self.depth - 1)


@tool(
    description="用无头浏览器导出网页语义结构（header/nav/main/section/h1-h3 的顺序与文本），"
                "用于检查布局结构是否被改动。只支持 http/https，默认拒绝内网。",
    params={"url": "网页地址"},
)
def page_outline(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "错误：只支持 http/https URL"
    if blocked_host(parsed.hostname):
        return f"错误：拒绝访问内网/本机地址 {parsed.hostname}（测试可设 FORCHAT_WEB_ALLOW_PRIVATE=1）"
    browser = _browser()
    if not browser:
        return "错误：没有找到 Edge/Chrome，无法导出结构"

    profile = tempfile.mkdtemp(prefix="forchat-outline-")
    command = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={profile}",
        "--dump-dom", parsed.geturl(),
    ]
    try:
        proc = exec_mod.run_simple(command, scrub=False, timeout=90)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if proc.returncode != 0 and not proc.stdout:
        return f"错误：无头浏览器取结构失败（rc={proc.returncode}）：{(proc.stderr or '')[-300:]}"
    parser = _Outline()
    parser.feed(proc.stdout or "")
    if not parser.lines:
        return "错误：页面没有可识别的 landmark/标题"
    if len(parser.lines) > 80:
        parser.lines = parser.lines[:80] + ["...（截断）"]
    return "\n".join(parser.lines)
