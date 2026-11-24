from __future__ import annotations

import struct
import time
from collections.abc import Sequence
from typing import Protocol

from game.character.adventure_group import project_adventure_group_effect_level
from game.character.customability_state import apply_customability_state_to_entry108
from game.character.item_metadata import (
    GUILD_MEDAL_FULL_WIRE_LIST_TYPE,
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
)
from game.chain_loader import load_chain
from game.quest.quest_common import is_client_hidden_quest_id
from pvf.adventure_group_index import resolve_expertise_progress


_avatar_socket = load_chain("helper-avatar_socket.py")
_guardian_gem_socket = load_chain("helper-guardian_gem_socket.py")


CLIENT_CERA_DISPLAY_CAP = 99_999_999
EQUIPPED_WEAPON_SLOT = 12
WEAPON_AVATAR_APPEARANCE_SLOT = 10
WEAPON_AVATAR_APPEARANCE_AUX = b"\x00" * 4
ITEM_ENTRY_FORGING_LEVEL_OFFSET = 76
ITEM_ENTRY_ENCHANT_INDEX_OFFSET = 14
ITEM_ENTRY_ENCHANT_UPGRADE_COUNT_OFFSET = 18
ITEM_ENTRY_SORT_LOCK_FLAG_OFFSET = 80
ITEM_ENTRY_EQUIPMENT_LOCK_ID_OFFSET = 81


class RosterCharacter(Protocol):
    character_id: int
    slot: int
    name_bytes: bytes
    job: int
    grow_type: int
    level: int


class EquippedItemLike(Protocol):
    slot: int
    item_id: int


def _appearance_item_id(item: EquippedItemLike) -> int:
    """Project a clone instance to its current ordinary-avatar appearance."""
    actual_id = int(item.item_id) & 0xFFFFFFFF
    clear_id = int(getattr(item, "clear_avatar_id", 0) or 0) & 0xFFFFFFFF
    return clear_id if clear_id > 0 else actual_id


def _pack_userinfo0_equipment_row(
    item: EquippedItemLike,
    appearance_id: int,
) -> bytes:
    """Pack one Stage3 compact appearance row.

    The Stage3 reader stores the length-prefixed blob as a per-slot object in
    the combat appearance snapshot.  A real list1 -> worn move supplies the
    weapon-avatar slot with a four-byte auxiliary object even when its value is
    zero.  Persisted minimum rows use custom_len=0, so materialize that object
    for slot 10 on USERINFO0 wire without migrating the database row.
    """
    slot = int(item.slot) & 0xFF
    actual_id = int(item.item_id) & 0xFFFFFFFF
    projected_id = int(appearance_id) & 0xFFFFFFFF
    raw = getattr(item, "raw_entry", None)
    if (
        projected_id == actual_id
        and isinstance(raw, (bytes, bytearray, memoryview))
    ):
        raw_row = bytes(raw)
        if len(raw_row) >= 19:
            custom_len = int.from_bytes(raw_row[5:9], "little")
            if 19 + custom_len <= len(raw_row):
                if slot == WEAPON_AVATAR_APPEARANCE_SLOT and custom_len == 0:
                    return (
                        raw_row[:5]
                        + len(WEAPON_AVATAR_APPEARANCE_AUX).to_bytes(4, "little")
                        + WEAPON_AVATAR_APPEARANCE_AUX
                        + raw_row[9:]
                    )
                return raw_row

    custom_blob = (
        WEAPON_AVATAR_APPEARANCE_AUX
        if slot == WEAPON_AVATAR_APPEARANCE_SLOT
        else b""
    )
    return (
        bytes([slot])
        + projected_id.to_bytes(4, "little")
        + len(custom_blob).to_bytes(4, "little")
        + custom_blob
        + bytes([int(getattr(item, "flag", 0) or 0) & 0xFF])
        + (
            int(getattr(item, "aux1", getattr(item, "expire_time", 0)) or 0)
            & 0xFFFFFFFF
        ).to_bytes(4, "little")
        + (int(getattr(item, "aux2", 0) or 0) & 0xFFFFFFFF).to_bytes(
            4, "little"
        )
        + bytes([int(getattr(item, "unknown", 0) or 0) & 0xFF])
    )


def _equipped_item_field(item: object, name: str, default: object = 0) -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def equipped_weapon_forging_level(
    equipment: Sequence[EquippedItemLike] | None,
) -> int:
    """Return forging for the actual primary weapon slot, otherwise zero."""
    from game.character.item_metadata import resolve_item_metadata
    from game.character.weapon_forging import parse_forging_level

    for item in equipment or ():
        slot = int(_equipped_item_field(item, "slot", -1) or -1) & 0xFF
        if slot != EQUIPPED_WEAPON_SLOT:
            continue
        item_id = int(_equipped_item_field(item, "item_id", 0) or 0) & 0xFFFFFFFF
        forging_level = parse_forging_level(
            _equipped_item_field(item, "extra_json", None)
        )
        if item_id <= 0 or forging_level <= 0:
            return 0
        metadata = resolve_item_metadata(item_id)
        if str(getattr(metadata, "equipment_type", "") or "") != "[weapon]":
            return 0
        return forging_level
    return 0


def equipped_weapon_forging_independent_attack(
    equipment: Sequence[EquippedItemLike] | None,
) -> int:
    """Resolve the PVF value for the forged weapon in primary slot 12."""
    from game.character.item_metadata import resolve_item_metadata
    from game.character.weapon_forging import (
        get_weapon_forging_config,
        parse_forging_level,
    )

    for item in equipment or ():
        slot = int(_equipped_item_field(item, "slot", -1) or -1) & 0xFF
        if slot != EQUIPPED_WEAPON_SLOT:
            continue
        item_id = int(_equipped_item_field(item, "item_id", 0) or 0) & 0xFFFFFFFF
        forging_level = parse_forging_level(
            _equipped_item_field(item, "extra_json", None)
        )
        if item_id <= 0 or forging_level <= 0:
            return 0
        metadata = resolve_item_metadata(item_id)
        if str(getattr(metadata, "equipment_type", "") or "") != "[weapon]":
            return 0
        return get_weapon_forging_config().independent_attack_for(
            minimum_level=int(getattr(metadata, "minimum_level", 0) or 0),
            rarity=int(getattr(metadata, "rarity", 0) or 0),
            forging_level=forging_level,
        )
    return 0


# Role-select USERINFO currently omits weapon slot 12. Partner live notes for
# current US EXE show Knight cards double-draw the starter weapon when this
# slot is present; coat/pants alone still preview armor and re-enter cleanly.
ROSTER_HIDDEN_EQUIP_SLOTS = frozenset({12})


def _dstr(text: str) -> bytes:
    encoded = text.encode("ascii")
    return struct.pack("<I", len(encoded)) + encoded


def build_game_channel_metadata(server_ip: str) -> bytes:
    body = bytearray((1,))
    body += _dstr("ch.1")
    body += struct.pack("<II", 0, 0)
    body += bytes((1, 1, 0))
    body += struct.pack("<I", int(time.time()))
    body += struct.pack("<I", 1)
    body += _dstr(server_ip)
    body += struct.pack("<II", 12311, 12312)
    body += b"\x00\x00"
    body += struct.pack("<II", 1086, 1036)
    return bytes(body)


def _build_userinfo_type2_entry(
    character: RosterCharacter, equipment: Sequence[EquippedItemLike] = (),
    *, rename_required: bool = False,
) -> bytes:
    """Encode one row in the exact read order of current EXE sub_18D2000."""
    name = bytes(character.name_bytes)
    job = int(character.job) & 0xFF
    body = bytearray()
    body += struct.pack("<H", int(character.slot))
    body += struct.pack("<I", len(name)) + name
    # packaging/state byte, job, packed grow/sub-grow, two level scalars,
    # reserved, then sub_18CC430 equipment count + 19B rows (含 custom_len)。
    # Runtime comparison confirms the first byte is the packaging/state
    # field and the second byte is the actual job id.  Writing job to both
    # positions leaves non-Slayer rows in the packaging state; writing zero
    # to the second position renders every row as Slayer.
    grow = int(getattr(character, "grow_type", 0)) & 0xFF
    level = max(1, int(getattr(character, "level", 1))) & 0xFF
    equipped = [
        (item, _appearance_item_id(item))
        for item in equipment
        if int(item.slot) not in ROSTER_HIDDEN_EQUIP_SLOTS
    ]
    if len(equipped) > 255:
        raise ValueError("USERINFO equipment count exceeds u8")
    body += bytes((int(bool(rename_required)), job, grow, level, level, 0, len(equipped)))
    # sub_18CC430: slot + item_id + custom_len0 + flag + aux1 + aux2 + unknown = 19B。
    # 本 type2 helper 默认 roster 不发装备（见 USDOF_USERINFO_ROSTER_SEND_EQUIPMENT）；
    # 进房穿戴真源是 build_userinfo_subtype0_min / helper-userinfo_equipment。
    for item, appearance_id in equipped:
        custom_blob = b""
        body += bytes([int(item.slot) & 0xFF])
        body += int(appearance_id).to_bytes(4, "little")
        body += len(custom_blob).to_bytes(4, "little")
        body += custom_blob
        body += bytes([int(getattr(item, "flags", getattr(item, "flag", 0)) or 0) & 0xFF])
        body += (int(getattr(item, "aux1", 0) or 0) & 0xFFFFFFFF).to_bytes(4, "little")
        body += (int(getattr(item, "aux2", 0) or 0) & 0xFFFFFFFF).to_bytes(4, "little")
        body += bytes([int(getattr(item, "unknown", 0) or 0) & 0xFF])
    # Post-equipment scalar sequence consumed by sub_18D2000 and its helpers:
    # u32, 4*u8, u8, u32, u8, u8, u32, render_flags, then 10*u8.
    body += struct.pack("<I", 0)
    body += b"\x00" * 4
    body += b"\x00"
    body += struct.pack("<I", 0)
    body += b"\x00\x00"
    body += struct.pack("<I", 0)
    # Current EXE sub_18CDC90 writes bit0 to temporary row+757 and
    # 0x18D234A -> sub_1C01C20 commits that value to the character object.
    # bit0=1 is the rename-required state used by non-Slayer cards.  Keep
    # bit1 for the normal profession/render path and bit3 to clear row+882.
    body += bytes((0x0a | int(bool(rename_required)),))
    body += b"\x00" * 10
    return bytes(body)


def build_userinfo_type2(
    characters: Sequence[RosterCharacter],
    slot_limit: int = 128,
    equipment_by_character: dict[int, Sequence[EquippedItemLike]] | None = None,
) -> bytes:
    roster = list(characters[:slot_limit])
    count = len(roster)
    # Current EXE sub_18D4140 calls sub_2B79000(A, A-B) and stores C as
    # controller target roster count.  Thus A=limit, B=limit-count, C=count.
    body = bytearray(b"\x02\x11")
    # Empty roster is 32/32/0: A=limit, B=limit-count, C=count.
    body += struct.pack("<HHH", slot_limit, slot_limit - count, count)
    body += struct.pack("<I", 0)
    body += struct.pack("<H", count)
    from game.character.character_rename import pending_ids
    rename_pending = pending_ids() if roster else frozenset()
    for character in roster:
        character_id = int(getattr(character, "character_id", -1))
        equipment = (equipment_by_character or {}).get(character_id, ())
        body += _build_userinfo_type2_entry(character, equipment,
            rename_required=character_id in rename_pending)
    # Fixed type2 trailer read after the row loop: u8 + u32 + u32.
    body += b"\x00" * 9
    return bytes(body)


def build_empty_userinfo_type2() -> bytes:
    # Empty roster A/B/C = 128/128/0 under the local account contract.
    return build_userinfo_type2(())


def build_load_extend_finish() -> bytes:
    return b"\x01\x02\x00\x00\x00\x10\x00"


def build_roster_extension_status() -> bytes:
    # Selector 0x0355 reads an exact 19-byte status block. The downstream
    # controller gate uses the first dword; zero is the neutral empty-roster
    # value and the remaining fields are not consumed by that path.
    return b"\x00" * 19


def build_state_init() -> bytes:
    # The common flag1 dispatcher consumes the leading success byte before
    # sub_1601B80 reads state. Keep the confirmed plaintext length at 152.
    return b"\x01\x04" + (b"\x00" * 150)


def build_charselect_window() -> bytes:
    body = bytearray(242)
    # body[0] is the dispatcher success byte. The 30 window pairs begin at +1.
    body[0] = 1
    struct.pack_into("<HI", body, 1, 12, 0)
    for index in range(1, 30):
        struct.pack_into("<HI", body, 1 + index * 6, 0xFFFF, 0)
    # Current client town list begins at key 1; historical UI trace placed n2 at body+0xDA.
    body[0xDA] = 1
    return bytes(body)


def _dstr_bytes(raw: bytes, max_len: int) -> bytes:
    data = bytes(raw)[:max_len]
    return struct.pack("<I", len(data)) + data


class InventoryItemLike(Protocol):
    slot_index: int
    item_template_id: int
    stack_count: int


# S4A12 ItemQuality.TopQualitySeed — 装备 000D/000E count 字段
EQUIP_TOP_QUALITY_SEED = 999999998


def _is_revive_coin_wallet_projection(item: InventoryItemLike) -> bool:
    """Return whether ``item`` is the fixed Main slot-1 wallet projection."""
    return (
        int(getattr(item, "slot_index", -1) or -1) == 1
        and int(getattr(item, "item_template_id", 0) or 0) == 1
    )


def wire_count_for_inventory_item(item: InventoryItemLike) -> int:
    """ITEMLIST/UPDATE 条目的 count/instance 字段。

    - 钱包 slot item_id=0：用 stack_count（金币总量）
    - 装备：TopQualitySeed（或行上已存的 instance_value）
    - 可堆叠：stack_count
    """
    item_id = int(getattr(item, "item_template_id", 0) or 0) & 0xFFFFFFFF
    stack = max(0, int(getattr(item, "stack_count", 0) or 0)) & 0xFFFFFFFF
    if item_id <= 0:
        return stack
    kind = str(getattr(item, "item_kind", "") or "")
    instance = getattr(item, "instance_value", None)
    if kind == "equipment":
        iv = int(instance or 0) & 0xFFFFFFFF
        return iv if iv >= EQUIP_TOP_QUALITY_SEED else EQUIP_TOP_QUALITY_SEED
    if instance is not None:
        iv = int(instance or 0) & 0xFFFFFFFF
        # 历史装备行可能 item_kind 不准，但 instance 已是 seed
        if iv >= EQUIP_TOP_QUALITY_SEED:
            return iv
    # The revive-coin wallet is a fixed client projection.  A zero balance is
    # meaningful and must remain zero in both a full list and an item update.
    if _is_revive_coin_wallet_projection(item):
        return stack
    return stack if stack > 0 else 1


# Stage3 IDA ``sub_10771F0``：赛丽亚 UI 指引 id(0..15) → 低区 flag 索引。
# ``sub_1074920`` 先查 high bank ``buf[101+id]``，再查 low bank ``buf[map[id]]``；
# START ``sub_2B66670`` → ``sub_1073D90`` **只写 low bank**（``buf[flag]=1``），
# 因此回灌必须写 map 后的 67..88，而不是 008F prefix=1 里的 guide id 7/9/13。
STAGE3_UI_GUIDE_ID_TO_LOW_FLAG: dict[int, int] = {
    0: 67,
    1: 68,
    2: 69,
    3: 75,
    4: 76,
    5: 77,
    6: 78,
    7: 79,
    8: 80,
    9: 81,
    10: 82,
    11: 83,
    12: 84,
    13: 85,
    14: 87,
    15: 88,
}


def expand_stage3_ui_tutorial_flags_for_start(
    flags: Sequence[int] | None,
) -> list[int]:
    """把 008F 存下的 flag 扩成 START 能跳过 UI 指引的 low-bank 索引。

    - prefix=0 完成标记（如 36/40）本身就是 low bank，原样保留。
    - prefix=1 的 guide id（0..15）必须映射到 67..88；同时保留 id 本身无害。
    """
    out: list[int] = []
    seen: set[int] = set()

    def _add(v: int) -> None:
        if v < 0 or v > 0x65 or v in seen:
            return
        seen.add(v)
        out.append(int(v))

    for item in list(flags or ()):
        try:
            fi = int(item) & 0xFFFFFFFF
        except Exception:
            continue
        if fi > 0x65:
            lo = fi & 0xFF
            hi = (fi >> 8) & 0xFF
            if lo in (0, 1) and hi <= 0x65 and (fi >> 16) == 0:
                fi = hi
            else:
                fi = fi & 0xFF
        _add(fi)
        mapped = STAGE3_UI_GUIDE_ID_TO_LOW_FLAG.get(int(fi))
        if mapped is not None:
            _add(int(mapped))
    return out


def normalize_start_tutorial_flags(
    flags: Sequence[int] | None,
    *,
    max_count: int = 0x65,
) -> list[int]:
    """Dedup / clamp tutorial flag indices for START ``sub_2B66670`` (u8 list).

    含 Stage3 UI 指引 id→low-bank 展开（见 ``expand_stage3_ui_tutorial_flags_for_start``）。
    """
    expanded = expand_stage3_ui_tutorial_flags_for_start(flags)
    out: list[int] = []
    seen: set[int] = set()
    for fi in expanded:
        if fi > 0x65 or fi in seen:
            continue
        seen.add(fi)
        out.append(int(fi))
        if len(out) >= int(max_count):
            break
    return out


def build_start_game_success(
    *,
    character_id: int = 0,
    map_key: int = 1,
    v115: int = 0,
    v127: int | None = None,
    n2_8: int | None = None,
    v123: int = 0,
    n2_6: int = 188,
    v121: int = 0,
    account_cera: int = 0,
    account_premiums: Sequence[tuple[int, int]] | None = None,
    # Tail after map_key (IDA sub_1603250 → sub_2B66670):
    #   u8 pad0, u8 flag_count, u8 flags[count], then fatigue/skipable fields.
    # Historical default ``n2_5=0x0100, v124=0x004E`` was a misaligned capture of
    # empty-pad + count=1 + flag=0x4E only — NOT a fixed u16 pair.
    fatigue_battery: int = 0,
    fatigue_grown_up: int = 0,
    trade_punish: int = 0,
    extra_field: int = 0,
    tutorial_skipable: int = 0,
    post_tutorial_u16: int = 0,
    active_quests: Sequence[tuple[int, int]] | None = None,
    quest_display_ids: Sequence[int] | None = None,
    tutorial_flags: Sequence[int] | None = None,
    body_length: int = 262,
) -> bytes:
    """S 1/0004 START success body for current EXE sub_1603250.

    Minimal enter-Seria fields (task4-saria3 live + reverse):
      n2_8 = character_id (local-key)
      premium_count @17, followed by count * {u8 type, i64 remaining_seconds}
      account_cera @18+9N = account Cera display balance; the persisted account
        balance remains authoritative, while this client field caps at 99999999
      n2_3/map_key @0xDA+9N = **当前城镇 id**（与 login_spawn / S0/0017.town 一致；
        银村=1 只是银村角色的自然值，不是全局常量。错绑其它城会进房即播城镇插画）
      30 quest pairs @22 (optional; non-0xFFFF → sub_1394650 注册进行中)
      4 quest_display_ids @202 (sub_1384900 → sub_1384700 右侧追踪栏，最多 4)
      @219+: tutorial flag list for UI 指引（商城/选图）— 必须回灌已看过的 index
      body_length=262 (226 min + pad；flag 多时自动加长)

    右侧/底部可交图标动效：display_ids 先挂 tracker，再靠后续 S1/0021
    (trigger→0) 走 sub_10434A0(qid,1)+sub_159F9F0。仅 023F 只点亮 F2 列表。

    Tutorial segment (IDA ``sub_2B66670`` after map_key):
      u8  pad0          (always 0 in S4 / live)
      u8  flag_count    (<= 0x65)
      u8  flags[n]      each index sets client ``buf_1[index]=1`` so guides skip
    """
    flags_u8 = normalize_start_tutorial_flags(tutorial_flags)
    premium_rows: list[tuple[int, int]] = []
    seen_premium_types: set[int] = set()
    for premium_type, remaining_seconds in list(account_premiums or ()):
        ptype = int(premium_type)
        remaining = int(remaining_seconds)
        if (
            ptype <= 0
            or ptype > 0xFF
            or remaining <= 0
            or remaining > 0x7FFFFFFFFFFFFFFF
            or ptype in seen_premium_types
        ):
            continue
        seen_premium_types.add(ptype)
        premium_rows.append((ptype, remaining))
        if len(premium_rows) >= 0xFF:
            break
    premium_shift = len(premium_rows) * 9
    # Fixed suffix begins after the variable premium rows. Keep the historical
    # body padding length in addition to moving every consumed field by 9N.
    min_body = 219 + premium_shift + 2 + len(flags_u8) + 8
    want_len = max(int(body_length) + premium_shift, min_body, 226 + premium_shift)
    if v127 is None:
        v127 = int(time.time()) & 0xFFFFFFFF
    if n2_8 is None:
        n2_8 = int(character_id) & 0xFFFF
    body = bytearray(int(want_len))
    body[0] = 1
    struct.pack_into("<I", body, 1, int(v115) & 0xFFFFFFFF)
    struct.pack_into("<I", body, 5, int(v127) & 0xFFFFFFFF)
    struct.pack_into("<H", body, 9, int(n2_8) & 0xFFFF)
    struct.pack_into("<H", body, 11, int(v123) & 0xFFFF)
    struct.pack_into("<H", body, 13, int(n2_6) & 0xFFFF)
    struct.pack_into("<H", body, 15, int(v121) & 0xFFFF)
    body[17] = len(premium_rows) & 0xFF
    for index, (premium_type, remaining) in enumerate(premium_rows):
        offset = 18 + index * 9
        struct.pack_into("<Bq", body, offset, premium_type, remaining)
    account_cera_offset = 18 + premium_shift
    account_cera_u32 = min(max(0, int(account_cera)), CLIENT_CERA_DISPLAY_CAP)
    struct.pack_into("<I", body, account_cera_offset, account_cera_u32)
    quest_offset = account_cera_offset + 4
    pairs = [(0xFFFF, 0)] * 30
    visible_active_quests = [
        (int(quest_id) & 0xFFFF, int(trigger) & 0xFFFFFFFF)
        for quest_id, trigger in (active_quests or ())
        if not is_client_hidden_quest_id(quest_id)
    ]
    for index, item in enumerate(visible_active_quests[:30]):
        pairs[index] = item
    for index, (quest_id, trigger) in enumerate(pairs):
        struct.pack_into("<HI", body, quest_offset + index * 6, quest_id, trigger)
    display_offset = quest_offset + 30 * 6
    display = [0, 0, 0, 0]
    visible_display_ids = [
        int(value) & 0xFFFFFFFF
        for value in (quest_display_ids or ())
        if not is_client_hidden_quest_id(value)
    ]
    for index, value in enumerate(visible_display_ids[:4]):
        display[index] = value
    for index, value in enumerate(display):
        struct.pack_into("<I", body, display_offset + index * 4, value)
    map_key_offset = display_offset + 4 * 4
    if map_key_offset != 0xDA + premium_shift:
        raise AssertionError("START body layout drift: n2_3 offset")
    body[map_key_offset] = int(map_key) & 0xFF
    # Tutorial flags (variable length) — NOT fixed u16 n2_5/v124.
    tutorial_offset = map_key_offset + 1
    body[tutorial_offset] = 0  # pad0 / v15
    body[tutorial_offset + 1] = len(flags_u8) & 0xFF
    for i, fi in enumerate(flags_u8):
        body[tutorial_offset + 2 + i] = int(fi) & 0xFF
    tail = tutorial_offset + 2 + len(flags_u8)
    struct.pack_into("<H", body, tail, int(fatigue_battery) & 0xFFFF)
    struct.pack_into("<H", body, tail + 2, int(fatigue_grown_up) & 0xFFFF)
    body[tail + 4] = int(trade_punish) & 0xFF
    struct.pack_into("<H", body, tail + 5, int(extra_field) & 0xFFFF)
    body[tail + 7] = int(tutorial_skipable) & 0xFF
    # optional post fields in remaining pad (post_tutorial_u16 + zeros)
    if len(body) >= tail + 10:
        struct.pack_into("<H", body, tail + 8, int(post_tutorial_u16) & 0xFFFF)
    return bytes(body)


def _pack_common_inventory_entry108(item: InventoryItemLike) -> bytes:
    """Stage3 common inventory 108B entry (list0/2/12 共用条目前缀)."""
    row = bytearray(108)
    #   u16 slot
    #   u32 item_template_id
    #   u32 count/instance   # 装备 = TopQualitySeed，不可用 stack=1 冒充
    #   u8  ext0
    #   u16 durability
    #   u8  seal_flag
    #   ... remaining reserved/padding to 108
    qty = wire_count_for_inventory_item(item)
    if (
        qty <= 0
        and int(getattr(item, "item_template_id", 0) or 0) > 0
        and not _is_revive_coin_wallet_projection(item)
    ):
        qty = 1
    dura = max(0, int(getattr(item, "durability", 0) or 0)) & 0xFFFF
    seal = max(0, int(getattr(item, "seal_flag", 0) or 0)) & 0xFF
    struct.pack_into(
        "<HII",
        row,
        0,
        int(item.slot_index) & 0xFFFF,
        int(item.item_template_id) & 0xFFFFFFFF,
        qty & 0xFFFFFFFF,
    )
    row[10] = max(0, int(getattr(item, "option_value", 0) or 0)) & 0xFF
    struct.pack_into("<H", row, 11, dura)
    row[13] = seal
    struct.pack_into(
        "<I",
        row,
        ITEM_ENTRY_ENCHANT_INDEX_OFFSET,
        max(0, int(getattr(item, "enchant_index", 0) or 0)) & 0xFFFFFFFF,
    )
    row[ITEM_ENTRY_ENCHANT_UPGRADE_COUNT_OFFSET] = (
        max(0, int(getattr(item, "enchant_upgrade_count", 0) or 0)) & 0xFF
    )
    marker = int(getattr(item, "marker_16", 0) or 0) & 0xFFFFFFFF
    expire = max(0, int(getattr(item, "expire_time", 0) or 0)) & 0xFFFFFFFF
    row[19] = max(0, int(getattr(item, "amplify_type", 0) or 0)) & 0xFF
    struct.pack_into(
        "<H", row, 20,
        max(0, int(getattr(item, "amplify_value", 0) or 0)) & 0xFFFF,
    )
    struct.pack_into("<I", row, 22, marker)
    struct.pack_into("<I", row, 43, expire)
    # Stage3 item_instance_from_entry108_19DF7A0 projects entry+76 into the
    # temporary attribute map at +120. sub_1B4F360 copies that map to the
    # runtime item at +4312, which sub_28A29C0 reads for DSTR 30249.
    row[ITEM_ENTRY_FORGING_LEVEL_OFFSET] = (
        max(0, int(getattr(item, "forging_level", 0) or 0)) & 0xFF
    )
    row[ITEM_ENTRY_SORT_LOCK_FLAG_OFFSET] = (
        1 if int(getattr(item, "sort_lock_flag", 0) or 0) else 0
    )
    row[ITEM_ENTRY_EQUIPMENT_LOCK_ID_OFFSET] = (
        max(0, int(getattr(item, "equipment_lock_id", 0) or 0)) & 0xFF
    )
    # 已解封 RO → Stage3 108B @60..71（IDA clear_entry；勿用 S4 0x2F）
    ro = getattr(item, "random_options", None)
    if ro:
        # extra_json RO is authoritative even for historical rows whose DB
        # seal_flag was left set by an older unseal path.
        row[13] = 0
        try:
            from game.chain_loader import load_chain as _load_ro

            _ro = _load_ro("helper-random_option.py")
            _ro.apply_random_options_to_entry108(row, list(ro)[:3])
        except Exception:
            entries = list(ro)[:3]
            if entries:
                n = min(3, len(entries))
                row[60] = n & 0xFF
                for i, e in enumerate(entries[:n]):
                    d = e or {}
                    row[61 + i] = int(d.get("type", 0) or 0) & 0xFF
                    row[64 + i] = int(d.get("value1", 0) or 0) & 0xFF
                    row[67 + i] = int(d.get("value2", 0) or 0) & 0xFF
                row[70] = 0x00
                row[71] = 0xFF
    _guardian_gem_socket.apply_guardian_gem_sockets_to_entry108(
        row,
        getattr(item, "extra_json", None),
    )
    apply_customability_state_to_entry108(
        row,
        getattr(item, "extra_json", None),
    )
    return bytes(row)


def _pack_avatar_inventory_entry108(
    item: InventoryItemLike,
    *,
    now_unix: int | None = None,
) -> bytes:
    """Stage3 Avatar/list1 108B entry.

    The fixed prefix uses avatar semantics: remaining lifetime at +6, generic
    attribute flags at +10, selected ability number at +11, template marker at
    +22, and the absolute instance expiration at +43.
    """
    if now_unix is None:
        now_unix = int(time.time())
    expire = max(0, int(getattr(item, "expire_time", 0) or 0)) & 0xFFFFFFFF
    remaining = max(0, expire - max(0, int(now_unix))) if expire else 0
    row = bytearray(108)
    struct.pack_into(
        "<HII",
        row,
        0,
        int(item.slot_index) & 0xFFFF,
        int(item.item_template_id) & 0xFFFFFFFF,
        remaining & 0xFFFFFFFF,
    )
    # Stage3 item_instance_from_entry108_19DF7A0 maps entry+10 to the generic
    # Attr field and entry+11 to instance state +40. The 01CC success handler
    # writes the selected avatar option to that same +40 state.
    row[10] = 0
    struct.pack_into(
        "<H",
        row,
        11,
        max(0, int(getattr(item, "option_value", 0) or 0)) & 0xFF,
    )
    row[13] = max(0, int(getattr(item, "seal_flag", 0) or 0)) & 0xFF
    struct.pack_into(
        "<I", row, 22, int(getattr(item, "marker_16", 0) or 0) & 0xFFFFFFFF
    )
    struct.pack_into("<I", row, 43, expire)
    return bytes(row)


def _pack_avatar_inventory_wire_entry(
    item: InventoryItemLike,
    *,
    now_unix: int | None = None,
) -> bytes:
    """Stage3 list1 entry: fixed state plus two length-prefixed avatar blobs."""
    fixed = _pack_avatar_inventory_entry108(item, now_unix=now_unix)
    # Both 000D and 000E readers unconditionally consume these fields for
    # list_type 1 (Stage3 0x01AAEB60 and 0x01AAEBB0).
    return fixed + _avatar_socket.pack_avatar_extra_wire(
        int(item.item_template_id),
        getattr(item, "extra_json", None),
    )


def pack_avatar_inventory_wire_entry(
    item: InventoryItemLike,
    *,
    now_unix: int | None = None,
) -> bytes:
    """Public Stage3 avatar wire entry shared by list1 and equipped list3.

    The frozen client conditionally reads the two length-prefixed blobs
    from both container and equipment update readers when the item is avatar
    equipment. Eligible empty-socket entries are 150 bytes on the wire.
    """
    return _pack_avatar_inventory_wire_entry(item, now_unix=now_unix)


def pack_pet_inventory_entry108(item: InventoryItemLike) -> bytes:
    """Stage3 list7 entry with pet-specific value semantics at offset 6.

    The frozen list7 reader consumes the same fixed 108-byte row shape as the
    common containers, but the third field is a creature handle for slots
    0..139, zero for artifact slots 140..188, and quantity for consumable
    slots 189..237.
    """
    row = bytearray(108)
    slot = int(item.slot_index) & 0xFFFF
    item_id = int(item.item_template_id) & 0xFFFFFFFF
    persisted = max(
        0, int(getattr(item, "pet_serial_or_handle", 0) or 0)
    ) & 0xFFFFFFFF
    if 189 <= slot <= 237:
        amount = persisted or (
            max(0, int(getattr(item, "stack_count", 0) or 0)) & 0xFFFFFFFF
        )
    elif 0 <= slot <= 139:
        amount = persisted
    else:
        amount = 0
    struct.pack_into("<HII", row, 0, slot, item_id, amount)
    row[10] = max(0, int(getattr(item, "option_value", 0) or 0)) & 0xFF
    struct.pack_into(
        "<H",
        row,
        11,
        max(0, int(getattr(item, "durability", 0) or 0)) & 0xFFFF,
    )
    row[13] = max(0, int(getattr(item, "seal_flag", 0) or 0)) & 0xFF
    struct.pack_into(
        "<I",
        row,
        ITEM_ENTRY_ENCHANT_INDEX_OFFSET,
        max(0, int(getattr(item, "enchant_index", 0) or 0)) & 0xFFFFFFFF,
    )
    row[ITEM_ENTRY_ENCHANT_UPGRADE_COUNT_OFFSET] = (
        max(0, int(getattr(item, "enchant_upgrade_count", 0) or 0)) & 0xFF
    )
    expire = max(0, int(getattr(item, "expire_time", 0) or 0)) & 0xFFFFFFFF
    struct.pack_into("<I", row, 43, expire)
    return bytes(row)


def build_item_list_mode0(
    items: Sequence[InventoryItemLike] | None = None,
    key: int = 0,
    *,
    list_type: int = 0,
    list_param16: int | None = None,
) -> bytes:
    """S 0/000D ITEMLIST mode0 from character_items list_type=0.

    Wire (S4A12 ItemListPacketBuilder.BuildCommonContainerBody):
      u8  list_type
      u16 list_param16   # container expansion/capacity param
      u16 count
      entries...

    ``key`` is kept as alias of list_param16 for old call sites.
    """
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("ITEM_LIST mode0 count exceeds u16")
    param = int(key if list_param16 is None else list_param16) & 0xFFFF
    body = bytearray(struct.pack("<BHH", int(list_type) & 0xFF, param, len(rows)))
    for item in rows:
        body.extend(_pack_common_inventory_entry108(item))
    return bytes(body)


def build_avatar_item_list_mode0(
    items: Sequence[InventoryItemLike] | None = None,
    *,
    list_type: int = 1,
    list_param16: int = 0,
    now_unix: int | None = None,
) -> bytes:
    """S0/000D full list1 or list33 container with Stage3 avatar entries.

    Stage3 consumes a capacity ``u16`` only for list1.  The dedicated Avatar
    Closet list33 reader proceeds directly from the discriminator to count.
    Both containers consume the same fixed entry and two avatar blobs.
    """
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("Avatar ITEM_LIST count exceeds u16")
    target_list = int(list_type) & 0xFF
    if target_list == 1:
        body = bytearray(
            struct.pack("<BHH", target_list, int(list_param16) & 0xFFFF, len(rows))
        )
    elif target_list == 33:
        body = bytearray(struct.pack("<BH", target_list, len(rows)))
    else:
        raise ValueError("Avatar ITEM_LIST supports only list1/list33")
    for item in rows:
        body.extend(pack_avatar_inventory_wire_entry(item, now_unix=now_unix))
    return bytes(body)


def build_pet_item_list_mode0(
    items: Sequence[InventoryItemLike] | None = None,
) -> bytes:
    """S0/000D full Pet container: u8 list7 + u16 count + 108B rows."""
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("Pet ITEM_LIST count exceeds u16")
    body = bytearray(struct.pack("<BH", 7, len(rows)))
    for item in rows:
        body.extend(pack_pet_inventory_entry108(item))
    return bytes(body)


def build_guild_medal_item_list_mode0(
    items: Sequence[InventoryItemLike] | None = None,
    *,
    selected_character_slot: int = 0,
) -> bytes:
    """S0/000D full guild inventory using Stage3 discriminator 39.

    Persisted moves and S0/000E updates use internal list_type 36. The full
    reader instead consumes ``u8 39, u8 character_slot, u16 count`` before the
    ordinary fixed 108-byte entries.
    """
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("Guild medal ITEM_LIST count exceeds u16")
    body = bytearray(
        struct.pack(
            "<BBH",
            GUILD_MEDAL_FULL_WIRE_LIST_TYPE,
            int(selected_character_slot) & 0xFF,
            len(rows),
        )
    )
    for item in rows:
        body.extend(_pack_common_inventory_entry108(item))
    return bytes(body)


def build_guild_medal_full_list36(
    items: Sequence[InventoryItemLike] | None = None,
) -> bytes:
    """S0/000D ordinary list36 full refresh for the current character.

    Unlike discriminator 39, this wire shape resolves the global list36
    container used by the open medal UI: ``u8 list_type, u16 count, 108B rows``.
    The S0/000D reader inserts these rows without the acquisition notification
    used by S0/000E runtime deltas.
    """
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("Guild medal login ITEM_LIST count exceeds u16")
    body = bytearray(
        struct.pack("<BH", GUILD_MEDAL_INVENTORY_LIST_TYPE, len(rows))
    )
    for item in rows:
        body.extend(_pack_common_inventory_entry108(item))
    return bytes(body)


def build_guild_medal_login_full_list36(
    items: Sequence[InventoryItemLike] | None = None,
) -> bytes:
    """Compatibility wrapper for the selected-character login flow."""
    return build_guild_medal_full_list36(items)


def build_item_list_update_000e(
    items: Sequence[InventoryItemLike] | None = None,
    *,
    list_type: int = 0,
    avatar: bool | None = None,
    now_unix: int | None = None,
) -> bytes:
    """S0/000E complete Stage3 entries for common persisted containers."""
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("UPDATE_ITEM_LIST count exceeds u16")
    is_avatar = int(list_type) == 1 if avatar is None else bool(avatar)
    body = bytearray(struct.pack("<BH", int(list_type) & 0xFF, len(rows)))
    for item in rows:
        if int(list_type) == 7:
            body.extend(pack_pet_inventory_entry108(item))
        elif is_avatar:
            body.extend(pack_avatar_inventory_wire_entry(item, now_unix=now_unix))
        else:
            body.extend(_pack_common_inventory_entry108(item))
    return bytes(body)


def build_account_cargo_item_list(
    items: Sequence[InventoryItemLike] | None = None,
    *,
    selection_key: int = 80,
    cargo_gold: int = 0,
) -> bytes:
    """S 0/000D ITEMLIST list_type=12 AccountCargo.

    Wire (S4A12 ItemListPacketBuilder.BuildAccountCargoBody / Stage3 sub_1666D80 n12==12):
      u8  list_type = 12
      u16 selection_key   # 开通槽位数（0=未开通）
      u32 cargo_gold      # 账号仓金币 value32
      u16 count
      entries... (108B each, Stage3)

    selection_key=0 时客户端走未开通；本地默认满档 80（PVF accountcargo.etc 顶档）。
    本地屏蔽 PVF min_level=60：任意等级都可发 key=80。
    """
    rows = list(items or ())
    if len(rows) > 0xFFFF:
        raise ValueError("ACCOUNT_CARGO item count exceeds u16")
    key = max(0, int(selection_key)) & 0xFFFF
    gold = int(cargo_gold) & 0xFFFFFFFF
    body = bytearray(struct.pack("<BHIH", 12, key, gold, len(rows)))
    for item in rows:
        body.extend(_pack_common_inventory_entry108(item))
    return bytes(body)


def build_creature_item_list(
    states: Sequence[object] | None = None,
) -> bytes:
    """Build the exact Stage3 S0/0069 creature-state list.

    Frozen-client reader ``sub_1650B40`` consumes::

      u8 count
      count * (
        u32 creature_key
        u8 field04
        u8 mode_flag
        u32 progress_value
        if mode_flag == 1: u8 mode1_field0a, u8 mode1_field0b
        u8 field_after_value
        DSTR creature_text
        u8 tail_flag
      )
    """
    rows = list(states or ())
    if len(rows) > 0xFF:
        raise ValueError("CREATURE_ITEM_LIST count exceeds u8")

    def _field(row: object, key: str, default: object = 0) -> object:
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    body = bytearray([len(rows) & 0xFF])
    for row in rows:
        key = int(_field(row, "creature_key", 0) or 0) & 0xFFFFFFFF
        if key == 0:
            raise ValueError("CREATURE_ITEM_LIST creature_key must be positive")
        mode = int(_field(row, "mode_flag", 0) or 0) & 0xFF
        raw_text = _field(row, "creature_text", b"")
        if isinstance(raw_text, str):
            text = raw_text.encode("utf-8")
        else:
            text = bytes(raw_text or b"")
        text = text[:30]

        body += struct.pack("<I", key)
        body.append(int(_field(row, "field04", 0) or 0) & 0xFF)
        body.append(mode)
        body += struct.pack(
            "<I", int(_field(row, "progress_value", 0) or 0) & 0xFFFFFFFF
        )
        if mode == 1:
            body.append(int(_field(row, "mode1_field0a", 0) or 0) & 0xFF)
            body.append(int(_field(row, "mode1_field0b", 0) or 0) & 0xFF)
        body.append(int(_field(row, "field_after_value", 0) or 0) & 0xFF)
        body += struct.pack("<I", len(text)) + text
        body.append(int(_field(row, "tail_flag", 0) or 0) & 0xFF)
    return bytes(body)


def build_creature_item_list_empty() -> bytes:
    """Compatibility wrapper for an empty Stage3 S0/0069 list."""
    return build_creature_item_list(())


def build_userinfo_subtype0_min(
    character: RosterCharacter,
    equipment: Sequence[EquippedItemLike] | None = None,
    guild_name: str = "",
    guild_id: int = 0,
) -> bytes:
    """S 0/0002 USERINFO subtype0 minimal body for current EXE.

    Live success path: empty-equip body accepted; client may emit C1/0023
    (town position) even without PIV 0002.
    """
    name = bytes(character.name_bytes or b"")[:31]
    raw33 = bytes(33)
    job = int(getattr(character, "job", 0)) & 0xFF
    grow = int(getattr(character, "grow_type", 0)) & 0xFF
    level = max(1, int(getattr(character, "level", 1))) & 0xFF
    forging_level = equipped_weapon_forging_level(equipment)
    guild_name_bytes = str(guild_name or "").encode("utf-8", errors="replace")
    guild_id_value = int(guild_id or 0)
    if guild_id_value < 0 or guild_id_value > 0xFFFFFFFF:
        raise ValueError("USERINFO0 guild_id exceeds u32")
    raw_visibility_bits = getattr(character, "visibility_bits", None)
    visibility_bits = (
        3 if raw_visibility_bits is None else int(raw_visibility_bits)
    ) & 0xFF

    body = bytearray()
    body += b"\x00"
    body += struct.pack("<H", 1)
    body += b"\x00"
    body += raw33
    body += struct.pack("<H", int(character.character_id) & 0xFFFF)
    body += _dstr_bytes(name, 31)
    body += bytes((job, grow))
    body += bytes((level, 0, 0))
    body += b"\x00"

    # 0x018CC430 equipment list (same parser as live type2 roster rows):
    #   u8 count
    #   repeat: u8 slot, u32 item_id, u32 custom_len, custom bytes,
    #           u8 flag, u32 aux1, u32 aux2, u8 unknown
    # Slot 10 must carry the four-byte auxiliary object used by the initial
    # combat appearance snapshot. Other minimum rows remain custom_len=0.
    eq = [(item, _appearance_item_id(item)) for item in (equipment or ())]
    if len(eq) > 0xFF:
        raise ValueError("USERINFO0 equip count exceeds u8")
    body += bytes((len(eq),))
    for item, appearance_id in eq:
        body += _pack_userinfo0_equipment_row(item, appearance_id)

    creature_item_id = 0
    creature_name = b""
    creature_alive_state = 0
    for item, _appearance_id in eq:
        if int(getattr(item, "slot", -1)) != 26:
            continue
        creature_item_id = int(getattr(item, "item_id", 0) or 0) & 0xFFFFFFFF
        if creature_item_id <= 0:
            break
        raw_creature_name = getattr(item, "creature_text", b"")
        if isinstance(raw_creature_name, str):
            creature_name = raw_creature_name.encode("utf-8", errors="replace")
        else:
            creature_name = bytes(raw_creature_name or b"")
        creature_name = creature_name[:31]
        creature_alive_state = max(
            0,
            min(1, int(getattr(item, "creature_alive_state", 1) or 0)),
        )
        break

    # Stage3 sub_18D2000 reads clone_title then the equipped primary weapon's
    # forging level. The client applies the PVF independent-attack formula.
    body += struct.pack("<I", 0)
    body += bytes((forging_level, 0, 0, 0))
    body += b"\x00"
    # Stage3 sub_18CD630 stores this u32 in the character snapshot at +0x2C8.
    # Guild entry dispatch tests that identity key before choosing menu 0x3C.
    body += struct.pack("<I", guild_id_value)
    body += b"\x00"
    # Stage3 sub_18CC640: equipped creature template, custom name, alive state.
    body += struct.pack("<I", creature_item_id)
    body += _dstr_bytes(creature_name, 31)
    body += bytes((creature_alive_state,))
    body += b"\x00"
    body += struct.pack("<II", 0, 0)
    # Stage3 sub_18CD6E0 binds this DSTR to the +0x2C8 guild identity key.
    body += _dstr_bytes(guild_name_bytes, 64)
    body += struct.pack("<I", 0)
    body += struct.pack("<I", 0)
    body += b"\x00"
    body += bytes((0, 0))
    body += b"\x00"
    body += struct.pack("<I", 0)
    body += b"\x00"
    body += struct.pack("<I", 0)
    body += struct.pack("<H", 0)
    body += struct.pack("<I", 0)
    body += b"\x00" * 8
    # sub_18CDC90 reads this u8 immediately after sub_18CCCB0's u32 + 8B
    # runtime snapshot and applies it to the character model/game options.
    body += bytes((visibility_bits,))
    body += struct.pack("<I", 0)
    # 018D1501/1517 reads this actual u8 and writes profile+2F8.
    # It is recovered vitality (100 = healthy), not a weakness penalty.
    # 01866C90 uses it directly for both town party gauges; zero hides their
    # fill. In-dungeon 01866250 instead reads real actor HP/MP via its vtable.
    weakness_offset = len(body)
    body += b"\x64"
    body += struct.pack("<H", 0)
    body += b"\x00"
    body += struct.pack("<HB", 0, 0)
    body += struct.pack("<H", 0)
    body += b"\x00"
    body += bytes((0, 0))
    body += struct.pack("<I", 0)
    from game.character.native_profile_projection import _stamp_userinfo0
    return _stamp_userinfo0(bytes(body), character_id=int(character.character_id) & 0xFFFF,
                           weakness_offset=weakness_offset)


def build_game_option_defaults() -> bytes:
    """S 0/00AD GAME_OPTION; keep opt[0x4A]=1 so f4213 is not cleared."""
    options = [0] * 79
    defaults = {
        0x00: 1,
        0x01: 1,
        0x02: 1,
        0x03: 3,
        0x04: 70,
        0x05: 25,
        0x0B: 1,
        0x0C: 1,
        0x0D: 1,
        0x0E: 1,
        0x11: 1,
        0x12: 3,
        0x13: 0,
        0x16: 1000,
        0x18: 1,
        0x19: 70,
        0x1A: 1,
        0x20: 1,
        0x21: 1,
        0x22: 1,
        0x24: 70,
        0x2A: 1,
        0x32: 1,
        0x36: 1,
        0x37: 1,
        0x38: 1,
        0x3A: 1,
        0x3B: 70,
        0x41: 1,
        0x42: 1,
        0x43: 1,
        0x44: 100,
        0x46: 1,
        0x47: 100,
        0x48: 1,
        0x49: 1,
        0x4A: 1,
    }
    for index, value in defaults.items():
        options[index] = int(value) & 0xFFFF
    primary = b"".join(struct.pack("<H", value) for value in options)
    if len(primary) != 158:
        raise ValueError(f"game option primary must be 158B, got {len(primary)}")
    return struct.pack("<I", 158) + primary + struct.pack("<II", 0, 0)


def _write_userinfo1_skill_page(
    body: bytearray,
    skills: Sequence[InitialSkillLike] | None,
) -> None:
    """Stage3 sub_18CCF80 / S4A12 WriteSkillPage：u8 count + (u16 skill_id, u8 level)*N。"""
    entries: list[tuple[int, int]] = []
    for skill in skills or ():
        level = int(getattr(skill, "level", 0) or 0)
        skill_id = int(getattr(skill, "skill_id", 0) or 0)
        if level <= 0 or skill_id <= 0:
            continue
        entries.append((skill_id & 0xFFFF, max(1, min(255, level))))
    if len(entries) > 255:
        entries = entries[:255]
    body += bytes((len(entries),))
    for skill_id, level in entries:
        body += struct.pack("<HB", skill_id, level)


def build_userinfo_subtype1_min(
    character: RosterCharacter,
    *,
    exp: int = 0,
    stat_level: int = 100,
    skills: Sequence[InitialSkillLike] | None = None,
    entry_flag: int = 0,
    equipment: Sequence[EquippedItemLike] | None = None,
    finished_quest_ids: Sequence[int] | None = None,
    adventure_group_level: int = 0,
    guild_membership_active: bool = False,
) -> bytes:
    """S 0/0002 USERINFO subtype1 for current EXE sub_18D3A70.

    Stage3 外层（018D4140 case1）+ 内层（18D3A70）读序（以 IDA 为准，非 S4A12）：
      type=1, u16 count=1, u8 entry_flag
      u32 shared_state                    # 140E2B0，当前保持 0
      u32 expertise_level                 # E29C10
      u64 expertise_current_level_exp     # E29C60
      u8 + u8 shared_flags                # 当前保持 0
      u16 key                             # 1E7F0F0(key) 必须先有 0131 对象
      u32 exp                            # 18CE440
      u32 len + len bytes                # 18CC850 战斗属性 blob（含 max HP/MP）
      u8 ex_equip
      u8 equip_count + equip rows        # 166AED0 —— Inventory 槽对象创建
      u32 clone_title                    # 166AED0 尾 u32（无独立 name_tag 段）
      u8 skill_tree_index
      skill_page0 + skill_page1          # 18CCF80：u8 count+(u16 id,u8 lv)*
      u8 creature_level                    # slot26 handle 对应的 0069 等级
      u8 dim_count + (u32,u8,u8)* + 2*u8 # 18CC900（Stage3 仅 2 flag）
      u8 time_fragment_count             # 1CB5380 -> actor+0x63F4
      u32 special_reward_count + u32*    # 18CCA50：个性 / 时空碎片特殊任务 ids
      u32 adventure_group_effect_level      # actor+0x6404, type 0x65 effect
      u8 18D04C0_count
      u8 final

    战斗 blob：length=83；82B 四维/属性/移速等 + 1B stat_level（面板锚，默认 100）。
    基础数值来自离线 .chr JSON。个性任务的 [special reward status] 由
    18CCA50 的任务 ID 列表重建；时空碎片任务 ID 同样保留在该列表用于
    客户端 UI，但实际碎片属性与 [required timepiece] 技能门槛只读取前一
    个 u8 time_fragment_count。冒险团账号等级先投影为其后的专有 u32 效果
    等级：Lv.0/1 写 0，使原生 PVF 查表在状态刷新前返回；Lv.2 起写真实等级，
    由客户端创建类型 0x65 的效果对象并刷新原生图标与角色属性。公会的两层权益中，一层由
    USERINFO1 权威四维投影，另一层由独立状态包创建；两层共同组成提示中的
    总值。skills 写入 type1 技能页（与 0013 互补）。

    equipment：进房身体穿戴；走 sub_166AED0 创建槽（非 type0 的 19B 紧凑表）。
    对照 S4A12/90qiyuan subtype1 都把装备写在这里；type0 只灌外观。
    """
    from game.character.character_stats import build_additional_info_for_character
    from game.chain_loader import load_chain
    from game.quest.personality_rewards import finished_personality_reward_ids
    from game.quest.time_fragment_rewards import finished_time_fragment_reward_ids

    key = int(character.character_id) & 0xFFFF
    job = int(getattr(character, "job", 0)) & 0xFF
    grow = int(getattr(character, "grow_type", 0)) & 0xFF
    # DB 分列觉醒；没有该字段时走 grow_type 打包解码
    raw_sub = getattr(character, "sub_grow_type", None)
    sub_grow = None if raw_sub is None else (int(raw_sub) & 0xFF)
    level = max(1, int(getattr(character, "level", 1)))
    expertise_level, expertise_current_level_exp = resolve_expertise_progress(
        int(getattr(character, "expertise_exp", 0) or 0)
    )
    time_fragment_count = 0
    if finished_quest_ids:
        personality_reward_ids = finished_personality_reward_ids(finished_quest_ids)
        time_fragment_reward_ids = finished_time_fragment_reward_ids(
            finished_quest_ids,
            job_id=job,
        )
        time_fragment_count = len(time_fragment_reward_ids)
        special_reward_quest_ids = tuple(
            sorted(set(personality_reward_ids).union(time_fragment_reward_ids))
        )
    else:
        special_reward_quest_ids = ()
    _stats, combat_blob = build_additional_info_for_character(
        job,
        level,
        grow,
        sub_grow_type=sub_grow,
        stat_level=int(stat_level) & 0xFF,
        guild_membership_active=guild_membership_active,
    )
    if len(combat_blob) != 83:
        raise ValueError(f"USERINFO1 combat blob must be 83B, got {len(combat_blob)}")

    equip_helper = load_chain("helper-userinfo1_equipment.py")
    equip_blob, _equip_n, _equip_note = equip_helper.encode_userinfo1_equipment_list(
        equipment or ()
    )

    body = bytearray()
    body += b"\x01"
    body += struct.pack("<H", 1)
    body += bytes((int(entry_flag) & 0xFF,))
    body += struct.pack(
        "<IIQBB",
        0,
        int(expertise_level) & 0xFFFFFFFF,
        int(expertise_current_level_exp) & 0xFFFFFFFFFFFFFFFF,
        0,
        0,
    )
    body += struct.pack("<H", key)
    body += struct.pack("<I", int(exp) & 0xFFFFFFFF)  # 18CE440 exp
    body += struct.pack("<I", len(combat_blob))  # 18CC850 length
    body += combat_blob  # 82B combat + 1B stat_level
    ex_equip = int(getattr(character, "ex_equip_slot_stat", 0) or 0) & 0xFF
    body += bytes((ex_equip,))  # Stage3 actor equipment state +0x19C
    body += equip_blob  # 166AED0: u8 count + rows（槽对象创建）
    body += struct.pack("<I", 0)  # 166AED0 trailing clone_title
    body += b"\x00"  # skill_tree_index
    # 两页技能树：主页填初始技能，副页空（Stage3 固定读 2 页）
    _write_userinfo1_skill_page(body, skills)
    _write_userinfo1_skill_page(body, None)
    creature_level = max(
        0,
        min(255, int(getattr(character, "equipped_creature_level", 0) or 0)),
    )
    body += bytes((creature_level,))
    body += b"\x00"  # dim_count
    body += b"\x00\x00"  # 18CC900 两枚 dim flag
    if not 0 <= time_fragment_count <= 0xFF:
        raise ValueError(
            "USERINFO1 time_fragment_count must fit u8: %r" % time_fragment_count
        )
    # Stage3 apply_time_fragment_effects hard-gates this value to job 9/10.
    body += bytes((time_fragment_count,))
    body += struct.pack("<I", len(special_reward_quest_ids))  # 18CCA50 reward quest count
    for quest_id in special_reward_quest_ids:
        body += struct.pack("<I", int(quest_id) & 0xFFFFFFFF)
    adventure_effect_level = project_adventure_group_effect_level(
        adventure_group_level
    )
    body += struct.pack("<I", adventure_effect_level)
    body += b"\x00"  # 18D04C0 count
    body += b"\x00"  # final u8
    from game.character.native_profile_projection import _stamp_userinfo1
    return _stamp_userinfo1(bytes(body), character_id=key)


def _pb_varint(value: int) -> bytes:
    n = int(value) & 0xFFFFFFFF
    out = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _pb_key(field: int, wire_type: int) -> bytes:
    return _pb_varint((int(field) << 3) | int(wire_type))


def _pb_tag_varint(field: int, value: int) -> bytes:
    return _pb_key(field, 0) + _pb_varint(value)


def _pb_tag_message(field: int, payload: bytes) -> bytes:
    data = bytes(payload)
    return _pb_key(field, 2) + _pb_varint(len(data)) + data


def _skill_command_bytes(skill: InitialSkillLike) -> bytes:
    """从 InitialSkill / 兼容对象取释放命令字节（extra_values）。"""
    raw = getattr(skill, "command_bytes", None)
    if raw is None:
        raw = getattr(skill, "extra_values", None)
    if raw is None:
        return b""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    try:
        return bytes(raw)
    except Exception:
        return b""


def _encode_skilltree_entry(skill: InitialSkillLike) -> bytes:
    """DNFPB.noti.PB_ENUM_NOTIPACKET_SKILLINFO.SkillTree (sub_4E5220).

    field1 = slot, field2 = skill_id, field3 = level.
    field4 = repeated int32 extras：技能释放命令字节逐字节（0..255）。
    无命令时省略 field4（与历史空包兼容）。
    """
    body = (
        _pb_tag_varint(1, int(skill.slot))
        + _pb_tag_varint(2, int(skill.skill_id))
        + _pb_tag_varint(3, int(skill.level))
    )
    cmd = _skill_command_bytes(skill)
    if cmd:
        # 每个命令字节写成一个 field4 varint（int32 语义，值域 0..255）
        for b in cmd:
            body += _pb_tag_varint(4, int(b) & 0xFF)
    return body


def _encode_skillinfo_page(
    skills: Sequence[InitialSkillLike],
    remaining_sp: int = 0,
    remaining_tp: int = 0,
) -> bytes:
    """DNFPB.noti.PB_ENUM_NOTIPACKET_SKILLINFO.SkillInfo (sub_4EC230).

    Stage3 ``sub_1663D00`` applies field1 through ``sub_1E72E70`` as the
    page's absolute remaining SP and field2 through ``sub_1E73090`` as the
    page's absolute remaining TP.  field3 is repeated SkillTree.
    """
    body = bytearray()
    body += _pb_tag_varint(1, int(remaining_sp))
    body += _pb_tag_varint(2, int(remaining_tp))
    for skill in skills:
        body += _pb_tag_message(3, _encode_skilltree_entry(skill))
    return bytes(body)


def build_skillinfo_from_pvf(
    skills: Sequence[InitialSkillLike],
    *,
    page0_sp: int = 0,
    page1_sp: int = 0,
    page0_tp: int = 0,
    page1_tp: int = 0,
    page1_skills: Sequence[InitialSkillLike] | None = None,
    mirror_page0_to_page1: bool = True,
) -> bytes:
    """S 0/0013 SKILLINFO body from current-PVF / DB skills.

    Wire:
      u32 protobuf_length + DNFPB.noti.PB_ENUM_NOTIPACKET_SKILLINFO
    Outer message (sub_4ED860):
      field1 = notice enum (19, optional; hard-gated by sub_4F75B0)
      field2 = repeated SkillInfo pages

    SkillInfo page scalars (current Stage3 ``sub_1663D00``)：
      field1 = 该页剩余 SP（绝对余额，非 delta；``sub_1E72E70``）
      field2 = 该页剩余 TP（绝对余额，非 delta；``sub_1E73090``）
      field3 = repeated SkillTree(slot, skill_id, level[, field4 command bytes])

    SkillTree field4：C1/014B 持久化的释放命令（extra_values 逐字节 int32）。

    page0 用 ``skills``；page1 默认镜像 page0（历史 starter 双页形），
    或显式 ``page1_skills`` / ``mirror_page0_to_page1=False`` 空页。
    """
    p0_sp = int(page0_sp) & 0xFFFF
    p1_sp = int(page1_sp) & 0xFFFF
    p0_tp = int(page0_tp) & 0xFFFF
    p1_tp = int(page1_tp) & 0xFFFF
    page0 = _encode_skillinfo_page(
        skills,
        remaining_sp=p0_sp,
        remaining_tp=p0_tp,
    )
    if page1_skills is not None:
        page1 = _encode_skillinfo_page(
            page1_skills,
            remaining_sp=p1_sp,
            remaining_tp=p1_tp,
        )
    elif mirror_page0_to_page1:
        page1 = _encode_skillinfo_page(
            skills,
            remaining_sp=p1_sp,
            remaining_tp=p1_tp,
        )
    else:
        page1 = _encode_skillinfo_page(
            (),
            remaining_sp=p1_sp,
            remaining_tp=p1_tp,
        )
    pb = bytearray()
    pb += _pb_tag_varint(1, 19)
    # Two SkillInfo pages. Client apply loop is over outer field2 repeated count.
    pb += _pb_tag_message(2, page0)
    pb += _pb_tag_message(2, page1)
    blob = bytes(pb)
    return struct.pack("<I", len(blob)) + blob



def build_skillinfo_empty() -> bytes:
    """S 0/0013 empty protobuf blob (u32 len=0). Fallback probe only."""
    return struct.pack("<I", 0)

def build_acceptable_quest_list(level: int = 1, quest_ids: Sequence[int] = ()) -> bytes:
    """S 0/0015 ACCEPTABLE_QUEST_LIST for current EXE sub_1650580.

    Wire: u32 protobuf_length + message parsed by sub_4E58C0.
      field1 = notice enum (21, gated by sub_4F75B0)
      field2 = character level (fed to sub_1374040)
      field3 = quest count
      field4 = repeated quest ids (packed or unpacked int32)

    Fresh level-1 characters currently have an empty PVF acceptable list.
    """
    ids = [
        int(q) & 0xFFFFFFFF
        for q in quest_ids
        if not is_client_hidden_quest_id(q)
    ]
    pb = bytearray()
    pb += _pb_tag_varint(1, 21)
    pb += _pb_tag_varint(2, max(1, int(level)))
    pb += _pb_tag_varint(3, len(ids))
    for quest_id in ids:
        pb += _pb_tag_varint(4, quest_id)
    blob = bytes(pb)
    return struct.pack("<I", len(blob)) + blob

def build_clear_quest_list_empty() -> bytes:
    """S 0/0164 CLEAR_QUEST_LIST for current EXE sub_164EA10.

    Reader sub_2BF4BC0: u32 byte_length + byte_length raw bytes into a
    30000-byte quest-state buffer that is zeroed first. Fresh characters
    have no cleared/hidden quest state, so the live-safe body is length 0.
    """
    return struct.pack("<I", 0)


def build_daily_challenge_status_0286() -> bytes:
    """S0/0286 one-byte Daily Challenge controller state.

    Extension registry maps selector 0x0286 to sub_E286F0, which reads one
    u8 state into the controller. The challenge entry table is a separate
    S0/0287 body consumed by sub_165B440.
    """
    return b"\x00"


def build_daily_challenge_default() -> bytes:
    """Compatibility alias for the accurately named S0/0286 state body."""
    return build_daily_challenge_status_0286()


def build_selector_007c_empty() -> bytes:
    """S 0/007C ENTER_GAMEWORLD_COMPLETE.

    Handler sub_167AFD0 has no body readers; capture bodyLen=0.
    Late empty 007C after AREA_USERS re-evaluates pending-area path.
    """
    return b""


def build_primary_user_object(
    character_id: int,
    field_u32_a: int = 0,
    field_u32_b: int = 0,
    field_u16_c: int = 0,
    field_u32_d: int = 0,
    field_u8_e: int = 0,
    field_u32_f: int = 0,
    field_u8_g: int = 0,
) -> bytes:
    """S 0/0131 primary object create/update for current EXE sub_1654C80.

    Minimal live body: key=character_id, remaining zeros.
    0016/0017/0018 only lookup the object; create/update is 0131.
    """
    return struct.pack(
        "<HIIHIBIB",
        int(character_id) & 0xFFFF,
        int(field_u32_a) & 0xFFFFFFFF,
        int(field_u32_b) & 0xFFFFFFFF,
        int(field_u16_c) & 0xFFFF,
        int(field_u32_d) & 0xFFFFFFFF,
        int(field_u8_e) & 0xFF,
        int(field_u32_f) & 0xFFFFFFFF,
        int(field_u8_g) & 0xFF,
    )


def build_user_position_seria(
    character_id: int = 0,
    pos_x: int = 474,
    pos_y: int = 234,
    direction: int = 5,
    param: int = 100,
) -> bytes:
    """S 0/0016 USER_POSITION for Seria gate room.

    Handler sub_1675F30:
      u16 instance_key
      u16 pos_x
      u16 pos_y
      u8  direction
      u16 param
    instance_key must match START n2_8 / USERINFO0 key (character_id).
    """
    return struct.pack(
        "<HHHBH",
        int(character_id) & 0xFFFF,
        int(pos_x) & 0xFFFF,
        int(pos_y) & 0xFFFF,
        int(direction) & 0xFF,
        int(param) & 0xFFFF,
    )


def build_user_area(
    character_id: int,
    town_id: int = 1,
    area_id: int = 0,
    pos_x: int = 474,
    pos_y: int = 234,
    direction: int = 5,
    state: int = 3,
) -> bytes:
    """S 0/0017 USER_AREA for current EXE sub_167A160 lineage.

      u16 instance_key
      u8  town_id
      u8  area_id
      u16 pos_x
      u16 pos_y
      u8  direction
      u8  state/active
    """
    return struct.pack(
        "<HBBHHBB",
        int(character_id) & 0xFFFF,
        int(town_id) & 0xFF,
        int(area_id) & 0xFF,
        int(pos_x) & 0xFFFF,
        int(pos_y) & 0xFFFF,
        int(direction) & 0xFF,
        int(state) & 0xFF,
    )




def build_avatar_closet_set_noti(
    unlocked_set_count: int = 2,
    current_set_index: int = 0,
) -> bytes:
    # Stage3 read_avatar_closet_state_043E_FA48E0 consumes two u32 values.
    # The first controls set visibility; the second selects the displayed set.
    return struct.pack(
        "<II",
        int(unlocked_set_count) & 0xFFFFFFFF,
        int(current_set_index) & 0xFFFFFFFF,
    )

def build_area_users(
    character_id: int,
    town_id: int = 1,
    area_id: int = 0,
    pos_x: int = 474,
    pos_y: int = 234,
    direction: int = 5,
    state: int = 3,
) -> bytes:
    """S 0/0018 AREA_USERS for current EXE sub_16800B0 lineage.

      u8  town_id
      u8  area_id
      u16 count
      repeat:
        u16 key
        u16 x
        u16 y
        u8  dir
        u8  active/state
    Minimal enter-room list is a single self entry.
    """
    body = bytearray()
    body += struct.pack("<BBH", int(town_id) & 0xFF, int(area_id) & 0xFF, 1)
    body += struct.pack(
        "<HHHBB",
        int(character_id) & 0xFFFF,
        int(pos_x) & 0xFFFF,
        int(pos_y) & 0xFFFF,
        int(direction) & 0xFF,
        int(state) & 0xFF,
    )
    return bytes(body)
