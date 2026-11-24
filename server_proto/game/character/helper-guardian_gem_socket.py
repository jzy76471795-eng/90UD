"""Persistent guardian-gem sockets for the frozen Stage3 guild medal entry."""
from __future__ import annotations

import json
import struct
from typing import Any


GUARDIAN_GEM_SOCKET_COUNT = 4
GUARDIAN_GEM_ID_BASE = 89999
GUARDIAN_GEM_ENTRY_OFFSET = 90
GUARDIAN_GEM_EXTRA_KEY = "guardian_gem_sockets"


def guardian_gem_id_to_wire_code(item_id: int) -> int:
    """Encode one exact guardian-gem item id as the Stage3 u16 socket value."""
    code = int(item_id) - GUARDIAN_GEM_ID_BASE
    if not 1 <= code <= 0xFFFF:
        raise ValueError("guardian gem item id is outside the Stage3 socket range")
    return code


def guardian_gem_wire_code_to_id(code: int) -> int:
    """Decode one Stage3 socket value; zero is the empty socket sentinel."""
    value = int(code)
    if value == 0:
        return 0
    if not 1 <= value <= 0xFFFF:
        raise ValueError("guardian gem socket code is outside u16")
    return value + GUARDIAN_GEM_ID_BASE


def _extra_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="ignore").strip()
    else:
        text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def parse_guardian_gem_sockets(raw: Any) -> tuple[int, int, int, int]:
    """Return four exact item ids from a medal instance's ``extra_json``."""
    value = _extra_object(raw).get(GUARDIAN_GEM_EXTRA_KEY)
    rows = list(value) if isinstance(value, (list, tuple)) else []
    sockets: list[int] = []
    for index in range(GUARDIAN_GEM_SOCKET_COUNT):
        try:
            item_id = int(rows[index]) if index < len(rows) else 0
            if item_id > 0:
                guardian_gem_id_to_wire_code(item_id)
            else:
                item_id = 0
        except (TypeError, ValueError):
            item_id = 0
        sockets.append(item_id)
    return tuple(sockets)  # type: ignore[return-value]


def merge_guardian_gem_socket(raw: Any, socket_index: int, item_id: int) -> str:
    """Replace one socket while preserving all unrelated instance metadata."""
    index = int(socket_index)
    if not 0 <= index < GUARDIAN_GEM_SOCKET_COUNT:
        raise ValueError("guardian gem socket index must be 0..3")
    exact_item_id = int(item_id)
    guardian_gem_id_to_wire_code(exact_item_id)
    payload = _extra_object(raw)
    sockets = list(parse_guardian_gem_sockets(payload))
    sockets[index] = exact_item_id
    payload[GUARDIAN_GEM_EXTRA_KEY] = sockets
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def apply_guardian_gem_sockets_to_entry108(row: bytearray, raw: Any) -> None:
    """Write the four Stage3 u16 socket codes at entry offsets 90..97."""
    if len(row) < GUARDIAN_GEM_ENTRY_OFFSET + GUARDIAN_GEM_SOCKET_COUNT * 2:
        raise ValueError("guardian gem target entry is shorter than 98 bytes")
    codes = tuple(
        guardian_gem_id_to_wire_code(item_id) if item_id > 0 else 0
        for item_id in parse_guardian_gem_sockets(raw)
    )
    struct.pack_into(
        "<4H",
        row,
        GUARDIAN_GEM_ENTRY_OFFSET,
        *codes,
    )


__all__ = [
    "GUARDIAN_GEM_SOCKET_COUNT",
    "GUARDIAN_GEM_ID_BASE",
    "GUARDIAN_GEM_ENTRY_OFFSET",
    "GUARDIAN_GEM_EXTRA_KEY",
    "guardian_gem_id_to_wire_code",
    "guardian_gem_wire_code_to_id",
    "parse_guardian_gem_sockets",
    "merge_guardian_gem_socket",
    "apply_guardian_gem_sockets_to_entry108",
]
