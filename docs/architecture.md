# 架构准备（backend-first）

## 目标 / 非目标

**目标**

- 核心是一个**库**：不 print、不读 cwd、不碰网络以外的东西；CLI 只是它的一个消费者。
- 应用层（CLI、将来的 HTTP、未来的 UI）通过**事件流**消费，不感知循环内部。
- 测试不需要 API key：核心逻辑可用假 Provider 全量跑通。

**非目标（现在不做）**

- 不做前端，不做多租户账号体系。
- 不自研完整 MCP 协议（只做 tools 能力 + stdio）。
- 不引入 DI 框架 / 插件沙箱 / 分布式调度——先把缝留出来，不预先实现。

## 分层与依赖规则

```
apps/        CLI（现状）、将来的 HTTP/SSE        ← 只调 runtime
runtime/     装配：Config、ToolRegistry 实例、生命周期
session/     会话状态、历史、持久化接口
providers/   deepseek / openai / fake(测试)      ← 实现 core 的 Provider
tools/       ToolSpec / Executor + local/ + mcp/
skills/      技能加载（已独立）
core/        事件、流式累积器、循环状态机         ← 不 import 任何人
```

| 规则 | 说明 |
|---|---|
| core 只依赖 stdlib | 不 import forchat 内任何模块，只依赖 `contracts.py` |
| tools 不认识 agent/session | 工具只拿到参数，返回值是字符串或结构化结果 |
| providers 只实现协议 | 循环不认识 DeepSeek 的 `reasoning_content` / `extra_body` |
| apps 里没有业务逻辑 | 只做参数解析、渲染事件、退出码 |
| 单向依赖 | 任何反向 import 都是架构事故，用测试卡住 |

## 核心契约（见 `forchat/core/contracts.py`）

- **事件**：`ThinkingDelta` / `TextDelta` / `ToolCallReady` / `ToolResult` / `RoundFinished` / `Done`
- **工具**：`ToolSpec`（name/description/parameters）、`ToolExecutor`（specs + execute）、`ToolSource`（本地、MCP、插件都实现它）
- **Provider**：`stream(model, messages, tools) -> Iterator[chunk]`，只吐原始分片
- **Session**：`messages` + 元数据，可序列化

一次对话在事件流里长这样（渲染层只认这些）：

```
RoundStarted(1) → ThinkingDelta* → TextDelta* → ToolCallReady* → ToolResult*
               → RoundFinished(1) → RoundStarted(2) → TextDelta* → Done
```

## 迁移顺序（每步可运行、可回滚）

| # | 动作 | 验收 |
|---|---|---|
| 1 | `REGISTRY` → `ToolRegistry` 实例，注入到执行器 | 两个实例互不干扰；`--tools` 仍正常 |
| 2 | `agent.run()` 去掉 print，改成 `yield Event` | CLI 渲染函数替换后输出等价；事件序列可断言 |
| 3 | `messages` 提升为 `Session`，支持 `resume` | 同一 session 连问两轮，历史连续 |
| 4 | 抽 `Provider`，DeepSeek 专有字段收进 provider | 加一个 fake provider，全套测试离线跑 |
| 5 | MCP 生命周期从 `atexit` 移到 runtime | 会话结束能优雅关闭子进程 |
| 6 | 工具执行前加 policy（路径、超时、审计） | 越权路径返回错误回执而非抛异常 |

顺序不是随意的：**1 和 2 必须先做**，因为 3~6 都要在"registry 实例 + 事件"的基础上才谈得上。

## 测试策略（对应"启动和测试都慢"）

| 层 | 内容 | 速度 |
|---|---|---|
| 纯函数 | 流式累积器、schema 生成、技能解析 | 毫秒，无 IO |
| 契约 | 假 Provider 喂预设分片 → 断言事件序列 | 毫秒，无网络 |
| 集成 | 真 MCP demo 服务端、真文件读写 | 百毫秒 |
| 冒烟 | 真调 API（可选，`-m live`） | 秒级，默认跳过 |

关键是第二层：**假 Provider 让整个循环可以离线测试**，这是重构的安全网。没有它，后面每一步都要真调模型验证，慢且不稳。

## 现在能定的一个决策

事件流 + registry 实例化，是所有后续解耦的地基。其余（会话、provider、权限、配置）都可以在上面分步叠加，不互相阻塞。
