"""工具包：import 本包会扫描并导入同目录下所有模块，完成自动注册。

新增工具只需在 forchat/tools/ 里丢一个 .py：
    from ..registry import tool

    @tool(description="...", params={"path": "..."})
    def my_tool(path: str) -> str:
        ...
不需要改任何其他文件。
"""
from ..registry import load_tools

load_tools()
