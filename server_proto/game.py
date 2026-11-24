# -*- coding: utf-8 -*-
"""Game 侧调度入口（薄再导出）。

业务实现在 ``game/`` 包与顶级 ``opcodes/``；本文件供外部统一 import：

    from game import GameServer  # 注意：包名 game 优先于本文件
    # 推荐：
    from game.game_server import GameServer

若与 ``game/`` 包冲突，请直接使用包内路径。本文件保留为调度文档锚点，
并在 ``__main__`` 时以 game-only 方式转调 start。

必须 CPython 3.12.10（与 Win10/Win11 分享包一致）::

    server-pack-win10\\cache\\python-3.12.10-full\\python.exe server_proto\\game.py
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
    """仅启动 Game 服（等价 start.py --game-only）。"""
    ensure_server_python(reexec=True)
    ensure_server_dir_on_sys_path()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--game-only" not in argv:
        argv = ["--game-only", *argv]
    # 替换 argv 后调用总入口
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
