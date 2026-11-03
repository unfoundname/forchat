"""Policy：执行前的确定性护栏。

它不是沙箱。沙箱靠容器 / cgroup / 只读挂载，属于部署层；这里只拦"明显越界"的东西：

- 路径跑出项目根（`../` 逃逸、绝对路径指向别处）
- 敏感文件（.env、.git、私钥、证书）
- run_python 执行 skills/ 与 examples/ 之外的脚本

被拒绝时返回原因字符串，运行时把它当错误回执喂给模型，不中断整个运行。
"""
from __future__ import annotations

import json
from pathlib import Path

DENY_PARTS = {".env", ".git", ".ssh", ".aws", "id_rsa", "id_ed25519"}
DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class Policy:
    def __init__(
        self,
        root: Path | str,
        script_roots: tuple[str, ...] = ("skills", "examples"),
    ) -> None:
        self.root = Path(root).resolve()
        self.script_roots = tuple((self.root / name).resolve() for name in script_roots)

    def check(self, tool: str, raw_arguments: str) -> str | None:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "参数不是合法 JSON"
        if not isinstance(args, dict):
            return "参数必须是 JSON 对象"

        raw_target = args.get("path", args.get("script"))
        if not raw_target:
            return None

        target = Path(str(raw_target)).expanduser()
        resolved = target.resolve() if target.is_absolute() else (self.root / target).resolve()

        if not self._inside(resolved, self.root):
            return f"路径越界：{resolved}"
        if DENY_PARTS & set(resolved.parts):
            return f"敏感路径：{resolved.name}"
        if resolved.suffix.lower() in DENY_SUFFIXES:
            return f"敏感文件：{resolved.name}"

        # cwd 也要在项目根内：否则工具只是"执行位置"越界，同样能读写到外面
        raw_cwd = args.get("cwd")
        if raw_cwd:
            cwd_path = Path(str(raw_cwd)).expanduser()
            resolved_cwd = cwd_path.resolve() if cwd_path.is_absolute() else (self.root / cwd_path).resolve()
            if not self._inside(resolved_cwd, self.root):
                return f"工作目录越界：{resolved_cwd}"

        if tool == "run_python":
            if resolved.suffix != ".py":
                return "只能执行 .py 脚本"
            if not any(self._inside(resolved, base) for base in self.script_roots):
                return "脚本必须位于 skills/ 或 examples/ 下"

        return None

    @staticmethod
    def _inside(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False
