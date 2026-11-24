"""USERINFO1 装备槽创建（Stage3 sub_166AED0）。

职责边界：
  - USERINFO0 / sub_18CC430：19B 紧凑穿戴表（模型/纸娃娃）——见 helper-userinfo_equipment
  - USERINFO1 / sub_166AED0：完整 Inventory 装备槽对象创建（vtable+1852 挂槽）
  - S0/000E list3 / sub_1667BD0：只更新已有槽（vtable+0xAC0）；槽不存在则静默跳过

对照：
  - S4A12 UserInfoSubtype1Builder：ex_equip 后 u8 count + InvenItem.Write ×n + clone_title
  - 90qiyuan C# subtype1：同样把 equipped RawEntry 写进 subtype1（buildCSharpUserInfoSubtype1）
  - 90qiyuan 新 EXE mode1：sub_1D77560 create row（56B 基线，含 emblem+rune）
  - Stage3 type1 调用：sub_166AED0(char_obj, 1, 0, 0) @ 0x18D3CB5

Stage3 最小 body 装备行（item type>11 且 !=26、无 slot9 linked）：53B
  与 S4A12 InvenItem 43B / 90qiyuan mode1 56B 都不同——以 IDA 读序为准。
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.character_db import EQUIP_TOP_QUALITY_SEED
from game.character.item_metadata import (
    AURA_ATTRIBUTE_EQUIP_SLOT,
    AURA_SKIN_EQUIP_SLOT,
    resolve_item_metadata,
)
from game.chain_loader import load_chain


_avatar_socket = load_chain("helper-avatar_socket.py")

# Stage3 sub_166AED0 body-equipment entry length. Avatar rows add two dynamic
# u32-length blobs after the common 24-byte prefix; 61 is the empty-blob floor.
USERINFO1_EQUIP_ENTRY_MIN_SIZE = 53
USERINFO1_AVATAR_ENTRY_MIN_SIZE = 61
USERINFO1_CREATURE_ENTRY_MIN_SIZE = 57


def pack_userinfo1_equip_entry_166aed0(
    slot: int,
    item_id: int,
    *,
    instance_value: int = EQUIP_TOP_QUALITY_SEED,
    durability: int = 0,
    attr: int = 0,
    clear_avatar: int = 0,
    enchant: int = 0,
    enchant_upgrade: int = 0,
    amplify_type: int = 0,
    amplify_value: int = 0,
    is_avatar: bool = False,
    is_creature: bool = False,
    creature_expire_time: int = 0,
    extra_json: Any = None,
    avatar_metadata: Any = None,
) -> bytes:
    """编码一条 Stage3 sub_166AED0 装备行（body 槽最小形）。

    读序（IDA sub_166AED0，body 装备 / item type>11）：
      u8  slot
      u32 item_id
      u32 value/instance
      u8  attr
      u16 durability
      u32 clear_avatar          # 0；非 0 且 slot==9 会再读 linked raw
      u32 enchant
      u8  enchant_upgrade
      u8  amplify_type
      u16 amplify_value
      # avatar item 时 1AAEB60/1AAEBB0 各读 u32len+blob
      # type==26 时多读 u32 creature_expire_time
      u8  chronicle_count       # 1663B30；0
      u32 trailing              # v76
      u8  seal_count            # 1570E00；0
      10B fixed zeros           # v89,v65,v87,v67,v88,v58,v64,v66,v68
      u32 rawA_len=0
      u32 rawB_len=0
      u8  mid=0                 # v72
      u32 rawC_len=0

    注意：clear_avatar 必须用 0 而非 0xFFFFFFFF；后者对 slot9 会强制读 linked 块。
    ``is_avatar`` 时 ``durability`` 参数承载的是同位宽的 AbilityNo。
    """
    qty = int(instance_value or 0) & 0xFFFFFFFF
    if not is_avatar and not is_creature and qty < EQUIP_TOP_QUALITY_SEED:
        qty = EQUIP_TOP_QUALITY_SEED
    body = bytearray()
    # slot u8 | item_id u32 | value u32 | attr u8 | durability u16
    body += struct.pack(
        "<BIIBH",
        int(slot) & 0xFF,
        int(item_id) & 0xFFFFFFFF,
        qty,
        int(attr) & 0xFF,
        max(0, int(durability or 0)) & 0xFFFF,
    )
    # clear_avatar u32 | enchant u32 | eu u8 | amp_type u8 | amp_val u16
    body += struct.pack(
        "<IIBBH",
        int(clear_avatar) & 0xFFFFFFFF,
        int(enchant) & 0xFFFFFFFF,
        int(enchant_upgrade) & 0xFF,
        int(amplify_type) & 0xFF,
        int(amplify_value) & 0xFFFF,
    )
    avatar_extra_size = 0
    if is_avatar:
        avatar_extra = _avatar_socket.pack_avatar_extra_wire(
            item_id,
            extra_json,
            metadata=avatar_metadata,
        )
        body += avatar_extra
        avatar_extra_size = len(avatar_extra)
    if is_creature:
        # Stage3 sub_166AED0 reads the absolute creature expiration after the
        # common prefix. Permanent creatures use zero.
        body += struct.pack(
            "<I", max(0, int(creature_expire_time or 0)) & 0xFFFFFFFF
        )
    # chronicle count + trailing
    body += b"\x00"
    body += struct.pack("<I", 0)
    # seal count empty
    body += b"\x00"
    # 10 fixed scalar zeros
    body += b"\x00" * 10
    # three empty length-prefixed blobs with mid u8 between 2nd and 3rd
    body += struct.pack("<II", 0, 0)
    body += b"\x00"
    body += struct.pack("<I", 0)
    if is_avatar:
        expected_size = USERINFO1_EQUIP_ENTRY_MIN_SIZE + avatar_extra_size
    elif is_creature:
        expected_size = USERINFO1_CREATURE_ENTRY_MIN_SIZE
    else:
        expected_size = USERINFO1_EQUIP_ENTRY_MIN_SIZE
    if len(body) != expected_size:
        raise ValueError(
            "USERINFO1 equip entry size %s != %s"
            % (len(body), expected_size)
        )
    return bytes(body)


def encode_userinfo1_equipment_list(entries: Any) -> tuple[bytes, int, str]:
    """u8 count + sub_166AED0 rows。Returns (blob, count, note)."""
    try:
        from game.chain_loader import load_chain

        entries = load_chain("helper-knight_shield.py").filter_body_equipment_entries(
            entries
        )
    except Exception:
        pass
    rows: list[tuple[Any, ...]] = []
    for entry in entries or ():
        if isinstance(entry, dict):
            slot = int(entry.get("slot", 0) or 0) & 0xFF
            item_id = int(entry.get("item_id", 0) or 0) & 0xFFFFFFFF
            dura = int(entry.get("durability", 0) or 0) & 0xFFFF
            option_value = int(entry.get("option_value", 0) or 0) & 0xFF
            expire_time = int(entry.get("expire_time", 0) or 0) & 0xFFFFFFFF
            clear_avatar_id = int(entry.get("clear_avatar_id", 0) or 0) & 0xFFFFFFFF
            marker_16 = int(entry.get("marker_16", 0) or 0) & 0xFFFFFFFF
            extra_json = entry.get("extra_json")
        else:
            slot = int(getattr(entry, "slot", 0) or 0) & 0xFF
            item_id = int(getattr(entry, "item_id", 0) or 0) & 0xFFFFFFFF
            dura = int(getattr(entry, "durability", 0) or 0) & 0xFFFF
            option_value = int(getattr(entry, "option_value", 0) or 0) & 0xFF
            expire_time = int(getattr(entry, "expire_time", 0) or 0) & 0xFFFFFFFF
            clear_avatar_id = int(getattr(entry, "clear_avatar_id", 0) or 0) & 0xFFFFFFFF
            marker_16 = int(getattr(entry, "marker_16", 0) or 0) & 0xFFFFFFFF
            extra_json = getattr(entry, "extra_json", None)
        if item_id <= 0:
            continue
        is_avatar = False
        is_clone_avatar = False
        is_creature = False
        meta = None
        try:
            meta = resolve_item_metadata(item_id)
            is_avatar = bool(getattr(meta, "is_avatar", False))
            is_clone_avatar = bool(getattr(meta, "is_clone_avatar", False))
            is_creature = bool(getattr(meta, "is_creature", False))
        except Exception:
            meta = None
        if dura <= 0:
            try:
                if meta is None:
                    meta = resolve_item_metadata(item_id)
                dura = int(getattr(meta, "durability", 0) or 0) & 0xFFFF
            except Exception:
                dura = 0
        value = EQUIP_TOP_QUALITY_SEED
        if is_creature:
            # Type 26 uses the common Value field as its instance handle.
            value = marker_16
        elif is_avatar:
            if expire_time > 0:
                import time

                value = max(0, expire_time - int(time.time())) & 0xFFFFFFFF
            else:
                value = 0
        clear_id = clear_avatar_id if is_clone_avatar else 0
        # Stage3 slot9 with nonzero clear_avatar reads an additional linked raw
        # block. Aura and aura-skin rows always carry their real template ID.
        if slot in (AURA_ATTRIBUTE_EQUIP_SLOT, AURA_SKIN_EQUIP_SLOT):
            clear_id = 0
        wire_option_value = 0 if slot == AURA_SKIN_EQUIP_SLOT else option_value
        creature_expire_time = expire_time if is_creature else 0
        from game.character.amplify_option import parse_amplify_state
        from game.character.enchant_by_bead import parse_enchant_state

        amplify_type, amplify_value = parse_amplify_state(extra_json)
        enchant, enchant_upgrade = parse_enchant_state(extra_json)
        rows.append(
            (
                slot,
                item_id,
                dura,
                wire_option_value,
                value,
                is_avatar,
                clear_id,
                is_creature,
                creature_expire_time,
                amplify_type,
                amplify_value,
                enchant,
                enchant_upgrade,
                extra_json,
                meta,
            )
        )
    rows.sort(key=lambda r: (r[0], r[1]))
    if len(rows) > 0xFF:
        rows = rows[:0xFF]
    # Python owns all semantic normalization above.  The native kernel only
    # receives fixed-width fields plus the already encoded avatar blobs.
    from native_core_loader import pack_userinfo1_rows

    wire_rows: list[tuple[Any, ...]] = []
    avatar_n = 0
    clone_n = 0
    creature_n = 0
    for (
        slot,
        item_id,
        dura,
        option_value,
        value,
        is_avatar,
        clear_id,
        is_creature,
        creature_expire_time,
        amplify_type,
        amplify_value,
        enchant,
        enchant_upgrade,
        extra_json,
        meta,
    ) in rows:
        avatar_n += int(is_avatar)
        clone_n += int(clear_id > 0)
        creature_n += int(is_creature)
        avatar_extra = b""
        if is_avatar:
            avatar_extra = _avatar_socket.pack_avatar_extra_wire(
                item_id,
                extra_json,
                metadata=meta,
            )
        wire_rows.append(
            (
                slot,
                item_id,
                value,
                0 if is_avatar else option_value,
                option_value if is_avatar else dura,
                clear_id,
                enchant,
                enchant_upgrade,
                amplify_type,
                amplify_value,
                creature_expire_time,
                1 if is_avatar else 2 if is_creature else 0,
                avatar_extra,
            )
        )
    out = pack_userinfo1_rows(wire_rows)
    slots = ",".join("%s:%s" % (s, i) for s, i, *_rest in rows)
    note = "userinfo1_166aed0 n=%s avatar=%s creature=%s clone=%s slots=%s body_entry=%sB avatar_entry=%sB creature_entry=%sB" % (
        len(rows),
        avatar_n,
        creature_n,
        clone_n,
        slots or "-",
        USERINFO1_EQUIP_ENTRY_MIN_SIZE,
        USERINFO1_AVATAR_ENTRY_MIN_SIZE,
        USERINFO1_CREATURE_ENTRY_MIN_SIZE,
    )
    return bytes(out), len(rows), note


__all__ = [
    "USERINFO1_EQUIP_ENTRY_MIN_SIZE",
    "USERINFO1_AVATAR_ENTRY_MIN_SIZE",
    "USERINFO1_CREATURE_ENTRY_MIN_SIZE",
    "pack_userinfo1_equip_entry_166aed0",
    "encode_userinfo1_equipment_list",
]
