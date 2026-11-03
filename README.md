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
│   ├── tools/                  能力工具：read / list_dir / run_python / skill
│   ├── skills/                 技能加载（SKILL.md）
│   ├── mcp.py                  MCP 客户端（stdio + JSON-RPC）
│   └── mcp_bridge.py           MCP 工具 → 工具注册表
├── examples/
│   ├── workflows/minimal/      最小 workflow 包（workflow.json + prompts/）
│   ├── run_demo.py             端到端自检（假 provider，无需 key）
│   ├── mcp_demo_server.py      80 行的 MCP 服务端
│   └── smoke_mcp.py            MCP 链路自检
├── tests/
│   ├── test_primitives.py      原语自检（9 条）
│   └── test_layering.py        依赖方向 / 领域词 / 唯一执行循环
├── skills/                     技能目录（数据）
└── mcp_servers.json            MCP 服务端配置
```

## 跑起来（不需要 key）

```powershell
python examples/run_demo.py        # 假 provider 跑一次完整运行：root → coder → hook 派生 reviewer
python tests/test_primitives.py    # 原语自检
python tests/test_layering.py      # 架构约束
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
  "limits": {"max_concurrent": 2, "max_depth": 3, "max_workers": 8},
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

运行时只给模型六个工具：`spawn`（派子 worker，受 can_spawn/深度/worker 数约束）、`report`（完成信号）、
`read_state` / `write_note`（plan 与笔记，落盘不靠记忆）、`write_artifact`（交付附件）、
`read_worker_report`（父 worker 回看子报告，不用翻事件流）。

`write_artifact` 写出的文件会自动进 `report.artifacts`，模型不必再手抄路径。

**轮数默认不限**：协调型 worker 跑几十上百轮是正常的，真正的闸是预算（tokens / wall_seconds）
和整轮墙钟。需要给单个模板设上限时，在 `workflow.json` 里写 `max_rounds`。

## 能力工具（read / list_dir / run_python / skill）

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
