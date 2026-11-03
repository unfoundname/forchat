"""run_checks：跑一组确定性验收脚本，输出 PASS/FAIL。

把"做得好不好"变成机器可判的退出码，而不是信模型在 report 里自述。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .. import subproc
from ..env_scrub import clean_environ
from ..registry import tool

TIMEOUT = 120
MAX_OUTPUT = 800


@tool(
    description="执行一组验收脚本（每个必须是项目内的 .py），按退出码返回 PASS/FAIL 汇总。"
                "用来把验收标准变成机器可判的检查，而不是靠 report 自述。",
    params={
        "checks": "数组：[{name, script, args?, cwd?}]，script 必须是项目内的 .py",
        "timeout_seconds": "每项超时秒数，默认 120",
    },
)
def run_checks(checks: list[dict], timeout_seconds: float = TIMEOUT) -> str:
    if not isinstance(checks, list) or not checks:
        return "错误：checks 必须是非空数组"
    root = Path.cwd().resolve()
    results: list[str] = []

    for item in checks:
        item = item or {}
        name = str(item.get("name") or item.get("script") or "?")
        script = str(item.get("script") or "")
        target = (root / script).resolve()
        if not str(target).startswith(str(root)):
            results.append(f"- {name}: FAIL（脚本越界）")
            continue
        if target.suffix != ".py" or not target.is_file():
            results.append(f"- {name}: FAIL（脚本不存在或不是 .py）")
            continue
        work = (root / str(item.get("cwd"))).resolve() if item.get("cwd") else root
        command = [sys.executable, str(target), *[str(a) for a in item.get("args") or []]]
        try:
            proc = subproc.run(
                command,
                cwd=work,
                timeout=float(timeout_seconds),
                env=clean_environ(),
            )
        except subprocess.TimeoutExpired:
            results.append(f"- {name}: FAIL（超时 >{timeout_seconds}s）")
            continue
        except subproc.SandboxUnavailable as exc:
            results.append(f"- {name}: FAIL（沙箱不可用：{exc}）")
            continue
        output = (proc.stdout or "")[-MAX_OUTPUT:]
        status = "PASS" if proc.returncode == 0 else f"FAIL(exit={proc.returncode})"
        results.append(f"- {name}: {status}\n{output}".rstrip())

    overall = "PASS" if results and all(": PASS" in row for row in results) else "FAIL"
    return f"验收总结果：{overall}\n" + "\n".join(results)
