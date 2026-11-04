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


def _runtime_with_workers():
    from forchat.primitives.types import WorkerInstance

    workflow = _workflow()
    runtime = Runtime(
        workflow, FakeProvider([]), tmpdir(),
        external_tools={"run_checks": (SPEC, lambda _args: "验收总结果：PASS")},
    )
    root = WorkerInstance(id="root-1", template="root", task="t", status="running")
    coder = WorkerInstance(id="n002-coder", template="root", task="t",
                           parent_id="root-1", status="running")
    runtime.workers = {root.id: root, coder.id: coder}
    return runtime, root, coder, workflow


def test_contract_change_protocol() -> None:
    runtime, root, coder, workflow = _runtime_with_workers()
    tpl = workflow.workers["root"]
    runtime._tool_write_contract(root, tpl, {
        "key": "api", "content": "v1", "status": "draft",
        "checks": [{"name": "c", "script": "c.py"}],
    })
    assert runtime.state["contracts"]["api"]["status"] == "draft"

    out = runtime._tool_propose_contract_change(coder, tpl, {
        "key": "api", "reason": "缺少分页字段", "content": "v2 with page",
        "affected": ["feat/list"], "status": "frozen",
        "checks": [{"name": "c", "script": "c.py"}],
    })
    assert "已提交" in out, out
    change_id = next(iter(runtime.state["contract_changes"]))

    denied = runtime._tool_decide_contract_change(coder, tpl,
                                                  {"change_id": change_id, "approve": True})
    assert "只有 root" in denied, denied

    approved = runtime._tool_decide_contract_change(root, tpl, {
        "change_id": change_id, "approve": True, "note": "接受",
    })
    assert "已批准" in approved, approved
    current = runtime.state["contracts"]["api"]
    assert current["version"] == 2 and current["status"] == "frozen"
    assert runtime.log.query(type="contract_change_decided")[0]["approved"] is True


def test_report_contract_version_mismatch() -> None:
    runtime, root, _coder, workflow = _runtime_with_workers()
    tpl = workflow.workers["root"]
    tpl.require_contracts = False
    runtime._tool_write_contract(root, tpl, {
        "key": "api", "content": "v1", "status": "frozen",
    })
    ok = runtime._tool_report(root, tpl, {
        "summary": "v1 完成", "contracts_used": {"api": 1},
    })
    assert ok == "report 已记录", ok

    runtime._tool_write_contract(root, tpl, {
        "key": "api", "content": "v2", "status": "frozen", "replace": True,
    })
    rejected = runtime._tool_report(root, tpl, {
        "summary": "旧版本", "contracts_used": {"api": 1},
    })
    assert "已升到 v2" in rejected, rejected
    assert runtime.log.query(type="contract_version_mismatch")


def main() -> int:
    checks = [
        ("契约 hash + checks 通过", test_contract_hash_and_checks_pass),
        ("checks 失败拒绝 report", test_contract_failed_checks_reject_report),
        ("契约篡改被检测", test_contract_tamper_detected),
        ("契约变更协议", test_contract_change_protocol),
        ("report 契约版本门禁", test_report_contract_version_mismatch),
    ]
    return run("契约机器校验自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
