# ForChat

一个 workflow 原语引擎：**原语（primitives）→ 可加载的 workflow 配置 → 一次运行**。
不含任何业务；"复刻某某"是运行输入，不是代码。

## 目录

```
ForChat/
├── AGENTS.md                   给 agent 的工作规则（四条硬规则）
├── CONTEXT.md                  领域词汇表：三层、worker、fence、report…
├── docs/
│   ├── decisions/              ADR
│   ├── sandbox.md              部署层隔离清单（容器/挂载/网络/cgroup/secrets）
│   └── release.md              发布说明：能力清单、真跑证据索引、已知边界
├── forchat/
│   ├── __main__.py             CLI：只装配，不执行
│   ├── primitives/             ★ 原语层（唯一执行循环在 runtime_worker.py）
│   │   ├── types.py            WorkerInstance / Budget / Report
│   │   ├── store.py            EventLog：events.jsonl + tools.jsonl，分段/文件锁/fsync
│   │   ├── scheduler.py        并发准入（等待型子 worker 内联，避免死锁）
│   │   ├── hooks.py            action / spawn 两类 hook
│   │   ├── workflow.py         workflow.json 严格加载与校验
│   │   ├── runtime.py          ★ Runtime：装配、调度、进程 worker、看门狗
│   │   ├── runtime_worker.py   执行循环（唯一一份）+ 流式解析 + 收尾
│   │   ├── runtime_compact.py  compact：归档 + 模型摘要 / 确定性兜底
│   │   ├── runtime_contracts.py 契约：写入/变更提案/决策/hash+checks
│   │   ├── runtime_tools.py    工具分发 + 运行时工具（spawn/report/facts/contracts/…）
│   │   ├── runtime_state.py    续跑恢复 / plan / 追加式快照 / metrics
│   │   ├── runtime_prompt.py   初始消息与兜底报告
│   │   ├── runtime_specs.py    运行时工具 schema
│   │   └── replay.py           review / tree / summary / graph
│   ├── providers/              base · fake(离线) · retry · deepseek(唯一用 SDK)
│   ├── tools/                  能力工具：read/grep/glob/edit_file/write_file/run_python/
│   │                           git/web_fetch/web_search/screenshot/run_checks
│   ├── skills/                 技能加载（SKILL.md）
│   ├── server.py               HTTP/SSE/metrics/artifacts 路由
│   ├── server_manager.py       RunManager：列表/状态/图/附件/停止/替换/缓存
│   ├── exec.py                 唯一进程出口（脱敏/沙箱/取消/Job 限额）
│   ├── sandbox_win.py          Windows ACL 写沙箱（受限令牌 + Low 完整性）
│   ├── stream_exec.py          请求级子进程（可 kill 的模型流）
│   ├── stream_child.py         子进程入口（python -m forchat.stream_child）
│   ├── worker_proc.py          整 worker 子进程入口（python -m forchat.worker_proc）
│   ├── remote_queue.py         共享目录任务队列（租约/心跳/取消/结果）
│   ├── remote_agent.py         远程 worker agent（python -m forchat.remote_agent）
│   ├── remote_executor.py      coordinator 侧远程执行器（注入 Runtime）
│   ├── subproc.py              受限子进程 + 取消注册表（kill 工具进程）
│   ├── policy.py               参数级护栏（不是沙箱）
│   ├── pricing.py              价目表 → 成本
│   ├── runs.py                 运行目录/compact 归档清理计划
│   ├── env_scrub.py            子进程环境脱敏
│   ├── registry.py             工具注册表
│   ├── tool_bridge.py          registry → Runtime 的注入层
│   ├── mcp.py / mcp_bridge.py  MCP 客户端与工具桥接
│   └── skills/__init__.py      技能加载实现
├── tests/                     全部文件 ≤500 行
│   ├── run_all.py              一条命令跑全部本地检查
│   ├── _support.py             共享夹具：MINIMAL、tmpdir、用例执行器
│   ├── test_primitives.py      预算/配置校验/跨进程事件流/工具日志（14 条）
│   ├── test_primitives_flows.py    派生/hook/并发/契约冻结/report 门禁（16 条）
│   ├── test_primitives_resources.py compact/资源感知/排队/心跳/看门狗（10 条）
│   ├── test_primitives_lifecycle.py restart/每 worker 事实（3 条）
│   ├── test_primitives_state.py    状态/续跑/自动恢复/运行图（11 条）
│   ├── test_primitives_guards.py   scope/停止/快照/重试/预算硬闸（11 条）
│   ├── test_tools.py           registry/provider/重试/成本/read/edit/grep/glob（18 条）
│   ├── test_policy.py          policy/scope/脱敏/备份（8 条）
│   ├── test_cli.py             --check/--inspect/--runs/--prune（7 条）
│   ├── test_server.py          HTTP/SSE/metrics/artifacts/强停（13 条）
│   ├── test_subproc.py         超时/脱敏/内存/取消（5 条）
│   ├── test_stream.py          请求级子进程 / 超时硬杀 / 取消硬杀（3 条）
│   ├── test_process_worker.py  叶子 worker 子进程执行 / 硬杀（2 条）
│   ├── test_sandbox_win.py     Windows ACL 沙箱（4 条，非 Windows 跳过）
│   ├── test_exec.py            进程出口：脱敏 / 按 worker kill（2 条）
│   ├── test_concurrency.py     原子写 / 乐观并发（4 条）
│   ├── test_locks.py           写锁：等锁/超时/跨进程（3 条）
│   ├── test_web.py             web_search/视觉/SSRF 边界（8 条）
│   └── test_layering.py        依赖方向 / 领域词 / 唯一循环 / 单文件 ≤500 行
├── skills/                     技能目录（数据）
└── mcp_servers.json            MCP 服务端配置
```

## 安装

```powershell
pip install -e ".[dev]"     # 开发：装上 forchat 命令 + pytest
pip install -e ".[llm]"     # 接真模型：装 openai
```

装完后可以直接 `forchat --help`（入口 `forchat.__main__:main`）。

## 跑起来（不需要 key）

```powershell
python tests/run_all.py            # 全部本地检查（全部测试文件 + 分层 + demo + MCP，推荐）
pytest -q                          # 同一批用例走 pytest（CI 用的就是这个）
python -m forchat --demo           # 一键跑通：假 provider 完整运行（root → coder → reviewer）
python -m forchat --init my-workflow   # 生成最小 workflow 包（workflow.json + prompts/root.md）
python tests/test_primitives.py            # 原语：预算/配置校验
python tests/test_primitives_flows.py      # 原语：派生/hook/并发
python tests/test_primitives_resources.py  # 原语：compact/资源/排队/心跳
python tests/test_primitives_lifecycle.py  # 原语：restart/事实
python tests/test_primitives_state.py      # 原语：状态/续跑/运行图
python tests/test_primitives_guards.py     # 原语：scope/停止/快照/重试/预算硬闸
python tests/test_tools.py                 # 工具：registry/provider/重试/成本
python tests/test_policy.py                # 安全：policy/scope/脱敏/备份
python tests/test_cli.py                   # CLI：--check/--inspect/--runs/--prune
python tests/test_server.py                # HTTP/SSE/metrics/artifacts/强停
python tests/test_subproc.py               # 受限子进程：超时/脱敏/取消
python tests/test_stream.py                # 请求级子进程硬中断
python tests/test_process_worker.py        # 整 worker 进程化硬杀
python tests/test_sandbox_win.py           # Windows ACL 沙箱（非 Windows 跳过）
python tests/test_exec.py                  # 进程唯一出口
python tests/test_concurrency.py           # 原子写/乐观并发
python tests/test_locks.py                 # 跨进程写锁
python tests/test_web.py                   # web_search/视觉/SSRF 边界
python tests/test_remote.py                # 分布式队列/agent/fencing
python tests/test_contracts.py             # 契约 hash + checks
python tests/test_supervision.py           # 超时先视察、不自动重启
python tests/test_capture.py               # 窗口/无头网页截图
python tests/test_visual.py                # 看图/像素对比/网页正文
python tests/test_crash.py                 # 崩溃注入
python tests/test_recovery.py              # 半写状态恢复
python -m forchat --demo                   # 一键假 provider 端到端（不需要 key）
python -m forchat --init my-workflow       # 生成最小 workflow 包
```

## 跑真模型

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
python -m forchat "把最小系统跑通"
python -m forchat --workflow examples/workflows/minimal/workflow.json "目标"
python -m forchat --tools          # 能力工具（含 MCP）
python -m forchat --skills         # 技能
python -m forchat --mcp            # 连 MCP 服务端
python -m forchat --inspect .runs/<run>   # 复查一次历史运行（树 + 每个 worker 的消耗与摘要）
python -m forchat --check examples/workflows/minimal/workflow.json   # 只校验配置，不跑模型
python -m forchat --runs           # 列出历史运行（状态 / worker 数 / token / 耗时）
python -m forchat --prune --keep 20          # 看清理计划（默认 dry-run，--yes 才真删）
python -m forchat --resume .runs/<run> "继续推进"                    # 在同一个运行目录上续跑
```

## Docker 部署（可选）

镜像只装引擎（不装测试/开发依赖），非 root 运行：

```powershell
docker build -t forchat:0.2.0 .
docker run --rm -it -v ${PWD}:/workspace -w /workspace `
  -e DEEPSEEK_API_KEY=$env:DEEPSEEK_API_KEY `
  forchat:0.2.0 --workflow examples/workflows/minimal/workflow.json "跑通最小示例"
```

`docker-compose.yml` 额外加了部署层隔离：只读根文件系统（工作区挂载可写）、`/tmp` tmpfs、
`cap_drop: ALL`、`no-new-privileges`、内存/pid 限额。用法：

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
docker compose run --rm forchat --workflow examples/workflows/minimal/workflow.json "目标"
```

注意：容器隔离的是**部署环境**，不是引擎内部工具；`run_python` 在容器里仍是任意代码，
真正的边界来自只读根、挂载范围、用户权限和网络策略（见 [docs/sandbox.md](docs/sandbox.md)）。

## 分布式执行（可选）

默认本地线程/子进程。设置 `FORCHAT_REMOTE_DIR` 指向共享目录后，**叶子 worker** 会被投递到
队列，由其他机器上的 `python -m forchat.remote_agent --queue <dir>` 租约执行、回传结果：

```powershell
$env:FORCHAT_REMOTE_DIR = "\\shared\forchat\queue"
python -m forchat --workflow examples/workflows/delivery/workflow.json "目标"
```

支持租约、心跳、协作式取消；租约超时（默认 1800s）**只标记 suspect，不自动重派**——
父 agent 用 `inspect_worker` 视察、确认偏离后才 `stop_worker`/显式 requeue。
要求 queue 和 run 目录共享、执行机代码版本一致。
安全与限制见 [docs/distributed.md](docs/distributed.md)（这是信任边界，不是零信任协议）。

**监督超时**：看门狗默认 `FORCHAT_STUCK_SECONDS=1800`（30 分钟），超过只记 `stuck_detected`
并标 suspect，**不自动 stop/restart**；父 agent 收到 `wait_workers` 的 `suspect=true` 后先
`inspect_worker(worker_id)` 看任务/预算/最近事件，确认真的跑偏再重启或关闭。
`FORCHAT_STUCK_ACTION=stop` 才恢复旧行为。

## 三层

| 层 | 是什么 | 放哪 |
|---|---|---|
| 原语 | 执行模型 / 能力面 / 记录面 / 装配面 | `forchat/primitives/` |
| workflow | 可加载配置：worker 模板、can_spawn、预算、hook、限制 | `examples/workflows/<name>/workflow.json` |
| 运行输入 | 用户目标与澄清结果（spec/plan） | 运行时状态，不进仓库 |

## 示例一览

下面这些 workflow 包各自验证一组原语，互相不重复（prompt 也几乎没有公共模板——审计过，唯一重复的行是 `## 工作方式` 这个小标题）：

| 包 | 演示什么 | 主要原语 |
|---|---|---|
| `minimal` | 最小闭环：root → coder → hook 派生 reviewer | spawn / hook / report |
| `delivery` | 真落盘 + git 核对 | write_file / git |
| `microproject` | 写代码 + 写测试 + 自动验收 | run_python / read |
| `parallel` | 两个 coder 并行后汇合 | spawn(wait=false) / wait_workers |
| `worktree` | 隔离分支开发 + worktree 内自测 | git worktree / run_python(cwd) |
| `merge` | 串行合并多个功能分支 | git merge |
| `capstone` | 封顶：3 分支并行 → 汇合 → 串行合并 → 复跑 | spawn(wait=false) / wait_workers / git merge |
| `scale` | 规模：6 分支并行（并发上限 4）→ 汇合 → 合并 → 复跑 | 同上 + 并发限流 |
| `stress` | 压力：10 分支并行（并发 4 排队）→ 合并 10 次 → 复跑 | 同上 + 排队与护栏压力 |
| `capture` | worker 自己截应用窗口/网页，看图后 report | window_screenshot / page_screenshot / 视觉 |
| `heavy` | 重型项目复刻：侦察 → 契约 → 地基 → 并行切片 → review → 合并验收 | 全量原语 + worktree + contracts + checks |
| `frontend` | 页面对照复刻：参考图 → critic 对比 → fixer 循环 → 机器 checks | 视觉工具 + 独立 critic，root 不拍板 |

## 写一个 workflow 包

```
examples/workflows/minimal/
├── workflow.json
└── prompts/{root,coder,reviewer}.md
```

`workflow.json` 只描述结构，字段拼错会在加载期报错：

```json
{
  "name": "minimal", "version": 1, "entry": "root",
  "limits": {
    "max_concurrent": 2, "max_depth": 3, "max_workers": 8,
    "max_wall_seconds": 3600, "max_total_tokens": 200000
  },
  "workers": {
    "root": {
      "role_file": "prompts/root.md",
      "can_spawn": ["coder"],
      "tools": ["spawn", "report", "read_state", "write_note"],
      "budget": {"tokens": 20000, "wall_seconds": 600},
      "hooks": {"on_done": [{"action": "snapshot"}]}
    }
  }
}
```

`limits` 是**整次运行**的闸（对应 `budget` 管单个 worker）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_concurrent` | 2 | 并发槽位数 |
| `max_depth` | 3 | 派生深度 |
| `max_workers` | 16 | worker 总数 |
| `max_wall_seconds` | 3600 | 整轮墙钟 |
| `max_total_tokens` | 1,000,000 | 软总闸；超了记 `run_budget_exceeded`、拒绝新派生，正在跑的收尾 |
| `max_total_hard_tokens` | 0（关） | **硬闸**：总 token 超了直接 `force_stop` 所有 worker |
| `max_total_seconds` | 3600 | 与 `max_wall_seconds` 取小 |

`run_finished` 事件里会记 `total_tokens` 与 `budget_limit`，`--inspect` 能看到。

运行时只给模型十三个工具：`spawn`（派子 worker，受 can_spawn/深度/worker 数约束）、`report`（完成信号）、
`read_state` / `write_note`（plan 与笔记，落盘不靠记忆）、`write_artifact`（交付附件）、
`read_worker_report`（父 worker 回看子报告）、`wait_workers`（汇合 `spawn(wait=false)` 的异步分支）、
`stop_worker`（协作式叫停：下一分片/下一轮边界退出，状态记为 `killed`）、
`restart`（把**自己**重启：同一个 worker 实例、清空对话、事实保留——上下文涣散时比硬喂更划算）、
`write_fact` / `read_facts`（跨 agent 的共享事实库，落盘 `state.json`，新 agent 的系统提示里自动带）。

**agent 生命周期**：`spawn` 新开、`restart` 自己重启自己（同实例、清空对话）、`stop_worker` 叫停；
**事实库**是"重启也不丢"的那一层：`write_fact("构建命令", "pytest -q")` 之后，`restart`/`--resume` 出的
agent 在系统提示里都能看到它，不必靠对话记忆。

**契约生命周期**：`write_contract("GET /api/videos", "返回 {items:[...]}", status="draft")` 之后，
契约会注入**所有** agent 的系统提示，**compact 也抹不掉**。状态分 `draft`（演进中）、`frozen`
（下一波依赖）、`deprecated`（废弃）。契约缺漏是常态：任何 worker 可 `propose_contract_change`
（带完整新内容 + 影响面），root 用 `decide_contract_change` 批准后升版本；coder 的 report 要声明
`contracts_used`（key→版本），版本不一致会被拒绝。

**机器校验**：契约可带 `checks`（格式同 `run_checks`），并自动记录内容 sha256；模板写
`"require_contracts": true` 后，`report` 前会**重新算 hash + 跑 checks**，任一失败就拒绝报告
（事件 `contract_violation` / `contract_verified`，metrics `forchat_contract_violations_total`）。
也可以随时调 `verify_contract` 主动校验。这样"契约"不只是提示词，而是可被机器判定、可演进的门禁。

**覆盖矩阵**：契约有 `covers`（路由/文件 glob）；项目根 `docs/surface.json` 列全部 surface 项与
`exceptions`。模板写 `"require_coverage": true` 后，`report` 前会跑 `check_contract_coverage`：
任何 surface 项既没被契约覆盖、也不在 exceptions 里，就拒绝报告。

`write_artifact` 写出的文件会自动进 `report.artifacts`，模型不必再手抄路径。

**轮数默认不限**：协调型 worker 跑几十上百轮是正常的，真正的闸是预算（tokens / wall_seconds）
和整轮墙钟。需要给单个模板设上限时，在 `workflow.json` 里写 `max_rounds`。

## 能力工具（read / grep / glob / edit_file / run_python / write_file / git / web_fetch / run_checks）

- `read` / `list_dir`：读文件与目录；`read` 支持 `offset` / `limit`，大文件可分段读；
- `grep` / `glob`：按正则搜内容、按模式找文件（找符号/调用点不用逐层猜路径）；
- `edit_file`：精确替换（`old_string` 默认必须唯一），改一两行不必整文件重写；
- `run_python`：执行脚本，支持 `cwd`（worktree 里跑测试就靠它）；
- `write_file`：把内容真正落盘（路径安全由 policy 统一拦，覆盖前自动备份）；
- **并发安全**：`write_file` / `edit_file` 都是**原子写**（临时文件 + `os.replace`），
  且整段持有**按路径的跨进程写锁**（`msvcrt` / `flock`，超时 `FORCHAT_WRITE_LOCK_TIMEOUT`，默认 5s）；
  可选 `expected_sha256` / `expected_mtime` 做乐观并发校验——文件被别人改过就拒绝并回传当前值；
  锁是建议锁：`run_python` 里的脚本不会自动遵守；
- `git`：**子命令白名单**（status/diff/log/add/commit/branch/worktree/merge/…），不是裸 shell；
- `web_fetch`：抓 URL 文本（查新 API 文档 / 线上报错 / 别人仓库页面）；默认本地抓取，
  SSRF 防护含**重定向逐跳校验**，十进制/八进制/十六进制 IP、IPv6/映射地址、localhost 变体都归一化拦截；
  设 `FORCHAT_FETCH_BACKEND=deepseek` 可改用 DeepSeek Fetch API（`/v1/tools/fetch`，服务端返回 Markdown）；
  限时 20s、限读 500KB；
- `web_search`：联网搜索，**默认走 DeepSeek Search API**（`POST /v1/tools/search`，key 取 `DEEPSEEK_API_KEY`）；
  也可用 `FORCHAT_SEARCH_URL` / `FORCHAT_SEARCH_KEY` 指向别的兼容后端；
- `screenshot`：抓屏存 PNG；`FORCHAT_VISION=1` 时运行时把它转成 `image_url` 多模态消息
  （`deepseek-flash` 原生支持 OpenAI 兼容的 image_url，已用真模型验证）；
- `window_screenshot`：**无浏览器**截任意应用窗口（Win32 `PrintWindow`，后台窗口也可），
  例如截我们自己的 DSH 窗口；比整屏截图更干净；
- `page_screenshot`：**无可见浏览器**截网页（调用本机 Edge/Chrome 的 `--headless=new --screenshot`），
  支持宽高；默认拒绝内网地址；返回同样的图像标记；
- `run_checks`：跑一组验收脚本（项目内 `.py`），按退出码输出 `PASS/FAIL` 汇总——**机器可判的验收**，不信报告自述；
  workflow 模板写 `"require_checks": true` 后，没跑检查/检查失败时 `report` 会被直接拒绝。

在 `forchat/tools/` 新建文件即自动注册，schema 由签名推导：

```python
from ..registry import tool

@tool(description="把文本转成大写", params={"text": "要转换的文本"})
def upper(text: str) -> str:
    return text.upper()
```

> 接入方式：`forchat/tool_bridge.py` 把 registry 里的工具导出成 `{name: (spec, caller)}` 注入 `Runtime`。
> 在 `workflow.json` 的 `tools` 里写工具名即可（校验时会拿"这台机器上当前注册了哪些工具"来比对）：

## 上下文管理（compact）

worker 默认不限轮数，但 `messages` 不会无限涨：每轮请求前，如果超过
`FORCHAT_CONTEXT_MESSAGES`（默认 80 条）或 `FORCHAT_CONTEXT_CHARS`（默认 160k 字符），
就保留 `system + 原始任务 + 最近轮次`，**把中间历史 compact 掉**：

1. 被裁掉的整段对话**归档**到 `compacted/<worker>-<序号>.json`（不丢，可查）；
   默认全留（`FORCHAT_COMPACT_KEEP=0`），设成 N 就只留最近 N 份——留着做优化分析，还是省盘，你定；
2. 在对话里插入一段**摘要**：默认调用模型总结（保留已确认事实/决策/未完成事项），
   模型调用失败或 `FORCHAT_COMPACT_LLM=0` 时退回确定性摘要（谁说了什么、调过哪些工具）；
3. 最近对话保留 `FORCHAT_RECENT_MESSAGES` 条（默认 20，至少 4 条）；
4. 记 `context_compacted` 事件（`summary_source=llm|digest`、归档路径；`--inspect` 能看到）。

摘要本身也计入该 worker 的 token 预算。归档暂时不自动清理，但清理 API 已经备好，给以后前端用：

```python
from forchat.runs import plan_compacted_cleanup, cleanup_compacted
plan_compacted_cleanup(runs_root, keep_latest=3)        # 只看计划
cleanup_compacted(runs_root, keep_latest=3, dry_run=True)
```

## 规模与预算

默认值按"真实项目"给，不按 demo 给：

| 闸 | 默认 | 说明 |
|---|---|---|
| 单 worker tokens | **20M** | 模板里写 `budget.tokens` 覆盖 |
| 单 worker 墙钟 | **4 小时** | `budget.wall_seconds` |
| 整轮 tokens | **1B** | `limits.max_total_tokens` |
| 整轮墙钟 | **7 天** | `limits.max_wall_seconds` / `max_total_seconds` |
| 并发 / worker 数 / 深度 | 2 / 16 / 3 | 按机器与项目调 |

做 5B~10B token 的重型项目：在 `workflow.json` 里显式写
`limits.max_total_tokens`、`limits.max_wall_seconds`，并给每个模板写 `budget.tokens`。

**默认是记账，不是闸**（按"花超一点点没关系"设计）；需要硬闸时显式配置：

- worker 超软预算 → 记 `budget_exceeded`，**继续跑**；
- 整轮超 `max_total_tokens` → 记 `run_budget_exceeded`，**只拒绝新派生**，正在跑的 worker 收尾；
- **硬闸**：worker 模板 `budget.hard_tokens` 或整轮 `limits.max_total_hard_tokens`（0 = 关闭），
  超限直接 `force_stop`（会 kill 模型/工具子进程），记 `budget_hard_stop` 事件、
  `run_finished.budget_hard_stops` 与 `/metrics` 的 `forchat_budget_hard_stops_total`；
- 也可用环境变量 `FORCHAT_HARD_TOKENS` / `FORCHAT_HARD_TOTAL_TOKENS` 临时开；
- 唯一会真正中断的是**墙钟**（`run_expired`）和手动 `stop_worker`；
- `run_finished` 带 `budget_exceeded` 标记，方便事后查"这次超了多少"。

**预算和上下文是两件事**：预算累计的是"每轮 prompt + completion 的总和"；单次请求的上下文大小
由 `FORCHAT_CONTEXT_MESSAGES` / `FORCHAT_CONTEXT_CHARS` 控制（超限裁剪）。1B 级运行还要注意：

- `thinking` 默认**不**逐条落事件，需要时设 `FORCHAT_LOG_THINKING=1`；
- 事件流按 `FORCHAT_EVENT_LOG_MB`（默认 32MB）分段，关键事件 fsync；
- `--prune` 定期清理旧运行目录。


**整 worker 进程化（叶子）**：`FORCHAT_PROCESS_WORKERS=1` 时，`can_spawn` 为空的叶子 worker
整个循环（模型请求 + 工具 + 快照）在子进程里跑；父进程只做调度与事件单写者。
`force_stop` / `stop_worker` 直接 `kill` 子进程——**纯 Python 死循环也能真杀**。
协调型 worker（会 spawn 的）仍在父进程，因为它们的工作是分派与汇总，不是长跑。
子进程通过 `python -m forchat.worker_proc` 启动，事件写回同一 run 目录（跨进程文件锁）。

**资源感知**：每次 `spawn` 的回执、以及不带 `key` 的 `read_state`，都会带上
`剩余总预算 N tokens / 共 M；已建 worker x/y`；宿主侧用 `Runtime.resources()` 拿到同样的结构
（tokens/cost/wall/workers/queue/concurrency）。整轮预算只剩 <20% 时，上下文裁剪会自动收紧并标 `tight`。

**排队与网状结构**（给前端预留的）：

- worker 等并发槽时状态是 `queued`，事件 `worker_queued` 带 `pending` 与 `max_concurrent`；
- `resources()["queue"]` 是当前排队数，`wait_workers` 会把排队中的分支显示成"排队中"；
- `Replay.graph()` / `GET /runs/<id>/graph` 输出 `{nodes, edges}`，前端可以直接拿去画图。

## 技能

`skills/<名字>/SKILL.md`，frontmatter 只有 `name` / `description`，正文是完整指令。

在 workflow 里挂载：`"skills": ["code-graph"]`——worker 初始化时正文拼进它的系统提示。
加载器由 `tool_bridge.build_skill_loader()` 注入，原语层只拿到 `name -> body` 的函数，
不认识 SKILL.md 的格式。技能可以带脚本，用 `run_python` 跑，见 `skills/code-graph/`。

## MCP

`mcp_servers.json` 声明服务端；工具名带 `mcp__<server>__<tool>` 前缀，远端工具走原始参数 JSON（`raw_caller`），因为远端 schema 可能嵌套。只支持 stdio + tools 能力。

服务端崩溃或超时会自动重启一次再重试；生命周期由 CLI 显式管理（不再依赖 `atexit`）。

## 安全护栏（不是沙箱）

`forchat/policy.py` 在工具执行前做**参数级**检查，CLI 默认启用（完整威胁模型与部署隔离清单见
[docs/sandbox.md](docs/sandbox.md)）：

- 路径跑出项目根（`../` 逃逸、绝对路径指向别处）→ 拒绝；
- `.env` / `.git` / 私钥 / 证书后缀 → 拒绝；
- **worker 授权范围（scope）**：`spawn(..., paths=[".worktrees/a"])` 把子 worker 限制在指定目录，
  写、跑脚本、git 路径都必须在范围内；父 worker 不能授予超出自己范围（`spawn_rejected: scope_grant`）；
- `git` 参数里出现的路径也要在范围内（`worktree add ../x` 会被拒）；
- `run_python` 只执行 `.py`，且脚本路径必须在授权范围内。

**进程唯一出口**：所有工具/子进程都经 `forchat/exec.py` 创建（环境脱敏、可选 ACL 沙箱、
取消登记、Job Object 限额）；`test_layering.py` 机械禁止绕开它。

**它挡不住什么（必须知道）**：

- 它不是沙箱：`run_python` 执行的脚本是**任意代码**，进程里想写哪就能写哪——护栏只检查
  模型传进来的参数，拦不住脚本内部自己去越界；
- 没有容器 / cgroup / 只读挂载 / 独立用户，没有网络隔离；
- 读操作只受项目根约束（读别人的 worktree 不产生副作用，故不按 scope 拦）。

已做的两条"降损"措施（不是隔离，但很实用）：

- **子进程环境擦除密钥**：`run_python` / `git` / MCP 服务端继承的环境里会去掉
  `*API_KEY*`、`*SECRET*`、`*TOKEN*` 等变量（`forchat/env_scrub.py`），避免脚本把 key 读走；
- **受限子进程**（`forchat/subproc.py`）：超时（`FORCHAT_SCRIPT_TIMEOUT` 默认 30s /
  `FORCHAT_GIT_TIMEOUT` 默认 60s）、内存上限（`FORCHAT_MEMORY_MB` 默认 512，Windows 用 Job Object、
  POSIX 用 `setrlimit`）、脱敏环境；超限时进程被杀，回执里会说明；
- **覆盖前自动备份**：`write_file` 覆盖已有文件前，把旧内容写到
  `.forchat-backups/<相对路径>`，写错至少能捞回来。

### Windows ACL 写沙箱（可选，`FORCHAT_SANDBOX=acl`）

用 `CreateRestrictedToken` + Low 完整性 + 工作区能力 SID（`S-1-4-x-y`）的 ACL 授权，
把 `run_python` / `git` 子进程的**写**限制在工作区（+ 私有临时目录）；`read-only` 全拒；
任何一步失败都 fail-closed。不需要管理员，也不是容器。机制与边界见
[docs/sandbox-windows.md](docs/sandbox-windows.md)：只限写、读不受限、网络不隔离。

完整隔离（读写 + 网络）仍属于部署层（容器 / VM），不在本仓库范围内。

## 成本核算

引擎不内置任何价格——价目表由你提供，缺价目时成本显示"未知"，**不假装是 0**。

`pricing.json`（或 `FORCHAT_PRICING` 指向的文件）：

```json
{
  "models": {
    "deepseek-chat": {"input_per_m": 0.27, "output_per_m": 1.10}
  }
}
```

单价含义：每百万 token 的美元，**数字请按你的实际合同/官网核对**（示例不是报价）。

接入后：

- 每个 `report.cost_usd` 按 prompt/completion token 分别计价；
- `run_finished` 记 `total_cost_usd`，`--inspect` 摘要多一行 `成本: $0.0123（按价目表估算）`；
- 预算可写 `budget.max_cost_usd`（单 worker）与 `limits.max_total_cost_usd`（整轮），超了和 token 一样熔断。
被拒时返回错误回执给模型，不中断整个运行。

## 失败隔离

- 模型/网络异常只让当前 worker 失败（事件 `worker_error`），整次运行继续；
- **瞬时错误自动重试**：`RetryProvider` 只对超时/连接/限流/5xx 重试，且**只在还没吐出任何分片时**重试
  （已流出去的内容不能重来）；次数用 `FORCHAT_RETRIES` 控制，默认 2；
- **worker 级重试**：模板写 `"retries": N`，worker 因 `reason=error` 失败时会自动重跑（新会话），
  用来兜住 provider 之外的基础设施抖动；业务失败不重试；
- **事件流分文件**：`events.jsonl` 只放控制/文本事件（assistant 文本按轮合并），工具调用/结果全文单独进 `tools.jsonl`（events 里只留 500 字符摘要）；thinking 整轮合并成一条、默认关闭；
- **滚动与耐久**：`events.<n>.jsonl` 超过 `FORCHAT_EVENT_LOG_MB` 自动分段，`report`/`worker_done`/`run_finished` 额外 fsync；append 对 `events.lock` 加**跨进程文件锁**；
- 工具执行抛异常 → 变成错误回执喂给模型，不中断循环；
- **只有墙钟到期才中断 worker**（`run_expired`）；token/成本超预算只记账，兄弟分支照常跑完；
- **协作式叫停**：`stop_worker(worker_id)` 让后台分支在下一个分片/下一轮边界退出，状态 `killed`；
- **硬中断（两层）**：
  - 模型请求：`FORCHAT_HARD_KILL=1` 放进子进程，stop/超时（`FORCHAT_CHUNK_TIMEOUT`）直接 kill；
  - 工具子进程：`force_stop` / `stop_worker` 会同时 kill 该 worker 正在跑的 `run_python`/`git` 子进程；
  - 仍杀不掉的是同进程里的纯 Python 死循环（用墙钟兜底）。
- 响应被 `max_tokens` 截断 → 记 `response_truncated`，半截工具参数不会被硬执行。

provider 的两个硬参数（真模型）：
| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FORCHAT_REQUEST_TIMEOUT` | 180s | 单次请求超时；挂住会以 `worker_error` 失败，不会拖死整次运行 |
| `FORCHAT_MAX_TOKENS` | 8192 | 单次响应上限；thinking 也吃这个额度，太小会截断工具参数 |

`--inspect` 的摘要带一行合计（总 token、总耗时、各状态计数）；有护栏事件时还会多一行
`护栏: tool_denied=… spawn_rejected=… worker_error=…`，长运行先看这行判断是不是"一直在撞墙"。

## worktree 并行（示例）

`examples/workflows/worktree` 演示"一个 root + 多个隔离分支"的最小闭环：

- worktree 统一放在 `<项目根>/.worktrees/<名字>`（已 gitignore），分支名 `feat-<名字>`；
- coder 用 `git` 工具建 worktree、`write_file` 落盘、再 `git cwd=.worktrees/<名字>` 提交；
- root 用 `git worktree list` + `git show --name-only` 核对隔离性，然后 report。

真跑过一次：两个 coder 并行（`wait=false`）各自在 worktree 内用 `run_python(cwd=...)` 跑通测试后提交
（`97ed50b` 统计模块 28 用例 / `63a9409` IO 工具 21 用例），root 用 `git worktree list` 与 `git show` 独立核对，
主工作区未被触碰。

## 串行合并（示例）

`examples/workflows/merge` 是接在 worktree 并行之后的一步：

- 从干净的 master 建 `integration` 分支；
- **一次只合一个**功能分支（`git merge feat-a --no-edit`、再 `git merge feat-b --no-edit`），让每次冲突单独可见；
- 合完核对文件与 log 再 report；不碰 master、不 push，冲突则 `merge --abort` 并写进 `unresolved`。

真跑过一次：`feat-a`（stats.py，28 用例）与 `feat-b`（io_utils.py，21 用例）依次合入 integration
（`43862b6`、`b31baa2`），无冲突；合并后在 integration 上复跑两套测试都是 `OK`，master 未被触碰。

## 用一个真项目验证（示例）

`examples/workflows/microproject` 的产出留在了 `examples/projects/wordcount/`：

- root 派 coder 写 `wordcount.py` + `test_wordcount.py`；
- coder 的 `on_done` hook 自动派生 verifier 复查；
- root 再亲手用 `run_python` 复跑测试、用 `read` 读源码，确认不是 `assert True`。

作者本人独立复跑过（不看模型报告）：

```
python examples/projects/wordcount/test_wordcount.py     -> ALL PASS (15 tests), exit 0
python examples/projects/wordcount/wordcount.py README.md -n 3
py: 24 / forchat: 22 / worker: 20
```

运行结束后 `git status` 只多出未跟踪目录，没有改动任何已有文件。

## 并行开发（示例）

`examples/workflows/parallel`：root 一次响应里发两个 `spawn(wait=false)`，然后 `wait_workers` 汇合，
再亲手跑两份测试，最后 report。

真跑数据（一次 `python -m forchat --workflow examples/workflows/parallel/...`）：

| worker | 耗时 | tokens |
|---|---|---|
| root | 35.5s | 3230 |
| coder A（stats.py + test） | 9.1s | 1297 |
| coder B（io_utils.py + test） | 13.4s | 2088 |

两个 coder 的时间重叠（串行会是 9.1 + 13.4 = 22.5s），根节点只花一次汇合。产出在
`examples/projects/multi/`，两套测试独立复跑都是 `ALL PASS` + 退出码 0。

## 封顶合成样本（capstone）

一次运行里走完"并行隔离开发 → 汇合 → 串行合并 → 复跑"：

```
root
 ├─ spawn(wait=false) ×3  → alpha / beta / gamma，各自 .worktrees/<名字> + feat-<名字>
 ├─ wait_workers          → 汇合三份报告
 ├─ git checkout -b integration
 │   merge feat-alpha    → fast-forward 996adca
 │   merge feat-beta     → 202416a
 │   merge feat-gamma    → 3c1c2b0
 └─ run_python ×3        → 三个测试套件全绿
```

作者本人独立复跑：`test_alpha_utils.py`（19 条断言）、`test_beta_utils.py`、`test_gamma_utils.py`
全部 `ALL PASS`、退出码 0；master 停在运行前的 `7079301` 未被触碰。
运行后残留的临时脚本与 worktree 已清理，仓库只留 master。

## 质量门：故意让它失败

真跑过一次"必须失败"的实验（microproject 流程，要求测试里保留 `assert 1 == 2`）：

- coder 没有粉饰，`report.unresolved` 里写明"测试按设计失败、不要修好"；
- root 也没有宣称完成，unresolved 里写清"若转绿需人工决定"；
- 同一轮里 verifier 因 `APITimeoutError` 失败——**整次运行没有崩**，root 照常收尾（失败隔离生效）；
- 这次实验暴露了一个真问题：hook 派生的 review 失败时父 worker 看不到。已修：`spawn(wait=true)`
  的返回里现在附带"子 worker 的派生：n003-reviewer(reviewer) failed"。

## 规模样本（scale）

`examples/workflows/scale` 把 capstone 从 3 分支放大到 6 分支，`max_concurrent=4` 用于验证限流：

| 指标 | 实测 |
|---|---|
| worker 数 | 7（root + 6 coder） |
| 事件数 | 8114 |
| tokens | 44198 / 400000（11%） |
| 墙钟 | 167.7s（6 个 worker 合计 449.4s，重叠执行） |
| 结果 | 6 分支全部合并、6 个测试套件独立复跑 `ALL PASS`，master 未动 |

同一次运行里护栏触发了 1 次：root 试图让 `run_python` 执行非 `.py` 文件，被
`tool_denied: 只能执行 .py 脚本` 拒绝——说明护栏在真实运行里是在生效的。

## 压力样本（stress）

`examples/workflows/stress`：10 个 coder 一次派出，`max_concurrent=4` 形成排队。

| 指标 | 实测 |
|---|---|
| worker 数 | 11（root + 10） |
| tokens | 57997 / 600000（10%） |
| 墙钟 | 229s（worker 合计 662s，排队后仍重叠执行） |
| 合并 | 10 次 `--no-ff`，无冲突 |
| 复跑 | 10 个测试我独立跑：`ok=10/10` |

**最有价值的部分**：这次真跑里护栏触发了 **6 次**，全是 worker 真实尝试越界：

| worker | 尝试 | 拦截 |
|---|---|---|
| n002 | 写 `D:\tmp\alpha_msg.txt` | 路径越界 |
| n006 | git 操作 `.epsilon_commit_msg.tmp` | 不在授权范围 |
| n009 | 写 `.git/theta-commit-msg.txt` | 不在授权范围（敏感路径） |
| n009 | 写 `.forchat-backups/...` | 不在授权范围 |
| n009 | 反复尝试临时文件 | 全部被拒 |

也就是说：**模型会为了让提交信息更好看，去写 worktree 之外的临时文件**——这正是你担心的"处理到别的范围"，
在 10 分支压力下被 scope 挡住了。

## HTTP/SSE 服务

```powershell
python -m forchat --serve 8000
```

| 接口 | 说明 |
|---|---|
| `GET /health` | 存活 |
| `GET /runs` | 运行列表（同 `--runs`） |
| `POST /runs` | `{"workflow": "minimal", "goal": "..."}` → `202 {"id", "run_dir"}` |
| `GET /runs/<id>` | 状态、worker 数、tokens、最后一份报告、卡死 worker（`stuck`） |
| `GET /runs/<id>/graph` | **运行图**：`{"nodes":[{id,template,depth,status,task,summary,tokens}], "edges":[{from,to}]}`，前端画网状结构用 |
| `GET /runs/<id>/events` | **SSE**：推送事件（带 `id:`），支持 `Last-Event-ID` / `?since=` 断点续传，结束发 `[DONE]` |
| `GET /runs/<id>/artifacts` | 列出附件（name/size/mtime） |
| `GET /runs/<id>/artifacts/<name>` | 下载附件（路径做了越界检查） |
| `GET /metrics` | Prometheus 文本：运行数/在跑数/按状态计数/worker 数/tokens/成本/卡死 worker（带 `FORCHAT_METRICS_TTL` 缓存，默认 5s） |
| `POST /runs/<id>/stop` | 请求停止该运行里所有在跑的 worker（协作式） |
| `POST /runs/<id>/workers/<wid>/stop` | 宿主强停单个 worker（下一个分片/轮次边界退出，记 killed） |
| `POST /runs/<id>/workers/<wid>/replace` | 停掉跑偏/过长的 worker，用 `{"task": "新任务"}` 换任务重开一个新实例 |

鉴权与限流：

- 设了 `FORCHAT_SERVER_TOKEN` 就要求 `Authorization: Bearer <token>`，否则 401；
- 绑定非本机地址**必须**设 token，否则启动直接拒绝；
- 每 IP 每分钟 `FORCHAT_RATE_LIMIT`（默认 120）次，超了 429。

它仍然只是"能用"级别：没有 TLS、没有多租户。

服务端行为：

- 运行结束会从内存表摘掉（长跑服务不会持有所有历史 Runtime），`stop` 对已结束的运行返回 0；
- SSE 会**按段读取**，事件流滚动后的老分段也能推；
- 服务端自己加载 `pricing.json`（和 CLI 一致），通过 HTTP 启动的运行也有成本。

## 续跑

`python -m forchat` 会把整个 workflow 包快照进运行目录（`workflow.json` + `prompts/`），
运行目录因此是自包含的：

- `--inspect <run-dir>`：只读复查（树 + 每个 worker 的消耗与摘要）；
- `--resume <run-dir> "新目标"`：在同一个事件流上继续推进。

续跑语义是「继续推进这个项目」，不是复活死掉的 worker：历史 worker 只读、新 worker 序号接着涨，
根 worker 的系统提示里会带上历史摘要（全文用 `read_worker_report` 查）。

**崩溃恢复（自动）**：`--resume` 现在会从每轮快照恢复整棵树——

- 快照是**追加式**的 `workers/<id>.jsonl`：每轮只追加新增消息（compact/restart 时写一条 `reset`），
  不再整段重写，避免长会话的 O(n²) IO；
- 没有 `worker_done` 的 worker 若有快照，会从断点继续，**深的先恢复**（父的后续轮次才能看到子报告）；
- 根自己中断且快照存在 → 直接续跑根（保住它自己的对话与未完成的决策），不再新起一个根；
- 根没中断 → 起一个新根，历史 worker 作为只读上下文。

**单点续跑**：也可以点名恢复某个 worker（事件追加在同一条流里）：

```powershell
python -m forchat --resume-worker .runs/<run> n002-coder
```

## 学习路径

1. `CONTEXT.md` —— 先对齐词汇，三层不许混
2. `forchat/primitives/types.py` —— WorkerInstance / Budget / Report
3. `forchat/primitives/runtime_worker.py` —— 唯一执行循环：spawn / hook / report
4. `forchat/primitives/workflow.py` —— 配置如何被严格校验
5. `forchat/primitives/replay.py` —— 从事件流重建一次运行
6. `examples/workflows/minimal/` —— 配置怎么拼出结构
