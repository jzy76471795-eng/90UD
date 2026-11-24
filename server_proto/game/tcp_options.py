from __future__ import annotations

import os
import socket
from typing import Callable


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def configure_game_tcp_socket(
    client: socket.socket,
    log: Callable[[str], None],
) -> bool:
    if not _env_bool("USDOF_GAME_TCP_NODELAY", True):
        return False
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True
    except OSError as exc:
        log("[Game/TCP] TCP_NODELAY setup failed: %s" % exc)
        return False
