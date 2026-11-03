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
        request_timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY")
                        or os.environ.get("FORCHAT_API_KEY"))
        # OpenAI 兼容端点：换 FORCHAT_BASE_URL + FORCHAT_MODEL 即可接别的 provider
        self.base_url = os.environ.get("FORCHAT_BASE_URL", base_url)
        self.thinking = thinking
        # 单次请求超时：默认 180s。600s 那种长挂会让整个运行卡死且看不出原因。
        self.request_timeout = request_timeout or float(os.environ.get("FORCHAT_REQUEST_TIMEOUT", 240))
        # 单次响应的 token 上限：thinking 也算在内，太小会截断工具参数，所以给足 8k。
        self.max_tokens = max_tokens or int(os.environ.get("FORCHAT_MAX_TOKENS", 8192))
        self._client: Any = None

    def _client_or_raise(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")
            try:
                from openai import OpenAI  # 延迟 import：没有 SDK 也能跑假 provider
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
            "max_tokens": self.max_tokens,
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
