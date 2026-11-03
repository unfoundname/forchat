"""EventLog：append-only 的不可变事件流，是"完整存储"的物理形态。

一条事件 = 一个 dict，落成 JSONL（一行一条）。查、重放、复盘都读它，不读对话历史。

三个工程约束：

1. **分段**：单文件超过 `FORCHAT_EVENT_LOG_MB`（默认 32）就滚动成 `events.<n>.jsonl`，
   长跑不会把内存和单文件一起撑爆；`read()` 按段顺序拼回来。
2. **耐久**：每条都 flush；`report` / `worker_done` / `run_finished` 这类关键事件额外 fsync，
   崩溃时丢的只是"正在写的那一行"，不会丢状态。
3. **线程安全**：并发 worker 同时 append 用锁串行化。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

# 这些事件出现后必须落盘（崩溃恢复依赖它们）
DURABLE_TYPES = {"report", "worker_done", "run_finished", "run_budget_exhausted", "run_resumed"}
SYNC_EVERY = 50


class EventLog:
    def __init__(self, root: Path | str, max_mb: float | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        size = max_mb if max_mb is not None else os.environ.get("FORCHAT_EVENT_LOG_MB", 32)
        self.max_bytes = max(4096, int(float(size) * 1024 * 1024))
        self._lock = threading.Lock()
        self._since_sync = 0

    # ------------------------------------------------------------------ 写
    def append(self, type: str, **fields: Any) -> dict[str, Any]:
        event = {"type": type, "ts": time.time(), **fields}
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._maybe_rotate()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                self._since_sync += 1
                if type in DURABLE_TYPES or self._since_sync >= SYNC_EVERY:
                    os.fsync(handle.fileno())
                    self._since_sync = 0
        return event

    def _segments(self) -> list[Path]:
        segs = sorted(
            (p for p in self.root.glob("events.*.jsonl")),
            key=lambda p: int(p.name.split(".")[1]) if p.name.split(".")[1].isdigit() else 0,
        )
        if self.path.is_file():
            segs.append(self.path)
        return segs

    def _maybe_rotate(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size < self.max_bytes:
            return
        existing = [p for p in self.root.glob("events.*.jsonl")]
        index = len(existing) + 1
        target = self.root / f"events.{index}.jsonl"
        while target.exists():
            index += 1
            target = self.root / f"events.{index}.jsonl"
        self.path.replace(target)

    def artifact(self, name: str, content: str) -> str:
        """落一个附件，返回相对路径（报告里引用的就是它）。"""
        safe = name.replace("/", "_").replace("\\", "_")
        target = self.root / "artifacts" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.root))

    # ------------------------------------------------------------------ 读
    def read(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in self._segments():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def query(
        self,
        type: str | None = None,
        worker_id: str | None = None,
        since_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        out = []
        for event in self.read():
            if type and event.get("type") != type:
                continue
            if worker_id and event.get("worker_id") != worker_id:
                continue
            if since_ts and event.get("ts", 0) < since_ts:
                continue
            out.append(event)
        return out

    def size_mb(self) -> float:
        total = sum(p.stat().st_size for p in self._segments())
        return total / 1024 / 1024
