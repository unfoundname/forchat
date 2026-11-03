"""环境变量清洗：给子进程（脚本、git、MCP 服务端）一个不含密钥的环境。

原因：`run_python` 执行的是任意代码，它继承了宿主进程的环境变量，
包括 `DEEPSEEK_API_KEY` 这类密钥——不擦掉，模型写一行代码就能把 key 读走并外发。

配置项仍可以通过显式 `extra` 注入（例如 MCP 服务端自己在配置里写的 env）。
"""
from __future__ import annotations

import os
import re

# 只擦这些"看起来是密钥"的变量，其余照常传递，避免误伤 PATH/HOME/TEMP 等
SENSITIVE = re.compile(r"(API_KEY|SECRET|_TOKEN$|TOKEN$|PASSWORD|CREDENTIAL|ACCESS_KEY)", re.I)


def clean_environ(
    extra: dict[str, str] | None = None,
    keep: tuple[str, ...] = (),
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in keep or not SENSITIVE.search(key)
    }
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
