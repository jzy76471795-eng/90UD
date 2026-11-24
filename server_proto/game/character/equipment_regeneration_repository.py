"""Atomic inventory settlement for equipment regeneration."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from game.chain_loader import load_chain
from game.character.equipment_regeneration_index import RegenerationMaterialCost
from game.character.payloads import EQUIP_TOP_QUALITY_SEED


_cube_soul = load_chain("helper-cube_soul_slots.py")
account_soul_slot = _cube_soul.account_soul_slot
cube_fragment_column = _cube_soul.cube_fragment_column
cube_fragment_slot = _cube_soul.cube_fragment_slot
is_account_soul = _cube_soul.is_account_soul
is_cube_fragment = _cube_soul.is_cube_fragment


@dataclass(frozen=True)
class EquipmentRegenerationSettlement:
    target_slot: int
    target_item_uid: int
    source_item_id: int
    result_item_id: int
    changed_material_slots: tuple[int, ...]
    material_remaining: dict[int, int]
    gold_cost: int
    updated_gold: int


class _RegenerationReject(RuntimeError):
    pass


def _normalize_material_costs(
    costs: Iterable[RegenerationMaterialCost],
) -> tuple[RegenerationMaterialCost, ...]:
    merged: dict[int, int] = {}
    for cost in costs:
        item_id = int(cost.item_id) & 0xFFFFFFFF
        count = int(cost.count)
        if item_id <= 0 or count <= 0:
            raise ValueError("invalid regeneration material requirement")
        merged[item_id] = merged.get(item_id, 0) + count
    return tuple(
        RegenerationMaterialCost(item_id, merged[item_id])
        for item_id in sorted(merged)
    )


def apply_equipment_regeneration(
    repo: Any,
    *,
    character_id: int,
    target_slot: int,
    expected_source_item_id: int,
    result_metadata: Any,
    material_costs: Iterable[RegenerationMaterialCost] = (),
    gold_cost: int = 0,
) -> tuple[bool, str, EquipmentRegenerationSettlement | None]:
    """Replace one list0 equipment row and settle every cost in one transaction."""
    cid = int(character_id)
    slot = int(target_slot)
    source_item_id = int(expected_source_item_id) & 0xFFFFFFFF
    result_item_id = int(getattr(result_metadata, "item_id", 0) or 0) & 0xFFFFFFFF
    gold_need = int(gold_cost)
    try:
        requirements = _normalize_material_costs(material_costs)
    except ValueError as exc:
        return False, str(exc), None
    if cid <= 0:
        return False, "character_missing", None
    if slot <= 0:
        return False, "target_slot_invalid", None
    if source_item_id <= 0 or result_item_id <= 0:
        return False, "item_identity_invalid", None
    if gold_need < 0:
        return False, "gold_cost_invalid", None
    if not bool(getattr(result_metadata, "is_equipment", False)):
        return False, "result_not_equipment", None

    durability = max(0, int(getattr(result_metadata, "durability", 0) or 0)) & 0xFFFF
    attach_type = str(
        getattr(result_metadata, "normalized_attach_type", lambda: "")()
        or ""
    )
    result_seal_flag = 1 if attach_type == "sealing" else 0

    repo._ensure_schema()
    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            character = conn.execute(
                "SELECT account_id FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (cid,),
            ).fetchone()
            if character is None:
                raise _RegenerationReject("character_missing")
            account_id = int(character[0])

            target = conn.execute(
                "SELECT item_uid, item_template_id, item_kind, stack_count "
                "FROM character_items WHERE character_id = ? AND list_type = 0 "
                "AND slot_index = ?",
                (cid, slot),
            ).fetchone()
            if target is None:
                raise _RegenerationReject("target_slot_empty")
            target_uid = int(target[0])
            actual_item_id = int(target[1]) & 0xFFFFFFFF
            if actual_item_id != source_item_id:
                raise _RegenerationReject(
                    "target_item_changed expected=%d actual=%d"
                    % (source_item_id, actual_item_id)
                )
            if str(target[2] or "") != "equipment" or int(target[3] or 0) != 1:
                raise _RegenerationReject("target_instance_invalid")

            ordinary_requirements = tuple(
                requirement
                for requirement in requirements
                if not is_cube_fragment(requirement.item_id)
                and not is_account_soul(requirement.item_id)
            )
            material_rows: dict[int, list[Any]] = {
                requirement.item_id: [] for requirement in ordinary_requirements
            }
            if ordinary_requirements:
                placeholders = ",".join("?" for _value in ordinary_requirements)
                rows = conn.execute(
                    "SELECT item_uid, slot_index, item_template_id, stack_count "
                    "FROM character_items WHERE character_id = ? AND list_type = 0 "
                    "AND item_template_id IN (%s) AND slot_index != ? "
                    "ORDER BY CASE WHEN slot_index BETWEEN 3 AND 8 THEN 0 ELSE 1 END, "
                    "slot_index, item_uid" % placeholders,
                    (
                        cid,
                        *(requirement.item_id for requirement in ordinary_requirements),
                        slot,
                    ),
                ).fetchall()
                for row in rows:
                    item_id = int(row[2]) & 0xFFFFFFFF
                    material_rows.setdefault(item_id, []).append(row)

            for requirement in requirements:
                if is_cube_fragment(requirement.item_id):
                    column = cube_fragment_column(requirement.item_id)
                    row = conn.execute(
                        "SELECT %s FROM accounts WHERE account_id = ?" % column,
                        (account_id,),
                    ).fetchone()
                    available = max(0, int(row[0] or 0)) if row is not None else 0
                elif is_account_soul(requirement.item_id):
                    row = conn.execute(
                        "SELECT balance FROM account_soul_balances "
                        "WHERE account_id = ? AND item_id = ?",
                        (account_id, requirement.item_id),
                    ).fetchone()
                    available = max(0, int(row[0] or 0)) if row is not None else 0
                else:
                    available = sum(
                        max(0, int(row[3] or 0))
                        for row in material_rows.get(requirement.item_id, ())
                    )
                if available < requirement.count:
                    raise _RegenerationReject(
                        "material_insufficient item=%d have=%d need=%d"
                        % (requirement.item_id, available, requirement.count)
                    )

            updated_gold = 0
            wallet: dict[str, int] | None = None
            if gold_need > 0:
                wallet = repo._currency_wallet_tx(conn, cid)
                gold_before = max(0, int(wallet["gold"]))
                if gold_before < gold_need:
                    raise _RegenerationReject(
                        "gold_insufficient have=%d need=%d"
                        % (gold_before, gold_need)
                    )
                updated_gold = gold_before - gold_need

            changed_material_slots: set[int] = set()
            material_remaining: dict[int, int] = {}
            for requirement in requirements:
                if is_cube_fragment(requirement.item_id):
                    column = cube_fragment_column(requirement.item_id)
                    changed = conn.execute(
                        "UPDATE accounts SET %s = %s - ? "
                        "WHERE account_id = ? AND %s >= ?" % (column, column, column),
                        (requirement.count, account_id, requirement.count),
                    )
                    if int(changed.rowcount or 0) != 1:
                        raise _RegenerationReject(
                            "material_changed item=%d" % requirement.item_id
                        )
                    row = conn.execute(
                        "SELECT %s FROM accounts WHERE account_id = ?" % column,
                        (account_id,),
                    ).fetchone()
                    material_remaining[requirement.item_id] = max(
                        0, int(row[0] or 0)
                    )
                    changed_material_slots.add(
                        int(cube_fragment_slot(requirement.item_id))
                    )
                    continue
                if is_account_soul(requirement.item_id):
                    changed = conn.execute(
                        "UPDATE account_soul_balances SET balance = balance - ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE account_id = ? "
                        "AND item_id = ? AND balance >= ?",
                        (
                            requirement.count,
                            account_id,
                            requirement.item_id,
                            requirement.count,
                        ),
                    )
                    if int(changed.rowcount or 0) != 1:
                        raise _RegenerationReject(
                            "material_changed item=%d" % requirement.item_id
                        )
                    row = conn.execute(
                        "SELECT balance FROM account_soul_balances "
                        "WHERE account_id = ? AND item_id = ?",
                        (account_id, requirement.item_id),
                    ).fetchone()
                    material_remaining[requirement.item_id] = max(
                        0, int(row[0] or 0)
                    )
                    changed_material_slots.add(
                        int(account_soul_slot(requirement.item_id))
                    )
                    continue
                remaining_need = requirement.count
                rows = material_rows.get(requirement.item_id, ())
                for row in rows:
                    if remaining_need <= 0:
                        break
                    item_uid = int(row[0])
                    material_slot = int(row[1])
                    stack_before = max(0, int(row[3] or 0))
                    consumed = min(stack_before, remaining_need)
                    stack_after = stack_before - consumed
                    if stack_after <= 0:
                        changed = conn.execute(
                            "DELETE FROM character_items WHERE item_uid = ? "
                            "AND character_id = ? AND list_type = 0 "
                            "AND slot_index = ? AND item_template_id = ? "
                            "AND stack_count = ?",
                            (
                                item_uid,
                                cid,
                                material_slot,
                                requirement.item_id,
                                stack_before,
                            ),
                        )
                    else:
                        changed = conn.execute(
                            "UPDATE character_items SET stack_count = ?, "
                            "instance_value = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE item_uid = ? AND character_id = ? AND list_type = 0 "
                            "AND slot_index = ? AND item_template_id = ? "
                            "AND stack_count = ?",
                            (
                                stack_after,
                                stack_after,
                                item_uid,
                                cid,
                                material_slot,
                                requirement.item_id,
                                stack_before,
                            ),
                        )
                    if int(changed.rowcount or 0) != 1:
                        raise _RegenerationReject(
                            "material_changed item=%d slot=%d"
                            % (requirement.item_id, material_slot)
                        )
                    changed_material_slots.add(material_slot)
                    remaining_need -= consumed
                if remaining_need != 0:
                    raise _RegenerationReject(
                        "material_plan_incomplete item=%d remain=%d"
                        % (requirement.item_id, remaining_need)
                    )
                material_remaining[requirement.item_id] = sum(
                    max(0, int(row[0] or 0))
                    for row in conn.execute(
                        "SELECT stack_count FROM character_items "
                        "WHERE character_id = ? AND list_type = 0 "
                        "AND item_template_id = ?",
                        (cid, requirement.item_id),
                    ).fetchall()
                )

            if wallet is not None:
                wallet["gold"] = updated_gold
                changed = conn.execute(
                    "UPDATE character_wallets SET gold = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE character_id = ?",
                    (updated_gold, cid),
                )
                if int(changed.rowcount or 0) != 1:
                    raise _RegenerationReject("wallet_changed")
                repo._project_account_wallet_tx(conn, cid, wallet)

            changed = conn.execute(
                "UPDATE character_items SET item_template_id = ?, "
                "item_kind = 'equipment', stack_count = 1, instance_value = ?, "
                "durability = ?, seal_flag = ?, option_value = 0, expire_time = 0, "
                "marker_16 = 0, pet_serial_or_handle = 0, extra_json = '{}', "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ? "
                "AND character_id = ? AND list_type = 0 AND slot_index = ? "
                "AND item_template_id = ? AND stack_count = 1",
                (
                    result_item_id,
                    EQUIP_TOP_QUALITY_SEED,
                    durability,
                    result_seal_flag,
                    target_uid,
                    cid,
                    slot,
                    source_item_id,
                ),
            )
            if int(changed.rowcount or 0) != 1:
                raise _RegenerationReject("target_item_changed_during_commit")

            settlement = EquipmentRegenerationSettlement(
                target_slot=slot,
                target_item_uid=target_uid,
                source_item_id=source_item_id,
                result_item_id=result_item_id,
                changed_material_slots=tuple(sorted(changed_material_slots)),
                material_remaining=material_remaining,
                gold_cost=gold_need,
                updated_gold=updated_gold,
            )
        return True, (
            "equipment_regeneration_ok slot=%d item=%d->%d materials=%s gold=%d"
            % (
                slot,
                source_item_id,
                result_item_id,
                [
                    (requirement.item_id, requirement.count)
                    for requirement in requirements
                ],
                gold_need,
            )
        ), settlement
    except _RegenerationReject as exc:
        return False, str(exc), None
    except (sqlite3.Error, ValueError) as exc:
        return False, "equipment_regeneration_transaction_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), None


__all__ = [
    "EquipmentRegenerationSettlement",
    "apply_equipment_regeneration",
]
