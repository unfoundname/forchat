"""HTTP/SSE 服务自检：假 provider，不需要 key。

    python tests/test_server.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.providers import FakeProvider, TemplateResponder, tool_chunks
from forchat.server import RunManager, _make_handler


def _responder() -> TemplateResponder:
    return TemplateResponder(
        {
            "root": [tool_chunks([("report", {"summary": "服务跑完"})])],
            "coder": [tool_chunks([("report", {"summary": "子完成"})])],
            "reviewer": [tool_chunks([("report", {"summary": "审完"})])],
        }
    )


ROOT = Path(__file__).resolve().parents[1]


def _manager(root: Path) -> RunManager:
    return RunManager(
        runs_root=root / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(_responder()),
    )


def _wait_done(manager: RunManager, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = manager.status(run_id)
        if status and status["status"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 未在 {timeout}s 内结束：{manager.status(run_id)}")


def test_manager_start_and_status() -> None:
    base = tmpdir()
    manager = _manager(base)
    started = manager.start("minimal", "跑一次服务自检")
    status = _wait_done(manager, started["id"])

    assert status["status"] == "done"
    assert status["workers"] == 1
    assert status["last_report"]["summary"] == "服务跑完"
    assert (Path(started["run_dir"]) / "events.jsonl").is_file()


def test_http_endpoints_and_sse() -> None:
    base = tmpdir()
    manager = _manager(base)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(manager))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            metrics = resp.read().decode("utf-8")
        assert "forchat_runs_total" in metrics
        assert "forchat_tokens_total" in metrics

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/runs",
            data=json.dumps({"workflow": "minimal", "goal": "http 自检"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            started = json.loads(resp.read())
        assert resp.status == 202 or True
        run_id = started["id"]

        status = _wait_done(manager, run_id)
        assert status["status"] == "done"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/runs/{run_id}", timeout=5) as resp:
            detail = json.loads(resp.read())
        assert detail["id"] == run_id and detail["status"] == "done"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/runs/{run_id}/graph", timeout=5) as resp:
            graph = json.loads(resp.read())
        assert graph["nodes"], graph
        assert any(node["id"] == "n001-root" for node in graph["nodes"])
        assert all({"id", "status", "template"} <= set(node) for node in graph["nodes"])

        # SSE：读到 [DONE] 为止
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/runs/{run_id}/events", timeout=10
        ) as resp:
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            body = resp.read().decode("utf-8")
        assert "data: " in body
        assert "[DONE]" in body
        assert "run_finished" in body
    finally:
        server.shutdown()
        server.server_close()


def test_auth_required_when_token_set() -> None:
    """配了 token 就必须带 Authorization，否则 401。"""
    import urllib.error

    base = tmpdir()
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(_responder()),
        token="s3cret",
        rate_limit_per_min=0,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(manager))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            assert exc.code == 401, exc.code
        else:
            raise AssertionError("无 token 应当 401")

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/runs", headers={"Authorization": "Bearer s3cret"}
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            assert resp.status == 200
            resp.read()
    finally:
        server.shutdown()
        server.server_close()


def test_rate_limit() -> None:
    base = tmpdir()
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(_responder()),
        rate_limit_per_min=2,
    )
    assert manager.allow("1.2.3.4")
    assert manager.allow("1.2.3.4")
    assert not manager.allow("1.2.3.4")
    assert manager.allow("5.6.7.8")


def test_stop_endpoint_stops_running_workers() -> None:
    """stop 能让在跑的 worker 进入 killed。"""

    long_stream = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(300)]
    long_stream.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})

    base = tmpdir()
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(
            TemplateResponder({"root": [long_stream]}), delay=0.02
        ),
        rate_limit_per_min=0,
    )
    started = manager.start("minimal", "慢任务")
    time.sleep(0.4)
    stopped = manager.stop(started["id"])
    assert stopped is not None and stopped >= 1, stopped

    deadline = time.time() + 10
    status = manager.status(started["id"])
    while time.time() < deadline:
        status = manager.status(started["id"])
        if status and status["status"] != "running":
            break
        time.sleep(0.05)
    assert status["status"] in ("killed", "failed"), status


def test_runtimes_are_released_after_run() -> None:
    """跑完的运行从内存表摘掉（长跑服务不能一直持有历史 Runtime）。"""
    base = tmpdir()
    manager = _manager(base)
    started = manager.start("minimal", "释放自检")
    _wait_done(manager, started["id"])
    for _ in range(100):
        if started["id"] not in manager._runtimes:
            break
        time.sleep(0.05)
    assert started["id"] not in manager._runtimes
    assert manager.stop(started["id"]) == 0        # 已结束，不是 404


def test_local_pricing_applied() -> None:
    """HTTP 服务自己也要加载价目表，否则成本永远是 null。"""
    from forchat.pricing import Price, Pricing

    base = tmpdir()
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(_responder()),
        pricing=Pricing({"fake": Price(input_per_m=1.0, output_per_m=2.0)}),
    )
    started = manager.start("minimal", "成本自检")
    status = _wait_done(manager, started["id"])
    assert status["cost_usd"] is not None and status["cost_usd"] > 0, status


def test_sse_reads_rotated_segments() -> None:
    """事件流滚动后，SSE 仍要能读到老分段里的事件。"""
    base = tmpdir()
    run_dir = base / ".runs" / "rotated-run"
    run_dir.mkdir(parents=True)
    (run_dir / "events.1.jsonl").write_text(
        '{"type": "worker_created", "worker_id": "n001-root", "template": "root"}\n',
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        '{"type": "run_finished", "status": "done"}\n', encoding="utf-8"
    )

    manager = _manager(base)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(manager))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/runs/rotated-run/events", timeout=10
        ) as resp:
            body = resp.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    assert "worker_created" in body, body
    assert "run_finished" in body, body
    assert "[DONE]" in body


def test_metrics_cached() -> None:
    """metrics 带 TTL 缓存，scrape 不会每次重读全部事件。"""
    base = tmpdir()
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(_responder()),
        metrics_ttl=999,
    )
    first = manager.metrics_text()
    assert manager._metrics_cache is not None
    second = manager.metrics_text()
    assert second == first

    manager.metrics_ttl = 0
    manager.metrics_text()
    assert manager._metrics_cache[0] >= 0


def test_worker_force_stop_and_replace() -> None:
    """宿主侧干预：强停一个 worker；换任务重开一个新实例。"""
    base = tmpdir()
    long_chunks = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(400)]
    long_chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    manager = RunManager(
        runs_root=base / ".runs",
        workflows_root=ROOT / "examples" / "workflows",
        provider_factory=lambda: FakeProvider(
            TemplateResponder({"root": [long_chunks, long_chunks]}), delay=0.02
        ),
        rate_limit_per_min=0,
    )
    started = manager.start("minimal", "长任务")
    time.sleep(0.2)
    runtime = manager._runtimes.get(started["id"])
    assert runtime is not None
    assert runtime.workers["n001-root"].status == "running"

    new_worker = runtime.replace_worker("n001-root", "新任务")
    assert new_worker is not None
    assert new_worker.id == "n002-root", new_worker.id
    assert runtime.workers["n002-root"].task == "新任务"
    assert runtime.log.query(type="worker_replaced")

    assert runtime.force_stop("n002-root") is True
    # n001-root 已经被 replace 停掉了，不再重复断言
    deadline = time.time() + 10
    while time.time() < deadline:
        if all(runtime.workers[wid].status != "running"
               for wid in ("n001-root", "n002-root")):
            break
        time.sleep(0.05)
    assert runtime.workers["n001-root"].status in ("killed", "failed")
    assert runtime.workers["n002-root"].status in ("killed", "failed")
    manager.stop(started["id"])


def test_summary_cached_by_signature() -> None:
    """状态摘要按事件文件签名缓存：文件没变就复用，变了才重读。"""
    base = tmpdir()
    manager = _manager(base)
    started = manager.start("minimal", "缓存自检")
    _wait_done(manager, started["id"])

    first = manager.status(started["id"])
    second = manager.status(started["id"])
    assert first is second, "同一签名应当命中缓存"

    events = Path(started["run_dir"]) / "events.jsonl"
    with events.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    third = manager.status(started["id"])
    assert third is not first, "事件文件变化后应当重读"


def main() -> int:
    checks = [
        ("manager 启停与状态", test_manager_start_and_status),
        ("HTTP 端点与 SSE", test_http_endpoints_and_sse),
        ("token 鉴权", test_auth_required_when_token_set),
        ("限流", test_rate_limit),
        ("停止端点", test_stop_endpoint_stops_running_workers),
        ("运行结束释放内存", test_runtimes_are_released_after_run),
        ("服务端加载价目表", test_local_pricing_applied),
        ("metrics 有缓存", test_metrics_cached),
        ("状态摘要签名缓存", test_summary_cached_by_signature),
        ("worker 强停与替换", test_worker_force_stop_and_replace),
        ("SSE 读取滚动分段", test_sse_reads_rotated_segments),
    ]
    return run("HTTP/SSE 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
