"""Repository transaction for emblem changes on worn avatar entries."""
from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from game.chain_loader import load_chain


class _TransactionReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def apply_equipped_avatar_emblems(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_target_item_id: int,
    expected_target_extra_json: Any,
    materials: Sequence[tuple[int, int]],
    new_socket_blob: bytes,
) -> tuple[bool, str, dict[str, Any]]:
    """Consume new emblems and replace the socket blob on one list3 avatar."""
    socket_helper = load_chain("helper-avatar_socket.py")
    empty: dict[str, Any] = {
        "material_remaining": {},
        "changed_main_slots": [],
        "changed_equipment_slots": [],
    }
    try:
        socket_blob = socket_helper.validate_socket_blob(bytes(new_socket_blob))
    except (TypeError, ValueError) as exc:
        return False, "invalid_socket_blob:%s" % exc, empty

    cid = int(character_id)
    target = int(target_slot)
    target_item_id = int(expected_target_item_id) & 0xFFFFFFFF
    expected_extra = str(expected_target_extra_json or "{}")
    requested = tuple(
        (int(slot), int(item_id) & 0xFFFFFFFF)
        for slot, item_id in materials
    )
    if cid <= 0:
        return False, "character_missing", empty
    if not 0 <= target <= 0xFF or target_item_id <= 0:
        return False, "invalid_target", empty
    if not requested or len(requested) > 5:
        return False, "invalid_material_count", empty
    material_item_by_slot: dict[int, int] = {}
    material_count_by_slot: dict[int, int] = {}
    for material_slot, material_item_id in requested:
        if not 0 <= material_slot <= 0xFFFF or material_item_id <= 0:
            return False, "invalid_material_identity", empty
        previous_item_id = material_item_by_slot.get(material_slot)
        if previous_item_id is not None and previous_item_id != material_item_id:
            return False, "invalid_material_identity", empty
        material_item_by_slot[material_slot] = material_item_id
        material_count_by_slot[material_slot] = (
            material_count_by_slot.get(material_slot, 0) + 1
        )

    repo._ensure_schema()
    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT 1 FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if owner is None:
                raise _TransactionReject("character_missing")

            target_row = conn.execute(
                "SELECT item_id, extra_json FROM character_equipped_entries "
                "WHERE character_id = ? AND slot = ?",
                (cid, target),
            ).fetchone()
            if target_row is None:
                raise _TransactionReject("target_slot_empty")
            if (int(target_row[0]) & 0xFFFFFFFF) != target_item_id:
                raise _TransactionReject("target_item_mismatch")
            current_extra = str(target_row[1] or "{}")
            if current_extra != expected_extra:
                raise _TransactionReject("target_socket_changed")

            material_rows: list[tuple[int, int, int]] = []
            for material_slot, material_item_id in material_item_by_slot.items():
                row = conn.execute(
                    "SELECT item_uid, item_template_id, item_kind, stack_count "
                    "FROM character_items WHERE character_id = ? "
                    "AND list_type = 0 AND slot_index = ?",
                    (cid, material_slot),
                ).fetchone()
                if row is None:
                    raise _TransactionReject(
                        "material_slot_empty",
                        {"failed_material_slot": material_slot},
                    )
                if (
                    (int(row[1]) & 0xFFFFFFFF) != material_item_id
                    or str(row[2] or "") in {"avatar", "equipment"}
                ):
                    raise _TransactionReject(
                        "material_item_mismatch",
                        {"failed_material_slot": material_slot},
                    )
                before = max(0, int(row[3] or 0))
                required_count = material_count_by_slot[material_slot]
                if before < required_count:
                    raise _TransactionReject(
                        "material_insufficient",
                        {"failed_material_slot": material_slot},
                    )
                material_rows.append((int(row[0]), material_slot, before))

            remaining_by_slot: dict[int, int] = {}
            for uid, material_slot, before in material_rows:
                remaining = before - material_count_by_slot[material_slot]
                remaining_by_slot[material_slot] = remaining
                if remaining == 0:
                    conn.execute(
                        "DELETE FROM character_items WHERE item_uid = ?",
                        (uid,),
                    )
                else:
                    conn.execute(
                        "UPDATE character_items SET stack_count = ?, "
                        "instance_value = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE item_uid = ?",
                        (remaining, remaining, uid),
                    )

            new_extra = socket_helper.merge_avatar_socket_blob(
                current_extra,
                socket_blob,
            )
            conn.execute(
                "UPDATE character_equipped_entries SET extra_json = ? "
                "WHERE character_id = ? AND slot = ?",
                (new_extra, cid, target),
            )
            detail = {
                "material_remaining": remaining_by_slot,
                "changed_main_slots": list(material_item_by_slot),
                "changed_equipment_slots": [target],
                "target_extra_json": new_extra,
            }
            return True, (
                "equipped_avatar_emblems target=%d slot=%d materials=%s"
                % (
                    target_item_id,
                    target,
                    [
                        (slot, item_id, remaining_by_slot[slot])
                        for slot, item_id in requested
                    ],
                )
            ), detail
    except _TransactionReject as exc:
        detail = dict(empty)
        detail.update(exc.detail)
        return False, exc.reason, detail
    except sqlite3.Error as exc:
        return False, "equipped_avatar_emblem_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty


__all__ = ["apply_equipped_avatar_emblems"]
