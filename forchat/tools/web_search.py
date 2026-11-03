"""web_search：把搜索委托给一个可配置的搜索后端。

不内置抓网页搜索（不稳定、容易被封、解析脆弱）。后端地址/密钥由环境变量给：
    FORCHAT_SEARCH_URL   例如 https://api.example.com/search
    FORCHAT_SEARCH_KEY   Bearer token（可选）
请求：POST {"query": "...", "max_results": N}
期望响应：{"results": [{"title","url","snippet"}]} 或直接数组。
DeepSeek 等提供的搜索接口可以直接填在 URL 上。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..registry import tool

TIMEOUT = 20
MAX_BYTES = 1_000_000


@tool(
    description="联网搜索（需要配置 FORCHAT_SEARCH_URL）。返回标题/URL/摘要列表，用于查新 API、"
                "线上报错、别人仓库等开放世界信息。",
    params={
        "query": "搜索词",
        "max_results": "最多返回几条，默认 5",
    },
)
def web_search(query: str, max_results: int = 5) -> str:
    endpoint = os.environ.get("FORCHAT_SEARCH_URL", "").strip()
    if not endpoint:
        return ("错误：未配置搜索后端。设置 FORCHAT_SEARCH_URL（以及可选的 FORCHAT_SEARCH_KEY）后可用；"
                "DeepSeek 的搜索接口可以直接填在这里。")
    key = os.environ.get("FORCHAT_SEARCH_KEY", "").strip()
    payload = json.dumps({"query": str(query), "max_results": int(max_results)}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
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
    for item in items[: max(1, int(max_results))]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        lines.append(f"- {title}\n  {url}\n  {snippet[:300]}".rstrip())
    return "\n".join(lines) or "（无结果）"
