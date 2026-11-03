"""web_search 自检：可配置搜索后端（本地假后端，不需要外网）。"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run

from forchat import registry, tools  # noqa: F401


def _without_keys(*names):
    saved = {name: os.environ.pop(name, None) for name in names}
    return saved


def _restore_keys(saved) -> None:
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


def test_search_requires_config() -> None:
    saved = _without_keys("FORCHAT_SEARCH_URL", "FORCHAT_SEARCH_KEY", "DEEPSEEK_API_KEY")
    try:
        out = registry.execute("web_search", json.dumps({"query": "x"}))
    finally:
        _restore_keys(saved)
    assert "未配置搜索 key" in out, out


def test_search_backend_returns_results() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            payload = json.dumps(
                {"results": [
                    {"title": f"结果-{body.get('query')}", "url": "https://example.com/a",
                     "snippet": "摘要内容"},
                ]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = _without_keys("FORCHAT_SEARCH_URL", "FORCHAT_SEARCH_KEY")
    os.environ["FORCHAT_SEARCH_URL"] = f"http://127.0.0.1:{server.server_address[1]}/search"
    os.environ["FORCHAT_SEARCH_KEY"] = "test-key"
    try:
        out = registry.execute("web_search", json.dumps({"query": "deepseek"}))
    finally:
        server.shutdown()
        server.server_close()
        _restore_keys(saved)

    assert "结果-deepseek" in out, out
    assert "https://example.com/a" in out, out
    assert "摘要内容" in out, out


def test_search_deepseek_schema() -> None:
    """DeepSeek Search API 的形状：count 字段 + results[].summary。"""
    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.update(body)
            payload = json.dumps(
                {"results": [{
                    "title": "DeepSeek V4",
                    "url": "https://example.com/v4",
                    "summary": "官方发布的版本摘要",
                }]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = _without_keys("FORCHAT_SEARCH_URL", "FORCHAT_SEARCH_KEY")
    os.environ["FORCHAT_SEARCH_URL"] = f"http://127.0.0.1:{server.server_address[1]}/search"
    os.environ["FORCHAT_SEARCH_KEY"] = "test-key"
    try:
        out = registry.execute("web_search", json.dumps({"query": "deepseek", "max_results": 2}))
    finally:
        server.shutdown()
        server.server_close()
        _restore_keys(saved)

    assert seen["count"] == 2, seen
    assert "官方发布的版本摘要" in out, out


def test_screenshot_marker_to_image_message() -> None:
    import os

    from _support import tmpdir
    from forchat.primitives import Runtime

    work = tmpdir()
    image = work / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    marker = f"@@IMAGE:{image}@@"

    old = os.environ.get("FORCHAT_VISION")
    os.environ["FORCHAT_VISION"] = "1"
    try:
        message = Runtime._image_message(marker)
    finally:
        if old is None:
            os.environ.pop("FORCHAT_VISION", None)
        else:
            os.environ["FORCHAT_VISION"] = old

    assert message is not None
    assert message["role"] == "user"
    assert any(part.get("type") == "image_url" for part in message["content"])

    assert Runtime._image_message(marker) is None
    assert "视觉未开启" in Runtime._normalize_image_result(marker)


def _fetch_module():
    from forchat.tools import web_fetch

    return web_fetch


def test_ssrf_numeric_encodings() -> None:
    """十进制/八进制/十六进制/短写 IP 都要归一化后拦下。"""
    wf = _fetch_module()
    blocked = [
        "2130706433",        # 127.0.0.1 的十进制
        "0x7f000001",        # 十六进制
        "0177.0.0.1",        # 八进制
        "127.1",             # 短写
        "0",                 # 0.0.0.0
    ]
    for host in blocked:
        assert wf.blocked_host(host), host


def test_ssrf_ipv6_forms() -> None:
    wf = _fetch_module()
    for host in ("::1", "::ffff:127.0.0.1", "fe80::1", "::"):
        assert wf.blocked_host(host), host
    assert not wf.blocked_host("93.184.216.34"), "公网 IP 不应被误杀"


def test_ssrf_localhost_variants() -> None:
    wf = _fetch_module()
    for host in ("localhost", "LOCALHOST.", "foo.localhost", "localhost.localdomain"):
        assert wf.blocked_host(host), host


def test_ssrf_dns_resolving_to_private() -> None:
    """域名解析到内网也必须拒绝（fail-closed）。"""
    wf = _fetch_module()
    original = wf.socket.getaddrinfo
    calls = {"n": 0}

    def fake(host, port, *args, **kwargs):
        calls["n"] += 1
        if host == "public.example":
            return [(wf.socket.AF_INET, wf.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return [(wf.socket.AF_INET, wf.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    wf.socket.getaddrinfo = fake
    try:
        assert wf.blocked_host("private.example") is True
        assert wf.blocked_host("public.example") is False
    finally:
        wf.socket.getaddrinfo = original
    assert calls["n"] >= 2


def test_ssrf_redirect_blocked() -> None:
    """公网 302 到内网/非 http(s) 必须被拦。"""
    wf = _fetch_module()
    handler = wf.SafeRedirectHandler()
    for target in (
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "file:///C:/Windows/win.ini",
    ):
        try:
            handler.redirect_request(None, None, 302, "Found", {}, target)
        except Exception as exc:                       # noqa: BLE001
            assert "redirect blocked" in str(exc), (target, exc)
        else:
            raise AssertionError(f"重定向到 {target} 应当被拒绝")


def test_fetch_deepseek_backend() -> None:
    """FORCHAT_FETCH_BACKEND=deepseek：走服务端 Fetch API，解析 title/content。"""
    seen: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            seen.update(json.loads(self.rfile.read(length) or b"{}"))
            payload = json.dumps(
                {"title": "示例页面", "content": "# 正文\n\n这是抓取到的内容。"},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = _without_keys("FORCHAT_FETCH_BACKEND", "FORCHAT_FETCH_URL",
                          "FORCHAT_FETCH_KEY", "DEEPSEEK_API_KEY")
    os.environ["FORCHAT_FETCH_BACKEND"] = "deepseek"
    os.environ["FORCHAT_FETCH_URL"] = f"http://127.0.0.1:{server.server_address[1]}/fetch"
    os.environ["FORCHAT_FETCH_KEY"] = "test-key"
    try:
        out = registry.execute(
            "web_fetch", json.dumps({"url": "http://93.184.216.34/page"})
        )
    finally:
        server.shutdown()
        server.server_close()
        _restore_keys(saved)

    assert seen["url"] == "http://93.184.216.34/page", seen
    assert "示例页面" in out and "抓取到的内容" in out, out


def test_fetch_deepseek_requires_key() -> None:
    saved = _without_keys("FORCHAT_FETCH_BACKEND", "FORCHAT_FETCH_URL",
                          "FORCHAT_FETCH_KEY", "DEEPSEEK_API_KEY")
    os.environ["FORCHAT_FETCH_BACKEND"] = "deepseek"
    try:
        out = registry.execute("web_fetch", json.dumps({"url": "http://93.184.216.34/page"}))
    finally:
        _restore_keys(saved)
    assert "需要 key" in out, out


def main() -> int:
    checks = [
        ("未配置时报错", test_search_requires_config),
        ("搜索后端返回结果", test_search_backend_returns_results),
        ("DeepSeek 搜索 schema", test_search_deepseek_schema),
        ("截图标记转多模态消息", test_screenshot_marker_to_image_message),
        ("Fetch-DeepSeek 后端", test_fetch_deepseek_backend),
        ("Fetch 缺 key 报错", test_fetch_deepseek_requires_key),
        ("SSRF-数字写法", test_ssrf_numeric_encodings),
        ("SSRF-IPv6", test_ssrf_ipv6_forms),
        ("SSRF-localhost", test_ssrf_localhost_variants),
        ("SSRF-DNS 解析内网", test_ssrf_dns_resolving_to_private),
        ("SSRF-重定向拦截", test_ssrf_redirect_blocked),
    ]
    return run("web_search 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
