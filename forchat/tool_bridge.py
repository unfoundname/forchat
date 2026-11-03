"""能力工具 → 原语层的唯一桥接。

为什么要有这一层：原语层（primitives）不许 import 工具实现或 registry（依赖只能向下）。
它只接受一种朴素结构：

    { "read": (openai_tool_spec, caller(raw_arguments_json) -> str), ... }

所以"工具"只有一个来源（registry），"执行"只有一个实现（registry.execute），
这里只做形状转换，没有任何业务。
"""
from __future__ import annotations

from typing import Callable

from . import registry

# name -> (OpenAI tool spec, 执行函数：吃原始参数 JSON，返回字符串回执)
ExternalTools = dict[str, tuple[dict, Callable[[str], str]]]


def build() -> ExternalTools:
    """把当前 registry 里的能力工具（本地 + MCP）全部导出。"""
    tools: ExternalTools = {}
    for name in registry.names():
        item = registry.REGISTRY[name]

        def caller(raw: str, _name: str = name) -> str:
            return registry.execute(_name, raw)

        tools[name] = (item.to_spec(), caller)
    return tools


def known_names() -> set[str]:
    """workflow 校验时需要的"这台机器上现在有哪些能力工具"。"""
    return set(registry.names())
