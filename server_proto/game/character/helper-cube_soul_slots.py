# -*- coding: utf-8 -*-
"""Stage3 主背包特殊材料槽：账号小晶体 + 六级账号灵魂。

Stage3 IDA（``sub_1B676B0`` / ``sub_1B67800``，2026-07-19）Main list_type=0 带：

| 带 | 槽 | 说明 |
|----|----|------|
| material | 121..176 | 普通材料 |
| avatar emblem | 289..353 | 徽章（S4 仅到 344，Stage3 扩展） |
| **special** | **354..366** | 特殊材料区（S4 无完整灵魂槽） |

区内固定分工（对照 S4 晶块 + Stage3 扩展）：

- **小晶体 / 晶块** ``354..359``：6 色固定槽，账号共享（``accounts.cube_*``）
- **六级灵魂** ``360..365``：账号共享账本在每个角色背包的固定投影

S4A12 仅有账号晶块，**无**灵魂固定槽；本模块为 Stage3 自建。
"""
from __future__ import annotations

from typing import Any

# ── 槽位带（Stage3 真值）──────────────────────────────────────
SLOT_RANGE_AVATAR_EMBLEM_STAGE3 = (289, 353)
SLOT_RANGE_MATERIAL_SPECIAL = (354, 366)
SLOT_RANGE_CUBE_FRAGMENT = (354, 359)
SLOT_RANGE_SOUL_MATERIAL = (360, 365)

# ── 账号级小晶体（S4 CurrencyService + Stage3 同槽）──────────
# item_id → (accounts 列名, 固定 slot)
CUBE_FRAGMENT_MAP: dict[int, tuple[str, int]] = {
    3033: ("cube_black", 354),  # 黑色小晶块
    3034: ("cube_white", 355),  # 白色小晶块
    3035: ("cube_red", 356),  # 红色小晶块
    3036: ("cube_blue", 357),  # 蓝色小晶块
    3037: ("cube_clear", 358),  # 无色小晶块
    3262: ("cube_gold", 359),  # 金色小晶块
}

CUBE_FRAGMENT_IDS: frozenset[int] = frozenset(CUBE_FRAGMENT_MAP.keys())

# ``epicpieceinfo.etc`` 的配方仅引用 Legendary/Epic Soul，但客户端有完整的
# Common..Legendary 六级家族。这里使用冻结 PVF 实际 item id，不能按名称猜测。
ACCOUNT_SOUL_MAP: dict[int, int] = {
    10100115: 360,  # Common Soul
    10100116: 361,  # Uncommon Soul
    10099773: 362,  # Rare Soul
    10099774: 363,  # Unique Soul
    10099775: 364,  # Legendary Soul (Stage3 fixed orange slot)
    10158124: 365,  # Epic Soul (Stage3 fixed gold slot)
}
ACCOUNT_SOUL_IDS: frozenset[int] = frozenset(ACCOUNT_SOUL_MAP)


def is_cube_fragment(item_id: int) -> bool:
    return int(item_id) in CUBE_FRAGMENT_IDS


def cube_fragment_slot(item_id: int) -> int:
    entry = CUBE_FRAGMENT_MAP.get(int(item_id))
    return int(entry[1]) if entry else -1


def cube_fragment_column(item_id: int) -> str:
    entry = CUBE_FRAGMENT_MAP.get(int(item_id))
    return str(entry[0]) if entry else ""


def is_cube_fragment_slot(slot: int) -> bool:
    return SLOT_RANGE_CUBE_FRAGMENT[0] <= int(slot) <= SLOT_RANGE_CUBE_FRAGMENT[1]


def is_soul_material_slot(slot: int) -> bool:
    return SLOT_RANGE_SOUL_MATERIAL[0] <= int(slot) <= SLOT_RANGE_SOUL_MATERIAL[1]


def is_account_soul(item_id: int) -> bool:
    return int(item_id) in ACCOUNT_SOUL_IDS


def account_soul_slot(item_id: int) -> int:
    return int(ACCOUNT_SOUL_MAP.get(int(item_id), -1))


def is_soul_material(
    item_id: int,
    *,
    meta: Any | None = None,
    force_ids: set[int] | frozenset[int] | None = None,
) -> bool:
    """Compatibility name for the six account-owned Soul items only.

    ``meta`` and ``force_ids`` are retained for callers from the previous
    special-material implementation.  Other material outputs must follow
    their normal PVF slot range and never occupy the account projection band.
    """
    _ = meta
    return is_account_soul(item_id) or bool(
        force_ids is not None and int(item_id) in force_ids and is_account_soul(item_id)
    )


def material_special_slot_range_for_item(
    item_id: int,
    *,
    meta: Any | None = None,
) -> tuple[int, int] | None:
    """若属特殊材料区返回 (lo, hi)；小晶体返回单槽 (s,s)；否则 None。"""
    iid = int(item_id)
    if is_cube_fragment(iid):
        s = cube_fragment_slot(iid)
        return (s, s) if s >= 0 else None
    if is_account_soul(iid):
        slot = account_soul_slot(iid)
        return (slot, slot) if slot >= 0 else None
    return None


__all__ = [
    "SLOT_RANGE_AVATAR_EMBLEM_STAGE3",
    "SLOT_RANGE_MATERIAL_SPECIAL",
    "SLOT_RANGE_CUBE_FRAGMENT",
    "SLOT_RANGE_SOUL_MATERIAL",
    "CUBE_FRAGMENT_MAP",
    "CUBE_FRAGMENT_IDS",
    "ACCOUNT_SOUL_MAP",
    "ACCOUNT_SOUL_IDS",
    "is_cube_fragment",
    "cube_fragment_slot",
    "cube_fragment_column",
    "is_cube_fragment_slot",
    "is_soul_material_slot",
    "is_account_soul",
    "account_soul_slot",
    "is_soul_material",
    "material_special_slot_range_for_item",
]
