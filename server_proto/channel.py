# -*- coding: utf-8 -*-
"""Channel 侧调度入口（薄再导出）。

业务实现在 ``channel/`` 包。本文件在 ``__main__`` 时以 channel-only 转调 start。

必须 CPython 3.12.10（与 Win10/Win11 分享包一致）::

    server-pack-win10\\cache\\python-3.12.10-full\\python.exe server_proto\\channel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from python_runtime import ensure_server_dir_on_sys_path, ensure_server_python  # noqa: E402

ensure_server_dir_on_sys_path()


def main(argv: list[str] | None = None) -> int:
    """仅启动 Channel 服（等价 start.py --channel-only）。"""
    ensure_server_python(reexec=True)
    ensure_server_dir_on_sys_path()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--channel-only" not in argv:
        argv = ["--channel-only", *argv]
    old = sys.argv
    try:
        sys.argv = [old[0], *argv]
        from start import main as start_main

        return start_main()
    finally:
        sys.argv = old


if __name__ == "__main__":
    ensure_server_python(reexec=True)
    raise SystemExit(main())
