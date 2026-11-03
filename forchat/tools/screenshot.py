"""screenshot：抓当前屏幕为 PNG，返回图像标记（视觉模型可看）。

返回 `@@IMAGE:<path>@@`；运行时在 FORCHAT_VISION=1 时会把它换成多模态 user 消息
（OpenAI 兼容的 image_url 格式）。不装 Pillow / 无桌面时返回错误回执。
"""
from __future__ import annotations

import time
from pathlib import Path

from ..registry import tool


@tool(
    description="截取当前屏幕保存为 PNG，供视觉模型查看（例如调前端/看渲染结果）。"
                "返回图像标记，运行时负责把它转成模型能看的图片消息。",
    params={"name": "文件名（可选），例如 login.png"},
)
def screenshot(name: str = "") -> str:
    try:
        from PIL import ImageGrab
    except ImportError:
        return "错误：没有安装 Pillow，无法截屏"

    folder = Path(".screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name.strip() or f"shot-{int(time.time())}.png")
    try:
        image = ImageGrab.grab()
        image.save(target)
    except Exception as exc:  # noqa: BLE001 - 无桌面/无权限都归到这里
        return f"错误：截屏失败：{exc}"
    return f"@@IMAGE:{target.resolve()}@@"
