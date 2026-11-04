# frontend：可迭代的前端复刻 workflow

针对"复刻页面"而不是"生成一个静态 HTML"。核心是**独立视觉裁判 + 机器验收 + 修复循环**，root 没有"我觉得可以了"的权力。

## 能不能保证 100%？

不能保证像素级 100%（动态内容、字体、登录态、动画、反爬都会造成差异）。能保证的是：

- **100% 的声明清单**：契约里的每个页面/视口/必现文案/关键区域，都必须有检查或显式豁免；
- **可测量的收敛**：每轮有 `image_diff` 差异像素、`checks/visual.py` 结果、critic 分数；
- **独立判定**：root 不能自己看截图拍板；PASS 只能来自 visual_critic + 机器 checks。

## 角色

| 模板 | 职责 |
|---|---|
| `root` | 驱动循环、记录轮次、最终报告；不判定视觉 |
| `recon` | 截参考图（1366x900 / 390x844）、提取必现文案与设计 token |
| `architect` | 前端骨架 + dev server + `checks/visual.py` + `docs/surface.json` |
| `ui_coder` | 实现页面，自测截图，提交可运行实现 |
| `visual_critic` | 独立对比参考/实现，按 rubric 给 PASS/FAIL + blocking defects |
| `fixer` | 按缺陷清单最小修复，重新截图对比 |

## 循环

```
recon 参考基线
   ↓
契约（design/page/acceptance）→ architect 骨架 + checks
   ↓
ui_coder 实现
   ↓
visual_critic 对比 ──FAIL──> fixer 修复 ──┐
   ↑                                      │
   └──────────── 重新截图/对比 ←──────────┘
   │ PASS
   ↓
run_checks + check_contract_coverage → report
```

- 最多 5 轮；5 轮仍 FAIL 就如实报告"未收敛"，不许 root 自己宣布通过；
- critic 判定：`score >= 85` 且 `blockers == 0` 才 PASS；
- 布局/导航/主内容区/响应式崩版 = blocker。

## 跑法

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
$env:FORCHAT_VISION = "1"                 # 让 read_image / 截图进入多模态消息
$env:FORCHAT_WEB_ALLOW_PRIVATE = "1"      # 允许截 localhost 的实现页面
$env:FORCHAT_PROCESS_WORKERS = "1"

python -m forchat --workflow examples/workflows/frontend/workflow.json `
  "复刻目标页：http://<target>/ ；实现放到 frontend/，Vite + React；"
  "先用 recon 截参考图，architect 搭骨架和 checks/visual.py，再实现并进入视觉循环。"
```

## 机器验收

`checks/visual.py` 由 architect 生成，至少包含：

- 无头浏览器截实现图（同视口）；
- 参考图 vs 实现图 `image_diff` 差异像素比例；
- 必现文案缺失列表；
- 达标退出 0，否则非 0。

root 模板开了 `require_checks` 与 `require_coverage`，最终 report 前会被这两个门禁检查。

## 注意

- 参考图必须真实（目标页截图或用户提供），不许自己画；
- 动态内容/字体/图片素材差异要列入 exceptions/non-blocking，不能静默忽略；
- 只复刻契约声明的页面与视口，不扩范围；
- `page_screenshot` / `page_text` 默认拦内网；本地 dev server 必须显式开
  `FORCHAT_WEB_ALLOW_PRIVATE=1`。
