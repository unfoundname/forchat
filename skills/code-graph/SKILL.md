---
name: code-graph
description: 扫描项目构建模块依赖图，输出 Mermaid 图和循环依赖清单
---

# 代码依赖图

## 何时用

用户问「项目结构」「模块关系」「谁依赖谁」「有没有循环引用」「改动会影响哪些文件」时。

## 首选：跑脚本（确定性优先）

本技能自带分析脚本 `skills/code-graph/scripts/graph.py`（纯 ast，无第三方依赖）。
**先调用 `run_python` 跑它**，不要自己一个个读文件推断：

```
run_python(script="skills/code-graph/scripts/graph.py", args=".")
run_python(script="skills/code-graph/scripts/graph.py", args="forchat --max-nodes=20")
```

脚本输出已经包含：模块数、内部依赖边（带行号）、循环依赖、Mermaid 图。
拿到结果后，你的工作是**解读**，不是重新扫描：

1. 先说结论：几层结构、有没有环、哪个模块被依赖最多
2. 直接引用脚本给的 Mermaid（必要时删掉无关节点，不要重画）
3. 把循环依赖逐条解释：哪条边导致的、怎么拆（推迟到函数内 import / 抽公共模块 / 依赖倒置）

## 兜底：脚本不可用时

`run_python` 不存在、或脚本报错、或目标不是 Python 项目时，才手工来：

1. `list_dir` 确认源码根，忽略 `.git`、`__pycache__`、`.venv`、`node_modules`
2. 逐个 `read`，**只取 import 区**（前 40 行通常够），目标是"边"不是逻辑
3. 边表自己整理，循环用肉眼或手写 DFS
4. 明确告诉用户：这是手工推断，可能漏边

## 输出格式

1. 一句话结论
2. Mermaid 图（节点上限 30，超了先按目录聚合）
3. 循环依赖清单，每条给证据：`a.py:12 -> b.py:5 -> a.py:2`
4. 未能解析的 import

## 约束

- 有脚本就用脚本；**不要**在脚本可用时还去读几十个文件，那是浪费上下文
- 不读第 41 行之后——依赖信息在 import 区就结束了
- 没有证据的边不要画；拿不准的标 `?` 并说明
- 所有路径必须是 `list_dir` 或脚本输出里真实出现过的
