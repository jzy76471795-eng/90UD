"""C1/02CB clears one main-bag equipment or avatar sorting lock."""
from __future__ import annotations

from typing import Any

from game.character.inventory_sort_lock_ui import (
    CLEAR_SORT_ITEM_LOCK_RESPONSE_OPCODE,
    build_sort_item_lock_state_body,
    parse_sort_item_lock_position_body,
)
from game.character.inventory_sort_repository import set_inventory_sort_lock
from game.quest.quest_session import resolve_selected_character_detail


CLEAR_SORT_ITEM_LOCK_OPCODE = 0x02CB


def handle_clear_sort_item_lock_02cb(
    server: Any,
    plain: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_sort_item_lock_position_body(plain)
    if parsed is None:
        return [], (
            "C1/02CB CLEAR_SORT_ITEM_LOCK reject=body_len_%d expect=3 attempt=2/3"
            % len(plain or b"")
        )
    list_type, slot_index = parsed
    detail = resolve_selected_character_detail(server, session)
    character_id = detail.get("character_id")
    if character_id is None:
        return [], (
            "C1/02CB CLEAR_SORT_ITEM_LOCK list_type=%d slot=%d "
            "reject=no_character attempt=2/3" % (list_type, slot_index)
        )

    try:
        result = set_inventory_sort_lock(
            server._repo(),
            character_id=int(character_id),
            list_type=list_type,
            slot_index=slot_index,
            locked=False,
        )
    except Exception as exc:
        return [], (
            "C1/02CB CLEAR_SORT_ITEM_LOCK cid=%d list_type=%d slot=%d "
            "fail=transaction_error:%s:%s attempt=2/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                type(exc).__name__,
                exc,
            )
        )

    if not result.success:
        return [], (
            "C1/02CB CLEAR_SORT_ITEM_LOCK cid=%d list_type=%d slot=%d "
            "ignored=%s item=%d attempt=2/3"
            % (
                int(character_id),
                list_type,
                slot_index,
                result.reason,
                result.item_template_id,
            )
        )

    response_body = build_sort_item_lock_state_body(list_type, slot_index)
    return [
        server._codec_response(
            1,
            CLEAR_SORT_ITEM_LOCK_RESPONSE_OPCODE,
            response_body,
        )
    ], (
        "C1/02CB CLEAR_SORT_ITEM_LOCK cid=%d list_type=%d slot=%d "
        "item=%d locked=%d changed=%d response=S1/02CB attempt=2/3"
        % (
            int(character_id),
            list_type,
            slot_index,
            result.item_template_id,
            result.sort_lock_flag,
            int(result.changed),
        )
    )


__all__ = [
    "CLEAR_SORT_ITEM_LOCK_OPCODE",
    "handle_clear_sort_item_lock_02cb",
]
