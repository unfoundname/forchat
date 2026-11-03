"""ForChat —— 一个最小的 agent 基座示例。

分层：
    client.py    模型客户端与配置
    registry.py  工具注册 / schema 生成 / 统一执行
    tools/       具体工具（放一个文件就自动注册）
    skills/      技能加载器（skills/ 目录下的 SKILL.md）
    agent.py     主循环：流式解析 + 工具回填
"""
__version__ = "0.1.0"
