# CONTEXT

本项目的领域词汇。任何人（包括 agent）读写代码前先对齐这里；词不达意时先改这份文件，再改代码。

## 三层，不许混

| 层 | 是什么 | 不是什么 |
|---|---|---|
| **原语（primitives）** | 执行模型、能力面、记录面、装配面的通用机制 | 不是业务流程，不含任何领域词 |
| **workflow** | 可加载的 JSON 配置包：worker 模板、can_spawn、预算、hook、限制 | 不是代码，不含业务实现 |
| **运行输入** | 用户给出的目标与澄清结果（spec/plan），随每次运行产生 | 不是配置，不写进仓库 |

## 术语

- **Worker 实例（WorkerInstance）**：一次运行中的执行单元。与一个 agent 会话 1:1。主 worker 在结构上没有任何特权，只是恰好配了 `can_spawn`。
- **Worker 模板**：`workflow.json` 里的定义（role、can_spawn、tools、budget、hooks）。运行时按模板实例化出 Worker 实例。
- **spawn**：唯一的拓扑构造动作。受 `can_spawn`、`max_depth`、`max_workers` 三重围栏约束。
- **report**：worker 实例的结构化交付物，同时是**唯一的完成信号**——调用它，本轮即结束。
- **hook**：生命周期挂载点，两类语义不同的东西：
  - `action` hook：确定性动作（snapshot、notify），不派生 worker；
  - `spawn` hook：派生新 worker（"完成后自动派 review"）。
- **plan**：运行时状态（`read_state` / `write_note` 读写），允许随时修改；它存在文件里，不靠对话记忆。
- **EventLog**：append-only 事件流，是"完整存储"的唯一物理形态。会话、重放、复盘都从它读。
- **Replay**：`review()`（人读的流水账）/ `rerun`（读录像重放工具调用，不调模型）。
- **围栏（fence）**：能力限制，优先于事后熔断。递归靠 `can_spawn` 为空来杜绝，不是靠层数。

## 不用这些词

- "流程"：说具体机制（spawn、hook、report），不要用笼统的流程代指。
- "角色"：说 **worker 模板**；模板是数据，不是代码里的类。
- "复刻 B 站"：这是**某次运行的目标**，不是项目的一部分，不进配置、不进代码。
