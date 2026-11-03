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
| 执行 | `spawn`（含 `paths` 授权）、`wait_workers`、`stop_worker`（协作式）、hook（action/spawn）、`report` 完成信号 |
| 预算 | 单 worker tokens/墙钟/成本、整轮 `max_total_tokens/max_total_seconds/max_total_cost_usd`、深度/worker 数/并发上限 |
| 能力工具 | `read` `list_dir` `run_python(cwd)` `write_file`（覆盖前备份）`git`（子命令白名单）+ MCP 工具 |
| 记录 | append-only 事件流、`Replay.review/tree/summary`、`--runs` / `--inspect` / `--resume` |
| 安全 | `Policy`（根/敏感文件/scope/git 路径）、子进程脱敏环境、`.forchat-backups/`、受限子进程（超时/内存上限） |
| 稳定性 | 失败隔离、`RetryProvider`（首片前重试）、worker 级 `retries`、截断检测、预算熔断、续跑、中断 worker 可见 |
| 服务 | HTTP/SSE（`--serve`，stdlib）：启动运行、查状态、订阅事件流；Bearer token、限流、停止端点 |
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

我（作者）独立复跑过的关键结论：capstone / scale 的合并结果与测试套件、microproject 的失败门禁、
resume 的历史恢复——都不是只信模型自述。

## 4. 测试与守卫

```
pytest -q                    # 73 条用例（CI 用这个）
python tests/run_all.py      # 同上 + 分层校验 + demo + MCP 自检
```

- `tests/test_primitives.py`：40 条（预算/围栏/事件流滚动/续跑/中断/并发/报告/停止/成本/重试）
- `tests/test_tools.py`：20 条（policy/scope/环境擦除/备份/provider/重试/工具/成本/超时）
- `tests/test_cli.py`：5 条（--check / --inspect / --runs）
- `tests/test_server.py`：5 条（HTTP / SSE / 鉴权 / 限流 / 停止）
- `tests/test_subproc.py`：3 条（超时 / 脱敏环境 / 内存上限）
- `tests/test_layering.py`：依赖方向、领域词隔离、唯一执行循环

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
