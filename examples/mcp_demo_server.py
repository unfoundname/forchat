"""一个极小的 MCP stdio 服务端，用来测试 forchat 的 MCP 桥。

只实现 initialize / tools/list / tools/call，协议消息一行一个 JSON。
注意：stdout 只能写协议消息，任何日志都必须走 stderr，否则客户端会解析失败。

直接运行（手动喂一行 JSON 看响应）：
    echo {"jsonrpc":"2.0","id":1,"method":"tools/list"} | python examples/mcp_demo_server.py
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回传入的文本，用来验证链路是否通",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "两个数字相加",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个加数"},
                "b": {"type": "number", "description": "第二个加数"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "server_time",
        "description": "返回服务端当前时间",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

PROTOCOL_VERSION = "2024-11-05"


class RPCError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def ok(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": str(text)}], "isError": is_error}


def handle_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        return ok(args.get("text", ""))
    if name == "add":
        try:
            return ok(float(args["a"]) + float(args["b"]))
        except KeyError as exc:
            return ok(f"缺少参数 {exc}", True)
        except (TypeError, ValueError) as exc:
            return ok(f"参数不是数字：{exc}", True)
    if name == "server_time":
        return ok(time.strftime("%Y-%m-%d %H:%M:%S"))
    return ok(f"未知工具：{name}", True)


def dispatch(msg: dict) -> dict | None:
    method = msg.get("method")
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "forchat-demo", "version": "0.1.0"},
        }
    if method == "notifications/initialized":
        return None                                   # 通知没有响应
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return handle_call(msg.get("params") or {})
    raise RPCError(-32601, f"不支持的方法：{method}")


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"坏消息：{exc}", file=sys.stderr)
            continue

        rid = msg.get("id")
        try:
            result = dispatch(msg)
        except RPCError as exc:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": exc.code, "message": exc.message}})
            continue
        except Exception as exc:                       # noqa: BLE001
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32603, "message": str(exc)}})
            continue

        if rid is None or result is None:
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
