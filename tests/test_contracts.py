"""契约机器校验自检：hash 冻结、checks 执行、report 门禁、篡改检测。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks

SPEC = {"type": "function", "function": {"name": "run_checks"}}


def _workflow() -> WorkflowDef:
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = [
        "write_contract", "verify_contract", "report",
    ]
    raw["workers"]["root"]["can_spawn"] = []
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["root"]["require_contracts"] = True
    return WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)


def _contract_call(content: str, checks: bool = True) -> dict:
    args = {
        "key": "GET /api/videos",
        "content": content,
        "checks": [{"name": "shape", "script": "shape.py"}],
    }
    return {"key": args["key"], "content": content, "checks": args["checks"]}


def test_contract_hash_and_checks_pass() -> None:
    workflow = _workflow()
    content = '{"items": "Video[]"}'
    passed = "验收总结果：PASS\n- shape: PASS"
    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("write_contract", _contract_call(content))]),
                    tool_chunks([("verify_contract", {})]),
                    tool_chunks([("report", {"summary": "完成"})]),
                ]
            }
        )
    )
    runtime = Runtime(
        workflow, provider, tmpdir(),
        external_tools={"run_checks": (SPEC, lambda _args: passed)},
    )
    root = runtime.start("任务")

    stored = runtime.state["contracts"]["GET /api/videos"]
    assert stored["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert stored["checks"]
    assert runtime.log.query(type="contract_verified")
    assert root.status == "done"
    assert root.report.summary == "完成"


def test_contract_failed_checks_reject_report() -> None:
    workflow = _workflow()
    failed = "验收总结果：FAIL\n- shape: FAIL"
    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("write_contract", _contract_call("x"))]),
                    tool_chunks([("report", {"summary": "偷跑"})]),
                    tool_chunks([("report", {"summary": "再偷跑"})]),
                ]
            }
        )
    )
    runtime = Runtime(
        workflow, provider, tmpdir(),
        external_tools={"run_checks": (SPEC, lambda _args: failed)},
    )
    root = runtime.start("任务")

    rejected = runtime.log.query(type="report_rejected")
    assert rejected and rejected[0]["reason"] == "contract", rejected
    assert runtime.log.query(type="contract_violation")[0]["reason"] == "checks"
    assert root.status == "failed"
    assert runtime.contract_violation_count >= 1


def test_contract_tamper_detected() -> None:
    workflow = _workflow()
    provider = FakeProvider(
        TemplateResponder(
            {
                "root": [
                    tool_chunks([("report", {"summary": "直接交"})]),
                    tool_chunks([("report", {"summary": "再交"})]),
                ]
            }
        )
    )
    runtime = Runtime(
        workflow, provider, tmpdir(),
        external_tools={"run_checks": (SPEC, lambda _args: "验收总结果：PASS")},
    )
    runtime.state["contracts"] = {
        "api": {
            "content": "被改过的契约",
            "version": 1,
            "sha256": hashlib.sha256("原始契约".encode("utf-8")).hexdigest(),
            "checks": [{"name": "shape", "script": "shape.py"}],
        }
    }
    root = runtime.start("任务")

    violations = runtime.log.query(type="contract_violation")
    assert violations and violations[0]["reason"] == "hash", violations
    assert root.status == "failed"


def main() -> int:
    checks = [
        ("契约 hash + checks 通过", test_contract_hash_and_checks_pass),
        ("checks 失败拒绝 report", test_contract_failed_checks_reject_report),
        ("契约篡改被检测", test_contract_tamper_detected),
    ]
    return run("契约机器校验自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
