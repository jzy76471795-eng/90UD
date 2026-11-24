"""Atomic inventory transaction for bead enchantment replacement."""
from __future__ import annotations

import sqlite3
from typing import Any

from game.character.enchant_by_bead import merge_enchant_state, parse_enchant_state


MAIN_LIST_TYPE = 0
EQUIPMENT_LIST_TYPE = 3
PET_LIST_TYPE = 7


class _TransactionReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def apply_enchant_by_bead(
    repo: Any,
    *,
    character_id: int,
    bead_list_type: int,
    bead_slot: int,
    expected_bead_item_id: int,
    expected_bead_extra_json: Any,
    target_list_type: int,
    target_slot: int,
    expected_target_item_id: int,
    expected_target_extra_json: Any,
    card_item_id: int,
    upgrade_count: int,
) -> tuple[bool, str, dict[str, Any]]:
    """Consume one bead and replace the target's complete enchant object."""
    cid = int(character_id)
    bead_list = int(bead_list_type)
    bead_slot_i = int(bead_slot)
    bead_item_id = int(expected_bead_item_id) & 0xFFFFFFFF
    target_list = int(target_list_type)
    target_slot_i = int(target_slot)
    target_item_id = int(expected_target_item_id) & 0xFFFFFFFF
    card_id = int(card_item_id) & 0xFFFFFFFF
    upgrade = int(upgrade_count)
    expected_bead_extra = str(expected_bead_extra_json or "{}")
    expected_target_extra = str(expected_target_extra_json or "{}")
    empty: dict[str, Any] = {
        "bead_remaining": 0,
        "old_card_item_id": 0,
        "old_upgrade_count": 0,
        "new_card_item_id": card_id,
        "new_upgrade_count": max(0, min(0xFF, upgrade)),
        "changed_main_slots": [],
        "changed_target_slots": [],
    }
    if cid <= 0:
        return False, "character_missing", empty
    if bead_list != MAIN_LIST_TYPE:
        return False, "bead_list_unsupported", empty
    if target_list not in {MAIN_LIST_TYPE, EQUIPMENT_LIST_TYPE, PET_LIST_TYPE}:
        return False, "target_list_unsupported", empty
    if not 0 <= bead_slot_i <= 0xFFFF or not 0 <= target_slot_i <= 0xFFFF:
        return False, "invalid_slot", empty
    if bead_slot_i == 0 or (
        target_list == bead_list and target_slot_i == bead_slot_i
    ):
        return False, "invalid_slot_identity", empty
    if bead_item_id <= 0 or target_item_id <= 0 or card_id <= 0:
        return False, "invalid_item_identity", empty
    if not 0 <= upgrade <= 0xFF:
        return False, "invalid_upgrade_count", empty

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

            bead_row = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, stack_count, extra_json "
                "FROM character_items WHERE character_id = ? AND list_type = 0 "
                "AND slot_index = ?",
                (cid, bead_slot_i),
            ).fetchone()
            if bead_row is None:
                raise _TransactionReject("bead_slot_empty")
            if (
                (int(bead_row[1]) & 0xFFFFFFFF) != bead_item_id
                or str(bead_row[2] or "") in {"equipment", "avatar", "pet"}
            ):
                raise _TransactionReject("bead_item_mismatch")
            bead_before = max(0, int(bead_row[3] or 0))
            if bead_before < 1:
                raise _TransactionReject("bead_insufficient")
            if str(bead_row[4] or "{}") != expected_bead_extra:
                raise _TransactionReject("bead_state_changed")

            if target_list in {MAIN_LIST_TYPE, PET_LIST_TYPE}:
                target_row = conn.execute(
                    "SELECT item_uid, item_template_id, item_kind, extra_json "
                    "FROM character_items WHERE character_id = ? AND list_type = ? "
                    "AND slot_index = ?",
                    (cid, target_list, target_slot_i),
                ).fetchone()
                if target_row is None:
                    raise _TransactionReject("target_slot_empty")
                target_kind = str(target_row[2] or "")
                valid_kind = (
                    target_kind == "equipment"
                    if target_list == MAIN_LIST_TYPE
                    else target_kind in {"pet", "equipment"}
                )
                if (
                    (int(target_row[1]) & 0xFFFFFFFF) != target_item_id
                    or not valid_kind
                ):
                    raise _TransactionReject("target_item_mismatch")
                target_extra = str(target_row[3] or "{}")
                if target_extra != expected_target_extra:
                    raise _TransactionReject("target_state_changed")
                target_uid = int(target_row[0])
                target_table = "character_items"
            else:
                target_row = conn.execute(
                    "SELECT item_id, extra_json FROM character_equipped_entries "
                    "WHERE character_id = ? AND slot = ?",
                    (cid, target_slot_i),
                ).fetchone()
                if target_row is None:
                    raise _TransactionReject("target_slot_empty")
                if (int(target_row[0]) & 0xFFFFFFFF) != target_item_id:
                    raise _TransactionReject("target_item_mismatch")
                target_extra = str(target_row[1] or "{}")
                if target_extra != expected_target_extra:
                    raise _TransactionReject("target_state_changed")
                target_uid = 0
                target_table = "character_equipped_entries"

            old_card_id, old_upgrade = parse_enchant_state(target_extra)
            new_target_extra = merge_enchant_state(
                target_extra,
                card_id,
                upgrade,
            )
            if target_table == "character_items":
                conn.execute(
                    "UPDATE character_items SET extra_json = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                    (new_target_extra, target_uid),
                )
            else:
                conn.execute(
                    "UPDATE character_equipped_entries SET extra_json = ? "
                    "WHERE character_id = ? AND slot = ?",
                    (new_target_extra, cid, target_slot_i),
                )

            bead_remaining = bead_before - 1
            if bead_remaining == 0:
                conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ?",
                    (int(bead_row[0]),),
                )
            else:
                conn.execute(
                    "UPDATE character_items SET stack_count = ?, instance_value = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                    (bead_remaining, bead_remaining, int(bead_row[0])),
                )

            detail = {
                "bead_remaining": bead_remaining,
                "old_card_item_id": old_card_id,
                "old_upgrade_count": old_upgrade,
                "new_card_item_id": card_id,
                "new_upgrade_count": upgrade,
                "target_extra_json": new_target_extra,
                "changed_main_slots": [bead_slot_i]
                + ([target_slot_i] if target_list == MAIN_LIST_TYPE else []),
                "changed_target_slots": [target_slot_i],
            }
            return True, (
                "enchant_replaced target=%d list=%d slot=%d card=%d:%d->%d:%d "
                "bead=%d %d->%d"
                % (
                    target_item_id,
                    target_list,
                    target_slot_i,
                    old_card_id,
                    old_upgrade,
                    card_id,
                    upgrade,
                    bead_item_id,
                    bead_before,
                    bead_remaining,
                )
            ), detail
    except _TransactionReject as exc:
        detail = dict(empty)
        detail.update(exc.detail)
        return False, exc.reason, detail
    except sqlite3.Error as exc:
        return False, "enchant_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty


__all__ = [
    "EQUIPMENT_LIST_TYPE",
    "MAIN_LIST_TYPE",
    "PET_LIST_TYPE",
    "apply_enchant_by_bead",
]
