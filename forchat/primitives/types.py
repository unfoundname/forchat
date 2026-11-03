"""原语层的公共类型：worker、预算、报告、状态。

原则：这里只有"结构"，没有"流程"。任何"先做什么后做什么"都属于 workflow 配置或运行时。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pending", "running", "done", "failed", "killed", "interrupted"]


@dataclass
class Budget:
    """token / 墙钟 二元预算。用完即判废，由运行时强制执行。

    没有 cost 字段：provider 不返回金额，记一个永远是 0 的成本只会误导。
    等真的接入计费（有价目表）再加。
    """

    tokens: int = 200_000
    wall_seconds: float = 1800.0
    spent_tokens: int = 0
    spent_seconds: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Budget":
        data = data or {}
        known = {"tokens", "wall_seconds"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"budget 里有未知字段：{sorted(unknown)}")
        return cls(**{k: data[k] for k in known if k in data})

    def charge(self, tokens: int = 0, seconds: float = 0.0) -> None:
        self.spent_tokens += max(tokens, 0)
        self.spent_seconds += max(seconds, 0.0)

    def exhausted(self) -> bool:
        return self.spent_tokens >= self.tokens or self.spent_seconds >= self.wall_seconds

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens": [self.spent_tokens, self.tokens],
            "seconds": [round(self.spent_seconds, 3), self.wall_seconds],
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
