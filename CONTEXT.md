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
- **轮数**：默认不限。协调型 worker 跑几十上百轮是常态，真正的闸是预算和整轮墙钟；需要时在模板里写 `max_rounds`。
- **回看**：父 worker 看子 worker 用 `read_worker_report`（读结构化报告），不要翻事件流。
- **汇合**：`spawn(wait=false)` 是异步的，必须用 `wait_workers` 汇合后再做验收/报告；不等就报告等于交空卷。
- **叫停**：`stop_worker` 是协作式的（下一分片/下一轮边界退出），状态记 `killed`；不是硬杀线程。
- **两道预算**：`budget` 管单个 worker，`limits.max_total_tokens` / `max_total_seconds` 管整次运行；总闸跳了拒绝新派生。
- **hook**：生命周期挂载点，两类语义不同的东西：
  - `action` hook：确定性动作（snapshot、notify），不派生 worker；
  - `spawn` hook：派生新 worker（"完成后自动派 review"）。
- **plan**：运行时状态（`read_state` / `write_note` 读写），允许随时修改；它存在文件里，不靠对话记忆。
- **EventLog**：append-only 事件流，是"完整存储"的唯一物理形态。会话、重放、复盘都从它读。
  超过 `FORCHAT_EVENT_LOG_MB` 自动分段（`events.<n>.jsonl`），关键事件 fsync。
- **重试**：provider 层只重试"首片之前"的瞬时错误；worker 层用模板 `retries` 兜基础设施抖动，业务失败不重试。
- **快照（snapshot）**：每轮把 worker 的 `messages`/轮次写到 `workers/<id>.json`；`--resume-worker` 用它从断点继续（同一 run 目录、事件追加）。
- **compact**：超限时保留 system+任务+最近 N 条；被裁对话归档到 `compacted/<worker>-NNN.json`，默认用模型生成摘要（失败退回确定性摘要），记 `context_compacted`（summary_source=llm|digest）。
- **保留策略**：`--prune` 按"最近 N 次 / 最老 N 天"清理 `.runs/`，默认 dry-run；`cleanup_compacted()` 清理 compact 归档（同样默认 dry-run）。
- **状态**：`pending / queued / running / done / failed / killed / interrupted`。
- **Replay**：只读重建一次运行——`review()` 流水账、`tree()` 派生树、`summary()` 每个 worker 的消耗与摘要。
- **续跑（resume）**：同一 run 目录再次运行。历史 worker 只读、序号接着涨，根 worker 的系统提示里带历史摘要。
- **中断（interrupted）**：上次进程崩溃/被杀时没有 `worker_done` 的 worker；`--resume` 会从快照自动恢复整棵树（深的先恢复，根有快照则续跑根），`--resume-worker` 可单点恢复。
- **restart**：worker 自己调用、重启自己：同一个 WorkerInstance、清空对话、事实保留（同模板/范围/任务，可换任务）；上下文涣散时用它。
- **事实（fact）**：跨 agent 共享的硬结论，落盘 `state.json["facts"]`，新 agent 系统提示里自动带（上限 20k 字符）。
- **成本**：按 `pricing.json` 的价目表把 prompt/completion token 换算成美元；没有价目表时明确为"未知"，不记 0。
- **围栏（fence）**：能力限制，优先于事后熔断。递归靠 `can_spawn` 为空来杜绝，不是靠层数。
- **授权范围（scope）**：worker 只能修改的目录列表（相对项目根）。空 = 整个项目根；由父 worker 在 `spawn(paths=...)` 时授予，不能超出自己范围。
- **护栏 ≠ 沙箱**：Policy 只检查工具参数；`run_python` 里的脚本是任意代码。
- **进程出口**：生产代码创建进程只允许走 `forchat/exec.py`（脱敏/沙箱/取消/Job 限额）；其他模块直接 `subprocess.Popen/run` 会被 `test_layering.py` 拦下。
- **ACL 沙箱（Windows，可选）**：`FORCHAT_SANDBOX=acl` 用受限令牌 + Low 完整性 + 能力 SID
  限制子进程**写**工作区；只限写，读/网络不隔离，fail-closed。
- **原子写 / 乐观并发 / 写锁**：`write_file`、`edit_file` 先写临时文件再 `os.replace`，读者看不到半截；整段持有按路径的跨进程写锁；可带 `expected_sha256` / `expected_mtime`，文件被别人改过就拒绝——这是**并发控制**，和沙箱（能不能写）是两个维度。锁是建议锁，`run_python` 里的脚本不遵守。

## 不用这些词

- "流程"：说具体机制（spawn、hook、report），不要用笼统的流程代指。
- "角色"：说 **worker 模板**；模板是数据，不是代码里的类。
- "复刻 B 站"：这是**某次运行的目标**，不是项目的一部分，不进配置、不进代码。
