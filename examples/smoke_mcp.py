"""MCP 链路自检：不需要 API key，不经过模型，直接验证协议与桥接层。

    python examples/smoke_mcp.py

通过就说明：子进程起来了、initialize 成功、tools/list 拿到了、tools/call 能回、
原始 JSON 参数能正确穿过 registry.execute。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forchat import registry, tools  # noqa: F401  (触发本地工具注册)
from forchat.mcp import MCPClient
from forchat.mcp_bridge import connect_from_config


def main() -> int:
    print("== 协议层 ==")
    client = MCPClient("demo", "python", ["examples/mcp_demo_server.py"]).start()
    print("serverInfo:", client.server_info)
    print("tools:", [tool["name"] for tool in client.list_tools()])
    print("add(2, 40) ->", client.call_tool("add", {"a": 2, "b": 40}))
    print("echo('hi')  ->", client.call_tool("echo", {"text": "hi"}))
    print("add 缺参数  ->", client.call_tool("add", {"a": 1}))
    client.restart()
    print("重启后 add  ->", client.call_tool("add", {"a": 3, "b": 4}))
    client.close()

    print("\n== 桥接层（经 registry.execute）==")
    connect_from_config()
    print("mcp__demo__add  ->", registry.execute("mcp__demo__add", '{"a": 1, "b": 2}'))
    print("mcp__demo__echo ->", registry.execute("mcp__demo__echo", '{"text": "round trip"}'))
    print("坏 JSON         ->", registry.execute("mcp__demo__add", "not-json"))
    print("本地工具仍在     ->", registry.execute("read", '{"path": "mcp_servers.json"}')[:60].replace("\n", " "))

    print(f"\n共 {len(registry.names())} 个工具：{', '.join(registry.names())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
