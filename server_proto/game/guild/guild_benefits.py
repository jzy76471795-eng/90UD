"""Guild membership lookup and Stage3 native status synchronization."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.guild.guild_repository import GuildRepository


GUILD_STATUS_UPSERT_OPCODE = 0x01E8
GUILD_STATUS_REMOVE_OPCODE = 0x01E9
GUILD_STATUS_ACTIVE_SET_OPCODE = 0x01EA


@dataclass(frozen=True)
class GuildStatusRecord:
    """One record consumed by Stage3 ``S0/01E8``."""

    status_id: int
    runtime_parameter: int
    duration_seconds: int
    overlap_count: int


# Frozen Script.pvf status 169 is Guild_Status1: four stats +60 per overlap,
# overlap max 2, icon index 206. Two overlaps produce the requested +120.
GUILD_MEMBERSHIP_STATUS = GuildStatusRecord(
    status_id=169,
    runtime_parameter=0,
    duration_seconds=0,
    overlap_count=2,
)


def _u32(value: int, field: str) -> int:
    number = int(value)
    if not 0 <= number <= 0xFFFFFFFF:
        raise ValueError("%s must fit u32: %r" % (field, value))
    return number


def _unique_status_ids(status_ids: Any) -> tuple[int, ...]:
    result: list[int] = []
    seen: set[int] = set()
    for raw_status_id in status_ids:
        status_id = _u32(raw_status_id, "guild status id")
        if status_id in seen:
            continue
        seen.add(status_id)
        result.append(status_id)
    return tuple(result)


def build_guild_status_upsert_body(statuses: Any) -> bytes:
    """Encode ``u8 count + count * 4*u32`` for Stage3 ``S0/01E8``."""

    records = tuple(statuses)
    if len(records) > 0xFF:
        raise ValueError("guild status upsert count must fit u8")
    body = bytearray((len(records),))
    for record in records:
        body += struct.pack(
            "<IIII",
            _u32(record.status_id, "guild status id"),
            _u32(record.runtime_parameter, "guild status runtime parameter"),
            _u32(record.duration_seconds, "guild status duration seconds"),
            _u32(record.overlap_count, "guild status overlap count"),
        )
    return bytes(body)


def build_guild_status_remove_body(status_ids: Any) -> bytes:
    """Encode ``u8 count + count * u32`` for Stage3 ``S0/01E9``."""

    ids = _unique_status_ids(status_ids)
    if len(ids) > 0xFF:
        raise ValueError("guild status remove count must fit u8")
    return bytes((len(ids),)) + b"".join(
        struct.pack("<I", status_id) for status_id in ids
    )


def build_guild_status_active_set_body(status_ids: Any) -> bytes:
    """Encode the complete ``u16 count + count * u32`` ``S0/01EA`` set."""

    ids = _unique_status_ids(status_ids)
    if len(ids) > 0xFFFF:
        raise ValueError("guild status active-set count must fit u16")
    return struct.pack("<H", len(ids)) + b"".join(
        struct.pack("<I", status_id) for status_id in ids
    )


def build_guild_membership_status_specs(
    membership_active: bool,
) -> tuple[tuple[int, bytes, str], ...]:
    """Build the complete native guild-status update for one character."""

    if membership_active:
        active_statuses = (GUILD_MEMBERSHIP_STATUS,)
        mutation = (
            GUILD_STATUS_UPSERT_OPCODE,
            build_guild_status_upsert_body(active_statuses),
            "guild status upsert id=169 duration=permanent overlap=2",
        )
    else:
        active_statuses = ()
        mutation = (
            GUILD_STATUS_REMOVE_OPCODE,
            build_guild_status_remove_body((GUILD_MEMBERSHIP_STATUS.status_id,)),
            "guild status remove id=169",
        )
    active_ids = tuple(status.status_id for status in active_statuses)
    active_set = (
        GUILD_STATUS_ACTIVE_SET_OPCODE,
        build_guild_status_active_set_body(active_ids),
        "guild status active-set ids=%s"
        % (",".join(str(status_id) for status_id in active_ids) or "none"),
    )
    return mutation, active_set


def pack_guild_membership_status_rows(
    server: Any,
    membership_active: bool,
) -> tuple[tuple[bytes, str], ...]:
    """Pack membership status specs as normal codec response queue rows."""

    return tuple(
        (server._codec_response(0, opcode, body), note)
        for opcode, body, note in build_guild_membership_status_specs(
            membership_active
        )
    )


def has_active_guild_membership(repo: Any, character: Any) -> bool:
    """Return whether a character's durable guild membership is active."""

    try:
        character_id = int(getattr(character, "character_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    db_path = getattr(repo, "db_path", None)
    if character_id <= 0 or db_path is None:
        return False
    return GuildRepository(db_path).is_character_in_guild(character_id)


__all__ = [
    "GUILD_MEMBERSHIP_STATUS",
    "GUILD_STATUS_ACTIVE_SET_OPCODE",
    "GUILD_STATUS_REMOVE_OPCODE",
    "GUILD_STATUS_UPSERT_OPCODE",
    "GuildStatusRecord",
    "build_guild_membership_status_specs",
    "build_guild_status_active_set_body",
    "build_guild_status_remove_body",
    "build_guild_status_upsert_body",
    "has_active_guild_membership",
    "pack_guild_membership_status_rows",
]
