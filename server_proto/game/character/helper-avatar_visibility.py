"""Stage3 character-model visibility bits and S0/0165 notification."""
from __future__ import annotations

import struct
from typing import Any


DEFAULT_VISIBILITY_BITS = 0x03
ADVANCED_AVATAR_OPTION_INDEX = 1
SHOW_ALL_AVATAR_OPTION_INDEX = 55
ADVANCED_AVATAR_BIT = 0x02
HIDE_ALL_AVATAR_BIT = 0x08


def _option_u16(main_game_option: bytes, index: int) -> int | None:
    offset = int(index) * 2
    data = bytes(main_game_option or b"")
    if offset < 0 or offset + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, offset)[0]


def visibility_update_masks(main_game_option: bytes) -> tuple[int, int]:
    """Return ``(set_mask, clear_mask)`` for options present in the blob."""
    set_mask = 0
    clear_mask = 0

    advanced = _option_u16(main_game_option, ADVANCED_AVATAR_OPTION_INDEX)
    if advanced is not None:
        if advanced:
            set_mask |= ADVANCED_AVATAR_BIT
        else:
            clear_mask |= ADVANCED_AVATAR_BIT

    show_all = _option_u16(main_game_option, SHOW_ALL_AVATAR_OPTION_INDEX)
    if show_all is not None:
        if show_all:
            clear_mask |= HIDE_ALL_AVATAR_BIT
        else:
            set_mask |= HIDE_ALL_AVATAR_BIT

    return set_mask & 0xFF, clear_mask & 0xFF


def apply_visibility_update(
    current_bits: int,
    *,
    set_mask: int,
    clear_mask: int,
) -> int:
    return ((int(current_bits) | int(set_mask)) & ~int(clear_mask)) & 0xFF


def build_avatar_visibility_body(character_id: int, visibility_bits: int) -> bytes:
    """Build Stage3 S0/0165 body: ``u16 object_key, u8 visibility_bits``."""
    return struct.pack(
        "<HB",
        int(character_id) & 0xFFFF,
        int(visibility_bits) & 0xFF,
    )


def pack_avatar_visibility(server: Any, character: Any) -> bytes:
    return pack_avatar_visibility_state(
        server,
        character_id=int(getattr(character, "character_id", 0) or 0),
        visibility_bits=int(
            getattr(character, "visibility_bits", DEFAULT_VISIBILITY_BITS) or 0
        ),
    )


def pack_avatar_visibility_state(
    server: Any,
    *,
    character_id: int,
    visibility_bits: int,
) -> bytes:
    """Build a visibility snapshot when only the session state is available."""
    return server._codec_response(
        0,
        0x0165,
        build_avatar_visibility_body(character_id, visibility_bits),
    )


__all__ = [
    "ADVANCED_AVATAR_BIT",
    "ADVANCED_AVATAR_OPTION_INDEX",
    "DEFAULT_VISIBILITY_BITS",
    "HIDE_ALL_AVATAR_BIT",
    "SHOW_ALL_AVATAR_OPTION_INDEX",
    "apply_visibility_update",
    "build_avatar_visibility_body",
    "pack_avatar_visibility",
    "pack_avatar_visibility_state",
    "visibility_update_masks",
]
