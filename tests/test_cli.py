"""CLI 自检：--check 的进程级行为与退出码。

    python tests/test_cli.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run


def test_check_cli_exit_codes() -> None:
    """--check 不需要 key：好配置退出 0，坏配置退出 2。"""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    good = root / "examples" / "workflows" / "minimal" / "workflow.json"
    result = subprocess.run(
        [sys.executable, "-m", "forchat", "--check", str(good)],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "[check] ok" in result.stdout

    bad = subprocess.run(
        [sys.executable, "-m", "forchat", "--check", str(root / "nope.json")],
        cwd=root, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert bad.returncode == 2, (bad.stdout or "") + (bad.stderr or "")
    assert "[check] 失败" in bad.stdout



def main() -> int:
    checks = [
        ("--check 进程级退出码", test_check_cli_exit_codes),
    ]
    return run("CLI 自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
