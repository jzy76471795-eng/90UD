"""Atomic persisted sorting for Stage3 inventory containers."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from game.character.item_metadata import (
    SLOT_RANGE_AVATAR_EMBLEM,
    SLOT_RANGE_CONSUMABLE,
    SLOT_RANGE_EQUIPMENT,
    SLOT_RANGE_MATERIAL,
    SLOT_RANGE_MATERIAL_EXPERT,
    SLOT_RANGE_QUEST,
    resolve_item_metadata,
)


MAIN_SORT_RANGES: dict[int, tuple[int, int]] = {
    1: SLOT_RANGE_EQUIPMENT,
    2: SLOT_RANGE_CONSUMABLE,
    3: SLOT_RANGE_MATERIAL,
    4: SLOT_RANGE_QUEST,
    9: SLOT_RANGE_AVATAR_EMBLEM,
    10: SLOT_RANGE_MATERIAL_EXPERT,
}


@dataclass(frozen=True)
class InventorySortResult:
    success: bool
    changed: bool
    list_type: int
    category: int
    start_slot: int
    end_slot: int
    item_count: int
    moved_count: int
    elapsed_ms: int
    reason: str


@dataclass(frozen=True)
class InventorySortLockResult:
    success: bool
    changed: bool
    list_type: int
    slot_index: int
    item_template_id: int
    sort_lock_flag: int
    reason: str


class _InventoryChanged(RuntimeError):
    pass


def _result(
    *,
    success: bool,
    changed: bool,
    list_type: int,
    category: int,
    start_slot: int = 0,
    end_slot: int = -1,
    item_count: int = 0,
    moved_count: int = 0,
    started: float,
    reason: str,
) -> InventorySortResult:
    return InventorySortResult(
        success=bool(success),
        changed=bool(changed),
        list_type=int(list_type) & 0xFF,
        category=int(category) & 0xFF,
        start_slot=int(start_slot),
        end_slot=int(end_slot),
        item_count=max(0, int(item_count)),
        moved_count=max(0, int(moved_count)),
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        reason=str(reason),
    )


def _sort_range(
    repo: Any,
    *,
    character_id: int,
    account_id: int,
    list_type: int,
    category: int,
) -> tuple[int, int] | None:
    lt = int(list_type) & 0xFF
    cat = int(category) & 0xFF
    if lt == int(repo.INVENTORY_LIST_MAIN):
        return MAIN_SORT_RANGES.get(cat)
    if lt == int(repo.INVENTORY_LIST_AVATAR):
        if cat != 8:
            return None
        expansion = int(repo.get_container_list_param(character_id, list_type=lt))
        end_slot = int(repo.BASE_AVATAR_SLOT_COUNT) + expansion - 1
        return (0, end_slot) if end_slot >= 0 else None
    if lt == int(repo.INVENTORY_LIST_PERSONAL_CARGO):
        if cat != 11:
            return None
        capacity = int(repo.get_container_list_param(character_id, list_type=lt))
        return (0, capacity - 1) if capacity > 0 else None
    if lt == int(repo.INVENTORY_LIST_ACCOUNT_CARGO):
        state = repo.get_account_cargo_state(account_id, ensure_open=True)
        capacity = int(state.get("selection_key") or 0)
        return (0, capacity - 1) if capacity > 0 else None
    return None


def _metadata_key(item_id: int) -> tuple[int, int, tuple[int, ...], int]:
    metadata = resolve_item_metadata(int(item_id))
    rarity = max(0, int(getattr(metadata, "rarity", 0) or 0))
    set_ids = tuple(
        int(value)
        for value in (getattr(metadata, "set_item_ids", ()) or ())
        if int(value) > 0
    )
    is_set_equipment = bool(
        set_ids
        and (
            bool(getattr(metadata, "is_equipment", False))
            or bool(getattr(metadata, "is_avatar", False))
        )
    )
    return (
        -rarity,
        0 if is_set_equipment else 1,
        set_ids if is_set_equipment else (),
        int(item_id) & 0xFFFFFFFF,
    )


def _load_character_rows(
    repo: Any,
    owner_id: int,
    list_type: int,
    start_slot: int,
    end_slot: int,
) -> list[tuple[int, int, int, int]]:
    with repo.open() as conn:
        rows = conn.execute(
            "SELECT item_uid, slot_index, item_template_id, sort_lock_flag "
            "FROM character_items WHERE character_id = ? AND list_type = ? "
            "AND slot_index BETWEEN ? AND ? ORDER BY slot_index, item_uid",
            (int(owner_id), int(list_type), int(start_slot), int(end_slot)),
        ).fetchall()
    return [
        (
            int(row[0]),
            int(row[1]),
            int(row[2]) & 0xFFFFFFFF,
            1 if int(row[3] or 0) else 0,
        )
        for row in rows
    ]


def _load_account_rows(
    repo: Any,
    owner_id: int,
    _list_type: int,
    start_slot: int,
    end_slot: int,
) -> list[tuple[int, int, int, int]]:
    with repo.open() as conn:
        repo._ensure_account_cargo_schema(conn)
        rows = conn.execute(
            "SELECT item_uid, slot_index, item_template_id "
            "FROM account_cargo_items WHERE account_id = ? "
            "AND slot_index BETWEEN ? AND ? ORDER BY slot_index, item_uid",
            (int(owner_id), int(start_slot), int(end_slot)),
        ).fetchall()
    return [
        (int(row[0]), int(row[1]), int(row[2]) & 0xFFFFFFFF, 0)
        for row in rows
    ]


def set_inventory_sort_lock(
    repo: Any,
    *,
    character_id: int,
    list_type: int,
    slot_index: int,
    locked: bool,
) -> InventorySortLockResult:
    """Atomically set or clear one main-bag or avatar sorting lock."""
    cid = int(character_id)
    lt = int(list_type) & 0xFF
    slot = int(slot_index)
    if cid <= 0:
        return InventorySortLockResult(
            False, False, lt, slot, 0, 0, "owner_missing"
        )
    if lt == int(repo.INVENTORY_LIST_MAIN):
        start_slot, end_slot = SLOT_RANGE_EQUIPMENT
        allowed_kinds = frozenset({"equipment"})
    elif lt == int(repo.INVENTORY_LIST_AVATAR):
        expansion = int(repo.get_container_list_param(cid, list_type=lt))
        start_slot = 0
        end_slot = int(repo.BASE_AVATAR_SLOT_COUNT) + expansion - 1
        allowed_kinds = frozenset({"equipment", "avatar"})
    else:
        return InventorySortLockResult(
            False, False, lt, slot, 0, 0, "unsupported_list_type"
        )
    if not start_slot <= slot <= end_slot:
        return InventorySortLockResult(
            False, False, lt, slot, 0, 0, "slot_out_of_range"
        )

    repo._ensure_schema()
    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT item_uid, item_template_id, item_kind, sort_lock_flag "
            "FROM character_items WHERE character_id = ? AND list_type = ? "
            "AND slot_index = ? ORDER BY item_uid LIMIT 1",
            (cid, lt, slot),
        ).fetchone()
        if row is None:
            return InventorySortLockResult(
                False, False, lt, slot, 0, 0, "slot_empty"
            )
        item_id = int(row[1]) & 0xFFFFFFFF
        if str(row[2] or "") not in allowed_kinds:
            return InventorySortLockResult(
                False, False, lt, slot, item_id, 0, "item_not_equipment"
            )
        current = 1 if int(row[3] or 0) else 0
        updated = 1 if bool(locked) else 0
        if current == updated:
            return InventorySortLockResult(
                True,
                False,
                lt,
                slot,
                item_id,
                updated,
                "already_set" if updated else "already_clear",
            )
        cursor = conn.execute(
            "UPDATE character_items SET sort_lock_flag = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
            "AND sort_lock_flag = ?",
            (updated, int(row[0]), current),
        )
        if int(cursor.rowcount or 0) != 1:
            raise _InventoryChanged("sort lock item changed")
    return InventorySortLockResult(
        True, True, lt, slot, item_id, updated, "set" if updated else "cleared"
    )


def _validate_snapshot(rows: list[tuple[int, int, int, int]]) -> str | None:
    slots: set[int] = set()
    for _uid, slot, item_id, _sort_lock_flag in rows:
        if slot in slots:
            return "duplicate_slot slot=%d" % slot
        if item_id <= 0:
            return "invalid_item_identity slot=%d" % slot
        slots.add(slot)
    return None


def _apply_character_plan(
    repo: Any,
    *,
    owner_id: int,
    list_type: int,
    start_slot: int,
    end_slot: int,
    snapshot: list[tuple[int, int, int, int]],
    plan: list[tuple[int, int, int, int, int]],
) -> None:
    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT item_uid, slot_index, item_template_id, sort_lock_flag "
            "FROM character_items WHERE character_id = ? AND list_type = ? "
            "AND slot_index BETWEEN ? AND ? ORDER BY slot_index, item_uid",
            (int(owner_id), int(list_type), int(start_slot), int(end_slot)),
        ).fetchall()
        current_snapshot = [
            (
                int(row[0]),
                int(row[1]),
                int(row[2]) & 0xFFFFFFFF,
                1 if int(row[3] or 0) else 0,
            )
            for row in current
        ]
        if current_snapshot != snapshot:
            raise _InventoryChanged("character inventory changed")
        minimum_row = conn.execute(
            "SELECT MIN(slot_index) FROM character_items "
            "WHERE character_id = ? AND list_type = ?",
            (int(owner_id), int(list_type)),
        ).fetchone()
        minimum = int(minimum_row[0]) if minimum_row and minimum_row[0] is not None else 0
        temp_start = min(-1, minimum - len(plan) - 1)
        for index, (item_uid, _slot, _item_id, _locked, _target) in enumerate(plan):
            conn.execute(
                "UPDATE character_items SET slot_index = ? WHERE item_uid = ?",
                (temp_start - index, int(item_uid)),
            )
        for item_uid, _slot, _item_id, _locked, target_slot in plan:
            conn.execute(
                "UPDATE character_items SET slot_index = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                (int(target_slot), int(item_uid)),
            )


def _apply_account_plan(
    repo: Any,
    *,
    owner_id: int,
    list_type: int,
    start_slot: int,
    end_slot: int,
    snapshot: list[tuple[int, int, int, int]],
    plan: list[tuple[int, int, int, int, int]],
) -> None:
    del list_type
    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        repo._ensure_account_cargo_schema(conn)
        current = conn.execute(
            "SELECT item_uid, slot_index, item_template_id "
            "FROM account_cargo_items WHERE account_id = ? "
            "AND slot_index BETWEEN ? AND ? ORDER BY slot_index, item_uid",
            (int(owner_id), int(start_slot), int(end_slot)),
        ).fetchall()
        current_snapshot = [
            (int(row[0]), int(row[1]), int(row[2]) & 0xFFFFFFFF, 0)
            for row in current
        ]
        if current_snapshot != snapshot:
            raise _InventoryChanged("account cargo changed")
        minimum_row = conn.execute(
            "SELECT MIN(slot_index) FROM account_cargo_items WHERE account_id = ?",
            (int(owner_id),),
        ).fetchone()
        minimum = int(minimum_row[0]) if minimum_row and minimum_row[0] is not None else 0
        temp_start = min(-1, minimum - len(plan) - 1)
        for index, (item_uid, _slot, _item_id, _locked, _target) in enumerate(plan):
            conn.execute(
                "UPDATE account_cargo_items SET slot_index = ? WHERE item_uid = ?",
                (temp_start - index, int(item_uid)),
            )
        for item_uid, _slot, _item_id, _locked, target_slot in plan:
            conn.execute(
                "UPDATE account_cargo_items SET slot_index = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                (int(target_slot), int(item_uid)),
            )
        conn.execute(
            "UPDATE account_cargo_state SET updated_at = CURRENT_TIMESTAMP "
            "WHERE account_id = ?",
            (int(owner_id),),
        )


def sort_inventory_container(
    repo: Any,
    *,
    character_id: int,
    account_id: int,
    list_type: int,
    category: int,
) -> InventorySortResult:
    """Compact one Stage3 inventory segment using cached PVF sort keys.

    PVF metadata resolution is deliberately completed before ``BEGIN IMMEDIATE``.
    Each unique template is resolved once per attempt, so SQLite write-lock time
    contains only the two-phase slot update.
    """
    started = time.perf_counter()
    cid = int(character_id)
    aid = int(account_id)
    lt = int(list_type) & 0xFF
    cat = int(category) & 0xFF
    if cid <= 0 or aid <= 0:
        return _result(
            success=False,
            changed=False,
            list_type=lt,
            category=cat,
            started=started,
            reason="owner_missing",
        )
    repo._ensure_schema()
    slot_range = _sort_range(
        repo,
        character_id=cid,
        account_id=aid,
        list_type=lt,
        category=cat,
    )
    if slot_range is None:
        return _result(
            success=False,
            changed=False,
            list_type=lt,
            category=cat,
            started=started,
            reason="unsupported_list_or_category",
        )
    start_slot, end_slot = map(int, slot_range)
    account_cargo = lt == int(repo.INVENTORY_LIST_ACCOUNT_CARGO)
    owner_id = aid if account_cargo else cid
    load_rows: Callable[..., list[tuple[int, int, int, int]]] = (
        _load_account_rows if account_cargo else _load_character_rows
    )
    apply_plan = _apply_account_plan if account_cargo else _apply_character_plan

    for _attempt in range(2):
        snapshot = load_rows(repo, owner_id, lt, start_slot, end_slot)
        invalid = _validate_snapshot(snapshot)
        if invalid is not None:
            return _result(
                success=False,
                changed=False,
                list_type=lt,
                category=cat,
                start_slot=start_slot,
                end_slot=end_slot,
                item_count=len(snapshot),
                started=started,
                reason=invalid,
            )
        metadata_keys = {
            item_id: _metadata_key(item_id)
            for item_id in {row[2] for row in snapshot if not row[3]}
        }
        locked_slots = {row[1] for row in snapshot if row[3]}
        ordered = sorted(
            (row for row in snapshot if not row[3]),
            key=lambda row: (
                metadata_keys[row[2]],
                int(row[1]),
                int(row[0]),
            ),
        )
        target_slots = (
            slot
            for slot in range(start_slot, end_slot + 1)
            if slot not in locked_slots
        )
        plan = [
            (item_uid, old_slot, item_id, sort_lock_flag, target_slot)
            for (item_uid, old_slot, item_id, sort_lock_flag), target_slot in zip(
                ordered,
                target_slots,
            )
        ]
        moved_count = sum(
            1
            for _uid, old_slot, _item_id, _locked, target_slot in plan
            if int(old_slot) != int(target_slot)
        )
        if moved_count == 0:
            return _result(
                success=True,
                changed=False,
                list_type=lt,
                category=cat,
                start_slot=start_slot,
                end_slot=end_slot,
                item_count=len(snapshot),
                moved_count=0,
                started=started,
                reason="already_sorted",
            )
        try:
            apply_plan(
                repo,
                owner_id=owner_id,
                list_type=lt,
                start_slot=start_slot,
                end_slot=end_slot,
                snapshot=snapshot,
                plan=plan,
            )
        except _InventoryChanged:
            continue
        except Exception as exc:
            return _result(
                success=False,
                changed=False,
                list_type=lt,
                category=cat,
                start_slot=start_slot,
                end_slot=end_slot,
                item_count=len(snapshot),
                moved_count=0,
                started=started,
                reason="sort_tx_error:%s:%s" % (type(exc).__name__, exc),
            )
        return _result(
            success=True,
            changed=True,
            list_type=lt,
            category=cat,
            start_slot=start_slot,
            end_slot=end_slot,
            item_count=len(snapshot),
            moved_count=moved_count,
            started=started,
            reason="sorted",
        )
    return _result(
        success=False,
        changed=False,
        list_type=lt,
        category=cat,
        start_slot=start_slot,
        end_slot=end_slot,
        started=started,
        reason="inventory_changed_twice",
    )


__all__ = [
    "InventorySortLockResult",
    "InventorySortResult",
    "MAIN_SORT_RANGES",
    "sort_inventory_container",
    "set_inventory_sort_lock",
]
