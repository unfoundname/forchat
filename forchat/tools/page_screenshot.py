"""page_screenshot：用无头浏览器截网页（不弹出浏览器窗口）。

实现：调用本机 Edge/Chrome 的 `--headless=new --screenshot`，不创建可见窗口；
适合把网页版界面截成 PNG 再交给视觉模型。

- 只支持 http/https；
- 默认拒绝内网地址（复用 web_fetch 的 SSRF 判断），测试可设 FORCHAT_WEB_ALLOW_PRIVATE=1；
- 需要本机安装 Edge 或 Chrome；找不到时明确报错。
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from .. import exec as exec_mod
from ..registry import tool
from .web_fetch import blocked_host

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _browser() -> str | None:
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    return None


@tool(
    description="用无头浏览器截一个网页为 PNG（不弹出浏览器窗口）。需要本机 Edge/Chrome；"
                "只支持 http/https，默认拒绝内网地址。返回图像标记。",
    params={
        "url": "网页地址",
        "width": "视口宽度，默认 1366",
        "height": "视口高度，默认 900",
    },
)
def page_screenshot(url: str, width: int = 1366, height: int = 900) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(url).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "错误：只支持 http/https URL"
    if blocked_host(parsed.hostname):
        return f"错误：拒绝访问内网/本机地址 {parsed.hostname}（测试可设 FORCHAT_WEB_ALLOW_PRIVATE=1）"
    browser = _browser()
    if not browser:
        return "错误：没有找到 Edge/Chrome，无法做无头网页截图"

    folder = Path(".screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"page-{int(time.time())}.png"
    profile = Path(tempfile.mkdtemp(prefix="forchat-page-"))
    command = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={max(320, int(width))},{max(240, int(height))}",
        f"--screenshot={out.resolve()}", parsed.geturl(),
    ]
    try:
        proc = exec_mod.run_simple(command, scrub=False, timeout=90)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not out.is_file() or out.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return f"错误：无头截图失败（rc={proc.returncode}）：{tail}"
    return f"网页已截取 @@IMAGE:{out.resolve()}@@"
