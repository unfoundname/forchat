# ForChat

一个 workflow 原语引擎：**原语（primitives）→ 可加载的 workflow 配置 → 一次运行**。
不含任何业务；"复刻某某"是运行输入，不是代码。

## 目录

```
ForChat/
├── AGENTS.md                   给 agent 的工作规则（四条硬规则）
├── CONTEXT.md                  领域词汇表：三层、worker、fence、report…
├── docs/decisions/             ADR
├── forchat/
│   ├── __main__.py             CLI：只装配，不执行
│   ├── primitives/             ★ 原语层（唯一执行循环在这里）
│   │   ├── types.py            WorkerInstance / Budget / Report
│   │   ├── store.py            EventLog：append-only 事件流
│   │   ├── scheduler.py        并发准入（等待型子 worker 内联，避免死锁）
│   │   ├── hooks.py            action / spawn 两类 hook
│   │   ├── workflow.py         workflow.json 严格加载与校验
│   │   ├── runtime.py          ★ Runtime：spawn / report / read_state / write_note
│   │   └── replay.py           review（人读）/ tree / tool_calls
│   ├── providers/              base(Protocol) · fake(离线) · deepseek(唯一用 SDK)
│   ├── tools/                  能力工具：read / list_dir / run_python / write_file / git
│   ├── skills/                 技能加载（SKILL.md）
│   ├── mcp.py                  MCP 客户端（stdio + JSON-RPC）
│   └── mcp_bridge.py           MCP 工具 → 工具注册表
├── examples/
│   ├── workflows/minimal/      最小 workflow 包（workflow.json + prompts/）
│   ├── workflows/delivery/     交付示例：coder 落盘 → hook 派生 reviewer → root 用 git 核对
│   ├── workflows/worktree/     worktree 并行示例：两个 coder 各建 worktree + 分支 + 提交
│   ├── workflows/merge/        串行合并示例：把多个 feat 分支合进 integration 分支
│   ├── workflows/capstone/     封顶样本：3 分支并行 → 汇合 → 串行合并 → 复跑
│   ├── workflows/microproject/ 真项目示例：写代码 + 测试 + 自动验收
│   ├── workflows/parallel/     并行示例：两个 coder 同时开工，wait_workers 汇合
│   └── projects/               microproject / parallel 的真实产出（真跑通过）
│   ├── run_demo.py             端到端自检（假 provider，无需 key）
│   ├── mcp_demo_server.py      80 行的 MCP 服务端
│   └── smoke_mcp.py            MCP 链路自检
├── tests/
│   ├── run_all.py              一条命令跑全部本地检查（三个测试文件 + demo + MCP）
│   ├── _support.py             共享夹具：MINIMAL、tmpdir、用例执行器
│   ├── test_primitives.py      原语：预算/围栏/事件流/续跑/并发/报告（34 条）
│   ├── test_tools.py           工具与安全：policy/provider/registry 工具（10 条）
│   ├── test_cli.py             CLI：--check 退出码（1 条）
│   └── test_layering.py        依赖方向 / 领域词 / 唯一执行循环
├── skills/                     技能目录（数据）
└── mcp_servers.json            MCP 服务端配置
```

## 跑起来（不需要 key）

```powershell
python tests/run_all.py            # 全部本地检查（三个测试文件 + 分层 + demo + MCP，推荐）
python tests/test_primitives.py    # 原语：预算/围栏/事件流/续跑/并发
python tests/test_tools.py         # 工具与安全：policy / provider / registry
python tests/test_cli.py           # CLI：--check 退出码
python tests/test_layering.py      # 架构约束
python examples/run_demo.py        # 假 provider 跑一次完整运行：root → coder → hook 派生 reviewer
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
python -m forchat --resume .runs/<run> "继续推进"                    # 在同一个运行目录上续跑
```

## 三层

| 层 | 是什么 | 放哪 |
|---|---|---|
| 原语 | 执行模型 / 能力面 / 记录面 / 装配面 | `forchat/primitives/` |
| workflow | 可加载配置：worker 模板、can_spawn、预算、hook、限制 | `examples/workflows/<name>/workflow.json` |
| 运行输入 | 用户目标与澄清结果（spec/plan） | 运行时状态，不进仓库 |

## 示例一览

六个 workflow 包各自验证一组原语，互相不重复（prompt 也几乎没有公共模板——审计过，唯一重复的行是 `## 工作方式` 这个小标题）：

| 包 | 演示什么 | 主要原语 |
|---|---|---|
| `minimal` | 最小闭环：root → coder → hook 派生 reviewer | spawn / hook / report |
| `delivery` | 真落盘 + git 核对 | write_file / git |
| `microproject` | 写代码 + 写测试 + 自动验收 | run_python / read |
| `parallel` | 两个 coder 并行后汇合 | spawn(wait=false) / wait_workers |
| `worktree` | 隔离分支开发 + worktree 内自测 | git worktree / run_python(cwd) |
| `merge` | 串行合并多个功能分支 | git merge |
| `capstone` | 封顶：3 分支并行 → 汇合 → 串行合并 → 复跑 | spawn(wait=false) / wait_workers / git merge |

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
| `max_total_tokens` | 1,000,000 | 整轮 token 总闸；超了所有 worker 收尾、拒绝新派生 |
| `max_total_seconds` | 3600 | 与 `max_wall_seconds` 取小 |

`run_finished` 事件里会记 `total_tokens` 与 `budget_limit`，`--inspect` 能看到。

运行时只给模型七个工具：`spawn`（派子 worker，受 can_spawn/深度/worker 数约束）、`report`（完成信号）、
`read_state` / `write_note`（plan 与笔记，落盘不靠记忆）、`write_artifact`（交付附件）、
`read_worker_report`（父 worker 回看子报告）、`wait_workers`（汇合 `spawn(wait=false)` 的异步分支）。

`write_artifact` 写出的文件会自动进 `report.artifacts`，模型不必再手抄路径。

**轮数默认不限**：协调型 worker 跑几十上百轮是正常的，真正的闸是预算（tokens / wall_seconds）
和整轮墙钟。需要给单个模板设上限时，在 `workflow.json` 里写 `max_rounds`。

## 能力工具（read / list_dir / run_python / write_file / git）

- `read` / `list_dir`：读文件与目录；
- `run_python`：执行脚本，支持 `cwd`（worktree 里跑测试就靠它）；
- `write_file`：把内容真正落盘（路径安全由 policy 统一拦）；
- `git`：**子命令白名单**（status/diff/log/add/commit/branch/worktree/merge/…），不是裸 shell。

在 `forchat/tools/` 新建文件即自动注册，schema 由签名推导：

```python
from ..registry import tool

@tool(description="把文本转成大写", params={"text": "要转换的文本"})
def upper(text: str) -> str:
    return text.upper()
```

> 接入方式：`forchat/tool_bridge.py` 把 registry 里的工具导出成 `{name: (spec, caller)}` 注入 `Runtime`。
> 在 `workflow.json` 的 `tools` 里写工具名即可（校验时会拿"这台机器上当前注册了哪些工具"来比对）：

## 技能

`skills/<名字>/SKILL.md`，frontmatter 只有 `name` / `description`，正文是完整指令。

在 workflow 里挂载：`"skills": ["code-graph"]`——worker 初始化时正文拼进它的系统提示。
加载器由 `tool_bridge.build_skill_loader()` 注入，原语层只拿到 `name -> body` 的函数，
不认识 SKILL.md 的格式。技能可以带脚本，用 `run_python` 跑，见 `skills/code-graph/`。

## MCP

`mcp_servers.json` 声明服务端；工具名带 `mcp__<server>__<tool>` 前缀，远端工具走原始参数 JSON（`raw_caller`），因为远端 schema 可能嵌套。只支持 stdio + tools 能力。

服务端崩溃或超时会自动重启一次再重试；生命周期由 CLI 显式管理（不再依赖 `atexit`）。

## 安全护栏

`forchat/policy.py` 在工具执行前做确定性检查，CLI 默认启用：

- 路径跑出项目根（`../` 逃逸、绝对路径指向别处）直接拒绝
- `.env` / `.git` / 私钥 / 证书后缀直接拒绝
- `run_python` 只能执行 `skills/` 与 `examples/` 下的脚本

它**不是沙箱**：不防任意代码执行，真正的隔离（容器、cgroup、只读挂载）属于部署层。
被拒时返回错误回执给模型，不中断整个运行。

## 失败隔离

- 模型/网络异常只让当前 worker 失败（事件 `worker_error`），整次运行继续；
- 工具执行抛异常 → 变成错误回执喂给模型，不中断循环；
- 预算/墙钟耗尽 → 该 worker 标记 `failed`，兄弟分支照常跑完；
- 响应被 `max_tokens` 截断 → 记 `response_truncated`，半截工具参数不会被硬执行。

provider 的两个硬参数（真模型）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FORCHAT_REQUEST_TIMEOUT` | 180s | 单次请求超时；挂住会以 `worker_error` 失败，不会拖死整次运行 |
| `FORCHAT_MAX_TOKENS` | 8192 | 单次响应上限；thinking 也吃这个额度，太小会截断工具参数 |

`--inspect` 的摘要带一行合计（总 token、总耗时、各状态计数）。

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

## 续跑

`python -m forchat` 会把整个 workflow 包快照进运行目录（`workflow.json` + `prompts/`），
运行目录因此是自包含的：

- `--inspect <run-dir>`：只读复查（树 + 每个 worker 的消耗与摘要）；
- `--resume <run-dir> "新目标"`：在同一个事件流上继续推进。

续跑语义是「继续推进这个项目」，不是复活死掉的 worker：历史 worker 只读、新 worker 序号接着涨，
根 worker 的系统提示里会带上历史摘要（全文用 `read_worker_report` 查）。

## 学习路径

1. `CONTEXT.md` —— 先对齐词汇，三层不许混
2. `forchat/primitives/types.py` —— WorkerInstance / Budget / Report
3. `forchat/primitives/runtime.py` —— 唯一执行循环：spawn / hook / report
4. `forchat/primitives/workflow.py` —— 配置如何被严格校验
5. `forchat/primitives/replay.py` —— 从事件流重建一次运行
6. `examples/workflows/minimal/` —— 配置怎么拼出结构
