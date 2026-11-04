"""截图能力自检：窗口截图（无浏览器）与无头网页截图（非 Windows 跳过）。"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run

from forchat import registry, tools  # noqa: F401

WINDOWS = sys.platform == "win32"

try:                                       # 直接 `python tests/test_capture.py` 时没有 pytest
    import pytest

    pytestmark = pytest.mark.skipif(not WINDOWS, reason="截图能力只在 Windows 桌面验证")
except ImportError:                        # pragma: no cover
    pass


def _image_path(output: str) -> Path:
    match = re.search(r"@@IMAGE:(.+?)@@", output)
    assert match, output
    return Path(match.group(1))


def test_window_screenshot_own_app() -> None:
    """Tk 窗口当"我们的软件"：不经过浏览器，直接截窗口。

    会真的弹一个 Tk 窗口，默认不跑；要验证窗口截图时显式设
    `FORCHAT_TEST_WINDOW=1`。产品运行不会创建任何 Tk 窗口。
    """
    if os.environ.get("FORCHAT_TEST_WINDOW") != "1":
        try:
            import pytest

            pytest.skip("窗口截图测试需显式设 FORCHAT_TEST_WINDOW=1")
        except ImportError:
            return
    try:
        import tkinter as tk
    except ImportError:                    # pragma: no cover - 精简 Python 可能没 Tk
        return

    from PIL import Image

    root = tk.Tk()
    root.title("ForChat Capture Test")
    root.geometry("420x260+120+120")
    tk.Label(root, text="ForChat window capture").pack(expand=True)
    root.update()
    time.sleep(0.4)
    root.update()
    try:
        out = registry.execute(
            "window_screenshot", json.dumps({"title": "ForChat Capture Test"})
        )
    finally:
        root.destroy()

    path = _image_path(out)
    assert path.is_file()
    image = Image.open(path).convert("RGB")
    assert image.width >= 300 and image.height >= 200
    assert image.getextrema() != ((0, 0), (0, 0), (0, 0)), "窗口截图不能全黑"


def test_headless_page_screenshot() -> None:
    """无头浏览器截图：不弹可见窗口。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = "<html><body><h1>ForChat Page</h1><p>no browser window</p></body></html>"
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    from PIL import Image

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    old = os.environ.get("FORCHAT_WEB_ALLOW_PRIVATE")
    os.environ["FORCHAT_WEB_ALLOW_PRIVATE"] = "1"
    try:
        out = registry.execute(
            "page_screenshot",
            json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}/",
                        "width": 800, "height": 600}),
        )
    finally:
        server.shutdown()
        server.server_close()
        if old is None:
            os.environ.pop("FORCHAT_WEB_ALLOW_PRIVATE", None)
        else:
            os.environ["FORCHAT_WEB_ALLOW_PRIVATE"] = old

    assert "@@IMAGE:" in out, out
    path = _image_path(out)
    assert path.is_file() and path.stat().st_size > 1000
    assert Image.open(path).width >= 300


def test_page_screenshot_blocks_private() -> None:
    old = os.environ.pop("FORCHAT_WEB_ALLOW_PRIVATE", None)
    try:
        out = registry.execute(
            "page_screenshot", json.dumps({"url": "http://127.0.0.1/"})
        )
    finally:
        if old is not None:
            os.environ["FORCHAT_WEB_ALLOW_PRIVATE"] = old
    assert "拒绝访问内网" in out


def test_dpi_awareness_enabled() -> None:
    """高 DPI 下必须 DPI-aware，否则 GetWindowRect 缩水、PrintWindow 裁掉右侧。"""
    import ctypes

    user32 = ctypes.windll.user32
    try:
        context = user32.GetThreadDpiAwarenessContext()
        awareness = user32.GetAwarenessFromDpiAwarenessContext(context)
    except Exception:                                  # noqa: BLE001
        return                                          # 旧系统没有该 API
    assert awareness >= 1, f"DPI awareness={awareness}，窗口截图会被裁边"


def main() -> int:
    if not WINDOWS:
        print("跳过：截图能力只在 Windows 桌面验证")
        return 0
    checks = [
        ("窗口截图（无浏览器）", test_window_screenshot_own_app),
        ("无头网页截图", test_headless_page_screenshot),
        ("内网地址被拒", test_page_screenshot_blocks_private),
        ("DPI awareness", test_dpi_awareness_enabled),
    ]
    return run("截图能力自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
