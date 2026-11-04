"""视觉工具自检：read_image / image_diff / page_text（无浏览器时跳过）。"""
from __future__ import annotations

import http.server
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat import registry, tools  # noqa: F401


def _make_image(path: Path, color: tuple[int, int, int], size=(120, 80)) -> None:
    from PIL import Image

    Image.new("RGB", size, color).save(path)


def test_read_image_marker() -> None:
    target = tmpdir() / "a.png"
    _make_image(target, (255, 0, 0))
    out = registry.execute("read_image", __import__("json").dumps({"path": str(target)}))
    assert out.startswith("@@IMAGE:") and "a.png" in out, out


def test_image_diff_identical_and_different() -> None:
    from PIL import Image

    work = tmpdir()
    ref, same, diff = work / "ref.png", work / "same.png", work / "diff.png"
    Image.new("RGB", (80, 60), (255, 255, 255)).save(ref)
    Image.new("RGB", (80, 60), (255, 255, 255)).save(same)
    Image.new("RGB", (80, 60), (255, 0, 0)).save(diff)

    import json

    out = registry.execute("image_diff", json.dumps({
        "reference": str(ref), "candidate": str(same),
    }))
    assert "差异像素 0.00%" in out, out

    out = registry.execute("image_diff", json.dumps({
        "reference": str(ref), "candidate": str(diff),
    }))
    assert "差异像素 100.00%" in out, out
    assert "@@IMAGE:" in out


def test_page_text_extracts_body() -> None:
    if sys.platform != "win32":
        return
    from forchat.tools.page_screenshot import _browser

    if not _browser():
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 约定
            body = "<html><body><h1>推荐</h1><nav>首页 视频</nav></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old = os.environ.get("FORCHAT_WEB_ALLOW_PRIVATE")
    os.environ["FORCHAT_WEB_ALLOW_PRIVATE"] = "1"
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        out = registry.execute("page_text", __import__("json").dumps({"url": url}))
    finally:
        server.shutdown()
        if old is None:
            os.environ.pop("FORCHAT_WEB_ALLOW_PRIVATE", None)
        else:
            os.environ["FORCHAT_WEB_ALLOW_PRIVATE"] = old
    assert "推荐" in out and "首页" in out, out


def main() -> int:
    return run("视觉工具自检", [
        ("read_image 标记", test_read_image_marker),
        ("image_diff 相同/不同", test_image_diff_identical_and_different),
        ("page_text 提取正文", test_page_text_extracts_body),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
