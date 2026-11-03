# 发布说明：workflow 原语引擎

日期：2026-11-03  
状态：引擎层目标达成；部署层（沙箱/多租户/UI）不在本仓库范围。

## 1. 范围

本仓库做的是**原语引擎**：执行模型、能力面、记录面、装配面。不含任何业务领域词。

三层划分（详见 `CONTEXT.md`）：

```
原语     forchat/primitives/ + tools/ + providers/
workflow examples/workflows/<name>/（可加载配置，非代码）
运行输入 用户目标（运行时的 spec/plan，不进仓库）
```

## 2. 能力清单

| 面 | 内容 |
|---|---|
| 执行 | `spawn`（含 `paths` 授权）、`restart`（worker 自重启：同实例、清空对话、事实保留）、`wait_workers`、`stop_worker`（协作式）、hook（action/spawn）、`report` 完成信号、长会话上下文裁剪 |
| 预算 | 单 worker tokens/墙钟/成本、整轮 `max_total_tokens/max_total_seconds/max_total_cost_usd`、深度/worker 数/并发上限；**预算只记账不中断**（超了继续跑，整轮超了只拦新派生） |
| 能力工具 | `read(offset/limit)` `list_dir` `grep` `glob` `edit_file` `write_file`（覆盖前备份）`run_python(cwd)` `git`（子命令白名单）`web_fetch`（SSRF 防护）`web_search`（可配置后端）`screenshot`（视觉）`run_checks`（机器可判验收）+ MCP 工具 |
| 记忆 | 契约冻结（`write_contract` 写一次不可改，注入所有 agent、compact 不丢）；每 worker 一份事实库：`write_fact` / `read_facts`，落盘 `facts/<worker>.json`（可用 `FORCHAT_FACTS_DIR` 指到长期目录）；restart 沿用、新 agent 系统提示自动带 |
| 记录 | 事件流分文件（`events.jsonl` 控制/文本、`tools.jsonl` 工具全文）、跨进程文件锁、分段、关键事件 fsync；`Replay.review/tree/summary/graph`、`--runs` / `--inspect` / `--resume` |
| 安全 | `Policy`（根/敏感文件/scope/git 路径）、子进程脱敏环境、`.forchat-backups/`、受限子进程（超时/内存上限） |
| 稳定性 | 失败隔离、`RetryProvider`（首片前重试）、worker 级 `retries`、截断检测、预算记账、续跑、中断 worker 可见、追加式快照续跑、心跳/stuck 检测 |
| 服务 | HTTP/SSE（`--serve`）：启动运行、查状态、订阅事件流、Bearer token、限流、停止；单 worker 强停/换任务重开；`/metrics` Prometheus 文本（TTL 缓存） |
| 运维 | `--prune` 按最近 N 次/最老 N 天清理运行目录（默认 dry-run）；事件流分段滚动 + 关键事件 fsync |
| 计费 | `pricing.json` 价目表 → `report.cost_usd` / `--inspect` 成本行；缺价目显示未知 |

## 3. 真跑证据（可在 `.runs/` 复查）

| 运行 | 形状 | 结果 |
|---|---|---|
| `stress-20261103-150621` | 11 worker / 10 分支并行（并发 4 排队）→ 合并 10 次 → 复跑 10 套测试 | done，58k tokens，229s；护栏触发 6 次 |
| `scale-20261103-145527` | 7 worker / 6 分支并行（并发 4）→ 串行合并 → 复跑 6 套测试 | done，44.2k tokens，167.7s |
| `capstone-20261103-141817` | 5 worker / 3 分支 → 合并 → 复跑 | done，39.1k tokens，328s |
| `worktree-20261103-140201` | 3 worker，分支内写码+自测+提交 | done，27.2k tokens |
| `parallel-20261103-135615` | 3 worker，两 coder 并行 + wait_workers | done |
| `merge-20261103-140517` | 1 worker，串行合并 | done |
| `microproject-20261103-142509` | 3 worker，故意失败实验（质量门） | done，未粉饰失败；verifier 超时被隔离 |
| `delivery-*` | 3 worker，write_file + git 核对 | done |
| `minimal-*`（含续跑 3 轮） | 1~3 worker | done |
| `minimal-20261103-162951-527` | **HTTP + 真模型**：`POST /runs` → 3 worker → 41s / 37.5k tokens | `failed`（minimal 的 coder 只有 report/read_state/write_note，被要求做仓库侦查，reviewer 如实判"未达成"而非粉饰） |

我（作者）独立复跑过的关键结论：capstone / scale 的合并结果与测试套件、microproject 的失败门禁、
resume 的历史恢复——都不是只信模型自述。

## 4. 测试与守卫

```
pytest -q                    # 143 条用例（CI 用这个）
python tests/run_all.py      # 同上 + 分层校验 + demo + MCP 自检
```

- `tests/test_primitives.py`：13 条（预算/配置校验/跨进程事件流/工具事件分离）
- `tests/test_primitives_flows.py`：15 条（派生/hook/并发/契约冻结/report 门禁）
- `tests/test_primitives_lifecycle.py`：3 条（restart/每 worker 事实）
- `tests/test_primitives_resources.py`：10 条（compact/保留配置/资源感知/排队/心跳/看门狗）
- `tests/test_primitives_state.py`：11 条（状态/续跑/自动恢复/运行图）
- `tests/test_primitives_guards.py`：11 条（scope/停止/快照/重试/预算硬闸）
- `tests/test_tools.py`：18 条（registry/provider/重试/成本/read/edit/grep/glob/web_fetch/run_checks）
- `tests/test_policy.py`：8 条（policy/scope/脱敏/备份）
- `tests/test_cli.py`：7 条（--check / --inspect / --runs / --prune / compact 清理）
- `tests/test_server.py`：13 条（HTTP / SSE 分段+断点续传 / metrics 缓存/卡死计数 / 摘要签名缓存 / artifacts 列表+下载 / 鉴权 / 限流 / 停止 / 单 worker 强停与替换 / 成本）
- `tests/test_stream.py`：3 条（请求级子进程 / 超时硬杀 / 取消硬杀）
- `tests/test_web.py`：3 条（web_search 后端 / 视觉标记转多模态）
- `tests/test_process_worker.py`：2 条（叶子 worker 子进程执行 / 子进程硬杀）
- `tests/test_concurrency.py`：4 条（原子写 / 旧 hash 拒绝 / edit 校验 hash+mtime）
- `tests/test_locks.py`：3 条（同进程等锁 / 超时 / 跨进程互斥）
- `tests/test_sandbox_win.py`：4 条（工作区写/外部拒 / read-only / fail-closed / 关闭对比；非 Windows 跳过）
- `tests/test_exec.py`：2 条（环境脱敏 / 按 worker kill）
- `tests/test_subproc.py`：5 条（超时 / 脱敏环境 / 内存上限 / 取消 kill 工具子进程 / force_stop 联动）
- `tests/test_layering.py`：依赖方向、领域词隔离、唯一执行循环、单文件 ≤500 行

CI：`.github/workflows/ci.yml`（ubuntu + Python 3.12，pytest + 分层 + demo）。

## 5. 已知边界（不是缺陷，是范围）

| 边界 | 说明 |
|---|---|
| 无沙箱 | `run_python` 是任意代码；资源限制（超时/内存）不是文件系统隔离，见 `docs/sandbox.md` |
| 叫停是协作式 | 在途网络请求不会立刻断 |
| 读不受 scope 限制 | 只受项目根约束（读无副作用） |
| 崩溃恢复是"暴露"不是"复活" | 中断 worker 标 `interrupted` 并把任务交给新根，不自动重放在途执行 |
| 成本需要自己配价目表 | 没有 `pricing.json` 时明确显示"未知"，不记 0 |
| HTTP 服务无鉴权 | 默认只绑 `127.0.0.1`；鉴权/多租户属部署层 |
| 没有 UI | 命令行 + 事件流 + HTTP/SSE |

## 6. 仓库状态

- 35+ 次提交，工作区干净（`git status` 为空）；
- 单个执行循环（`primitives/runtime.py`），旧实现与草案契约已删除；
- 文档：`README.md`（用法）、`CONTEXT.md`（词汇）、`AGENTS.md`（规则与踩坑）、`docs/sandbox.md`（部署隔离）、`docs/decisions/`（ADR）。
