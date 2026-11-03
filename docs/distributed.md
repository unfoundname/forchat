# 分布式执行（共享目录队列）

目标：把**叶子 worker** 的完整循环放到另一台机器/另一个进程里跑；coordinator 只负责调度、预算、结果汇总。
默认仍是本地线程/子进程，只有显式配置才启用远程。

## 拓扑

```
coordinator (forchat)
  └── run_dir/remote/tasks/<worker-id>.json     ← 提交任务
remote agent (forchat.remote_agent) x N
  ├── 原子 rename 租约：tasks/<id>.json → leases/<id>.<agent>.json
  ├── 起 python -m forchat.worker_proc 执行（与本地进程模式同一入口）
  ├── 每 5s 刷 lease mtime（心跳）
  └── results/<id>.json                         ← 回传结果
coordinator 轮询 results/<id>.json，更新 WorkerInstance
```

- 超时租约（默认 120s）由 agent 的 `requeue_stale()` 放回 tasks；
- 取消：coordinator 写 `cancels/<id>.cancel`，agent 看到后 kill 子进程；
- worker 的事件仍然写共享 run 目录（跨进程文件锁）。

## 启用

共享文件系统（NFS/SMB/本机目录）上放一个 queue 目录，再让 coordinator 指过去：

```powershell
$env:FORCHAT_REMOTE_DIR = "\\shared\forchat\queue"
python -m forchat --workflow examples/workflows/delivery/workflow.json "目标"
```

在每台执行机上跑 agent（代码版本、Python 环境、依赖要一致）：

```powershell
$env:FORCHAT_CHILD_SCRIPT = ""      # 真模型不需要；测试用脚本时才有
python -m forchat.remote_agent --queue "\\shared\forchat\queue" --poll 0.5
```

`--once` 表示执行一个任务后退出，适合 cron/一次性机器。

## 注意（部署边界）

- **这是信任边界**：能写 queue 目录的人就能让 agent 执行任意 worker 代码。只放在受信网络/专用账户，
  不要把 queue 暴露给不可信用户；agent 上不要放比 worker 需要更多的密钥。
- `run_dir` 也必须对 agent 可读写（事件、facts、结果都要共享）；不同机器上的路径必须一致或可映射。
- 跨主机**没有强杀**：取消是协作式的（agent 收到 cancel 后 kill 本地子进程）；agent 自己挂掉时，
  租约超时后任务会被重新派发，原进程可能成为孤儿，需要部署层兜底（systemd/cgroup 清理）。
- 共享文件系统的一致性/锁语义由 NFS/SMB 负责；`EVENT_LOG` 的文件锁在部分网络文件系统上不可靠，
  大规模部署建议把 run_dir 放在同一台存储节点，或后续换 HTTP 队列。
- 模型 key 在执行机上（agent 继承环境）；coordinator 不需要替远程 worker 保管 key。

## 已验证

`tests/test_remote.py` 3 条（本机两个进程模拟跨主机）：

1. 队列租约 + agent 执行 + 结果回传；
2. Runtime 把叶子 worker 投给远程 agent，父进程拿到报告；
3. 远程 worker 被 stop → cancel 文件 → agent kill 子进程 → 父进程记 `killed`。
