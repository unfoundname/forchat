"""可选：把模型请求放进子进程，换真正的硬中断（kill 子进程）。

为什么只做到"请求级"：worker 的执行循环、工具调用、快照都在父进程；真正会无限挂住的
是模型网络请求（流式读取）。把这个调用放进子进程，父进程就能在 stop/超时时直接 kill，
不会留下卡死的线程。

子进程协议（stdin 一行 JSON，stdout 每行一个 JSON）：
    请求：{"model": "...", "messages": [...], "tools": [...]}
    输出：{"chunk": {...}} / {"error": "..."} / {"done": true}

子进程的 provider 由环境变量选择：
    FORCHAT_STREAM_CHILD_SCRIPT=<json 文件>   离线脚本模式（测试用）
    否则用 DeepSeekProvider（需要 DEEPSEEK_API_KEY）
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

CHUNK_TIMEOUT = float(os.environ.get("FORCHAT_CHUNK_TIMEOUT", 300))


class StreamKilled(RuntimeError):
    """子进程被取消/超时/异常退出。"""


class ProcessStreamFactory:
    """每次 stream 调用起一个子进程；cancel 或超时就 kill。"""

    def __init__(self, model: str, python: str | None = None, chunk_timeout: float | None = None):
        self.model = model
        self.python = python or sys.executable
        self.chunk_timeout = chunk_timeout or CHUNK_TIMEOUT

    def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        cancel: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        request = json.dumps(
            {"model": self.model, "messages": messages, "tools": tools or []},
            ensure_ascii=False,
            default=str,
        )
        proc = subprocess.Popen(
            [self.python, "-m", "forchat.stream_child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)),
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(request + "\n")
        proc.stdin.flush()

        lines: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        try:
            while True:
                deadline = time.time() + self.chunk_timeout
                line: str | None = None
                while line is None:
                    if cancel is not None and cancel.is_set():
                        raise StreamKilled("cancelled")
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise StreamKilled(f"chunk timeout >{self.chunk_timeout}s")
                    try:
                        line = lines.get(timeout=min(0.2, remaining))
                    except queue.Empty:
                        continue
                if line is None:
                    break
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("error"):
                    raise StreamKilled(str(payload["error"]))
                if payload.get("done"):
                    break
                if payload.get("chunk") is not None:
                    yield payload["chunk"]
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def child_main() -> int:
    """子进程入口：读一行请求，流式输出 chunk。"""
    from .providers.deepseek import DeepSeekProvider

    raw = sys.stdin.readline()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad request: {exc}"}), flush=True)
        return 2

    script_path = os.environ.get("FORCHAT_STREAM_CHILD_SCRIPT")
    try:
        if script_path:
            chunks = json.loads(Path(script_path).read_text(encoding="utf-8"))
            provider = None
        else:
            provider = DeepSeekProvider()
            chunks = None
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 2

    if provider is None:
        for chunk in chunks or []:
            if isinstance(chunk, dict) and "sleep" in chunk:
                import time

                time.sleep(float(chunk["sleep"]))
                continue
            print(json.dumps({"chunk": chunk}, ensure_ascii=False, default=str), flush=True)
        print(json.dumps({"done": True}), flush=True)
        return 0

    try:
        for chunk in provider.stream(request.get("model", "deepseek-chat"),
                                     request.get("messages") or [],
                                     request.get("tools") or None):
            print(json.dumps({"chunk": chunk}, ensure_ascii=False, default=str), flush=True)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 1
    print(json.dumps({"done": True}), flush=True)
    return 0
