"""Stage3 equipment-protection lock response and restore payloads."""
from __future__ import annotations

import struct
from typing import Any, Iterable, Sequence

from game.character.inventory_equipment_lock_repository import (
    EquipmentLockEntry,
    list_equipment_locks,
)


ITEM_LOCK_LIST_OPCODE = 0x00FB
ITEM_UNLOCK_NOTICE_OPCODE = 0x00FC
ITEM_LOCK_STATE_ACTIVE = 1


def build_item_position_body(list_type: int, slot_index: int) -> bytes:
    return struct.pack("<Bh", int(list_type) & 0xFF, int(slot_index))


def build_item_lock_success_body(list_type: int, slot_index: int) -> bytes:
    return b"\x01" + build_item_position_body(list_type, slot_index)


def build_item_unlock_success_body(
    list_type: int,
    slot_index: int,
    *,
    remaining_seconds: int = 0,
) -> bytes:
    return (
        b"\x01"
        + build_item_position_body(list_type, slot_index)
        + struct.pack("<I", max(0, int(remaining_seconds)) & 0xFFFFFFFF)
    )


def build_item_lock_list_body(
    entries: Sequence[EquipmentLockEntry],
) -> bytes:
    rows = list(entries)
    if len(rows) > 0xFFFF:
        raise ValueError("equipment lock count exceeds u16")
    body = bytearray(struct.pack("<H", len(rows)))
    for entry in rows:
        body += build_item_position_body(entry.list_type, entry.slot_index)
        body.append(ITEM_LOCK_STATE_ACTIVE)
    return bytes(body)


def build_item_lock_delta_body(list_type: int, slot_index: int) -> bytes:
    return build_item_lock_list_body(
        [
            EquipmentLockEntry(
                list_type=int(list_type) & 0xFF,
                slot_index=int(slot_index),
                equipment_lock_id=1,
            )
        ]
    )


def build_equipment_lock_restore_packets(
    server: Any,
    repo: Any,
    *,
    character_id: int,
    list_types: Iterable[int] = (0,),
) -> tuple[list[bytes], str]:
    entries = list_equipment_locks(
        repo,
        character_id=int(character_id),
        list_types=list_types,
    )
    if not entries:
        return [], "S0/00FB equipment_lock_restore n=0"
    body = build_item_lock_list_body(entries)
    packet = server._codec_response(0, ITEM_LOCK_LIST_OPCODE, body)
    positions = ",".join(
        "%d:%d" % (entry.list_type, entry.slot_index) for entry in entries
    )
    return [packet], (
        "S0/00FB equipment_lock_restore n=%d positions=%s"
        % (len(entries), positions)
    )


__all__ = [
    "ITEM_LOCK_LIST_OPCODE",
    "ITEM_LOCK_STATE_ACTIVE",
    "ITEM_UNLOCK_NOTICE_OPCODE",
    "build_equipment_lock_restore_packets",
    "build_item_lock_delta_body",
    "build_item_lock_list_body",
    "build_item_lock_success_body",
    "build_item_position_body",
    "build_item_unlock_success_body",
]
