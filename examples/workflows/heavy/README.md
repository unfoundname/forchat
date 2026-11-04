# heavy：重型项目复刻 workflow

目标不是"生成一个看起来像的页面"，而是把一个真实系统的**一个垂直切片**做成可运行、可验收、可合并的交付物。

## 适合什么

- 复刻一个网站的若干关键页面 + 接口（不追求整站）；
- 把一个已有项目重写到新栈；
- 从规格/竞品出发做 3~6 个功能切片的并行实现。

## 角色

| 模板 | 职责 |
|---|---|
| `root` | 冻结契约、切功能、并行派发、串行合并、最终验收 |
| `recon` | 抓目标站点/文档，产出页面/接口/数据/验收清单 |
| `architect` | 搭骨架 + 写 `checks/`（机器验收），不写业务 |
| `coder` | 在独立 worktree 实现一个切片，测试后提交分支 |
| `reviewer` | coder 完成自动派生，读 diff + 跑测试，独立 PASS/FAIL |
| `integrator` | 把分支按顺序合进 `integration`，每步跑检查 |
| `fixer` | 集成验收失败时做最小修复，最多 3 轮 |

## 跑法（以 B 站网页端切片为例）

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:FORCHAT_PROCESS_WORKERS = "1"    # 叶子 worker 进程化，可硬杀
$env:FORCHAT_SANDBOX = "acl"          # Windows：工具写入限制在工作区（可选）

python -m forchat --workflow examples/workflows/heavy/workflow.json `
  "复刻 B站 网页端的一个可运行切片：首页推荐流 + 视频详情页 + 搜索页；技术栈 React + Vite；"
  "先 recon 出页面/接口清单，冻结契约，搭骨架和 checks，再并行实现 3 个切片并合并验收。"
```

## 阶段（root 按顺序执行）

1. recon：情报落 `artifacts/recon.md`；
2. 冻结 architecture / api / acceptance 契约；
3. architect 搭骨架 + `checks/api_contract.py`、`checks/e2e.py`；
4. 4~6 个 coder 在 `.worktrees/<slice>` 并行实现，reviewer 自动审查；
5. integrator 串行合并到 `integration`；
6. 全量 checks，失败交 fixer，最多 3 轮；
7. report：范围/契约/checks/分支/差距。

## 成本与恢复

- 默认限制：24 workers、并发 4、总预算 100M tokens、墙钟 3 天；
- 重型项目按实际规模调 `limits` 和每个模板的 `budget`；
- 崩溃/中断后用 `--resume .runs/<run>` 继续，已完成的 worker 不会重跑。

## 注意

- recon 不登录、不绕验证码；抓不到就按公开信息做，并在报告里标明假设；
- 契约是硬约束：改契约必须 `replace=true` 升版本；
- "完成"必须由 `checks/` 或 diff 证明，reviewer 会证伪；
- 只复刻结构与交互，不要整站搬运受版权保护的内容。
