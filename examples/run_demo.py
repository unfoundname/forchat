"""端到端自检：不联网、不需要 API key，用假 Provider 跑通一次完整运行。

    python examples/run_demo.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forchat.primitives import Replay, Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks

HERE = Path(__file__).resolve().parent


def build_responder() -> TemplateResponder:
    return TemplateResponder(
        {
            "root": [
                tool_chunks([("spawn", {"template": "coder", "task": "实现 hello 函数", "wait": True})]),
                tool_chunks([("report", {
                    "summary": "整体完成：coder 交付 hello，reviewer 已审",
                    "decisions": ["用最小实现，不引入依赖"],
                    "unresolved": [],
                })]),
            ],
            "coder": [
                tool_chunks([("report", {
                    "summary": "hello 函数已实现",
                    "evidence": ["demo.py:1 def hello(): return 'hi'"],
                })]),
            ],
            "reviewer": [
                tool_chunks([("report", {
                    "summary": "审查通过",
                    "evidence": ["复核 coder 的 evidence 有效"],
                })]),
            ],
        }
    )


def main() -> int:
    workflow = WorkflowDef.load(HERE / "workflows" / "minimal" / "workflow.json")
    run_dir = Path(".runs/demo")
    shutil.rmtree(run_dir, ignore_errors=True)

    runtime = Runtime(workflow, FakeProvider(build_responder()), run_dir, model="fake")
    root = runtime.start("把最小系统跑通：一个 coder 写代码，一个 reviewer 审查")

    print("== worker 树 ==")
    print(Replay(runtime.log).tree())
    print("\n== 调度 ==")
    print(runtime.scheduler.stats())
    print("\n== 事件流（人读） ==")
    print(Replay(runtime.log).review())
    print("\n== 报告 ==")
    for worker_id, worker in runtime.workers.items():
        report = worker.report.to_dict() if worker.report else {}
        print(f"{worker_id:16} status={worker.status:6} summary={report.get('summary', '')}")
    print(f"\n事件条数={len(runtime.log.read())}  日志体积={runtime.log.size_mb():.3f}MB")
    print(f"根 worker={root.id} status={root.status}")
    return 0 if len(runtime.workers) == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
