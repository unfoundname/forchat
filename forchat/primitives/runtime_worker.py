"""WorkerMixin：单个 worker 的会话循环（唯一执行循环）、流式解析、收尾与 hook。"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

from . import hooks as hooks_mod
from .hooks import HookContext
from .types import WorkerInstance
from .workflow import WorkerTemplate


class WorkerMixin:

    @staticmethod
    def _image_message(result: str) -> dict | None:
        """把 @@IMAGE:path@@ 工具回执转成多模态 user 消息（OpenAI image_url 格式）。"""
        if os.environ.get("FORCHAT_VISION") != "1" or "@@IMAGE:" not in result:
            return None
        match = re.search(r"@@IMAGE:(.+?)@@", result)
        if not match:
            return None
        path = Path(match.group(1))
        if not path.is_file():
            return None
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"（截图 {path.name}）"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            ],
        }

    def _trim_context(self, worker: WorkerInstance, messages: list[dict]) -> None:
        """上下文管理：超长时保留 system + 任务 + 最近轮次，砍掉中间历史。

        默认 ""不限轮数"" 只在有裁剪时成立：否则几十轮后 messages 必然撑爆上下文。
        """
        limit_messages = int(os.environ.get("FORCHAT_CONTEXT_MESSAGES", "80"))
        limit_chars = int(os.environ.get("FORCHAT_CONTEXT_CHARS", "160000"))
        # 预算感知：整轮预算快用完时自动收紧上下文，别等撞闸才后悔
        tight = False
        if self.max_total_tokens > 0:
            remaining = max(0.0, 1.0 - self.total_tokens / self.max_total_tokens)
            if remaining < 0.2:
                tight = True
                limit_messages = min(limit_messages, 30)
                limit_chars = min(limit_chars, 60000)
        chars = sum(len(str(item.get("content") or "")) for item in messages)
        if len(messages) <= limit_messages and chars <= limit_chars:
            return

        head = messages[:2]
        tail = messages[2:]
        recent = int(os.environ.get("FORCHAT_RECENT_MESSAGES", "20"))
        keep = max(4, min(limit_messages - 3, recent))   # 2 条头部 + 1 条摘要 + 最近 keep 条
        selected = tail[-keep:]
        while selected and selected[0].get("role") == "tool":
            selected = selected[1:]
        dropped = tail[: len(tail) - len(selected)]
        if not dropped:
            return

        # compact = 归档全文 + LLM 摘要（失败/禁用时退回确定性摘要）
        archive = self._archive_compacted(worker, dropped)
        digest = self._digest(dropped)
        summary, source, summary_tokens = self._compact_summary(worker, dropped, digest)
        note = {
            "role": "user",
            "content": (
                f"（较早的 {len(dropped)} 条消息已 compact，摘要如下：\n{summary}\n"
                f"全文：{archive}；需要细节也可以重新 read/grep。）"
            ),
        }
        messages[:] = head + [note] + selected
        worker.snapshot_offset = -1        # 对话被压缩过，下一次快照写 reset
        self.log.append(
            "context_compacted",
            worker_id=worker.id,
            dropped=len(dropped),
            kept=len(messages),
            chars=sum(len(str(item.get("content") or "")) for item in messages),
            archive=archive,
            summary_source=source,
            summary_tokens=summary_tokens,
            tight=tight,
        )

    def _compact_summary(
        self, worker: WorkerInstance, dropped: list[dict], digest: str
    ) -> tuple[str, str, int]:
        """返回 (摘要, 来源, 消耗 tokens)。默认用模型总结，失败退回确定性摘要。"""
        if self.compact_summarizer is not None:
            try:
                text = str(self.compact_summarizer(dropped) or "").strip()
                if text:
                    return text[:2000], "llm", 0
            except Exception:                            # noqa: BLE001
                pass
        if os.environ.get("FORCHAT_COMPACT_LLM", "1") != "1":
            return digest, "digest", 0

        transcript = []
        for item in dropped:
            content = str(item.get("content") or "")[:400]
            if item.get("role") == "assistant":
                calls = [
                    str(call.get("function", {}).get("name", "?"))
                    for call in item.get("tool_calls") or []
                ]
                line = f"assistant: {content}"
                if calls:
                    line += f" [调用工具 {', '.join(calls)}]"
                transcript.append(line)
            else:
                transcript.append(f"{item.get('role')}: {content}")
        prompt = [
            {
                "role": "system",
                "content": (
                    "把下面这段 agent 工作对话压缩成要点：保留已确认的事实、做过的决定、"
                    "未完成事项和踩过的坑；不要编造；300 字以内。"
                ),
            },
            {"role": "user", "content": "\n".join(transcript)[:40000]},
        ]
        content: list[str] = []
        tokens = 0
        try:
            for chunk in self.provider.stream(self.model, prompt, None):
                usage = chunk.get("usage") or {}
                tokens += int(usage.get("prompt_tokens", 0)) + int(
                    usage.get("completion_tokens", 0)
                )
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
        except Exception as exc:                         # noqa: BLE001
            self.log.append("compact_summary_failed", worker_id=worker.id,
                            error=f"{type(exc).__name__}: {exc}")
            return digest, "digest", 0

        text = "".join(content).strip()
        if not text:
            return digest, "digest", tokens
        if tokens:
            worker.budget.charge(tokens=tokens)
            self._charge_total(tokens)
        return text[:2000], "llm", tokens

    def _archive_compacted(self, worker: WorkerInstance, dropped: list[dict]) -> str:
        """把被裁掉的对话整段落盘，之后可以查（--inspect 或直接读文件）。

        保留策略：`FORCHAT_COMPACT_KEEP`（默认 0 = 全留，方便事后优化分析）；
        设成 N 就只留最近 N 份（同一 worker）。
        """
        folder = self.dir / "compacted"
        folder.mkdir(parents=True, exist_ok=True)
        existing: list[int] = []
        for item in folder.glob(f"{worker.id}-*.json"):
            suffix = item.stem.rsplit("-", 1)[-1]
            if suffix.isdigit():
                existing.append(int(suffix))
        index = (max(existing) if existing else 0) + 1
        path = folder / f"{worker.id}-{index:03d}.json"
        payload = {
            "worker_id": worker.id,
            "round": index,
            "messages": dropped,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

        keep = int(os.environ.get("FORCHAT_COMPACT_KEEP", "0"))
        if keep > 0:
            files = sorted(folder.glob(f"{worker.id}-*.json"))
            for stale in files[:-keep]:
                stale.unlink(missing_ok=True)
        return str(path.relative_to(self.dir))

    @staticmethod
    def _digest(messages: list[dict]) -> str:
        """只取"说过什么、调过什么工具"，不保留原文——够唤醒记忆即可。"""
        parts: list[str] = []
        for item in messages:
            role = item.get("role")
            if role == "assistant":
                text = str(item.get("content") or "")[:100]
                calls = [
                    str(call.get("function", {}).get("name", "?"))
                    for call in item.get("tool_calls") or []
                ]
                line = f"assistant: {text}".rstrip()
                if calls:
                    line += f" [工具 {', '.join(calls)}]"
                parts.append(line)
            elif role == "tool":
                parts.append(f"tool结果: {str(item.get('content') or '')[:60]}")
            elif role == "user":
                parts.append(f"user: {str(item.get('content') or '')[:80]}")
        return "\n".join(parts)[:1000]

    def _run_worker(
        self, worker: WorkerInstance, resume: dict | None = None, run_hooks: bool = True
    ) -> None:
        tpl = self.wf.workers[worker.template]
        worker.status = "running"
        started = time.time()
        self.log.append("worker_started", worker_id=worker.id, template=worker.template, depth=worker.depth)
        if run_hooks:
            # 续跑时不重放 on_start：它上次已经跑过，重放会重复派生
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
            worker.snapshot_offset = len(messages)
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
            # 预算是"记账"不是"闸"：超一点点继续跑，只留事件；真正会打断的只有墙钟
            if worker.budget.exhausted() and not worker.budget.soft_logged:
                worker.budget.soft_logged = True
                self.log.append("budget_exceeded", worker_id=worker.id,
                                budget=worker.budget.snapshot())
            if self._budget_tripped and not self._total_budget_logged:
                self._total_budget_logged = True
                self.log.append("run_budget_exceeded", worker_id=worker.id,
                                total_tokens=self.total_tokens, limit=self.max_total_tokens)

            self._trim_context(worker, messages)
            try:
                text, calls, usage = self._stream_round(worker, tpl, messages, rnd)
            except Exception as exc:                     # noqa: BLE001
                # 模型/网络层炸了：只让这一个 worker 失败，不掀翻整次运行
                stop_event = self._stop_events.get(worker.id)
                if stop_event is not None and stop_event.is_set():
                    reason = "stopped"                  # 子进程被 kill（硬中断）
                    self.log.append("worker_hard_stopped", worker_id=worker.id,
                                    detail=f"{type(exc).__name__}: {exc}")
                    break
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
                self._beat(worker.id)                     # 工具执行前也算活着
                try:
                    result = self._execute_tool(worker, tpl, call, messages)
                except Exception as exc:                 # noqa: BLE001
                    self.log.append("tool_error", worker_id=worker.id, tool=call["name"],
                                    error=f"{type(exc).__name__}: {exc}")
                    result = f"错误：{type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                image = self._image_message(result)
                if image is not None:
                    messages.append(image)

            self._save_worker_state(worker, messages, rnd, reported)
            if worker.restart_requested:
                # 同一个 worker 实例自重启：清空对话，事实保留
                worker.restart_requested = False
                messages = self._initial_messages(worker, tpl)
                rnd = 0
                reported = False
                worker.report = None
                last_text = ""
                last_charge = time.time()
                worker.snapshot_offset = -1    # 新会话，快照要 reset
                self.log.append("worker_restarted", worker_id=worker.id, task=worker.task[:120])
                continue
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

        if self.stream_factory is not None:
            # 请求级子进程：cancel/超时时 factory 直接 kill 子进程
            stream = self.stream_factory(messages, self._tool_specs(tpl), stop_event)
        else:
            stream = self.provider.stream(self.model, messages, self._tool_specs(tpl))

        for chunk in stream:
            self._beat(worker.id)
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
            if delta.get("reasoning_content") and os.environ.get("FORCHAT_LOG_THINKING") == "1":
                # 默认不逐条落 thinking：1B token 级运行会把事件流写爆
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
