"""page_text：用无头浏览器导出网页正文，供"必须出现这些文案"的确定性检查。"""
from __future__ import annotations

import html
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .. import exec as exec_mod
from ..registry import tool
from .page_screenshot import _browser
from .web_fetch import blocked_host

TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")


@tool(
    description="用无头浏览器打开网页并提取正文文本（去掉 HTML 标签），"
                "用于前端验收：断言导航/标题/关键文案存在。只支持 http/https，默认拒绝内网。",
    params={
        "url": "网页地址",
        "max_chars": "最多返回多少字符，默认 8000",
    },
)
def page_text(url: str, max_chars: int = 8000) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "错误：只支持 http/https URL"
    if blocked_host(parsed.hostname):
        return f"错误：拒绝访问内网/本机地址 {parsed.hostname}（测试可设 FORCHAT_WEB_ALLOW_PRIVATE=1）"
    browser = _browser()
    if not browser:
        return "错误：没有找到 Edge/Chrome，无法提取网页正文"

    profile = Path(tempfile.mkdtemp(prefix="forchat-dom-"))
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
        return f"错误：无头浏览器取 DOM 失败（rc={proc.returncode}）：{(proc.stderr or '')[-300:]}"
    text = html.unescape(TAG.sub(" ", proc.stdout or ""))
    text = SPACE.sub(" ", text).strip()
    try:
        limit = max(200, int(max_chars))
    except (TypeError, ValueError):
        limit = 8000
    if not text:
        return "错误：页面没有可提取的正文"
    suffix = "" if len(text) <= limit else f"\n...（截断，共 {len(text)} 字符）"
    return text[:limit] + suffix
