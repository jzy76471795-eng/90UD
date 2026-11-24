"""Stage3 tower packet payload builders.

The field widths in this module follow the frozen Stage3 readers.  The
ordinary dungeon payload helpers deliberately do not own these packet shapes:
death-tower DGN files have an ordered floor-map table instead of a maze.
"""
from __future__ import annotations

import struct
from typing import Any, Sequence


def build_death_tower_info_008e(
    dungeon_id: int,
    final_floor: int,
    *,
    mode: int = 0,
    buff_type: int = 11,
) -> bytes:
    """Build S0/008E: ``u32 dungeon, u16 final_floor, u8 mode, u8 buff``."""
    return struct.pack(
        "<IHBB",
        int(dungeon_id) & 0xFFFFFFFF,
        int(final_floor) & 0xFFFF,
        int(mode) & 0xFF,
        int(buff_type) & 0xFF,
    )


def build_death_tower_monster_row_14b(monster: dict[str, Any]) -> bytes:
    """Build one Stage3 S0/008F monster row."""
    return struct.pack(
        "<IHI4B",
        int(monster.get("list_index", monster.get("template_order", 0)))
        & 0xFFFFFFFF,
        int(monster.get("unique_id", monster.get("seq", 0))) & 0xFFFF,
        int(monster.get("monster_id", monster.get("code", 0))) & 0xFFFFFFFF,
        int(monster.get("level", 1)) & 0xFF,
        int(monster.get("type", monster.get("mon_type", 0))) & 0xFF,
        int(monster.get("is_box", 0)) & 0xFF,
        int(monster.get("box_index", 0)) & 0xFF,
    )


def build_death_tower_item_row_20b(item: dict[str, Any] | bytes) -> bytes:
    """Build one Stage3 S0/008F item row without assigning guessed semantics."""
    if isinstance(item, (bytes, bytearray)):
        raw = bytes(item)
        if len(raw) != 20:
            raise ValueError("death tower item rows must be exactly 20 bytes")
        return raw
    return struct.pack(
        "<BIIIHBHH",
        int(item.get("field0", 0)) & 0xFF,
        int(item.get("field1", 0)) & 0xFFFFFFFF,
        int(item.get("field2", 0)) & 0xFFFFFFFF,
        int(item.get("field3", 0)) & 0xFFFFFFFF,
        int(item.get("field4", 0)) & 0xFFFF,
        int(item.get("field5", 0)) & 0xFF,
        int(item.get("field6", 0)) & 0xFFFF,
        int(item.get("field7", 0)) & 0xFFFF,
    )


def build_death_tower_floor_008f(
    floor: int,
    seed: int,
    map_id: int,
    *,
    monsters: Sequence[dict[str, Any]] | None = None,
    items: Sequence[dict[str, Any] | bytes] | None = None,
) -> bytes:
    """Build S0/008F using the Stage3 u32 map id and 20-byte item rows."""
    monster_rows = list(monsters or ())[:0xFF]
    item_rows = list(items or ())[:0xFF]
    body = bytearray(
        struct.pack(
            "<HII",
            int(floor) & 0xFFFF,
            int(seed) & 0xFFFFFFFF,
            int(map_id) & 0xFFFFFFFF,
        )
    )
    body.append(len(monster_rows))
    for monster in monster_rows:
        body += build_death_tower_monster_row_14b(monster)
    body.append(len(item_rows))
    for item in item_rows:
        body += build_death_tower_item_row_20b(item)
    return bytes(body)


def build_empty_death_tower_ranking_0090(dungeon_id: int) -> bytes:
    """Build the complete 305-byte empty ranking body read by Stage3."""
    body = bytearray()
    body += struct.pack("<BII", 0, 0, 0)
    body += bytes([0])
    body += struct.pack("<I", int(dungeon_id) & 0xFFFFFFFF)
    body += bytes([0])
    for _group in range(5):
        for _row in range(8):
            body += struct.pack("<I", 0)
            body += bytes(2)
        body += struct.pack("<HII", 0, 0, 0)
    if len(body) != 305:
        raise AssertionError("empty Stage3 death tower ranking must be 305 bytes")
    return bytes(body)


def build_empty_death_tower_reward_0091(summary: int = 0) -> bytes:
    """Build S0/0091 with four empty counted reward groups."""
    return struct.pack("<I4B", int(summary) & 0xFFFFFFFF, 0, 0, 0, 0)


def build_death_tower_eplp_0092(cleared: bool) -> bytes:
    return bytes([1 if cleared else 0])
