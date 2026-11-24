"""Stage3 damage-font ownership snapshot and wire payload builders."""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0


DAMAGE_FONT_LIST_OPCODE = 0x04D9
DAMAGE_FONT_SELECT_OPCODE = 0x0507
DEFAULT_DAMAGE_FONT_ID = 0


def build_damage_font_list_body(
    repo: Any,
    character_id: int,
    *,
    now_unix: int | None = None,
) -> tuple[bytes, int, int]:
    current_font_id, rows = repo.list_damage_fonts(
        int(character_id),
        now_unix=now_unix,
    )
    if len(rows) >= 0xFFFF:
        raise ValueError("damage-font list exceeds u16 count")
    entry_count = len(rows) + 1
    body = bytearray(
        struct.pack("<HH", int(current_font_id) & 0xFFFF, entry_count)
    )
    body.extend(struct.pack("<HHI", DEFAULT_DAMAGE_FONT_ID, 0, 0))
    for row in rows:
        font_id = int(row["font_id"])
        expire_at = 0
        if not 0 < font_id <= 0xFFFF:
            raise ValueError("owned damage-font id is outside u16")
        body.extend(struct.pack("<HHI", font_id, 0, expire_at))
    return bytes(body), int(current_font_id), entry_count


def build_damage_font_list_packet(
    server: Any,
    repo: Any,
    character_id: int,
    *,
    now_unix: int | None = None,
) -> tuple[bytes, str]:
    body, current, count = build_damage_font_list_body(
        repo,
        character_id,
        now_unix=now_unix,
    )
    return server._codec_response(0, DAMAGE_FONT_LIST_OPCODE, body), (
        "S0/04D9 current=%d entries=%d" % (current, count)
    )


def build_main_inventory_refresh_packet(
    server: Any,
    repo: Any,
    character_id: int,
) -> tuple[bytes, str]:
    rows, list_param16 = repo.build_itemlist_mode0_rows(
        int(character_id),
        list_type=0,
    )
    body = build_item_list_mode0(
        [InventoryItemView(row) for row in rows],
        list_type=0,
        list_param16=int(list_param16),
    )
    return server._codec_response(0, 0x000D, body), (
        "S0/000D list0 entries=%d" % len(rows)
    )


__all__ = [
    "DAMAGE_FONT_LIST_OPCODE",
    "DAMAGE_FONT_SELECT_OPCODE",
    "DEFAULT_DAMAGE_FONT_ID",
    "build_damage_font_list_body",
    "build_damage_font_list_packet",
    "build_main_inventory_refresh_packet",
]
