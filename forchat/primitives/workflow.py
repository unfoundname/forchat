"""WorkflowDef：可加载的配置包。严格校验，任何拼写错误在加载期就报，不要留到运行期。

它不含任何业务：只有"有哪些 worker 模板、谁能派谁、预算多少、挂哪些 hook"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hooks
from .types import Budget

BUILTIN_TOOLS = {
    "spawn", "report", "read_state", "write_note", "write_artifact",
    "read_worker_report", "wait_workers", "stop_worker",
}
_TOP_KEYS = {"name", "version", "entry", "limits", "workers"}
_LIMIT_KEYS = {
    "max_concurrent", "max_depth", "max_workers", "max_wall_seconds",
    "max_total_tokens", "max_total_seconds", "max_total_cost_usd",
}
_WORKER_KEYS = {
    "role", "role_file", "can_spawn", "max_rounds", "tools", "skills", "budget", "hooks",
    "scope", "retries",
}


class WorkflowError(ValueError):
    """配置包不合法。所有错误一次性报出来。"""


@dataclass
class WorkerTemplate:
    name: str
    role: str
    can_spawn: list[str] = field(default_factory=list)
    max_rounds: int | None = None          # None = 不限轮数，由预算与墙钟兜底
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    hooks: dict[str, list[dict]] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)   # 模板级默认授权范围；空 = 继承父 worker
    retries: int = 0                                 # 仅对"瞬时错误"重跑该 worker，默认 0


@dataclass
class WorkflowDef:
    name: str
    version: int
    entry: str
    limits: dict[str, Any]
    workers: dict[str, WorkerTemplate]
    base_dir: Path

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(
        cls,
        path: Path | str,
        known_tools: set[str] | None = None,
        known_skills: set[str] | None = None,
    ) -> "WorkflowDef":
        path = Path(path)
        if not path.is_file():
            raise WorkflowError(f"workflow 文件不存在：{path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"{path} 不是合法 JSON：{exc}") from exc
        return cls.from_dict(
            raw, base_dir=path.parent, known_tools=known_tools, known_skills=known_skills
        )

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        base_dir: Path | str,
        known_tools: set[str] | None = None,
        known_skills: set[str] | None = None,
    ) -> "WorkflowDef":
        base = Path(base_dir)
        errors: list[str] = []

        if not isinstance(raw, dict):
            raise WorkflowError("workflow 顶层必须是对象")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            errors.append(f"顶层未知字段：{sorted(unknown)}")
        for key in ("name", "workers"):
            if key not in raw:
                errors.append(f"顶层缺少必填字段：{key}")
        if errors:
            raise WorkflowError("；".join(errors))

        limits = raw.get("limits") or {}
        if not isinstance(limits, dict):
            raise WorkflowError("limits 必须是对象")
        bad_limits = set(limits) - _LIMIT_KEYS
        if bad_limits:
            raise WorkflowError(f"limits 未知字段：{sorted(bad_limits)}")

        workers_raw = raw["workers"]
        if not isinstance(workers_raw, dict) or not workers_raw:
            raise WorkflowError("workers 必须是非空对象")

        names = set(workers_raw)
        templates: dict[str, WorkerTemplate] = {}
        allowed_tools = set(BUILTIN_TOOLS) | set(known_tools or ())

        for name, spec in workers_raw.items():
            errors.extend(
                cls._check_worker(name, spec, names, base, templates, allowed_tools, known_skills)
            )
        if errors:
            raise WorkflowError("；".join(errors))

        entry = raw.get("entry", "root")
        if entry not in names:
            raise WorkflowError(f"entry 指向不存在的模板：{entry}")

        return cls(
            name=raw["name"],
            version=int(raw.get("version", 1)),
            entry=entry,
            limits=limits,
            workers=templates,
            base_dir=base,
        )

    @classmethod
    def _check_worker(
        cls,
        name: str,
        spec: dict,
        names: set[str],
        base: Path,
        out: dict[str, WorkerTemplate],
        known_tools: set[str],
        known_skills: set[str] | None = None,
    ) -> list[str]:
        errors: list[str] = []
        if not isinstance(spec, dict):
            return [f"worker {name} 必须是对象"]

        unknown = set(spec) - _WORKER_KEYS
        if unknown:
            errors.append(f"worker {name} 未知字段：{sorted(unknown)}")
        if not spec.get("role") and not spec.get("role_file"):
            errors.append(f"worker {name} 必须提供 role 或 role_file")

        role = spec.get("role", "")
        if spec.get("role_file"):
            role_path = (base / spec["role_file"]).resolve()
            if not role_path.is_file():
                errors.append(f"worker {name} 的 role_file 不存在：{role_path}")
            else:
                role = role_path.read_text(encoding="utf-8")

        can_spawn = spec.get("can_spawn") or []
        if not isinstance(can_spawn, list):
            errors.append(f"worker {name} 的 can_spawn 必须是数组")
            can_spawn = []
        for target in can_spawn:
            if target not in names:
                errors.append(f"worker {name} 想派生不存在的模板：{target}")

        tools = spec.get("tools") or []
        if not isinstance(tools, list):
            errors.append(f"worker {name} 的 tools 必须是数组")
            tools = []
        bad_tools = set(tools) - known_tools
        if bad_tools:
            errors.append(f"worker {name} 使用了未实现的工具：{sorted(bad_tools)}")

        skills = spec.get("skills") or []
        if not isinstance(skills, list):
            errors.append(f"worker {name} 的 skills 必须是数组")
            skills = []
        if known_skills is not None:
            bad_skills = set(skills) - known_skills
            if bad_skills:
                errors.append(f"worker {name} 挂载了不存在的技能：{sorted(bad_skills)}")

        max_rounds = spec.get("max_rounds")
        if max_rounds is not None and (not isinstance(max_rounds, int) or max_rounds < 1):
            errors.append(f"worker {name} 的 max_rounds 必须是正整数（不写 = 不限）")

        retries = spec.get("retries", 0)
        if not isinstance(retries, int) or retries < 0:
            errors.append(f"worker {name} 的 retries 必须是非负整数")

        try:
            budget = Budget.from_dict(spec.get("budget"))
        except ValueError as exc:
            errors.append(f"worker {name}：{exc}")
            budget = Budget()

        hook_map = spec.get("hooks") or {}
        if not isinstance(hook_map, dict):
            errors.append(f"worker {name} 的 hooks 必须是对象")
            hook_map = {}
        for when, entries in hook_map.items():
            if when not in hooks.HOOK_WHEN:
                errors.append(f"worker {name} 未知 hook 时机：{when}")
                continue
            if not isinstance(entries, list):
                errors.append(f"worker {name} 的 {when} 必须是数组")
                continue
            for entry in entries:
                try:
                    hooks.validate_entry(entry, names)
                except ValueError as exc:
                    errors.append(f"worker {name}：{exc}")

        if not errors:
            out[name] = WorkerTemplate(
                name=name,
                role=role,
                can_spawn=list(can_spawn),
                max_rounds=max_rounds,
                tools=list(tools),
                skills=list(spec.get("skills") or []),
                budget=budget,
                hooks={k: list(v) for k, v in hook_map.items()},
                scope=[str(s) for s in (spec.get("scope") or [])],
                retries=int(spec.get("retries") or 0),
            )
        return errors
