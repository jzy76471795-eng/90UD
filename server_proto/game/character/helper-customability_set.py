"""Stage3 Aiken dynamic part-set state packets and login restoration."""
from __future__ import annotations

import struct
from typing import Any

from game.character.customability_repository import (
    CustomAbilitySetRecord,
    load_customability_set_records,
)


CUSTOMABILITY_SET_STATE_OPCODE = 0x0500
CUSTOMABILITY_SET_STATE_BODY_SIZE = 12


def build_customability_set_state_body(
    actor_id: int,
    record: CustomAbilitySetRecord,
) -> bytes:
    actor = int(actor_id)
    record_id = int(record.set_record_id)
    required_part_count = int(record.required_part_count)
    option_ids = tuple(int(value) for value in record.option_ids)
    set_grade = int(record.set_grade)
    if not 0 <= actor <= 0xFFFFFFFF:
        raise ValueError("customability set actor ID must be a u32")
    if not 1 <= record_id <= 0xFF:
        raise ValueError("customability set record ID must be a nonzero u8")
    if not 3 <= required_part_count <= 6:
        raise ValueError("customability set required part count must be 3..6")
    if (
        not 1 <= len(option_ids) <= 5
        or len(set(option_ids)) != len(option_ids)
        or any(not 1 <= value <= 0xFF for value in option_ids)
    ):
        raise ValueError("customability set options must be distinct nonzero u8")
    if not 0 <= set_grade <= 3:
        raise ValueError("customability set grade must be between 0 and 3")
    padded = option_ids + (0,) * (5 - len(option_ids))
    body = struct.pack(
        "<IBB5BB",
        actor,
        record_id,
        required_part_count,
        *padded,
        set_grade,
    )
    if len(body) != CUSTOMABILITY_SET_STATE_BODY_SIZE:
        raise AssertionError("customability set state body size drift")
    return body


def prepare_customability_set_records(
    repo: Any,
    character_id: int,
) -> tuple[tuple[CustomAbilitySetRecord, ...], str]:
    records = load_customability_set_records(repo, int(character_id))
    return records, (
        "customability_set_prepare records=%d ids=%s required_parts=%s"
        % (
            len(records),
            tuple(record.set_record_id for record in records),
            tuple(record.required_part_count for record in records),
        )
    )


def build_customability_set_restore_packets(
    server: Any,
    repo: Any,
    *,
    character_id: int,
    actor_id: int,
) -> tuple[list[bytes], str]:
    records, _prepare_note = prepare_customability_set_records(
        repo,
        int(character_id),
    )
    packets = [
        server._codec_response(
            0,
            CUSTOMABILITY_SET_STATE_OPCODE,
            build_customability_set_state_body(int(actor_id), record),
        )
        for record in records
    ]
    return packets, (
        "customability_set_restore records=%d ids=%s required_parts=%s configs=%s"
    ) % (
        len(records),
        tuple(record.set_record_id for record in records),
        tuple(record.required_part_count for record in records),
        tuple(record.part_set_index for record in records),
    )


__all__ = [
    "CUSTOMABILITY_SET_STATE_BODY_SIZE",
    "CUSTOMABILITY_SET_STATE_OPCODE",
    "build_customability_set_restore_packets",
    "build_customability_set_state_body",
    "prepare_customability_set_records",
]
