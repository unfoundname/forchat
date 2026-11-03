"""分布式调度自检：共享目录队列 + 远程 agent + Runtime 注入。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import MINIMAL, run, tmpdir

from forchat.primitives import Runtime, WorkflowDef
from forchat.providers import FakeProvider, TemplateResponder, tool_chunks
from forchat.remote_executor import RemoteExecutor
from forchat.remote_queue import RemoteQueue

ROOT = Path(__file__).resolve().parents[1]


def _child_script(summary: str = "远程完成") -> Path:
    path = tmpdir() / "child_script.json"
    path.write_text(
        json.dumps([tool_chunks([("report", {"summary": summary})])], ensure_ascii=False),
        encoding="utf-8",
    )
    os.environ["FORCHAT_CHILD_SCRIPT"] = str(path)
    return path


def _agent_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_queue_lease_and_agent() -> None:
    """队列租约 + agent 执行 + 结果回传。"""
    run_dir = tmpdir()
    queue_root = run_dir / "remote"
    _child_script("队列任务完成")
    workflow = WorkflowDef.load(MINIMAL)
    payload = {
        "workflow": workflow.to_dict(),
        "base_dir": str(MINIMAL.parent),
        "run_dir": str(run_dir),
        "model": "fake",
        "facts_dir": "",
        "worker": {
            "id": "w-remote", "template": "root", "task": "远程任务",
            "parent_id": None, "depth": 0, "scope": [], "facts_owner": "",
        },
    }
    queue = RemoteQueue(queue_root)
    queue.submit("w-remote", payload)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "forchat.remote_agent",
             "--queue", str(queue_root), "--once"],
            cwd=ROOT, env=_agent_env(), timeout=60, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    finally:
        os.environ.pop("FORCHAT_CHILD_SCRIPT", None)

    result = queue.result("w-remote")
    assert result is not None, proc.stderr
    assert result["status"] == "done", result
    assert result["report"]["summary"] == "队列任务完成"


def test_runtime_remote_leaf_worker() -> None:
    """Runtime 把叶子 worker 投给远程 agent，父进程拿回结果。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["report"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    queue_root = run_dir / "remote"
    _child_script("远程 coder 完成")
    agent = subprocess.Popen(
        [sys.executable, "-m", "forchat.remote_agent",
         "--queue", str(queue_root), "--poll", "0.1"],
        cwd=ROOT, env=_agent_env(),
    )
    try:
        parent = FakeProvider(
            TemplateResponder(
                {
                    "root": [
                        tool_chunks([("spawn", {"template": "coder", "task": "远程子任务"})]),
                        tool_chunks([("report", {"summary": "根完成"})]),
                    ]
                }
            )
        )
        runtime = Runtime(
            workflow, parent, run_dir,
            remote_executor=RemoteExecutor(queue_root),
        )
        root = runtime.start("任务")
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        os.environ.pop("FORCHAT_CHILD_SCRIPT", None)

    coder = runtime.workers["n002-coder"]
    assert coder.status == "done", (coder.status, coder.report)
    assert coder.report.summary == "远程 coder 完成"
    assert root.status == "done"


def test_remote_cancel_marks_killed() -> None:
    """远程 worker 被 stop：coordinator 写 cancel，agent kill 子进程，父进程记 killed。"""
    raw = json.loads(MINIMAL.read_text(encoding="utf-8"))
    raw["workers"]["root"]["tools"] = ["spawn", "stop_worker", "wait_workers", "report"]
    raw["workers"]["root"]["can_spawn"] = ["coder"]
    raw["workers"]["root"]["hooks"] = {}
    raw["workers"]["coder"]["tools"] = ["report"]
    raw["workers"]["coder"]["hooks"] = {}
    workflow = WorkflowDef.from_dict(raw, base_dir=MINIMAL.parent)

    run_dir = tmpdir()
    queue_root = run_dir / "remote"
    script = tmpdir() / "long.json"
    long_stream = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(300)]
    long_stream.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    script.write_text(json.dumps([long_stream]), encoding="utf-8")
    os.environ["FORCHAT_CHILD_SCRIPT"] = str(script)
    os.environ["FORCHAT_CHILD_DELAY"] = "0.05"
    agent = subprocess.Popen(
        [sys.executable, "-m", "forchat.remote_agent",
         "--queue", str(queue_root), "--poll", "0.1"],
        cwd=ROOT, env=_agent_env(),
    )
    try:
        parent = FakeProvider(
            TemplateResponder(
                {
                    "root": [
                        tool_chunks([
                            ("spawn", {"template": "coder", "task": "长任务", "wait": False}),
                            ("stop_worker", {"worker_id": "n002-coder"}),
                        ]),
                        tool_chunks([("wait_workers", {"timeout_seconds": 20})]),
                        tool_chunks([("report", {"summary": "已叫停"})]),
                    ]
                }
            )
        )
        runtime = Runtime(workflow, parent, run_dir,
                          remote_executor=RemoteExecutor(queue_root))
        runtime.start("任务")
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()
        os.environ.pop("FORCHAT_CHILD_SCRIPT", None)
        os.environ.pop("FORCHAT_CHILD_DELAY", None)

    assert runtime.workers["n002-coder"].status == "killed"


def test_queue_fencing_and_idempotency() -> None:
    """旧租约持有者写结果必须被拒；重复回传先写者赢。"""
    from forchat.remote_queue import RemoteQueue

    queue = RemoteQueue(tmpdir() / "remote", max_attempts=3)
    queue.submit("t", {"worker": {"id": "t"}})
    task_id, _payload, token1 = queue.lease("agent-a")
    assert queue.requeue(task_id) is True
    task_id, _payload, token2 = queue.lease("agent-b")

    assert queue.complete(task_id, "agent-a", token1, {"status": "stale"}) is False
    assert queue.result(task_id) is None
    assert queue.complete(task_id, "agent-b", token2, {"status": "done"}) is True
    assert queue.result(task_id)["status"] == "done"


def test_queue_retry_limit() -> None:
    """超过尝试上限进 dead，不会无限重派。"""
    from forchat.remote_queue import RemoteQueue

    queue = RemoteQueue(tmpdir() / "remote", max_attempts=2)
    queue.submit("poison", {"worker": {"id": "poison"}})
    task_id, _payload, _token = queue.lease("agent-a")
    assert queue.requeue(task_id) is True
    task_id, _payload, _token = queue.lease("agent-b")
    assert queue.requeue(task_id) is False
    dead = queue.dead_tasks()
    assert dead and dead[0]["task_id"] == "poison"
    assert queue.result(task_id) is None


def main() -> int:
    checks = [
        ("队列租约 + agent", test_queue_lease_and_agent),
        ("Runtime 远程叶子", test_runtime_remote_leaf_worker),
        ("远程取消", test_remote_cancel_marks_killed),
        ("租约 fencing / 幂等", test_queue_fencing_and_idempotency),
        ("重试上限进 dead", test_queue_retry_limit),
    ]
    return run("分布式调度自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
