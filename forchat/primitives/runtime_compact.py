"""CompactMixin：上下文 compact（归档 + 模型摘要 / 确定性兜底）。"""
from __future__ import annotations

import json
import os

from .types import WorkerInstance


class CompactMixin:

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

