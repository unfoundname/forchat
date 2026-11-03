"""web_search：联网搜索（默认用 DeepSeek Search API）。

后端选择：
- 默认：`https://api.deepseek.com/v1/tools/search`，key 取 `FORCHAT_SEARCH_KEY` 或 `DEEPSEEK_API_KEY`；
- 覆盖：`FORCHAT_SEARCH_URL`（以及可选的 `FORCHAT_SEARCH_KEY`）指向任何兼容后端。

两种响应都接受：DeepSeek 的 `results[].summary`，或通用的 `results[].snippet`。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..registry import tool

TIMEOUT = 20
MAX_BYTES = 1_000_000
DEFAULT_URL = "https://api.deepseek.com/v1/tools/search"


@tool(
    description="联网搜索（默认走 DeepSeek Search API，需要 DEEPSEEK_API_KEY）。"
                "返回标题/URL/摘要列表，用于查新 API、线上报错、别人仓库等开放世界信息。",
    params={
        "query": "搜索词",
        "max_results": "最多返回几条，默认 5",
    },
)
def web_search(query: str, max_results: int = 5) -> str:
    endpoint = os.environ.get("FORCHAT_SEARCH_URL", "").strip() or DEFAULT_URL
    key = (os.environ.get("FORCHAT_SEARCH_KEY", "").strip()
           or os.environ.get("DEEPSEEK_API_KEY", "").strip())
    if not key:
        return ("错误：未配置搜索 key。设置 DEEPSEEK_API_KEY（默认走 DeepSeek Search API），"
                "或 FORCHAT_SEARCH_URL + FORCHAT_SEARCH_KEY 指向别的后端。")

    count = max(1, min(50, int(max_results)))
    payload = json.dumps({
        "query": str(query),
        "count": count,
        "max_results": count,        # 兼容自定义后端；DeepSeek 会忽略未知字段
        "need_content": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        return f"错误：搜索接口 HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"错误：搜索失败：{exc}"

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return "错误：搜索后端返回的不是 JSON"
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return "错误：搜索后端返回格式不符合预期（需要 results 数组）"

    lines = []
    for item in items[:count]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        summary = (str(item.get("summary") or "").strip()
                   or str(item.get("snippet") or "").strip()
                   or str(item.get("content") or "").strip())
        lines.append(f"- {title}\n  {url}\n  {summary[:400]}".rstrip())
    return "\n".join(lines) or "（无结果）"
