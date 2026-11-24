"""穿戴栏（Inventory Equipment / list_type=3）同步。

范围硬边界：
  - list_type=0 Main  = 背包 → S0/000D 全量（带 list_param16）
  - list_type=1 Avatar = 时装栏（本 helper 不碰）
  - list_type=3 Equipment = 已穿槽（时装 0..10；身体装备 12..）

对照结论（90qiyuan + S4A12，Stage3 入口仍以 IDA 为准）：
  - S4A12 进房 000D **不发** list3；穿/卸后用 **S0/000E** list_type=3 增量。
  - 90qiyuan 进房 000D 只发 list0/1/2；装备栏另走 **op14 / 0x000E** list3
    （body = u8 type + u16 count + entries；**无** list_param16）。
  - Stage3 000E 读序（loc_1667BD0）：u8 list_type | u16 count | count×108B。
  - Stage3 list_type==3 **不走** sub_1ABF960 容器表（type3 返回 NULL）；
    走角色对象 vtable+0xAC0(slot) 更新已有装备槽（由 USERINFO1 sub_166AED0 创建）。
  - 进房时序硬约束：0131 → USERINFO0(外观) → USERINFO1(166AED0 建槽) → 000E list3。

因此：装备栏禁止用 000D mode0；进房 delayed 在 USERINFO1 后发 000E list3；
穿卸后即时 000E list3（会话内槽对象已存在）。

穿戴真源：character_equipped_entries。
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.character_db import EQUIP_TOP_QUALITY_SEED
from game.character.customability_state import apply_customability_state_to_entry108
from game.character.item_metadata import (
    AURA_SKIN_EQUIP_SLOT,
    GUILD_MEDAL_EQUIP_SLOT,
    resolve_item_metadata,
)
from game.character.payloads import (
    ITEM_ENTRY_EQUIPMENT_LOCK_ID_OFFSET,
    ITEM_ENTRY_FORGING_LEVEL_OFFSET,
    ITEM_ENTRY_ENCHANT_INDEX_OFFSET,
    ITEM_ENTRY_ENCHANT_UPGRADE_COUNT_OFFSET,
    pack_avatar_inventory_wire_entry,
)
from game.chain_loader import load_chain

# InventoryListType.Equipment — 已穿槽，不是 list1 时装库存容器
INVENTORY_LIST_EQUIPMENT = 3

# Stage3 000E entry 硬编码 0x6C（与背包 000D/000E 同形）
EQUIP_UPDATE_ENTRY_SIZE = 108

_guardian_gem_socket = load_chain("helper-guardian_gem_socket.py")


class EquipBarInventoryItem:
    """装备栏 000E 条目所需字段。"""

    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
        "random_options",
        "option_value",
        "expire_time",
        "marker_16",
        "equipment_lock_id",
        "amplify_type",
        "amplify_value",
        "forging_level",
        "enchant_index",
        "enchant_upgrade_count",
        "is_avatar",
        "is_creature",
        "extra_json",
    )

    def __init__(
        self,
        slot_index: int,
        item_template_id: int,
        *,
        durability: int = 0,
        instance_value: int = EQUIP_TOP_QUALITY_SEED,
        seal_flag: int = 0,
        random_options: list | None = None,
        option_value: int = 0,
        expire_time: int = 0,
        marker_16: int = 0,
        equipment_lock_id: int = 0,
        amplify_type: int = 0,
        amplify_value: int = 0,
        forging_level: int = 0,
        enchant_index: int = 0,
        enchant_upgrade_count: int = 0,
        is_avatar: bool = False,
        is_creature: bool = False,
        extra_json: Any = None,
    ) -> None:
        self.slot_index = int(slot_index) & 0xFFFF
        self.item_template_id = int(item_template_id) & 0xFFFFFFFF
        self.stack_count = 1
        self.durability = max(0, int(durability or 0)) & 0xFFFF
        self.instance_value = max(0, int(instance_value or 0)) & 0xFFFFFFFF
        if not is_creature and self.instance_value < EQUIP_TOP_QUALITY_SEED:
            self.instance_value = EQUIP_TOP_QUALITY_SEED
        self.item_kind = "avatar" if is_avatar else "equipment"
        self.seal_flag = max(0, int(seal_flag or 0)) & 0xFF
        self.random_options = list(random_options or [])
        self.option_value = max(0, int(option_value or 0)) & 0xFF
        self.expire_time = max(0, int(expire_time or 0)) & 0xFFFFFFFF
        self.marker_16 = max(0, int(marker_16 or 0)) & 0xFFFFFFFF
        self.equipment_lock_id = (
            max(0, int(equipment_lock_id or 0)) & 0xFF
        )
        self.amplify_type = max(0, int(amplify_type or 0)) & 0xFF
        self.amplify_value = max(0, int(amplify_value or 0)) & 0xFFFF
        self.forging_level = max(0, int(forging_level or 0)) & 0xFF
        self.enchant_index = max(0, int(enchant_index or 0)) & 0xFFFFFFFF
        self.enchant_upgrade_count = (
            max(0, int(enchant_upgrade_count or 0)) & 0xFF
        )
        self.is_avatar = bool(is_avatar)
        self.is_creature = bool(is_creature)
        self.extra_json = extra_json


def list_equipment_bar_items(repo: Any, character_id: int) -> list[EquipBarInventoryItem]:
    """从 character_equipped_entries 映射为已穿栏条目。"""
    try:
        from game.chain_loader import load_chain

        _row_ro = load_chain("helper-random_option.py").parse_random_options_from_extra_json
    except Exception:
        _row_ro = lambda _x: []  # noqa: E731

    rows = repo.list_equipped_items(int(character_id))
    try:
        from game.chain_loader import load_chain

        rows = list(
            load_chain("helper-knight_shield.py").filter_body_equipment_entries(rows)
        )
    except Exception:
        pass
    out: list[EquipBarInventoryItem] = []
    for row in rows:
        item_id = int(row.get("item_id", 0) or 0) & 0xFFFFFFFF
        if item_id <= 0:
            continue
        slot = int(row.get("slot", 0) or 0) & 0xFF
        meta = resolve_item_metadata(item_id)
        is_avatar = bool(getattr(meta, "is_avatar", False))
        is_creature = bool(getattr(meta, "is_creature", False))
        marker_16 = int(row.get("marker_16", 0) or 0)
        dura = int(getattr(meta, "durability", 0) or 0) & 0xFFFF
        ro = _row_ro(row.get("extra_json"))
        from game.character.amplify_option import parse_amplify_state
        from game.character.weapon_forging import parse_forging_level
        from game.character.enchant_by_bead import parse_enchant_state

        amplify_type, amplify_value = parse_amplify_state(row.get("extra_json"))
        forging_level = parse_forging_level(row.get("extra_json"))
        enchant_index, enchant_upgrade_count = parse_enchant_state(
            row.get("extra_json")
        )
        out.append(
            EquipBarInventoryItem(
                slot,
                item_id,
                durability=dura,
                instance_value=(marker_16 if is_creature else EQUIP_TOP_QUALITY_SEED),
                seal_flag=0 if ro else 0,
                random_options=ro or [],
                option_value=(
                    0
                    if slot == AURA_SKIN_EQUIP_SLOT
                    else int(row.get("option_value", 0) or 0)
                ),
                expire_time=int(row.get("expire_time", 0) or 0),
                marker_16=marker_16,
                equipment_lock_id=int(row.get("equipment_lock_id", 0) or 0),
                amplify_type=amplify_type,
                amplify_value=amplify_value,
                forging_level=forging_level,
                enchant_index=enchant_index,
                enchant_upgrade_count=enchant_upgrade_count,
                is_avatar=is_avatar,
                is_creature=is_creature,
                extra_json=row.get("extra_json"),
            )
        )
    out.sort(key=lambda it: int(it.slot_index))
    return out


def pack_equipment_update_entry_108(
    slot_index: int,
    item_template_id: int,
    *,
    instance_value: int = EQUIP_TOP_QUALITY_SEED,
    durability: int = 0,
    seal_flag: int = 0,
    random_options: list | None = None,
    option_value: int = 0,
    amplify_type: int = 0,
    amplify_value: int = 0,
    forging_level: int = 0,
    enchant_index: int = 0,
    enchant_upgrade_count: int = 0,
    marker_16: int = 0xFFFFFFFF,
    equipment_lock_id: int = 0,
    expire_time: int = 0,
    is_creature: bool = False,
    extra_json: Any = None,
) -> bytes:
    """Stage3 装备栏 000E 单条 108B。

    前缀与背包 000D/000E 同形；Marker16@0x16 = -1 对齐 S4A12
    BuildRawEquipEntry / LoadEquipmentCommonItemForRefresh。
    已解封 RO → offset 60..71（与背包同布局；缺 RO 则穿上后仍显示「魔法封印」）。
    """
    row = bytearray(EQUIP_UPDATE_ENTRY_SIZE)
    qty = int(instance_value or 0) & 0xFFFFFFFF
    if not is_creature and qty < EQUIP_TOP_QUALITY_SEED:
        qty = EQUIP_TOP_QUALITY_SEED
    struct.pack_into(
        "<HII",
        row,
        0,
        int(slot_index) & 0xFFFF,
        int(item_template_id) & 0xFFFFFFFF,
        qty,
    )
    row[10] = max(0, int(option_value or 0)) & 0xFF
    struct.pack_into("<H", row, 11, max(0, int(durability or 0)) & 0xFFFF)
    row[13] = max(0, int(seal_flag or 0)) & 0xFF
    struct.pack_into(
        "<I",
        row,
        ITEM_ENTRY_ENCHANT_INDEX_OFFSET,
        max(0, int(enchant_index or 0)) & 0xFFFFFFFF,
    )
    row[ITEM_ENTRY_ENCHANT_UPGRADE_COUNT_OFFSET] = (
        max(0, int(enchant_upgrade_count or 0)) & 0xFF
    )
    row[19] = max(0, int(amplify_type or 0)) & 0xFF
    struct.pack_into("<H", row, 20, max(0, int(amplify_value or 0)) & 0xFFFF)
    # The equipped type-26 handle lives in Value, not Marker16. Stage3
    # sub_19D65B0 copies Marker16's low word into the creature remaining-time
    # field, so permanent creatures must use zero here. Body equipment keeps
    # the established -1 marker; absolute expiration remains at offset 43.
    marker = int(marker_16) & 0xFFFFFFFF
    struct.pack_into("<I", row, 0x16, marker)
    struct.pack_into(
        "<I", row, 43, max(0, int(expire_time or 0)) & 0xFFFFFFFF
    )
    row[ITEM_ENTRY_FORGING_LEVEL_OFFSET] = (
        max(0, int(forging_level or 0)) & 0xFF
    )
    row[ITEM_ENTRY_EQUIPMENT_LOCK_ID_OFFSET] = (
        max(0, int(equipment_lock_id or 0)) & 0xFF
    )
    if random_options:
        try:
            from game.chain_loader import load_chain

            load_chain("helper-random_option.py").apply_random_options_to_entry108(
                row, list(random_options)[:3]
            )
        except Exception:
            pass
    _guardian_gem_socket.apply_guardian_gem_sockets_to_entry108(row, extra_json)
    apply_customability_state_to_entry108(row, extra_json)
    return bytes(row)


def build_equipment_bar_update_000e_body(
    repo: Any,
    character_id: int,
) -> tuple[bytes, int, str]:
    """构造 S0/000E 已穿栏 body（list_type=3）。

    Wire（Stage3 sub_1667BD0 / 90qiyuan op14 equipment path）::
      u8  list_type = 3
      u16 count
      count × entry（身体装备 108B；时装 108B + 两个 length blob）

    **禁止** 带 list_param16 的 000D mode0 头。

    Returns:
        (body, count, note)
    """
    items = list_equipment_bar_items(repo, int(character_id))
    body = bytearray()
    body += struct.pack("<BH", INVENTORY_LIST_EQUIPMENT, len(items) & 0xFFFF)
    ro_n = 0
    for it in items:
        if it.random_options:
            ro_n += 1
        if it.is_avatar:
            body += pack_avatar_inventory_wire_entry(it)
        else:
            body += pack_equipment_update_entry_108(
                int(it.slot_index),
                int(it.item_template_id),
                instance_value=int(it.instance_value),
                durability=int(it.durability),
                seal_flag=int(it.seal_flag),
                random_options=list(it.random_options or []),
                option_value=int(it.option_value),
                amplify_type=int(it.amplify_type),
                amplify_value=int(it.amplify_value),
                forging_level=int(it.forging_level),
                enchant_index=int(it.enchant_index),
                enchant_upgrade_count=int(it.enchant_upgrade_count),
                marker_16=(0 if it.is_creature else 0xFFFFFFFF),
                equipment_lock_id=int(it.equipment_lock_id),
                expire_time=int(it.expire_time),
                is_creature=bool(it.is_creature),
                extra_json=it.extra_json,
            )
    slots = ",".join(
        "%s:%s" % (int(it.slot_index), int(it.item_template_id)) for it in items
    )
    avatar_n = sum(1 for it in items if it.is_avatar)
    creature_n = sum(1 for it in items if it.is_creature)
    note = "S0/000E equip_bar list_type=%s n=%s avatar=%s creature=%s ro=%s slots=%s plain_len=%s" % (
        INVENTORY_LIST_EQUIPMENT,
        len(items),
        avatar_n,
        creature_n,
        ro_n,
        slots or "-",
        len(body),
    )
    return bytes(body), len(items), note


def build_guardian_gem_refresh_000e_body(
    repo: Any,
    character_id: int,
) -> tuple[bytes, int, str]:
    """Build the minimal list3 refresh needed for worn guardian gems.

    USERINFO1 creates the worn medal object but its compact equipment row has
    no room for the four 108-byte guardian-gem socket words.  After a dungeon
    map is installed, refresh only slot 31 when the persisted medal instance
    actually contains a gem.  An empty result keeps the normal enter flow
    unchanged.
    """
    try:
        equipped = list_equipment_bar_items(repo, int(character_id))
    except Exception as exc:
        return b"", 0, "S0/000E guardian_gem_refresh skip=repo_error:%s" % (
            type(exc).__name__,
        )
    items = [
        item
        for item in equipped
        if int(item.slot_index) == GUILD_MEDAL_EQUIP_SLOT
        and any(
            int(value) > 0
            for value in _guardian_gem_socket.parse_guardian_gem_sockets(
                item.extra_json
            )
        )
    ]
    if not items:
        return b"", 0, "S0/000E guardian_gem_refresh skip=none"

    item = items[0]
    body = struct.pack("<BH", INVENTORY_LIST_EQUIPMENT, 1)
    body += pack_equipment_update_entry_108(
        int(item.slot_index),
        int(item.item_template_id),
        instance_value=int(item.instance_value),
        durability=int(item.durability),
        seal_flag=int(item.seal_flag),
        random_options=list(item.random_options or []),
        option_value=int(item.option_value),
        amplify_type=int(item.amplify_type),
        amplify_value=int(item.amplify_value),
        forging_level=int(item.forging_level),
        enchant_index=int(item.enchant_index),
        enchant_upgrade_count=int(item.enchant_upgrade_count),
        marker_16=0 if item.is_creature else 0xFFFFFFFF,
        equipment_lock_id=int(item.equipment_lock_id),
        expire_time=int(item.expire_time),
        is_creature=bool(item.is_creature),
        extra_json=item.extra_json,
    )
    sockets = _guardian_gem_socket.parse_guardian_gem_sockets(item.extra_json)
    return bytes(body), 1, (
        "S0/000E guardian_gem_refresh slot=%d medal=%d sockets=%s plain_len=%d"
        % (
            int(item.slot_index),
            int(item.item_template_id),
            ",".join(str(int(value)) for value in sockets),
            len(body),
        )
    )


# 兼容旧名：历史调用 build_equipment_bar_itemlist_body 的站点改为 000E
def build_equipment_bar_itemlist_body(
    repo: Any,
    character_id: int,
    *,
    list_param16: int = 0,
) -> tuple[bytes, int, str]:
    """兼容入口：忽略 list_param16，始终构造 000E list3 body。"""
    _ = list_param16
    return build_equipment_bar_update_000e_body(repo, int(character_id))


__all__ = [
    "INVENTORY_LIST_EQUIPMENT",
    "EQUIP_UPDATE_ENTRY_SIZE",
    "EquipBarInventoryItem",
    "list_equipment_bar_items",
    "pack_equipment_update_entry_108",
    "build_equipment_bar_update_000e_body",
    "build_guardian_gem_refresh_000e_body",
    "build_equipment_bar_itemlist_body",
]
