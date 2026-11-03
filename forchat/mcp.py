"""最小 MCP 客户端：stdio 传输 + JSON-RPC 2.0（消息以换行分隔）。

只覆盖 agent 真正需要的三个方法：initialize / tools/list / tools/call。
没实现 resources、prompts、sampling、HTTP 传输——那些按同样方式加方法即可。

协议要点（和 OpenAI 的 tool_calls 对照着看会很清楚）：
- 客户端启动服务端子进程，双方用 stdin/stdout 收发**一行一个 JSON**；
- 请求带自增 id，响应靠 id 配对 → 所以需要读线程 + 等待表，不能"发一句读一句"；
- 服务端往 stdout 打日志是协议违规，一律忽略坏行；日志该走 stderr。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any

from .env_scrub import clean_environ


class MCPError(RuntimeError):
    """协议层错误：启动失败、超时、服务端返回 error。"""


def flatten_content(result: dict) -> str:
    """把 tools/call 的 content 数组压成一段文本，方便直接当 tool 回执。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(part for part in parts if part) or "（空结果）"
    if result.get("isError"):
        text = "[工具报错] " + text
    return text


class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.name = name
        # 配置里写 python 时用当前解释器，避免 PATH 里是另一个环境
        self.command = sys.executable if command in ("python", "python3") else command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout

        self.proc: subprocess.Popen[str] | None = None
        self.server_info: dict[str, Any] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.stderr_lines: list[str] = []

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "MCPClient":
        # 继承的环境擦掉密钥；配置里显式写的 env 仍然生效（那是用户主动授予的）
        env = clean_environ(self.env)
        from . import exec as exec_mod

        self.proc = exec_mod.spawn(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            env=env,
            scrub=False,
            cwd=self.cwd,
        )
        threading.Thread(target=self._read_loop, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name=f"mcp-{self.name}-err", daemon=True).start()
        self.initialize()
        return self

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "forchat", "version": "0.1.0"},
            },
        )
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:                                    # noqa: BLE001
            try:
                proc.kill()
            except Exception:                                # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 协议
    def restart(self) -> "MCPClient":
        """服务端崩了/超时之后重建子进程与握手。工具列表由调用方决定是否重新拉取。"""
        self.close()
        self._pending.clear()
        return self.start()

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        rid = self._alloc_id()
        event = threading.Event()
        box: dict[str, Any] = {}
        self._pending[rid] = (event, box)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

        if not event.wait(timeout or self.timeout):
            self._pending.pop(rid, None)
            raise MCPError(f"[{self.name}] {method} 超时（{timeout or self.timeout}s）")
        if "error" in box:
            err = box["error"] or {}
            raise MCPError(f"[{self.name}] {method} 失败：{err.get('message')} (code {err.get('code')})")
        return box.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools") or []

    def call_tool(self, tool: str, arguments: dict) -> str:
        result = self.request(
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=max(self.timeout, 60),
        )
        return flatten_content(result)

    # ------------------------------------------------------------------ 内部
    def _alloc_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _send(self, payload: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise MCPError(f"[{self.name}] 未启动")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(f"[{self.name}] 写入失败（服务端可能已退出）：{exc}") from exc

    def _read_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                       # 服务端往 stdout 打日志，跳过
            if not isinstance(msg, dict):
                continue

            rid = msg.get("id")
            if rid is not None and "method" in msg:      # 服务端反向请求 → 不支持
                try:
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601, "message": "client 不支持该请求"}})
                except MCPError:
                    pass
                continue

            if rid is None:
                continue                       # 通知，无需配对
            slot = self._pending.pop(rid, None)
            if slot is None:
                continue
            event, box = slot
            box.update(msg)
            event.set()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            self.stderr_lines.append(raw.rstrip())
            if len(self.stderr_lines) > 200:
                self.stderr_lines.pop(0)
