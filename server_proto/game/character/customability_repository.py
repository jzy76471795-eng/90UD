"""Atomic persistence and material settlement for Aiken equipment changes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.character.customability_index import CustomAbilityMaterialCost
from game.character.customability_state import (
    CustomAbilityState,
    merge_customability_state,
    parse_customability_state,
)


@dataclass(frozen=True)
class CustomAbilityOptionSettlement:
    target_slot: int
    target_item_id: int
    option_line: int
    old_option_id: int
    new_option_id: int
    grade: int
    material_item_id: int
    material_cost: int
    material_remaining: int
    changed_material_slots: tuple[int, ...]


@dataclass(frozen=True)
class CustomAbilityUpgradeSettlement:
    target_slot: int
    target_item_id: int
    old_grade: int
    new_grade: int
    material_item_id: int
    material_cost: int
    material_remaining: int
    changed_material_slots: tuple[int, ...]


@dataclass(frozen=True)
class CustomAbilitySetTarget:
    slot: int
    item_id: int
    expected_state: CustomAbilityState


@dataclass(frozen=True)
class CustomAbilitySetRecord:
    set_record_id: int
    required_part_count: int
    part_set_index: int
    option_ids: tuple[int, ...]
    set_grade: int


@dataclass(frozen=True)
class CustomAbilitySetSettlement:
    target_slots: tuple[int, ...]
    target_item_ids: tuple[int, ...]
    record: CustomAbilitySetRecord
    material_item_id: int
    material_cost: int
    material_remaining: int
    changed_material_slots: tuple[int, ...]
    changed_inventory_slots: tuple[int, ...]
    changed_equipment_slots: tuple[int, ...]
    removed_record_ids: tuple[int, ...]


class _CustomAbilityReject(RuntimeError):
    pass


def _load_target(
    conn: Any,
    *,
    character_id: int,
    target_slot: int,
    target_item_id: int,
    expected_state: CustomAbilityState,
) -> tuple[Any, CustomAbilityState]:
    target = conn.execute(
        "SELECT item_uid, item_template_id, item_kind, stack_count, "
        "extra_json FROM character_items "
        "WHERE character_id = ? AND list_type = 0 AND slot_index = ?",
        (character_id, target_slot),
    ).fetchone()
    if target is None:
        raise _CustomAbilityReject("target_slot_empty")
    if (
        (int(target[1]) & 0xFFFFFFFF) != target_item_id
        or str(target[2] or "") != "equipment"
        or int(target[3] or 0) != 1
    ):
        raise _CustomAbilityReject("target_item_changed")
    current_state = parse_customability_state(target[4])
    if current_state != expected_state:
        raise _CustomAbilityReject("target_state_changed")
    return target, current_state


def _consume_material_stacks(
    conn: Any,
    *,
    character_id: int,
    target_slot: int,
    material_item_id: int,
    material_count: int,
) -> tuple[int, int, tuple[int, ...]]:
    material_rows = conn.execute(
        "SELECT item_uid, slot_index, item_kind, stack_count "
        "FROM character_items WHERE character_id = ? AND list_type = 0 "
        "AND item_template_id = ? AND slot_index != ? "
        "ORDER BY slot_index, item_uid",
        (character_id, material_item_id, target_slot),
    ).fetchall()
    valid_material_rows = tuple(
        row for row in material_rows
        if str(row[2] or "") == "stackable" and int(row[3] or 0) > 0
    )
    material_before = sum(int(row[3]) for row in valid_material_rows)
    if material_before < material_count:
        raise _CustomAbilityReject(
            "material_insufficient item=%d have=%d need=%d"
            % (material_item_id, material_before, material_count)
        )

    remaining_need = material_count
    changed_material_slots: set[int] = set()
    for row in valid_material_rows:
        if remaining_need <= 0:
            break
        item_uid = int(row[0])
        material_slot = int(row[1])
        stack_before = int(row[3])
        consumed = min(stack_before, remaining_need)
        stack_after = stack_before - consumed
        if stack_after == 0:
            changed = conn.execute(
                "DELETE FROM character_items WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 0 "
                "AND slot_index = ? AND item_template_id = ? "
                "AND stack_count = ?",
                (
                    item_uid,
                    character_id,
                    material_slot,
                    material_item_id,
                    stack_before,
                ),
            )
        else:
            changed = conn.execute(
                "UPDATE character_items SET stack_count = ?, "
                "instance_value = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE item_uid = ? AND character_id = ? "
                "AND list_type = 0 AND slot_index = ? "
                "AND item_template_id = ? AND stack_count = ?",
                (
                    stack_after,
                    stack_after,
                    item_uid,
                    character_id,
                    material_slot,
                    material_item_id,
                    stack_before,
                ),
            )
        if int(changed.rowcount or 0) != 1:
            raise _CustomAbilityReject("material_changed slot=%d" % material_slot)
        changed_material_slots.add(material_slot)
        remaining_need -= consumed
    if remaining_need != 0:
        raise _CustomAbilityReject("material_settlement_incomplete")
    return (
        material_before,
        material_before - material_count,
        tuple(sorted(changed_material_slots)),
    )


def _set_record_member_counts(conn: Any, character_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    for table, where in (
        ("character_items", "character_id = ? AND list_type = 0"),
        ("character_equipped_entries", "character_id = ?"),
    ):
        rows = conn.execute(
            "SELECT extra_json FROM %s WHERE %s" % (table, where),
            (int(character_id),),
        ).fetchall()
        for row in rows:
            state = parse_customability_state(row[0])
            if state is not None and state.set_record_id:
                record_id = int(state.set_record_id)
                counts[record_id] = counts.get(record_id, 0) + 1
    return counts


def _rewrite_set_member_references(
    conn: Any,
    character_id: int,
    *,
    keep_record_id: int | None,
    replacement_record_id: int = 1,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Keep at most one set membership and return every changed client slot."""
    cid = int(character_id)
    kept = None if keep_record_id is None else int(keep_record_id)
    replacement = int(replacement_record_id)
    changed_inventory_slots: set[int] = set()
    changed_equipment_slots: set[int] = set()
    referenced_record_ids: set[int] = set()

    inventory_rows = conn.execute(
        "SELECT item_uid, slot_index, extra_json FROM character_items "
        "WHERE character_id = ? AND list_type = 0",
        (cid,),
    ).fetchall()
    for row in inventory_rows:
        state = parse_customability_state(row[2])
        if state is None or not state.set_record_id:
            continue
        old_record_id = int(state.set_record_id)
        referenced_record_ids.add(old_record_id)
        new_record_id = replacement if old_record_id == kept else 0
        if new_record_id == old_record_id:
            continue
        updated = merge_customability_state(
            row[2],
            state.individual_options,
            state.grade,
            set_record_id=new_record_id,
        )
        changed = conn.execute(
            "UPDATE character_items SET extra_json = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
            "AND character_id = ? AND list_type = 0 AND slot_index = ?",
            (updated, int(row[0]), cid, int(row[1])),
        )
        if int(changed.rowcount or 0) != 1:
            raise _CustomAbilityReject(
                "set_member_changed list=0 slot=%d" % int(row[1])
            )
        changed_inventory_slots.add(int(row[1]))

    equipped_rows = conn.execute(
        "SELECT slot, item_id, extra_json FROM character_equipped_entries "
        "WHERE character_id = ?",
        (cid,),
    ).fetchall()
    for row in equipped_rows:
        state = parse_customability_state(row[2])
        if state is None or not state.set_record_id:
            continue
        old_record_id = int(state.set_record_id)
        referenced_record_ids.add(old_record_id)
        new_record_id = replacement if old_record_id == kept else 0
        if new_record_id == old_record_id:
            continue
        updated = merge_customability_state(
            row[2],
            state.individual_options,
            state.grade,
            set_record_id=new_record_id,
        )
        changed = conn.execute(
            "UPDATE character_equipped_entries SET extra_json = ? "
            "WHERE character_id = ? AND slot = ? AND item_id = ?",
            (updated, cid, int(row[0]), int(row[1])),
        )
        if int(changed.rowcount or 0) != 1:
            raise _CustomAbilityReject(
                "set_member_changed list=3 slot=%d" % int(row[0])
            )
        changed_equipment_slots.add(int(row[0]))

    return (
        tuple(sorted(changed_inventory_slots)),
        tuple(sorted(changed_equipment_slots)),
        tuple(sorted(referenced_record_ids)),
    )


def load_customability_set_records(
    repo: Any,
    character_id: int,
) -> tuple[CustomAbilitySetRecord, ...]:
    cid = int(character_id)
    if cid <= 0:
        return ()
    repo._ensure_schema()
    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        member_counts = _set_record_member_counts(conn, cid)
        rows = conn.execute(
            "SELECT rowid, set_record_id, part_set_index, required_part_count, "
            "option_1, option_2, option_3, option_4, option_5, set_grade "
            "FROM character_customability_sets WHERE character_id = ? "
            "ORDER BY rowid",
            (cid,),
        ).fetchall()
        candidates = [row for row in rows if member_counts.get(int(row[1]), 0) > 0]
        if not candidates:
            _rewrite_set_member_references(
                conn,
                cid,
                keep_record_id=None,
            )
            conn.execute(
                "DELETE FROM character_customability_sets WHERE character_id = ?",
                (cid,),
            )
            return ()

        # Historical builds allowed several records. The native feature keeps
        # only the most recently assigned record for one character.
        row = candidates[-1]
        source_record_id = int(row[1])
        member_count = int(member_counts[source_record_id])
        required_part_count = int(row[3] or 0)
        if required_part_count == 0:
            required_part_count = min(6, max(3, member_count))
        padded = tuple(int(row[index]) & 0xFF for index in range(4, 9))
        record = CustomAbilitySetRecord(
            set_record_id=1,
            required_part_count=required_part_count,
            part_set_index=int(row[2]) & 0xFF,
            option_ids=tuple(value for value in padded if value),
            set_grade=int(row[9]) & 0xFF,
        )
        if (
            len(rows) == 1
            and source_record_id == 1
            and set(member_counts) == {1}
            and int(row[3] or 0) == required_part_count
        ):
            return (record,)
        _rewrite_set_member_references(
            conn,
            cid,
            keep_record_id=source_record_id,
            replacement_record_id=1,
        )
        conn.execute(
            "DELETE FROM character_customability_sets WHERE character_id = ?",
            (cid,),
        )
        options = record.option_ids + (0,) * (5 - len(record.option_ids))
        conn.execute(
            "INSERT INTO character_customability_sets "
            "(character_id, set_record_id, part_set_index, "
            "required_part_count, option_1, option_2, option_3, "
            "option_4, option_5, set_grade) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                record.part_set_index,
                record.required_part_count,
                *options,
                record.set_grade,
            ),
        )
    return (record,)


def apply_customability_set_assignment(
    repo: Any,
    *,
    character_id: int,
    targets: tuple[CustomAbilitySetTarget, ...] | list[CustomAbilitySetTarget],
    part_set_index: int,
    option_ids: tuple[int, ...] | list[int],
    set_grade: int,
    material_item_id: int,
    material_count: int,
) -> tuple[bool, str, CustomAbilitySetSettlement | None]:
    """Create one character-local Aiken set record and settle it atomically."""
    cid = int(character_id)
    normalized_targets = tuple(targets)
    config_id = int(part_set_index)
    selected_options = tuple(int(value) for value in option_ids)
    grade = int(set_grade)
    material_id = int(material_item_id) & 0xFFFFFFFF
    cost = int(material_count)
    if cid <= 0:
        return False, "character_missing", None
    if any(not isinstance(target, CustomAbilitySetTarget) for target in normalized_targets):
        return False, "targets_invalid", None
    slots = tuple(int(target.slot) for target in normalized_targets)
    if (
        not 3 <= len(normalized_targets) <= 6
        or len(set(slots)) != len(slots)
        or any(not 0 <= slot <= 0xFFFF for slot in slots)
    ):
        return False, "target_slots_invalid", None
    if (
        not 1 <= config_id <= 0xFF
        or not 1 <= len(selected_options) <= 5
        or len(set(selected_options)) != len(selected_options)
        or any(not 1 <= value <= 0xFF for value in selected_options)
        or not 0 <= grade <= 3
    ):
        return False, "record_invalid", None
    if material_id <= 0 or cost <= 0:
        return False, "material_invalid", None

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
                raise _CustomAbilityReject("character_missing")

            loaded: list[tuple[CustomAbilitySetTarget, Any, CustomAbilityState]] = []
            for target in normalized_targets:
                row, current_state = _load_target(
                    conn,
                    character_id=cid,
                    target_slot=int(target.slot),
                    target_item_id=int(target.item_id) & 0xFFFFFFFF,
                    expected_state=target.expected_state,
                )
                loaded.append((target, row, current_state))

            table_record_ids = {
                int(row[0])
                for row in conn.execute(
                    "SELECT set_record_id FROM character_customability_sets "
                    "WHERE character_id = ?",
                    (cid,),
                ).fetchall()
            }
            (
                changed_inventory_slots,
                changed_equipment_slots,
                referenced_record_ids,
            ) = _rewrite_set_member_references(
                conn,
                cid,
                keep_record_id=None,
            )
            removed_record_ids = tuple(
                sorted(table_record_ids | set(referenced_record_ids))
            )
            conn.execute(
                "DELETE FROM character_customability_sets WHERE character_id = ?",
                (cid,),
            )
            set_record_id = 1

            material_before, material_remaining, changed_material_slots = (
                _consume_material_stacks(
                    conn,
                    character_id=cid,
                    target_slot=-1,
                    material_item_id=material_id,
                    material_count=cost,
                )
            )

            padded_options = selected_options + (0,) * (5 - len(selected_options))
            conn.execute(
                "INSERT INTO character_customability_sets "
                "(character_id, set_record_id, part_set_index, "
                "required_part_count, option_1, option_2, option_3, "
                "option_4, option_5, set_grade) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    set_record_id,
                    config_id,
                    len(normalized_targets),
                    *padded_options,
                    grade,
                ),
            )

            for target, row, current_state in loaded:
                updated_extra_json = merge_customability_state(
                    row[4],
                    current_state.individual_options,
                    current_state.grade,
                    set_record_id=set_record_id,
                )
                changed = conn.execute(
                    "UPDATE character_items SET extra_json = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                    "AND item_template_id = ? AND stack_count = 1",
                    (
                        updated_extra_json,
                        int(row[0]),
                        cid,
                        int(target.slot),
                        int(target.item_id) & 0xFFFFFFFF,
                    ),
                )
                if int(changed.rowcount or 0) != 1:
                    raise _CustomAbilityReject(
                        "target_item_changed slot=%d" % int(target.slot)
                    )

            record = CustomAbilitySetRecord(
                set_record_id=set_record_id,
                required_part_count=len(normalized_targets),
                part_set_index=config_id,
                option_ids=selected_options,
                set_grade=grade,
            )
            settlement = CustomAbilitySetSettlement(
                target_slots=tuple(sorted(slots)),
                target_item_ids=tuple(int(target.item_id) for target in normalized_targets),
                record=record,
                material_item_id=material_id,
                material_cost=cost,
                material_remaining=material_remaining,
                changed_material_slots=changed_material_slots,
                changed_inventory_slots=tuple(
                    sorted(set(changed_inventory_slots) | set(slots))
                ),
                changed_equipment_slots=changed_equipment_slots,
                removed_record_ids=removed_record_ids,
            )
            return True, (
                "customability_set record=%d config=%d required_parts=%d "
                "slots=%s options=%s grade=%d material=%d %d->%d removed=%s"
                % (
                    set_record_id,
                    config_id,
                    len(normalized_targets),
                    settlement.target_slots,
                    selected_options,
                    grade,
                    material_id,
                    material_before,
                    material_remaining,
                    removed_record_ids,
                )
            ), settlement
    except _CustomAbilityReject as exc:
        return False, str(exc), None
    except Exception as exc:
        return False, "customability_set_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), None


def apply_customability_option_change(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_target_item_id: int,
    expected_state: CustomAbilityState,
    option_line: int,
    new_option_id: int,
    material_cost: CustomAbilityMaterialCost,
) -> tuple[bool, str, CustomAbilityOptionSettlement | None]:
    """Change one option and consume all required material stacks atomically."""
    cid = int(character_id)
    slot = int(target_slot)
    target_item_id = int(expected_target_item_id) & 0xFFFFFFFF
    line = int(option_line)
    selected_option = int(new_option_id)
    material_item_id = int(material_cost.item_id) & 0xFFFFFFFF
    material_count = int(material_cost.count)
    if cid <= 0:
        return False, "character_missing", None
    if not 0 <= slot <= 0xFFFF:
        return False, "target_slot_invalid", None
    if target_item_id <= 0 or material_item_id <= 0 or material_count <= 0:
        return False, "item_identity_invalid", None
    if line not in (0, 1) or not 1 <= selected_option <= 0xFF:
        return False, "option_invalid", None
    if not isinstance(expected_state, CustomAbilityState):
        return False, "state_invalid", None

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
                raise _CustomAbilityReject("character_missing")

            target, current_state = _load_target(
                conn,
                character_id=cid,
                target_slot=slot,
                target_item_id=target_item_id,
                expected_state=expected_state,
            )
            old_option = int(current_state.individual_options[line])
            if old_option == selected_option:
                raise _CustomAbilityReject("option_unchanged")

            material_before, material_remaining, changed_material_slots = (
                _consume_material_stacks(
                    conn,
                    character_id=cid,
                    target_slot=slot,
                    material_item_id=material_item_id,
                    material_count=material_count,
                )
            )

            updated_options = list(current_state.individual_options)
            updated_options[line] = selected_option
            updated_extra_json = merge_customability_state(
                target[4],
                updated_options,
                current_state.grade,
            )
            changed_target = conn.execute(
                "UPDATE character_items SET extra_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 0 AND slot_index = ?",
                (updated_extra_json, int(target[0]), cid, slot),
            )
            if int(changed_target.rowcount or 0) != 1:
                raise _CustomAbilityReject("target_item_changed")

            settlement = CustomAbilityOptionSettlement(
                target_slot=slot,
                target_item_id=target_item_id,
                option_line=line,
                old_option_id=old_option,
                new_option_id=selected_option,
                grade=current_state.grade,
                material_item_id=material_item_id,
                material_cost=material_count,
                material_remaining=material_remaining,
                changed_material_slots=changed_material_slots,
            )
            return True, (
                "customability_option item=%d slot=%d line=%d option=%d->%d "
                "grade=%d material=%d %d->%d"
                % (
                    target_item_id,
                    slot,
                    line,
                    old_option,
                    selected_option,
                    current_state.grade,
                    material_item_id,
                    material_before,
                    settlement.material_remaining,
                )
            ), settlement
    except _CustomAbilityReject as exc:
        return False, str(exc), None
    except Exception as exc:
        return False, "customability_option_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), None


def apply_customability_grade_upgrade(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_target_item_id: int,
    expected_state: CustomAbilityState,
    target_grade: int,
    material_cost: CustomAbilityMaterialCost,
) -> tuple[bool, str, CustomAbilityUpgradeSettlement | None]:
    """Raise one Aiken instance by exactly one grade and settle materials."""
    cid = int(character_id)
    slot = int(target_slot)
    target_item_id = int(expected_target_item_id) & 0xFFFFFFFF
    new_grade = int(target_grade)
    material_item_id = int(material_cost.item_id) & 0xFFFFFFFF
    material_count = int(material_cost.count)
    if cid <= 0:
        return False, "character_missing", None
    if not 0 <= slot <= 0xFFFF:
        return False, "target_slot_invalid", None
    if target_item_id <= 0 or material_item_id <= 0 or material_count <= 0:
        return False, "item_identity_invalid", None
    if not isinstance(expected_state, CustomAbilityState):
        return False, "state_invalid", None
    if not 1 <= new_grade <= 3:
        return False, "target_grade_invalid", None

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
                raise _CustomAbilityReject("character_missing")

            target, current_state = _load_target(
                conn,
                character_id=cid,
                target_slot=slot,
                target_item_id=target_item_id,
                expected_state=expected_state,
            )
            if new_grade != current_state.grade + 1:
                raise _CustomAbilityReject(
                    "target_grade_changed current=%d target=%d"
                    % (current_state.grade, new_grade)
                )

            material_before, material_remaining, changed_material_slots = (
                _consume_material_stacks(
                    conn,
                    character_id=cid,
                    target_slot=slot,
                    material_item_id=material_item_id,
                    material_count=material_count,
                )
            )
            updated_extra_json = merge_customability_state(
                target[4],
                current_state.individual_options,
                new_grade,
            )
            changed_target = conn.execute(
                "UPDATE character_items SET extra_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 0 AND slot_index = ?",
                (updated_extra_json, int(target[0]), cid, slot),
            )
            if int(changed_target.rowcount or 0) != 1:
                raise _CustomAbilityReject("target_item_changed")

            settlement = CustomAbilityUpgradeSettlement(
                target_slot=slot,
                target_item_id=target_item_id,
                old_grade=current_state.grade,
                new_grade=new_grade,
                material_item_id=material_item_id,
                material_cost=material_count,
                material_remaining=material_remaining,
                changed_material_slots=changed_material_slots,
            )
            return True, (
                "customability_upgrade item=%d slot=%d grade=%d->%d "
                "material=%d %d->%d"
                % (
                    target_item_id,
                    slot,
                    current_state.grade,
                    new_grade,
                    material_item_id,
                    material_before,
                    material_remaining,
                )
            ), settlement
    except _CustomAbilityReject as exc:
        return False, str(exc), None
    except Exception as exc:
        return False, "customability_upgrade_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), None


__all__ = [
    "CustomAbilityOptionSettlement",
    "CustomAbilitySetRecord",
    "CustomAbilitySetSettlement",
    "CustomAbilitySetTarget",
    "CustomAbilityUpgradeSettlement",
    "apply_customability_set_assignment",
    "apply_customability_grade_upgrade",
    "apply_customability_option_change",
    "load_customability_set_records",
]
