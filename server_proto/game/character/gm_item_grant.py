"""Capacity-checked persistent item grants used by GM control commands."""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    SLOT_RANGE_AVATAR_EMBLEM,
    SLOT_RANGE_CONSUMABLE,
    SLOT_RANGE_EQUIPMENT,
    SLOT_RANGE_MATERIAL,
    SLOT_RANGE_MATERIAL_EXPERT,
    SLOT_RANGE_MATERIAL_EXT,
    SLOT_RANGE_PET_ARTIFACT,
    SLOT_RANGE_PET_CONSUMABLE,
    SLOT_RANGE_PET_CREATURE,
    SLOT_RANGE_GUILD_MEDAL,
    SLOT_RANGE_GUARDIAN_GEM,
    SLOT_RANGE_QUICK,
    SLOT_RANGE_QUEST,
    resolve_item_metadata,
)
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.character.character_db import is_virtual_inventory_grant
from game.shop.system_reward_activation import (
    SYSTEM_REWARD_PREMIUM,
    classify_system_reward,
)


GM_GRANT_COUNT_MAX = 9999
_cube_soul = load_chain("helper-cube_soul_slots.py")
GM_INVENTORY_SECTIONS = (
    "all",
    "quick",
    "personal_cargo",
    "account_cargo",
    "equipment",
    "consumable",
    "material",
    "quest",
    "avatar",
    "emblem",
    "pet",
    "pet_equipment",
    "guild_medal",
    "guardian_gem",
)


def _target_list_type(metadata: Any) -> int:
    resolver = getattr(metadata, "inventory_list_type", None)
    if callable(resolver):
        return int(resolver())
    if bool(getattr(metadata, "is_pet_inventory_item", False)):
        return 7
    if bool(getattr(metadata, "is_avatar", False)):
        return 1
    return 0


def _slot_ranges(repo: Any, character_id: int, metadata: Any, list_type: int):
    if list_type == 1:
        expansion = int(repo.get_container_list_param(character_id, list_type=1))
        return [(0, int(repo.BASE_AVATAR_SLOT_COUNT) + expansion - 1)]
    pet_range = metadata.pet_slot_range() if list_type == 7 else None
    start, end = pet_range if pet_range is not None else metadata.slot_range()
    start, end = int(start), int(end)
    if end < start:
        start, end = end, start
    ranges = [(start, end)]
    prefer_quick = str(
        os.environ.get("USDOF_GAME_GRANT_CONSUMABLE_PREFER_QUICK_SLOT", "1")
    ).strip().lower() not in {"0", "false", "off", "no"}
    if (
        list_type == 0
        and prefer_quick
        and not bool(getattr(metadata, "is_equipment", False))
        and (start, end) == SLOT_RANGE_CONSUMABLE
    ):
        ranges = [SLOT_RANGE_QUICK, (start, end)]
    return ranges


def _row_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("item_template_id", 0) or 0),
        int(row.get("stack_count", 0) or 0),
        int(row.get("instance_value", 0) or 0),
        int(row.get("durability", 0) or 0),
        int(row.get("seal_flag", 0) or 0),
        int(row.get("option_value", 0) or 0),
        int(row.get("expire_time", 0) or 0),
        str(row.get("item_kind", "") or ""),
    )


def _rows_by_slot(repo: Any, character_id: int, list_type: int) -> dict[int, dict[str, Any]]:
    return {
        int(row.get("slot_index", -1)): row
        for row in repo.list_inventory_items(character_id, list_type=list_type)
        if int(row.get("slot_index", -1)) >= 0
    }


def _gm_grant_seal_flag(metadata: Any) -> int:
    """Project PVF sealing equipment to the Stage3 sealed instance state."""
    if not bool(getattr(metadata, "is_equipment", False)):
        return 0
    normalizer = getattr(metadata, "normalized_attach_type", None)
    attach_type = normalizer() if callable(normalizer) else ""
    return 1 if str(attach_type or "").strip().lower() == "sealing" else 0


def _has_capacity(
    rows: dict[int, dict[str, Any]],
    metadata: Any,
    item_id: int,
    count: int,
    ranges: list[tuple[int, int]],
) -> bool:
    remaining = int(count)
    is_equipment = bool(getattr(metadata, "is_equipment", False))
    allows_merge = bool(getattr(metadata, "allows_stack_merge", False)) and not is_equipment
    stack_limit = max(0, int(getattr(metadata, "effective_stack_limit", 1) or 0))
    allowed_slots = {
        slot
        for start, end in ranges
        for slot in range(int(start), int(end) + 1)
    }
    if allows_merge:
        for slot in sorted(allowed_slots):
            row = rows.get(slot)
            if row is None or int(row.get("item_template_id", 0) or 0) != item_id:
                continue
            current = max(0, int(row.get("stack_count", 0) or 0))
            capacity = remaining if stack_limit == 0 else max(0, stack_limit - current)
            remaining -= min(remaining, capacity)
            if remaining <= 0:
                return True
    free_slots = 0
    for slot in allowed_slots:
        if slot in rows:
            continue
        if _target_list_type(metadata) == 0 and _cube_soul.is_cube_fragment_slot(slot):
            continue
        free_slots += 1
    if allows_merge:
        if stack_limit == 0:
            return free_slots >= 1
        return free_slots * stack_limit >= remaining
    return free_slots >= remaining

def grant_gm_item(
    repo: Any,
    *,
    character_id: int,
    item_id: int,
    count: int = 1,
) -> tuple[bool, str, dict[str, Any]]:
    cid = int(character_id)
    iid = int(item_id)
    quantity = int(count)
    if cid <= 0 or iid <= 0:
        return False, "invalid_character_or_item", {}
    if not 1 <= quantity <= GM_GRANT_COUNT_MAX:
        return False, "count_out_of_range", {}
    metadata = resolve_item_metadata(iid)
    if str(getattr(metadata, "item_kind", "")) not in {"equipment", "stackable"}:
        return False, "item_metadata_unavailable", {}
    list_type = _target_list_type(metadata)
    before = _rows_by_slot(repo, cid, list_type)
    activation = classify_system_reward(metadata)
    virtual_account_item = bool(
        is_revive_coin_grant_item_id(iid)
        or _cube_soul.is_cube_fragment(iid)
        or _cube_soul.is_account_soul(iid)
        or (activation is not None and activation.kind == SYSTEM_REWARD_PREMIUM)
    )
    ranges = _slot_ranges(repo, cid, metadata, list_type)
    if not virtual_account_item and not _has_capacity(
        before,
        metadata,
        iid,
        quantity,
        ranges,
    ):
        return False, "container_full", {
            "item_id": iid,
            "count": quantity,
            "list_type": list_type,
        }
    grant_kwargs: dict[str, Any] = {"list_type": list_type}
    grant_kwargs["seal_flag"] = _gm_grant_seal_flag(metadata)
    if list_type in {1, GUILD_MEDAL_INVENTORY_LIST_TYPE}:
        grant_kwargs["slot_start"], grant_kwargs["slot_end"] = ranges[0]
    ok, note, first_slot = repo.grant_inventory_item(
        cid,
        iid,
        quantity,
        require_full_capacity=True,
        **grant_kwargs,
    )
    if not ok or (int(first_slot) < 0 and not is_virtual_inventory_grant(note)):
        return False, str(note or "grant_failed"), {}
    after = _rows_by_slot(repo, cid, list_type)
    changed_slots = sorted(
        slot
        for slot in set(before) | set(after)
        if _row_signature(before.get(slot, {})) != _row_signature(after.get(slot, {}))
    )
    if not changed_slots and int(first_slot) >= 0:
        changed_slots = [int(first_slot)]
    return True, str(note or "ok"), {
        "character_id": cid,
        "item_id": iid,
        "name": str(getattr(metadata, "name", "") or ""),
        "count": quantity,
        "list_type": list_type,
        "changed_slots": changed_slots,
        "first_slot": int(first_slot),
        "delivery_flow": "database_then_container_update",
    }


def _section_target(
    repo: Any,
    character_id: int,
    metadata: Any,
    section: str,
) -> tuple[int, tuple[int, int]] | None:
    native_range = tuple(int(value) for value in metadata.slot_range())
    pet_range_raw = metadata.pet_slot_range()
    pet_range = (
        tuple(int(value) for value in pet_range_raw)
        if pet_range_raw is not None
        else None
    )
    if section == "quick":
        if metadata.is_stackable and not metadata.is_pet_inventory_item and native_range == SLOT_RANGE_CONSUMABLE:
            return 0, SLOT_RANGE_QUICK
        return None
    if section == "personal_cargo":
        if (
            metadata.item_kind not in {"equipment", "stackable"}
            or metadata.is_avatar
            or metadata.is_pet_inventory_item
            or metadata.is_guild_medal_inventory_item
        ):
            return None
        capacity = int(repo.get_container_list_param(character_id, list_type=2))
        return (2, (0, capacity - 1)) if capacity > 0 else None
    if section == "equipment":
        return (
            (0, SLOT_RANGE_EQUIPMENT)
            if metadata.is_equipment
            and not metadata.is_avatar
            and not metadata.is_pet_inventory_item
            and not metadata.is_guild_medal_inventory_item
            else None
        )
    if section == "consumable":
        return (0, SLOT_RANGE_CONSUMABLE) if metadata.is_stackable and not metadata.is_pet_inventory_item and native_range == SLOT_RANGE_CONSUMABLE else None
    if section == "material":
        if _cube_soul.is_cube_fragment(metadata.item_id) or _cube_soul.is_account_soul(metadata.item_id):
            return 0, SLOT_RANGE_MATERIAL_EXT
        material_ranges = {
            SLOT_RANGE_MATERIAL,
            SLOT_RANGE_MATERIAL_EXPERT,
            SLOT_RANGE_MATERIAL_EXT,
        }
        return (0, native_range) if metadata.is_stackable and native_range in material_ranges else None
    if section == "quest":
        return (0, SLOT_RANGE_QUEST) if metadata.is_stackable and native_range == SLOT_RANGE_QUEST else None
    if section == "avatar":
        if not metadata.is_avatar or metadata.is_pet_inventory_item:
            return None
        expansion = int(repo.get_container_list_param(character_id, list_type=1))
        return 1, (0, int(repo.BASE_AVATAR_SLOT_COUNT) + expansion - 1)
    if section == "emblem":
        return (0, SLOT_RANGE_AVATAR_EMBLEM) if metadata.is_stackable and native_range == SLOT_RANGE_AVATAR_EMBLEM else None
    if section == "pet":
        if pet_range in {SLOT_RANGE_PET_CREATURE, SLOT_RANGE_PET_CONSUMABLE}:
            return 7, pet_range
        return None
    if section == "pet_equipment":
        return (7, SLOT_RANGE_PET_ARTIFACT) if pet_range == SLOT_RANGE_PET_ARTIFACT else None
    if section == "guild_medal":
        return (
            (GUILD_MEDAL_INVENTORY_LIST_TYPE, SLOT_RANGE_GUILD_MEDAL)
            if metadata.is_guild_medal
            else None
        )
    if section == "guardian_gem":
        return (
            (GUILD_MEDAL_INVENTORY_LIST_TYPE, SLOT_RANGE_GUARDIAN_GEM)
            if metadata.is_guardian_gem
            else None
        )
    return None


def grant_inventory_item_to_section(
    repo: Any,
    *,
    character_id: int,
    item_id: int,
    count: int,
    section: str,
    require_full_capacity: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """Capacity-check and add a known item to one selected persisted container."""
    cid = int(character_id)
    iid = int(item_id)
    quantity = int(count)
    target_section = str(section or "").strip().lower()
    if cid <= 0 or iid <= 0 or target_section not in GM_INVENTORY_SECTIONS:
        return False, "invalid_character_item_or_section", {}
    if target_section == "all" or not 1 <= quantity <= GM_GRANT_COUNT_MAX:
        return False, "invalid_section_or_count", {}

    metadata = resolve_item_metadata(iid)
    if is_revive_coin_grant_item_id(iid):
        before = _rows_by_slot(repo, cid, 0)
        ok, note, first_slot = repo.grant_inventory_item(cid, iid, quantity)
        if not ok or int(first_slot) < 0:
            return False, str(note or "grant_failed"), {}
        after = _rows_by_slot(repo, cid, 0)
        changed_slots = sorted(
            slot
            for slot in set(before) | set(after)
            if _row_signature(before.get(slot, {}))
            != _row_signature(after.get(slot, {}))
        )
        if not changed_slots:
            changed_slots = [int(first_slot)]
        return True, str(note or "ok"), {
            "character_id": cid,
            "item_id": iid,
            "name": str(metadata.name or ""),
            "count": quantity,
            "list_type": 0,
            "section": target_section,
            "changed_slots": changed_slots,
            "first_slot": int(first_slot),
            "delivery_flow": "account_revive_coin_wallet",
        }
    activation = classify_system_reward(metadata)
    if activation is not None and activation.kind == SYSTEM_REWARD_PREMIUM:
        ok, note, first_slot = repo.grant_inventory_item(cid, iid, quantity)
        if not ok or (int(first_slot) < 0 and not is_virtual_inventory_grant(note)):
            return False, str(note or "grant_failed"), {}
        return True, str(note or "ok"), {
            "character_id": cid,
            "item_id": iid,
            "name": str(metadata.name or ""),
            "count": quantity,
            "list_type": 0,
            "section": target_section,
            "changed_slots": [],
            "first_slot": int(first_slot),
            "delivery_flow": "account_premium_activation",
        }
    if target_section == "account_cargo":
        if bool(metadata.is_guild_medal_inventory_item):
            return False, "item_section_mismatch", {}
        account_id = repo.get_account_id_for_character(cid)
        if account_id is None:
            return False, "account_missing", {}
        ok, note, detail = repo.grant_account_cargo_item(
            int(account_id),
            iid,
            quantity,
            seal_flag=_gm_grant_seal_flag(metadata),
        )
        if ok:
            detail.update(
                {
                    "character_id": cid,
                    "name": str(metadata.name or ""),
                    "section": target_section,
                    "delivery_flow": "database_then_container_update",
                }
            )
        return ok, note, detail

    target = _section_target(repo, cid, metadata, target_section)
    if target is None:
        return False, "item_section_mismatch", {}
    list_type, slot_range = target
    before = _rows_by_slot(repo, cid, list_type)
    virtual_account_item = bool(
        is_revive_coin_grant_item_id(iid)
        or _cube_soul.is_cube_fragment(iid)
        or _cube_soul.is_account_soul(iid)
        or (activation is not None and activation.kind == SYSTEM_REWARD_PREMIUM)
    )
    if not virtual_account_item and not _has_capacity(
        before,
        metadata,
        iid,
        quantity,
        [slot_range],
    ):
        return False, "container_full", {
            "item_id": iid,
            "count": quantity,
            "list_type": list_type,
            "section": target_section,
        }
    grant_kwargs = {
        "list_type": list_type,
        "slot_start": slot_range[0],
        "slot_end": slot_range[1],
        "seal_flag": _gm_grant_seal_flag(metadata),
    }
    if require_full_capacity:
        grant_kwargs["require_full_capacity"] = True
    ok, note, first_slot = repo.grant_inventory_item(
        cid,
        iid,
        quantity,
        **grant_kwargs,
    )
    if not ok or (int(first_slot) < 0 and not is_virtual_inventory_grant(note)):
        return False, str(note or "grant_failed"), {}
    after = _rows_by_slot(repo, cid, list_type)
    changed_slots = sorted(
        slot
        for slot in set(before) | set(after)
        if _row_signature(before.get(slot, {})) != _row_signature(after.get(slot, {}))
    )
    if not changed_slots and int(first_slot) >= 0:
        changed_slots = [int(first_slot)]
    return True, str(note or "ok"), {
        "character_id": cid,
        "item_id": iid,
        "name": str(metadata.name or ""),
        "count": quantity,
        "list_type": list_type,
        "section": target_section,
        "changed_slots": changed_slots,
        "first_slot": int(first_slot),
        "delivery_flow": "database_then_container_update",
    }


def grant_gm_item_to_section(
    repo: Any,
    *,
    character_id: int,
    item_id: int,
    count: int,
    section: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Compatibility entry for the GM control surface."""
    return grant_inventory_item_to_section(
        repo,
        character_id=character_id,
        item_id=item_id,
        count=count,
        section=section,
        require_full_capacity=True,
    )


__all__ = [
    "GM_GRANT_COUNT_MAX",
    "GM_INVENTORY_SECTIONS",
    "grant_gm_item",
    "grant_inventory_item_to_section",
    "grant_gm_item_to_section",
]
