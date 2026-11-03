"""EventLog：append-only 的不可变事件流，是"完整存储"的物理形态。

一条事件 = 一个 dict，落成 JSONL（一行一条）。查、重放、复盘都读它，不读对话历史。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterator


class EventLog:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 写
    def append(self, type: str, **fields: Any) -> dict[str, Any]:
        event = {"type": type, "ts": time.time(), **fields}
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return event

    def artifact(self, name: str, content: str) -> str:
        """落一个附件，返回相对路径（报告里引用的就是它）。"""
        safe = name.replace("/", "_").replace("\\", "_")
        target = self.root / "artifacts" / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.root))

    # ------------------------------------------------------------------ 读
    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def iter_events(self) -> Iterator[dict[str, Any]]:
        yield from self.read()

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
        return self.path.stat().st_size / 1024 / 1024 if self.path.is_file() else 0.0
