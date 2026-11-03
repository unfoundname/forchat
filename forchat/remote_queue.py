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
        for name in ("tasks", "leases", "results", "cancels", "suspects"):
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

    def mark_suspects(self, timeout: float = 1800.0) -> list[str]:
        """心跳超时的租约只标记为 suspect，不自动重派（先让人/父 agent 视察）。"""
        now = time.time()
        marked: list[str] = []
        for lease in list((self.root / "leases").glob("*.json")):
            try:
                if now - lease.stat().st_mtime <= timeout:
                    continue
            except OSError:
                continue
            task_id, agent = lease.name.split(".", 1)
            suspect = self.root / "suspects" / f"{task_id}.json"
            if suspect.is_file():
                continue
            self._write(suspect, {
                "task_id": task_id,
                "agent": agent.removesuffix(".json"),
                "lease": lease.name,
                "since": now,
                "note": "lease heartbeat stale; inspect before requeue/cancel",
            })
            marked.append(task_id)
        return marked

    def suspects(self) -> list[dict]:
        items = []
        for path in sorted((self.root / "suspects").glob("*.json")):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return items

    def requeue(self, task_id: str) -> bool:
        """人工/父 agent 视察后明确要求重派。"""
        leases = list((self.root / "leases").glob(f"{task_id}.*.json"))
        if not leases:
            return False
        try:
            payload = json.loads(leases[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        self._write(self.task_path(task_id), payload)
        leases[0].unlink(missing_ok=True)
        (self.root / "suspects" / f"{task_id}.json").unlink(missing_ok=True)
        return True
