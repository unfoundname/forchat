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

from .primitives import EventLog, Replay, Runtime, WorkflowDef, WorkflowError


class RunManager:
    def __init__(
        self,
        runs_root: Path,
        workflows_root: Path,
        provider_factory: Callable[[], Any],
        external_tools_factory: Callable[[], dict] | None = None,
        skill_loader: Callable[[str], str | None] | None = None,
        guard: Callable[[str, str, list[str]], str | None] | None = None,
        token: str | None = None,
        rate_limit_per_min: int = 120,
        pricing: Any | None = None,
        keep_runtimes: int = 100,
        metrics_ttl: float = 5.0,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.workflows_root = Path(workflows_root)
        self.provider_factory = provider_factory
        self.external_tools_factory = external_tools_factory
        self.skill_loader = skill_loader
        self.guard = guard
        self.token = token
        self.rate_limit_per_min = rate_limit_per_min
        self.pricing = pricing
        self.keep_runtimes = max(1, keep_runtimes)
        self.metrics_ttl = max(0.0, float(metrics_ttl))
        self._metrics_cache: tuple[float, str] | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._runtimes: dict[str, Runtime] = {}
        self._rate: dict[str, tuple[float, int]] = {}
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
            pricing=self.pricing,
        )

        def _run_and_forget() -> None:
            try:
                runtime.start(goal)
            finally:
                # 跑完就从内存表里摘掉：长跑服务不应持有所有历史 Runtime
                with self._lock:
                    self._threads.pop(run_dir.name, None)
                    self._runtimes.pop(run_dir.name, None)
                    if len(self._runtimes) > self.keep_runtimes:
                        for stale in sorted(self._runtimes)[: len(self._runtimes) - self.keep_runtimes]:
                            self._runtimes.pop(stale, None)

        thread = threading.Thread(target=_run_and_forget, name=f"run-{run_dir.name}", daemon=True)
        with self._lock:
            self._threads[run_dir.name] = thread
            self._runtimes[run_dir.name] = runtime
        thread.start()
        return {"id": run_dir.name, "run_dir": str(run_dir)}

    def stop(self, run_id: str) -> int | None:
        """请求停止某个运行里的所有 worker；运行不存在返回 None，已结束返回 0。"""
        runtime = self._runtimes.get(run_id)
        if runtime is not None:
            return runtime.stop_all()
        return 0 if (self.runs_root / run_id).is_dir() else None

    def allow(self, client_ip: str) -> bool:
        """极简限流：每 IP 每分钟 N 次（0 = 关闭）。"""
        if self.rate_limit_per_min <= 0:
            return True
        now = time.time()
        with self._lock:
            start, count = self._rate.get(client_ip, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            count += 1
            self._rate[client_ip] = (start, count)
            return count <= self.rate_limit_per_min

    # ------------------------------------------------------------------ 读
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._summary(path) for path in self._iter_runs(limit)]

    def status(self, run_id: str) -> dict[str, Any] | None:
        run_dir = self.runs_root / run_id
        if not (run_dir / "events.jsonl").is_file():
            return None
        return self._summary(run_dir)

    def graph(self, run_id: str) -> dict[str, Any] | None:
        """运行图：节点 + 父子边，前端画网状结构用。"""
        run_dir = self.runs_root / run_id
        if not (run_dir / "events.jsonl").is_file() and not list(run_dir.glob("events.*.jsonl")):
            return None
        return Replay(EventLog(run_dir)).graph()

    def metrics_text(self) -> str:
        """Prometheus 文本格式。带 TTL 缓存：scrape 不能每次重读全部事件。"""
        now = time.time()
        if self._metrics_cache is not None and now - self._metrics_cache[0] < self.metrics_ttl:
            return self._metrics_cache[1]

        runs = self.list_runs(1000)
        running = sum(1 for item in runs if item["status"] == "running")
        workers = sum(int(item["workers"] or 0) for item in runs)
        tokens = sum(int(item["tokens"] or 0) for item in runs)
        cost = sum(float(item["cost_usd"] or 0) for item in runs)
        text = (
            "# HELP forchat_runs_total Total runs known to this server.\n"
            "# TYPE forchat_runs_total gauge\n"
            f"forchat_runs_total {len(runs)}\n"
            "# HELP forchat_runs_running Runs that have not finished yet.\n"
            "# TYPE forchat_runs_running gauge\n"
            f"forchat_runs_running {running}\n"
            "# HELP forchat_workers_total Workers across known runs.\n"
            "# TYPE forchat_workers_total gauge\n"
            f"forchat_workers_total {workers}\n"
            "# HELP forchat_tokens_total Tokens across known runs.\n"
            "# TYPE forchat_tokens_total gauge\n"
            f"forchat_tokens_total {tokens}\n"
            "# HELP forchat_cost_usd_total Estimated cost across known runs.\n"
            "# TYPE forchat_cost_usd_total gauge\n"
            f"forchat_cost_usd_total {cost:.6f}\n"
        )
        self._metrics_cache = (now, text)
        return text

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
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:                 # noqa: N802
            if self._deny():
                return
            path = urlparse(self.path).path.rstrip("/")
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
