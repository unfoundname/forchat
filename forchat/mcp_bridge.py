"""MCP 桥：读配置 → 拉起服务端子进程 → 把它的工具注册进本地工具表。

暴露给模型的工具名带前缀 `mcp__<server>__<tool>`，避免和本地工具撞名，
也让模型（和日志）一眼看出这个工具是外来的。

配置文件 mcp_servers.json：
    {
      "servers": [
        {"name": "demo", "command": "python", "args": ["examples/mcp_demo_server.py"]}
      ]
    }
"""
from __future__ import annotations

import json
from pathlib import Path

from . import registry
from .mcp import MCPClient, MCPError

CONFIG_PATH = Path("mcp_servers.json")
_clients: list[MCPClient] = []
_loaded = False


def register_server_tool(server: str, client: MCPClient, spec: dict) -> str:
    original = spec.get("name", "")
    exposed = f"mcp__{server}__{original}"[:64]
    description = spec.get("description") or f"{server} 提供的工具 {original}"
    parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def caller(raw: str, _client: MCPClient = client, _name: str = original) -> str:
        try:
            args = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"参数不是合法 JSON（{exc}）") from exc
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是 JSON 对象")
        try:
            return _client.call_tool(_name, args)
        except MCPError:
            # 子进程崩了/超时：重启一次再试，失败就把异常交给 registry 变成错误回执
            _client.restart()
            return _client.call_tool(_name, args)

    registry.register_external(exposed, description, parameters, caller)
    return exposed


def connect_from_config(config_path: Path | str | None = None) -> list[MCPClient]:
    """连接配置里的所有 MCP 服务端。失败不影响本地工具，只打印警告。"""
    global _loaded
    if _loaded:
        return _clients
    _loaded = True

    path = Path(config_path or CONFIG_PATH)
    if not path.is_file():
        return _clients

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[mcp] 配置读取失败：{exc}")
        return _clients

    for entry in config.get("servers", []):
        name = entry.get("name") or "server"
        if not entry.get("command"):
            print(f"[mcp] {name}: 缺少 command，跳过")
            continue
        try:
            client = MCPClient(
                name,
                entry["command"],
                args=entry.get("args"),
                env=entry.get("env"),
                cwd=entry.get("cwd"),
                timeout=float(entry.get("timeout", 20)),
            ).start()
            tools = client.list_tools()
        except Exception as exc:                    # noqa: BLE001
            print(f"[mcp] {name}: 连接失败 —— {exc}")
            continue

        registered = [register_server_tool(name, client, tool) for tool in tools]
        _clients.append(client)
        info = client.server_info or {}
        print(
            f"[mcp] {name}: 已连接 {info.get('name', '?')} {info.get('version', '')}，"
            f"注册 {len(registered)} 个工具"
        )
        for tool_name in registered:
            print(f"[mcp]   - {tool_name}")

    return _clients


def close_all() -> None:
    global _loaded
    for client in _clients:
        client.close()
    _clients.clear()
    _loaded = False
