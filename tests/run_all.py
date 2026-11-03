"""一条命令跑完所有本地检查（不需要网络/API key）。

    python tests/run_all.py

顺序：原语自检 → 分层校验 → 假 provider 端到端 → MCP 自检。
任何一步失败立即返回非零退出码。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = [
    ("原语自检", [sys.executable, "tests/test_primitives.py"]),
    ("原语流程自检", [sys.executable, "tests/test_primitives_flows.py"]),
    ("原语资源/上下文自检", [sys.executable, "tests/test_primitives_resources.py"]),
    ("原语生命周期自检", [sys.executable, "tests/test_primitives_lifecycle.py"]),
    ("原语状态/预算自检", [sys.executable, "tests/test_primitives_state.py"]),
    ("原语围栏自检", [sys.executable, "tests/test_primitives_guards.py"]),
    ("工具自检", [sys.executable, "tests/test_tools.py"]),
    ("策略与安全自检", [sys.executable, "tests/test_policy.py"]),
    ("CLI 自检", [sys.executable, "tests/test_cli.py"]),
    ("HTTP/SSE 自检", [sys.executable, "tests/test_server.py"]),
    ("受限子进程自检", [sys.executable, "tests/test_subproc.py"]),
    ("分层校验", [sys.executable, "tests/test_layering.py"]),
    ("端到端 demo", [sys.executable, "examples/run_demo.py"]),
    ("MCP 自检", [sys.executable, "examples/smoke_mcp.py"]),
]


def main() -> int:
    for label, cmd in STEPS:
        print(f"\n=== {label} ===", flush=True)
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            print(f"[FAIL] {label} 退出码 {proc.returncode}")
            return proc.returncode
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
