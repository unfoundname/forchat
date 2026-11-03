"""共享目录任务队列：coordinator 提交，远程 agent 租约执行。

目录结构（放在 run 目录下即可，要求 coordinator 与 agent 能访问同一文件系统）：

    remote/tasks/<task>.json       待租约
    remote/leases/<task>.<agent>.json  已租约（mtime = 心跳）
    remote/results/<task>.json     执行结果
    remote/cancels/<task>.cancel   取消标记

租约用同目录 `rename` 原子抢占；agent 定期刷新 lease mtime，超时由 `requeue_stale` 放回。
这是"共享文件系统"语义，不提供跨主机网络传输——NFS/SMB 要自己保证一致性与锁语义。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class RemoteQueue:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        for name in ("tasks", "leases", "results", "cancels"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def task_path(self, task_id: str) -> Path:
        return self.root / "tasks" / f"{task_id}.json"

    def _write(self, path: Path, data: dict) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def submit(self, task_id: str, payload: dict) -> None:
        self._write(self.task_path(task_id), payload)

    def cancel(self, task_id: str) -> None:
        (self.root / "cancels" / f"{task_id}.cancel").touch()

    def cancelled(self, task_id: str) -> bool:
        return (self.root / "cancels" / f"{task_id}.cancel").is_file()

    def lease(self, agent_id: str) -> tuple[str, dict] | None:
        for path in sorted((self.root / "tasks").glob("*.json")):
            task_id = path.stem
            target = self.root / "leases" / f"{task_id}.{agent_id}.json"
            try:
                path.rename(target)          # 原子抢占；失败说明别人先拿了
            except OSError:
                continue
            try:
                return task_id, json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def heartbeat(self, task_id: str, agent_id: str) -> None:
        try:
            os.utime(self.root / "leases" / f"{task_id}.{agent_id}.json", None)
        except OSError:
            pass

    def complete(self, task_id: str, agent_id: str, result: dict) -> None:
        self._write(self.root / "results" / f"{task_id}.json", result)
        (self.root / "leases" / f"{task_id}.{agent_id}.json").unlink(missing_ok=True)

    def result(self, task_id: str) -> dict | None:
        path = self.root / "results" / f"{task_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def requeue_stale(self, timeout: float = 120.0) -> int:
        now = time.time()
        moved = 0
        for lease in list((self.root / "leases").glob("*.json")):
            try:
                if now - lease.stat().st_mtime <= timeout:
                    continue
            except OSError:
                continue
            task_id = lease.name.split(".", 1)[0]
            try:
                os.replace(lease, self.task_path(task_id))
                moved += 1
            except OSError:
                pass
        return moved
