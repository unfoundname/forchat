"""image_diff：比较参考图与实现图，给出客观差异指标 + 差异热力图。"""
from __future__ import annotations

import time
from pathlib import Path

from ..registry import tool


@tool(
    description="比较两张图片：返回差异像素比例、平均通道差，并保存红色差异热力图标记。",
    params={
        "reference": "参考图路径（基线）",
        "candidate": "待测图路径（实现）",
        "threshold": "单像素通道差阈值 0-255，默认 24（越小越严格）",
    },
)
def image_diff(reference: str, candidate: str, threshold: int = 24) -> str:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return "错误：没有安装 Pillow，无法比较图片"

    ref_path, cand_path = Path(reference), Path(candidate)
    for path in (ref_path, cand_path):
        if not path.is_file():
            return f"错误：图片不存在 -> {path}"
    try:
        ref = Image.open(ref_path).convert("RGB")
        cand = Image.open(cand_path).convert("RGB")
    except OSError as exc:
        return f"错误：图片打不开：{exc}"

    resized = ref.size != cand.size
    if resized:
        cand = cand.resize(ref.size)

    diff = ImageChops.difference(ref, cand)
    r, g, b = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    try:
        limit = max(0, min(255, int(threshold)))
    except (TypeError, ValueError):
        limit = 24
    mask = max_diff.point(lambda value: 255 if value > limit else 0)
    total = ref.width * ref.height
    changed = sum(count for value, count in enumerate(mask.histogram()) if value)
    ratio = (changed / total) if total else 0.0
    mean = sum(value * count for value, count in enumerate(max_diff.histogram())) / total

    folder = Path(".screenshots")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"diff-{int(time.time())}.png"
    red = Image.new("RGB", ref.size, (255, 0, 0))
    heat = Image.composite(red, ref, mask)
    heat.save(out)

    note = "（尺寸不同，已把 candidate 缩放到 reference）" if resized else ""
    return (f"差异像素 {ratio * 100:.2f}%（阈值 {limit}），平均通道差 {mean:.1f}；"
            f"reference={ref.width}x{ref.height} {note}\n@@IMAGE:{out.resolve()}@@")
