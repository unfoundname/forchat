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


def test_search_requires_config() -> None:
    old = os.environ.pop("FORCHAT_SEARCH_URL", None)
    try:
        out = registry.execute("web_search", json.dumps({"query": "x"}))
    finally:
        if old is not None:
            os.environ["FORCHAT_SEARCH_URL"] = old
    assert "未配置搜索后端" in out, out


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
    old = os.environ.get("FORCHAT_SEARCH_URL")
    os.environ["FORCHAT_SEARCH_URL"] = f"http://127.0.0.1:{server.server_address[1]}/search"
    try:
        out = registry.execute("web_search", json.dumps({"query": "deepseek"}))
    finally:
        server.shutdown()
        server.server_close()
        if old is None:
            os.environ.pop("FORCHAT_SEARCH_URL", None)
        else:
            os.environ["FORCHAT_SEARCH_URL"] = old

    assert "结果-deepseek" in out, out
    assert "https://example.com/a" in out, out
    assert "摘要内容" in out, out


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


def main() -> int:
    checks = [
        ("未配置时报错", test_search_requires_config),
        ("搜索后端返回结果", test_search_backend_returns_results),
        ("截图标记转多模态消息", test_screenshot_marker_to_image_message),
    ]
    return run("web_search 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
