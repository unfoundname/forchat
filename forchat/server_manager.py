"""RunManager：run 目录之上的服务层（列表/状态/运行图/附件/停止/替换/限流/metrics 缓存）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

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
        self._summary_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
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

    def stop_worker(self, run_id: str, worker_id: str) -> bool | None:
        runtime = self._runtimes.get(run_id)
        if runtime is None:
            return None
        return runtime.force_stop(worker_id)

    def replace_worker(self, run_id: str, worker_id: str, task: str | None) -> str | None:
        runtime = self._runtimes.get(run_id)
        if runtime is None:
            return None
        child = runtime.replace_worker(worker_id, task)
        return child.id if child is not None else ""

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

    def artifacts(self, run_id: str) -> list[dict[str, Any]] | None:
        run_dir = self.runs_root / run_id
        if not run_dir.is_dir():
            return None
        folder = run_dir / "artifacts"
        if not folder.is_dir():
            return []
        items = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                items.append({
                    "name": str(path.relative_to(folder)).replace("\\", "/"),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
        return items

    def artifact_path(self, run_id: str, name: str) -> Path | None:
        folder = (self.runs_root / run_id / "artifacts").resolve()
        if not folder.is_dir():
            return None
        target = (folder / name).resolve()
        if not str(target).startswith(str(folder)) or not target.is_file():
            return None
        return target

    def status(self, run_id: str) -> dict[str, Any] | None:
        run_dir = self.runs_root / run_id
        if not (run_dir / "events.jsonl").is_file() and not list(run_dir.glob("events.*.jsonl")):
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
        stuck = sum(len(rt.stuck_workers()) for rt in list(self._runtimes.values()))
        status_counts: dict[str, int] = {}
        for item in runs:
            status_counts[str(item["status"])] = status_counts.get(str(item["status"]), 0) + 1
        status_lines = "".join(
            f'forchat_runs_by_status{{status="{status}"}} {count}\n'
            for status, count in sorted(status_counts.items())
        )
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
            "# HELP forchat_stuck_workers Live workers without a recent heartbeat.\n"
            "# TYPE forchat_stuck_workers gauge\n"
            f"forchat_stuck_workers {stuck}\n"
            "# HELP forchat_runs_by_status Runs grouped by final status.\n"
            "# TYPE forchat_runs_by_status gauge\n"
            f"{status_lines}"
        )
        self._metrics_cache = (now, text)
        return text

    def _iter_runs(self, limit: int):
        if not self.runs_root.is_dir():
            return []
        runs = [p for p in self.runs_root.iterdir() if p.is_dir() and (p / "events.jsonl").is_file()]
        runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return runs[:limit]

    def _signature(self, run_dir: Path) -> tuple[int, int]:
        total = 0
        latest = 0
        for path in list(run_dir.glob("events*.jsonl")) + [run_dir / "tools.jsonl"]:
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            latest = max(latest, stat.st_mtime_ns)
        return latest, total

    def _summary(self, run_dir: Path) -> dict[str, Any]:
        signature = self._signature(run_dir)
        cached = self._summary_cache.get(run_dir.name)
        if cached is not None and cached[0] == signature:
            return cached[1]

        log = EventLog(run_dir)
        finished = log.query(type="run_finished")
        last = finished[-1] if finished else None
        report = log.query(type="report")
        latest = report[-1].get("report") if report else None
        thread = self._threads.get(run_dir.name)
        runtime = self._runtimes.get(run_dir.name)
        stuck = runtime.stuck_workers() if runtime is not None else []
        summary = {
            "id": run_dir.name,
            "status": (last or {}).get("status", "running"),
            "workers": (last or {}).get("workers"),
            "tokens": (last or {}).get("total_tokens"),
            "cost_usd": (last or {}).get("total_cost_usd"),
            "elapsed": (last or {}).get("elapsed"),
            "alive": bool(thread and thread.is_alive()),
            "stuck": stuck,
            "last_report": latest,
        }
        self._summary_cache[run_dir.name] = (signature, summary)
        return summary
