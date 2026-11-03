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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .primitives import EventLog, Runtime, WorkflowDef, WorkflowError


class RunManager:
    def __init__(
        self,
        runs_root: Path,
        workflows_root: Path,
        provider_factory: Callable[[], Any],
        external_tools_factory: Callable[[], dict] | None = None,
        skill_loader: Callable[[str], str | None] | None = None,
        guard: Callable[[str, str, list[str]], str | None] | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.workflows_root = Path(workflows_root)
        self.provider_factory = provider_factory
        self.external_tools_factory = external_tools_factory
        self.skill_loader = skill_loader
        self.guard = guard
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 启动
    def start(self, workflow: str, goal: str) -> dict[str, str]:
        package = self.workflows_root / workflow / "workflow.json"
        if not package.is_file():
            raise ValueError(f"workflow 不存在：{workflow}")
        try:
            definition = WorkflowDef.load(package)
        except WorkflowError as exc:
            raise ValueError(f"workflow 配置无效：{exc}") from exc

        run_dir = self.runs_root / f"{workflow}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time()*1000)%1000:03d}"
        runtime = Runtime(
            definition,
            self.provider_factory(),
            run_dir,
            external_tools=self.external_tools_factory() if self.external_tools_factory else None,
            skill_loader=self.skill_loader,
            guard=self.guard,
        )

        thread = threading.Thread(target=runtime.start, args=(goal,), name=f"run-{run_dir.name}", daemon=True)
        with self._lock:
            self._threads[run_dir.name] = thread
        thread.start()
        return {"id": run_dir.name, "run_dir": str(run_dir)}

    # ------------------------------------------------------------------ 读
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._summary(path) for path in self._iter_runs(limit)]

    def status(self, run_id: str) -> dict[str, Any] | None:
        run_dir = self.runs_root / run_id
        if not (run_dir / "events.jsonl").is_file():
            return None
        return self._summary(run_dir)

    def _iter_runs(self, limit: int):
        if not self.runs_root.is_dir():
            return []
        runs = [p for p in self.runs_root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()]
        runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return runs[:limit]

    def _summary(self, run_dir: Path) -> dict[str, Any]:
        log = EventLog(run_dir)
        finished = log.query(type="run_finished")
        last = finished[-1] if finished else None
        report = log.query(type="report")
        latest = report[-1].get("report") if report else None
        thread = self._threads.get(run_dir.name)
        return {
            "id": run_dir.name,
            "status": (last or {}).get("status", "running"),
            "workers": (last or {}).get("workers"),
            "tokens": (last or {}).get("total_tokens"),
            "cost_usd": (last or {}).get("total_cost_usd"),
            "elapsed": (last or {}).get("elapsed"),
            "alive": bool(thread and thread.is_alive()),
            "last_report": latest,
        }


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

        def do_GET(self) -> None:                  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"ok": True})
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
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:                 # noqa: N802
            if urlparse(self.path).path.rstrip("/") != "/runs":
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
            events_path = run_dir / "events.jsonl"
            if not events_path.is_file():
                self._json(404, {"error": "run not found"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            offset = 0
            finished = False
            try:
                while True:
                    with events_path.open("r", encoding="utf-8") as handle:
                        handle.seek(offset)
                        for line in handle:
                            if not line.strip():
                                continue
                            offset += len(line.encode("utf-8"))
                            self.wfile.write(b"data: " + line.encode("utf-8") + b"\n\n")
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
    server = ThreadingHTTPServer((host, port), _make_handler(manager))
    print(f"[serve] http://{host}:{port}  （无鉴权，仅限本机）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _default_manager() -> RunManager:
    from .policy import Policy
    from .providers.deepseek import DeepSeekProvider
    from .providers.retry import RetryProvider
    from .tool_bridge import build as build_tools, build_skill_loader

    root = Path.cwd()
    return RunManager(
        runs_root=root / ".runs",
        workflows_root=root / "examples" / "workflows",
        provider_factory=lambda: RetryProvider(DeepSeekProvider(), attempts=2),
        external_tools_factory=build_tools,
        skill_loader=build_skill_loader(),
        guard=Policy(root).check,
    )
