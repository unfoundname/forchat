"""价目表：把 token 换算成金额。

没有价目表时**明确返回 None**，报告里显示"未知"——不假装成本是 0。
价目表来源（按优先级）：

1. 显式传入的路径（CLI 的 `--pricing`）；
2. 环境变量 `FORCHAT_PRICING`；
3. 当前目录的 `pricing.json`。

格式：
    {"models": {"deepseek-chat": {"input_per_m": 0.27, "output_per_m": 1.10}}}
单价为"每百万 token 的美元"，具体数字请按你的实际合同/官网核对，本文件不内置任何价格。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Price:
    input_per_m: float
    output_per_m: float


class Pricing:
    def __init__(self, models: dict[str, Price] | None = None) -> None:
        self.models = dict(models or {})

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Pricing":
        candidate = Path(path or os.environ.get("FORCHAT_PRICING", "pricing.json"))
        if not candidate.is_file():
            return cls()
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        models: dict[str, Price] = {}
        for name, item in (raw.get("models") or {}).items():
            try:
                models[str(name)] = Price(
                    input_per_m=float(item["input_per_m"]),
                    output_per_m=float(item["output_per_m"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cls(models)

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
        price = self.models.get(model)
        if price is None:
            return None
        return round(
            prompt_tokens / 1_000_000 * price.input_per_m
            + completion_tokens / 1_000_000 * price.output_per_m,
            6,
        )

    def known(self) -> list[str]:
        return sorted(self.models)
