"""Restore native inventory sorting-lock markers after container snapshots."""
from __future__ import annotations

import struct
from typing import Any

from game.character.item_metadata import SLOT_RANGE_EQUIPMENT


SET_SORT_ITEM_LOCK_RESPONSE_OPCODE = 0x02CA
CLEAR_SORT_ITEM_LOCK_RESPONSE_OPCODE = 0x02CB


def parse_sort_item_lock_position_body(body: bytes) -> tuple[int, int] | None:
    """Parse the Stage3 writer: ``u8 list_type + i16 slot_index``."""
    raw = bytes(body or b"")
    if len(raw) != 3:
        return None
    list_type, slot_index = struct.unpack("<Bh", raw)
    return int(list_type), int(slot_index)


def build_sort_item_lock_state_body(list_type: int, slot_index: int) -> bytes:
    """Build ``u8 success + u8 list_type + i16 slot`` for S1/02CA and 02CB."""
    return b"\x01" + struct.pack("<Bh", int(list_type), int(slot_index))


def list_inventory_sort_locked_slots(
    repo: Any,
    *,
    character_id: int,
    list_type: int = 0,
) -> tuple[int, ...]:
    """Return current supported-container slots whose sorting lock is set."""
    lt = int(list_type) & 0xFF
    if lt == int(repo.INVENTORY_LIST_MAIN):
        start_slot, end_slot = SLOT_RANGE_EQUIPMENT
        allowed_kinds = frozenset({"equipment"})
    elif lt == int(repo.INVENTORY_LIST_AVATAR):
        expansion = int(repo.get_container_list_param(character_id, list_type=lt))
        start_slot = 0
        end_slot = int(repo.BASE_AVATAR_SLOT_COUNT) + expansion - 1
        allowed_kinds = frozenset({"equipment", "avatar"})
    else:
        return ()
    rows = repo.list_inventory_items(
        int(character_id),
        list_type=lt,
    )
    return tuple(
        sorted(
            {
                int(row.get("slot_index", -1))
                for row in rows
                if str(row.get("item_kind", "") or "") in allowed_kinds
                and int(row.get("sort_lock_flag", 0) or 0) == 1
                and start_slot <= int(row.get("slot_index", -1)) <= end_slot
            }
        )
    )


def build_inventory_sort_lock_restore_packets(
    server: Any,
    repo: Any,
    *,
    character_id: int,
    list_type: int = 0,
) -> tuple[list[Any], str]:
    """Build one verified native setter per locked slot after S0/000D."""
    lt = int(list_type) & 0xFF
    slots = list_inventory_sort_locked_slots(
        repo,
        character_id=int(character_id),
        list_type=lt,
    )
    packets = [
        server._codec_response(
            1,
            SET_SORT_ITEM_LOCK_RESPONSE_OPCODE,
            build_sort_item_lock_state_body(lt, slot),
        )
        for slot in slots
    ]
    return packets, "S1/02CA sort_lock_restore list%d n=%d slots=%s" % (
        lt,
        len(slots),
        ",".join(str(slot) for slot in slots) or "-",
    )


__all__ = [
    "CLEAR_SORT_ITEM_LOCK_RESPONSE_OPCODE",
    "SET_SORT_ITEM_LOCK_RESPONSE_OPCODE",
    "build_inventory_sort_lock_restore_packets",
    "build_sort_item_lock_state_body",
    "list_inventory_sort_locked_slots",
    "parse_sort_item_lock_position_body",
]
