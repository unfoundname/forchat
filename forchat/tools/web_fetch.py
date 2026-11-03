"""web_fetch：抓一个 URL 的文本（只读工具）。

- 只支持 http/https；
- 默认拒绝内网/本机/元数据地址（SSRF 防护）；测试可用 FORCHAT_WEB_ALLOW_PRIVATE=1 放行；
- 限时 20s、限读 500KB、返回截断到 max_chars。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..registry import tool

TIMEOUT = 20
MAX_BYTES = 500_000
BLOCKED = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254", "metadata"}


def _is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _html_to_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"[ \t]{2,}", " ", text)


@tool(
    description="抓取一个 URL 的文本内容（只读）。用于查新 API 文档、看线上报错、读别人的仓库页面。"
                "默认拒绝内网地址；返回内容会截断。",
    params={
        "url": "http(s) URL",
        "max_chars": "最多返回多少字符，默认 12000",
    },
)
def web_fetch(url: str, max_chars: int = 12000) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in ("http", "https"):
        return "错误：只支持 http/https URL"
    host = parsed.hostname or ""
    allow_private = os.environ.get("FORCHAT_WEB_ALLOW_PRIVATE") == "1"
    if not host:
        return "错误：URL 缺少主机名"
    if not allow_private and (host in BLOCKED or _is_private(host)):
        return f"错误：拒绝访问内网/本机地址 {host}（如确需访问，设 FORCHAT_WEB_ALLOW_PRIVATE=1）"

    request = urllib.request.Request(url, headers={"User-Agent": "forchat/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        return f"错误：HTTP {exc.code} {url}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"错误：抓取失败 {url}: {exc}"

    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        text = _html_to_text(text)
    text = text.strip()
    limit = max(200, int(max_chars))
    if len(text) > limit:
        text = text[:limit] + f"\n...（已截断，共 {len(text)} 字符）"
    return text or "（空内容）"
