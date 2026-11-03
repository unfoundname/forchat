"""技能（skill）：一段按需加载的说明文本，不是可执行代码。

工具 vs 技能
------------
- 工具：模型调用它来「做一件事」，参数结构化、有副作用，返回值是结果。
- 技能：模型调用它来「学会怎么做一件事」，返回值是 instructions，
  进了上下文之后，后续行为按它来。没有副作用。

相同点：都靠 name + description 出现在提示里，由模型自己决定何时取用。
不同点：工具扩展「能力」，技能扩展「行为习惯 / 领域知识」。

这就是「渐进式披露」：技能正文不常驻上下文，只常驻一句话介绍，
用到时才整段注入 —— 相当于给模型一个可以自己查的手册。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path


def skills_dir() -> Path:
    """默认读项目根下的 skills/，可用 FORCHAT_SKILLS 覆盖。"""
    return Path(os.environ.get("FORCHAT_SKILLS", Path.cwd() / "skills"))


def _parse(text: str, fallback_name: str) -> tuple[str, str, str]:
    """解析极简 frontmatter（只有 key: value 形式的行）。"""
    matched = _FRONTMATTER.match(text)
    if not matched:
        return fallback_name, "", text.strip()

    meta: dict[str, str] = {}
    for line in matched.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]          # 兼容 YAML 的 quoted string
            meta[key.strip()] = value
    return meta.get("name", fallback_name), meta.get("description", ""), matched.group(2).strip()


def load_skills() -> dict[str, Skill]:
    """扫描 skills/<name>/SKILL.md，每次调用都重新读盘（改完立刻生效）。"""
    found: dict[str, Skill] = {}
    root = skills_dir()
    if not root.is_dir():
        return found

    for child in sorted(root.iterdir()):
        manifest = child / "SKILL.md"
        if not (child.is_dir() and manifest.is_file()):
            continue
        name, description, body = _parse(
            manifest.read_text(encoding="utf-8", errors="replace"), child.name
        )
        found[name] = Skill(name=name, description=description, body=body, path=manifest)
    return found


def catalog() -> str:
    """给系统提示用的一行式目录。"""
    return "\n".join(f"- {name}: {s.description}" for name, s in load_skills().items())
