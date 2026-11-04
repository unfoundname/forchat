# heavy：重型项目复刻 workflow

目标不是"生成一个看起来像的页面"，而是把一个真实系统的**一个垂直切片**做成可运行、可验收、可合并的交付物。

核心假设：**契约是跑出来的，不是一次写全的**。允许缺漏、允许改，但必须版本化、有影响面、禁止静默偏离。

## 适合什么

- 复刻一个网站的若干关键页面 + 接口（不追求整站）；
- 把一个已有项目重写到新栈；
- 从规格/竞品出发做多个功能切片的并行实现。

## 角色

| 模板 | 职责 |
|---|---|
| `root` | 侦察排期、契约决策、并行派发、合并验收 |
| `recon` | 抓页面/文档，产出情报 + open questions |
| `spike` | 行走骨架：打通一条最小真实链路，从代码反推契约草案 |
| `architect` | 骨架 + stubs + `checks/` + open questions，不写业务 |
| `contract_doctor` | 冻结前扫接口漂移/缺口，只提交变更请求 |
| `coder` | 在独立 worktree 实现一个切片；缺契约走变更协议 |
| `reviewer` | 自动派生；读 diff + 跑测试 + 核对契约版本，独立 PASS/FAIL |
| `integrator` | 按序合并到 `integration`，每步跑检查 |
| `fixer` | 集成失败最小修复，最多 3 轮 |

## 阶段

1. **recon**：页面/接口/数据/验收 + open questions；
2. **行走骨架（spike）**：一个页面 + 一个接口 + 测试，从真实代码反推契约；
3. **契约 v1（draft）**：只冻稳定核心；architect 按契约搭骨架/stubs/checks；
4. **波次 1**：2~3 个 coder 并行；缺契约走 `propose_contract_change` → root `decide_contract_change`；
5. **契约收敛**：contract_doctor 扫漂移；root 批准变更、升版本、更新 checks，升级为 `frozen`；
6. **波次 2**：在 frozen 契约上并行剩余切片；
7. **合并 + 验收**：integrator 串行合并；全量 checks；fixer ≤3 轮；
8. **交付**：契约矩阵、checks 证据、open questions 关闭情况、差距。

## 跑法（以 B 站网页端切片为例）

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:FORCHAT_PROCESS_WORKERS = "1"
$env:FORCHAT_SANDBOX = "acl"          # Windows 可选

python -m forchat --workflow examples/workflows/heavy/workflow.json `
  "复刻 B站 网页端的一个可运行切片：首页推荐流 + 视频详情页 + 搜索页；技术栈 React + Vite；"
  "先 recon，再行走骨架，契约先 draft 后 frozen，分两波并行实现并合并验收。"
```

## 成本与恢复

- 默认：24 workers、并发 4、总预算 100M tokens、墙钟 3 天；
- 重型项目按规模调 `limits` 与各模板 `budget`；
- 崩溃/中断用 `--resume .runs/<run>` 继续，已完成 worker 不重跑。

## 注意

- recon 不登录、不绕验证码；抓不到就按公开信息做并标明假设；
- 契约改必须 `propose_contract_change` + root 批准；reviewer 会查"声明的版本 vs 当前版本"；
- "完成"必须由 `checks/` 或 diff 证明；
- 只复刻结构与交互，不整站搬运受版权保护的内容。
