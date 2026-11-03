"""pytest 配置：把仓库根与 tests 目录放进 sys.path，让 `python tests/xxx.py` 和 pytest 都能跑。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
