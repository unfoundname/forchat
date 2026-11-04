"""ContractMixin：契约写入、读取、变更提案/决策、hash + checks 校验。"""
from __future__ import annotations

import hashlib
import json
import time

from .types import WorkerInstance
from .workflow import WorkerTemplate


class ContractMixin:

    def _apply_contract(self, worker: WorkerInstance, key: str, content: str,
                        checks: list[dict], status: str, change_reason: str,
                        replace: bool, owner: str | None = None) -> str:
        contracts = self.state.setdefault("contracts", {})
        existing = contracts.get(key) or {}
        if existing and not replace:
            return (f"错误：契约 {key} 已存在（v{existing.get('version', 1)}）；"
                    "确实要改请走 propose_contract_change 或 replace=true")
        version = int(existing.get("version", 0)) + 1
        stored = content[:8000]
        contracts[key] = {
            "content": stored,
            "version": version,
            "status": status,
            "owner": owner or worker.id,
            "sha256": hashlib.sha256(stored.encode("utf-8")).hexdigest(),
            "checks": checks,
            "change_reason": change_reason,
            "supersedes": existing.get("version") if existing else None,
        }
        self._save_state()
        self.log.append("contract_written", worker_id=worker.id, key=key,
                        version=version, status=status, replace=bool(existing),
                        checks=len(checks), reason=change_reason[:200])
        return (f"契约 {key} v{version} [{status}] 已写入（checks={len(checks)}；"
                "会注入所有 agent 的提示词，compact 也抹不掉）")

    def _tool_write_contract(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        key = str(args.get("key") or "").strip()
        content = str(args.get("content") or "").strip()
        replace = bool(args.get("replace", False))
        checks = args.get("checks") or []
        status = str(args.get("status") or "draft").strip().lower()
        reason = str(args.get("change_reason") or "").strip()
        if not key or not content:
            return "错误：write_contract 需要 key 与 content"
        if status not in ("draft", "frozen", "deprecated"):
            return "错误：status 只能是 draft / frozen / deprecated"
        if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
            return "错误：checks 必须是对象数组（格式同 run_checks）"
        return self._apply_contract(worker, key, content, checks, status, reason, replace)

    def _tool_read_contracts(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        contracts = self.state.get("contracts") or {}
        if not contracts:
            return "（还没有契约）"
        lines = [
            f"- {key} v{item.get('version', 1)} [{item.get('status', 'draft')}] "
            f"owner={item.get('owner', '?')} checks={len(item.get('checks') or [])} "
            f"sha={str(item.get('sha256', ''))[:8]}: {item.get('content', '')}"
            for key, item in contracts.items()
        ]
        pending = [c for c in (self.state.get("contract_changes") or {}).values()
                   if c.get("state") == "pending"]
        if pending:
            lines.append("待决策变更：")
            for item in pending:
                lines.append(f"- {item.get('id')} key={item.get('key')} "
                             f"by={item.get('proposed_by')} reason={item.get('reason')}")
        return "\n".join(lines)

    def _tool_propose_contract_change(self, worker: WorkerInstance, tpl: WorkerTemplate,
                                      args: dict) -> str:
        key = str(args.get("key") or "").strip()
        reason = str(args.get("reason") or "").strip()
        content = str(args.get("content") or "").strip()
        checks = args.get("checks") or []
        affected = [str(a) for a in args.get("affected") or []]
        status = str(args.get("status") or "frozen").strip().lower()
        if not key or not reason:
            return "错误：propose_contract_change 需要 key 与 reason"
        if not content:
            return "错误：变更请求必须附带完整的新 content（root 批准后直接落盘）"
        if status not in ("draft", "frozen", "deprecated"):
            return "错误：status 只能是 draft / frozen / deprecated"
        if not isinstance(checks, list) or any(not isinstance(item, dict) for item in checks):
            return "错误：checks 必须是对象数组"
        changes = self.state.setdefault("contract_changes", {})
        change_id = f"cc-{len(changes) + 1}"
        changes[change_id] = {
            "id": change_id, "key": key, "reason": reason, "content": content,
            "checks": checks, "affected": affected, "target_status": status,
            "proposed_by": worker.id, "created_at": time.time(), "state": "pending",
        }
        self._save_state()
        self.log.append("contract_change_proposed", worker_id=worker.id,
                        change_id=change_id, key=key, affected=affected,
                        reason=reason[:200])
        return (f"变更请求 {change_id} 已提交（key={key}）；"
                "等 root 用 decide_contract_change 决策，批准前不要按新契约实现")

    def _tool_decide_contract_change(self, worker: WorkerInstance, tpl: WorkerTemplate,
                                     args: dict) -> str:
        if worker.parent_id is not None:
            return "错误：只有 root worker 能决定契约变更"
        change_id = str(args.get("change_id") or "").strip()
        approve = bool(args.get("approve", False))
        note = str(args.get("note") or "").strip()
        changes = self.state.get("contract_changes") or {}
        item = changes.get(change_id)
        if item is None:
            return f"错误：没有变更请求 {change_id}"
        if item.get("state") != "pending":
            return f"错误：变更 {change_id} 已经是 {item.get('state')}"
        if approve:
            result = self._apply_contract(
                worker, item["key"], item["content"], item.get("checks") or [],
                item.get("target_status") or "frozen", item.get("reason", ""),
                replace=True, owner=item.get("proposed_by"),
            )
            if not result.startswith("契约"):
                return result
            item["state"] = "approved"
            item["decided_by"] = worker.id
            item["decision_note"] = note
            self.log.append("contract_change_decided", worker_id=worker.id,
                            change_id=change_id, approved=True, key=item["key"])
            self._save_state()
            return f"变更 {change_id} 已批准；{result}"
        item["state"] = "rejected"
        item["decided_by"] = worker.id
        item["decision_note"] = note
        self.log.append("contract_change_decided", worker_id=worker.id,
                        change_id=change_id, approved=False, key=item["key"])
        self._save_state()
        return f"变更 {change_id} 已拒绝" + (f"：{note}" if note else "")

    def _verify_contracts(self, worker: WorkerInstance, tpl: WorkerTemplate,
                          only: str | None = None) -> str | None:
        """校验契约：内容 hash 未被改写 + 契约里的 checks 全部 PASS。"""
        contracts = self.state.get("contracts") or {}
        targets = {k: v for k, v in contracts.items()
                   if (only is None or k == only) and v.get("status") != "deprecated"}
        if not targets:
            return "错误：没有可校验契约（或指定的 key 不存在/已废弃）"
        runner = self.external.get("run_checks")
        for key, item in targets.items():
            content = str(item.get("content", ""))
            expected = item.get("sha256")
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if not expected or actual != expected:
                self.contract_violation_count += 1
                self.log.append("contract_violation", worker_id=worker.id, key=key,
                                reason="hash")
                return f"错误：契约 {key} 内容 hash 不匹配（被改写），拒绝"
            checks = item.get("checks") or []
            if not checks:
                if getattr(tpl, "require_contracts", False):
                    self.contract_violation_count += 1
                    self.log.append("contract_violation", worker_id=worker.id, key=key,
                                    reason="no_checks")
                    return f"错误：契约 {key} 没有可执行 checks，无法机器校验"
                continue
            if runner is None:
                return "错误：没有可用的 run_checks 执行器，无法校验契约 checks"
            result = str(runner[1](json.dumps({"checks": checks})))
            if not result.startswith("验收总结果：PASS"):
                self.contract_violation_count += 1
                self.log.append("contract_violation", worker_id=worker.id, key=key,
                                reason="checks")
                return f"错误：契约 {key} checks 未通过：{result[:300]}"
            self.log.append("contract_verified", worker_id=worker.id, key=key,
                            version=item.get("version"), checks=len(checks))
        return None

    def _tool_verify_contract(self, worker: WorkerInstance, tpl: WorkerTemplate, args: dict) -> str:
        only = str(args.get("key") or "").strip() or None
        problem = self._verify_contracts(worker, tpl, only)
        return problem or "契约校验：PASS（hash 与 checks 全部通过）"
