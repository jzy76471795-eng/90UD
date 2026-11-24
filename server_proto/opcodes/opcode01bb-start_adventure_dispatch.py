# -*- coding: utf-8 -*-
"""C1/01BB starts one account-wide Stage3 adventure dispatch."""
from __future__ import annotations

from typing import Any
import struct

from game.character.adventure_group import load_adventure_group_overview
from game.character.adventure_group_features import (
    MERCENARY_STATE_OPCODE,
    build_mercenary_state_body_0540,
    load_mercenary_character_points,
    resolve_account_id,
)
from pvf.adventure_group_feature_index import get_adventure_group_feature_index


START_ADVENTURE_DISPATCH_OPCODE = 0x01BB
ERROR_CODE = 1
DISPATCH_DAYS = frozenset((1,3,7,10,15,20,30))


def parse_start_adventure_dispatch_01bb(body: bytes) -> int | None:
    raw = bytes(body or b"")
    if len(raw) == 5:
        seconds=struct.unpack_from('<I',raw,1)[0]
        if seconds and (seconds % 86400 or seconds // 86400 not in DISPATCH_DAYS):
            return None
    elif len(raw) < 1 or any(raw[1:]):
        return None
    return int(raw[0])


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        START_ADVENTURE_DISPATCH_OPCODE,
        bytes((0, ERROR_CODE)),
    )
    return [packet], "C1/01BB ADVENTURE_DISPATCH reject=%s" % reason


def handle_start_adventure_dispatch_01bb(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    area_index = parse_start_adventure_dispatch_01bb(body)
    if area_index is None:
        return _failure(server, "invalid_body")
    feature_index = get_adventure_group_feature_index()
    area = feature_index.dispatch_area(area_index)
    if area is None:
        return _failure(server, "unknown_area=%d" % area_index)
    requested_duration=struct.unpack_from('<I',body,1)[0] if len(body)==5 else 0
    duration=requested_duration or area.duration_seconds

    repo = server._repo()
    account_id = resolve_account_id(server, repo, session)
    overview = load_adventure_group_overview(repo, account_id)
    if overview.progression.adventure_level < area.required_adventure_level:
        return _failure(
            server,
            "adventure_level_low have=%d need=%d"
            % (
                overview.progression.adventure_level,
                area.required_adventure_level,
            ),
        )
    records = load_mercenary_character_points(repo, account_id)
    eligible = tuple(
        record for record in records if record.level >= area.required_character_level
    )
    committed_points = sum(record.total_points for record in eligible)
    if committed_points < area.minimum_points:
        return _failure(
            server,
            "mercenary_points_low have=%d need=%d eligible=%d"
            % (committed_points, area.minimum_points, len(eligible)),
        )
    ok, detail, state = repo.start_adventure_dispatch(
        account_id,
        area_index=area.area_index,
        committed_points=committed_points,
        duration_seconds=duration,
    )
    if not ok:
        return _failure(server, detail)
    packets = [
        server._codec_response(1, START_ADVENTURE_DISPATCH_OPCODE, b"\x01"),
        server._codec_response(
            0,
            MERCENARY_STATE_OPCODE,
            build_mercenary_state_body_0540(state),
        ),
    ]
    return packets, (
        "C1/01BB ADVENTURE_DISPATCH success area=%d name=%r account=%d "
        "eligible=%d committed=%d duration=%d S1/01BB,S0/0540"
        % (
            area.area_index,
            area.name,
            account_id,
            len(eligible),
            committed_points,
            duration,
        )
    )


__all__ = [
    "START_ADVENTURE_DISPATCH_OPCODE",
    "handle_start_adventure_dispatch_01bb",
    "parse_start_adventure_dispatch_01bb",
]
