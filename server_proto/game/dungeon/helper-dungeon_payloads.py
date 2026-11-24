"""USDOF dungeon S2C body builders (from 副本实现A.md / friend DFOUSServer).

Authority for wire shapes:
  - US client readers: sub_1631B00 (0005), sub_1678070 (001B),
    sub_163D660 (001C), sub_16411A0 (001D), sub_1638290 (022F),
    sub_167ACB0 (0138)
  - Document: 副本实现A.md
  - Do NOT use s4a12 short bodies for 0005/001B on this client.
"""
from __future__ import annotations

import struct
from typing import Iterable, Sequence

from pvf.adventure_group_index import resolve_expertise_progress


CLIENT_GOLD_DISPLAY_CAP = 999999999
CLIENT_REVIVE_COIN_DISPLAY_CAP = 9_999


def clamp_wallet_gold_for_client(gold_total: int) -> int:
    """Return the fixed wallet value accepted by the Stage3 client UI."""
    return min(CLIENT_GOLD_DISPLAY_CAP, max(0, int(gold_total)))


def clamp_wallet_revive_coin_for_client(revive_coin_total: int) -> int:
    """Return the bounded revive-coin wallet projection for the Stage3 UI."""
    return min(CLIENT_REVIVE_COIN_DISPLAY_CAP, max(0, int(revive_coin_total)))


def build_dungeon_permission_0005(
    entries: Sequence[tuple[int, int]] | None = None,
    *,
    clear_state: int = 1,
) -> bytes:
    """S0/0005 DUNGEON_PERMISSION (US sub_1631B00).

    ``u16 count`` + count * (``u32 dungeon_id`` + ``u8 clear_state``).
    Forbidden: s4a12 ``u16 id + u8`` short rows.
    """
    rows = list(entries or [])
    if len(rows) > 0xFFFF:
        rows = rows[:0xFFFF]
    body = bytearray(struct.pack("<H", len(rows)))
    state = int(clear_state) & 0xFF
    for dungeon_id, row_state in rows:
        body += struct.pack(
            "<IB",
            int(dungeon_id) & 0xFFFFFFFF,
            int(row_state if row_state is not None else state) & 0xFF,
        )
    return bytes(body)


def build_license_dungeon_clear_info_02fe(day_key: int = 0) -> bytes:
    """S0/02FE LICENSE_DUNGEON_CLEAR_INFO: one ``u8`` schedule key.

    Stage3 reader ``0x01633F60`` forwards this byte to ``0x008DDF80``.
    In the frozen ``LicenseDungeonInfo.etc`` table, key 0 selects the authored
    all-open Dark Elf Ruins set.
    """
    value = int(day_key)
    if not 0 <= value <= 0xFF:
        raise ValueError("02FE license dungeon day key exceeds u8")
    return struct.pack("<B", value)


def build_license_dungeon_permission_02fd(
    entries: Sequence[tuple[int, int, int]] | None = None,
) -> bytes:
    """Build Stage3 S0/02FD's current licensed-dungeon snapshot.

    The frozen reader ``0x01640C50`` consumes a ``u16`` row count followed by
    ``u32 dungeon_id, u32 license_level, u32 field3`` triples.  The local
    snapshot uses ``field3=0``; it is retained on the wire for the client
    license manager but does not participate in star access.
    """
    rows = list(entries or [])
    if len(rows) > 0xFFFF:
        rows = rows[:0xFFFF]
    body = bytearray(struct.pack("<H", len(rows)))
    for dungeon_id, license_level, field3 in rows:
        body += struct.pack(
            "<III",
            int(dungeon_id) & 0xFFFFFFFF,
            int(license_level) & 0xFFFFFFFF,
            int(field3) & 0xFFFFFFFF,
        )
    return bytes(body)


def build_enter_select_user_state(user_id: int, state: int = 0x01) -> bytes:
    """S0/0003 USER_STATE: u8 count=1 + u16 user_id + u8 state."""
    return bytes([0x01]) + (int(user_id) & 0xFFFF).to_bytes(2, "little") + bytes([int(state) & 0xFF])


def build_party_host_slot(slot: int) -> bytes:
    """S0/001A selects a party host slot (016528F0 -> 02D65BD0+30).

    Zero selects slot 0; it does not disable UDP. The actor lookup uses the
    eight-slot native party table (01E83AA0 / 01E83740).
    """
    if type(slot) is not int or not 0 <= slot < 8:
        raise ValueError("native host requires an explicit party slot 0..7")
    return bytes([slot])


def build_udp_host_unavailable() -> bytes:
    """Legacy name: preserves the existing slot-zero body, not UDP readiness."""
    return build_party_host_slot(0)


def build_enter_select_dungeon_001b_us(
    user_id: int,
    *,
    mode: int = 1,
    reserved: int = 0,
    missing_hell_quest_user_keys: Sequence[int] = (),
    field_a: int = 0,
    field_b: int = 0,
    field_c: int = 0,
    field_d: int = 0,
    flag_e: int = 0,
    n4: int = 0,
    field_f: int = 0,
) -> bytes:
    """S0/001B ENTER_SELECT_DUNGEON US body (sub_1678070).

    Layout (27B when no member is blocked):
      u32 mode
      u32 reserved          # US is u32, NOT s4a12 u16
      u8  missing_hell_quest_count
      u16 missing_hell_quest_user_keys[count]
      u8  list2_count       # 0
      u8  list3_count       # 0
      u32 field_a
      u16 field_b
      u16 field_c
      u32 field_d
      u8  flag_e
      u8  n4                # must be < 4
      u16 field_f

    Stage3 ``sub_1678070`` clears the hell-button enable flag whenever
    ``missing_hell_quest_count`` is non-zero.  ``user_id`` remains in the
    signature for call compatibility; it is emitted only when the caller
    explicitly includes it in ``missing_hell_quest_user_keys``.
    """
    if int(n4) >= 4:
        raise ValueError("001B n4 must be < 4")
    blocked_keys = tuple(
        int(value) & 0xFFFF for value in missing_hell_quest_user_keys
    )
    if len(blocked_keys) > 0xFF:
        raise ValueError("001B missing hell quest list exceeds u8 count")

    body = bytearray(struct.pack(
        "<IIB",
        int(mode) & 0xFFFFFFFF,
        int(reserved) & 0xFFFFFFFF,
        len(blocked_keys),
    ))
    if blocked_keys:
        body += struct.pack("<%dH" % len(blocked_keys), *blocked_keys)
    body += struct.pack(
        "<BBIHHIBBH",
        0,  # list2_count
        0,  # list3_count
        int(field_a) & 0xFFFFFFFF,
        int(field_b) & 0xFFFF,
        int(field_c) & 0xFFFF,
        int(field_d) & 0xFFFFFFFF,
        int(flag_e) & 0xFF,
        int(n4) & 0xFF,
        int(field_f) & 0xFFFF,
    )
    return bytes(body)


def build_guild_dungeon_repeat_bonus_info_02e6(
    clear_counts: Sequence[tuple[int, int]] = (),
    *,
    enable: bool = True,
    value_a: int = 0,
    value_b: int = 0,
    value_c: int = 0,
) -> bytes:
    """S0/02E6 guild-dungeon state consumed by Stage3 ``sub_B35190``.

    Layout: ``u8 enable`` + three ``u32`` values + ``u32 pair_count`` +
    ``pair_count * {u32 dungeon_id, u32 clear_count}``.
    """
    rows = tuple(clear_counts or ())
    if len(rows) > 0xFFFFFFFF:
        raise ValueError("02E6 guild dungeon pair count exceeds u32")

    scalars = (value_a, value_b, value_c)
    if any(not 0 <= int(value) <= 0xFFFFFFFF for value in scalars):
        raise ValueError("02E6 guild dungeon scalar exceeds u32")
    body = bytearray(
        struct.pack(
            "<BIIII",
            1 if enable else 0,
            int(value_a),
            int(value_b),
            int(value_c),
            len(rows),
        )
    )
    for dungeon_id, clear_count in rows:
        if not 0 < int(dungeon_id) <= 0xFFFFFFFF:
            raise ValueError("02E6 guild dungeon id must be a nonzero u32")
        if not 0 <= int(clear_count) <= 0xFFFFFFFF:
            raise ValueError("02E6 guild dungeon clear count exceeds u32")
        body += struct.pack("<II", int(dungeon_id), int(clear_count))
    return bytes(body)


def build_enter_select_dungeon_001b_s4_legacy(user_id: int) -> bytes:
    """Legacy s4a12 19B body — DO NOT send to Stage3/US client."""
    return struct.pack("<iHBH", 1, 0, 1, int(user_id) & 0xFFFF) + bytes(10)


def build_dungeon_info_001c_us(
    dungeon_id: int,
    difficulty: int = 0,
    *,
    boss_x: int = 0,
    boss_y: int = 0,
    maze_index: int = 0,
    entry_option: int = 0,
    hell_x: int = 0xFF,
    hell_y: int = 0xFF,
    hell_party_enabled: int = 0,
    dungeon_mode: int = 0,
    pair_groups: Sequence[Sequence[tuple[int, int]]] = (),
    value2: int = 0,
    flag_a: int = 0,
) -> bytes:
    """S0/001C DUNGEON_INFO US body (Stage3 sub_163D660 / 90qiyuan currentDungeonInfo).

    Layout (minimal empty groups, 36B; 对齐 90qiyuan dungeon_info.go)::

      u32 dungeon_id
      u8  difficulty
      u16 entry_option          # 普通进本 0（不是 maze_index！）
      u8  maze_index            # 剧情迷宫写真实 index；自由 maze 多为 0
      u8  bossX, bossY
      u8  hellX, hellY
      u8  dungeonMode
      u8  groupCount
      groupCount * {
        u8 pointCount
        pointCount * { u8 x, u8 y }
      }
      u16 hellPartyEnabled
      u16 value1=0x000C
      u8  value2, flagA
      u32 packetSeed=0xFFFFFFFF  # -1 skips seed apply
      u8  paramA/B/C
      u8  flag0/1/2
      u8  extra_list_count=0
      u8  tail_state
      u16 tail0, tail1

    历史 bug：曾把 maze_index 写成 u16（占 entry_option 位），导致 maze=1 时
    客户端读成 EntryOption=1 / MazeIndex=0 → 永远进自由迷宫拓扑。
    90qiyuan 证据：EntryOption u16 + MazeIndex u8（见 dungeon_info_test.go）。

    Stage3 stream still consumes tail_state before tail0/tail1. The
    earlier 35B scaffold dropped it and under-ran the 001C reader, which
    aborts enter before C1/0025 FINISH_LOADING.
    """
    groups = tuple(tuple(group or ()) for group in tuple(pair_groups or ()))
    if len(groups) > 0xFF:
        raise ValueError("001C pair group count exceeds u8")
    if any(len(group) > 0xFF for group in groups):
        raise ValueError("001C pair point count exceeds u8")

    body = bytearray()
    body += struct.pack("<I", int(dungeon_id) & 0xFFFFFFFF)
    body += bytes([int(difficulty) & 0xFF])
    body += struct.pack("<H", int(entry_option) & 0xFFFF)  # EntryOption（普通=0）
    body += bytes([int(maze_index) & 0xFF])  # MazeIndex u8（剧情迷宫真实 index）
    body += bytes([int(boss_x) & 0xFF, int(boss_y) & 0xFF])
    body += bytes([int(hell_x) & 0xFF, int(hell_y) & 0xFF])
    body += bytes([int(dungeon_mode) & 0xFF])
    body += bytes([len(groups)])
    for group in groups:
        body += bytes([len(group)])
        for point in group:
            if not isinstance(point, (tuple, list)) or len(point) < 2:
                raise ValueError("001C pair point must contain x and y")
            body += bytes([int(point[0]) & 0xFF, int(point[1]) & 0xFF])
    body += struct.pack("<H", int(hell_party_enabled) & 0xFFFF)
    body += struct.pack("<H", 0x000C)  # value1 / DungeonValue
    body += bytes([int(value2) & 0xFF, int(flag_a) & 0xFF])
    body += struct.pack("<I", 0xFFFFFFFF)  # packetSeed = -1 skip apply
    body += bytes([0, 0, 0])  # paramA/B/C
    body += bytes([0, 0, 0])  # flag0/1/2
    body += bytes([0])  # extra_list_count
    body += bytes([0])  # tail_state
    body += struct.pack("<HH", 0, 0)  # tail0, tail1
    return bytes(body)


def build_monster_row_us24(
    *,
    template_order: int = 0,
    slot_index: int = 0,
    seq: int = 1,
    code: int = 0,
    level: int = 1,
    mon_type: int = 0,
    aux: int = 0,
    aux2: int = 0,
    flag0: int = 0,
    flag1: int = 0,
    extra_state: int = 0,
    extra_flag: int = 0,
) -> bytes:
    """Stage3 001D mon row 24B (sub_16411A0 / friend doc).

    Layout:
      u16 templateOrder
      u16 aux=0
      u8  aux2=0
      u32 mon+0 slot_index (NOT script id)
      u16 seq
      u32 mon+12 script id
      u8  level
      u8  type
      u8  flag0, flag1
      u32 extraState
      u8  extraFlag
    """
    body = bytearray()
    body += struct.pack("<H", int(template_order) & 0xFFFF)
    body += struct.pack("<H", int(aux) & 0xFFFF)
    body += bytes([int(aux2) & 0xFF])
    body += struct.pack("<I", int(slot_index) & 0xFFFFFFFF)
    body += struct.pack("<H", int(seq) & 0xFFFF)
    body += struct.pack("<I", int(code) & 0xFFFFFFFF)
    body += bytes([
        int(level) & 0xFF,
        int(mon_type) & 0xFF,
        int(flag0) & 0xFF,
        int(flag1) & 0xFF,
    ])
    body += struct.pack("<I", int(extra_state) & 0xFFFFFFFF)
    body += bytes([int(extra_flag) & 0xFF])
    if len(body) != 24:
        raise ValueError("US mon row must be 24B, got %d" % len(body))
    return bytes(body)


def build_passive_object_drop_entry_21b(
    *,
    object_index: int = 0,
    global_seq: int = 0,
    item_id: int = 0,
    stack_count: int = 1,
    endurance: int = 0,
) -> bytes:
    """S0/001D passive-object drop extra row consumed by Stage3 (21B).

    Layout::

      u8  object_index   # MAP special-passive instance id, resolved from [item]
      u32 global_seq     # 客户端地面物键；C1/002B 回声键
      u32 item_id
      u32 stack_count
      u16 endurance
      u8  amplify_type=0
      u16 amplify_value=0
      u16 extended0=0
      u8  extended1=0
    """
    body = bytearray()
    body += bytes([int(object_index) & 0xFF])
    body += struct.pack("<I", int(global_seq) & 0xFFFFFFFF)
    body += struct.pack("<I", int(item_id) & 0xFFFFFFFF)
    body += struct.pack("<I", max(1, int(stack_count)) & 0xFFFFFFFF)
    body += struct.pack("<H", max(0, int(endurance)) & 0xFFFF)
    body += bytes([0])  # amplify type
    body += struct.pack("<H", 0)  # amplify value
    body += struct.pack("<H", 0)  # extended
    body += bytes([0])  # extended
    if len(body) != 21:
        raise ValueError("Stage3 passive-object drop row must be 21B")
    return bytes(body)


def build_minimap_icon_info_022f_us(
    entries: Sequence[tuple[int, int, int]] = (),
) -> bytes:
    """S0/022F MINIMAP_ICON_INFO for the frozen Stage3 client.

    Stage3 ``sub_1638290`` reads ``u16 count`` followed by
    ``count * (u8 x, u8 y, u32 monsterCode)``.  The code selects an entry from
    the DGN ``[dungeon minimap icon setting]`` table; it is a locator key, not
    the quest's kill/rescue target.
    """
    rows = tuple(entries or ())
    if len(rows) > 0xFFFF:
        raise ValueError("022F minimap icon count exceeds u16")

    body = bytearray(struct.pack("<H", len(rows)))
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) < 3:
            raise ValueError("022F minimap icon row must contain x, y and code")
        body += bytes([int(row[0]) & 0xFF, int(row[1]) & 0xFF])
        body += struct.pack("<I", int(row[2]) & 0xFFFFFFFF)
    return bytes(body)


def build_complete_condition_pass_gate_0138_us() -> bytes:
    """S0/0138 COMPLETE_CONDITION_PASS_GATE for frozen Stage3.

    Stage3 ``sub_167ACB0`` reads an unused ``u32`` followed by an unused
    ``u8``, then marks the client-local condition gate complete and refreshes
    the scene containers.  Both fields are zero in the native trigger shape.
    """
    return struct.pack("<IB", 0, 0)


def build_ridable_object_row_20b(
    *,
    pos_x: int,
    pos_y: int,
    object_index: int,
    faction: int,
    reserved: int = 0,
) -> bytes:
    """Stage3 001D interactive vehicle row consumed by ``sub_16411A0``."""
    return struct.pack(
        "<iiiii",
        int(pos_x),
        int(pos_y),
        int(object_index),
        int(faction),
        int(reserved),
    )


def build_start_map_001d_us(
    *,
    cell_x: int = 0,
    cell_y: int = 0,
    seed: int = 0x12345678,
    map_index: int = 0,
    room_flag: int = 1,
    layered: int = 0,
    monsters: Sequence[dict] | None = None,
    map_index_width: str = "u32",
    hell_party_mode: int = 0,
    unknown: int = 0,
    room_state_value: int = 1,
    open_door_even_enemy: bool = False,
    hell_fog: int = 0,
    party_member_index: int = 0,
    passive_object_drops: Sequence[dict] | None = None,
    ridable_objects: Sequence[dict] | None = None,
) -> bytes:
    """S0/001D START_MAP with optional US24 mon rows.

    Stage3 sub_16411A0 uses the final byte to select a party actor through
    01E83AA0 (01641AB4..D2), then branches on that actor's local/remote
    player object. 01642459..64 later compares it with the local slot only
    for a conditional peer-send branch. This is not an unconditional gate
    requiring each recipient's own slot. Preserve the historical parameter
    name; do not replace it with a per-recipient slot without full peer-flow
    evidence. 0xFF skips indexed selection and takes a separate fallback.

    ``passive_object_drops``：.dgn ``[special passive object item]`` 预生成
    建筑掉落（每项 21B；Stage3 ``sub_16411A0``）。缺省 count=0。

    ``ridable_objects``：maze ``[randomized object creation]`` 中本房间的
    可交互载具。Stage3 使用一个组，每项为五个 i32（20B）。

    ``open_door_even_enemy``：maze 同名标签存在时，把仍属敌对挡门的
    wire actor 行末 clear-scan skip byte 置 1。Stage3 会保留 actor 本身，
    但在本地房门存活扫描中跳过它。
    """
    mons = [
        mon
        for mon in list(monsters or [])
        if bool(mon.get("wire_visible", True))
    ]
    if len(mons) > 0xFF:
        mons = mons[:0xFF]
    extras = list(passive_object_drops or [])
    if len(extras) > 0xFF:
        extras = extras[:0xFF]
    ridables = list(ridable_objects or [])
    if len(ridables) > 0xFF:
        ridables = ridables[:0xFF]
    body = bytearray()
    body += bytes([int(cell_x) & 0xFF, int(cell_y) & 0xFF])
    body += bytes([int(layered) & 0xFF])
    body += struct.pack("<I", int(seed) & 0xFFFFFFFF)
    body += bytes([int(hell_party_mode) & 0xFF, int(unknown) & 0xFF])
    body += struct.pack("<I", int(room_state_value) & 0xFFFFFFFF)
    body += bytes([int(room_flag) & 0xFF])
    width = str(map_index_width or "u32").strip().lower()
    if width in {"u16", "16", "short"}:
        body += struct.pack("<H", int(map_index) & 0xFFFF)
    else:
        body += struct.pack("<I", int(map_index) & 0xFFFFFFFF)
    body += bytes([len(mons) & 0xFF])
    for mon in mons:
        body += build_monster_row_us24(
            template_order=int(mon.get("template_order") or 0),
            slot_index=int(mon.get("slot_index") or mon.get("packet_index") or 0),
            seq=int(mon.get("seq") or 0),
            code=int(mon.get("code") or mon.get("script_id") or 0),
            level=(
                int(mon.get("level"))
                if mon.get("level") is not None
                else 1
            ),
            mon_type=int(mon.get("type") or mon.get("mon_type") or 0),
            aux=int(mon.get("aux") or 0),
            aux2=int(mon.get("aux2") or 0),
            flag0=int(mon.get("flag0") or 0),
            flag1=int(mon.get("flag1") or 0),
            extra_state=int(mon.get("extra_state") or 0),
            extra_flag=(
                1
                if open_door_even_enemy
                and bool(mon.get("is_blocking", True))
                else int(mon.get("extra_flag") or 0)
            ),
        )
    body += bytes([len(extras) & 0xFF])  # extraPassiveCount
    for entry in extras:
        if isinstance(entry, (bytes, bytearray)):
            raw = bytes(entry)
            if len(raw) >= 21:
                body += raw[:21]
            else:
                body += raw + bytes(21 - len(raw))
            continue
        body += build_passive_object_drop_entry_21b(
            object_index=int(entry.get("object_index") or entry.get("index") or 0),
            global_seq=int(
                entry.get("global_seq")
                or entry.get("scene_slot")
                or entry.get("seq")
                or 0
            ),
            item_id=int(entry.get("item_id") or entry.get("template_id") or 0),
            stack_count=int(entry.get("stack_count") or entry.get("count") or 1),
            endurance=int(entry.get("endurance") or 0),
        )
    body += bytes([int(hell_fog) & 0xFF])
    if ridables:
        body += bytes([1, len(ridables) & 0xFF])
        for entry in ridables:
            body += build_ridable_object_row_20b(
                pos_x=int(entry.get("pos_x", entry.get("x", 0)) or 0),
                pos_y=int(entry.get("pos_y", entry.get("y", 0)) or 0),
                object_index=int(
                    entry.get("object_index", entry.get("index", 0)) or 0
                ),
                faction=int(entry.get("faction", 0) or 0),
                reserved=int(entry.get("reserved", 0) or 0),
            )
    else:
        body += bytes([0])  # ridableGroups
    body += bytes([int(party_member_index) & 0xFF])
    return bytes(body)


def build_start_map_revisit_001d_us(
    *,
    cell_x: int,
    cell_y: int,
    seed: int,
    room_state_value: int = 1,
    party_member_index: int = 0,
    ridable_objects: Sequence[dict] | None = None,
) -> bytes:
    """Stage3 ``001D`` short form for a room already initialized this run.

    The Stage3 reader branches on ``room_flag`` after the common 14-byte
    header.  Values 1/2 consume map actors and passive-drop rows; value 0
    reuses the client's existing room object graph and immediately consumes
    the ridable-group count plus local party slot.  A non-empty ridable group
    can therefore add an interactive vehicle without re-running the cached
    room's MAP actors or passive-object initialization.
    """
    ridables = list(ridable_objects or [])
    if len(ridables) > 0xFF:
        ridables = ridables[:0xFF]
    body = bytearray()
    body += bytes([int(cell_x) & 0xFF, int(cell_y) & 0xFF])
    body += bytes([0])  # layered-room selector: keep the cached room layer
    body += struct.pack("<I", int(seed) & 0xFFFFFFFF)
    body += bytes([0, 0])  # hell-party mode and its trailing selector
    body += struct.pack("<I", int(room_state_value) & 0xFFFFFFFF)
    body += bytes([0])  # room_flag=0: revisit, no map/actor reinitialization
    if ridables:
        body += bytes([1, len(ridables) & 0xFF])
        for entry in ridables:
            body += build_ridable_object_row_20b(
                pos_x=int(entry.get("pos_x", entry.get("x", 0)) or 0),
                pos_y=int(entry.get("pos_y", entry.get("y", 0)) or 0),
                object_index=int(
                    entry.get("object_index", entry.get("index", 0)) or 0
                ),
                faction=int(entry.get("faction", 0) or 0),
                reserved=int(entry.get("reserved", 0) or 0),
            )
    else:
        body += bytes([0])  # ridable-group count
    body += bytes([int(party_member_index) & 0xFF])
    return bytes(body)


def build_start_map_001d_us_empty(
    *,
    cell_x: int = 0,
    cell_y: int = 0,
    seed: int = 0x12345678,
    map_index: int = 0,
    room_flag: int = 1,
    layered: int = 0,
    map_index_width: str = "u32",
    party_member_index: int = 0,
) -> bytes:
    """S0/001D START_MAP empty-monster Stage3 scaffold (monCount=0)."""
    return build_start_map_001d_us(
        cell_x=cell_x,
        cell_y=cell_y,
        seed=seed,
        map_index=map_index,
        room_flag=room_flag,
        layered=layered,
        monsters=None,
        map_index_width=map_index_width,
        party_member_index=party_member_index,
    )


def build_exp_notification_0025(
    level: int,
    total_exp: int,
    *,
    expertise_exp: int = 0,
    remain_sp: int = 0,
    remain_tp: int = 0,
    page0_sp: int | None = None,
    page1_sp: int | None = None,
    page0_tp: int | None = None,
    page1_tp: int | None = None,
    pvp_victory_point: int = 0,
    bonus_exp: int = 0,
    party_reward_exp: int = 0,
    member_reward_exp: int = 0,
    growth_contract_bonus_exp: int = 0,
    fatigue_burn_bonus_exp: int = 0,
    neo_premium_bonus_exp: int = 0,
    gain_exp: int = 0,
    sp_value: int = 0,
    pairs: Sequence[tuple[int, int]] | None = None,
    layout: str = "stage3_78",
) -> bytes:
    """S0/0x0025 ENUM_NOTIPACKET_EXP.

    Default ``stage3_78``：按当前 Stage3 IDA reader 的空 pair 完整读长发送 **78B**。
    DIE/settlement：单发累计 total；SP/TP 各写两份 u16；Stage3 尾部同步
    熟练度等级与当前档内经验；只投影调用方提供的已实现 bonus/status。

    A/B layouts:
      - ``stage3_78``：IDA ``sub_166CB70`` empty-pair 完整读长
      - ``stage3_74``：旧配置兼容名，也生成修正后的 78B body
      - ``friend_87``：历史朋友 DFOUSServer 87B 布局，仅显式 A/B 使用
      - ``s4a12_95``：旧 95B（+9 memberBonus 会错位 SP→TP；US 忽略）
    """
    layout_norm = str(layout or "stage3_78").strip().lower()
    p0 = int(page0_sp if page0_sp is not None else remain_sp) & 0xFFFF
    p1 = int(page1_sp if page1_sp is not None else remain_sp) & 0xFFFF
    t0 = int(page0_tp if page0_tp is not None else remain_tp) & 0xFFFF
    t1 = int(page1_tp if page1_tp is not None else 0) & 0xFFFF
    if layout_norm in {"s4a12", "s4a12_95", "95", "friend_95"}:
        return _build_exp_notification_0025_s4a12_95(
            level,
            total_exp,
            page0_sp=p0,
            page1_sp=p1,
            page0_tp=t0,
            page1_tp=t1,
            pvp_victory_point=pvp_victory_point,
            party_reward_exp=party_reward_exp if party_reward_exp else bonus_exp,
            member_reward_exp=member_reward_exp,
            growth_contract_bonus_exp=growth_contract_bonus_exp,
            fatigue_burn_bonus_exp=fatigue_burn_bonus_exp,
            neo_premium_bonus_exp=neo_premium_bonus_exp,
            gain_exp=gain_exp,
        )
    if layout_norm in {
        "stage3",
        "stage3_78",
        "78",
        "stage3_74",
        "74",
        "empty_pair",
    }:
        return _build_exp_notification_0025_stage3(
            level,
            total_exp,
            expertise_exp=expertise_exp,
            page0_sp=p0,
            page1_sp=p1,
            page0_tp=t0,
            page1_tp=t1,
            bonus_exp=bonus_exp,
            growth_contract_bonus_exp=growth_contract_bonus_exp,
            fatigue_burn_bonus_exp=fatigue_burn_bonus_exp,
            neo_premium_bonus_exp=neo_premium_bonus_exp,
            gain_exp=gain_exp,
            pvp_victory_point=sp_value if sp_value else pvp_victory_point,
            pairs=pairs,
        )
    if layout_norm in {"friend", "friend_87", "us_87", "87"}:
        return _build_exp_notification_0025_friend_87(
            level,
            total_exp,
            page0_sp=p0,
            page1_sp=p1,
            page0_tp=t0,
            page1_tp=t1,
            bonus_exp=bonus_exp if bonus_exp else party_reward_exp,
        )
    # Unknown/missing layouts must remain on the verified Stage3 reader shape;
    # the legacy 87B body is available only through an explicit alias above.
    return _build_exp_notification_0025_stage3(
        level,
        total_exp,
        expertise_exp=expertise_exp,
        page0_sp=p0,
        page1_sp=p1,
        page0_tp=t0,
        page1_tp=t1,
        bonus_exp=bonus_exp,
        growth_contract_bonus_exp=growth_contract_bonus_exp,
        fatigue_burn_bonus_exp=fatigue_burn_bonus_exp,
        neo_premium_bonus_exp=neo_premium_bonus_exp,
        gain_exp=gain_exp,
        pvp_victory_point=sp_value if sp_value else pvp_victory_point,
        pairs=pairs,
    )


def _build_exp_notification_0025_friend_87(
    level: int,
    total_exp: int,
    *,
    page0_sp: int = 0,
    page1_sp: int = 0,
    page0_tp: int = 0,
    page1_tp: int = 0,
    remain_sp: int = 0,
    remain_tp: int = 0,
    bonus_exp: int = 0,
) -> bytes:
    """朋友 DFOUSServer / 明细1.md：固定 **87B (0x57)** live 已证。

    字段表（杀怪路径）：
      +0  u8  level
      +1  u32 total_exp
      +5  u32 cat0/partyBonus = 0（或 bonus_exp A/B）
      +9  u16 remain_sp
      +11 u16 remain_sp（重复）
      +13 u16 remain_tp
      +15 u16 remain_tp（重复）
      +17 u32 PVP victory abs = 0（Stage3 sub_166CB70 v121；杀怪必须 0，非 0 会走决斗胜点 toast）
      +21 u32 cat1 = 0
      +25 u8  result_flag = 0
      +26..+45  5×u32 cat2/3/ind/4/5 = 0
      +46 u8  dynamic_count = 0（必须 0）
      +47..+86 10×u32 尾部分类 = 0

    Live 样本 lv7 exp=19519 sp=15：
      ``07 3F 4C 00 00 00 00 00 00 0F 00 0F 00 ...`` 共 87B。
    90qiyuan buildCurrentFinishLoadingCharacterStateBody 同 87B 布局（对照非事实标准）。
    """
    if remain_sp and not page0_sp:
        page0_sp = page1_sp = int(remain_sp) & 0xFFFF
    if remain_tp and not page0_tp:
        page0_tp = page1_tp = int(remain_tp) & 0xFFFF
    # 朋友：两份 remain_sp / 两份 remain_tp（page0 优先；page1 缺省跟 page0）
    sp = int(page0_sp) & 0xFFFF
    sp2 = int(page1_sp if page1_sp else page0_sp) & 0xFFFF
    tp = int(page0_tp) & 0xFFFF
    tp2 = int(page1_tp if page1_tp else page0_tp) & 0xFFFF
    body = bytearray()
    body += bytes([int(level) & 0xFF])  # +0
    body += struct.pack("<I", int(total_exp) & 0xFFFFFFFF)  # +1
    body += struct.pack("<I", int(bonus_exp) & 0xFFFFFFFF)  # +5 cat0
    body += struct.pack("<HHHH", sp, sp2, tp, tp2)  # +9..+16
    # +17 = Stage3 v121 PVP victory absolute — kill/clear path MUST stay 0
    body += struct.pack("<I", 0)
    body += struct.pack("<I", 0)  # +21 cat1
    body += bytes([0])  # +25 result_flag
    for _ in range(5):  # +26 cat2,cat3,independent,cat4,cat5
        body += struct.pack("<I", 0)
    body += bytes([0])  # +46 dynamic_count MUST 0
    for _ in range(10):  # +47..+86
        body += struct.pack("<I", 0)
    if len(body) != 87:
        raise ValueError("0025 EXP friend_87 body must be 87B, got %d" % len(body))
    return bytes(body)


def _build_exp_notification_0025_s4a12_95(
    level: int,
    total_exp: int,
    *,
    page0_sp: int = 0,
    page1_sp: int = 0,
    page0_tp: int = 0,
    page1_tp: int = 0,
    pvp_victory_point: int = 0,
    party_reward_exp: int = 0,
    member_reward_exp: int = 0,
    growth_contract_bonus_exp: int = 0,
    fatigue_burn_bonus_exp: int = 0,
    neo_premium_bonus_exp: int = 0,
    gain_exp: int = 0,
    remain_sp: int = 0,
    remain_tp: int = 0,
    bonus_exp: int = 0,
) -> bytes:
    """s4a12 ExpNotificationBuilder.Build — 95B (副本实现A authority)."""
    if remain_sp and not page0_sp:
        page0_sp = page1_sp = int(remain_sp) & 0xFFFF
    if remain_tp and not page0_tp:
        page0_tp = int(remain_tp) & 0xFFFF
    if bonus_exp and not party_reward_exp:
        party_reward_exp = int(bonus_exp) & 0xFFFFFFFF

    body = bytearray()
    body += bytes([int(level) & 0xFF])  # +0x00 level
    body += struct.pack("<I", int(total_exp) & 0xFFFFFFFF)  # +0x01
    body += struct.pack("<I", int(party_reward_exp) & 0xFFFFFFFF)  # +0x05
    body += struct.pack("<I", int(member_reward_exp) & 0xFFFFFFFF)  # +0x09
    body += struct.pack(
        "<HHHH",
        int(page0_sp) & 0xFFFF,
        int(page1_sp) & 0xFFFF,
        int(page0_tp) & 0xFFFF,
        int(page1_tp) & 0xFFFF,
    )  # +0x0D
    body += struct.pack("<I", int(pvp_victory_point) & 0xFFFFFFFF)  # +0x15 PVP
    body += struct.pack("<I", 0)  # +0x19 fatigueBuff
    body += bytes([0])  # +0x1D mode
    body += struct.pack("<I", 0)  # +0x1E seria
    body += struct.pack(
        "<I", int(growth_contract_bonus_exp) & 0xFFFFFFFF
    )  # +0x22 growth contract
    body += struct.pack("<I", 0)  # +0x26 unk
    body += struct.pack(
        "<I", int(fatigue_burn_bonus_exp) & 0xFFFFFFFF
    )  # +0x2A fatigue burn
    body += struct.pack("<I", 0)  # +0x2E cafe
    body += bytes([0])  # +0x32 N=0
    body += struct.pack("<I", 0)  # +0x33 elite
    body += struct.pack("<I", 0)  # +0x37
    body += struct.pack("<I", 0)  # +0x3B capsule
    body += struct.pack("<I", 0)  # +0x3F honor lv
    body += struct.pack("<I", 0)  # +0x43 honor exp
    body += struct.pack("<I", 0)  # +0x47
    body += struct.pack("<I", 0)  # +0x4B
    body += struct.pack("<I", 0)  # +0x4F channel
    body += struct.pack("<I", 0)  # +0x53 welcome
    body += struct.pack("<II", 0, 0)  # +0x57/+0x5B tail
    if len(body) != 95:
        raise ValueError("0025 EXP s4a12 body must be 95B, got %d" % len(body))
    return bytes(body)


def _build_exp_notification_0025_stage3(
    level: int,
    total_exp: int,
    *,
    expertise_exp: int = 0,
    page0_sp: int = 0,
    page1_sp: int = 0,
    page0_tp: int = 0,
    page1_tp: int = 0,
    remain_sp: int = 0,
    remain_tp: int = 0,
    bonus_exp: int = 0,
    growth_contract_bonus_exp: int = 0,
    fatigue_burn_bonus_exp: int = 0,
    neo_premium_bonus_exp: int = 0,
    gain_exp: int = 0,
    pvp_victory_point: int = 0,
    sp_value: int = 0,
    pairs: Sequence[tuple[int, int]] | None = None,
) -> bytes:
    """Stage3 ``sub_166CB70`` EXP body, empty-pair size **78B** (IDA-verified).

    Reader read order (sub_166CB70, stream helpers sub_2C74A00=u8,
    sub_2C74A80=u16, sub_2C74B00=u32, sub_2C74B80=blob):

      off  width var      meaning
      0    u8    n90_2    level          (drives level-up FX; delta vs local)
      1    u32   v123     total_exp      (delta = total_exp - client local_exp)
      5    u32   v120     Party Reward
      9    u16x4 v130/v119/v128/v126     SP0/SP1/TP0/TP1 absolute
      17   u32   v121     PVP victory absolute (keep 0 for normal EXP)
      21   u32x6 v118/v116/v127/v129/v117/v111
           Fatigue / Reward(Neo) / Weekend / Growth / Express / Internet Cafe
      45   u8    v134     pair_count
      +0   u32   v113     Gain (PVF EXP consumable)
      +4   u32   v110     Gain + dynamic-pair detail total
      +8   u32   shared state (``sub_140E280`` implicit read; keep 0)
      +12  u32   Expertise completed level (absolute offset ``+0x3A``)
      +16  u64   Expertise current-level EXP (absolute offset ``+0x3E``)
      +24  u32   v125     Seria Welcome (keep 0)
      +28  u32   v124     Guild Reward (keep 0)

    Only sources implemented by the server are populated. Other fixed rows
    remain zero so the native toast does not attribute experience to unrelated
    categories. Dynamic pairs carry verified character-status contributions.
    The native formatter appends Gain and pair rows but does not subtract them
    individually from its calculated Base value; ``v110`` is the non-rendered
    aggregate it subtracts for those detail rows.

    Empty-pair total = 45 + 1 (pair_count) + 4*4 + 8 + 2*4 = 78 bytes.
    Each pair adds 8B (two u32) between pair_count and the trailing block.
    """
    if remain_sp and not page0_sp:
        page0_sp = page1_sp = int(remain_sp) & 0xFFFF
    if remain_tp and not page0_tp:
        page0_tp = int(remain_tp) & 0xFFFF
    pvp = int(sp_value if sp_value else pvp_victory_point) & 0xFFFFFFFF
    expertise_level, expertise_current_level_exp = resolve_expertise_progress(
        expertise_exp
    )
    rows = list(pairs or [])
    if len(rows) > 0xFF:
        rows = rows[:0xFF]
    dynamic_detail_total = max(0, int(gain_exp)) + sum(
        max(0, int(value)) for _, value in rows
    )
    body = bytearray()
    body += bytes([int(level) & 0xFF])                       # 0    level
    body += struct.pack("<I", int(total_exp) & 0xFFFFFFFF)   # 1    total_exp
    body += struct.pack("<I", int(bonus_exp) & 0xFFFFFFFF)   # 5    Party Reward
    body += struct.pack(                                     # 9    SP0/SP1/TP0/TP1
        "<HHHH",
        int(page0_sp) & 0xFFFF,
        int(page1_sp) & 0xFFFF,
        int(page0_tp) & 0xFFFF,
        int(page1_tp) & 0xFFFF,
    )
    body += struct.pack("<I", pvp)                           # 17   PVP (0)
    # Stage3 fixed detail rows: Fatigue, Reward(Neo), Weekend,
    # Growth, Express, Internet Cafe.  The old builder placed Growth and
    # Fatigue two slots late, which made the client label the values as
    # Weekend/Express.
    bonus_fields = (
        int(fatigue_burn_bonus_exp) & 0xFFFFFFFF,
        int(neo_premium_bonus_exp) & 0xFFFFFFFF,
        0,
        int(growth_contract_bonus_exp) & 0xFFFFFFFF,
        0,
        0,
    )
    for bonus_value in bonus_fields:                         # 21   6x fixed rows
        body += struct.pack("<I", bonus_value)
    body += bytes([len(rows) & 0xFF])                        # 45   pair_count
    for a, b in rows:
        body += struct.pack("<II", int(a) & 0xFFFFFFFF, int(b) & 0xFFFFFFFF)
    body += struct.pack("<I", int(gain_exp) & 0xFFFFFFFF)    # +0   Gain / Potion
    body += struct.pack(                                     # +4   detail aggregate
        "<I", int(dynamic_detail_total) & 0xFFFFFFFF
    )
    body += struct.pack("<I", 0)                             # +8   shared state
    body += struct.pack(                                     # +12  Expertise level
        "<I", int(expertise_level) & 0xFFFFFFFF
    )
    body += struct.pack(                                     # +16  current-level EXP
        "<Q", int(expertise_current_level_exp) & 0xFFFFFFFFFFFFFFFF
    )
    body += struct.pack("<I", 0)                             # +24  v125 (0)
    body += struct.pack("<I", 0)                             # +28  v124 (0)
    expected = 78 + 8 * len(rows)
    if len(body) != expected:
        raise ValueError(
            "0025 EXP stage3 body must be %dB (count=%d), got %d"
            % (expected, len(rows), len(body))
        )
    return bytes(body)


# Stage3 道具条长度：000E / DIE 读包均 push 0x6C（IDA 2026-07-19）
STAGE3_ITEM_LIST_ENTRY_SIZE = 108
# DIE 掉落行：u32 objectKey + 108B entry + u16 sentinel + u16 owner
STAGE3_DEATH_DROP_WIRE_SIZE = 4 + STAGE3_ITEM_LIST_ENTRY_SIZE + 2 + 2  # 116
STAGE3_ZERO_DROP_DEATH_BODY_SIZE = 7
# S0/0027 物品拾取固定 19B
STAGE3_PICKUP_ITEM_RESULT_SIZE = 19
# 金币：u32 key + u16 picker + group0(enable+amount+v79[+extra×n+tax]) + 7×(enable+u32)
# enable=1 时 IDA 必读 v79；v79>0 再读 v79×u32 + tax（S4/音效 effect 路径）
# 默认 effect：v79=1 + extra_u32 + tax_u32 → 6+5+1+4+4+7*5 = 55B
STAGE3_PICKUP_GOLD_RESULT_SIZE = 4 + 2 + 5 + 1 + 4 + 4 + 7 * 5  # 55


def build_drop_item_entry_108(
    scene_slot: int,
    template_id: int,
    stack_count: int,
    *,
    endurance: int = 0,
    upgrade_level: int = 0,
) -> bytes:
    """Stage3 108B 道具条（与 000E / DIE entry 同形）。

    IDA ``sub_160F990`` + ``sub_1647080`` 读后引用::

      +0  u16 slot
      +2  u32 item_template_id   # 0 = 金币
      +6  u32 count/amount       # 金币金额
      +10 u8  ext0/upgrade
      +11 u16 durability
      +13 u8  seal
      其余 0
    """
    row = bytearray(STAGE3_ITEM_LIST_ENTRY_SIZE)
    struct.pack_into(
        "<HII",
        row,
        0,
        int(scene_slot) & 0xFFFF,
        int(template_id) & 0xFFFFFFFF,
        max(0, int(stack_count)) & 0xFFFFFFFF,
    )
    row[10] = int(upgrade_level) & 0xFF
    struct.pack_into("<H", row, 11, max(0, int(endurance)) & 0xFFFF)
    row[13] = 0
    return bytes(row)


def build_monster_die_0026(
    seq: int,
    *,
    drop_count: int = 0,
    drops: list | None = None,
    owner_actor_id: int = 0,
    tail: bytes | None = None,
) -> bytes:
    """S0/0x0026 DIE_MONSTER — Stage3 IDA ``sub_1647080``（2026-07-19）。

    布局（**否决** s4a12 39B 紧凑行）::

      u16 monsterSeq
      u8  dropCount
      dropCount * 116B:
        u32 sceneObjectKey          # GET_ITEM 回声键
        108B item entry             # slot/tid/amount 同 000E
        u16 unknownTailSentinel     # 0xFFFF
        u16 ownerActorObjectKey
      tail 00 00 FF 00

    证据：``push 0x6C; call sub_2C74B80`` @ ``0x16475d8``；
    金额在 entry+6，错位会导致客户端显示金币 0。
    """
    rows: list = list(drops or [])
    count = len(rows) if rows else max(0, min(0xFF, int(drop_count)))
    if count > 0xFF:
        rows = rows[:0xFF]
        count = 0xFF

    body = bytearray()
    body += struct.pack("<H", int(seq) & 0xFFFF)
    body += bytes([int(count) & 0xFF])

    owner = int(owner_actor_id) & 0xFFFF
    for d in rows:
        if isinstance(d, dict):
            scene_slot = int(d.get("scene_slot") or d.get("SceneSlot") or 0)
            template_id = int(d.get("template_id") or d.get("TemplateId") or 0)
            if "stack_count" in d or "StackCount" in d:
                stack_count = int(d.get("stack_count", d.get("StackCount", 0)) or 0)
            else:
                stack_count = 1
            endurance = int(d.get("endurance") or d.get("Endurance") or 0)
            upgrade = int(d.get("upgrade_level") or d.get("UpgradeLevel") or 0)
            object_key = int(
                d.get("object_key")
                or d.get("ObjectKey")
                or d.get("SceneObjectKey")
                or scene_slot
                or 0
            )
        else:
            scene_slot = int(getattr(d, "scene_slot", 0) or 0)
            template_id = int(getattr(d, "template_id", 0) or 0)
            if hasattr(d, "stack_count"):
                stack_count = int(getattr(d, "stack_count") or 0)
            else:
                stack_count = 1
            endurance = int(getattr(d, "endurance", 0) or 0)
            upgrade = int(getattr(d, "upgrade_level", 0) or 0)
            if hasattr(d, "wire_object_key"):
                object_key = int(getattr(d, "wire_object_key") or 0)
            else:
                object_key = int(getattr(d, "object_key", 0) or scene_slot or 0)
        if template_id != 0 and stack_count < 1:
            stack_count = 1
        if stack_count < 0:
            stack_count = 0
        if object_key <= 0:
            object_key = max(1, scene_slot) & 0xFFFF
        if scene_slot <= 0:
            scene_slot = object_key & 0xFFFF

        body += struct.pack("<I", int(object_key) & 0xFFFFFFFF)
        body += build_drop_item_entry_108(
            scene_slot,
            template_id,
            stack_count,
            endurance=endurance,
            upgrade_level=upgrade,
        )
        body += struct.pack("<H", 0xFFFF)  # UnknownTailSentinel
        body += struct.pack("<H", owner)

    if tail is None:
        body += bytes([0x00, 0x00, 0xFF, 0x00])
    else:
        body += bytes(tail)

    expected = STAGE3_ZERO_DROP_DEATH_BODY_SIZE + STAGE3_DEATH_DROP_WIRE_SIZE * count
    if len(body) != expected:
        raise ValueError(
            "0026 DIE body must be %dB (drops=%d, row=%d), got %d"
            % (expected, count, STAGE3_DEATH_DROP_WIRE_SIZE, len(body))
        )
    return bytes(body)


def build_enable_clear_001f(flag: int = 0) -> bytes:
    """S0/0x001F ENABLE_CLEAR_DUNGEON — friend body {0x00}."""
    return bytes([int(flag) & 0xFF])


def build_play_result_0022(
    *,
    play_time_ms: int = 0,
    best_time_ms: int = 0,
    user_key: int = 0,
    rank_letter: int = 99,
    client_rank_point: int = 99,
    all_kill: int = 1,
    result_flag: int = 1,
) -> bytes:
    """S0/0x0022 PLAY_RESULT simplified (副本实现A.md §5.6 / Stage3 sub_1635AF0).

    Layout (the two u32 values are milliseconds, consumed by the settlement
    time UI; they are not experience totals):
      u8  rankLetter
      u32 play_time_ms
      u8  flagB=0
      u8  clientRankPoint
      u8  all_kill
      u8  resultCount=1
      u16 user_key
      u32 best_time_ms
      u8  resultFlag
    """
    current_ms = max(0, min(0xFFFFFFFF, int(play_time_ms)))
    record_ms = max(0, min(0xFFFFFFFF, int(best_time_ms)))
    body = bytearray()
    body += bytes([int(rank_letter) & 0xFF])
    body += struct.pack("<I", current_ms)
    body += bytes([0])  # flagB
    body += bytes([int(client_rank_point) & 0xFF])
    body += bytes([int(all_kill) & 0xFF])
    body += bytes([1])  # resultCount
    body += struct.pack("<H", int(user_key) & 0xFFFF)
    body += struct.pack("<I", record_ms)
    body += bytes([int(result_flag) & 0xFF])
    return bytes(body)


def build_card_select_right_state_0045(ok: int = 1) -> bytes:
    """S1/0x0045 CARD_SELECT_RIGHT_STATE — Stage3 live body: ``u8 ok``."""
    return bytes([int(ok) & 0xFF])


def build_card_layout_ack_0046() -> bytes:
    """S1/0x0046 card layout ack — Stage3 live body.

    Wire: u8 ok=1, u16 free_slot0=1, then 7×u16 0xFFFF.
    """
    body = bytearray()
    body += bytes([0x01])
    body += struct.pack("<H", 0x0001)
    for _ in range(7):
        body += struct.pack("<H", 0xFFFF)
    return bytes(body)


def build_select_card_ack_0047_stage3(
    *,
    command_ok: int = 1,
    player_id: int = 0,
    card_type: int = 0,
    free_owner_seat: int = 0,
    paid_owner_seat: int = 0,
    player_status_seats: Sequence[int] | None = None,
    reward_seat: int = -1,
    rewards: Sequence[tuple[int, int]] | None = None,
    attach_rewards: bool = False,
) -> bytes:
    """S1/0x0047 SELECT_CARD ack — Stage3 CMD 读者 ``sub_15F6D20``。

    完整 S1 包体先放一个 CMD 成功字节，随后才是 reader 的八条记录::

      u8 command_ok  # CMD 分发先消费；成功路径为 1

      # ``sub_15F6D20`` 从这里开始读取：
      for player_record in 0..7:
        u8  fieldA
        u8  fieldB
        u8  reward_count
        count × (u32 key, u32 value)
        u8  player_status

    Stage3 依据：opcode 71 注册在 CMD 表 ``sub_15FDAA0``；同表的 0046
    完整体为 ``u8 ok + 8*u16``，而 ``sub_15FE640`` 只读取后续八个
    ``u16``。因此 ``sub_15F6D20`` 的 32B 记录体也必须位于该成功字节
    之后，完整无奖励成功包固定为 **33B**。

    ``sub_15F6D20`` 的记录读序::

        u8  fieldA   # free 选中者 player_id；0xFF=无人
                     # ==本机 id → sub_2A5F9A0 用本地已点的上方牌位翻牌
        u8  fieldB   # paid 选中者 player_id；0xFF=无人
                     # ==本机 id → sub_2A5FAC0 用本地已点的下方牌位翻牌
                     # 同时当 fieldB<=7 时，本记录 count 奖励落到 fieldB
        u8  reward_count
        count × (u32 key, u32 value)   # key=-1 → 金币
        u8  player_status              # sub_2A5F280(player_record, flag)

    硬约束（2026-07-18 复测回归）：
      - **禁止**把 fieldB 写成座位号 0..7 铺满：solo player_id=0 时 seat0 的
        fieldB=0 会命中本机 → 付费行 8 张全动画（「下面牌乱开」根因）。
      - free 路径：fieldA=player_id（落在 free_owner_seat），fieldB 全 0xFF。
      - paid 路径：fieldB=player_id（落在 paid_owner_seat），fieldA 全 0xFF。
      - 8 条 record 是玩家记录；上四、下四的具体牌位由客户端本地点击状态
        （``this+0x6F4`` / ``this+0x654``）决定，不能把 record 索引当牌位。
      - ``player_status`` 与选中动作独立。单机通关时必须全 0；把本机记录
        写成 1 会进入角色死亡样式，并可能联动另一行牌面。
      - 牌面奖励内容优先靠 S0/0023 card group；0047 默认 **不** 附 rewards
        （attach_rewards=False），避免 fieldB 与 paid-owner 语义冲突。
      - 真钱包入账仍靠 S0/0x000E。

    C2S 对照 ``sub_2A5F830``/``sub_2A5F8C0``：wire 是 ``u8 type + u8 player_id``，
    **不含** 牌面 index（index 只在客户端 this+1780 本地记）。
    """
    pid = int(player_id) & 0xFF
    is_free = int(card_type) == 0
    free_seat = max(0, min(7, int(free_owner_seat)))
    paid_seat = max(0, min(7, int(paid_owner_seat)))

    status_seats = {
        int(seat) & 7
        for seat in tuple(player_status_seats or ())
        if int(seat) >= 0
    }

    rew_pairs: list[tuple[int, int]] = []
    rew_seat = -1
    if attach_rewards and rewards:
        rew_pairs = list(rewards)[:255]
        rew_seat = int(reward_seat)
        if rew_seat < 0:
            rew_seat = free_seat if is_free else paid_seat
        rew_seat = max(0, min(7, rew_seat))

    # Do not omit this byte. Without it, the CMD dispatcher consumes record0
    # fieldA as its result code, shifts fieldB to the paid callback, and leaves
    # the last record one byte short (C1/00D9 {01,47,00}).
    body = bytearray([int(command_ok) & 0xFF])
    for seat in range(8):
        if is_free and seat == free_seat and pid != 0xFF:
            field_a = pid
        else:
            field_a = 0xFF
        if (not is_free) and seat == paid_seat and pid != 0xFF:
            field_b = pid
        else:
            field_b = 0xFF

        # Optional paid rewards belong to the same local player callback as
        # the lower-card animation.  ``seat`` is a record position, not the
        # client player id; using it here breaks any non-zero local id.
        seat_rewards: list[tuple[int, int]] = []
        if rew_pairs and seat == rew_seat:
            field_b = pid if pid != 0xFF else 0xFF
            seat_rewards = rew_pairs

        body += bytes([field_a & 0xFF, field_b & 0xFF])
        body += bytes([len(seat_rewards) & 0xFF])
        for key, value in seat_rewards:
            body += struct.pack(
                "<II",
                int(key) & 0xFFFFFFFF,
                int(value) & 0xFFFFFFFF,
            )
        body += bytes([1 if seat in status_seats else 0])
    return bytes(body)


def build_select_card_ack_0047_stub(
    *,
    flip_seat: int = 0,
    free_gold: int = 0,
    player_id: int = 0,
    card_type: int = 0,
    **_legacy: object,
) -> bytes:
    """兼容入口：solo free 翻牌；默认不在 0047 附奖励（避免 paid 误触发）。"""
    pid = int(player_id) & 0xFF
    seat = max(0, min(7, int(flip_seat)))
    gold = max(0, int(free_gold or 0))
    # 旧参数 flip_seat 若传入，用作 free_owner_seat；flip 仍按 player_id
    return build_select_card_ack_0047_stage3(
        player_id=pid,
        card_type=int(card_type),
        free_owner_seat=seat if int(card_type) == 0 else 0,
        paid_owner_seat=seat if int(card_type) != 0 else 0,
        attach_rewards=False,
        rewards=[(0xFFFFFFFF, gold)] if gold > 0 else None,
        reward_seat=seat,
    )


def build_item_list_update_000e(
    entries: Sequence[tuple[int, ...]] | None = None,
    *,
    list_type: int = 0,
    entry_size: int = 108,
) -> bytes:
    """S0/0x000E UPDATE_ITEM_LIST — Stage3 ``loc_1667BD0`` / ``sub_1667BD0``。

    IDA 读序（``sub_1AB3F90`` 登记 opcode=14 覆盖 toast-only 旧表）::
      sub_2C74A00 → u8  list_type
      sub_2C74A80 → u16 count
      loop: ``push 0x6C; call sub_2C74B80`` → 每条 **108B** 整块拷贝

    **无** list_param16（那是 000D ``sub_1666D80`` type0 才有的字段）。

    条目前缀与 000D 同形（``build_item_list_mode0``）::
      u16 slot
      u32 item_template_id
        - 真实道具 id
        - **0** = 仅 list0 **slot0** 钱包金币（count=金币总量）
        - **-1 / 0xFFFFFFFF** = **空槽**（S4 ``CreateEmptyCommonItem``）
          非 slot0 绝不可写 item_id=0，否则客户端把该格当金币显示
      u32 count/instance     # 装备用 TopQualitySeed=999999998
      u8  ext0
      u16 durability
      u8  seal
      其余 0 至 108

    entries 支持::
      (slot, item_id, count)
      (slot, item_id, count, durability)
    """
    rows = list(entries or [])
    if len(rows) > 0xFFFF:
        rows = rows[:0xFFFF]
    # 强制 108：Stage3 IDA 硬编码 push 0x6C
    size = 108
    body = bytearray()
    body += struct.pack("<BH", int(list_type) & 0xFF, len(rows))
    for row_in in rows:
        slot = int(row_in[0]) & 0xFFFF
        # 允许 -1 空槽哨兵（& 0xFFFFFFFF → 0xFFFFFFFF）
        item_id = int(row_in[1]) & 0xFFFFFFFF
        count = max(0, int(row_in[2])) & 0xFFFFFFFF
        dura = max(0, int(row_in[3]) if len(row_in) > 3 else 0) & 0xFFFF
        row = bytearray(size)
        struct.pack_into("<HII", row, 0, slot, item_id, count)
        row[10] = 0  # ext0
        struct.pack_into("<H", row, 11, dura)
        row[13] = 0  # seal
        body += row
    return bytes(body)


def build_item_list_clear_slot_000e(
    slot_index: int,
    *,
    list_type: int = 0,
) -> bytes:
    """S0/0x000E 清空单格：item_id=-1（非 0）。

    对齐 S4A12 ``ItemListUpdateBuilder.CreateEmptyCommonItem`` /
    ``WriteEmptyEntry``（slot + item_id=-1 + 尾部 0）。
    """
    return build_item_list_update_000e(
        [(int(slot_index), -1, 0)],
        list_type=list_type,
    )


def build_wallet_gold_update_000e(gold_total: int, *, list_type: int = 0) -> bytes:
    """S0/0x000E 钱包金币刷新：list0 slot0 item_id=0 count=wallet_total。

    对齐 Stage3 000D 金币槽语义 + 000E 108B 条目（IDA ``sub_1667BD0``）。
    """
    return build_item_list_update_000e(
        [(0, 0, clamp_wallet_gold_for_client(gold_total))],
        list_type=list_type,
    )


def build_wallet_revive_coin_update_000e(
    revive_coin_total: int, *, list_type: int = 0
) -> bytes:
    """S0/0x000E refresh of the revive-coin wallet at list0 slot 1.

    Item id 1 remains a client-visible projection only.  The actual balance is
    stored in ``account_wallets.revive_coin`` and is capped at 9999 only here.
    """
    return build_item_list_update_000e(
        [(1, 1, clamp_wallet_revive_coin_for_client(revive_coin_total))],
        list_type=list_type,
    )


def build_get_item_pickup_gold_0027(
    object_key: int,
    picker_actor_id: int,
    gold_amount: int,
    *,
    extra_gold: int = 0,
    pickup_effect: bool = True,
) -> bytes:
    """S0/0x0027 GET_ITEM 金币 — Stage3 IDA ``sub_166E300`` 金币分支。

    读序（enable=1 时比「纯 8×(u8+u32)」多读 effect 字段）::

      u32 dropObjectKey
      u16 pickerActorId
      u8  enable0=1
      u32 gold_amount
      u8  v79                # effect / extra count；>0 走 sub_1AC1E50+1AD2D80
      if v79>0:
        v79 × u32 extra      # 第 0 个通常 = extra_gold
        u32 tax
      7 × (u8 enable=0 + u32 0)

    旧 46B 体在 enable 后立刻塞下一组 enable，会把 v79 读成 0 并错位，
    客户端金币飞入/拾取音效路径不完整。默认 ``pickup_effect=True`` 对齐
    S4A12 ``BuildPickupGold``（始终写 effect 标志）与 90qiyuan 的
    pickup-effect gate 语义。
    """
    body = bytearray()
    body += struct.pack("<I", int(object_key) & 0xFFFFFFFF)
    body += struct.pack("<H", int(picker_actor_id) & 0xFFFF)
    amount = max(0, int(gold_amount)) & 0xFFFFFFFF
    extra = max(0, int(extra_gold)) & 0xFFFFFFFF
    # group 0
    body += bytes([1])
    body += struct.pack("<I", amount)
    if pickup_effect:
        # v79=1 → 读 1 个 extra u32 + tax u32（S4 即使 extra=0 也走 effect）
        body += bytes([1])
        body += struct.pack("<I", extra)
        body += struct.pack("<I", 0)  # tax
    else:
        # 仅修正错位：v79=0，无 extra/tax（47B，无音效 effect 路径）
        body += bytes([0])
    # groups 1..7 disabled
    for _ in range(7):
        body += bytes([0])
        body += struct.pack("<I", 0)
    expected = STAGE3_PICKUP_GOLD_RESULT_SIZE if pickup_effect else (4 + 2 + 6 + 7 * 5)
    if len(body) != expected:
        raise ValueError(
            "0027 gold pickup must be %dB, got %d" % (expected, len(body))
        )
    return bytes(body)


def build_get_item_pickup_item_0027(
    object_key: int,
    picker_actor_id: int,
    dst_inv_slot: int,
    *,
    move_flag: int = 0,
) -> bytes:
    """S0/0x0027 GET_ITEM 物品 — Stage3 IDA ``sub_166E300`` 物品分支。

    读序（共 19B）::

      u32 dropObjectKey
      u16 pickerActorId
      8B  zero
      u16 pickerActorId
      u16 dstInvSlot
      u8  moveFlag
    """
    body = bytearray()
    body += struct.pack("<I", int(object_key) & 0xFFFFFFFF)
    body += struct.pack("<H", int(picker_actor_id) & 0xFFFF)
    body += bytes(8)
    body += struct.pack("<H", int(picker_actor_id) & 0xFFFF)
    body += struct.pack("<H", int(dst_inv_slot) & 0xFFFF)
    body += bytes([int(move_flag) & 0xFF])
    if len(body) != STAGE3_PICKUP_ITEM_RESULT_SIZE:
        raise ValueError(
            "0027 item pickup must be %dB, got %d"
            % (STAGE3_PICKUP_ITEM_RESULT_SIZE, len(body))
        )
    return bytes(body)


def build_drop_item_noti_0028(
    updates: Sequence[tuple[int, bytes]] | None = None,
) -> bytes:
    """S0/0x0028 ENUM_NOTIPACKET_DROP_ITEM — Stage3 IDA ``sub_1634FD0``。

    注册：``sub_1AB3F40(40, sub_1634FD0, 0)`` @ ``0x1649bfb``。

    读序::

      u8 count
      count × (
        u8  slot_or_index     # 背包槽（u8，0..255）
        u32 blob_size
        blob_size bytes       # 通常 108B 道具条；0 = 仅占位
      )

    丢弃后刷新背包格：count=1 + slot + 108B（空条或剩余堆叠）。
    count=0 合法（读者立即返回），仅用于无槽可改时的空确认。
    """
    rows = list(updates or [])
    if len(rows) > 0xFF:
        rows = rows[:0xFF]
    body = bytearray()
    body += bytes([len(rows) & 0xFF])
    for slot_or_index, blob in rows:
        raw = bytes(blob or b"")
        body += bytes([int(slot_or_index) & 0xFF])
        body += struct.pack("<I", len(raw) & 0xFFFFFFFF)
        body += raw
    return bytes(body)


def build_drop_item_slot_update_0028(
    slot_index: int,
    *,
    item_template_id: int = 0,
    stack_count: int = 0,
    endurance: int = 0,
    upgrade_level: int = 0,
) -> bytes:
    """S0/0x0028 单槽刷新：108B 道具条（item_id=0/count=0 表示清空）。"""
    entry = build_drop_item_entry_108(
        int(slot_index) & 0xFFFF,
        int(item_template_id) & 0xFFFFFFFF,
        max(0, int(stack_count)),
        endurance=int(endurance),
        upgrade_level=int(upgrade_level),
    )
    return build_drop_item_noti_0028([(int(slot_index) & 0xFF, entry)])


def build_eplp_command_ack_0048(state: int = 1, option: int = 0) -> bytes:
    """S1/0x0048 EPLP_COMMAND ack — s4a12 CardRewardService.HandleEplpCommand."""
    return bytes([0x01, int(state) & 0xFF, int(option) & 0xFF])


def build_clear_dungeon_reward_0023_stage3(
    *,
    clear_base_exp: int = 0,
    score_bonus_exp: int = 0,
    clear_bonus_exp: int = 0,
    neo_premium_exp: int = 0,
    adventure_exp: int = 0,
    potion_exp: int = 0,
    growth_contract_exp: int = 0,
    fatigue_burning_exp: int = 0,
    kill_growth_contract_exp: int = 0,
    kill_neo_premium_exp: int = 0,
    kill_fatigue_burning_exp: int = 0,
    boss_exp: int = 0,
    champion_exp: int = 0,
    super_champion_exp: int = 0,
    monster_exp: int = 0,
    free_card_gold: int = 0,
    free_card_item_id: int = 0,
    free_card_item_count: int = 0,
    paid_card_cost: int = 0,
    show_result: int = 1,
) -> bytes:
    """S0/0x0023 CLEAR_DUNGEON_REWARD — Stage3 ``sub_1645DA0`` 完整体.

    权威：Stage3 IDA ``sub_1645DA0``（非 s4a12 222B 盲抄，也非旧 61B stub）。

    顺序（counts=0 的最小骨架，**182B**）：
      4×u32 base (clear_base / score_bonus / clear_bonus / pad)
      u8  base_flag
      21×u32 early slots
      u8  groupA_count (=0) / u8 groupB_count (=0)
      u32 post_slot
      4×u32 score  (0 / champion / super_champion / boss)
      u32 quest_count (=0)
      u8  drop_count (=0)
      8× (u8 seat_count [+ count×(u32,u32)])  card group1
      u32 paid_card_cost
      8× u8 seat_count (=0)                     card group2
      8× u8 seat_count (=0)                     card group3
      u32 pretail
      u32 value0
      u8  show_result   ← **v83**：非 0 才走 sub_2A5F510(1) 打开结算结果窗
      u8  flag0
      4×u32 tail (monster_exp 放 value1；Adventure 放 value4)

    ``sub_1645DA0`` 在初始 flag 后依次读取 ``v71, v68, v61, v72,
    v63, v62, v67, v64, v66, v60, v46, v53, v50, v56, v57, v58, v45,
    dst__, v55, v47, v48``，共 21 个 ``u32``；随后才读取两个变长组
    count。少写一个会使所有后续区段前移 4B，并在尾部触发客户端
    ``C1/00D9 {00,23,00}`` overflow 报告。
    旧 61B stub 读不到 v83，结果窗不打开 → 客户端点「返回城镇」根本不发包。
    """
    body = bytearray()
    body += struct.pack("<I", int(clear_base_exp) & 0xFFFFFFFF)
    body += struct.pack("<I", int(score_bonus_exp) & 0xFFFFFFFF)
    body += struct.pack("<I", int(clear_bonus_exp) & 0xFFFFFFFF)
    body += struct.pack("<I", 0)
    body += bytes([0])  # base_flag / v82
    # Stage3 reader: v71..v48, exactly 21 u32 after the first flag byte.
    # A-group rows describe accumulated monster-kill bonuses, while B-group
    # rows describe this clear's bonuses. Rank remains authoritative in the
    # prefix (+0x04). The reader initially maps +0x25 to B[6], then overwrites
    # B[6] from the final tail u32, so the early slot must stay zero and the
    # authoritative Adventure value is emitted as value4. The separate
    # +0x29/+0x45 fields feed the native A[6] aggregate and stay zero here.
    early = [0] * 21
    # A group: Fatigue Burning +0x49, Growth +0x51, Neo +0x61.
    early[(0x49 - 0x11) // 4] = max(0, int(kill_fatigue_burning_exp))
    early[(0x51 - 0x11) // 4] = max(0, int(kill_growth_contract_exp))
    early[(0x61 - 0x11) // 4] = max(0, int(kill_neo_premium_exp))
    # B group: Potion +0x15, Neo +0x19, Growth +0x2D,
    # Fatigue Burning +0x31. Adventure is finalized in tail value4.
    early[(0x15 - 0x11) // 4] = max(0, int(potion_exp))
    early[(0x19 - 0x11) // 4] = max(0, int(neo_premium_exp))
    early[(0x2D - 0x11) // 4] = max(0, int(growth_contract_exp))
    early[(0x31 - 0x11) // 4] = max(0, int(fatigue_burning_exp))
    for value in early:
        body += struct.pack("<I", int(value) & 0xFFFFFFFF)
    body += bytes([0, 0])  # groupA / groupB empty
    body += struct.pack("<I", 0)  # post_slot v59
    # score: [0, champion, super_champion, boss] — 业务槽位参考 s4a12，长度以 Stage3 为准
    for val in (
        0,
        int(champion_exp) & 0xFFFFFFFF,
        int(super_champion_exp) & 0xFFFFFFFF,
        int(boss_exp) & 0xFFFFFFFF,
    ):
        body += struct.pack("<I", val)
    body += struct.pack("<I", 0)  # quest_count
    body += bytes([0])  # drop_count

    free_pairs: list[tuple[int, int]] = []
    gold_i = int(free_card_gold or 0)
    item_id = int(free_card_item_id or 0)
    item_cnt = int(free_card_item_count or 0)
    if gold_i > 0:
        free_pairs.append((0, gold_i))
    if item_id > 0 and item_cnt > 0:
        free_pairs.append((item_id, item_cnt))
    if len(free_pairs) > 255:
        free_pairs = free_pairs[:255]

    # card group1: free seat0 may carry gold/item pairs
    for seat in range(8):
        if seat == 0 and free_pairs:
            body += bytes([len(free_pairs)])
            for key, value in free_pairs:
                body += struct.pack(
                    "<II", int(key) & 0xFFFFFFFF, int(value) & 0xFFFFFFFF
                )
        else:
            body += bytes([0])

    # This post-card u32 is the lower-row gold commission.  It is consumed
    # before the two following card groups, so keeping it at zero makes the
    # paid-card UI display a free/empty lower row.
    body += struct.pack("<I", max(0, int(paid_card_cost)) & 0xFFFFFFFF)
    body += bytes([0] * 8)  # card group2 empty
    body += bytes([0] * 8)  # card group3 empty (count=0 时无 trailing u8)
    body += struct.pack("<I", 0)  # pretail v54
    body += struct.pack("<I", 0)  # value0 / v41
    body += bytes([int(show_result) & 0xFF, 0])  # v83 ShowResult, v81 flag0
    body += struct.pack("<I", int(monster_exp) & 0xFFFFFFFF)  # value1 / v52
    body += struct.pack("<I", 0)  # value2
    body += struct.pack("<I", 0)  # value3
    # Stage3 0x01646B13 writes this final field to B[6], replacing the early
    # +0x25 value before the native settlement aggregate runs.
    body += struct.pack("<I", max(0, int(adventure_exp)) & 0xFFFFFFFF)  # value4
    return bytes(body)


def build_clear_dungeon_reward_0023_stub(
    *,
    clear_base_exp: int = 0,
    boss_exp: int = 0,
    monster_exp: int = 0,
    gold: int = 0,
    free_item_id: int = 0,
    free_item_count: int = 0,
    paid_card_cost: int = 0,
    score_bonus_exp: int = 0,
    neo_premium_exp: int = 0,
    adventure_exp: int = 0,
    potion_exp: int = 0,
    growth_contract_exp: int = 0,
    fatigue_burning_exp: int = 0,
    kill_growth_contract_exp: int = 0,
    kill_neo_premium_exp: int = 0,
    kill_fatigue_burning_exp: int = 0,
    show_result: int = 1,
) -> bytes:
    """兼容入口：默认走 Stage3 完整体（含 ShowResult）。

    ``gold`` 映射为 free_card_gold（通关免费金币卡展示）。
    """
    return build_clear_dungeon_reward_0023_stage3(
        clear_base_exp=clear_base_exp,
        score_bonus_exp=score_bonus_exp,
        neo_premium_exp=neo_premium_exp,
        adventure_exp=adventure_exp,
        potion_exp=potion_exp,
        growth_contract_exp=growth_contract_exp,
        fatigue_burning_exp=fatigue_burning_exp,
        kill_growth_contract_exp=kill_growth_contract_exp,
        kill_neo_premium_exp=kill_neo_premium_exp,
        kill_fatigue_burning_exp=kill_fatigue_burning_exp,
        boss_exp=boss_exp,
        monster_exp=monster_exp,
        free_card_gold=gold,
        free_card_item_id=free_item_id,
        free_card_item_count=free_item_count,
        paid_card_cost=paid_card_cost,
        show_result=show_result,
    )


def permissions_ids_for_level(level: int, known_ids: Iterable[int] | None = None) -> list[int]:
    """Minimal permission filter when full PVF meta is unavailable.

    From 副本实现A.md:
      unlock_cap = max(char_level+2, 10)
      low-level ensure id 1..20
    """
    lv = max(1, int(level or 1))
    unlock_cap = max(lv + 2, 10)
    ids: list[int] = []
    if known_ids:
        for dungeon_id in known_ids:
            did = int(dungeon_id)
            if did <= 0:
                continue
            # without .dgn level meta: early ids + soft cap by id band
            if did <= 50 or did <= unlock_cap * 5:
                ids.append(did)
    # always ensure early beginner nodes
    for did in range(1, 21):
        if did not in ids:
            ids.append(did)
    ids = sorted(set(ids))
    return ids
