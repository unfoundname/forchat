"""假 Provider：不需要网络、不需要 key，用来把整个运行时跑通和做测试。

分片格式与 OpenAI/DeepSeek 一致，所以同一套运行时既跑假 provider 也跑真 provider。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterator

Responder = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


def tool_chunks(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = [{"choices": [{"delta": {"role": "assistant", "content": ""}}]}]
    for index, (name, args) in enumerate(calls):
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": f"call_{index + 1}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args, ensure_ascii=False),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
    chunks.append(
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"completion_tokens": 20},
        }
    )
    return chunks


class FakeProvider:
    """script 可以是分片列表的列表（按调用顺序），也可以是一个 responder(messages)。"""

    name = "fake"

    def __init__(self, script: list[Any] | Responder) -> None:
        self._script = script
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    def stream(
        self, model: str, messages: list[dict[str, Any]], tools: list[dict] | None = None
    ) -> Iterator[dict[str, Any]]:
        self.seen.append(messages)
        index = self.calls
        self.calls += 1

        if callable(self._script):
            chunks = self._script(messages)
        else:
            if index >= len(self._script):
                raise RuntimeError(f"fake script 用完了（第 {index + 1} 次调用）")
            chunks = self._script[index]
        yield from chunks


class TemplateResponder:
    """按"系统提示里的模板名"分派响应，并按模板计数 —— 并行时也稳定。"""

    def __init__(self, table: dict[str, list[list[dict[str, Any]]]]) -> None:
        self.table = table
        self.counters: dict[str, int] = {}

    def __call__(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system = messages[0].get("content", "")
        template = next((name for name in self.table if f"模板 {name}" in system), None)
        if template is None:
            raise RuntimeError(f"没有为这段系统提示配置响应：{system[:80]}")
        index = self.counters.get(template, 0)
        self.counters[template] = index + 1
        script = self.table[template]
        if index >= len(script):
            raise RuntimeError(f"模板 {template} 的脚本用完了（第 {index + 1} 次）")
        return script[index]
