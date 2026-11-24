"""Persistent equipment-protection lock state for Stage3 inventory items."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


SUPPORTED_EQUIPMENT_LOCK_LIST_TYPES = frozenset({0, 1, 3, 36})
LOCKABLE_ITEM_KINDS = frozenset({"equipment", "avatar", "pet"})
LOCKABLE_ITEM_KINDS_BY_LIST_TYPE = {
    0: LOCKABLE_ITEM_KINDS,
    1: frozenset({"equipment", "avatar"}),
    36: frozenset({"equipment"}),
}


@dataclass(frozen=True)
class EquipmentLockResult:
    success: bool
    changed: bool
    reason: str
    list_type: int
    slot_index: int
    item_template_id: int = 0
    equipment_lock_id: int = 0


@dataclass(frozen=True)
class EquipmentLockEntry:
    list_type: int
    slot_index: int
    equipment_lock_id: int


def _result(
    *,
    success: bool,
    changed: bool,
    reason: str,
    list_type: int,
    slot_index: int,
    item_template_id: int = 0,
    equipment_lock_id: int = 0,
) -> EquipmentLockResult:
    return EquipmentLockResult(
        success=bool(success),
        changed=bool(changed),
        reason=str(reason),
        list_type=int(list_type) & 0xFF,
        slot_index=int(slot_index),
        item_template_id=int(item_template_id) & 0xFFFFFFFF,
        equipment_lock_id=max(0, int(equipment_lock_id)) & 0xFF,
    )


def _allocate_lock_id(conn: Any, character_id: int) -> int:
    cid = int(character_id)
    used = {
        int(row[0]) & 0xFF
        for row in conn.execute(
            "SELECT equipment_lock_id FROM character_items "
            "WHERE character_id = ? AND equipment_lock_id BETWEEN 1 AND 255",
            (cid,),
        ).fetchall()
    }
    used.update(
        int(row[0]) & 0xFF
        for row in conn.execute(
            "SELECT equipment_lock_id FROM character_equipped_entries "
            "WHERE character_id = ? AND equipment_lock_id BETWEEN 1 AND 255",
            (cid,),
        ).fetchall()
    )
    for candidate in range(1, 256):
        if candidate not in used:
            return candidate
    return 0


def set_equipment_lock(
    repo: Any,
    *,
    character_id: int,
    list_type: int,
    slot_index: int,
    locked: bool,
) -> EquipmentLockResult:
    """Set or clear one lock by its current Stage3 container position."""
    cid = int(character_id)
    lt = int(list_type) & 0xFF
    slot = int(slot_index)
    if cid <= 0:
        return _result(
            success=False,
            changed=False,
            reason="character_missing",
            list_type=lt,
            slot_index=slot,
        )
    if lt not in SUPPORTED_EQUIPMENT_LOCK_LIST_TYPES:
        return _result(
            success=False,
            changed=False,
            reason="unsupported_list_type",
            list_type=lt,
            slot_index=slot,
        )
    if not -0x8000 <= slot <= 0x7FFF:
        return _result(
            success=False,
            changed=False,
            reason="slot_out_of_range",
            list_type=lt,
            slot_index=slot,
        )

    repo._ensure_schema()
    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if lt == 3:
            row = conn.execute(
                "SELECT item_id, equipment_lock_id "
                "FROM character_equipped_entries "
                "WHERE character_id = ? AND slot = ?",
                (cid, slot),
            ).fetchone()
            if row is None:
                return _result(
                    success=False,
                    changed=False,
                    reason="item_missing",
                    list_type=lt,
                    slot_index=slot,
                )
            item_id = int(row[0]) & 0xFFFFFFFF
            current = int(row[1] or 0) & 0xFF
            table = "character_equipped_entries"
            update_set = "equipment_lock_id = ?"
            identity_sql = "character_id = ? AND slot = ?"
            identity = (cid, slot)
        else:
            row = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, equipment_lock_id "
                "FROM character_items WHERE character_id = ? "
                "AND list_type = ? AND slot_index = ? "
                "ORDER BY item_uid LIMIT 1",
                (cid, lt, slot),
            ).fetchone()
            if row is None:
                return _result(
                    success=False,
                    changed=False,
                    reason="item_missing",
                    list_type=lt,
                    slot_index=slot,
                )
            kind = str(row[2] or "")
            if kind not in LOCKABLE_ITEM_KINDS_BY_LIST_TYPE.get(lt, frozenset()):
                return _result(
                    success=False,
                    changed=False,
                    reason="item_not_equipment",
                    list_type=lt,
                    slot_index=slot,
                    item_template_id=int(row[1]),
                )
            item_id = int(row[1]) & 0xFFFFFFFF
            current = int(row[3] or 0) & 0xFF
            table = "character_items"
            update_set = (
                "equipment_lock_id = ?, updated_at = CURRENT_TIMESTAMP"
            )
            identity_sql = "item_uid = ?"
            identity = (int(row[0]),)

        if bool(locked) == (current > 0):
            return _result(
                success=True,
                changed=False,
                reason="already_locked" if current else "already_unlocked",
                list_type=lt,
                slot_index=slot,
                item_template_id=item_id,
                equipment_lock_id=current,
            )

        target = _allocate_lock_id(conn, cid) if locked else 0
        if locked and target <= 0:
            return _result(
                success=False,
                changed=False,
                reason="lock_id_exhausted",
                list_type=lt,
                slot_index=slot,
                item_template_id=item_id,
            )
        updated = conn.execute(
            "UPDATE %s SET %s WHERE %s AND equipment_lock_id = ?"
            % (table, update_set, identity_sql),
            (target,) + identity + (current,),
        )
        if int(updated.rowcount or 0) != 1:
            return _result(
                success=False,
                changed=False,
                reason="item_changed",
                list_type=lt,
                slot_index=slot,
                item_template_id=item_id,
                equipment_lock_id=current,
            )
        return _result(
            success=True,
            changed=True,
            reason="locked" if locked else "unlocked",
            list_type=lt,
            slot_index=slot,
            item_template_id=item_id,
            equipment_lock_id=target,
        )


def list_equipment_locks(
    repo: Any,
    *,
    character_id: int,
    list_types: Iterable[int] = (0, 3),
) -> list[EquipmentLockEntry]:
    cid = int(character_id)
    wanted = {
        int(value) & 0xFF
        for value in list_types
        if int(value) & 0xFF in SUPPORTED_EQUIPMENT_LOCK_LIST_TYPES
    }
    if cid <= 0 or not wanted:
        return []
    repo._ensure_schema()
    out: list[EquipmentLockEntry] = []
    with repo.open() as conn:
        inventory_list_types = sorted(wanted - {3})
        if inventory_list_types:
            placeholders = ",".join("?" for _value in inventory_list_types)
            rows = conn.execute(
                "SELECT list_type, slot_index, equipment_lock_id "
                "FROM character_items WHERE character_id = ? "
                "AND list_type IN (%s) "
                "AND equipment_lock_id BETWEEN 1 AND 255 "
                "ORDER BY list_type, slot_index, item_uid" % placeholders,
                (cid,) + tuple(inventory_list_types),
            ).fetchall()
            out.extend(
                EquipmentLockEntry(
                    list_type=int(row[0]) & 0xFF,
                    slot_index=int(row[1]),
                    equipment_lock_id=int(row[2]) & 0xFF,
                )
                for row in rows
            )
        if 3 in wanted:
            rows = conn.execute(
                "SELECT slot, equipment_lock_id "
                "FROM character_equipped_entries WHERE character_id = ? "
                "AND equipment_lock_id BETWEEN 1 AND 255 ORDER BY slot",
                (cid,),
            ).fetchall()
            out.extend(
                EquipmentLockEntry(
                    list_type=3,
                    slot_index=int(row[0]),
                    equipment_lock_id=int(row[1]) & 0xFF,
                )
                for row in rows
            )
    out.sort(key=lambda entry: (entry.list_type, entry.slot_index))
    return out


__all__ = [
    "EquipmentLockEntry",
    "EquipmentLockResult",
    "SUPPORTED_EQUIPMENT_LOCK_LIST_TYPES",
    "list_equipment_locks",
    "set_equipment_lock",
]
