"""Atomic list0/list1 settlement for ordinary avatar compounding."""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from game.chain_loader import load_chain
from game.character.character_db import EQUIP_TOP_QUALITY_SEED
from game.character.item_metadata import ItemMetadata


_avatar_socket = load_chain("helper-avatar_socket.py")


class _AvatarCompoundReject(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def _empty_detail() -> dict[str, Any]:
    return {
        "source_rows": [],
        "result_row": None,
        "material_remaining": 0,
    }


def apply_avatar_compound(
    repo: Any,
    *,
    character_id: int,
    compounder_slot: int,
    expected_compounder_item_id: int,
    first_avatar_slot: int,
    expected_first_avatar_item_id: int,
    second_avatar_slot: int,
    expected_second_avatar_item_id: int,
    result_metadata: ItemMetadata,
    result_option: int = 0,
    now_unix: int = 0,
) -> tuple[bool, str, dict[str, Any]]:
    cid = int(character_id)
    binder_slot = int(compounder_slot)
    first_slot = int(first_avatar_slot)
    second_slot = int(second_avatar_slot)
    binder_item_id = int(expected_compounder_item_id) & 0xFFFFFFFF
    first_item_id = int(expected_first_avatar_item_id) & 0xFFFFFFFF
    second_item_id = int(expected_second_avatar_item_id) & 0xFFFFFFFF
    result_item_id = int(result_metadata.item_id) & 0xFFFFFFFF
    option = max(0, int(result_option)) & 0xFF
    now = int(time.time()) if int(now_unix or 0) <= 0 else int(now_unix)
    empty = _empty_detail()

    if (
        cid <= 0
        or first_slot == second_slot
        or not all(0 <= value <= 0xFFFF for value in (binder_slot, first_slot, second_slot))
        or min(binder_item_id, first_item_id, second_item_id, result_item_id) <= 0
    ):
        return False, "invalid_request", empty
    if (
        int(result_metadata.item_id) != result_item_id
        or not result_metadata.is_avatar
        or int(result_metadata.inventory_list_type()) != 1
    ):
        return False, "invalid_result_metadata", empty

    result_extra_json = _avatar_socket.merge_default_avatar_extra(
        None,
        result_metadata,
    )
    try:
        repo._ensure_schema()
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT 1 FROM characters WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if owner is None:
                raise _AvatarCompoundReject("character_missing")

            binder = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, stack_count "
                "FROM character_items WHERE character_id = ? "
                "AND list_type = 0 AND slot_index = ?",
                (cid, binder_slot),
            ).fetchone()
            if binder is None:
                raise _AvatarCompoundReject("compounder_slot_empty")
            if (
                (int(binder[1]) & 0xFFFFFFFF) != binder_item_id
                or str(binder[2] or "") != "stackable"
                or int(binder[3] or 0) <= 0
            ):
                raise _AvatarCompoundReject("compounder_item_mismatch")

            source_rows = []
            for slot, expected_item_id in (
                (first_slot, first_item_id),
                (second_slot, second_item_id),
            ):
                row = conn.execute(
                    "SELECT item_uid, item_template_id, item_kind, stack_count, "
                    "equipment_lock_id, expire_time FROM character_items "
                    "WHERE character_id = ? AND list_type = 1 AND slot_index = ?",
                    (cid, slot),
                ).fetchone()
                if row is None:
                    raise _AvatarCompoundReject("avatar_slot_empty")
                if (
                    (int(row[1]) & 0xFFFFFFFF) != expected_item_id
                    or str(row[2] or "") not in {"avatar", "equipment"}
                    or int(row[3] or 0) != 1
                ):
                    raise _AvatarCompoundReject("avatar_item_mismatch")
                if int(row[4] or 0) != 0:
                    raise _AvatarCompoundReject("avatar_equipment_locked")
                expire_time = max(0, int(row[5] or 0))
                if expire_time and expire_time <= now:
                    raise _AvatarCompoundReject("avatar_expired")
                source_rows.append(
                    {
                        "item_uid": int(row[0]),
                        "list_type": 1,
                        "slot_index": slot,
                        "item_template_id": expected_item_id,
                        "remove_count": 1,
                    }
                )

            for source in source_rows:
                deleted = conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = 1 AND slot_index = ? "
                    "AND item_template_id = ? AND stack_count = 1",
                    (
                        int(source["item_uid"]),
                        cid,
                        int(source["slot_index"]),
                        int(source["item_template_id"]),
                    ),
                )
                if int(deleted.rowcount or 0) != 1:
                    raise _AvatarCompoundReject("avatar_item_changed")

            binder_count = int(binder[3])
            material_remaining = binder_count - 1
            if material_remaining > 0:
                updated = conn.execute(
                    "UPDATE character_items SET stack_count = ?, instance_value = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                    "AND item_template_id = ? AND stack_count = ?",
                    (
                        material_remaining,
                        material_remaining,
                        int(binder[0]),
                        cid,
                        binder_slot,
                        binder_item_id,
                        binder_count,
                    ),
                )
            else:
                updated = conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                    "AND item_template_id = ? AND stack_count = 1",
                    (int(binder[0]), cid, binder_slot, binder_item_id),
                )
            if int(updated.rowcount or 0) != 1:
                raise _AvatarCompoundReject("compounder_item_changed")

            result_slot = first_slot
            conn.execute(
                "INSERT INTO character_items "
                "(owner_scope, owner_id, character_id, list_type, slot_index, "
                "item_template_id, item_kind, stack_count, instance_value, "
                "durability, seal_flag, option_value, expire_time, marker_16, "
                "pet_serial_or_handle, extra_json) "
                "VALUES ('character', ?, ?, 1, ?, ?, 'avatar', 1, ?, "
                "0, 0, ?, 0, -1, 0, ?)",
                (
                    cid,
                    cid,
                    result_slot,
                    result_item_id,
                    EQUIP_TOP_QUALITY_SEED,
                    option,
                    result_extra_json,
                ),
            )

            response_sources = [
                {
                    "list_type": int(source["list_type"]),
                    "slot_index": int(source["slot_index"]),
                    "item_template_id": int(source["item_template_id"]),
                    "remove_count": 1,
                }
                for source in source_rows
            ]
            response_sources.append(
                {
                    "list_type": 0,
                    "slot_index": binder_slot,
                    "item_template_id": binder_item_id,
                    "remove_count": 1,
                }
            )
            result_row = {
                "list_type": 1,
                "slot_index": result_slot,
                "item_template_id": result_item_id,
                "instance_value": EQUIP_TOP_QUALITY_SEED,
                # Stage3 passes this field to the avatar instance constructor.
                # It is remaining lifetime on list1; zero means permanent.
                "wire_value": 0,
                "option_value": option,
                "extra_json": result_extra_json,
            }
            detail = {
                "source_rows": response_sources,
                "result_row": result_row,
                "material_remaining": material_remaining,
            }
            return True, (
                "avatar_compound_ok sources=%s binder=(%d,%d->%d) result=(%d,%d)"
                % (
                    [(row["slot_index"], row["item_template_id"]) for row in source_rows],
                    binder_slot,
                    binder_item_id,
                    material_remaining,
                    result_slot,
                    result_item_id,
                )
            ), detail
    except _AvatarCompoundReject as exc:
        return False, exc.reason, empty
    except sqlite3.Error as exc:
        return False, "avatar_compound_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty


__all__ = ["apply_avatar_compound"]
