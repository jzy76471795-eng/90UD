"""Stage3 payload builders for the Blood/Ultimate altar protocols."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AltarWaveActor:
    effect_selector: int
    monster_seq: int
    monster_code: int
    monster_type: int
    level: int
    scale: int
    x: int
    y: int
    z: int


def build_start_blood_map_00d3(
    *,
    cell_x: int,
    cell_y: int,
    seed: int,
    mode: int,
    map_id: int,
    global_flag: int = 0,
    primary_rows: Sequence[bytes] = (),
    secondary_rows: Sequence[bytes] = (),
    tail: int = 0,
) -> bytes:
    """Build S0/00D3 with its native 21-byte and 20-byte counted tables."""
    mode_value = int(mode)
    if mode_value not in {1, 2}:
        raise ValueError("altar mode must be 1 or 2")
    if len(primary_rows) > 0xFF or len(secondary_rows) > 0xFF:
        raise ValueError("00D3 counted tables exceed u8 count")
    if any(len(row) != 21 for row in primary_rows):
        raise ValueError("00D3 primary rows must be exactly 21 bytes")
    if any(len(row) != 20 for row in secondary_rows):
        raise ValueError("00D3 secondary rows must be exactly 20 bytes")
    body = bytearray(
        struct.pack(
            "<BBIBBI",
            int(cell_x) & 0xFF,
            int(cell_y) & 0xFF,
            int(seed) & 0xFFFFFFFF,
            int(global_flag) & 0xFF,
            mode_value & 0xFF,
            int(map_id) & 0xFFFFFFFF,
        )
    )
    body.append(len(primary_rows))
    for row in primary_rows:
        body.extend(row)
    body.append(len(secondary_rows))
    for row in secondary_rows:
        body.extend(row)
    body.append(int(tail) & 0xFF)
    return bytes(body)


def build_blood_monster_spawn_00d2(
    actors: Sequence[AltarWaveActor],
    *,
    tail_state: int = 0,
) -> bytes:
    """Build S0/00D2: u16 count, count*17-byte actor rows, u16 tail."""
    if len(actors) > 0xFFFF:
        raise ValueError("00D2 actor count exceeds u16")
    body = bytearray(struct.pack("<H", len(actors)))
    for actor in actors:
        body.extend(
            struct.pack(
                "<BHIBBHHHH",
                int(actor.effect_selector) & 0xFF,
                int(actor.monster_seq) & 0xFFFF,
                int(actor.monster_code) & 0xFFFFFFFF,
                int(actor.monster_type) & 0xFF,
                int(actor.level) & 0xFF,
                int(actor.scale) & 0xFFFF,
                int(actor.x) & 0xFFFF,
                int(actor.y) & 0xFFFF,
                int(actor.z) & 0xFFFF,
            )
        )
    body.extend(struct.pack("<H", int(tail_state) & 0xFFFF))
    return bytes(body)


def parse_blood_wave_clear_00ff(body: bytes) -> tuple[int, ...]:
    raw = bytes(body or b"")
    if not raw:
        raise ValueError("00FF body is empty")
    count = raw[0]
    expected = 1 + count * 2
    if len(raw) != expected:
        raise ValueError(
            "00FF body length mismatch count=%d expected=%d actual=%d"
            % (count, expected, len(raw))
        )
    if count == 0:
        return ()
    return tuple(struct.unpack("<%dH" % count, raw[1:]))


def build_blood_wave_clear_ack_00ff(reward_value: int = 0) -> bytes:
    return struct.pack("<I", int(reward_value) & 0xFFFFFFFF)


def parse_blood_selection_013d(body: bytes) -> int:
    raw = bytes(body or b"")
    if len(raw) != 1:
        raise ValueError("013D body must be exactly one byte")
    return raw[0]


def build_blood_selection_ack_013d(selection: int) -> bytes:
    value = int(selection)
    if value in {1, 2}:
        return bytes((1, value))
    return b"\x00"


def build_blood_round_interval_00d7(
    zero_based_round: int,
    first_wave_delay_ms: int,
) -> bytes:
    return struct.pack(
        "<BI",
        max(0, min(0xFF, int(zero_based_round) + 1)),
        max(0, min(0xFFFFFFFF, int(first_wave_delay_ms))),
    )


def build_ultimate_difficulty_prompt_0129(
    completed_rounds: int,
    timeout_seconds: int,
) -> bytes:
    return struct.pack(
        "<BBH",
        0,
        max(0, min(0xFF, int(completed_rounds))),
        max(0, min(0xFFFF, int(timeout_seconds))),
    )


def build_ultimate_difficulty_resolved_0129(selection: int) -> bytes:
    value = int(selection)
    if value not in {1, 2}:
        raise ValueError("ultimate difficulty must be 1 or 2")
    return bytes((value,))


def build_blood_result_summary_00d0(
    *,
    play_time_ms: int,
    current_round: int,
    best_time_ms: int,
    best_round: int,
    max_round: int,
    reward_exp: int,
) -> bytes:
    return struct.pack(
        "<6I",
        max(0, min(0xFFFFFFFF, int(play_time_ms))),
        max(0, min(0xFFFFFFFF, int(current_round))),
        max(0, min(0xFFFFFFFF, int(best_time_ms))),
        max(0, min(0xFFFFFFFF, int(best_round))),
        max(0, min(0xFFFFFFFF, int(max_round))),
        max(0, min(0xFFFFFFFF, int(reward_exp))),
    )


def build_blood_result_groups_00d1(
    *,
    current_round: int,
    max_round: int,
    groups: Sequence[Sequence[tuple[int, int]]],
) -> bytes:
    if len(groups) > 4:
        raise ValueError("00D1 supports exactly four reward groups")
    body = bytearray(
        (
            max(0, min(0xFF, int(current_round))),
            max(0, min(0xFF, int(max_round))),
        )
    )
    normalized = list(groups) + [()] * (4 - len(groups))
    for group in normalized:
        rows = tuple(group)
        if len(rows) > 0xFF:
            raise ValueError("00D1 reward group exceeds u8 count")
        body.append(len(rows))
        for item_id, count in rows:
            body.extend(
                struct.pack(
                    "<II",
                    int(item_id) & 0xFFFFFFFF,
                    max(0, min(0xFFFFFFFF, int(count))),
                )
            )
    return bytes(body)
