"""image_palette：比较两张图的全局配色，给设计 token 漂移一个可测指标。"""
from __future__ import annotations

from pathlib import Path

from ..registry import tool


def _palette(path: str, colors: int) -> list[tuple[tuple[int, int, int], float]]:
    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((400, 400))
    quantized = image.quantize(colors=max(2, min(32, colors)))
    palette = quantized.getpalette() or []
    counts = quantized.getcolors() or []
    total = sum(count for count, _ in counts) or 1
    result = []
    for count, index in counts:
        base = index * 3
        rgb = tuple(palette[base:base + 3])
        if len(rgb) == 3:
            result.append((rgb, count / total))
    return sorted(result, key=lambda item: item[1], reverse=True)


def _nearest(rgb: tuple[int, int, int], others: list[tuple[tuple[int, int, int], float]]) -> float:
    best = 1.0
    for other, _weight in others:
        distance = sum((a - b) ** 2 for a, b in zip(rgb, other)) ** 0.5
        best = min(best, distance / 441.67)       # 441.67 = sqrt(3)*255
    return best


@tool(
    description="提取两张图的全局配色（主色+占比），返回配色差异百分比；"
                "用于判断整体配色漂移，避免只看像素差。",
    params={
        "reference": "参考图路径",
        "candidate": "实现图路径",
        "colors": "调色板颜色数，默认 8",
    },
)
def image_palette(reference: str, candidate: str, colors: int = 8) -> str:
    for path in (Path(reference), Path(candidate)):
        if not path.is_file():
            return f"错误：图片不存在 -> {path}"
    try:
        ref = _palette(reference, colors)
        cand = _palette(candidate, colors)
    except Exception as exc:  # noqa: BLE001 - 图片/解码问题统一回执
        return f"错误：提取配色失败：{exc}"

    forward = sum(weight * _nearest(rgb, cand) for rgb, weight in ref)
    backward = sum(weight * _nearest(rgb, ref) for rgb, weight in cand)
    distance = (forward + backward) / 2 * 100
    ref_hex = ", ".join(f"#{r:02x}{g:02x}{b:02x}({w * 100:.0f}%)" for (r, g, b), w in ref[:6])
    cand_hex = ", ".join(f"#{r:02x}{g:02x}{b:02x}({w * 100:.0f}%)" for (r, g, b), w in cand[:6])
    return (f"配色差异 {distance:.1f}%\n参考主色: {ref_hex}\n实现主色: {cand_hex}")
