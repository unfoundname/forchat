"""load_skill 工具：把一个技能的正文取回上下文。

这是「技能」和「工具」在实现上唯一的交叉点 —— 技能本身不执行任何东西，
只是靠一个普通工具把 instructions 送进上下文。
"""
from __future__ import annotations

from ..registry import tool
from ..skills import load_skills


@tool(
    description="加载某个技能的完整使用说明。系统提示里只列了技能名和一句话介绍，"
                "当任务与某个技能匹配时，先调用它取回步骤，再按步骤执行。",
    params={"name": "技能名，例如 code-review"},
)
def load_skill(name: str) -> str:
    available = load_skills()
    skill = available.get(name)
    if skill is None:
        return f"错误：没有名为 {name} 的技能。可用：{', '.join(available) or '（无）'}"
    return f"# 技能：{skill.name}\n\n{skill.body}"
