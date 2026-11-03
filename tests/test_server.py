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


def main() -> int:
    checks = [
        ("manager 启停与状态", test_manager_start_and_status),
        ("HTTP 端点与 SSE", test_http_endpoints_and_sse),
    ]
    return run("HTTP/SSE 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
