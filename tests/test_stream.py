"""请求级子进程的硬中断自检（不需要 key：离线脚本模式）。"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import run, tmpdir

from forchat.stream_exec import ProcessStreamFactory, StreamKilled


def _with_script(chunks: list[dict]):
    work = tmpdir()
    path = work / "chunks.json"
    path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    os.environ["FORCHAT_STREAM_CHILD_SCRIPT"] = str(path)
    return path


def test_script_mode_streams_chunks() -> None:
    _with_script(
        [
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    try:
        factory = ProcessStreamFactory(model="fake", chunk_timeout=20)
        chunks = list(factory([{"role": "user", "content": "x"}], None, None))
    finally:
        os.environ.pop("FORCHAT_STREAM_CHILD_SCRIPT", None)
    assert chunks
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"


def test_chunk_timeout_kills_child() -> None:
    _with_script([{"sleep": 5}, {"choices": [{"delta": {"content": "late"}}]}])
    factory = ProcessStreamFactory(model="fake", chunk_timeout=0.4)
    started = time.time()
    try:
        try:
            list(factory([{"role": "user", "content": "x"}], None, None))
        except StreamKilled:
            pass
        else:
            raise AssertionError("应当抛 StreamKilled")
    finally:
        os.environ.pop("FORCHAT_STREAM_CHILD_SCRIPT", None)
    assert time.time() - started < 4


def test_cancel_kills_child() -> None:
    _with_script([{"sleep": 5}, {"choices": [{"delta": {"content": "late"}}]}])
    factory = ProcessStreamFactory(model="fake", chunk_timeout=20)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.time()
    try:
        try:
            list(factory([{"role": "user", "content": "x"}], None, cancel))
        except StreamKilled:
            pass
        else:
            raise AssertionError("应当抛 StreamKilled")
    finally:
        os.environ.pop("FORCHAT_STREAM_CHILD_SCRIPT", None)
    assert time.time() - started < 4


def main() -> int:
    checks = [
        ("子进程流式脚本", test_script_mode_streams_chunks),
        ("分片超时硬杀", test_chunk_timeout_kills_child),
        ("取消硬杀", test_cancel_kills_child),
    ]
    return run("请求级子进程自检", checks)


if __name__ == "__main__":
    raise SystemExit(main())
