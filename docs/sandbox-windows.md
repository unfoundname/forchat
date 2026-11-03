# Windows ACL 写沙箱（可选）

对标 DSH 的 `windows-acl` 档，用**系统原语**（不是容器、不是虚拟机）把工具子进程的**写**限制在
工作区内。任何一步失败都 fail-closed，绝不退化成普通进程。

## 启用

```powershell
$env:FORCHAT_SANDBOX = "acl"           # 打开
$env:FORCHAT_SANDBOX_MODE = "workspace-write"   # 或 read-only
# 可选：默认工作区 = 工具调用的 cwd
$env:FORCHAT_SANDBOX_WORKSPACE = "D:\Project\ForChat"
```

只对走 `subproc.run` 的工具生效（`run_python`、`git`）。`FORCHAT_SANDBOX` 未设置时行为完全不变。

## 机制

1. 按工作区路径派生能力 SID：`S-1-4-x-y`（SHA-256 取两个 30 位子权威）；
2. 给工作区 + 私有临时目录（`<workspace>/.forchat-sandbox-tmp`）加一条
   能力 SID 的 allow ACE，并显式补当前用户的 ACE（设置 DACL 时继承 ACE 会被剥掉）；
3. 用 `CreateRestrictedToken(WRITE_RESTRICTED|SANDBOX_INERT|DISABLE_MAX_PRIVILEGE)` 造令牌，
   restricting SIDs = `[logon SID, Everyone, 能力 SID]`：
   - 缺 logon SID 或 Everyone，子进程 DLL 初始化会 `0xC0000142`；
   - 写检查只走 restricting SIDs，所以工作区外没有能力 SID 授权的对象写不了；
4. 把能力 SID 的全权 ACE 并进令牌**默认 DACL**（子进程建匿名管道需要，否则 `spawn EPERM`）；
5. 令牌降到 Low 完整性（`S-1-16-4096`），并给授权目录打 Low 标签（`icacls /setintegritylevel`）；
6. `CreateProcessAsUser` 启动子进程，先放进 kill-on-close 的 Job Object，再 resume；
7. 启动器 `python -m forchat.sandbox_win` 由 `subproc.run` 自动拉起；启动器本身不跑用户代码，
   子进程退出码原样返回。启动失败时 stderr 打 `forchat-acl-run: …`、退出码 127，
   父进程把它翻译成"沙箱不可用，命令未执行"。

`read-only` 不给能力 SID，只补用户读 ACE——工作区里也写不了。

## 边界（和 DSH 的 `partial` 一致，必须知道）

- **只限制写**，读不受限：脚本可以读工作区外的可读文件；
- **网络不隔离**；
- NTFS **硬链接**会把工作区路径和外部路径指向同一个文件对象，可绕过；
- 在 restricting SIDs 里的 **Everyone** 有写权的对象（如 `C:\Users\Public`、`NUL`）仍可写；
- 被其他 AppContainer 工具用包 SID ACL 过的目录树，Low 令牌可能读不了；
- 工作区/临时目录的 ACL 与 Low 标签是**持久修改**（standing grant），和 DSH 一样为了复用；
- 不是防恶意的完整边界：真正的读写+网络隔离仍应放在容器/VM。

## 验证

`tests/test_sandbox_win.py`（非 Windows 自动跳过）：

- 工作区写成功、工作区外写被拒（`Permission denied`）；
- `read-only` 下工作区写被拒；
- 授权失败（工作区不可用）时 fail-closed，命令没有在无约束环境执行；
- 关闭沙箱时行为与原来一致。

实测（本机 Windows，无管理员权限）：`in=True out=False`，`read-only` 下 `in=False`。
