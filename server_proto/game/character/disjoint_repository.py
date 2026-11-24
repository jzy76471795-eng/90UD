"""Atomic batch settlement for Stage3 equipment disassembly."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from game.character.item_metadata import ItemMetadata


@dataclass(frozen=True)
class DisjointTarget:
    slot_index: int
    item_template_id: int
    stack_count: int


@dataclass(frozen=True)
class DisjointToolUse:
    slot_index: int
    item_template_id: int
    count: int


@dataclass(frozen=True)
class DisjointReward:
    item_template_id: int
    count: int
    metadata: ItemMetadata


class _DisjointBatchReject(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = dict(detail or {})


def _empty_detail() -> dict[str, Any]:
    return {
        "removed_targets": [],
        "consumed_tools": [],
        "reward_rows": [],
    }


def apply_disjoint_batch(
    repo: Any,
    *,
    character_id: int,
    targets: Sequence[DisjointTarget],
    tool_uses: Sequence[DisjointToolUse],
    rewards: Sequence[DisjointReward],
) -> tuple[bool, str, dict[str, Any]]:
    """Destroy all targets and grant aggregated rewards in one transaction."""
    cid = int(character_id)
    normalized_targets = tuple(targets)
    normalized_tools = tuple(tool_uses)
    normalized_rewards = tuple(rewards)
    empty = _empty_detail()

    if cid <= 0 or not normalized_targets:
        return False, "invalid_disjoint_batch", empty

    target_slots: set[int] = set()
    for target in normalized_targets:
        slot = int(target.slot_index)
        item_id = int(target.item_template_id) & 0xFFFFFFFF
        count = int(target.stack_count)
        if slot <= 0 or item_id <= 0 or count <= 0 or slot in target_slots:
            return False, "invalid_or_duplicate_target slot=%d" % slot, empty
        target_slots.add(slot)

    tools_by_slot: dict[int, DisjointToolUse] = {}
    for tool in normalized_tools:
        slot = int(tool.slot_index)
        item_id = int(tool.item_template_id) & 0xFFFFFFFF
        count = int(tool.count)
        if slot < 0 or item_id <= 0 or count <= 0 or slot in target_slots:
            return False, "invalid_tool_use slot=%d" % slot, empty
        previous = tools_by_slot.get(slot)
        if previous is not None:
            if int(previous.item_template_id) != item_id:
                return False, "tool_identity_conflict slot=%d" % slot, empty
            tools_by_slot[slot] = DisjointToolUse(
                slot_index=slot,
                item_template_id=item_id,
                count=int(previous.count) + count,
            )
        else:
            tools_by_slot[slot] = DisjointToolUse(slot, item_id, count)

    reward_order: list[int] = []
    rewards_by_id: dict[int, DisjointReward] = {}
    for reward in normalized_rewards:
        item_id = int(reward.item_template_id) & 0xFFFFFFFF
        count = int(reward.count)
        metadata = reward.metadata
        if (
            item_id <= 0
            or count <= 0
            or not isinstance(metadata, ItemMetadata)
            or int(metadata.item_id) != item_id
        ):
            return False, "invalid_disjoint_reward item=%d" % item_id, empty
        previous = rewards_by_id.get(item_id)
        if previous is None:
            reward_order.append(item_id)
            rewards_by_id[item_id] = DisjointReward(item_id, count, metadata)
            continue
        total = int(previous.count) + count
        if total > 0xFFFFFFFF:
            return False, "disjoint_reward_overflow item=%d" % item_id, empty
        rewards_by_id[item_id] = DisjointReward(item_id, total, previous.metadata)

    requested_slots = sorted(target_slots | set(tools_by_slot))
    placeholders = ",".join("?" for _ in requested_slots)

    try:
        with repo.inventory_transaction_scope() as conn:
            owner = conn.execute(
                "SELECT 1 FROM characters WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if owner is None:
                raise _DisjointBatchReject("character_missing")

            rows = conn.execute(
                "SELECT item_uid, slot_index, item_template_id, item_kind, "
                "stack_count, equipment_lock_id FROM character_items "
                "WHERE character_id = ? AND list_type = 0 "
                "AND slot_index IN (%s)" % placeholders,
                (cid, *requested_slots),
            ).fetchall()
            rows_by_slot = {int(row[1]): row for row in rows}

            target_rows: list[tuple[DisjointTarget, Any]] = []
            for target in normalized_targets:
                slot = int(target.slot_index)
                row = rows_by_slot.get(slot)
                if row is None:
                    raise _DisjointBatchReject("target_slot_empty slot=%d" % slot)
                actual_item = int(row[2]) & 0xFFFFFFFF
                actual_kind = str(row[3] or "")
                actual_count = max(0, int(row[4] or 0))
                lock_id = max(0, int(row[5] or 0)) & 0xFF
                if lock_id > 0:
                    raise _DisjointBatchReject(
                        "equipment_locked slot=%d lock_id=%d" % (slot, lock_id)
                    )
                if (
                    actual_item != (int(target.item_template_id) & 0xFFFFFFFF)
                    or actual_kind not in {"equipment", ""}
                    or actual_count != int(target.stack_count)
                ):
                    raise _DisjointBatchReject(
                        "target_item_changed slot=%d expected=%d actual=%d"
                        % (slot, int(target.item_template_id), actual_item)
                    )
                target_rows.append((target, row))

            tool_rows: list[tuple[DisjointToolUse, Any]] = []
            for slot in sorted(tools_by_slot):
                tool = tools_by_slot[slot]
                row = rows_by_slot.get(slot)
                if row is None:
                    raise _DisjointBatchReject("tool_slot_empty slot=%d" % slot)
                actual_item = int(row[2]) & 0xFFFFFFFF
                actual_kind = str(row[3] or "")
                actual_count = max(0, int(row[4] or 0))
                if (
                    actual_item != (int(tool.item_template_id) & 0xFFFFFFFF)
                    or actual_kind not in {"stackable", ""}
                    or actual_count < int(tool.count)
                ):
                    raise _DisjointBatchReject(
                        "tool_item_changed slot=%d expected=%dx%d actual=%dx%d"
                        % (
                            slot,
                            int(tool.item_template_id),
                            int(tool.count),
                            actual_item,
                            actual_count,
                        )
                    )
                tool_rows.append((tool, row))

            removed_targets: list[dict[str, int]] = []
            for target, row in target_rows:
                deleted = conn.execute(
                    "DELETE FROM character_items WHERE item_uid = ? "
                    "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                    "AND item_template_id = ? AND equipment_lock_id = 0",
                    (
                        int(row[0]),
                        cid,
                        int(target.slot_index),
                        int(target.item_template_id) & 0xFFFFFFFF,
                    ),
                )
                if int(deleted.rowcount or 0) != 1:
                    raise _DisjointBatchReject(
                        "target_delete_changed slot=%d" % int(target.slot_index)
                    )
                removed_targets.append(
                    {
                        "slot_index": int(target.slot_index),
                        "item_template_id": int(target.item_template_id) & 0xFFFFFFFF,
                        "removed": int(target.stack_count),
                    }
                )

            consumed_tools: list[dict[str, int]] = []
            for tool, row in tool_rows:
                current = max(0, int(row[4] or 0))
                remaining = current - int(tool.count)
                if remaining <= 0:
                    changed = conn.execute(
                        "DELETE FROM character_items WHERE item_uid = ? "
                        "AND stack_count = ?",
                        (int(row[0]), current),
                    )
                else:
                    changed = conn.execute(
                        "UPDATE character_items SET stack_count = ?, instance_value = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                        "AND stack_count = ?",
                        (remaining, remaining, int(row[0]), current),
                    )
                if int(changed.rowcount or 0) != 1:
                    raise _DisjointBatchReject(
                        "tool_consume_changed slot=%d" % int(tool.slot_index)
                    )
                consumed_tools.append(
                    {
                        "slot_index": int(tool.slot_index),
                        "item_template_id": int(tool.item_template_id) & 0xFFFFFFFF,
                        "removed": int(tool.count),
                        "remaining": max(0, remaining),
                    }
                )

            reward_rows: list[dict[str, Any]] = []
            for item_id in reward_order:
                reward = rewards_by_id[item_id]
                ok, grant_note, first_slot = repo.grant_inventory_item(
                    cid,
                    item_id,
                    int(reward.count),
                    list_type=0,
                    metadata=reward.metadata,
                    require_full_capacity=True,
                )
                if not ok or int(first_slot) < 0:
                    raise _DisjointBatchReject(
                        "inventory_full reward=%dx%d reason=%s"
                        % (item_id, int(reward.count), grant_note),
                        {
                            "failed_reward_item_id": item_id,
                            "failed_reward_count": int(reward.count),
                        },
                    )
                reward_rows.append(
                    {
                        "slot_index": int(first_slot),
                        "item_template_id": item_id,
                        "granted_count": int(reward.count),
                        "note": str(grant_note or ""),
                    }
                )

            detail = {
                "removed_targets": removed_targets,
                "consumed_tools": consumed_tools,
                "reward_rows": reward_rows,
            }
            return True, (
                "disjoint_batch_ok targets=%d tools=%d rewards=%d"
                % (len(removed_targets), len(consumed_tools), len(reward_rows))
            ), detail
    except _DisjointBatchReject as exc:
        detail = _empty_detail()
        detail.update(exc.detail)
        return False, exc.reason, detail
    except sqlite3.Error as exc:
        return False, "disjoint_batch_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty
    except Exception as exc:
        return False, "disjoint_batch_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), empty


__all__ = [
    "DisjointTarget",
    "DisjointToolUse",
    "DisjointReward",
    "apply_disjoint_batch",
]
