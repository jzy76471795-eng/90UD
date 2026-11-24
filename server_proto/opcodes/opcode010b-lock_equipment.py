"""C1/010B immediately protects one equipment instance."""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_equipment_lock_repository import set_equipment_lock
from game.character.inventory_equipment_lock_ui import (
    ITEM_LOCK_LIST_OPCODE,
    build_item_lock_delta_body,
    build_item_lock_success_body,
)
from game.quest.quest_session import resolve_selected_character_detail


REQUEST_ITEM_LOCK_OPCODE = 0x010B
ITEM_LOCK_ERROR_INVALID_TARGET = 0x13


def parse_item_lock_request(body: bytes) -> tuple[int, int] | None:
    raw = bytes(body or b"")
    if len(raw) != 3:
        return None
    list_type, slot_index = struct.unpack("<Bh", raw)
    return int(list_type), int(slot_index)


def build_item_lock_error_body(
    error_code: int = ITEM_LOCK_ERROR_INVALID_TARGET,
) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def handle_lock_equipment_010b(
    server: Any,
    plain: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_item_lock_request(plain)
    if parsed is None:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_LOCK_OPCODE,
            build_item_lock_error_body(),
        )
        return [packet], (
            "C1/010B REQUEST_ITEM_LOCK reject=body_len_%d expect=3 attempt=1/3"
            % len(plain or b"")
        )
    list_type, slot_index = parsed
    detail = resolve_selected_character_detail(server, session)
    character_id = detail.get("character_id")
    if character_id is None:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_LOCK_OPCODE,
            build_item_lock_error_body(),
        )
        return [packet], (
            "C1/010B REQUEST_ITEM_LOCK list_type=%d slot=%d "
            "reject=no_character attempt=1/3" % (list_type, slot_index)
        )
    try:
        result = set_equipment_lock(
            server._repo(),
            character_id=int(character_id),
            list_type=list_type,
            slot_index=slot_index,
            locked=True,
        )
    except Exception as exc:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_LOCK_OPCODE,
            build_item_lock_error_body(),
        )
        return [packet], (
            "C1/010B REQUEST_ITEM_LOCK cid=%d list_type=%d slot=%d "
            "fail=transaction_error:%s:%s attempt=1/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                type(exc).__name__,
                exc,
            )
        )
    if not result.success:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_LOCK_OPCODE,
            build_item_lock_error_body(),
        )
        return [packet], (
            "C1/010B REQUEST_ITEM_LOCK cid=%d list_type=%d slot=%d "
            "reject=%s item=%d attempt=1/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                result.reason,
                result.item_template_id,
            )
        )

    ack = server._codec_response(
        1,
        REQUEST_ITEM_LOCK_OPCODE,
        build_item_lock_success_body(list_type, slot_index),
    )
    notice = server._codec_response(
        0,
        ITEM_LOCK_LIST_OPCODE,
        build_item_lock_delta_body(list_type, slot_index),
    )
    return [ack, notice], (
        "C1/010B REQUEST_ITEM_LOCK cid=%d list_type=%d slot=%d item=%d "
        "lock_id=%d changed=%d response=S1/010B->S0/00FB attempt=1/3"
        % (
            int(character_id),
            list_type,
            slot_index,
            result.item_template_id,
            result.equipment_lock_id,
            int(result.changed),
        )
    )


__all__ = [
    "ITEM_LOCK_ERROR_INVALID_TARGET",
    "REQUEST_ITEM_LOCK_OPCODE",
    "build_item_lock_error_body",
    "handle_lock_equipment_010b",
    "parse_item_lock_request",
]
