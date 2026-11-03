"""Policy：执行前的确定性护栏。

**它不是沙箱。** 沙箱靠容器 / cgroup / 只读挂载 / 独立用户，属于部署层；
这里只做参数级检查，挡的是"模型主动声明越界路径"，挡不了脚本内部自己去越界（run_python 的代码想写哪都能写）。

它拦这些：
- 路径跑出项目根（`../` 逃逸、绝对路径指向别处）
- 路径跑出 worker 的授权范围（scope，见 spawn 的 paths 参数）
- 敏感文件（.env、.git、私钥、证书）
- git 参数里出现的越界路径

被拒绝时返回原因字符串，运行时把它当错误回执喂给模型，不中断整个运行。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DENY_PARTS = {".env", ".git", ".ssh", ".aws", "id_rsa", "id_ed25519"}
DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class Policy:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def check(self, tool: str, raw_arguments: str, scope: tuple[str, ...] | list[str] = ()) -> str | None:
        """参数级护栏。scope 非空时，只有这些目录下的路径允许**修改**；
        只读工具（read/list_dir）仍只受项目根约束。

        注意：这仍然不是沙箱——它只检查参数，不阻止 run_python 里的脚本自己越界。
        """
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "参数不是合法 JSON"
        if not isinstance(args, dict):
            return "参数必须是 JSON 对象"

        mutable_tools = {"write_file", "run_python", "git"}
        scope_roots = [(self.root / str(s)).resolve() for s in scope]
        if tool in mutable_tools and scope_roots:
            allowed_roots = scope_roots
        else:
            allowed_roots = [self.root]

        raw_cwd = args.get("cwd")
        base = self.root
        if raw_cwd:
            cwd_path = Path(str(raw_cwd)).expanduser()
            resolved_cwd = cwd_path.resolve() if cwd_path.is_absolute() else (self.root / cwd_path).resolve()
            if not self._inside(resolved_cwd, self.root):
                return f"工作目录越界：{resolved_cwd}"
            if tool in mutable_tools and scope_roots and not any(
                self._inside(resolved_cwd, base_root) for base_root in scope_roots
            ):
                return f"工作目录不在授权范围：{resolved_cwd}"
            base = resolved_cwd

        raw_target = args.get("path", args.get("script"))
        if raw_target is None:
            # 没有路径参数时也要看 git 的参数里有没有可疑路径
            if tool == "git":
                return self._check_git_paths(str(args.get("args", "")), allowed_roots)
            return None

        target = Path(str(raw_target)).expanduser()
        resolved = target.resolve() if target.is_absolute() else (base / target).resolve()

        if not self._inside(resolved, self.root):
            return f"路径越界：{resolved}"
        if not any(self._inside(resolved, base_root) for base_root in allowed_roots):
            return f"路径不在授权范围：{resolved}"
        if DENY_PARTS & set(resolved.parts):
            return f"敏感路径：{resolved.name}"
        if resolved.suffix.lower() in DENY_SUFFIXES:
            return f"敏感文件：{resolved.name}"

        if tool == "run_python":
            if resolved.suffix != ".py":
                return "只能执行 .py 脚本"
        if tool == "git":
            git_check = self._check_git_paths(str(args.get("args", "")), allowed_roots)
            if git_check:
                return git_check

        return None

    def _check_git_paths(self, args: str, allowed_roots: list[Path]) -> str | None:
        """git 的参数里如果出现"像路径"的词，它也必须落在授权范围内。"""
        for token in args.split():
            if not self._looks_like_path(token):
                continue
            candidate = Path(token.replace('"', "").replace("'", "")).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
            if not self._inside(resolved, self.root):
                return f"git 路径越界：{resolved}"
            if not any(self._inside(resolved, base_root) for base_root in allowed_roots):
                return f"git 路径不在授权范围：{resolved}"
        return None

    @staticmethod
    def _looks_like_path(token: str) -> bool:
        if not token or token.startswith("-"):
            return False
        if token.startswith((".", "/", "~")) or ".." in token:
            return True
        return bool(re.match(r"^[A-Za-z]:[\\/]", token))

    @staticmethod
    def _inside(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False
