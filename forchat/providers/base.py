"""Provider：模型侧的唯一接口。运行时只认 stream()，不认任何厂商字段。"""
from __future__ import annotations

from typing import Any, Iterator, Protocol


class Provider(Protocol):
    name: str

    def stream(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict] | None = None
    ) -> Iterator[dict[str, Any]]: ...
