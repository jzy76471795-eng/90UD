"""Frozen Stage3 avatar socket instance state and wire codec."""
from __future__ import annotations

import json
import struct
from typing import Any

from game.character.item_metadata import (
    AVATAR_SOCKET_FALLBACK_BY_EQUIPMENT_TYPE,
    AVATAR_SOCKET_TYPE_BY_LABEL,
    ItemMetadata,
    resolve_item_metadata,
)


AVATAR_SOCKET_BLOB_KEY = "avatar_socket_blob_v1"
AVATAR_AUX_BLOB_KEY = "avatar_aux_blob_v1"
AVATAR_SOCKET_COUNT = 5
AVATAR_SOCKET_ENTRY_SIZE = 6
AVATAR_SOCKET_BLOB_SIZE = AVATAR_SOCKET_COUNT * AVATAR_SOCKET_ENTRY_SIZE
AVATAR_AUX_BLOB_SIZE = 4
VALID_AVATAR_SOCKET_TYPES = frozenset(AVATAR_SOCKET_TYPE_BY_LABEL.values())


def _extra_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="ignore")
    else:
        text = str(raw or "{}")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def template_socket_types(metadata: ItemMetadata) -> tuple[int, ...]:
    values = tuple(int(value) & 0xFFFF for value in getattr(metadata, "avatar_socket_types", ()) or ())
    if not values and bool(getattr(metadata, "is_avatar", False)):
        equipment_type = str(getattr(metadata, "equipment_type", "") or "").strip().lower()
        values = AVATAR_SOCKET_FALLBACK_BY_EQUIPMENT_TYPE.get(equipment_type, ())
    if len(values) > AVATAR_SOCKET_COUNT:
        raise ValueError("avatar socket count exceeds Stage3 limit")
    if any(value not in VALID_AVATAR_SOCKET_TYPES for value in values):
        raise ValueError("avatar template contains an invalid socket type")
    return values


def encode_socket_blob(
    socket_types: tuple[int, ...] | list[int],
    emblem_item_ids: tuple[int, ...] | list[int] | None = None,
) -> bytes:
    types = tuple(int(value) & 0xFFFF for value in socket_types)
    ids = tuple(int(value) & 0xFFFFFFFF for value in (emblem_item_ids or ()))
    if len(types) > AVATAR_SOCKET_COUNT or len(ids) > AVATAR_SOCKET_COUNT:
        raise ValueError("avatar socket state exceeds Stage3 limit")
    if any(value not in VALID_AVATAR_SOCKET_TYPES for value in types):
        raise ValueError("invalid Stage3 avatar socket type")
    out = bytearray(AVATAR_SOCKET_BLOB_SIZE)
    for index, socket_type in enumerate(types):
        emblem_id = ids[index] if index < len(ids) else 0
        struct.pack_into("<HI", out, index * AVATAR_SOCKET_ENTRY_SIZE, socket_type, emblem_id)
    return bytes(out)


def validate_socket_blob(blob: bytes) -> bytes:
    value = bytes(blob)
    if len(value) != AVATAR_SOCKET_BLOB_SIZE:
        raise ValueError("avatar socket blob must be exactly 30 bytes")
    for offset in range(0, AVATAR_SOCKET_BLOB_SIZE, AVATAR_SOCKET_ENTRY_SIZE):
        socket_type, _emblem_id = struct.unpack_from("<HI", value, offset)
        if socket_type != 0 and socket_type not in VALID_AVATAR_SOCKET_TYPES:
            raise ValueError("invalid Stage3 avatar socket type 0x%04X" % socket_type)
    return value


def decode_socket_blob(blob: bytes) -> tuple[tuple[int, int], ...]:
    value = validate_socket_blob(blob)
    return tuple(
        struct.unpack_from("<HI", value, offset)
        for offset in range(0, AVATAR_SOCKET_BLOB_SIZE, AVATAR_SOCKET_ENTRY_SIZE)
    )


def _hex_blob(value: Any, expected_size: int) -> bytes | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != expected_size * 2:
        return None
    try:
        blob = bytes.fromhex(text)
    except ValueError:
        return None
    return blob if len(blob) == expected_size else None


def default_socket_blob(metadata: ItemMetadata) -> bytes | None:
    socket_types = template_socket_types(metadata)
    return encode_socket_blob(socket_types) if socket_types else None


def merge_default_avatar_extra(raw: Any, metadata: ItemMetadata) -> str:
    data = _extra_json_dict(raw)
    default_blob = default_socket_blob(metadata)
    if default_blob is not None and AVATAR_SOCKET_BLOB_KEY not in data:
        data[AVATAR_SOCKET_BLOB_KEY] = default_blob.hex().upper()
        data.setdefault(AVATAR_AUX_BLOB_KEY, (b"\x00" * AVATAR_AUX_BLOB_SIZE).hex().upper())
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def merge_avatar_socket_blob(raw: Any, socket_blob: bytes) -> str:
    data = _extra_json_dict(raw)
    value = validate_socket_blob(socket_blob)
    data[AVATAR_SOCKET_BLOB_KEY] = value.hex().upper()
    data.setdefault(
        AVATAR_AUX_BLOB_KEY,
        (b"\x00" * AVATAR_AUX_BLOB_SIZE).hex().upper(),
    )
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def resolve_avatar_blobs(
    item_id: int,
    raw_extra_json: Any,
    *,
    metadata: ItemMetadata | None = None,
) -> tuple[bytes, bytes] | None:
    meta = metadata if metadata is not None else resolve_item_metadata(int(item_id))
    data = _extra_json_dict(raw_extra_json)
    socket_blob = _hex_blob(data.get(AVATAR_SOCKET_BLOB_KEY), AVATAR_SOCKET_BLOB_SIZE)
    if socket_blob is not None:
        try:
            socket_blob = validate_socket_blob(socket_blob)
        except ValueError:
            socket_blob = None
    if socket_blob is None:
        socket_blob = default_socket_blob(meta)
    if socket_blob is None:
        return None
    aux_blob = _hex_blob(data.get(AVATAR_AUX_BLOB_KEY), AVATAR_AUX_BLOB_SIZE)
    if aux_blob is None:
        aux_blob = b"\x00" * AVATAR_AUX_BLOB_SIZE
    return socket_blob, aux_blob


def pack_avatar_extra_wire(
    item_id: int,
    raw_extra_json: Any,
    *,
    metadata: ItemMetadata | None = None,
) -> bytes:
    blobs = resolve_avatar_blobs(item_id, raw_extra_json, metadata=metadata)
    if blobs is None:
        return struct.pack("<II", 0, 0)
    socket_blob, aux_blob = blobs
    return (
        struct.pack("<I", len(socket_blob))
        + socket_blob
        + struct.pack("<I", len(aux_blob))
        + aux_blob
    )


__all__ = [
    "AVATAR_AUX_BLOB_KEY",
    "AVATAR_AUX_BLOB_SIZE",
    "AVATAR_SOCKET_BLOB_KEY",
    "AVATAR_SOCKET_BLOB_SIZE",
    "AVATAR_SOCKET_COUNT",
    "default_socket_blob",
    "decode_socket_blob",
    "encode_socket_blob",
    "merge_avatar_socket_blob",
    "merge_default_avatar_extra",
    "pack_avatar_extra_wire",
    "resolve_avatar_blobs",
    "template_socket_types",
    "validate_socket_blob",
]
