"""子进程 worker 入口：`python -m forchat.worker_proc`。

父进程通过 stdin 传一行 JSON（workflow + worker 描述 + run_dir），子进程跑**同一个**
`Runtime._run_worker` 循环（事件写回共享 run 目录，带跨进程文件锁），结束后把结果写到
`workers/<id>.result.json`，父进程据此更新内存状态。

这层解决的是"同进程里的纯 Python 死循环杀不掉"：父进程 stop 时直接 kill 这个进程。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    raw = sys.stdin.readline()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"bad payload: {exc}", file=sys.stderr)
        return 2

    from . import tools as _tools  # noqa: F401  触发能力工具注册（否则 registry 是空的）
    from .policy import Policy
    from .primitives import Runtime, WorkflowDef
    from .primitives.types import WorkerInstance
    from .providers.deepseek import DeepSeekProvider
    from .providers.fake import FakeProvider
    from .tool_bridge import build as build_tools
    from .tool_bridge import build_skill_loader

    tools = build_tools()
    workflow = WorkflowDef.from_dict(
        payload["workflow"],
        base_dir=Path(payload.get("base_dir") or Path.cwd()),
        known_tools=set(tools),
    )
    script_path = os.environ.get("FORCHAT_CHILD_SCRIPT")
    if script_path:
        script = json.loads(Path(script_path).read_text(encoding="utf-8"))
        provider = FakeProvider(
            script, delay=float(os.environ.get("FORCHAT_CHILD_DELAY", "0"))
        )
    else:
        provider = DeepSeekProvider()

    runtime = Runtime(
        workflow,
        provider,
        payload["run_dir"],
        model=payload.get("model", "fake"),
        external_tools=tools,
        skill_loader=build_skill_loader(),
        guard=Policy(Path.cwd()).check,
        facts_dir=payload.get("facts_dir") or None,
        restore=False,
    )
    runtime.hooks_enabled = False

    info = payload["worker"]
    worker = WorkerInstance(
        id=str(info["id"]),
        template=str(info["template"]),
        task=str(info.get("task", "")),
        parent_id=info.get("parent_id"),
        depth=int(info.get("depth", 0)),
        scope=list(info.get("scope") or []),
        facts_owner=str(info.get("facts_owner") or ""),
    )
    runtime.workers[worker.id] = worker

    try:
        runtime._run_worker(worker)
    except BaseException as exc:                     # noqa: BLE001
        worker.status = "failed"
        worker.report = None
        print(f"worker crashed: {type(exc).__name__}: {exc}", file=sys.stderr)

    report = worker.report.to_dict() if worker.report else {}
    result = {
        "status": worker.status,
        "report": report,
        "tokens": worker.budget.spent_tokens,
        "cost_usd": report.get("cost_usd"),
    }
    out = Path(payload["run_dir"]) / "workers" / f"{worker.id}.result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
