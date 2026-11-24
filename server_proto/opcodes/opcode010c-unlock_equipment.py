"""C1/010C immediately removes one equipment-protection lock."""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_equipment_lock_repository import set_equipment_lock
from game.character.inventory_equipment_lock_ui import (
    ITEM_UNLOCK_NOTICE_OPCODE,
    build_item_position_body,
    build_item_unlock_success_body,
)
from game.quest.quest_session import resolve_selected_character_detail


REQUEST_ITEM_UNLOCK_OPCODE = 0x010C
ITEM_UNLOCK_ERROR_INVALID_TARGET = 0x13
STAGE3_IMMEDIATE_UNLOCK_MARKER = 0xFF


def parse_item_unlock_request(body: bytes) -> tuple[int, int, int] | None:
    raw = bytes(body or b"")
    if len(raw) != 4:
        return None
    list_type, slot_index, stage3_marker = struct.unpack("<BhB", raw)
    if stage3_marker != STAGE3_IMMEDIATE_UNLOCK_MARKER:
        return None
    return int(list_type), int(slot_index), int(stage3_marker)


def build_item_unlock_error_body(
    error_code: int = ITEM_UNLOCK_ERROR_INVALID_TARGET,
    *,
    remaining_seconds: int = 0,
) -> bytes:
    return bytes((0, int(error_code) & 0xFF)) + struct.pack(
        "<I", max(0, int(remaining_seconds)) & 0xFFFFFFFF
    )


def handle_unlock_equipment_010c(
    server: Any,
    plain: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_item_unlock_request(plain)
    if parsed is None:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_UNLOCK_OPCODE,
            build_item_unlock_error_body(),
        )
        return [packet], (
            "C1/010C REQUEST_ITEM_UNLOCK reject=body_shape_%d "
            "expect=4_tail_ff attempt=2/3"
            % len(plain or b"")
        )
    list_type, slot_index, stage3_marker = parsed
    detail = resolve_selected_character_detail(server, session)
    character_id = detail.get("character_id")
    if character_id is None:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_UNLOCK_OPCODE,
            build_item_unlock_error_body(),
        )
        return [packet], (
            "C1/010C REQUEST_ITEM_UNLOCK list_type=%d slot=%d marker=%02x "
            "reject=no_character attempt=2/3"
            % (list_type, slot_index, stage3_marker)
        )
    try:
        result = set_equipment_lock(
            server._repo(),
            character_id=int(character_id),
            list_type=list_type,
            slot_index=slot_index,
            locked=False,
        )
    except Exception as exc:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_UNLOCK_OPCODE,
            build_item_unlock_error_body(),
        )
        return [packet], (
            "C1/010C REQUEST_ITEM_UNLOCK cid=%d list_type=%d slot=%d marker=%02x "
            "fail=transaction_error:%s:%s attempt=2/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                stage3_marker,
                type(exc).__name__,
                exc,
            )
        )
    if not result.success:
        packet = server._codec_response(
            1,
            REQUEST_ITEM_UNLOCK_OPCODE,
            build_item_unlock_error_body(),
        )
        return [packet], (
            "C1/010C REQUEST_ITEM_UNLOCK cid=%d list_type=%d slot=%d marker=%02x "
            "reject=%s item=%d attempt=2/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                stage3_marker,
                result.reason,
                result.item_template_id,
            )
        )

    ack = server._codec_response(
        1,
        REQUEST_ITEM_UNLOCK_OPCODE,
        build_item_unlock_success_body(
            list_type,
            slot_index,
            remaining_seconds=0,
        ),
    )
    notice = server._codec_response(
        0,
        ITEM_UNLOCK_NOTICE_OPCODE,
        build_item_position_body(list_type, slot_index),
    )
    return [ack, notice], (
        "C1/010C REQUEST_ITEM_UNLOCK cid=%d list_type=%d slot=%d marker=%02x "
        "item=%d changed=%d remaining=0 response=S1/010C->S0/00FC attempt=2/3"
        % (
            int(character_id),
            list_type,
            slot_index,
            stage3_marker,
            result.item_template_id,
            int(result.changed),
        )
    )


__all__ = [
    "ITEM_UNLOCK_ERROR_INVALID_TARGET",
    "REQUEST_ITEM_UNLOCK_OPCODE",
    "STAGE3_IMMEDIATE_UNLOCK_MARKER",
    "build_item_unlock_error_body",
    "handle_unlock_equipment_010c",
    "parse_item_unlock_request",
]
