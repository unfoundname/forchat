"""工具注册表：一个装饰器 = 一份 JSON Schema + 一个可调用体。

对照单文件版 agent.py 里手写的 TOOLS：
- name / 参数类型从函数签名自动推导，schema 与实现永远同步；
- JSON 解析失败、参数不匹配统一在这里兜住，工具函数不必自己处理；
- 新增工具 = 新增一个文件，不用改这里的任何一行。
"""
from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _build_parameters(func: Callable[..., Any], param_docs: dict[str, str]) -> dict:
    """函数签名 -> JSON Schema。只支持 str/int/float/bool 和 Optional[...]。"""
    hints = typing.get_type_hints(func)
    sig = inspect.signature(func)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        hint = hints.get(pname, str)
        optional = param.default is not inspect.Parameter.empty

        origin = typing.get_origin(hint)
        if origin in (typing.Union, types.UnionType):      # X | None
            non_none = [a for a in typing.get_args(hint) if a is not type(None)]
            if non_none:
                hint = non_none[0]
                optional = True

        prop: dict[str, Any] = {"type": _PY_TO_JSON.get(hint, "string")}
        if pname in param_docs:
            prop["description"] = param_docs[pname]
        properties[pname] = prop
        if not optional:
            required.append(pname)

    return {"type": "object", "properties": properties, "required": required}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    # 本地工具：用 **kwargs 调用；MCP 工具为 None
    func: Callable[..., Any] | None = None
    # 外部工具（MCP）：直接吃原始 arguments JSON 字符串，参数结构不受本地 schema 限制
    raw_caller: Callable[[str], str] | None = None

    def to_spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str | None = None, *, description: str = "", params: dict[str, str] | None = None):
    """注册一个工具。params 只用于给单个参数补 description。"""

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        tname = name or func.__name__
        REGISTRY[tname] = Tool(
            name=tname,
            description=description or inspect.getdoc(func) or "",
            parameters=_build_parameters(func, params or {}),
            func=func,
        )
        return func

    return deco


def register_external(
    name: str,
    description: str,
    parameters: dict,
    caller: Callable[[str], str],
) -> None:
    """注册外部工具（MCP）：schema 由对端给出，执行走 caller(原始参数 JSON)。"""
    REGISTRY[name] = Tool(
        name=name,
        description=description,
        parameters=parameters,
        func=None,
        raw_caller=caller,
    )


def specs() -> list[dict]:
    """发给模型的 tools 参数。"""
    return [t.to_spec() for t in REGISTRY.values()]


def names() -> list[str]:
    return sorted(REGISTRY)


def execute(name: str, arguments: str) -> str:
    """执行一次工具调用。任何异常都变成字符串回执，交给模型自己纠错。"""
    item = REGISTRY.get(name)
    if item is None:
        return f"错误：没有名为 {name} 的工具，可用：{', '.join(names())}"

    # 外部工具（MCP）：参数在对端校验，这里只负责把原始 JSON 递过去
    if item.raw_caller is not None:
        try:
            return str(item.raw_caller(arguments or "{}"))
        except Exception as exc:                     # noqa: BLE001
            return f"错误：{type(exc).__name__}: {exc}"

    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return f"错误：参数不是合法 JSON（{exc}），请重新生成完整参数，不要截断"

    if not isinstance(args, dict):
        return "错误：参数必须是 JSON 对象"

    try:
        return str(item.func(**args))
    except TypeError as exc:
        return f"错误：参数与工具定义不匹配（{exc}），请检查必填项"
    except Exception as exc:                     # noqa: BLE001
        return f"错误：{type(exc).__name__}: {exc}"


def load_tools(package: str = "forchat.tools") -> list[str]:
    """扫描包内所有模块并 import，让 @tool 装饰器自动完成注册。"""
    mod = importlib.import_module(package)
    loaded = []
    for info in pkgutil.iter_modules(mod.__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{package}.{info.name}")
        loaded.append(info.name)
    return loaded
