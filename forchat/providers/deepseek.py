"""DeepSeek Provider：全项目唯一使用 openai SDK 的地方。

只做一件事：把 SDK 的流式 chunk 转成普通 dict（与假 provider 同形），
所以运行时、测试、演示都不需要知道 SDK 的存在。
"""
from __future__ import annotations

import os
from typing import Any, Iterator

DEFAULT_MODEL = "deepseek-flash"


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        thinking: bool = True,
        request_timeout: float = 600.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = base_url
        self.thinking = thinking
        self.request_timeout = request_timeout
        self._client: Any = None

    def _client_or_raise(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")
            try:
                from openai import OpenAI      # 延迟 import：没有 SDK 也能跑假 provider
            except ImportError as exc:
                raise RuntimeError("缺少 openai SDK：pip install openai") from exc
            self._client = OpenAI(
                api_key=self.api_key, base_url=self.base_url, timeout=self.request_timeout
            )
        return self._client

    def stream(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict] | None = None
    ) -> Iterator[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "model": model if model not in ("", "fake") else DEFAULT_MODEL,
            "messages": messages,
            "stream": True,
            # 让服务端在最后一帧带 usage，否则预算只能靠字符数估算
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        stream = self._client_or_raise().chat.completions.create(**kwargs)
        for chunk in stream:
            yield chunk.model_dump(exclude_none=True)
