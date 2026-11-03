"""原语层的公共类型：worker、预算、报告、状态。

原则：这里只有"结构"，没有"流程"。任何"先做什么后做什么"都属于 workflow 配置或运行时。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pending", "queued", "running", "done", "failed", "killed", "interrupted"]


@dataclass
class Budget:
    """token / 墙钟 / 成本 三元预算。用完即判废，由运行时强制执行。

    cost 只有配了价目表（pricing.json / FORCHAT_PRICING）才有意义；
    没配时 cost 字段为 None，报告里明确显示"未知"，不假装是 0。
    """

    tokens: int = 20_000_000
    wall_seconds: float = 14400.0
    max_cost_usd: float | None = None
    spent_tokens: int = 0
    spent_seconds: float = 0.0
    spent_prompt_tokens: int = 0
    spent_completion_tokens: int = 0
    spent_cost_usd: float = 0.0
    soft_logged: bool = False        # 超软预算只记一次事件，不中断

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Budget":
        data = data or {}
        known = {"tokens", "wall_seconds", "max_cost_usd"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"budget 里有未知字段：{sorted(unknown)}")
        return cls(**{k: data[k] for k in known if k in data})

    def charge(
        self,
        tokens: int = 0,
        seconds: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.spent_tokens += max(tokens, 0)
        self.spent_seconds += max(seconds, 0.0)
        self.spent_prompt_tokens += max(prompt_tokens, 0)
        self.spent_completion_tokens += max(completion_tokens, 0)
        self.spent_cost_usd += max(cost_usd, 0.0)

    def exhausted(self) -> bool:
        """软预算是否已超：只用于记账（超了也继续跑），不用于中断。"""
        if self.spent_tokens >= self.tokens:
            return True
        return self.max_cost_usd is not None and self.spent_cost_usd >= self.max_cost_usd

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens": [self.spent_tokens, self.tokens],
            "seconds": [round(self.spent_seconds, 3), self.wall_seconds],
            "cost_usd": [
                round(self.spent_cost_usd, 4),
                self.max_cost_usd,
            ],
            "prompt_tokens": self.spent_prompt_tokens,
            "completion_tokens": self.spent_completion_tokens,
        }


@dataclass
class Report:
    """一个worker的结构化交付物。字段是固定的，内容自由。"""

    worker_id: str
    status: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "decisions": self.decisions,
            "unresolved": self.unresolved,
            "evidence": self.evidence,
            "budget": self.budget,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at,
        }


@dataclass
class WorkerInstance:
    """一个运行中的执行单元。agent 与它 1:1。主 worker 在结构上没有任何特殊地位。"""

    id: str
    template: str
    task: str
    parent_id: str | None = None
    depth: int = 0
    status: Status = "pending"
    budget: Budget = field(default_factory=Budget)
    report: Report | None = None
    artifacts: list[str] = field(default_factory=list)
    # 授权范围（相对项目根的目录）。空 = 整个项目根；非空 = 只允许在这些目录内改动
    scope: list[str] = field(default_factory=list)
    retries_used: int = 0
