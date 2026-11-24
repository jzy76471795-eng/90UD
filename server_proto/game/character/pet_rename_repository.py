"""Atomic persistence for renaming the currently equipped creature."""
from __future__ import annotations

import sqlite3
from typing import Any

from game.character.character_db import CharacterRepository
from game.character.item_metadata import SLOT_RANGE_PET_CONSUMABLE


PET_LIST_TYPE = 7
EQUIPPED_CREATURE_SLOT = 26


class _PetRenameRollback(RuntimeError):
    pass


def load_pet_rename_context(
    repo: CharacterRepository,
    character_id: int,
    *,
    source_list_type: int,
    source_slot: int,
) -> dict[str, int]:
    """Load the two PVF identities that the handler must validate."""
    repo._ensure_schema()
    cid = int(character_id)
    list_type = int(source_list_type) & 0xFF
    slot = int(source_slot)
    context = {
        "source_item_id": 0,
        "source_count": 0,
        "pet_item_id": 0,
        "creature_key": 0,
    }
    if (
        cid <= 0
        or list_type != PET_LIST_TYPE
        or not SLOT_RANGE_PET_CONSUMABLE[0]
        <= slot
        <= SLOT_RANGE_PET_CONSUMABLE[1]
    ):
        return context

    with repo.open() as conn:
        source = conn.execute(
            "SELECT item_template_id, stack_count FROM character_items "
            "WHERE character_id = ? AND list_type = ? AND slot_index = ? "
            "LIMIT 1",
            (cid, list_type, slot),
        ).fetchone()
        equipped = conn.execute(
            "SELECT item_id, marker_16 FROM character_equipped_entries "
            "WHERE character_id = ? AND slot = ? LIMIT 1",
            (cid, EQUIPPED_CREATURE_SLOT),
        ).fetchone()
    if source is not None:
        context["source_item_id"] = int(source[0]) & 0xFFFFFFFF
        context["source_count"] = max(0, int(source[1] or 0))
    if equipped is not None:
        context["pet_item_id"] = int(equipped[0]) & 0xFFFFFFFF
        context["creature_key"] = max(0, int(equipped[1] or 0)) & 0xFFFFFFFF
    return context


def rename_equipped_creature(
    repo: CharacterRepository,
    character_id: int,
    *,
    source_list_type: int,
    source_slot: int,
    expected_source_item_id: int,
    expected_pet_item_id: int,
    expected_creature_key: int,
    name_bytes: bytes,
) -> tuple[bool, str, dict[str, Any]]:
    """Persist the name and consume exactly one rename card in one transaction."""
    repo._ensure_schema()
    cid = int(character_id)
    list_type = int(source_list_type) & 0xFF
    slot = int(source_slot)
    source_item_id = int(expected_source_item_id) & 0xFFFFFFFF
    pet_item_id = int(expected_pet_item_id) & 0xFFFFFFFF
    creature_key = int(expected_creature_key) & 0xFFFFFFFF
    raw_name = bytes(name_bytes or b"")
    detail: dict[str, Any] = {
        "character_id": cid,
        "source_list_type": list_type,
        "source_slot": slot,
        "source_item_id": source_item_id,
        "pet_item_id": pet_item_id,
        "creature_key": creature_key,
        "source_original": 0,
        "source_remaining": 0,
        "name_bytes": raw_name,
    }
    if cid <= 0:
        return False, "character_invalid", detail
    if list_type != PET_LIST_TYPE:
        return False, "source_list_invalid", detail
    if not SLOT_RANGE_PET_CONSUMABLE[0] <= slot <= SLOT_RANGE_PET_CONSUMABLE[1]:
        return False, "source_slot_invalid", detail
    if source_item_id <= 0 or pet_item_id <= 0 or creature_key <= 0:
        return False, "identity_invalid", detail
    if len(raw_name) > 30:
        return False, "name_too_long", detail

    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            character = conn.execute(
                "SELECT 1 FROM characters "
                "WHERE character_id = ? AND delete_flag = 0 LIMIT 1",
                (cid,),
            ).fetchone()
            if character is None:
                return False, "character_missing", detail

            source = conn.execute(
                "SELECT item_uid, item_template_id, stack_count "
                "FROM character_items "
                "WHERE character_id = ? AND list_type = ? AND slot_index = ? "
                "LIMIT 1",
                (cid, list_type, slot),
            ).fetchone()
            if source is None:
                return False, "source_missing", detail
            source_uid = int(source[0])
            actual_source_item_id = int(source[1]) & 0xFFFFFFFF
            original = max(0, int(source[2] or 0))
            detail["source_original"] = original
            detail["source_remaining"] = original
            if actual_source_item_id != source_item_id:
                return False, "source_changed", detail
            if original <= 0:
                return False, "source_empty", detail

            equipped = conn.execute(
                "SELECT item_id, marker_16 FROM character_equipped_entries "
                "WHERE character_id = ? AND slot = ? LIMIT 1",
                (cid, EQUIPPED_CREATURE_SLOT),
            ).fetchone()
            if equipped is None:
                return False, "pet_not_equipped", detail
            actual_pet_item_id = int(equipped[0]) & 0xFFFFFFFF
            actual_creature_key = max(0, int(equipped[1] or 0)) & 0xFFFFFFFF
            if actual_pet_item_id != pet_item_id or actual_creature_key != creature_key:
                return False, "pet_changed", detail

            state = conn.execute(
                "SELECT 1 FROM character_creatures "
                "WHERE character_id = ? AND creature_key = ? LIMIT 1",
                (cid, creature_key),
            ).fetchone()
            if state is None:
                return False, "pet_state_missing", detail

            renamed = conn.execute(
                "UPDATE character_creatures SET creature_text = ? "
                "WHERE character_id = ? AND creature_key = ?",
                (sqlite3.Binary(raw_name), cid, creature_key),
            )
            if int(renamed.rowcount or 0) != 1:
                raise _PetRenameRollback("creature_update_lost")

            remaining = original - 1
            if remaining <= 0:
                consumed = conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ?",
                    (source_uid,),
                )
            else:
                consumed = conn.execute(
                    "UPDATE character_items "
                    "SET stack_count = ?, instance_value = ?, "
                    "pet_serial_or_handle = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE item_uid = ?",
                    (remaining, remaining, remaining, source_uid),
                )
            if int(consumed.rowcount or 0) != 1:
                raise _PetRenameRollback("source_update_lost")
            detail["source_remaining"] = remaining
            return True, "renamed", detail
    except _PetRenameRollback as exc:
        return False, str(exc), detail
    except Exception as exc:
        return False, "pet_rename_error:%s:%s" % (type(exc).__name__, exc), detail


__all__ = [
    "EQUIPPED_CREATURE_SLOT",
    "PET_LIST_TYPE",
    "load_pet_rename_context",
    "rename_equipped_creature",
]
