"""测试共享夹具：路径、临时目录、用例登记与执行。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MINIMAL = ROOT / "examples" / "workflows" / "minimal" / "workflow.json"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="forchat-test-"))


def run(title: str, checks: list[tuple[str, object]]) -> int:
    print(f"{title}：")
    passed = 0
    failed = 0
    for label, fn in checks:
        try:
            fn()
            passed += 1
            print(f"  ok  {label}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(checks)} 通过")
    return 1 if failed else 0
