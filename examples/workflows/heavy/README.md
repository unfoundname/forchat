# heavy：重型项目复刻 workflow

**一个 workflow 同时覆盖后端/接口和前端页面复刻。** 目标不是"生成一个看起来像的页面"，而是把一个真实系统的**一个垂直切片**做成可运行、可验收、可合并的交付物。

核心假设：

- **契约是跑出来的，不是一次写全的**：允许缺漏、允许改，但必须版本化、有影响面、禁止静默偏离；
- **前端不是 root 看截图拍板**：UI 切片必须过独立 `visual_critic` + `checks/visual.py` 的循环，root 只有调度权。

## 适合什么

- 复刻一个网站的若干关键页面 + 接口（不追求整站）；
- 把一个已有项目重写到新栈；
- 从规格/竞品出发做多个功能切片的并行实现；
- 前端复刻（参考图 → 实现 → critic 对比 → fixer 循环 → 机器 checks）。

## 角色

| 模板 | 职责 |
|---|---|
| `root` | 侦察排期、契约决策、并行派发、视觉循环调度、合并验收；不判定视觉 |
| `recon` | 抓页面/文档/参考截图，产出情报 + open questions |
| `spike` | 行走骨架：打通一条最小真实链路，从代码反推契约草案 |
| `architect` | 骨架 + stubs + `checks/`（含 `checks/visual.py`）+ open questions，不写业务 |
| `contract_doctor` | 冻结前扫接口漂移/缺口，只提交变更请求 |
| `coder` | 在独立 worktree 实现一个切片；缺契约走变更协议；UI 切片先自检截图 |
| `reviewer` | 自动派生；读 diff + 跑测试 + 核对契约版本，独立 PASS/FAIL |
| `visual_critic` | UI 切片独立对比参考/实现，PASS/FAIL + blockers；不修改代码 |
| `integrator` | 按序合并到 `integration`，每步跑检查 |
| `fixer` | 集成/视觉缺陷最小修复 |

## 阶段

1. **recon**：页面/接口/数据/验收 + 参考截图（1366x900、390x844）+ open questions；
2. **行走骨架（spike）**：一个页面 + 一个接口 + 测试，从真实代码反推契约；
3. **契约 v1（draft）**：只冻稳定核心；architect 按契约搭骨架/stubs/checks；
4. **波次 1**：2~3 个 coder 并行；缺契约走 `propose_contract_change` → root `decide_contract_change`；
5. **契约收敛**：contract_doctor 扫漂移；root 批准变更、升版本、更新 checks，升级为 `frozen`；
6. **波次 2**：在 frozen 契约上并行剩余切片；
7. **UI 视觉循环**（每个 UI 切片）：`visual_critic` 对比参考图与实现 → FAIL 交 `fixer` → 重新截图，
   最多 5 轮；只有 `VERDICT: PASS` 且 `blockers=0` 才算通过；
8. **合并 + 验收**：integrator 串行合并；全量 `run_checks`（含 `checks/visual.py`）；fixer ≤3 轮；
9. **交付**：契约矩阵、视觉循环历史、checks 证据、open questions 关闭情况、差距。

## 跑法（B 站网页端切片）

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:FORCHAT_PROCESS_WORKERS = "1"
$env:FORCHAT_VISION = "1"                 # UI 切片：让 read_image/截图进多模态
$env:FORCHAT_WEB_ALLOW_PRIVATE = "1"      # UI 切片：允许截 localhost dev server
$env:FORCHAT_SANDBOX = "acl"              # Windows 可选

python -m forchat --workflow examples/workflows/heavy/workflow.json `
  "复刻 B站 网页端的一个可运行切片：首页推荐流 + 视频详情页 + 搜索页；技术栈 React + Vite；"
  "先 recon 截参考图，再行走骨架，契约先 draft 后 frozen，分两波实现；"
  "每个 UI 切片都要跑 visual_critic 循环并过 checks/visual.py。"
```

纯后端任务同样用这个 workflow：recon 不截参考图、architect 不写 `checks/visual.py`、
root 在报告里说明"无 UI，跳过视觉循环"即可。

## 前端复刻的判定

- 参考图必须真实（目标页或用户提供），不许自己画；
- `visual_critic` 判定：`score >= 85` 且 `blockers == 0` 才 PASS；
- 布局/导航/主内容区/响应式崩版 = blocker；
- 动态内容/字体/素材差异可 non-blocking，但必须列出；
- root 不能用"我看差不多"覆盖 FAIL，也不能让 coder 自评；
- 5 轮仍 FAIL 就如实报"未收敛"。

## 成本与恢复

- 默认：24 workers、并发 4、总预算 100M tokens、墙钟 3 天；
- 重型项目按规模调 `limits` 与各模板 `budget`；
- 崩溃/中断用 `--resume .runs/<run>` 继续，已完成 worker 不重跑。

## 注意

- recon 不登录、不绕验证码；抓不到就按公开信息做并标明假设；
- 契约改必须 `propose_contract_change` + root 批准；reviewer 会查"声明的版本 vs 当前版本"；
- "完成"必须由 `checks/` 或 diff 证明；
- 只复刻结构与交互，不整站搬运受版权保护的内容。
