# -*- coding: utf-8 -*-
"""C1/057E recalculates one online character's mercenary points."""
from __future__ import annotations

import struct
from typing import Any

from game.character.adventure_group_features import (
    load_mercenary_character_points,
    resolve_account_id,
)


REFRESH_MERCENARY_POINTS_OPCODE = 0x057E
ERROR_CODE = 1


def parse_refresh_mercenary_points_057e(body: bytes) -> int | None:
    raw = bytes(body or b"")
    if len(raw) < 4 or any(raw[4:]):
        return None
    character_id = struct.unpack_from("<I", raw, 0)[0]
    return character_id if character_id > 0 else None


def build_refresh_mercenary_points_success_057e(
    character_id: int,
    total_points: int,
    avatar_point_tier: int,
) -> bytes:
    values = (int(character_id), int(total_points), int(avatar_point_tier))
    if (
        not 0 < values[0] <= 0xFFFFFFFF
        or not 0 <= values[1] <= 0xFFFFFFFF
        or not 0 <= values[2] <= 0xFF
    ):
        raise ValueError("invalid mercenary point refresh response")
    return struct.pack("<BIIB", 1, *values)


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        REFRESH_MERCENARY_POINTS_OPCODE,
        bytes((0, ERROR_CODE)),
    )
    return [packet], "C1/057E MERCENARY_POINTS reject=%s" % reason


def handle_refresh_mercenary_points_057e(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    character_id = parse_refresh_mercenary_points_057e(body)
    if character_id is None:
        return _failure(server, "invalid_body")
    repo = server._repo()
    account_id = resolve_account_id(server, repo, session)
    record = next(
        (
            entry
            for entry in load_mercenary_character_points(repo, account_id)
            if entry.character_id == character_id
        ),
        None,
    )
    if record is None:
        return _failure(server, "character_not_owned")
    response = build_refresh_mercenary_points_success_057e(
        character_id,
        record.total_points,
        record.avatar_point_tier,
    )
    packet = server._codec_response(1, REFRESH_MERCENARY_POINTS_OPCODE, response)
    return [packet], (
        "C1/057E MERCENARY_POINTS success account=%d character=%d "
        "equipment=%d avatar=%d tier=%d total=%d"
        % (
            account_id,
            character_id,
            record.equipment_points,
            record.avatar_points,
            record.avatar_point_tier,
            record.total_points,
        )
    )


__all__ = [
    "REFRESH_MERCENARY_POINTS_OPCODE",
    "build_refresh_mercenary_points_success_057e",
    "handle_refresh_mercenary_points_057e",
    "parse_refresh_mercenary_points_057e",
]
