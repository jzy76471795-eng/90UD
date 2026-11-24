"""Atomic list0 settlement for Stage3 avatar-emblem compounding."""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from game.character.emblem_compound_config import (
    EmblemCompoundConfig,
    MAX_MATERIAL_COUNT,
    MIN_MATERIAL_COUNT,
    get_emblem_compound_config,
)


@dataclass(frozen=True)
class EmblemCompoundMaterial:
    material_slot: int
    emblem_item_id: int


class _EmblemCompoundReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def _empty_detail() -> dict[str, Any]:
    return {
        "result_rows": [],
        "changed_main_slots": [],
        "material_remaining": {},
    }


def _normalize_materials(
    materials: Sequence[EmblemCompoundMaterial | tuple[int, int]],
) -> tuple[EmblemCompoundMaterial, ...]:
    normalized: list[EmblemCompoundMaterial] = []
    for value in materials:
        if isinstance(value, EmblemCompoundMaterial):
            slot = int(value.material_slot)
            item_id = int(value.emblem_item_id) & 0xFFFFFFFF
        else:
            try:
                raw_slot, raw_item_id = value
                slot = int(raw_slot)
                item_id = int(raw_item_id) & 0xFFFFFFFF
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_material_selection") from exc
        if not 0 <= slot <= 0xFFFF or item_id <= 0:
            raise ValueError("invalid_material_selection")
        normalized.append(
            EmblemCompoundMaterial(
                material_slot=slot,
                emblem_item_id=item_id,
            )
        )
    if not MIN_MATERIAL_COUNT <= len(normalized) <= MAX_MATERIAL_COUNT:
        raise ValueError("invalid_material_count")
    return tuple(normalized)


def _randrange_callable(rng: Any) -> Any:
    if rng is None:
        return secrets.randbelow
    method = getattr(rng, "randrange", None)
    if callable(method):
        return method
    if callable(rng):
        return rng
    raise ValueError("invalid_random_source")


def apply_emblem_compound(
    repo: Any,
    *,
    character_id: int,
    materials: Sequence[EmblemCompoundMaterial | tuple[int, int]],
    config: EmblemCompoundConfig | None = None,
    rng: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Consume 2-5 same-grade emblems and grant PVF-weighted rewards atomically."""
    cid = int(character_id)
    empty = _empty_detail()
    if cid <= 0:
        return False, "character_missing", empty
    try:
        selections = _normalize_materials(materials)
        randrange = _randrange_callable(rng)
    except ValueError as exc:
        return False, str(exc), empty

    try:
        compound_config = (
            config if config is not None else get_emblem_compound_config()
        )
    except RuntimeError as exc:
        return False, "compound_config_unavailable:%s" % exc, empty
    required_by_slot: dict[int, dict[str, int]] = {}
    entries_by_slot: dict[int, Any] = {}
    for selection in selections:
        slot = int(selection.material_slot)
        item_id = int(selection.emblem_item_id)
        emblem = compound_config.resolve_emblem(item_id)
        if emblem is None:
            return False, "material_not_avatar_emblem item=%d" % item_id, empty
        existing = required_by_slot.get(slot)
        if existing is not None:
            if int(existing["item_id"]) != item_id:
                return False, "material_slot_identity_conflict slot=%d" % slot, empty
            existing["count"] = int(existing["count"]) + 1
        else:
            required_by_slot[slot] = {"item_id": item_id, "count": 1}
            entries_by_slot[slot] = emblem

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
                raise _EmblemCompoundReject("character_missing")

            rows = conn.execute(
                "SELECT item_uid, slot_index, item_template_id, item_kind, "
                "stack_count, instance_value FROM character_items "
                "WHERE character_id = ? AND list_type = 0 ORDER BY slot_index",
                (cid,),
            ).fetchall()
            original_by_slot: dict[int, dict[str, Any]] = {}
            for row in rows:
                slot = int(row[1])
                if slot in original_by_slot:
                    raise _EmblemCompoundReject(
                        "duplicate_inventory_slot slot=%d" % slot
                    )
                original_by_slot[slot] = {
                    "item_uid": int(row[0]),
                    "slot_index": slot,
                    "item_id": int(row[2]) & 0xFFFFFFFF,
                    "item_kind": str(row[3] or ""),
                    "stack_count": max(0, int(row[4] or 0)),
                    "instance_value": max(0, int(row[5] or 0)) & 0xFFFFFFFF,
                }
            final_by_slot = {
                slot: dict(row) for slot, row in original_by_slot.items()
            }

            source_grades: set[int] = set()
            changed_slots: set[int] = set()
            for slot, required in required_by_slot.items():
                row = final_by_slot.get(slot)
                item_id = int(required["item_id"])
                needed = int(required["count"])
                emblem = entries_by_slot[slot]
                metadata = emblem.metadata
                start, end = sorted(map(int, metadata.slot_range()))
                if not start <= slot <= end:
                    raise _EmblemCompoundReject(
                        "material_slot_outside_emblem_band slot=%d item=%d"
                        % (slot, item_id)
                    )
                if row is None:
                    raise _EmblemCompoundReject(
                        "material_slot_empty slot=%d" % slot
                    )
                if (
                    int(row["item_id"]) != item_id
                    or str(row["item_kind"]) != "stackable"
                ):
                    raise _EmblemCompoundReject(
                        "material_item_mismatch slot=%d expected=%d actual=%d"
                        % (slot, item_id, int(row["item_id"]))
                    )
                if int(row["stack_count"]) < needed:
                    raise _EmblemCompoundReject(
                        "material_insufficient slot=%d have=%d need=%d"
                        % (slot, int(row["stack_count"]), needed)
                    )
                source_grades.add(int(metadata.grade))
                remaining = int(row["stack_count"]) - needed
                changed_slots.add(slot)
                if remaining <= 0:
                    del final_by_slot[slot]
                else:
                    row["stack_count"] = remaining
                    row["instance_value"] = remaining

            if len(source_grades) != 1:
                raise _EmblemCompoundReject(
                    "mixed_material_grade grades=%s" % sorted(source_grades)
                )
            source_grade = next(iter(source_grades))
            rule = compound_config.resolve_rule(source_grade, len(selections))
            if rule is None:
                raise _EmblemCompoundReject(
                    "compound_rule_missing grade=%d count=%d"
                    % (source_grade, len(selections))
                )
            try:
                rolled = rule.roll(randrange=randrange)
            except ValueError as exc:
                raise _EmblemCompoundReject(
                    "reward_roll_error:%s" % exc
                ) from exc

            reward_order: list[int] = []
            reward_counts: dict[int, int] = {}
            reward_entries: dict[int, Any] = {}
            for candidate in rolled:
                item_id = int(candidate.item_id) & 0xFFFFFFFF
                count = int(candidate.count)
                if item_id not in reward_counts:
                    reward_order.append(item_id)
                    reward_counts[item_id] = 0
                    reward_entries[item_id] = candidate.emblem
                total = int(reward_counts[item_id]) + count
                if total > 0xFFFFFFFF:
                    raise _EmblemCompoundReject(
                        "reward_count_overflow item=%d" % item_id
                    )
                reward_counts[item_id] = total

            for item_id in reward_order:
                emblem = reward_entries[item_id]
                metadata = emblem.metadata
                if compound_config.resolve_emblem(item_id) is not emblem:
                    raise _EmblemCompoundReject(
                        "reward_identity_changed item=%d" % item_id
                    )
                if not metadata.is_stackable:
                    raise _EmblemCompoundReject(
                        "reward_not_stackable_emblem item=%d" % item_id
                    )
                start, end = sorted(map(int, metadata.slot_range()))
                if start < 0 or end > 0xFFFF:
                    raise _EmblemCompoundReject(
                        "reward_slot_range_invalid item=%d" % item_id
                    )
                stack_limit = int(metadata.effective_stack_limit or 0)
                remaining = int(reward_counts[item_id])
                for slot in sorted(final_by_slot):
                    if remaining <= 0:
                        break
                    row = final_by_slot[slot]
                    if (
                        slot < start
                        or slot > end
                        or int(row["item_id"]) != item_id
                    ):
                        continue
                    current = max(0, int(row["stack_count"]))
                    capacity = (
                        max(0, stack_limit - current)
                        if stack_limit > 0
                        else max(0, 0xFFFFFFFF - current)
                    )
                    granted = min(remaining, capacity)
                    if granted <= 0:
                        continue
                    row["stack_count"] = current + granted
                    row["instance_value"] = current + granted
                    row["item_kind"] = metadata.db_item_kind()
                    changed_slots.add(slot)
                    remaining -= granted

                while remaining > 0:
                    free_slot = next(
                        (
                            slot
                            for slot in range(start, end + 1)
                            if slot not in final_by_slot
                        ),
                        None,
                    )
                    if free_slot is None:
                        raise _EmblemCompoundReject(
                            "inventory_full",
                            {"failed_reward_item_id": item_id},
                        )
                    granted = (
                        min(remaining, stack_limit)
                        if stack_limit > 0
                        else min(remaining, 0xFFFFFFFF)
                    )
                    if granted <= 0:
                        raise _EmblemCompoundReject(
                            "reward_stack_limit_invalid item=%d" % item_id
                        )
                    final_by_slot[free_slot] = {
                        "item_uid": None,
                        "slot_index": free_slot,
                        "item_id": item_id,
                        "item_kind": metadata.db_item_kind(),
                        "stack_count": granted,
                        "instance_value": granted,
                    }
                    changed_slots.add(free_slot)
                    remaining -= granted

            for slot, original in original_by_slot.items():
                final = final_by_slot.get(slot)
                same_uid = final is not None and final.get("item_uid") == original["item_uid"]
                if not same_uid:
                    deleted = conn.execute(
                        "DELETE FROM character_items WHERE item_uid = ? "
                        "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                        "AND item_template_id = ? AND stack_count = ?",
                        (
                            int(original["item_uid"]),
                            cid,
                            slot,
                            int(original["item_id"]),
                            int(original["stack_count"]),
                        ),
                    )
                    if int(deleted.rowcount or 0) != 1:
                        raise _EmblemCompoundReject(
                            "material_or_reward_row_changed slot=%d" % slot
                        )
                    continue
                assert final is not None
                if (
                    int(final["stack_count"]) == int(original["stack_count"])
                    and str(final["item_kind"]) == str(original["item_kind"])
                ):
                    continue
                updated = conn.execute(
                    "UPDATE character_items SET item_kind = ?, stack_count = ?, "
                    "instance_value = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE item_uid = ? AND character_id = ? AND list_type = 0 "
                    "AND slot_index = ? AND item_template_id = ? AND stack_count = ?",
                    (
                        str(final["item_kind"]),
                        int(final["stack_count"]),
                        int(final["instance_value"]),
                        int(original["item_uid"]),
                        cid,
                        slot,
                        int(original["item_id"]),
                        int(original["stack_count"]),
                    ),
                )
                if int(updated.rowcount or 0) != 1:
                    raise _EmblemCompoundReject(
                        "material_or_reward_row_changed slot=%d" % slot
                    )

            for slot in sorted(final_by_slot):
                final = final_by_slot[slot]
                if final.get("item_uid") is not None:
                    continue
                cursor = conn.execute(
                    "INSERT INTO character_items "
                    "(owner_scope, owner_id, character_id, list_type, slot_index, "
                    "item_template_id, item_kind, stack_count, instance_value, "
                    "durability, seal_flag, option_value, expire_time, marker_16, "
                    "pet_serial_or_handle, extra_json) "
                    "VALUES ('character', ?, ?, 0, ?, ?, ?, ?, ?, "
                    "0, 0, 0, 0, 0, 0, '{}')",
                    (
                        cid,
                        cid,
                        slot,
                        int(final["item_id"]),
                        str(final["item_kind"]),
                        int(final["stack_count"]),
                        int(final["instance_value"]),
                    ),
                )
                final["item_uid"] = int(cursor.lastrowid)

            material_remaining = {}
            for slot, required in required_by_slot.items():
                final = final_by_slot.get(slot)
                material_remaining[slot] = (
                    int(final["stack_count"])
                    if final is not None
                    and int(final["item_id"]) == int(required["item_id"])
                    else 0
                )
            result_rows = [
                {
                    "item_id": item_id,
                    "granted_count": int(reward_counts[item_id]),
                }
                for item_id in reward_order
            ]
            detail = {
                "result_rows": result_rows,
                "changed_main_slots": sorted(changed_slots),
                "material_remaining": material_remaining,
                "source_grade": source_grade,
                "material_count": len(selections),
                "rule_material_count": int(rule.material_count),
                "booster_item_id": int(rule.booster_item_id),
                "total_weight": int(rule.total_weight),
            }
            return True, (
                "emblem_compound_ok grade=%d materials=%d lookup=%d "
                "booster=%d rewards=%s"
                % (
                    source_grade,
                    len(selections),
                    int(rule.material_count),
                    int(rule.booster_item_id),
                    [
                        (row["item_id"], row["granted_count"])
                        for row in result_rows
                    ],
                )
            ), detail
    except _EmblemCompoundReject as exc:
        detail = _empty_detail()
        detail.update(exc.detail)
        return False, exc.reason, detail
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        return False, "emblem_compound_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), _empty_detail()


__all__ = [
    "EmblemCompoundMaterial",
    "apply_emblem_compound",
]
