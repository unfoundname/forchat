# AGENTS

给在本仓库工作的 agent（和未来的自己）的规则。先读 `CONTEXT.md` 对齐词汇。

## 五条硬规则（违反即返工）

1. **禁止二次实现**：同一能力只允许一处实现。要加"像 X 一样"的东西，先看 X 能不能扩展；能扩展就不许新写一份。
2. **一次实现，先合并结构相近的东西**：发现两处结构相似（比如两个 registry、两条循环、两套 hook 校验），先合并成一套再往前走，不要"先各写一份，以后再统一"。
3. **依赖只能向下**：`primitives → providers / tools / workflow / runtime`，`apps → runtime`。反向 import 视为架构事故，`tests/test_layering.py` 会拦。
4. **没用的东西及时删**：不保留"可能以后用得上"的代码、配置字段、事件类型、文档章节。删掉比留着便宜——留着的东西会被误用、会被复制、会让后来的人以为它是活的。判据：一个符号若在仓库里没有任何调用点（测试除外），删。
5. **单文件不超过 500 行**：超过就按职责拆（mixin / 纯函数 / 数据声明），不许靠压缩行数糊弄。`tests/test_layering.py` 机械拦截。

## 分层

```
forchat/primitives/   原语：types / store / scheduler / hooks / workflow / replay / runtime*
forchat/providers/    模型侧：base(Protocol) / fake(测试与演示) / deepseek(唯一用 SDK 的地方)
forchat/tools/        能力工具（read/write/edit/grep/glob/run_python/git/web_*/screenshot/run_checks）
forchat/skills/       技能加载
forchat/server*.py    HTTP/SSE：server.py 路由 + server_manager.py 运行管理
forchat/exec.py       唯一进程出口：环境脱敏/沙箱/取消/限额都在这里
forchat/subproc.py    受限子进程封装（超时/取消），内部只调 exec.spawn
forchat/stream_exec.py / worker_proc.py   请求级 / 整 worker 子进程（硬中断）
forchat/__main__.py   只做装配与展示，不含执行逻辑（执行只在 primitives/runtime_worker.py）
```

`Runtime` 按职责拆成 mixin（都 ≤ 500 行）：`runtime.py` 装配与调度、`runtime_worker.py` 执行循环、
`runtime_tools.py` 工具、`runtime_state.py` 续跑/快照/metrics、`runtime_prompt.py` 提示词、
`runtime_compact.py` compact、`runtime_specs.py` schema。

历史包袱已清：旧的单 agent 循环（`agent.py`）和草案契约层（`core/contracts.py`）都已删除，它们和原语层重复。

## 测试（不需要网络/API key）

```powershell
python tests/run_all.py             # 一条命令：全部测试文件 + 分层 + demo + MCP（提交前跑这个）
pytest -q                           # 同一批用例走 pytest（CI 用的就是这个）

python tests/test_primitives.py         # 原语：预算/配置校验
python tests/test_primitives_flows.py   # 原语：派生/hook/并发
python tests/test_primitives_state.py   # 原语：状态/续跑/预算闸
python tests/test_primitives_guards.py  # 原语：scope/停止/快照/重试
python tests/test_tools.py              # 工具：registry/provider/重试/成本
python tests/test_policy.py             # 安全：policy/scope/脱敏/备份
python tests/test_cli.py                # CLI：--check/--inspect/--runs/--prune
python tests/test_server.py             # HTTP/SSE/metrics/鉴权/限流/停止
python tests/test_subproc.py            # 受限子进程：超时/脱敏/内存上限
python tests/test_layering.py           # 依赖方向/领域词/唯一循环/单文件 ≤500 行
python examples/run_demo.py             # 端到端：假 provider 跑一次完整运行
```

真模型只在最后一步接：`providers/deepseek.py` 实现 `Provider.stream`，其余代码一行不改。

## 写 workflow 包

目录：`examples/workflows/<name>/`，含 `workflow.json` + `prompts/*.md`。

- 字段严格校验，拼错字段名会在加载期报错，不会静默忽略；
- 每个模板都要有 `role` 或 `role_file`；
- `can_spawn` 里的名字必须存在；想表达"叶子"就把 `can_spawn` 留空；
- `report` 是完成信号，模板的 role 里必须告诉模型"结束时要 report"。

## 踩坑记录

- **subprocess 捕获输出必须显式 `encoding="utf-8", errors="replace"`**：Windows 默认按 GBK 解码子进程输出，遇到 UTF-8 中文会抛 `UnicodeDecodeError`（本仓库已踩过一次，`run_python` 和 `--check` 的测试都显式传了）。
- **不要用 PowerShell 管道改仓库里的文本文件**：`Get-Content | Set-Content` 会按 GBK 读、UTF-8 写，中文直接变乱码（README 踩过）。改文件用编辑工具，别走 shell 管道。
- **提交前先看 `git status`，不要闭眼 `git add -A`**：真跑会在仓库根留下 `stats.py`、`_n002_selfcheck.py` 这类残留，`add -A` 会把它们带进提交（踩过两次）。运行产物应落在 `.runs/` 或 `examples/projects/` 下。

## 提交前

- 新增原语要有对应的 `tests/test_primitives.py` 用例（含"删掉它某能力就表达不出来"的反证思路）；
- 改 `workflow.json` 要保证 `examples/run_demo.py` 仍然通过；
- 不要在 `primitives/` 里出现领域词（B 站、视频、商品…）。
