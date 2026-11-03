"""共享目录任务队列：coordinator 提交，远程 agent 租约执行。

目录结构（放在 run 目录下即可，要求 coordinator 与 agent 能访问同一文件系统）：

    remote/tasks/<task>.json        待租约
    remote/leases/<task>.<agent>.json  已租约（含 token/attempt，mtime = 心跳）
    remote/results/<task>.json      执行结果（先写者赢）
    remote/suspects/<task>.json     心跳超时（只标记，不自动重派）
    remote/dead/<task>.json         超过重试上限的毒任务
    remote/cancels/<task>.cancel    取消标记

正确性约定：

- 租约用同目录 `rename` 原子抢占，并带一次性 token；
- `complete()` 校验 token，租约过期后旧 agent 写结果会被拒绝（fencing）；
- 结果文件先写者赢，重复回传是幂等的；
- `requeue()` 有尝试次数上限，超过就进 dead，不会无限重跑。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class RemoteQueue:
    def __init__(self, root: Path | str, max_attempts: int = 3) -> None:
        self.root = Path(root)
        self.max_attempts = max(1, int(max_attempts))
        for name in ("tasks", "leases", "results", "cancels", "suspects", "dead"):
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

    def lease(self, agent_id: str) -> tuple[str, dict, str] | None:
        """返回 (task_id, payload, token)；token 在 complete 时用于 fencing。"""
        for path in sorted((self.root / "tasks").glob("*.json")):
            task_id = path.stem
            target = self.root / "leases" / f"{task_id}.{agent_id}.json"
            try:
                path.rename(target)          # 原子抢占；失败说明别人先拿了
            except OSError:
                continue
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                try:
                    os.replace(target, self.task_path(task_id))
                except OSError:
                    pass
                return None
            attempt = int(payload.pop("__forchat_attempt", 0)) + 1
            token = uuid.uuid4().hex
            self._write(target, {
                "task_id": task_id, "agent": agent_id, "token": token,
                "attempt": attempt, "payload": payload,
            })
            return task_id, payload, token
        return None

    def heartbeat(self, task_id: str, agent_id: str) -> None:
        try:
            os.utime(self.root / "leases" / f"{task_id}.{agent_id}.json", None)
        except OSError:
            pass

    def complete(self, task_id: str, agent_id: str, token: str, result: dict) -> bool:
        """只有当前租约持有者能写结果；重复回传先写者赢。"""
        lease_path = self.root / "leases" / f"{task_id}.{agent_id}.json"
        try:
            wrapper = json.loads(lease_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if wrapper.get("agent") != agent_id or wrapper.get("token") != token:
            return False
        result_path = self.root / "results" / f"{task_id}.json"
        if not result_path.is_file():
            self._write(result_path, result)
        lease_path.unlink(missing_ok=True)
        return True

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
            try:
                wrapper = json.loads(lease.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                wrapper = {}
            task_id = wrapper.get("task_id") or lease.name.split(".", 1)[0]
            suspect = self.root / "suspects" / f"{task_id}.json"
            if suspect.is_file():
                continue
            self._write(suspect, {
                "task_id": task_id,
                "agent": wrapper.get("agent"),
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

    def dead_tasks(self) -> list[dict]:
        items = []
        for path in sorted((self.root / "dead").glob("*.json")):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return items

    def requeue(self, task_id: str, max_attempts: int | None = None) -> bool:
        """显式重派；超过尝试上限进 dead，绝不无限重跑。"""
        leases = list((self.root / "leases").glob(f"{task_id}.*.json"))
        if not leases:
            return False
        try:
            wrapper = json.loads(leases[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        limit = self.max_attempts if max_attempts is None else max(1, int(max_attempts))
        attempt = int(wrapper.get("attempt", 1))
        if attempt >= limit:
            self._write(self.root / "dead" / f"{task_id}.json", {
                "task_id": task_id, "attempt": attempt,
                "reason": "max_attempts", "since": time.time(),
            })
            leases[0].unlink(missing_ok=True)
            (self.root / "suspects" / f"{task_id}.json").unlink(missing_ok=True)
            return False
        payload = wrapper.get("payload") or {}
        payload["__forchat_attempt"] = attempt
        self._write(self.task_path(task_id), payload)
        leases[0].unlink(missing_ok=True)
        (self.root / "suspects" / f"{task_id}.json").unlink(missing_ok=True)
        return True
