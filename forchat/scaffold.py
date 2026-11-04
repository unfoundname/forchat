"""forchat init：生成一个最小可跑的 workflow 包。"""
from __future__ import annotations

import json
from pathlib import Path

WORKFLOW = {
    "name": "hello",
    "version": 1,
    "entry": "root",
    "limits": {"max_concurrent": 1, "max_depth": 1, "max_workers": 2, "max_wall_seconds": 300},
    "workers": {
        "root": {
            "role_file": "prompts/root.md",
            "can_spawn": [],
            "tools": ["report"],
            "budget": {"tokens": 200000, "wall_seconds": 240},
        }
    },
}

ROOT_PROMPT = """你是最小 workflow 的 root worker。

目标：用最短路径完成用户任务，结束时必须调用 `report`，在 summary 里写清你做了什么、结果是什么。
不要调用不存在的工具；当前只授权了 `report`。
"""

README = """# hello（forchat init 生成）

```powershell
python -m forchat --workflow workflow.json "你的目标"
```

先看 `--check`：

```powershell
python -m forchat --check workflow.json
```

需要工具时，在 `workflow.json` 的 `workers.root.tools` 里加（例如 `read`、`write_file`、`run_python`），
并在 `prompts/root.md` 里告诉模型什么时候用。
"""


def init_project(target: Path | str) -> list[Path]:
    folder = Path(target)
    workflow = folder / "workflow.json"
    if workflow.exists():
        raise FileExistsError(f"{workflow} 已存在")
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    workflow.write_text(json.dumps(WORKFLOW, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = folder / "prompts" / "root.md"
    prompt.write_text(ROOT_PROMPT, encoding="utf-8")
    (folder / "README.md").write_text(README, encoding="utf-8")
    return [workflow, prompt]
