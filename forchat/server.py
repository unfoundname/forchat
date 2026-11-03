"""HTTP/SSE 服务：把 runtime 以事件流方式暴露成后端接口。

只用标准库（`http.server`），默认只绑定 `127.0.0.1`——没有鉴权，**不要直接暴露公网**。
鉴权/多租户属于部署层（反向代理 + token），不在内核里。

接口：

    GET  /health                 → {"ok": true}
    GET  /runs                   → 运行列表（同 --runs 的数据）
    POST /runs                   → {"workflow": "minimal", "goal": "..."} → {"id": "...", "run_dir": "..."}
    GET  /runs/<id>              → 状态、worker 数、tokens、最后一份报告
    GET  /runs/<id>/events       → SSE：从第 0 条开始推送事件，运行结束发送 [DONE]
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .server_manager import RunManager


def _make_handler(manager: RunManager) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):        # 安静一点，别刷屏
            return

        def _json(self, code: int, payload: dict | list) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not manager.token:
                return True
            expected = f"Bearer {manager.token}"
            return self.headers.get("Authorization", "") == expected

        def _deny(self) -> bool:
            """鉴权 + 限流；被拒时已经写好响应，返回 True。"""
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return True
            if not manager.allow(self.client_address[0]):
                self._json(429, {"error": "rate limited"})
                return True
            return False

        def do_GET(self) -> None:                  # noqa: N802
            if self._deny():
                return
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"ok": True})
                return
            if path == "/metrics":
                body = manager.metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/runs":
                self._json(200, {"runs": manager.list_runs()})
                return
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) == 3:
                    item = manager.status(parts[2])
                    self._json(200 if item else 404, item or {"error": "not found"})
                    return
                if len(parts) == 4 and parts[3] == "events":
                    self._stream_events(parts[2])
                    return
                if len(parts) == 4 and parts[3] == "graph":
                    item = manager.graph(parts[2])
                    self._json(200 if item else 404, item or {"error": "not found"})
                    return
                if len(parts) == 4 and parts[3] == "artifacts":
                    items = manager.artifacts(parts[2])
                    self._json(200 if items is not None else 404,
                               {"artifacts": items} if items is not None
                               else {"error": "not found"})
                    return
                if len(parts) == 5 and parts[3] == "artifacts":
                    target = manager.artifact_path(parts[2], "/".join(parts[4:]))
                    if target is None:
                        self._json(404, {"error": "artifact not found"})
                        return
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{target.name}"')
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:                 # noqa: N802
            if self._deny():
                return
            path = urlparse(self.path).path.rstrip("/")
            if path.startswith("/runs/") and "/workers/" in path:
                parts = path.strip("/").split("/")
                # /runs/<id>/workers/<wid>/stop|replace
                if len(parts) == 5 and parts[2] == "workers" and parts[4] in ("stop", "replace"):
                    run_id, worker_id, action = parts[1], parts[3], parts[4]
                    if action == "stop":
                        result = manager.stop_worker(run_id, worker_id)
                        if result is None:
                            self._json(404, {"error": "run not live"})
                        elif not result:
                            self._json(409, {"error": "worker 已结束或不存在"})
                        else:
                            self._json(200, {"id": run_id, "worker_id": worker_id,
                                             "stopped": True})
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    try:
                        payload = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        payload = {}
                    new_id = manager.replace_worker(run_id, worker_id,
                                                    str(payload.get("task") or "") or None)
                    if new_id is None:
                        self._json(404, {"error": "run not live"})
                    elif not new_id:
                        self._json(409, {"error": "replace 被资源限制拒绝"})
                    else:
                        self._json(200, {"old": worker_id, "new": new_id})
                    return
            if path.startswith("/runs/") and path.endswith("/stop"):
                run_id = path.split("/")[2]
                stopped = manager.stop(run_id)
                if stopped is None:
                    self._json(404, {"error": "run not found"})
                else:
                    self._json(200, {"id": run_id, "stopped_workers": stopped})
                return
            if path != "/runs":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            workflow = str(payload.get("workflow", "minimal"))
            goal = str(payload.get("goal", "")).strip()
            if not goal:
                self._json(400, {"error": "goal 不能为空"})
                return
            try:
                started = manager.start(workflow, goal)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(202, started)

        def _stream_events(self, run_id: str) -> None:
            run_dir = manager.runs_root / run_id
            if not (run_dir / "events.jsonl").is_file() and not list(run_dir.glob("events.*.jsonl")):
                self._json(404, {"error": "run not found"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            offsets: dict[str, int] = {}
            buffers: dict[str, str] = {}
            finished = False
            resume_after: float | None = None
            raw_resume = self.headers.get("Last-Event-ID")
            if not raw_resume:
                query = dict(
                    part.split("=", 1)
                    for part in urlparse(self.path).query.split("&")
                    if "=" in part
                )
                raw_resume = query.get("since")
            if raw_resume:
                try:
                    resume_after = float(raw_resume)
                except ValueError:
                    resume_after = None

            def segments() -> list[Path]:
                rotated = sorted(
                    run_dir.glob("events.*.jsonl"),
                    key=lambda p: int(p.name.split(".")[1]) if p.name.split(".")[1].isdigit() else 0,
                )
                current = run_dir / "events.jsonl"
                return rotated + ([current] if current.is_file() else [])

            try:
                while True:
                    for path in segments():
                        offset = offsets.get(path.name, 0)
                        if not path.is_file() or path.stat().st_size <= offset:
                            continue
                        with path.open("rb") as handle:
                            handle.seek(offset)
                            chunk = handle.read()
                        offsets[path.name] = offset + len(chunk)
                        text = buffers.pop(path.name, "") + chunk.decode("utf-8", errors="replace")
                        if not text.endswith("\n"):
                            head, _, tail = text.rpartition("\n")
                            text, buffers[path.name] = head + "\n", tail
                        for line in text.splitlines():
                            if not line.strip():
                                continue
                            ts = None
                            try:
                                ts = json.loads(line).get("ts")
                            except json.JSONDecodeError:
                                pass
                            if (resume_after is not None and ts is not None
                                    and float(ts) <= resume_after):
                                continue
                            prefix = f"id: {ts}\n" if ts is not None else ""
                            self.wfile.write(
                                (prefix + "data: " + line + "\n\n").encode("utf-8")
                            )
                            if '"type": "run_finished"' in line:
                                finished = True
                    self.wfile.flush()
                    if finished:
                        break
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                return
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    manager_factory: Callable[[], RunManager] | None = None,
) -> None:
    manager = manager_factory() if manager_factory else _default_manager()
    if not manager.token and host not in ("127.0.0.1", "localhost"):
        raise SystemExit("绑定非本机地址时必须设置 FORCHAT_SERVER_TOKEN")
    server = ThreadingHTTPServer((host, port), _make_handler(manager))
    auth = "Bearer token" if manager.token else "无鉴权（仅建议本机）"
    print(f"[serve] http://{host}:{port}  auth={auth}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _default_manager() -> RunManager:
    from .policy import Policy
    from .pricing import Pricing
    from .providers.deepseek import DeepSeekProvider
    from .providers.retry import RetryProvider
    from .tool_bridge import build as build_tools, build_skill_loader

    root = Path.cwd()
    pricing = Pricing.load()
    return RunManager(
        runs_root=root / ".runs",
        workflows_root=root / "examples" / "workflows",
        provider_factory=lambda: RetryProvider(DeepSeekProvider(), attempts=2),
        external_tools_factory=build_tools,
        skill_loader=build_skill_loader(),
        guard=Policy(root).check,
        pricing=pricing if pricing.known() else None,
        token=os.environ.get("FORCHAT_SERVER_TOKEN") or None,
        rate_limit_per_min=int(os.environ.get("FORCHAT_RATE_LIMIT", 120)),
        metrics_ttl=float(os.environ.get("FORCHAT_METRICS_TTL", 5)),
    )
