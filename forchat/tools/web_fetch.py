"""web_fetch：抓一个 URL 的文本（只读工具）。

- 只支持 http/https；
- SSRF 防护：拒绝内网/本机/元数据地址，**每次重定向都重新校验**；
  IPv4 的十进制/八进制/十六进制/简写形式与 IPv6（含映射地址）都归一化后再判断；
- 测试可用 FORCHAT_WEB_ALLOW_PRIVATE=1 放行；
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


def _allow_private() -> bool:
    return os.environ.get("FORCHAT_WEB_ALLOW_PRIVATE") == "1"


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把常见字面量写法归一化成 IP；不是 IP 就返回 None。

    覆盖：标准 IPv4/IPv6、`inet_aton` 接受的十进制/八进制/十六进制/短写（127.1、2130706433、0x7f000001）。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)      # Windows/POSIX 都按历史规则解析
    except OSError:
        return None
    try:
        return ipaddress.ip_address(packed)
    except ValueError:
        return None


def _blocked_ip(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global


def blocked_host(host: str, allow_private: bool | None = None) -> bool:
    """判断主机是否禁止访问（fail-closed：解析失败也拒绝）。"""
    if allow_private is None:
        allow_private = _allow_private()
    if allow_private:
        return False
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host or host in BLOCKED or host.endswith(".localhost"):
        return True
    literal = _literal_ip(host)
    if literal is not None:
        return _blocked_ip(literal)
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    if not infos:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if _blocked_ip(ip):
            return True
    return False


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一步重定向都重新做主机校验，防止公网 302 → 内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or blocked_host(host):
            raise urllib.error.HTTPError(
                newurl, code, f"redirect blocked (SSRF): {host}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _html_to_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"[ \t]{2,}", " ", text)


@tool(
    description="抓取一个 URL 的文本内容（只读）。用于查新 API 文档、看线上报错、读别人的仓库页面。"
                "默认拒绝内网地址（重定向逐跳校验）；返回内容会截断。",
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
    if not host:
        return "错误：URL 缺少主机名"
    if blocked_host(host):
        return f"错误：拒绝访问内网/本机地址 {host}（如确需访问，设 FORCHAT_WEB_ALLOW_PRIVATE=1）"

    request = urllib.request.Request(url, headers={"User-Agent": "forchat/0.1"})
    opener = urllib.request.build_opener(SafeRedirectHandler())
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        return f"错误：HTTP {exc.code} {url}（{exc.reason}）"
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
