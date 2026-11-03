"""Coordinator 侧的远程执行器：把 worker payload 放进共享目录队列，等结果。

Runtime 只认识 `remote_executor(payload, cancel) -> result` 这个注入接口；
具体队列/文件系统由这里负责（原语层不依赖远程传输）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .remote_queue import RemoteQueue


class RemoteExecutor:
    def __init__(self, queue_root: Path | str, timeout: float = 86400.0) -> None:
        self.queue = RemoteQueue(queue_root)
        self.timeout = timeout

    def suspects(self) -> list[dict]:
        """心跳超时的任务（只标记，不自动重派；父 agent 视察后再决定）。"""
        return self.queue.suspects()

    def requeue(self, task_id: str) -> bool:
        """视察确认需要重派时调用。"""
        return self.queue.requeue(task_id)

    def __call__(self, payload: dict[str, Any], cancel: Callable[[], bool]) -> dict[str, Any]:
        task_id = str(payload["worker"]["id"])
        self.queue.submit(task_id, payload)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            result = self.queue.result(task_id)
            if result is not None:
                return result
            if cancel():
                self.queue.cancel(task_id)
                for _ in range(20):
                    time.sleep(0.1)
                    result = self.queue.result(task_id)
                    if result is not None:
                        return result
                return {"status": "killed", "report": {}}
            time.sleep(0.2)
        return {"status": "failed", "report": {"summary": "远程执行超时"}}
