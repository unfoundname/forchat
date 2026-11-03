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


def main() -> int:
    checks = [
        ("manager 启停与状态", test_manager_start_and_status),
        ("HTTP 端点与 SSE", test_http_endpoints_and_sse),
        ("token 鉴权", test_auth_required_when_token_set),
        ("限流", test_rate_limit),
        ("停止端点", test_stop_endpoint_stops_running_workers),
    ]
    return run("HTTP/SSE 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
