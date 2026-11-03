# Changelog

## 0.2.0 — 2026-11-03

### 执行与隔离
- 整 worker 进程化（`FORCHAT_PROCESS_WORKERS=1`）：叶子 worker 的完整循环跑在子进程，stop 直接 kill。
- 请求级硬中断（`FORCHAT_HARD_KILL=1`）：模型流式请求放进子进程，取消/分片超时直接 kill。
- 工具子进程可被 stop 硬杀（`subproc` 取消注册表）。
- 进程创建收敛到唯一出口 `forchat/exec.py`，`test_layering.py` 禁止绕过。
- Windows ACL 写沙箱（`FORCHAT_SANDBOX=acl`，可选）：受限令牌 + Low 完整性 + 能力 SID，fail-closed；
  边界见 `docs/sandbox-windows.md`。

### 预算与并发
- 预算硬闸：`budget.hard_tokens` / `limits.max_total_hard_tokens`，超限 force_stop；默认关闭。
- `write_file` / `edit_file` 原子写 + 跨进程写锁 + `expected_sha256` / `expected_mtime` 乐观并发。

### 可观测与服务
- SSE 支持 `Last-Event-ID` / `?since=` 断点续传；新增 artifacts 列表/下载端点。
- 卡死看门狗（`FORCHAT_STUCK_SECONDS` / `FORCHAT_STUCK_ACTION`）；metrics 增加状态计数与预算硬停计数。
- 事件流分文件：工具全文进 `tools.jsonl`，thinking 整轮合并；`tools.jsonl` 也滚动。

### 模型能力
- `web_search` 默认接入 DeepSeek Search API（`/v1/tools/search`，`DEEPSEEK_API_KEY`）。
- `web_fetch` 可选 DeepSeek Fetch API 后端（`FORCHAT_FETCH_BACKEND=deepseek`，`/v1/tools/fetch`）。
- `deepseek-flash` 视觉链路真模型验证：`image_url` data URL → 读图正确（生成 "42" 的 PNG）。

### 安全
- SSRF 防护升级：重定向逐跳校验，十进制/八进制/十六进制 IP、IPv6/映射地址归一化拦截。
- `run_checks` 接 report 门禁（`require_checks`）；截断的工具调用不再执行。
