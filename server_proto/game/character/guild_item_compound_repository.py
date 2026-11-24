"""Atomic list36 settlement for guild-medal and guardian-gem compounding."""
from __future__ import annotations

import sqlite3
import time
from collections import Counter
from typing import Any, Sequence

from game.character.character_db import EQUIP_TOP_QUALITY_SEED
from game.character.guild_item_compound_index import (
    GUILD_ITEM_KINDS,
    GuildItemCompoundIndex,
    KIND_GUARDIAN_GEM,
    KIND_GUILD_MEDAL,
    get_guild_item_compound_index,
    roll_guild_item_compound_reward,
)
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    resolve_item_expire_time,
)


MIN_MATERIAL_COUNT = 2
MAX_MATERIAL_COUNT = 4


class _GuildItemCompoundReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def _empty_detail() -> dict[str, Any]:
    return {
        "changed_list36_slots": [],
        "material_remaining": {},
        "result_item_id": 0,
        "result_slot": -1,
        "source_rarity": -1,
        "result_rarity": -1,
        "kind": "",
        "upgraded": False,
        "higher_pool_missing": False,
    }


def _normalize_slots(material_slots: Sequence[int]) -> tuple[int, ...]:
    try:
        slots = tuple(int(value) for value in material_slots)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_material_slots") from exc
    if not MIN_MATERIAL_COUNT <= len(slots) <= MAX_MATERIAL_COUNT:
        raise ValueError("invalid_material_count")
    if any(slot < 0 or slot > 97 for slot in slots):
        raise ValueError("material_slot_out_of_range")
    return slots


def _row_to_state(row: Any) -> dict[str, Any]:
    return {
        "item_uid": int(row[0]),
        "slot_index": int(row[1]),
        "item_id": int(row[2]) & 0xFFFFFFFF,
        "item_kind": str(row[3] or ""),
        "stack_count": max(0, int(row[4] or 0)),
        "instance_value": max(0, int(row[5] or 0)) & 0xFFFFFFFF,
        "durability": max(0, int(row[6] or 0)) & 0xFFFF,
        "seal_flag": max(0, int(row[7] or 0)) & 0xFF,
        "option_value": max(0, int(row[8] or 0)) & 0xFF,
        "expire_time": max(0, int(row[9] or 0)) & 0xFFFFFFFF,
        "marker_16": int(row[10] or 0) & 0xFFFFFFFF,
        "pet_serial_or_handle": max(0, int(row[11] or 0)) & 0xFFFFFFFF,
        "extra_json": str(row[12] or "{}"),
    }


def apply_guild_item_compound(
    repo: Any,
    *,
    character_id: int,
    material_slots: Sequence[int],
    expected_kind: str | None = None,
    index: GuildItemCompoundIndex | None = None,
    rng: Any = None,
    now_unix: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Consume 2-4 same-rarity list36 items and grant one rolled result."""
    empty = _empty_detail()
    cid = int(character_id)
    if cid <= 0:
        return False, "character_missing", empty
    required_kind = str(expected_kind or "").strip()
    if required_kind and required_kind not in GUILD_ITEM_KINDS:
        return False, "invalid_expected_kind:%s" % required_kind, empty
    try:
        slots = _normalize_slots(material_slots)
    except ValueError as exc:
        return False, str(exc), empty
    try:
        compound_index = (
            index if index is not None else get_guild_item_compound_index()
        )
    except RuntimeError as exc:
        return False, "guild_item_compound_index_unavailable:%s" % exc, empty

    now_value = int(time.time()) if now_unix is None else max(0, int(now_unix))
    required_by_slot = Counter(slots)
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
                raise _GuildItemCompoundReject("character_missing")

            rows = conn.execute(
                "SELECT item_uid, slot_index, item_template_id, item_kind, "
                "stack_count, instance_value, durability, seal_flag, "
                "option_value, expire_time, marker_16, pet_serial_or_handle, "
                "extra_json FROM character_items "
                "WHERE character_id = ? AND list_type = ? ORDER BY slot_index",
                (cid, GUILD_MEDAL_INVENTORY_LIST_TYPE),
            ).fetchall()
            original_by_slot: dict[int, dict[str, Any]] = {}
            for raw_row in rows:
                row = _row_to_state(raw_row)
                slot = int(row["slot_index"])
                if slot in original_by_slot:
                    raise _GuildItemCompoundReject(
                        "duplicate_inventory_slot slot=%d" % slot
                    )
                original_by_slot[slot] = row
            final_by_slot = {
                slot: dict(row) for slot, row in original_by_slot.items()
            }

            source_entries: dict[int, Any] = {}
            source_kinds: set[str] = set()
            source_rarities: set[int] = set()
            for slot, needed in sorted(required_by_slot.items()):
                row = original_by_slot.get(int(slot))
                if row is None:
                    raise _GuildItemCompoundReject(
                        "material_slot_empty slot=%d" % slot
                    )
                if int(row["expire_time"]) > 0 and int(row["expire_time"]) <= now_value:
                    raise _GuildItemCompoundReject(
                        "material_expired slot=%d" % slot
                    )
                entry = compound_index.entry_for(int(row["item_id"]))
                if entry is None:
                    raise _GuildItemCompoundReject(
                        "material_not_guild_compound_item slot=%d item=%d"
                        % (slot, int(row["item_id"]))
                    )
                start, end = sorted(map(int, entry.metadata.slot_range()))
                if not start <= int(slot) <= end:
                    raise _GuildItemCompoundReject(
                        "material_slot_identity_mismatch slot=%d item=%d"
                        % (slot, int(row["item_id"]))
                    )
                expected_db_kind = entry.metadata.db_item_kind()
                if str(row["item_kind"]) != expected_db_kind:
                    raise _GuildItemCompoundReject(
                        "material_kind_mismatch slot=%d item=%d"
                        % (slot, int(row["item_id"]))
                    )
                available = int(row["stack_count"])
                if entry.kind == KIND_GUILD_MEDAL:
                    if needed != 1:
                        raise _GuildItemCompoundReject(
                            "material_equipment_slot_reused slot=%d" % slot
                        )
                    if available != 1:
                        raise _GuildItemCompoundReject(
                            "material_equipment_instance_invalid slot=%d count=%d"
                            % (slot, available)
                        )
                elif available < int(needed):
                    raise _GuildItemCompoundReject(
                        "material_insufficient slot=%d have=%d need=%d"
                        % (slot, available, int(needed))
                    )
                source_entries[slot] = entry
                source_kinds.add(str(entry.kind))
                source_rarities.add(int(entry.rarity))

            if len(source_kinds) != 1:
                raise _GuildItemCompoundReject(
                    "mixed_material_kind kinds=%s" % sorted(source_kinds)
                )
            if len(source_rarities) != 1:
                raise _GuildItemCompoundReject(
                    "mixed_material_rarity rarities=%s"
                    % sorted(source_rarities)
                )
            source_kind = next(iter(source_kinds))
            source_rarity = next(iter(source_rarities))
            if required_kind and source_kind != required_kind:
                raise _GuildItemCompoundReject(
                    "material_kind_not_allowed expected=%s actual=%s"
                    % (required_kind, source_kind)
                )

            try:
                reward_plan = roll_guild_item_compound_reward(
                    source_kind,
                    source_rarity,
                    len(slots),
                    index=compound_index,
                    rng=rng,
                )
            except (RuntimeError, ValueError) as exc:
                raise _GuildItemCompoundReject(
                    "reward_roll_error:%s" % exc
                ) from exc

            changed_slots: set[int] = set(required_by_slot)
            for slot, needed in sorted(required_by_slot.items()):
                row = final_by_slot[slot]
                remaining = int(row["stack_count"]) - int(needed)
                if remaining <= 0:
                    del final_by_slot[slot]
                else:
                    row["stack_count"] = remaining
                    row["instance_value"] = remaining

            result_entry = reward_plan.entry
            result_meta = result_entry.metadata
            result_start, result_end = sorted(map(int, result_meta.slot_range()))
            result_expire = resolve_item_expire_time(
                result_meta,
                now_unix=now_value,
            )
            result_slot: int | None = None
            allows_merge = bool(
                result_entry.kind == KIND_GUARDIAN_GEM
                and result_meta.allows_stack_merge
                and result_expire == 0
            )
            stack_limit = max(0, int(result_meta.effective_stack_limit))
            if allows_merge:
                for slot in sorted(final_by_slot):
                    row = final_by_slot[slot]
                    if (
                        slot < result_start
                        or slot > result_end
                        or int(row["item_id"]) != int(result_entry.item_id)
                        or str(row["item_kind"]) != "stackable"
                        or int(row["expire_time"]) != 0
                    ):
                        continue
                    current = max(0, int(row["stack_count"]))
                    capacity = (
                        max(0, stack_limit - current)
                        if stack_limit > 0
                        else max(0, 0xFFFFFFFF - current)
                    )
                    if capacity <= 0:
                        continue
                    row["stack_count"] = current + 1
                    row["instance_value"] = current + 1
                    result_slot = slot
                    break

            if result_slot is None:
                result_slot = next(
                    (
                        slot
                        for slot in range(result_start, result_end + 1)
                        if slot not in final_by_slot
                    ),
                    None,
                )
                if result_slot is None:
                    raise _GuildItemCompoundReject(
                        "inventory_full",
                        {
                            "failed_reward_item_id": int(result_entry.item_id),
                            "kind": source_kind,
                        },
                    )
                is_equipment = result_entry.kind == KIND_GUILD_MEDAL
                final_by_slot[result_slot] = {
                    "item_uid": None,
                    "slot_index": result_slot,
                    "item_id": int(result_entry.item_id),
                    "item_kind": result_meta.db_item_kind(),
                    "stack_count": 1,
                    "instance_value": (
                        EQUIP_TOP_QUALITY_SEED if is_equipment else 1
                    ),
                    "durability": (
                        max(0, int(result_meta.durability)) & 0xFFFF
                        if is_equipment
                        else 0
                    ),
                    "seal_flag": 0,
                    "option_value": 0,
                    "expire_time": int(result_expire) & 0xFFFFFFFF,
                    "marker_16": 0,
                    "pet_serial_or_handle": 0,
                    "extra_json": "{}",
                }
            changed_slots.add(int(result_slot))

            for slot, original in original_by_slot.items():
                final = final_by_slot.get(slot)
                same_uid = (
                    final is not None
                    and final.get("item_uid") == original["item_uid"]
                )
                if not same_uid:
                    deleted = conn.execute(
                        "DELETE FROM character_items WHERE item_uid = ? "
                        "AND character_id = ? AND list_type = ? AND slot_index = ? "
                        "AND item_template_id = ? AND item_kind = ? AND stack_count = ?",
                        (
                            int(original["item_uid"]),
                            cid,
                            GUILD_MEDAL_INVENTORY_LIST_TYPE,
                            slot,
                            int(original["item_id"]),
                            str(original["item_kind"]),
                            int(original["stack_count"]),
                        ),
                    )
                    if int(deleted.rowcount or 0) != 1:
                        raise _GuildItemCompoundReject(
                            "material_or_reward_row_changed slot=%d" % slot
                        )
                    continue
                assert final is not None
                if (
                    int(final["stack_count"]) == int(original["stack_count"])
                    and int(final["instance_value"])
                    == int(original["instance_value"])
                ):
                    continue
                updated = conn.execute(
                    "UPDATE character_items SET stack_count = ?, instance_value = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = ? AND slot_index = ? "
                    "AND item_template_id = ? AND item_kind = ? AND stack_count = ?",
                    (
                        int(final["stack_count"]),
                        int(final["instance_value"]),
                        int(original["item_uid"]),
                        cid,
                        GUILD_MEDAL_INVENTORY_LIST_TYPE,
                        slot,
                        int(original["item_id"]),
                        str(original["item_kind"]),
                        int(original["stack_count"]),
                    ),
                )
                if int(updated.rowcount or 0) != 1:
                    raise _GuildItemCompoundReject(
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
                    "VALUES ('character', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cid,
                        cid,
                        GUILD_MEDAL_INVENTORY_LIST_TYPE,
                        int(slot),
                        int(final["item_id"]),
                        str(final["item_kind"]),
                        int(final["stack_count"]),
                        int(final["instance_value"]) & 0xFFFFFFFF,
                        int(final["durability"]) & 0xFFFF,
                        int(final["seal_flag"]) & 0xFF,
                        int(final["option_value"]) & 0xFF,
                        int(final["expire_time"]) & 0xFFFFFFFF,
                        int(final["marker_16"]) & 0xFFFFFFFF,
                        int(final["pet_serial_or_handle"]) & 0xFFFFFFFF,
                        str(final["extra_json"] or "{}"),
                    ),
                )
                final["item_uid"] = int(cursor.lastrowid)

            material_remaining: dict[int, int] = {}
            for slot in sorted(required_by_slot):
                original = original_by_slot[slot]
                final = final_by_slot.get(slot)
                material_remaining[slot] = (
                    int(final["stack_count"])
                    if final is not None
                    and int(final["item_id"]) == int(original["item_id"])
                    else 0
                )
            detail = {
                "changed_list36_slots": sorted(changed_slots),
                "material_remaining": material_remaining,
                "result_item_id": int(result_entry.item_id),
                "result_slot": int(result_slot),
                "source_rarity": int(source_rarity),
                "result_rarity": int(reward_plan.result_rarity),
                "kind": source_kind,
                "upgraded": bool(reward_plan.upgraded),
                "higher_pool_missing": bool(
                    reward_plan.higher_pool_missing
                ),
                "higher_rarity_percent": int(
                    reward_plan.higher_rarity_percent
                ),
                "material_count": len(slots),
            }
            return True, (
                "guild_item_compound_ok kind=%s rarity=%d->%d materials=%d "
                "result=%d slot=%d fallback=%d"
                % (
                    source_kind,
                    source_rarity,
                    int(reward_plan.result_rarity),
                    len(slots),
                    int(result_entry.item_id),
                    int(result_slot),
                    int(bool(reward_plan.higher_pool_missing)),
                )
            ), detail
    except _GuildItemCompoundReject as exc:
        detail = _empty_detail()
        detail.update(exc.detail)
        return False, exc.reason, detail
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        return False, "guild_item_compound_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty


__all__ = [
    "MAX_MATERIAL_COUNT",
    "MIN_MATERIAL_COUNT",
    "apply_guild_item_compound",
]
