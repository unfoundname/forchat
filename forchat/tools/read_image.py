"""read_image：把本地图片变成运行时能看的多模态消息。

返回 `@@IMAGE:<path>@@`；运行时在 FORCHAT_VISION=1 时转成 image_url 消息。
用于视觉对比：reference 截图和实现截图都可以先 read_image 再看。
"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool

ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@tool(
    description="读取本地图片（PNG/JPG/WebP/GIF），返回图像标记，供视觉模型查看。",
    params={"path": "图片路径（必须在项目工作区内）"},
)
def read_image(path: str) -> str:
    target = Path(path)
    if not target.is_file():
        return f"错误：图片不存在 -> {path}"
    if target.suffix.lower() not in ALLOWED:
        return f"错误：不支持的图片格式 {target.suffix}"
    return f"@@IMAGE:{target.resolve()}@@"
