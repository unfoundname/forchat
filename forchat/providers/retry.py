"""RetryProvider：包装任意 Provider，只对**瞬时错误**重试。

两条铁律：

1. **业务失败不重试**：worker 判 failed 的不该重来（会重复副作用）；
2. **已经开始输出就不重试**：分片一旦吐给运行时，重来会导致内容重复。

默认只重试超时 / 连接 / 限流 / 5xx。次数与退避可配。
"""
from __future__ import annotations

import time
from typing import Any, Iterator


def _default_transient() -> tuple[type[BaseException], ...]:
    try:
        from openai import (  # 延迟 import：没有 SDK 也能用假 provider
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except ImportError:
        return (TimeoutError, ConnectionError)
    return (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


class RetryProvider:
    name = "retry"

    def __init__(
        self,
        inner: Any,
        attempts: int = 2,
        backoff: float = 0.5,
        transient: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self.inner = inner
        self.attempts = max(1, int(attempts))
        self.backoff = max(0.0, float(backoff))
        self.transient = tuple(transient) if transient else _default_transient()

    def stream(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict] | None = None
    ) -> Iterator[dict[str, Any]]:
        last_error: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            started = False
            try:
                for chunk in self.inner.stream(model, messages, tools):
                    started = True
                    yield chunk
                return
            except self.transient as exc:            # type: ignore[misc]
                if started:
                    raise                           # 已经流出去的内容不能重来
                last_error = exc
                if attempt >= self.attempts:
                    raise
                if self.backoff:
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
        if last_error is not None:                  # pragma: no cover - 循环必有 return/raise
            raise last_error
