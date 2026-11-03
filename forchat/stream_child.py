"""子进程入口：python -m forchat.stream_child（由 ProcessStreamFactory 启动）。"""
from .stream_exec import child_main

if __name__ == "__main__":
    raise SystemExit(child_main())
