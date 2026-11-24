# -*- coding: utf-8 -*-
"""C1/057C returns the active Stage3 adventure dispatch."""
from __future__ import annotations

from typing import Any

from game.character.adventure_group_features import (
    MERCENARY_STATE_OPCODE,
    build_mercenary_state_body_0540,
    resolve_account_id,
)


RETURN_ADVENTURE_DISPATCH_OPCODE = 0x057C
ERROR_CODE = 1


def parse_return_adventure_dispatch_057c(body: bytes) -> bool:
    return not any(bytes(body or b""))


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        RETURN_ADVENTURE_DISPATCH_OPCODE,
        bytes((0, ERROR_CODE)),
    )
    return [packet], "C1/057C ADVENTURE_RETURN reject=%s" % reason


def handle_return_adventure_dispatch_057c(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    if not parse_return_adventure_dispatch_057c(body):
        return _failure(server, "invalid_body")
    repo = server._repo()
    account_id = resolve_account_id(server, repo, session)
    ok, detail, state = repo.return_adventure_dispatch(account_id)
    if not ok:
        return _failure(server, detail)
    packets = [
        server._codec_response(1, RETURN_ADVENTURE_DISPATCH_OPCODE, b"\x01"),
        server._codec_response(
            0,
            MERCENARY_STATE_OPCODE,
            build_mercenary_state_body_0540(state),
        ),
    ]
    return packets, (
        "C1/057C ADVENTURE_RETURN success account=%d result=%s state=%d "
        "S1/057C,S0/0540"
        % (account_id, detail, int(state["dispatch_state"]))
    )


__all__ = [
    "RETURN_ADVENTURE_DISPATCH_OPCODE",
    "handle_return_adventure_dispatch_057c",
    "parse_return_adventure_dispatch_057c",
]
