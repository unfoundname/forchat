"""远程 worker agent：从共享目录队列租任务，启动 `forchat.worker_proc` 执行。

    python -m forchat.remote_agent --queue <run-dir>/remote [--once] [--poll 0.5]

agent 只负责"领任务 + 起子进程 + 回传结果 + 心跳 + 取消"；具体执行复用 worker_proc。
跨主机部署时要求 queue 目录（通常就是 run 目录）在共享文件系统上，且两边代码/解释器一致。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import exec as exec_mod
from .remote_queue import RemoteQueue


def _run_task(task_id: str, payload: dict, queue: RemoteQueue, agent_id: str) -> dict:
    result_path = queue.root / "results" / f"{task_id}.worker.json"
    payload = dict(payload)
    payload["result_path"] = str(result_path)

    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(5.0):
            queue.heartbeat(task_id, agent_id)

    threading.Thread(target=_beat, daemon=True).start()
    proc = exec_mod.spawn(
        [sys.executable, "-m", "forchat.worker_proc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
        scrub=False,
    )
    assert proc.stdin
    proc.stdin.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    proc.stdin.flush()

    cancelled = False
    while proc.poll() is None:
        if queue.cancelled(task_id):
            cancelled = True
            proc.kill()
            break
        time.sleep(0.2)
    stop.set()
    proc.communicate()

    if cancelled:
        result_path.unlink(missing_ok=True)
        return {"status": "killed", "report": {}}
    if result_path.is_file():
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"status": "failed", "report": {"summary": "remote worker 没有产出结果"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForChat remote worker agent")
    parser.add_argument("--queue", required=True, help="queue 目录（通常是 <run-dir>/remote）")
    parser.add_argument("--agent-id", default=f"{os.uname().nodename}-{os.getpid()}"
                        if hasattr(os, "uname") else f"agent-{os.getpid()}")
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--stale", type=float, default=1800.0,
                        help="租约多久没心跳算 suspect（默认 1800s，只标记不重派）")
    parser.add_argument("--once", action="store_true", help="执行一个任务后退出")
    args = parser.parse_args(argv)

    queue = RemoteQueue(Path(args.queue))
    while True:
        queue.mark_suspects(args.stale)
        leased = queue.lease(args.agent_id)
        if leased is None:
            if args.once:
                return 0
            time.sleep(args.poll)
            continue
        task_id, payload = leased
        result = _run_task(task_id, payload, queue, args.agent_id)
        queue.complete(task_id, args.agent_id, result)
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
