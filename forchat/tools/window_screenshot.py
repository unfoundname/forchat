"""window_screenshot：直接截一个应用窗口（Win32 PrintWindow，不需要浏览器）。

用途：截我们自己的软件 / 任何桌面窗口（聊天客户端、编辑器、终端）；
后台窗口也能截，不要求它在前台。失败时回退到"前台 + 屏幕区域"。

返回 `@@IMAGE:<path>@@` 标记；运行时在 FORCHAT_VISION=1 时转成多模态消息。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys
import time
from pathlib import Path

from ..registry import tool

WINDOWS = sys.platform == "win32"
PW_RENDERFULLCONTENT = 0x2


def _enable_dpi_awareness() -> None:
    """必须在取窗口坐标前开启，否则 GetWindowRect 给的是缩放后的逻辑尺寸，
    PrintWindow 会把窗口右侧/底部裁掉（高 DPI 下常见）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
    except Exception:                                  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                              # noqa: BLE001
            pass


if WINDOWS:
    _enable_dpi_awareness()
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", w.DWORD * 3)]


def _windows() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    def callback(hwnd, _):
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        found.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def _capture(hwnd: int):
    from PIL import Image

    rect = w.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc = _user32.GetWindowDC(hwnd)
    mem = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, width, height)
    _gdi32.SelectObject(mem, bmp)
    ok = _user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
    bmi = _BitmapInfo()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(width * height * 4)
    _gdi32.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(bmi), 0)
    image = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1)
    _gdi32.DeleteObject(bmp)
    _gdi32.DeleteDC(mem)
    _user32.ReleaseDC(hwnd, hdc)
    return ok, image.convert("RGB"), rect


def _fallback_grab(hwnd: int, rect):
    from PIL import ImageGrab

    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))


@tool(
    description="截取一个桌面应用窗口（Win32 PrintWindow，无需浏览器；后台窗口也可以）。"
                "title 传窗口标题的一部分；返回图像标记。",
    params={
        "title": "窗口标题的子串（不区分大小写），例如 'DeepSeek Harness'",
    },
)
def window_screenshot(title: str = "") -> str:
    if not WINDOWS:
        return "错误：窗口截图只支持 Windows"
    title = (title or "").strip().lower()
    if not title:
        return "错误：window_screenshot 需要 title（窗口标题的一部分）"
    matches = [(hwnd, name) for hwnd, name in _windows() if title in name.lower()]
    if not matches:
        return f"错误：没有找到标题包含「{title}」的可见窗口"
    hwnd, name = max(matches, key=lambda pair: _area(pair[0]))
    ok, image, rect = _capture(hwnd)
    if not ok or image.getextrema() == ((0, 0), (0, 0), (0, 0)):
        image = _fallback_grab(hwnd, rect)
    folder = Path(".screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"window-{int(time.time())}.png"
    image.save(out)
    return f"窗口「{name}」已截取 @@IMAGE:{out.resolve()}@@"


def _area(hwnd: int) -> int:
    rect = w.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
