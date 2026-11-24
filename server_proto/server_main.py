# -*- coding: utf-8 -*-
"""兼容入口：转发到 start.main。

请优先使用（CPython 3.12.10）::

    server-pack-win10\\cache\\python-3.12.10-full\\python.exe server_proto\\start.py
    # 或
    python server_proto/start.py   # 版本不匹配时会 re-exec
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from python_runtime import ensure_server_dir_on_sys_path, ensure_server_python  # noqa: E402

ensure_server_dir_on_sys_path()

if __name__ == "__main__":
    ensure_server_python(reexec=True)
    ensure_server_dir_on_sys_path()
    from start import main

    raise SystemExit(main())
