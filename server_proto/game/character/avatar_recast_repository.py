"""Atomic inventory settlement for Stage3 avatar recasting."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from game.character.item_metadata import ItemMetadata


@dataclass(frozen=True)
class AvatarRecastReward:
    """One rolled emblem granted by an avatar-recast operation."""

    item_id: int
    metadata: ItemMetadata


class _AvatarRecastReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def _empty_detail() -> dict[str, Any]:
    return {
        "reward_rows": [],
        "reward_roll_count": 0,
        "changed_main_slots": [],
        "changed_avatar_slots": [],
    }


def apply_avatar_recast(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_target_item_id: int,
    rewards: Sequence[AvatarRecastReward],
) -> tuple[bool, str, dict[str, Any]]:
    """Delete one list1 avatar and grant all reward emblems in one transaction."""
    cid = int(character_id)
    slot = int(target_slot)
    target_item_id = int(expected_target_item_id) & 0xFFFFFFFF
    reward_specs = tuple(rewards)
    empty = _empty_detail()

    if cid <= 0:
        return False, "character_missing", empty
    if not 0 <= slot <= 0xFFFF or target_item_id <= 0:
        return False, "invalid_target", empty
    if not reward_specs or len(reward_specs) > 0xFFFF:
        return False, "invalid_reward_count", empty

    normalized: list[dict[str, Any]] = []
    normalized_by_id: dict[int, dict[str, Any]] = {}
    for reward in reward_specs:
        item_id = int(reward.item_id) & 0xFFFFFFFF
        metadata = reward.metadata
        if item_id <= 0:
            return False, "invalid_reward_identity", empty
        if int(metadata.item_id) & 0xFFFFFFFF != item_id:
            return False, "reward_metadata_mismatch", empty
        stackable_type = str(metadata.stackable_type or "").strip().lower()
        if (
            not metadata.is_stackable
            or not stackable_type.startswith("[avatar emblem]")
        ):
            return False, "reward_not_avatar_emblem", empty
        start, end = sorted(map(int, metadata.slot_range()))
        if start < 0 or end > 0xFFFF:
            return False, "invalid_reward_slot_range", empty
        # A limit of one is allocated as one reward per free emblem slot below.
        stack_limit = int(metadata.effective_stack_limit or 0)
        identity = (
            start,
            end,
            stack_limit,
            metadata.db_item_kind(),
        )
        existing = normalized_by_id.get(item_id)
        if existing is not None:
            if tuple(existing["identity"]) != identity:
                return False, "reward_metadata_mismatch", empty
            existing["quantity"] = int(existing["quantity"]) + 1
            continue
        spec = {
            "item_id": item_id,
            "start": start,
            "end": end,
            "stack_limit": stack_limit,
            "item_kind": metadata.db_item_kind(),
            "quantity": 1,
            "identity": identity,
        }
        normalized.append(spec)
        normalized_by_id[item_id] = spec

    try:
        repo._ensure_schema()
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT 1 FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if owner is None:
                raise _AvatarRecastReject("character_missing")

            target = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, stack_count "
                "FROM character_items WHERE character_id = ? "
                "AND list_type = 1 AND slot_index = ?",
                (cid, slot),
            ).fetchone()
            if target is None:
                raise _AvatarRecastReject("target_slot_empty")
            if (
                (int(target[1]) & 0xFFFFFFFF) != target_item_id
                or str(target[2] or "") not in {"avatar", "equipment"}
                or int(target[3] or 0) != 1
            ):
                raise _AvatarRecastReject("target_item_mismatch")

            used_slots = {
                int(row[0])
                for row in conn.execute(
                    "SELECT slot_index FROM character_items "
                    "WHERE character_id = ? AND list_type = 0",
                    (cid,),
                ).fetchall()
            }
            plans: list[dict[str, Any]] = []
            for spec in normalized:
                item_id = int(spec["item_id"])
                start = int(spec["start"])
                end = int(spec["end"])
                stack_limit = int(spec["stack_limit"])
                item_kind = str(spec["item_kind"])
                remaining = int(spec["quantity"])
                rows = conn.execute(
                    "SELECT item_uid, slot_index, stack_count "
                    "FROM character_items WHERE character_id = ? "
                    "AND list_type = 0 AND item_template_id = ? "
                    "AND slot_index BETWEEN ? AND ? ORDER BY slot_index",
                    (cid, item_id, start, end),
                ).fetchall()
                for row in rows:
                    if remaining <= 0:
                        break
                    current = max(0, int(row[2] or 0))
                    capacity = (
                        max(0, stack_limit - current)
                        if stack_limit > 0
                        else max(0, 0xFFFFFFFF - current)
                    )
                    grant_count = min(remaining, capacity)
                    if grant_count > 0:
                        plans.append(
                            {
                                "mode": "merge",
                                "item_uid": int(row[0]),
                                "slot_index": int(row[1]),
                                "item_template_id": item_id,
                                "item_kind": item_kind,
                                "granted_count": grant_count,
                                "final_stack_count": current + grant_count,
                            }
                        )
                        remaining -= grant_count

                while remaining > 0:
                    free_slot = next(
                        (
                            candidate
                            for candidate in range(start, end + 1)
                            if candidate not in used_slots
                        ),
                        None,
                    )
                    if free_slot is None:
                        raise _AvatarRecastReject(
                            "inventory_full",
                            {
                                "failed_reward_item_id": item_id,
                                "reward_slot_range": (start, end),
                            },
                        )
                    grant_count = (
                        remaining
                        if stack_limit <= 0
                        else min(remaining, stack_limit)
                    )
                    used_slots.add(int(free_slot))
                    plans.append(
                        {
                            "mode": "insert",
                            "slot_index": int(free_slot),
                            "item_template_id": item_id,
                            "item_kind": item_kind,
                            "granted_count": grant_count,
                            "final_stack_count": grant_count,
                        }
                    )
                    remaining -= grant_count

            deleted = conn.execute(
                "DELETE FROM character_items WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 1 AND slot_index = ? "
                "AND item_template_id = ?",
                (int(target[0]), cid, slot, target_item_id),
            )
            if int(deleted.rowcount or 0) != 1:
                raise _AvatarRecastReject("target_item_changed")

            reward_rows: list[dict[str, Any]] = []
            for plan in plans:
                granted_count = int(plan["granted_count"])
                final_count = int(plan["final_stack_count"])
                if plan["mode"] == "merge":
                    updated = conn.execute(
                        "UPDATE character_items SET stack_count = ?, "
                        "instance_value = ?, item_kind = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                        (
                            final_count,
                            final_count,
                            str(plan["item_kind"]),
                            int(plan["item_uid"]),
                        ),
                    )
                    if int(updated.rowcount or 0) != 1:
                        raise _AvatarRecastReject("reward_stack_changed")
                else:
                    conn.execute(
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
                            int(plan["slot_index"]),
                            int(plan["item_template_id"]),
                            str(plan["item_kind"]),
                            final_count,
                            final_count,
                        ),
                    )
                reward_rows.append(
                    {
                        "list_type": 0,
                        "slot_index": int(plan["slot_index"]),
                        "item_template_id": int(plan["item_template_id"]),
                        "granted_count": granted_count,
                        "final_stack_count": final_count,
                    }
                )

            changed_main_slots = [int(row["slot_index"]) for row in reward_rows]
            detail = {
                "reward_rows": reward_rows,
                "reward_roll_count": len(reward_specs),
                "changed_main_slots": changed_main_slots,
                "changed_avatar_slots": [slot],
            }
            return True, (
                "avatar_recast_ok target=%d slot=%d rolls=%d rewards=%s"
                % (
                    target_item_id,
                    slot,
                    len(reward_specs),
                    [
                        (
                            row["slot_index"],
                            row["item_template_id"],
                            row["granted_count"],
                            row["final_stack_count"],
                        )
                        for row in reward_rows
                    ],
                )
            ), detail
    except _AvatarRecastReject as exc:
        detail = _empty_detail()
        detail.update(exc.detail)
        return False, exc.reason, detail
    except sqlite3.Error as exc:
        return False, "avatar_recast_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), _empty_detail()


__all__ = ["AvatarRecastReward", "apply_avatar_recast"]
