"""Protocol notices for package rewards activated directly by the server."""
from __future__ import annotations

import struct
import time
from typing import Any

from game.shop.cube_contract import (
    CUBE_CONTRACT_STATE_OPCODE,
    resolve_cube_contract_state,
)
from game.shop.premium_contract_index import PREMIUM_TYPE_CUBE


def build_premium_activation_notice(
    premium_type: int,
    remaining_seconds: int,
) -> bytes:
    ptype = int(premium_type)
    remaining = int(remaining_seconds)
    if ptype <= 0 or ptype > 0xFF:
        raise ValueError("invalid premium_type=%d" % ptype)
    if remaining <= 0 or remaining > 0x7FFFFFFFFFFFFFFF:
        raise ValueError("invalid premium remaining=%d" % remaining)
    return struct.pack("<HBq", 2, ptype, remaining)


def build_system_reward_activation_packets(
    server: Any,
    repo: Any,
    *,
    detail: dict[str, Any],
    account_id: int,
    character_id: int,
    session: Any = None,
) -> tuple[list[bytes], tuple[str, ...]]:
    """Publish the final timer for each premium type activated by one box."""
    raw_rows = tuple(detail.get("premium_activations", ()) or ())
    latest_by_type: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for raw in raw_rows:
        row = dict(raw or {})
        premium_type = int(row.get("premium_type", 0) or 0) & 0xFF
        remaining = int(row.get("remaining_seconds", 0) or 0)
        if premium_type <= 0 or remaining <= 0:
            continue
        if premium_type not in latest_by_type:
            order.append(premium_type)
        latest_by_type[premium_type] = row
    if not order:
        return [], ()

    packets: list[bytes] = []
    notes: list[str] = []
    for premium_type in order:
        row = latest_by_type[premium_type]
        remaining = int(row["remaining_seconds"])
        packets.append(
            server._codec_response(
                0,
                0x0042,
                build_premium_activation_notice(premium_type, remaining),
            )
        )
        notes.append("premium_type=%d remaining=%d" % (premium_type, remaining))

    if PREMIUM_TYPE_CUBE in latest_by_type:
        now_unix = max(
            0,
            int(detail.get("system_activation_now", 0) or int(time.time())),
        )
        cube_state = resolve_cube_contract_state(
            repo,
            account_id=int(account_id),
            character_id=int(character_id),
            now_unix=now_unix,
        )
        packets.append(
            server._codec_response(
                0,
                CUBE_CONTRACT_STATE_OPCODE,
                cube_state.to_body(),
            )
        )
        notes.append("cube_selection=%02x" % cube_state.wire_selection)

    if session is not None:
        try:
            session.account_premium_effects = None
        except Exception:
            pass
    return packets, tuple(notes)


__all__ = [
    "build_premium_activation_notice",
    "build_system_reward_activation_packets",
]
