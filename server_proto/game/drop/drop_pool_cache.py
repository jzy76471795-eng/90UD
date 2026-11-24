"""Explicit cache payload for equipment/stackable drop pools."""
from __future__ import annotations

import json
from typing import Any


CACHE_DOMAIN = "drop_pools"
CACHE_SCHEMA = 2
CACHE_SEMANTIC_VERSION = 2
_ITEMDICT_PATHS = {
    "etc/itemdictionary/(r)itemdictionary.etc",
    "etc/itemdictionary/(f)itemdictionary.etc",
}


def _positive_item_id(value: Any) -> int:
    item_id = int(value)
    if item_id <= 0 or item_id > 0xFFFFFFFF:
        raise ValueError("drop pool item ID is out of range")
    return item_id


def _pool_key(value: Any, *, max_rarity: int) -> int:
    key = int(value)
    grade, rarity = divmod(key, 10)
    if not 1 <= grade <= 200 or not 0 <= rarity <= max_rarity:
        raise ValueError("drop pool key is invalid")
    return key


def restore(payload: Any) -> Any:
    if not isinstance(payload, dict) or int(payload.get("schema", -1)) != CACHE_SCHEMA:
        raise ValueError("drop pool cache schema mismatch")

    from game.drop.equip_stackable_pools import EquipStackablePools

    raw_equip_ids = payload.get("equipment_ids")
    raw_stackable_ids = payload.get("stackable_ids")
    raw_equip_pool = payload.get("equipment_pool")
    raw_stackable_pool = payload.get("stackable_pool")
    if not all(
        isinstance(value, list)
        for value in (
            raw_equip_ids,
            raw_stackable_ids,
            raw_equip_pool,
            raw_stackable_pool,
        )
    ):
        raise ValueError("drop pool cache collections are invalid")

    equipment_ids = {_positive_item_id(value) for value in raw_equip_ids}
    stackable_ids = {_positive_item_id(value) for value in raw_stackable_ids}
    if (
        not equipment_ids
        or not stackable_ids
        or len(equipment_ids) != len(raw_equip_ids)
        or len(stackable_ids) != len(raw_stackable_ids)
    ):
        raise ValueError("drop pool registry identity is invalid")

    equipment_pool: dict[int, list[tuple[int, int]]] = {}
    equipment_count = 0
    for bucket in raw_equip_pool:
        if not isinstance(bucket, list) or len(bucket) != 2 or not isinstance(bucket[1], list):
            raise ValueError("equipment drop pool bucket is invalid")
        key = _pool_key(bucket[0], max_rarity=5)
        if key in equipment_pool:
            raise ValueError("duplicate equipment drop pool bucket")
        rows: list[tuple[int, int]] = []
        for row in bucket[1]:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("equipment drop pool row is invalid")
            item_id = _positive_item_id(row[0])
            weight = int(row[1])
            if item_id not in equipment_ids or weight <= 0:
                raise ValueError("equipment drop pool reference is invalid")
            rows.append((item_id, weight))
        if not rows:
            raise ValueError("equipment drop pool bucket is empty")
        equipment_pool[key] = rows
        equipment_count += len(rows)

    stackable_pool: dict[int, list[tuple[int, int]]] = {}
    stackable_count = 0
    for bucket in raw_stackable_pool:
        if not isinstance(bucket, list) or len(bucket) != 2 or not isinstance(bucket[1], list):
            raise ValueError("stackable drop pool bucket is invalid")
        key = _pool_key(bucket[0], max_rarity=6)
        if key in stackable_pool:
            raise ValueError("duplicate stackable drop pool bucket")
        rows: list[tuple[int, int]] = []
        for row in bucket[1]:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("stackable drop pool row is invalid")
            item_id = _positive_item_id(row[0])
            weight = int(row[1])
            if item_id not in stackable_ids or weight <= 0:
                raise ValueError("stackable drop pool reference is invalid")
            rows.append((item_id, weight))
        if not rows:
            raise ValueError("stackable drop pool bucket is empty")
        stackable_pool[key] = rows
        stackable_count += len(rows)

    equip_from_dict = int(payload.get("equip_from_dict", -1))
    equip_from_equ = int(payload.get("equip_from_equ", -1))
    equip_non_lst_rejected = int(payload.get("equip_non_lst_rejected", -1))
    if (
        equipment_count != int(payload.get("equip_count", -1))
        or stackable_count != int(payload.get("stackable_count", -1))
        or equip_from_dict < 0
        or equip_from_equ < 0
        or equip_from_dict + equip_from_equ != equipment_count
        or equip_non_lst_rejected < 0
    ):
        raise ValueError("drop pool cache counts are invalid")
    itemdict_path = str(payload.get("itemdict_path") or "").replace("\\", "/").lower()
    if itemdict_path and itemdict_path not in _ITEMDICT_PATHS:
        raise ValueError("drop pool ItemDictionary path is invalid")
    if not itemdict_path and equip_from_dict != 0:
        raise ValueError("drop pool ItemDictionary source count is invalid")

    pools = EquipStackablePools()
    pools.equip_pool = equipment_pool
    pools.stackable_pool = stackable_pool
    pools.item_ids_equip = equipment_ids
    pools.item_ids_stackable = stackable_ids
    pools.itemdict_path = itemdict_path
    pools.equip_count = equipment_count
    pools.equip_from_dict = equip_from_dict
    pools.equip_from_equ = equip_from_equ
    pools.stackable_count = stackable_count
    pools.equip_non_lst_rejected = equip_non_lst_rejected
    pools.loaded = True
    return pools


def to_payload(pools: Any) -> dict[str, Any]:
    if pools is None or not bool(getattr(pools, "loaded", False)):
        raise ValueError("loaded drop pools required")
    payload = {
        "schema": CACHE_SCHEMA,
        "itemdict_path": str(pools.itemdict_path),
        "equip_count": int(pools.equip_count),
        "equip_from_dict": int(pools.equip_from_dict),
        "equip_from_equ": int(pools.equip_from_equ),
        "stackable_count": int(pools.stackable_count),
        "equip_non_lst_rejected": int(pools.equip_non_lst_rejected),
        "equipment_ids": sorted(int(value) for value in pools.item_ids_equip),
        "stackable_ids": sorted(int(value) for value in pools.item_ids_stackable),
        "equipment_pool": [
            [
                int(key),
                [[int(item_id), int(weight)] for item_id, weight in values],
            ]
            for key, values in sorted(pools.equip_pool.items())
        ],
        "stackable_pool": [
            [
                int(key),
                [[int(item_id), int(weight)] for item_id, weight in values],
            ]
            for key, values in sorted(pools.stackable_pool.items())
        ],
    }
    # Reuse the restore validator so the published form and hit path enforce
    # exactly the same references and counts.
    restore(payload)
    json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return payload


def record_count(pools: Any) -> int:
    return (
        len(pools.item_ids_equip)
        + len(pools.item_ids_stackable)
        + int(pools.equip_count)
        + int(pools.stackable_count)
    )


__all__ = [
    "CACHE_DOMAIN",
    "CACHE_SCHEMA",
    "CACHE_SEMANTIC_VERSION",
    "record_count",
    "restore",
    "to_payload",
]
