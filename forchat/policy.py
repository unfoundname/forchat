"""Policy：执行前的确定性护栏。

**它不是沙箱。** 沙箱靠容器 / cgroup / 只读挂载 / 独立用户，属于部署层；
这里只做参数级检查，挡的是"模型主动声明越界路径"，挡不了脚本内部自己去越界（run_python 的代码想写哪都能写）。

白名单式：只有 `READONLY_TOOLS` 里的工具按"只读"处理；其余工具**默认受限**，
其参数里的路径必须落在项目根（以及 worker 的 scope）内。新工具忘了登记也不会自动放行——
它要么没有路径参数（不受影响），要么就按受限工具检查。

它拦这些：
- 路径跑出项目根（`../` 逃逸、绝对路径指向别处、`..\\ForChat-evil` 前缀绕过）
- 路径跑出 worker 的授权范围（scope）
- 敏感文件（.env、.git、私钥、证书）
- git 参数里出现的越界路径
- run_checks 里每个 check 的 script/cwd
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DENY_PARTS = {".env", ".git", ".ssh", ".aws", "id_rsa", "id_ed25519"}
DENY_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

READONLY_TOOLS = {
    "read", "list_dir", "grep", "glob",
    "read_state", "read_worker_report", "read_facts", "read_contracts", "inspect_worker",
    "web_fetch", "web_search", "screenshot", "window_screenshot", "page_screenshot",
    "page_text", "read_image", "image_diff", "image_palette", "page_outline",
}

PATH_KEYS = ("path", "script", "cwd", "file", "dir", "workdir")


class Policy:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def check(self, tool: str, raw_arguments: str, scope: tuple[str, ...] | list[str] = ()) -> str | None:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return "参数不是合法 JSON"
        if not isinstance(args, dict):
            return "参数必须是 JSON 对象"

        restricted = tool not in READONLY_TOOLS
        scope_roots = [(self.root / str(s)).resolve() for s in scope]
        allowed_roots = scope_roots if (restricted and scope_roots) else [self.root]

        base = self._resolve(args.get("cwd"), self.root) if args.get("cwd") else self.root
        if args.get("cwd"):
            if not self._inside(base, self.root):
                return f"工作目录越界：{base}"
            if restricted and not any(self._inside(base, base_root) for base_root in allowed_roots):
                return f"工作目录不在授权范围：{base}"

        for label, raw_path, base_override in self._paths(args, base):
            resolved = self._resolve(raw_path, base_override or base)
            problem = self._check_path(label, resolved, allowed_roots, restricted)
            if problem:
                return problem
            if tool == "run_python" and resolved.suffix != ".py":
                return "只能执行 .py 脚本"
            if tool == "run_checks" and label.endswith(".script") and resolved.suffix != ".py":
                return "run_checks 只允许执行 .py 脚本"

        if tool == "git":
            return self._check_git_paths(str(args.get("args", "")), allowed_roots)
        return None

    def _paths(self, args: dict, base: Path):
        """产出 (label, raw_path, base_override)；覆盖顶层与 run_checks 嵌套。"""
        for key in PATH_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                yield key, value, None
        for key in ("paths", "scripts"):
            value = args.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        yield key, item, None
        checks = args.get("checks")
        if isinstance(checks, list):
            for index, item in enumerate(checks):
                if not isinstance(item, dict):
                    continue
                for key in ("script", "cwd"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        yield f"checks[{index}].{key}", value, self.root
        # 兼容旧字段：script 已在 PATH_KEYS

    def _check_path(self, label: str, resolved: Path, allowed_roots: list[Path], restricted: bool) -> str | None:
        if not self._inside(resolved, self.root):
            return f"路径越界：{resolved}"
        if restricted and not any(self._inside(resolved, base_root) for base_root in allowed_roots):
            return f"路径不在授权范围：{resolved}"
        if DENY_PARTS & set(resolved.parts):
            return f"敏感路径：{resolved.name}"
        if resolved.suffix.lower() in DENY_SUFFIXES:
            return f"敏感文件：{resolved.name}"
        return None

    @staticmethod
    def _resolve(raw, base: Path) -> Path:
        candidate = Path(str(raw)).expanduser()
        return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()

    def _check_git_paths(self, args: str, allowed_roots: list[Path]) -> str | None:
        for token in args.split():
            cleaned = token.replace('"', "").replace("'", "")
            if cleaned.startswith("--") and "=" in cleaned:
                cleaned = cleaned.split("=", 1)[1]
            if not self._looks_like_path(cleaned):
                continue
            resolved = self._resolve(cleaned, self.root)
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
