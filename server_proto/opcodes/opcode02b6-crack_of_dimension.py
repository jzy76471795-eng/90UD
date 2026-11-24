"""C1/0x02B6 Another Arad / Crack of Dimension entry request.

Frozen Stage3 sends exactly two little-endian u32 values selected from its
local Crack-of-Dimension pair vector: historical dungeon id and crack quest id.
"""
from __future__ import annotations

from typing import Any


def parse_crack_of_dimension_02b6(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    return {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" "),
        "historical_dungeon_id": (
            int.from_bytes(raw[0:4], "little") if len(raw) >= 8 else None
        ),
        "crack_quest_id": (
            int.from_bytes(raw[4:8], "little") if len(raw) >= 8 else None
        ),
        "trailing_len": max(0, len(raw) - 8),
    }


def handle_crack_of_dimension_02b6(
    server: Any,
    plain: bytes,
    *,
    session: Any,
) -> tuple[list[bytes], str]:
    parsed = parse_crack_of_dimension_02b6(plain)
    dungeon_id = parsed["historical_dungeon_id"]
    quest_id = parsed["crack_quest_id"]
    base = (
        "C1/02B6 CRACK_OF_DIMENSION plain_len=%d plain=%s "
        "historical_dungeon=%s crack_quest=%s trailing=%d "
        "ida=0x02BCCB90"
        % (
            parsed["raw_len"],
            parsed["raw_hex"] or "<empty>",
            dungeon_id,
            quest_id,
            parsed["trailing_len"],
        )
    )
    if dungeon_id is None or quest_id is None:
        return [], "%s; reject=short_u32_pair" % base
    if session is None:
        return [], "%s; reject=no_game_session" % base

    packets, note = server.dungeon_enter_000f_0010.grant_another_arad(
        session=session,
        historical_dungeon_id=int(dungeon_id),
        crack_quest_id=int(quest_id),
    )
    return packets, "%s; %s" % (base, note)
