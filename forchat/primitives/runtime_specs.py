"""运行时工具声明（OpenAI function schema），与执行逻辑分离。"""
from __future__ import annotations

TOOL_SPECS: dict[str, dict] = {
    "spawn": {
        "type": "function",
        "function": {
            "name": "spawn",
            "description": "派一个子 worker 执行子任务。只能派 can_spawn 里允许的模板。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "worker 模板名"},
                    "task": {"type": "string", "description": "交给它的完整任务描述"},
                    "wait": {"type": "boolean", "description": "是否等它跑完再继续，默认 true"},
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "授权给子 worker 的目录（相对项目根），例如 [\".worktrees/a\"]；"
                                       "省略则继承你的范围。只能授予你自己范围内的目录。",
                    },
                },
                "required": ["template", "task"],
            },
        },
    },
    "report": {
        "type": "function",
        "function": {
            "name": "report",
            "description": "提交本次工作的结构化交付物。这是唯一的完成信号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "unresolved": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "contracts_used": {
                        "type": "object",
                        "description": "本次实现所依据的契约版本，例如 {\"api\": 2}；"
                                       "版本与当前契约不一致会被拒绝",
                        "additionalProperties": {"type": "integer"},
                    },
                },
                "required": ["summary"],
            },
        },
    },
    "read_state": {
        "type": "function",
        "function": {
            "name": "read_state",
            "description": "读取当前运行状态（plan、笔记）。key 省略时返回全部。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
            },
        },
    },
    "write_note": {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "写一条状态/笔记，供自己和子 worker 后续读取。用于防止上下文遗忘。",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            },
        },
    },
    "write_artifact": {
        "type": "function",
        "function": {
            "name": "write_artifact",
            "description": "把交付附件（补丁、日志片段、说明）写进本次运行目录，返回相对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "附件名，例如 patch.diff"},
                    "content": {"type": "string", "description": "附件内容"},
                },
                "required": ["name", "content"],
            },
        },
    },
    "read_worker_report": {
        "type": "function",
        "function": {
            "name": "read_worker_report",
            "description": "读取某个 worker 的报告与预算。长运行里回看子 worker 的细节时用它，"
                           "不要去翻整段事件流。",
            "parameters": {
                "type": "object",
                "properties": {"worker_id": {"type": "string", "description": "子 worker 的 id"}},
                "required": ["worker_id"],
            },
        },
    },
    "inspect_worker": {
        "type": "function",
        "function": {
            "name": "inspect_worker",
            "description": "视察一个在跑/卡住的 worker：返回任务、状态、预算、是否 suspect、最近事件。"
                           "只读，不重启也不停止；父 agent 用它判断子 agent 是否真的偏离工作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {"type": "string", "description": "要视察的 worker id"},
                    "tail": {"type": "integer", "description": "返回最近多少条事件，默认 8"},
                },
                "required": ["worker_id"],
            },
        },
    },
    "wait_workers": {
        "type": "function",
        "function": {
            "name": "wait_workers",
            "description": "等待异步派出的子 worker 完成（spawn(wait=false) 之后用它汇合）。"
                           "不传 worker_ids 时等待本 worker 的所有直接子 worker。",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要等的 worker id 列表；省略表示全部直接子 worker",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "最多等多久，默认 600 秒；超时只返回状态与 suspect，不自动重启",
                    },
                },
            },
        },
    },
    "stop_worker": {
        "type": "function",
        "function": {
            "name": "stop_worker",
            "description": "请求停止一个仍在运行的 worker（协作式：它在下一个分片/下一轮边界退出）。",
            "parameters": {
                "type": "object",
                "properties": {"worker_id": {"type": "string"}},
                "required": ["worker_id"],
            },
        },
    },
    "restart": {
        "type": "function",
        "function": {
            "name": "restart",
            "description": "把**你自己**重启：清空当前对话，用同一个 worker 实例、同一模板/范围重新开始"
                           "（事实库保留）。上下文涣散、越跑越差时用它，比继续喂更划算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "重启后的任务；省略则沿用当前任务"},
                },
            },
        },
    },
    "write_fact": {
        "type": "function",
        "function": {
            "name": "write_fact",
            "description": "把一条'事实'写进**你这个 worker 自己的**事实库（落盘长期保存）。"
                           "restart/续跑之后的新 agent 会自动在系统提示里看到这些事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "事实名，例如 '构建命令'"},
                    "content": {"type": "string", "description": "事实内容（尽量短、可验证）"},
                },
                "required": ["name", "content"],
            },
        },
    },
    "read_facts": {
        "type": "function",
        "function": {
            "name": "read_facts",
            "description": "读事实：默认读自己的；也可以读别的 worker 的（协调时用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_id": {"type": "string", "description": "要读谁的；省略则读自己的"},
                },
            },
        },
    },
    "write_contract": {
        "type": "function",
        "function": {
            "name": "write_contract",
            "description": "写一条契约（接口签名/数据格式/命令约定），注入所有 agent 的提示词，"
                           "不会被 compact 抹掉。status=draft 表示还在演进；frozen 表示下一波依赖它；"
                           "deprecated 表示已废弃。可附带可执行 checks；模板开 require_contracts 时 "
                           "report 前必须全部 PASS。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "契约名，例如 'GET /api/videos 响应'"},
                    "content": {"type": "string", "description": "契约内容（字段/类型/示例）"},
                    "checks": {
                        "type": "array",
                        "description": "机器校验脚本列表，格式同 run_checks："
                                       "[{name, script, args?, cwd?}]；约定只能放 .py",
                        "items": {"type": "object"},
                    },
                    "status": {
                        "type": "string",
                        "enum": ["draft", "frozen", "deprecated"],
                        "description": "默认 draft；准备让下一波并行实现时再 frozen",
                    },
                    "change_reason": {"type": "string", "description": "为什么写/改这条契约"},
                    "replace": {"type": "boolean", "description": "确实要改时传 true（会升版本）"},
                },
                "required": ["key", "content"],
            },
        },
    },
    "propose_contract_change": {
        "type": "function",
        "function": {
            "name": "propose_contract_change",
            "description": "提交契约变更请求（任何 worker 可提）：必须带完整新 content、原因和影响切片；"
                           "root 用 decide_contract_change 批准后才会升版本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "reason": {"type": "string"},
                    "content": {"type": "string", "description": "完整的新契约内容"},
                    "checks": {"type": "array", "items": {"type": "object"}},
                    "affected": {"type": "array", "items": {"type": "string"},
                                 "description": "受影响的切片/worker/文件"},
                    "status": {"type": "string", "enum": ["draft", "frozen", "deprecated"]},
                },
                "required": ["key", "reason", "content"],
            },
        },
    },
    "decide_contract_change": {
        "type": "function",
        "function": {
            "name": "decide_contract_change",
            "description": "root 专用：批准/拒绝一条契约变更；批准会升版本并更新 checks。"
                           "拒绝要写 note（原因），让提案方知道下一步。",
            "parameters": {
                "type": "object",
                "properties": {
                    "change_id": {"type": "string"},
                    "approve": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["change_id", "approve"],
            },
        },
    },
    "verify_contract": {
        "type": "function",
        "function": {
            "name": "verify_contract",
            "description": "机器校验冻结契约：重算内容 hash，跑契约里的 checks；返回 PASS/FAIL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "只校验某一条；空 = 全部"},
                },
            },
        },
    },
    "read_contracts": {
        "type": "function",
        "function": {
            "name": "read_contracts",
            "description": "读取当前所有冻结契约。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
}

