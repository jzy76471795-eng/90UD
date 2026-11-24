"""Poll the owning Game socket without changing its send deadline."""
from __future__ import annotations

import select
import socket


def recv_game_chunk(client: socket.socket, timeout: float) -> bytes:
    """Keep receive polling separate from the socket's bounded send timeout."""
    if not select.select([client], [], [], timeout)[0]:
        raise socket.timeout("Game receive poll elapsed")
    return client.recv(4096)
