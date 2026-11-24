"""C1/0507 selects one owned Stage3 damage font."""
from __future__ import annotations

import struct
from typing import Any

from game.character.damage_font import DAMAGE_FONT_SELECT_OPCODE
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_OR_UNOWNED = 0x11


def parse_select_damage_font_0507(body: bytes) -> int | None:
    raw = bytes(body or b"")
    if len(raw) != 2:
        return None
    return int(struct.unpack("<H", raw)[0])


def build_select_damage_font_success(font_id: int) -> bytes:
    value = int(font_id)
    if not 0 <= value <= 0xFFFF:
        raise ValueError("selected damage-font id is outside u16")
    return b"\x01" + struct.pack("<H", value)


def build_select_damage_font_failure(
    error_code: int = ERROR_INVALID_OR_UNOWNED,
) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    body = build_select_damage_font_failure()
    return [server._codec_response(1, DAMAGE_FONT_SELECT_OPCODE, body)], (
        "C1/0507 DAMAGE_FONT_SELECT reject=%s error=0x%02x; S1/0507 failure"
        % (reason, ERROR_INVALID_OR_UNOWNED)
    )


def handle_select_damage_font_0507(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    font_id = parse_select_damage_font_0507(body)
    if font_id is None:
        return _failure(server, "malformed_body")
    detail = resolve_selected_character_detail(server, session)
    character_id = int(detail.get("character_id") or 0)
    if character_id <= 0:
        return _failure(server, "no_selected_character")
    ok, note, result = server._repo().select_damage_font(
        character_id,
        font_id,
    )
    if not ok:
        return _failure(server, note)
    response = server._codec_response(
        1,
        DAMAGE_FONT_SELECT_OPCODE,
        build_select_damage_font_success(font_id),
    )
    return [response], (
        "C1/0507 DAMAGE_FONT_SELECT success character=%d font=%d previous=%d; "
        "S1/0507 success"
        % (
            character_id,
            font_id,
            int(result.get("previous_font_id") or 0),
        )
    )


__all__ = [
    "ERROR_INVALID_OR_UNOWNED",
    "build_select_damage_font_failure",
    "build_select_damage_font_success",
    "handle_select_damage_font_0507",
    "parse_select_damage_font_0507",
]
