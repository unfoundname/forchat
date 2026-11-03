"""PromptMixin：构造每个 worker 的初始消息与兜底报告。"""
from __future__ import annotations

from typing import Any

from .types import Report, WorkerInstance
from .workflow import WorkerTemplate


class PromptMixin:

    def _initial_messages(self, worker: WorkerInstance, tpl: WorkerTemplate) -> list[dict]:
        tool_names = ", ".join(tpl.tools) or "（无）"
        system = (
            f"{tpl.role}\n\n"
            f"--- 运行时约定 ---\n"
            f"你的 worker id 是 {worker.id}，模板 {tpl.name}，深度 {worker.depth}。\n"
            f"可用工具：{tool_names}。\n"
            f"需要子 worker 时用 spawn；完成时用 report 提交交付物（这是唯一的完成信号）。\n"
            f"不确定上下文时用 read_state 读当前 plan 和笔记，不要靠记忆。\n"
        )

        # 技能：由装配层注入 loader，原语层不认识技能的存储格式
        mounted: list[str] = []
        if self.skill_loader is not None:
            for name in tpl.skills:
                body = self.skill_loader(name)
                if body:
                    mounted.append(f"## 技能：{name}\n{body}")
        if mounted:
            system += "\n--- 已挂载技能 ---\n" + "\n\n".join(mounted) + "\n"

        # 续跑：把历史 worker 的结论直接摆在新的根 worker 面前，别让它以为从零开始
        if worker.parent_id is None and self._restored:
            lines = []
            for wid in self._restored[-12:]:
                prev = self.workers[wid]
                summary = (prev.report.summary if prev.report else "")[:200]
                lines.append(f"- {wid} [{prev.template}] {prev.status}: {summary or '（无报告）'}")
            system += (
                "\n--- 历史运行（同一 run 目录的上一轮）---\n"
                + "\n".join(lines)
                + "\n需要全文时用 read_worker_report(worker_id)；plan 与笔记用 read_state 读。\n"
            )

            if self._interrupted:
                detail = []
                for wid in self._interrupted[-10:]:
                    prev = self.workers[wid]
                    detail.append(f"- {wid} [{prev.template}] 任务：{prev.task[:120]}")
                system += (
                    "\n--- 上次运行中断（进程崩溃/被杀时还在跑）---\n"
                    + "\n".join(detail)
                    + "\n这些任务没有完成也没有报告。请判断是否需要用 spawn 重新派发，"
                      "或者由你接手；不要假设它们已经做完。\n"
                )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": worker.task},
        ]
    def _fallback_report(self, worker: WorkerInstance, text: str) -> Report:
        return Report(
            worker_id=worker.id,
            status="done",
            summary=(text or "").strip()[:2000] or "（无输出，未调用 report）",
        )
