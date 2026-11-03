"""机械校验架构约束（AGENTS.md 的硬规则里可自动化的部分）：

1. 依赖只能向下：primitives 只许 import 标准库和自身；providers 同理。
2. primitives 里不许出现领域词（B 站、视频、商品…）。
3. 同一能力不许二次实现：核心循环只允许出现在 primitives/runtime_worker.py。
4. 单文件不许超过 500 行（超过就拆，拆不动说明职责没切开）。

    python tests/test_layering.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "forchat"

LAYERS = {
    "forchat/primitives": {"forchat.primitives"},
    "forchat/providers": {"forchat.providers"},
}

DOMAIN_WORDS = {"bilibili", "B站", "哔哩", "视频", "弹幕", "视频流", "淘宝", "商品", "小红书"}

LOOP_OWNER = "forchat/primitives/runtime_worker.py"
MAX_LINES = 500
# 执行循环的"指纹"：同时出现 tool_calls 累积（setdefault）、finish_reason 处理和流式分片循环
LOOP_MARKERS = {"setdefault", "finish_reason", "for chunk"}


def py_files(rel: str) -> list[Path]:
    return sorted((ROOT / rel).rglob("*.py"))


def imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
    return found


def check_direction() -> list[str]:
    problems: list[str] = []
    for rel, allowed in LAYERS.items():
        for path in py_files(rel):
            for module in imports_of(path):
                if not module.startswith("forchat"):
                    continue                      # 标准库/第三方
                if not any(module == prefix or module.startswith(prefix + ".")
                           for prefix in allowed):
                    problems.append(f"{path.relative_to(ROOT)} 反向 import：{module}")
    return problems


def check_domain_words() -> list[str]:
    problems: list[str] = []
    for rel in LAYERS:
        for path in py_files(rel):
            text = path.read_text(encoding="utf-8")
            for word in DOMAIN_WORDS:
                if word.lower() in text.lower():
                    problems.append(f"{path.relative_to(ROOT)} 出现领域词：{word}")
    return problems


def check_single_loop() -> list[str]:
    """核心执行循环只许有一份：凡是同时处理 tool_calls 与 finish_reason 的文件都算嫌疑。"""
    problems: list[str] = []
    for path in py_files("forchat"):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel == LOOP_OWNER:
            continue
        text = path.read_text(encoding="utf-8")
        if all(marker in text for marker in LOOP_MARKERS):
            problems.append(f"{rel} 疑似自带执行循环，应复用 {LOOP_OWNER}")
    return problems


def check_file_length() -> list[str]:
    """单文件超过 500 行就拆：这是可维护性的硬闸，不是建议。"""
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"__pycache__", ".runs", ".worktrees", ".venv"}:
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MAX_LINES:
            problems.append(
                f"{path.relative_to(ROOT)} 有 {lines} 行，超过单文件上限 {MAX_LINES} 行"
            )
    return problems


def check_spawn_outlet() -> list[str]:
    """进程创建只能走 forchat/exec.py（统一取消/限额/沙箱）。"""
    owner = ROOT / "forchat" / "exec.py"
    calls = (
        "subprocess.Popen(", "subprocess.run(", "subprocess.call(",
        "subprocess.check_output(", "subprocess.check_call(",
        "os.system(", "os.popen(", "os.spawn",
    )
    problems: list[str] = []
    for path in py_files("forchat"):
        if path == owner:
            continue
        text = path.read_text(encoding="utf-8")
        for call in calls:
            if call in text:
                problems.append(
                    f"{path.relative_to(ROOT)} 直接调用 {call.rstrip('(')}，必须走 forchat/exec.py"
                )
    return problems


def main() -> int:
    problems = (
        check_direction()
        + check_domain_words()
        + check_single_loop()
        + check_spawn_outlet()
        + check_file_length()
    )
    if problems:
        print("分层校验失败：")
        for item in problems:
            print("  -", item)
        return 1
    print("分层校验通过：依赖方向、领域词隔离、唯一执行循环、进程唯一出口、单文件 ≤ 500 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
