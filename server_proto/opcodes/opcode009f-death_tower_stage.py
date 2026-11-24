"""C1/0x009F death-tower floor start/complete reports.

Frozen Stage3 sends command 1 after a floor finishes loading and command 2
after the floor runtime completes its PVF ``tower talk``/``tower dialog``
events.  Advancing to the next floor remains owned by the following C1/002D.
"""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain

_tower_payloads = load_chain("helper-tower_payloads.py")
build_empty_death_tower_ranking_0090 = (
    _tower_payloads.build_empty_death_tower_ranking_0090
)
build_empty_death_tower_reward_0091 = (
    _tower_payloads.build_empty_death_tower_reward_0091
)
build_death_tower_eplp_0092 = _tower_payloads.build_death_tower_eplp_0092


def parse_death_tower_stage_009f(body: bytes) -> dict[str, Any]:
    raw = bytes(body or b"")
    command = int(raw[0]) if raw else None
    metrics: list[tuple[int, int, int]] = []
    if command == 2:
        offset = 1
        for _index in range(8):
            if offset + 10 > len(raw):
                break
            metrics.append(struct.unpack_from("<HII", raw, offset))
            offset += 10
    return {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" "),
        "command": command,
        "metrics": tuple(metrics),
        "complete": command == 1 or (command == 2 and len(metrics) == 8),
    }


def build_death_tower_stage_ack_009f() -> bytes:
    return b"\x01"


def handle_death_tower_stage_009f(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_death_tower_stage_009f(body)
    command = parsed["command"]
    is_death_tower = (
        session is not None
        and bool(getattr(session, "in_dungeon_run", False))
        and str(getattr(session, "dungeon_tower_kind", "") or "").lower()
        == "death"
    )

    if is_death_tower and command in (1, 2):
        session.dungeon_tower_stage_state = int(command)
        session.dungeon_tower_stage_metrics = parsed["metrics"]
        if command == 1:
            # Floor enter/advance owns the reset, and DIE owns the transition
            # to cleared. A late start event must not erase an earlier clear.
            session.dungeon_tower_awaiting_move = False
        else:
            session.dungeon_tower_awaiting_move = True

    packets = [
        server._codec_response(1, 0x009F, build_death_tower_stage_ack_009f())
    ]
    settlement = "none"
    if is_death_tower and command == 2:
        floor = int(getattr(session, "dungeon_tower_floor", 0) or 0)
        final_floor = int(getattr(session, "dungeon_tower_final_floor", 0) or 0)
        floor_cleared = bool(
            getattr(session, "dungeon_tower_floor_cleared", False)
        )
        already_settled = bool(getattr(session, "dungeon_tower_settled", False))
        if floor_cleared and final_floor > 0 and floor >= final_floor:
            if already_settled:
                settlement = "duplicate_ignored"
            else:
                dungeon_id = int(
                    getattr(session, "selected_dungeon_id", 0)
                    or getattr(session, "cur_dungeon_id", 0)
                    or 0
                )
                packets.extend(
                    [
                        server._codec_response(
                            0,
                            0x0090,
                            build_empty_death_tower_ranking_0090(dungeon_id),
                        ),
                        server._codec_response(
                            0,
                            0x0091,
                            build_empty_death_tower_reward_0091(),
                        ),
                        server._codec_response(
                            0,
                            0x0092,
                            build_death_tower_eplp_0092(True),
                        ),
                    ]
                )
                session.dungeon_tower_settled = True
                session.dungeon_tower_awaiting_move = False
                session.dungeon_clear_phase = 2
                settlement = "final_0090,0091,0092"

    return packets, (
        "C1/009F DEATH_TOWER_STAGE command=%s plain_len=%d metrics=%d "
        "complete=%d tower_session=%d floor=%s/%s floor_cleared=%d "
        "awaiting_002d=%d; "
        "ACK=S1/009F 01 settlement=%s dialogue=client_pvf_tower_events"
        % (
            command,
            parsed["raw_len"],
            len(parsed["metrics"]),
            int(bool(parsed["complete"])),
            int(is_death_tower),
            getattr(session, "dungeon_tower_floor", None),
            getattr(session, "dungeon_tower_final_floor", None),
            int(bool(getattr(session, "dungeon_tower_floor_cleared", False))),
            int(bool(getattr(session, "dungeon_tower_awaiting_move", False))),
            settlement,
        )
    )
