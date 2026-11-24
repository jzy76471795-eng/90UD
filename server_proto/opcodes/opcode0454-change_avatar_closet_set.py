"""C1/0454 handles UI Select and changes the displayed closet set."""
from __future__ import annotations

from typing import Any

from game.character.avatar_closet_repository import (
    change_avatar_closet_display_set,
)


CHANGE_AVATAR_CLOSET_SET_OPCODE = 0x0454


def parse_change_avatar_closet_set_request(body: bytes) -> int | None:
    raw = bytes(body or b"")
    return int(raw[0]) if len(raw) == 1 else None


def build_change_avatar_closet_set_result(
    success: bool,
    selected_set_index: int = 0,
) -> bytes:
    if not success:
        return b"\x00"
    selected = int(selected_set_index)
    if not 0 <= selected <= 0xFF:
        raise ValueError("selected_set_index is outside u8")
    # Stage3 sub_FA4820 stores this byte and calls sub_FA47A0, which refreshes
    # the selection highlight, closet UI, and Seria-room mannequin.
    return bytes((1, selected))


def _selected_character_id(session: Any) -> int | None:
    try:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return character_id if character_id > 0 else None


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        CHANGE_AVATAR_CLOSET_SET_OPCODE,
        build_change_avatar_closet_set_result(False),
    )
    return [packet], "C1/0454 AVATAR_CLOSET_SELECT success=0 detail=%s" % reason


def handle_change_avatar_closet_set_0454(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    target = parse_change_avatar_closet_set_request(body)
    if target is None:
        return _failure(server, "malformed body_len=%d" % len(body or b""))
    character_id = _selected_character_id(session)
    if character_id is None:
        return _failure(server, "no_selected_character")

    ok, note, detail = change_avatar_closet_display_set(
        server._repo(),
        character_id,
        target,
    )
    if not ok:
        return _failure(server, note)

    selected_set = int(detail.get("current_set_index", target) or 0)
    packet = server._codec_response(
        1,
        CHANGE_AVATAR_CLOSET_SET_OPCODE,
        build_change_avatar_closet_set_result(True, selected_set),
    )
    return [packet], (
        "C1/0454 AVATAR_CLOSET_SELECT cid=%d target=%d success=1 "
        "display=%d unlocked=%s detail=%s; S1/0454 native display refresh"
        % (
            character_id,
            target,
            selected_set,
            detail.get("unlocked_set_count"),
            note,
        )
    )


__all__ = [
    "CHANGE_AVATAR_CLOSET_SET_OPCODE",
    "build_change_avatar_closet_set_result",
    "handle_change_avatar_closet_set_0454",
    "parse_change_avatar_closet_set_request",
]
