"""WorkerMixin：单个 worker 的会话循环（唯一执行循环）、流式解析、收尾与 hook。"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import hooks as hooks_mod
from .hooks import HookContext
from .types import WorkerInstance
from .workflow import WorkerTemplate


class WorkerMixin:

    def _run_worker(self, worker: WorkerInstance, resume: dict | None = None) -> None:
        tpl = self.wf.workers[worker.template]
        worker.status = "running"
        started = time.time()
        self.log.append("worker_started", worker_id=worker.id, template=worker.template, depth=worker.depth)
        self._run_hooks(worker, "on_start")

        messages = self._initial_messages(worker, tpl)
        reason = "stop"
        last_charge = started
        last_text = ""
        reported = False
        rnd = 0
        if resume:
            # 断点续跑：从快照里的 messages 继续；轮次清零（max_rounds 是"单次会话"的闸）
            messages = resume.get("messages") or messages
            rnd = 0
            reported = bool(resume.get("reported", False))
            self.log.append("worker_snapshot_loaded", worker_id=worker.id,
                            resume_round=resume.get("round", 0), messages=len(messages))

        # 默认不限轮数：协调型 worker 跑几十上百轮是正常的，真正的闸是预算与墙钟。
        # 想给单个模板设上限，就在 workflow.json 里写 max_rounds。
        while True:
            if tpl.max_rounds is not None and rnd >= tpl.max_rounds:
                reason = "max_rounds"
                break
            stop_event = self._stop_events.get(worker.id)
            if stop_event is not None and stop_event.is_set():
                reason = "stopped"
                self.log.append("worker_stop_requested", worker_id=worker.id, round=rnd)
                break
            rnd += 1
            # 墙钟要按轮次记账，否则 wall_seconds 只有在 worker 结束后才生效
            now = time.time()
            worker.budget.charge(seconds=now - last_charge)
            last_charge = now

            if self._deadline_exceeded():
                reason = "wall_clock"
                self.log.append("run_expired", worker_id=worker.id,
                                limit=self.max_wall_seconds)
                break
            if worker.budget.exhausted():
                reason = "budget"
                self.log.append("budget_exhausted", worker_id=worker.id, budget=worker.budget.snapshot())
                break
            if self._budget_tripped:
                reason = "total_budget"
                self.log.append("run_budget_exhausted", worker_id=worker.id,
                                total_tokens=self.total_tokens, limit=self.max_total_tokens)
                break

            try:
                text, calls, usage = self._stream_round(worker, tpl, messages, rnd)
            except Exception as exc:                     # noqa: BLE001
                # 模型/网络层炸了：只让这一个 worker 失败，不掀翻整次运行
                reason = "error"
                self.log.append("worker_error", worker_id=worker.id,
                                error=f"{type(exc).__name__}: {exc}")
                last_text = last_text or f"（异常：{type(exc).__name__}: {exc}）"
                break
            tokens = usage["prompt"] + usage["completion"]
            cost = None
            if self.pricing is not None:
                cost = self.pricing.cost(self.model, usage["prompt"], usage["completion"])
            worker.budget.charge(
                tokens=tokens,
                prompt_tokens=usage["prompt"],
                completion_tokens=usage["completion"],
                cost_usd=cost or 0.0,
            )
            self._charge_total(tokens, cost)
            last_text = text

            stop_event = self._stop_events.get(worker.id)
            if stop_event is not None and stop_event.is_set():
                reason = "stopped"
                break

            if not calls:
                self._save_worker_state(worker, messages, rnd, reported)
                reason = "stop"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in calls
                    ],
                }
            )
            for call in calls:
                try:
                    result = self._execute_tool(worker, tpl, call, messages)
                except Exception as exc:                 # noqa: BLE001
                    self.log.append("tool_error", worker_id=worker.id, tool=call["name"],
                                    error=f"{type(exc).__name__}: {exc}")
                    result = f"错误：{type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

            self._save_worker_state(worker, messages, rnd, reported)
            if worker.report is not None:
                # report 是显式的完成信号：本轮结束，不再请求下一轮
                reported = True
                reason = "stop"
                break

        worker.budget.charge(seconds=time.time() - last_charge)
        self._finish(worker, reason, reported=reported, last_text=last_text)
    def _finish(self, worker: WorkerInstance, reason: str, reported: bool, last_text: str = "") -> None:
        tpl = self.wf.workers[worker.template]
        if reason == "error" and worker.retries_used < tpl.retries:
            worker.retries_used += 1
            worker.report = None
            self.log.append("worker_retry", worker_id=worker.id,
                            attempt=worker.retries_used, retries=tpl.retries)
            self._run_worker(worker)
            return

        if worker.report is None:
            worker.report = self._fallback_report(worker, last_text)
        if reason == "stopped":
            status = "killed"
        elif reported:
            status = "done"
        else:
            status = "failed"
        # 只有真的调用过 report 才算完成；跑满轮次/超预算/只说话不交报告都不算
        worker.report.status = "done" if status == "done" else "failed"
        worker.report.budget = worker.budget.snapshot()
        if self.pricing is not None:
            worker.report.cost_usd = self.pricing.cost(
                self.model,
                worker.budget.spent_prompt_tokens,
                worker.budget.spent_completion_tokens,
            )
        worker.status = status
        self.log.append(
            "worker_done",
            worker_id=worker.id,
            status=worker.status,
            reason=reason,
            budget=worker.budget.snapshot(),
        )
        self.log.append("report", worker_id=worker.id, report=worker.report.to_dict())
        self._run_hooks(worker, "on_done" if worker.status == "done" else "on_fail")
    def _stream_round(
        self, worker: WorkerInstance, tpl: WorkerTemplate, messages: list[dict], rnd: int
    ) -> tuple[str, list[dict], dict[str, int]]:
        self.log.append("round_start", worker_id=worker.id, round=rnd)
        content: list[str] = []
        pending: dict[int, dict] = {}
        prompt_tokens = 0
        completion_tokens = 0

        stop_event = self._stop_events.setdefault(worker.id, threading.Event())

        for chunk in self.provider.stream(self.model, messages, self._tool_specs(tpl)):
            if stop_event.is_set():
                # 协作式取消：只能做到"下一个分片到达时停"，在途网络请求没法硬杀
                self.log.append("worker_stop_requested", worker_id=worker.id, round=rnd)
                break
            usage = chunk.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens", 0))
            completion_tokens += int(usage.get("completion_tokens", 0))
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                self.log.append("thinking_delta", worker_id=worker.id,
                                text=delta["reasoning_content"])

            for tc in delta.get("tool_calls") or []:
                slot = pending.setdefault(int(tc.get("index", 0)),
                                          {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

            if choice.get("finish_reason"):
                self.log.append("round_finish", worker_id=worker.id, round=rnd,
                                finish_reason=choice["finish_reason"])
                if choice["finish_reason"] == "length":
                    # 被 max_tokens 截断：工具参数可能缺尾巴，调用方应重试而不是硬执行
                    self.log.append("response_truncated", worker_id=worker.id, round=rnd)

        text = "".join(content)
        if text:
            self.log.append("assistant_message", worker_id=worker.id, round=rnd, text=text)
        calls = [pending[i] for i in sorted(pending)]
        if completion_tokens == 0:
            completion_tokens = max(1, (len(text) + sum(len(c["arguments"]) for c in calls)) // 4)
        return text, calls, {"prompt": prompt_tokens, "completion": completion_tokens}
    def _run_hooks(self, worker: WorkerInstance, when: str) -> None:
        tpl = self.wf.workers[worker.template]
        for entry in tpl.hooks.get(when, []):
            if hooks_mod.is_spawn(entry):
                template = entry["spawn"]
                task = str(entry.get("task", "review {worker_id}")).format(
                    worker_id=worker.id, template=tpl.name
                )
                child = self._create_worker(template, task, parent=worker, depth=worker.depth)
                if child is None:
                    continue
                self.log.append("hook_spawn", worker_id=worker.id, hook=when,
                                child=child.id, template=template)
                if entry.get("wait", False):
                    self._run_worker(child)
                else:
                    self._submit(child)
                continue

            action = hooks_mod.ACTION_HOOKS[entry["action"]]
            ctx = HookContext(runtime=self, worker_id=worker.id, state=self.state)
            if entry.get("sync", True):
                result = action(ctx)
                self.log.append("hook_action", worker_id=worker.id, hook=when,
                                action=entry["action"], result=result)
            else:
                self.scheduler.submit(self._hook_async, worker.id, when, action, ctx)
    def _hook_async(self, worker_id: str, when: str, action, ctx: HookContext) -> None:
        result = action(ctx)
        self.log.append("hook_action", worker_id=worker_id, hook=when, action=action.__name__,
                        result=result, async_=True)
