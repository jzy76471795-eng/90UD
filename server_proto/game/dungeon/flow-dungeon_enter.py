"""Dungeon enter C->S handlers (USDOF wire from 副本实现A.md).

Opcodes:
  0x000F ENTER_SELECT_DUNGEON  body: u32 field0 (often 0; may carry preselect id)
  0x0010 SELECT_DUNGEON        body: 17B US (not S4 5B)
  0x0308 GUIDE_ENTER_DUNGEON   body: u32 dungeon_id + u32 field1 (live red guide button)
  0x0048 optional pre-packet on UI enter path (01 01)

Special dungeon (区域特殊本，如银色村庄「遗忘之森」2009):
  - 与普通本同一 0010 grant 路径；PVF ``dungeon/Special/*.dgn`` + ``[special dungeon] 1``
  - 2026-07-21 crash: AES 32B pad 下 parse_0010 误选 reserved3+17B → id=256/no_maze/map0
  - fix: 默认 leading 17B；无 maze/map 拒绝 grant（防空 001D 闪退）

Guide button (选图 UI 右下角引导):
  - 绿色：有进行中主线 quest connection → 客户端发 000F preselect + 0010 SELECT
  - 红色：无可用主线连接 / 推荐节点时 → 客户端只发 0x0308（sub_2BB6340），
    plain = u32 dungeon_id + u32 circle_id；难度由预热的 circledungeoninfo.etc
    对该二元组的 [difficulty level] 恢复
  Live 证据（cid=3 无主线 active connection）:
    2026-07-21 10:28:39 C1/0308 plain=95 00 00 00 27 3a 00 00 → dungeon=149 field1=14887
    未处理则无 S2C，用户点击无反馈；应复用 0010 grant → 001C+001D

Phase A enter-select S2C (friend DFOUSServer / 副本实现A.md):

  S0/0x02FD LICENSE_DUNGEON_PERMISSION u16 count + (u32 dungeon_id, u32 license_level, u32 field3) [codec9]
  S0/0x02FE LICENSE_DUNGEON_CLEAR_INFO u8 schedule key (Dark Elf Ruins only) [codec10]
  S0/0x0005 DUNGEON_PERMISSION  u16 count + (u32 id + u8 state)*  [codec5]
  S0/0x0017 USER_AREA           area forced 0xFF                  [codec9]
  S0/0x0003 USER_STATE          count=1, userId, state=1          [codec3]
  S0/0x001A party host slot     body 00 = slot 0                  [codec12]
  S0/0x001B ENTER_SELECT_DUNGEON US ~29B body (NOT s4a12 19B)    [codec13]

Crash root-cause on prior trial: s4a12 19B 001B underflows US sub_1678070.
Doc: UI needs area=0xFF + 0005 permissions; 001B alone is not enough.
Default order is friend-doc 0005->0017 (+ optional post-001B 0005 refresh).

Env:
  USDOF_GAME_DUNGEON_ENTER_HANDLE=1
  USDOF_GAME_DUNGEON_ENTER_GRANT=1          # 0x0010 / 0x0308 SELECT -> 001C+001D grant
  USDOF_GAME_DUNGEON_GUIDE_0308=1          # 红色引导 0x0308 -> 复用 0010 grant
  USDOF_GAME_DUNGEON_ENTER_001D_CELL=start  # start|ff  Stage3 wire must be real start; s4a12 FF is server-internal only
  USDOF_GAME_DUNGEON_ENTER_001D_ROOM_FLAG=1 # 1|2 (enter mon path)
  USDOF_GAME_DUNGEON_ENTER_001D_LAYERED=0   # 0 normal / 1 layered
  USDOF_GAME_DUNGEON_ENTER_001D_PARTY_MEMBER=0  # solo local slot; 0xFF soft-fails
  USDOF_GAME_DUNGEON_ENTER_SEND_0009=0     # on SELECT grant: OFF (friend=001C+001D only)
  USDOF_GAME_DUNGEON_ENTER_PRELOAD_0009=1  # on ENTER_SELECT: preload solo party before click enter
  USDOF_DUNGEON_MONSTERS=0                 # 0 empty START_MAP first (stable enter); 1 load mon rows
  USDOF_GAME_DUNGEON_QUEST_MAZE=1          # 1 按任务 quest connection 选剧情迷宫（90qiyuan 同规则）；0 强制自由迷宫
  USDOF_GAME_DUNGEON_001C_MAZE_INDEX=1     # 1 001C 写真实 maze_index（u8，见 helper-dungeon_payloads）；0 强制 0
  USDOF_GAME_DUNGEON_ENTER_SEND_0117=0     # Stage3 OFF: 0x0117 is C->S LAG_STATISTICS
  USDOF_GAME_DUNGEON_ENTER_SEND_019F=0     # Stage3 OFF: 0x019F is CHARAC_SLOT_EXTEND, not enter S2C
  USDOF_GAME_DUNGEON_ENTER_SEND_0005=1
  USDOF_GAME_DUNGEON_ENTER_SEND_0017=1
  USDOF_GAME_DUNGEON_ENTER_SEND_0003=1
  USDOF_GAME_DUNGEON_ENTER_SEND_001A=1
  USDOF_GAME_DUNGEON_ENTER_SEND_001B=1
  USDOF_GAME_DUNGEON_ENTER_SEND_0002=1  # slot10 only: complete USERINFO1 after 0017
  USDOF_GAME_DUNGEON_ENTER_0002_MODE=codec
  USDOF_GAME_DUNGEON_ENTER_001A_MODE=codec
  USDOF_GAME_DUNGEON_ENTER_001B_LAYOUT=us|s4_legacy  (default us)
  USDOF_GAME_DUNGEON_ENTER_SEND_0164=1  # finished memory before select
  USDOF_GAME_DUNGEON_ENTER_SEND_02FD=1  # Dark Elf Ruins: highest PVF star per group before UI
  USDOF_GAME_DUNGEON_ENTER_SEND_02FE=1  # Dark Elf Ruins: all-open day key 0 before UI
  USDOF_DUNGEON_PERM_EMPTY=0
  USDOF_DUNGEON_PERM_MAX=256
  USDOF_DUNGEON_PERM_CLEAR_STATE=4  # legacy PVF fallback when [difficulty level] is absent
  USDOF_DUNGEON_PERM_USE_PVF_DIFFICULTY=1

Authority:
  副本实现A.md (friend same-client DFOUSServer)
  helper-dungeon_payloads.py
"""
from __future__ import annotations
from game.raid.local_bindings import local_raid_hooks

import os
import struct
import time
from typing import Any

from game.character.payloads import build_user_area
from game.character.adventure_group import is_recommended_dungeon_level
from game.chain_loader import load_chain
from game.drop.room_drop_preload import preload_room_drops_before_visibility
from game.quest.quest_clear import DungeonClearContext, persist_clear_map_progress
from game.quest.adventure_season1 import begin_run as begin_adventure_season1_run
from game.quest.weekly_attendance import begin_run as begin_weekly_attendance_run

_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
_dungeon_worldmap = load_chain("helper-dungeon_worldmap.py")
_guild_dungeon_purification = load_chain(
    "helper-guild_dungeon_purification.py"
)
_clear_conditions = load_chain("helper-dungeon_clear_conditions.py")
_circle_dungeon_info = load_chain("helper-circle_dungeon_info.py")
_license_dungeon = load_chain("helper-dungeon_license_boss.py")
resolve_circle_dungeon_difficulty = (
    _circle_dungeon_info.resolve_circle_dungeon_difficulty
)
build_dungeon_permission_0005 = _dungeon_payloads.build_dungeon_permission_0005
build_license_dungeon_clear_info_02fe = (
    _dungeon_payloads.build_license_dungeon_clear_info_02fe
)
build_license_dungeon_permission_02fd = (
    _dungeon_payloads.build_license_dungeon_permission_02fd
)
license_dungeon_permission_records = (
    _license_dungeon.license_dungeon_permission_records
)
build_guild_dungeon_repeat_bonus_info_02e6 = (
    _dungeon_payloads.build_guild_dungeon_repeat_bonus_info_02e6
)
build_enter_select_user_state = _dungeon_payloads.build_enter_select_user_state
build_udp_host_unavailable = _dungeon_payloads.build_udp_host_unavailable
build_party_host_slot = _dungeon_payloads.build_party_host_slot
build_enter_select_dungeon_001b_us = _dungeon_payloads.build_enter_select_dungeon_001b_us
build_enter_select_dungeon_001b_s4_legacy = _dungeon_payloads.build_enter_select_dungeon_001b_s4_legacy
build_dungeon_info_001c_us = _dungeon_payloads.build_dungeon_info_001c_us
build_minimap_icon_info_022f_us = _dungeon_payloads.build_minimap_icon_info_022f_us
build_start_map_001d_us_empty = _dungeon_payloads.build_start_map_001d_us_empty
build_start_map_001d_us = _dungeon_payloads.build_start_map_001d_us
_map_mons = load_chain("helper-dungeon_map_monsters.py")
load_map_monsters = _map_mons.load_map_monsters
load_map_passive_objects = _map_mons.load_map_passive_objects
inject_story_face_preloads = _map_mons.inject_story_face_preloads
_tournament = load_chain("helper-dungeon_tournament.py")
is_tournament_dungeon = _tournament.is_tournament_dungeon
initialize_tournament_run = _tournament.initialize_tournament_run
clear_tournament_session = _tournament.clear_tournament_session
build_tournament_info_0183 = _tournament.build_tournament_info_0183
build_tournament_map_info_0184 = _tournament.build_tournament_map_info_0184
_party_info = load_chain("helper-party_info.py")
build_party_info_type0_solo = _party_info.build_party_info_type0_solo
permissions_ids_for_level = _dungeon_payloads.permissions_ids_for_level
_dungeon_start_room = load_chain("helper-dungeon_start_room.py")
resolve_dungeon_start_room = _dungeon_start_room.resolve_dungeon_start_room
layered_map_ids_for_cell = _dungeon_start_room.layered_map_ids_for_cell
compute_dungeon_seed = _dungeon_start_room.compute_dungeon_seed
_ridable_objects = load_chain("helper-dungeon_ridable_objects.py")
ridable_objects_for_room = _ridable_objects.ridable_objects_for_room
from game.dungeon import energy_objects
dungeon_ids_for_town_area = _dungeon_worldmap.dungeon_ids_for_town_area
all_worldmap_dungeon_ids = _dungeon_worldmap.all_worldmap_dungeon_ids
merge_permission_ids = _dungeon_worldmap.merge_permission_ids
resolve_worldmap_id = _dungeon_worldmap.resolve_worldmap_id
worldmap_id_for_dungeon = _dungeon_worldmap.worldmap_id_for_dungeon
GUILD_DUNGEON_WORLDMAP_ID = (
    _guild_dungeon_purification.GUILD_DUNGEON_WORLDMAP_ID
)
LICENSE_DUNGEON_WORLDMAP_ID = 38
LICENSE_DUNGEON_ALL_OPEN_DAY_KEY = 0
resolve_guild_dungeon_purification_config = (
    _guild_dungeon_purification.resolve_guild_dungeon_purification_config
)
attach_destroy_object_clear_shadows = (
    _clear_conditions.attach_destroy_object_clear_shadows
)
wire_actor_count = _clear_conditions.wire_actor_count

from game.quest.quest_common import build_clear_quest_list_0164
from game.chain_loader import load_chain
_quest_unlock_helper = load_chain('helper-quest_unlock.py')
ensure_unlock_and_finished_ids = _quest_unlock_helper.ensure_unlock_and_finished_ids
_quest_maze_helper = load_chain('helper-dungeon_quest_connection.py')
resolve_dungeon_start_info_with_quests = _quest_maze_helper.resolve_dungeon_start_info_with_quests
_room_state = load_chain("helper-dungeon_room_state.py")
init_run_room_state = _room_state.init_run_room_state
cache_room_state = _room_state.cache_room_state
filter_destroyed_objects = _room_state.filter_destroyed_objects
bind_room_passive_objects = _room_state.bind_room_passive_objects
resequence_monsters = _room_state.resequence_monsters
_special_conditions = load_chain("helper-dungeon_special_conditions.py")
initialize_run_special_conditions = (
    _special_conditions.initialize_run_special_conditions
)
special_condition_minimap_pair_groups = (
    _special_conditions.special_condition_minimap_pair_groups
)
special_condition_minimap_entries = (
    _special_conditions.special_condition_minimap_entries
)
append_room_condition_actors = _special_conditions.append_room_condition_actors
bind_room_condition_passives = _special_conditions.bind_room_condition_passives
_weapon_avatar_render = load_chain(
    "helper-dungeon_weapon_avatar_render.py"
)
clear_pending_weapon_avatar_render = _weapon_avatar_render.clear_pending_render
prepare_pending_weapon_avatar_render = _weapon_avatar_render.prepare_pending_render
_preselect_weapon_avatar_userinfo1 = load_chain(
    "helper-dungeon_preselect_weapon_avatar_userinfo1.py"
)
build_preselect_weapon_avatar_userinfo1 = (
    _preselect_weapon_avatar_userinfo1.build_preselect_weapon_avatar_userinfo1
)
_actor_keys = load_chain("helper-dungeon_actor_keys.py")
initial_actor_key_base = _actor_keys.initial_actor_key_base
_story_mode = load_chain("helper-dungeon_story_mode.py")
apply_story_scales_to_monsters = _story_mode.apply_story_scales_to_monsters
bind_story_to_session = _story_mode.bind_story_to_session
is_story_run = _story_mode.is_story_run
story_info_from_dungeon = _story_mode.story_info_from_dungeon
_ally_companions = load_chain("helper-dungeon_ally_companions.py")
bind_companions_on_enter = _ally_companions.bind_companions_on_enter
companion_progress_codes = _ally_companions.companion_progress_codes
_hell_party = load_chain("helper-dungeon_hell_party.py")
resolve_hell_party_room = _hell_party.resolve_hell_party_room
bind_hell_party_to_session = _hell_party.bind_hell_party_to_session
append_hellparty_templates = _hell_party.append_hellparty_templates
_another_arad = load_chain("helper-another_arad.py")
apply_another_arad_monster_level = _another_arad.apply_another_arad_monster_level
bind_another_arad_session = _another_arad.bind_another_arad_session
clear_another_arad_session = _another_arad.clear_another_arad_session
correct_another_arad_start_room = _another_arad.correct_another_arad_start_room
matched_another_arad_monster_level = _another_arad.matched_monster_level
maximum_another_arad_difficulty = _another_arad.maximum_difficulty
validate_another_arad_selection = _another_arad.validate_another_arad_selection
_hundred_floor_progress = load_chain("helper-hundred_floor_tower_progress.py")
tower_kind_for_dungeon = _hundred_floor_progress.tower_kind_for_dungeon
correct_stale_dungeon_request = _hundred_floor_progress.correct_stale_dungeon_request
build_grief_progress_050e = _hundred_floor_progress.build_grief_progress_050e
TowerEnterFlow = load_chain("flow-tower_enter.py").TowerEnterFlow
_altar_random = load_chain("flow-altar-random.py")
AltarRandomFlow = _altar_random.AltarRandomFlow
clear_altar_session = _altar_random.clear_altar_session
_linked_chain = load_chain("helper-dungeon_linked_chain.py")
resolve_linked_dungeon_retry = _linked_chain.resolve_linked_dungeon_retry
_special_selector = load_chain("helper-special_dungeon_selector.py")
is_special_dungeon = _special_selector.is_special_dungeon
build_special_selector_sync_packets = _special_selector.build_selector_sync_packets
normalize_special_selector_highest_difficulty = (
    _special_selector.normalize_highest_difficulty
)
special_selector_state_context = _special_selector.selector_state_context
special_entry_option_is_unlocked = _special_selector.special_entry_option_is_unlocked
canonical_special_selector_progress_key = (
    _special_selector.canonical_progress_key_for_dungeon
)

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def _logical_views(raw: bytes) -> list[tuple[str, bytes]]:
    """Candidate bodies: raw, and pad-truncated only on full codec zero blocks."""
    data = bytes(raw or b"")
    views: list[tuple[str, bytes]] = [("raw", data)]
    zero16 = bytes(16)
    zero8 = bytes(8)
    for block, zpad in ((16, zero16), (8, zero8)):
        if len(data) > block and len(data) % block == 0:
            cut = data
            while len(cut) > block and cut.endswith(zpad):
                cut = cut[:-block]
            if cut != data:
                views.append(("drop_zero_blocks_%d" % block, cut))
    return views


def parse_000f_plain(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "field0": None,
        "field0_hex": None,
    }
    best = None
    for view_name, view in _logical_views(raw):
        candidates = []
        if len(view) >= 7:
            candidates.append(("%s:reserved3+u32" % view_name, view[3:7]))
        if len(view) >= 4:
            candidates.append(("%s:u32" % view_name, view[:4]))
        for layout, blob in candidates:
            field0 = int.from_bytes(blob[:4], "little")
            score = 0
            if field0 not in (0, 0xFFFFFFFF):
                score += 2
            if "reserved3" in layout and len(view) >= 7:
                score += 1
            item = (score, layout, field0)
            if best is None or item[0] > best[0]:
                best = item
    if best is not None:
        _score, layout, field0 = best
        out["layout"] = layout
        out["field0"] = field0
        out["field0_hex"] = "0x%08X" % field0
    return out


def parse_0308_plain(plain: bytes) -> dict[str, Any]:
    """Stage3 红色引导按钮 C1/0x0308 body.

    Live (2026-07-21):
      plain_prefix = 95 00 00 00 27 3a 00 00
      → u32 dungeon_id=149, u32 field1=14887

    IDA: sub_2BB6340 → net_send_gate_check_1093_1094(writer, 0x308)
      + sub_2BF05A0(u32) + sub_2BF05A0(u32)  共两 dword。
    ``etc/circledungeoninfo.etc`` 的 ``[Dungeon]`` 将两个值登记为
    ``dungeon_id, circle_id``；同组 ``[difficulty level]`` 是进本难度槽。
    """
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "dungeon_id": None,
        "circle_id": None,
        "field1": None,
        "fields_hex": None,
    }
    best = None
    for view_name, view in _logical_views(raw):
        if len(view) < 8:
            continue
        # Prefer leading 8B; allow reserved3 prefix if present.
        candidates = []
        if len(view) >= 11:
            candidates.append((1, "%s:reserved3+2xu32" % view_name, view[3:11]))
        candidates.append((2, "%s:2xu32" % view_name, view[:8]))
        for score, layout, body in candidates:
            dungeon_id = int.from_bytes(body[0:4], "little")
            field1 = int.from_bytes(body[4:8], "little")
            s = score + (2 if dungeon_id not in (0, 0xFFFFFFFF) else 0)
            item = (s, layout, dungeon_id, field1, body)
            if best is None or item[0] > best[0]:
                best = item
    if best is not None:
        _score, layout, dungeon_id, field1, body = best
        out["layout"] = layout
        out["dungeon_id"] = int(dungeon_id) & 0xFFFFFFFF
        out["circle_id"] = int(field1) & 0xFFFFFFFF
        out["field1"] = int(field1) & 0xFFFFFFFF
        out["fields_hex"] = body[:8].hex(" ")
    return out


def parse_0010_plain(plain: bytes) -> dict[str, Any]:
    """Stage3 SELECT_DUNGEON 17B layout (evidence section 13).

    Wire body (after codec):
      u32 dungeon_id | u8 difficulty | u16 entry_option |
      u8 mode2 | u8 sso |
      u16 extra16 | u32 extra32 | u8 tail0 | u8 tail1   (=17B)

    The Stage3 common writer serializes the normal difficulty and its third
    stack argument independently.  The latter is the same ``u16 entry_option``
    consumed by the S0/001C reader.  Special difficulty pages use it for their
    one-based UI choice (for example 1..3), so it must be preserved as a
    separate field rather than normalized into ``difficulty``.

    Codec AES 常把 plain 对齐到 16/32B 尾零。**禁止**因此优先 ``reserved3+17B``：
    live 2026-07-21 特殊本 2009（遗忘之森 / SealForest）:
      plain32 = d9 07 00 00 01 00 00 00 00 ff ff 00 00 00 00 00 00 + zeros
      正确 17B → dungeon_id=2009 difficulty=1
      错误 reserved3+17B → dungeon_id=256（无 .dgn）→ map_index=0 闪退
    """
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "dungeon_id": None,
        "difficulty": None,
        "entry_option": None,
        "mode2": None,
        "sso_helper": None,
        "extra16": None,
        "extra32": None,
        "tail0": None,
        "tail1": None,
        "fields_hex": None,
    }

    def _fill(layout: str, body: bytes) -> dict[str, Any]:
        return {
            "layout": layout,
            "dungeon_id": int.from_bytes(body[0:4], "little") & 0xFFFFFFFF,
            "difficulty": body[4] & 0xFF,
            "entry_option": int.from_bytes(body[5:7], "little"),
            "mode2": body[7] & 0xFF,
            "sso_helper": body[8] & 0xFF,
            "extra16": int.from_bytes(body[9:11], "little"),
            "extra32": int.from_bytes(body[11:15], "little"),
            "tail0": body[15] & 0xFF,
            "tail1": body[16] & 0xFF,
            "fields_hex": body[:17].hex(" "),
        }

    def _score_candidate(layout: str, body: bytes) -> int:
        """Prefer leading 17B; reserved3 only when leading id is junk."""
        dungeon_id = int.from_bytes(body[0:4], "little") & 0xFFFFFFFF
        difficulty = body[4] & 0xFF
        extra16 = int.from_bytes(body[9:11], "little")
        s = 0
        if dungeon_id in (0, 0xFFFFFFFF):
            s -= 20
        elif 1 <= dungeon_id <= 0x7FFF:
            # Stage3 常规 / 特殊本 id 都落在此区间（144..149, 2008..2010…）
            s += 8
        elif dungeon_id <= 0xFFFF:
            s += 4
        else:
            # 高位垃圾 id（历史 codec 错位）
            s -= 8
        # difficulty 通常 0..4；特殊本可 1
        if difficulty <= 5:
            s += 2
        else:
            s -= 1
        # live 常见 extra16 = 0 或 0xFFFF
        if extra16 in (0, 0xFFFF):
            s += 1
        if "reserved3" in layout:
            # 仅当前 3 字节像对齐残渣且 leading 无效时才该赢；默认降权
            s -= 3
        else:
            s += 4  # 默认真布局 = 头 17B
        return s

    best = None
    for view_name, view in _logical_views(raw):
        options = []
        # 先评估 leading 17B，再评估 reserved3（仅 fallback）
        if len(view) >= 17:
            options.append(("%s:17B" % view_name, view[:17]))
        if len(view) >= 20:
            # reserved3：仅当前 3 字节全 0（真 padding）时才作为候选
            if view[0:3] == b"\x00\x00\x00":
                options.append(("%s:reserved3+17B" % view_name, view[3:20]))
        for layout, body in options:
            s = _score_candidate(layout, body)
            item = (s, _fill(layout, body))
            if best is None or item[0] > best[0]:
                best = item
    if best is not None:
        out.update(best[1])
        return out

    for view_name, view in _logical_views(raw):
        if len(view) >= 8:
            body5 = view[3:8]
            layout = "%s:s4_5B_after_reserved3_contrast_only" % view_name
        elif len(view) >= 5:
            body5 = view[:5]
            layout = "%s:s4_5B_contrast_only" % view_name
        else:
            continue
        out["layout"] = layout
        out["dungeon_id"] = int.from_bytes(body5[0:2], "little")
        out["difficulty"] = body5[2] & 0xFF
        out["mode2"] = body5[3] & 0xFF
        out["sso_helper"] = body5[4] & 0xFF
        out["fields_hex"] = body5[:5].hex(" ")
        break
    return out


def parse_0048_plain(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "b0": None,
        "b1": None,
    }
    for view_name, view in _logical_views(raw):
        if len(view) >= 5:
            out["layout"] = "%s:reserved3+u8+u8" % view_name
            out["b0"] = view[3] & 0xFF
            out["b1"] = view[4] & 0xFF
            return out
        if len(view) >= 2:
            out["layout"] = "%s:u8+u8" % view_name
            out["b0"] = view[0] & 0xFF
            out["b1"] = view[1] & 0xFF
            return out
        if len(view) == 1:
            out["layout"] = "%s:u8" % view_name
            out["b0"] = view[0] & 0xFF
            return out
    return out






def build_enter_select_dungeon_001b(user_id: int) -> bytes:
    """Default US 001B body (alias). Use s4_legacy only via env layout flag."""
    layout = (os.environ.get("USDOF_GAME_DUNGEON_ENTER_001B_LAYOUT") or "us").strip().lower()
    if layout in {"s4", "s4a12", "s4_legacy", "legacy19"}:
        return build_enter_select_dungeon_001b_s4_legacy(user_id)
    return build_enter_select_dungeon_001b_us(user_id)


class DungeonEnter000F0010Service:
    """ENTER_SELECT opens dungeon UI; SELECT grant emits 001C+001D."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.tower_enter = TowerEnterFlow(server)
        self.altar_enter = AltarRandomFlow(server)

    def grant_by_dungeon_id(
        self,
        *,
        session: Any,
        dungeon_id: int,
        difficulty: int = 0,
        source: str = "force",
        another_arad_selection: Any = None,
    ) -> tuple[list[bytes], str]:
        """服务端主动进本（新角色教程 / 调试）。复用 handle_0010 完整 grant 路径。"""
        _tut = load_chain("helper-tutorial_dungeon.py")
        plain = _tut.build_select_dungeon_0010_plain(
            int(dungeon_id), int(difficulty) & 0xFF
        )
        # grant 路径要求 GRANT=1；临时打开以免调用方 env 遗漏
        prev_grant = os.environ.get("USDOF_GAME_DUNGEON_ENTER_GRANT")
        prev_handle = os.environ.get("USDOF_GAME_DUNGEON_ENTER_HANDLE")
        try:
            os.environ["USDOF_GAME_DUNGEON_ENTER_GRANT"] = "1"
            os.environ["USDOF_GAME_DUNGEON_ENTER_HANDLE"] = "1"
            packets, note = self.handle_0010(
                plain,
                session=session,
                another_arad_selection=another_arad_selection,
            )
        finally:
            if prev_grant is None:
                os.environ.pop("USDOF_GAME_DUNGEON_ENTER_GRANT", None)
            else:
                os.environ["USDOF_GAME_DUNGEON_ENTER_GRANT"] = prev_grant
            if prev_handle is None:
                os.environ.pop("USDOF_GAME_DUNGEON_ENTER_HANDLE", None)
            else:
                os.environ["USDOF_GAME_DUNGEON_ENTER_HANDLE"] = prev_handle
        if packets and another_arad_selection is None:
            _tut.bind_session_tutorial_flags(
                session,
                dungeon_id=int(dungeon_id),
                active=True,
            )
        return packets, "grant_by_id source=%s dungeon=%s; %s" % (
            source,
            int(dungeon_id) & 0xFFFFFFFF,
            note,
        )

    def grant_another_arad(
        self,
        *,
        session: Any,
        historical_dungeon_id: int,
        crack_quest_id: int,
    ) -> tuple[list[bytes], str]:
        """Validate a Stage3 random pair and enter its historical dungeon."""
        clear_another_arad_session(session)
        selection, validation = validate_another_arad_selection(
            historical_dungeon_id,
            crack_quest_id,
        )
        if selection is None:
            return [], (
                "another_arad reject=%s wrapper=%d historical=%d quest=%d"
                % (
                    validation,
                    int(_another_arad.ANOTHER_ARAD_WRAPPER_DUNGEON_ID),
                    int(historical_dungeon_id),
                    int(crack_quest_id),
                )
            )
        packets, note = self.grant_by_dungeon_id(
            session=session,
            dungeon_id=int(selection.historical_dungeon_id),
            difficulty=maximum_another_arad_difficulty(
                int(selection.historical_dungeon_id)
            ),
            source="another_arad_02b6",
            another_arad_selection=selection,
        )
        if not packets:
            clear_another_arad_session(session)
        return packets, "another_arad pair=ok; %s" % note

    def handle_0308(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        """C1/0x0308 红色引导进本：解析 dungeon_id 后复用 0010 grant。

        绿色引导走 000F+0010；红色（无主线 connection / 推荐节点）只发 0x0308。
        服务端未处理则客户端无反馈。默认 GRANT 与 0010 同门禁。
        """
        parsed = parse_0308_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_HANDLE", True)
        guide_on = _env_bool("USDOF_GAME_DUNGEON_GUIDE_0308", True)
        grant_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_GRANT", False)
        cid = self._cid(session)
        dungeon_id = parsed.get("dungeon_id")
        circle_id = parsed.get("circle_id")
        base = (
            "C1/0308 GUIDE_ENTER_DUNGEON plain_len=%s plain=%s layout=%s "
            "dungeon_id=%s circle_id=%s cid=%s handle=%s guide=%s grant=%s "
            "ida=sub_2BB6340"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["layout"],
                dungeon_id,
                circle_id,
                cid,
                int(handle_on),
                int(guide_on),
                int(grant_on),
            )
        )
        if not handle_on or not guide_on:
            return [], (
                "%s; gate=OFF no_s2c; chain_file=game/dungeon/flow-dungeon_enter.py"
                % base
            )
        if dungeon_id in (None, 0, 0xFFFFFFFF):
            # 无 dungeon_id 时尝试 session soft preselect
            pre = None
            if session is not None:
                try:
                    pre = getattr(session, "selected_dungeon_id", None)
                except Exception:
                    pre = None
            if pre not in (None, 0, 0xFFFFFFFF):
                dungeon_id = int(pre) & 0xFFFFFFFF
                base += "; dungeon_id_from_soft_preselect=0x%X" % dungeon_id
            else:
                return [], (
                    "%s; gate=ON reject dungeon_id=0 no_START_MAP; "
                    "chain_file=game/dungeon/flow-dungeon_enter.py"
                    % base
                )
        if circle_id in (None, 0, 0xFFFFFFFF):
            return [], (
                "%s; gate=ON reject circle_id=0 no_START_MAP; "
                "chain_file=game/dungeon/flow-dungeon_enter.py"
                % base
            )
        difficulty = resolve_circle_dungeon_difficulty(
            int(dungeon_id),
            int(circle_id),
        )
        if difficulty is None:
            return [], (
                "%s; gate=ON reject circle_pair_unmapped no_START_MAP; "
                "chain_file=game/dungeon/flow-dungeon_enter.py"
                % base
            )
        base += "; guide_difficulty=%d source=pvf_circledungeoninfo" % int(
            difficulty
        )
        if session is not None:
            try:
                session.selected_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
                session.guide_enter_field1 = (
                    int(circle_id) & 0xFFFFFFFF
                )
                session.guide_enter_circle_id = int(circle_id) & 0xFFFFFFFF
            except Exception:
                pass

        # 复用 0010 完整 grant（001C+001D + story maze），不走 tutorial bind
        _tut = load_chain("helper-tutorial_dungeon.py")
        plain = _tut.build_select_dungeon_0010_plain(
            int(dungeon_id),
            int(difficulty),
        )
        parties = getattr(self.server, "normal_parties", None)
        if parties is not None and hasattr(parties, "guides"):
            shared = parties.guides.start(plain, session)
            if shared is not None:
                return shared[0], "%s; shared_guide_entry; %s" % (base, shared[1])
        packets, note = self.handle_0010(plain, session=session)
        return packets, "%s; reuse_0010_grant; %s" % (base, note)

    def _cid(self, session: Any = None) -> int | None:
        if session is not None:
            cid = getattr(session, "selected_character_id", None)
            if cid is not None:
                return int(cid)
        try:
            rows = self.server._repo().list_by_account(getattr(self.server, "account_id", 1))
            if rows:
                return int(rows[0].character_id)
        except Exception:
            return None
        return None

    def _load_character(self, character_id: int | None) -> Any | None:
        if character_id is None or int(character_id) <= 0:
            return None
        try:
            repo = self.server._repo()
            account_id = int(getattr(self.server, "account_id", 1) or 1)
            for row in repo.list_by_account(account_id):
                if int(row.character_id) == int(character_id):
                    return row
            for row in repo.list_all_alive():
                if int(row.character_id) == int(character_id):
                    return row
        except Exception:
            return None
        return None

    def _account_id_for_character(
        self,
        character_id: int | None,
        character: Any | None = None,
    ) -> int | None:
        if character is not None:
            try:
                account_id = int(getattr(character, "account_id", 0) or 0)
                if account_id > 0:
                    return account_id
            except (TypeError, ValueError):
                pass
        if character_id is None or int(character_id) <= 0:
            return None
        try:
            account_id = self.server._repo().get_account_id_for_character(
                int(character_id)
            )
            if account_id is not None and int(account_id) > 0:
                return int(account_id)
        except Exception:
            pass
        return None

    def _base_permission_clear_state(self, dungeon_id: int) -> int:
        """Return the ordinary PVF-derived S0/0005 state for one map."""
        clear_state = max(
            0,
            _env_int("USDOF_DUNGEON_PERM_CLEAR_STATE", 4) & 0xFF,
        )
        if not _env_bool("USDOF_DUNGEON_PERM_USE_PVF_DIFFICULTY", True):
            return clear_state
        try:
            from game.quest.quest_index import (
                get_warmed_dungeon_permission_clear_state,
            )

            pvf_state = get_warmed_dungeon_permission_clear_state(int(dungeon_id))
        except Exception:
            pvf_state = None
        if pvf_state is None:
            return clear_state
        return max(0, min(4, int(pvf_state)))

    def _resolve_user_id(self, character_id: int | None, character: Any | None) -> int:
        env_uid = os.environ.get("USDOF_GAME_DUNGEON_ENTER_USER_ID")
        if env_uid is not None and str(env_uid).strip():
            return int(str(env_uid).strip(), 0) & 0xFFFF
        if character is not None:
            return int(getattr(character, "character_id", 0) or 0) & 0xFFFF
        if character_id is not None:
            return int(character_id) & 0xFFFF
        return 0

    def _snapshot_pos(self, session: Any, character: Any | None) -> dict[str, int]:
        town = None
        area = None
        pos_x = None
        pos_y = None
        direction = None
        state = None
        if session is not None:
            town = getattr(session, "cur_town_id", None)
            area = getattr(session, "cur_area_id", None)
            pos_x = getattr(session, "cur_pos_x", None)
            pos_y = getattr(session, "cur_pos_y", None)
            direction = getattr(session, "cur_direction", None)
            state = getattr(session, "cur_area_state", None)
        if character is not None:
            if town is None:
                town = getattr(character, "town_id", 1)
            if area is None:
                area = getattr(character, "area_id", 0)
            if pos_x is None:
                pos_x = getattr(character, "pos_x", 474)
            if pos_y is None:
                pos_y = getattr(character, "pos_y", 234)
            if direction is None:
                direction = getattr(character, "direction", 5)
            if state is None:
                state = getattr(character, "area_state", 3)
        # 0xFF = S4 select-limbo (Stage3 live never PIV'd this). Negative or
        # USDOF_GAME_DUNGEON_ENTER_KEEP_AREA=1 keeps the current town area.
        keep_area = _env_bool("USDOF_GAME_DUNGEON_ENTER_KEEP_AREA", False)
        force_raw = os.environ.get("USDOF_GAME_DUNGEON_ENTER_FORCE_AREA")
        if keep_area:
            area_id = int(area if area is not None else 0) & 0xFF
        elif force_raw is None or not str(force_raw).strip():
            area_id = 0xFF
        else:
            force_val = int(str(force_raw).strip(), 0)
            if force_val < 0 or force_val > 0xFF:
                area_id = int(area if area is not None else 0) & 0xFF
            else:
                area_id = force_val & 0xFF
        return {
            "town_id": int(town if town is not None else 1) & 0xFF,
            "area_id": area_id,
            "pos_x": int(pos_x if pos_x is not None else 474),
            "pos_y": int(pos_y if pos_y is not None else 234),
            "direction": int(direction if direction is not None else 5) & 0xFF,
            "state": int(state if state is not None else 3) & 0xFF,
        }

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str) -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            pkt = self.server._raw_response(flag, opcode, body)
            return pkt, "raw"
        pkt = self.server._codec_response(flag, opcode, body)
        return pkt, "codec"

    def _append_special_dungeon_selector_sync(
        self,
        packets: list[bytes],
        notes: list[str],
        *,
        session: Any = None,
        character_id: int | None,
        player_uid: int,
        tag: str,
    ) -> None:
        """Publish one complete account snapshot for the selected player UID."""
        try:
            cid = int(character_id or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid <= 0:
            notes.append("special_selector_sync tag=%s skip=no_character" % tag)
            return
        try:
            uid = int(player_uid)
        except (TypeError, ValueError):
            uid = 0
        if not 0 < uid <= 0xFFFF:
            notes.append("special_selector_sync tag=%s skip=invalid_player_uid" % tag)
            return

        try:
            repo = self.server._repo()
        except Exception as exc:
            notes.append(
                "special_selector_sync tag=%s skip=repo_err=%s:%s"
                % (tag, type(exc).__name__, exc)
            )
            return

        context, context_note = special_selector_state_context(
            repo,
            session,
            uid,
            character_id=cid,
        )
        if context is None:
            notes.append("special_selector_sync tag=%s uid=%d %s" % (tag, uid, context_note))
            return
        resolved_uid, selected_cid, account_id, progress = context
        try:
            state_packet, records_packet = build_special_selector_sync_packets(
                self.server,
                resolved_uid,
                progress,
            )
            packets.extend((state_packet, records_packet))
            notes.append(
                "special_selector_sync tag=%s account=%d uid=%d cid=%d keys=%d "
                "reply=[S0/0317,S0/0318]"
                % (
                    tag,
                    account_id,
                    resolved_uid,
                    selected_cid,
                    len(progress),
                )
            )
        except Exception as exc:
            notes.append(
                "special_selector_sync tag=%s uid=%d err=%s:%s"
                % (tag, resolved_uid, type(exc).__name__, exc)
            )

    def _permission_entries(
        self,
        character: Any | None,
        *,
        session: Any = None,
        town_id: int | None = None,
        area_id: int | None = None,
    ) -> list[tuple[int, int]]:
        """Build S0/0005 rows for current select worldmap.

        Stage3 live:
          SilverCrown town=1 area=2 dungeon-gate -> worldmap 21 Abnova
          nodes 144..149 (+2009). Sending only 1..32 yields empty Abnova UI.

        clear_state:
          Stage3 stores the maximum received state per dungeon.  The current
          PVF ``[difficulty level]`` table is the authoritative five-slot
          availability source, so local defaults use its highest enabled slot
          per map.  ``USDOF_DUNGEON_PERM_CLEAR_STATE`` is only the fallback
          for legacy maps which omit that PVF field.  Values above the PVF
          table (the previous 15/16 trial) can leave select UI slots empty.
        """
        if _env_bool("USDOF_DUNGEON_PERM_EMPTY", False):
            return []
        max_n = max(0, _env_int("USDOF_DUNGEON_PERM_MAX", 256))
        def _state_for_dungeon(dungeon_id: int) -> int:
            return self._base_permission_clear_state(int(dungeon_id))

        tid = town_id
        aid = area_id
        if session is not None:
            if tid is None:
                tid = getattr(session, "pre_select_town_id", None)
            if tid is None:
                tid = getattr(session, "cur_town_id", None)
            if aid is None or int(aid) == 0xFF:
                aid = getattr(session, "pre_select_area_id", None)
        if character is not None:
            if tid is None:
                tid = getattr(character, "town_id", None)
            if aid is None or int(aid) == 0xFF:
                aid = getattr(character, "area_id", None)
        tid_i = int(tid if tid is not None else 1) & 0xFF
        aid_i = int(aid if aid is not None else 0) & 0xFF
        if aid_i == 0xFF:
            aid_i = 0

        area_ids = dungeon_ids_for_town_area(tid_i, aid_i)
        if _env_bool("USDOF_DUNGEON_PERM_ALL_WORLDMAP", False):
            ids = all_worldmap_dungeon_ids()
        elif area_ids:
            # Strict: only current worldmap nodes. Do NOT mix Act1 1..20 here —
            # wrong ids pollute Abnova select UI and can leave map empty.
            ids = list(area_ids)
            if _env_bool("USDOF_DUNGEON_PERM_INCLUDE_FALLBACK_1_20", False):
                ids = merge_permission_ids(ids, range(1, 21))
        else:
            # Unknown gate: unlock every worldmap-listed dungeon so all towns work.
            ids = all_worldmap_dungeon_ids()
            if not ids:
                ids = list(range(1, 21))

        if max_n and len(ids) > max_n:
            ids = ids[:max_n]
        rows = [(int(i), _state_for_dungeon(int(i))) for i in ids]
        raid_hooks = local_raid_hooks(self.server)
        return raid_hooks.filter_permission_entries(session, rows, _state_for_dungeon) if raid_hooks is not None else rows

    def _cache_pre_select(self, session: Any, snap: dict[str, int]) -> None:
        if session is None:
            return
        if not getattr(session, "in_dungeon_select", False):
            session.pre_select_town_id = snap["town_id"]
            # real town area before forcing 0xFF
            session.pre_select_area_id = int(
                getattr(session, "cur_area_id", snap.get("area_id", 0)) or 0
            ) & 0xFF
            session.pre_select_pos_x = snap["pos_x"]
            session.pre_select_pos_y = snap["pos_y"]
            session.pre_select_direction = snap["direction"]
            session.pre_select_area_state = snap["state"]

    def _persist_dungeon_login_recovery(
        self,
        *,
        character_id: int | None,
        session: Any,
        character: Any | None,
    ) -> str:
        """Save the pre-dungeon town position before S0/001C and S0/001D.

        The session is lost on a client crash, so the pre-select cache alone
        cannot restore the player on the next TCP session.
        """
        if character_id is None or int(character_id) <= 0:
            return "dungeon_login_recovery=skip cid=none"

        from game.raid.landing import is_raid_waiting_position
        if is_raid_waiting_position(session, character_id):
            return "dungeon_login_recovery=skip anton_waiting_position"

        def pick(session_attr: str, character_attr: str, default: int) -> int:
            value = getattr(session, session_attr, None) if session is not None else None
            if value is None and character is not None:
                value = getattr(character, character_attr, None)
            return int(default if value is None else value)

        town_id = pick("pre_select_town_id", "town_id", 1) & 0xFF
        area_id = pick("pre_select_area_id", "area_id", 0) & 0xFF
        if area_id == 0xFF:
            return "dungeon_login_recovery=skip area=ff"
        pos_x = pick("pre_select_pos_x", "pos_x", 474)
        pos_y = pick("pre_select_pos_y", "pos_y", 234)
        direction = pick("pre_select_direction", "direction", 5) & 0xFF
        area_state = pick("pre_select_area_state", "area_state", 3) & 0xFF
        try:
            repo = self.server._repo()
            if not hasattr(repo, "save_dungeon_login_recovery"):
                return "dungeon_login_recovery=skip repo_unsupported"
            ok = repo.save_dungeon_login_recovery(
                int(character_id),
                town_id=town_id,
                area_id=area_id,
                pos_x=pos_x,
                pos_y=pos_y,
                direction=direction,
                area_state=area_state,
            )
            return (
                "dungeon_login_recovery=%s town=%s area=%s pos=(%s,%s)"
                % ("saved" if ok else "miss", town_id, area_id, pos_x, pos_y)
            )
        except Exception as exc:
            return "dungeon_login_recovery=save_fail err=%r" % (exc,)

    def _build_enter_select_packets(
        self,
        *,
        session: Any,
        character_id: int | None,
        character: Any | None,
    ) -> tuple[list[bytes], list[str]]:
        packets: list[bytes] = []
        notes: list[str] = []
        user_id = self._resolve_user_id(character_id, character)
        # Snapshot real position first (for pre_select cache), then force area 0xFF.
        if session is not None:
            # temporary: keep current area for cache before FORCE
            pass
        # Force area 0xFF for select UI (doc: critical signal).
        # Keep KEEP_AREA=0 by default.
        snap = self._snapshot_pos(session, character)
        # If FORCE_AREA defaulted to 0xFF already in _snapshot_pos — good.
        # Ensure area is 0xFF unless KEEP_AREA.
        if not _env_bool("USDOF_GAME_DUNGEON_ENTER_KEEP_AREA", False):
            snap["area_id"] = _env_int("USDOF_GAME_DUNGEON_ENTER_FORCE_AREA", 0xFF) & 0xFF
            if snap["area_id"] > 0xFF:
                snap["area_id"] = 0xFF

        user_state = _env_int("USDOF_GAME_DUNGEON_ENTER_USER_STATE", 0x01) & 0xFF

        if session is not None:
            if not bool(getattr(session, "in_dungeon_select", False)):
                # A new selector opening may use the same TCP session after a
                # previous leave; allow one fresh preselect snapshot.
                session.dungeon_preselect_weapon_avatar_userinfo1_sent = False
            self._cache_pre_select(session, {
                "town_id": snap["town_id"],
                "area_id": int(getattr(session, "cur_area_id", 0) or 0) & 0xFF,
                "pos_x": snap["pos_x"],
                "pos_y": snap["pos_y"],
                "direction": snap["direction"],
                "state": snap["state"],
            })
            session.user_state = user_state
            session.in_dungeon_select = True
            session.dungeon_select_active = True
            session.dungeon_select_area_id = snap["area_id"]
            session.cur_area_id = snap["area_id"]
            session.sent_dungeon_permission = bool(
                getattr(session, "sent_dungeon_permission", False)
            )

        # Resolve permission rows once; used for pre-UI 0005 and optional post-UI refresh.
        # Use pre_select town/area (real gate) not forced 0xFF.
        perm_town = (
            int(getattr(session, "pre_select_town_id", snap["town_id"]) or snap["town_id"])
            if session is not None
            else snap["town_id"]
        )
        perm_area = (
            int(getattr(session, "pre_select_area_id", 0) or 0)
            if session is not None
            else 0
        )
        entries = self._permission_entries(
            character,
            session=session,
            town_id=perm_town,
            area_id=perm_area,
        )
        select_worldmap_id = resolve_worldmap_id(perm_town, perm_area)

        def _append_0164_finished(*, tag: str = "pre_select") -> None:
            """S0/0164 finished-quest memory before select UI.

            Task-doc section 7.4:
              hydrate -> ensure unlock finished -> 0164 -> 0005 -> 001B
            """
            if not _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0164", True):
                return
            finished_ids: list[int] = []
            unlock_seeded = 0
            try:
                cid = character_id
                if cid is None and character is not None:
                    cid = getattr(character, "character_id", None)
                if cid is not None and int(cid) > 0:
                    job = int(getattr(character, "job", 0) or 0) if character is not None else 0
                    level = int(getattr(character, "level", 1) or 1) if character is not None else 1
                    grow = int(getattr(character, "grow_type", 0) or 0) if character is not None else 0
                    # The helper honors the local-unlock switch internally.
                    # Always use it here so expired daily completions are
                    # removed from the client-facing 0164 snapshot as well.
                    unlocked = ensure_unlock_and_finished_ids(
                        self.server,
                        character_id=int(cid),
                        job=job,
                        level=level,
                        grow_type=grow,
                        session=session,
                    )
                    finished_ids = [int(x) & 0xFFFF for x in (unlocked.get("finished_ids") or [])]
                    unlock_seeded = int((unlocked.get("unlock") or {}).get("seeded", 0) or 0)
                    notes.append(
                        "unlock-quests n=%s finished=%s tag=%s"
                        % (unlock_seeded, len(finished_ids), tag)
                    )
            except Exception as exc:
                notes.append("S0/0164 skip err=%r tag=%s" % (exc, tag))
                return
            body_0164 = build_clear_quest_list_0164(finished_ids)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_0164_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_0164_MODE", "codec")
            pkt, used = self._emit(flag, 0x0164, body_0164, mode)
            packets.append(pkt)
            sample = ",".join(str(x) for x in finished_ids[:8]) if finished_ids else "-"
            notes.append(
                "S%d/0164 mode=%s plain=%d wire=%d finished=%d sample=%s tag=%s unlock_seeded=%s"
                % (
                    flag,
                    used,
                    len(body_0164),
                    len(pkt),
                    len(finished_ids),
                    sample,
                    tag,
                    unlock_seeded,
                )
            )
            if session is not None:
                try:
                    session.finished_quest_ids = list(finished_ids)
                except Exception:
                    pass

        def _append_0005(rows: list[tuple[int, int]], *, tag: str) -> None:
            body_0005 = build_dungeon_permission_0005(rows)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_0005_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_0005_MODE", "codec")
            pkt, used = self._emit(flag, 0x0005, body_0005, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0005 mode=%s plain=%d wire=%d entries=%d sample=%s tag=%s"
                % (
                    flag,
                    used,
                    len(body_0005),
                    len(pkt),
                    len(rows),
                    ",".join(str(e[0]) for e in rows[:8]),
                    tag,
                )
            )
            if session is not None:
                session.sent_dungeon_permission = True

        def _append_license_dungeon_02fd(*, tag: str) -> None:
            records = tuple(license_dungeon_permission_records())
            if not records:
                notes.append(
                    "S0/02FD skip=no_warmed_license_records worldmap=%d tag=%s"
                    % (LICENSE_DUNGEON_WORLDMAP_ID, tag)
                )
                return
            body_02fd = build_license_dungeon_permission_02fd(records)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_02FD_FLAG", 0) & 0xFF
            mode = os.environ.get(
                "USDOF_GAME_DUNGEON_ENTER_02FD_MODE",
                "codec",
            )
            pkt, used = self._emit(flag, 0x02FD, body_02fd, mode)
            packets.append(pkt)
            notes.append(
                "S%d/02FD mode=%s plain=%d wire=%d entries=%d sample=%s "
                "field3_zero=1 worldmap=%d tag=%s"
                % (
                    flag,
                    used,
                    len(body_02fd),
                    len(pkt),
                    len(records),
                    ",".join(
                        "%d:%d:%d" % (dungeon_id, level, field3)
                        for dungeon_id, level, field3 in records[:8]
                    ),
                    LICENSE_DUNGEON_WORLDMAP_ID,
                    tag,
                )
            )

        def _append_license_dungeon_02fe(*, tag: str) -> None:
            body_02fe = build_license_dungeon_clear_info_02fe(
                LICENSE_DUNGEON_ALL_OPEN_DAY_KEY
            )
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_02FE_FLAG", 0) & 0xFF
            mode = os.environ.get(
                "USDOF_GAME_DUNGEON_ENTER_02FE_MODE",
                "codec",
            )
            pkt, used = self._emit(flag, 0x02FE, body_02fe, mode)
            packets.append(pkt)
            notes.append(
                "S%d/02FE mode=%s plain=%d wire=%d day_key=%d all_open=1 "
                "worldmap=%d tag=%s"
                % (
                    flag,
                    used,
                    len(body_02fe),
                    len(pkt),
                    LICENSE_DUNGEON_ALL_OPEN_DAY_KEY,
                    LICENSE_DUNGEON_WORLDMAP_ID,
                    tag,
                )
            )

        def _append_guild_dungeon_02e6(*, tag: str) -> None:
            config = resolve_guild_dungeon_purification_config()
            clear_counts = config.full_clear_counts()
            body_02e6 = build_guild_dungeon_repeat_bonus_info_02e6(
                clear_counts,
                enable=True,
                value_a=config.style_id,
                value_b=0,
                value_c=0,
            )
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_02E6_FLAG", 0) & 0xFF
            mode = os.environ.get(
                "USDOF_GAME_DUNGEON_ENTER_02E6_MODE",
                "codec",
            )
            pkt, used = self._emit(flag, 0x02E6, body_02e6, mode)
            packets.append(pkt)
            notes.append(
                "S%d/02E6 mode=%s plain=%d wire=%d enable=1 value_a=%d "
                "entries=%d sample=%s tag=%s"
                % (
                    flag,
                    used,
                    len(body_02e6),
                    len(pkt),
                    int(config.style_id),
                    len(clear_counts),
                    ",".join(
                        "%d:%d" % (dungeon_id, clear_count)
                        for dungeon_id, clear_count in clear_counts[:8]
                    ),
                    tag,
                )
            )

        # 0) CLEAR_QUEST 0164 finished memory (任务系统思路.md 7.4: before 0005/001B)
        _append_0164_finished(tag="pre_select")

        # Dark Elf Ruins has local star and daily-open gates independent of
        # 0005.  Publish the warmed highest tier before selecting PVF day 0.
        if (
            select_worldmap_id == LICENSE_DUNGEON_WORLDMAP_ID
            and _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_02FD", True)
        ):
            _append_license_dungeon_02fd(tag="pre_ui")

        if (
            select_worldmap_id == LICENSE_DUNGEON_WORLDMAP_ID
            and _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_02FE", True)
        ):
            _append_license_dungeon_02fe(tag="pre_ui")

        # GuildDungeon.wdm evaluates its purification table while painting.
        if (
            select_worldmap_id == GUILD_DUNGEON_WORLDMAP_ID
            and _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_02E6", True)
        ):
            _append_guild_dungeon_02e6(tag="pre_ui")

        # 1) DUNGEON_PERMISSION 0005 — unlock nodes (doc: empty map without this)
        # Friend doc order places this BEFORE 0017 so store is warm when UI paints.
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0005", True):
            _append_0005(entries, tag="pre_ui")

        # 1) USER_AREA area=0xFF — primary UI open signal
        send_user_area = _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0017", True)
        send_user_state = _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0003", True)
        if send_user_area:
            body_0017 = build_user_area(
                character_id=user_id,
                town_id=snap["town_id"],
                area_id=snap["area_id"],
                pos_x=snap["pos_x"] & 0xFFFF,
                pos_y=snap["pos_y"] & 0xFFFF,
                direction=snap["direction"],
                state=snap["state"],
            )
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_0017_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_0017_MODE", "codec")
            pkt, used = self._emit(flag, 0x0017, body_0017, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0017 mode=%s plain=%d wire=%d area=0x%02X"
                % (flag, used, len(body_0017), len(pkt), snap["area_id"])
            )

        # Stage3 already has the town primary object at this point.  When the
        # live worn set contains slot 10, install one complete mode1 snapshot
        # before USER_STATE changes the selector context.  Do not send a
        # partial/empty USERINFO1: a construction error is a hard skip.
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0002", False):
            if not send_user_area or not send_user_state:
                notes.append(
                    "S0/0002 preselect_skip=requires_0017_and_0003"
                )
            elif bool(
                getattr(session, "dungeon_preselect_weapon_avatar_userinfo1_sent", False)
            ):
                notes.append("S0/0002 preselect_skip=already_sent")
            else:
                body_0002, userinfo1_note = build_preselect_weapon_avatar_userinfo1(
                    self.server,
                    session=session,
                    character=character,
                )
                notes.append(userinfo1_note)
                if body_0002 is not None:
                    flag = _env_int("USDOF_GAME_DUNGEON_ENTER_0002_FLAG", 0) & 0xFF
                    mode = os.environ.get(
                        "USDOF_GAME_DUNGEON_ENTER_0002_MODE",
                        "codec",
                    )
                    pkt, used = self._emit(flag, 0x0002, body_0002, mode)
                    packets.append(pkt)
                    if session is not None:
                        session.dungeon_preselect_weapon_avatar_userinfo1_sent = True
                    notes.append(
                        "S0/0002/%s subtype1_preselect len=%d"
                        % (used, len(body_0002))
                    )

        # 2) USER_STATE
        if send_user_state:
            body_0003 = build_enter_select_user_state(user_id, user_state)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_0003_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_0003_MODE", "codec")
            pkt, used = self._emit(flag, 0x0003, body_0003, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0003 mode=%s plain=%d wire=%d uid=0x%04X state=0x%02X"
                % (flag, used, len(body_0003), len(pkt), user_id, user_state)
            )

        # 3) UDP host unavailable
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_001A", True):
            # Preserve the existing solo slot; 00 is not a UDP-disable flag.
            # Native multiplayer requires its actual party/endpoint producer.
            body_001a = build_party_host_slot(0)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_001A_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_001A_MODE", "codec")
            pkt, used = self._emit(flag, 0x001A, body_001a, mode)
            packets.append(pkt)
            notes.append(
                "S%d/001A mode=%s plain=%d wire=%d"
                % (flag, used, len(body_001a), len(pkt))
            )

        # Stage3 S0/050E stores the account's cleared Grief Tower floor before
        # 001B creates the select UI. Restrict it to gates whose permission set
        # actually contains the Grief Tower entry.
        if any(
            tower_kind_for_dungeon(int(dungeon_id)) == "grief"
            for dungeon_id, _state in entries
        ):
            try:
                repo = self.server._repo()
                account_id = int(getattr(character, "account_id", 0) or 0)
                if account_id <= 0 and character_id is not None:
                    account_id = int(
                        repo.get_account_id_for_character(int(character_id)) or 0
                    )
                progress = repo.get_hundred_floor_tower_progress(
                    account_id,
                    "grief",
                )
                body_050e = build_grief_progress_050e(progress)
                pkt, used = self._emit(0, 0x050E, body_050e, "codec")
                packets.append(pkt)
                notes.append(
                    "S0/050E mode=%s plain=%d wire=%d grief_cleared_floor=%d tag=pre_ui"
                    % (used, len(body_050e), len(pkt), progress)
                )
            except Exception as exc:
                notes.append("S0/050E skip grief_progress err=%r" % (exc,))

        # 4) ENTER_SELECT US long body
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_001B", True):
            body_001b = build_enter_select_dungeon_001b(user_id)
            flag = _env_int("USDOF_GAME_DUNGEON_ENTER_001B_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_ENTER_001B_MODE", "codec")
            pkt, used = self._emit(flag, 0x001B, body_001b, mode)
            packets.append(pkt)
            notes.append(
                "S%d/001B mode=%s plain=%d wire=%d uid=0x%04X layout=%s"
                % (
                    flag,
                    used,
                    len(body_001b),
                    len(pkt),
                    user_id,
                    (os.environ.get("USDOF_GAME_DUNGEON_ENTER_001B_LAYOUT") or "us"),
                )
            )

        # The selector itself needs no temporary party. Publish the solo
        # actor roster only after successful admission, before START_MAP.
        # Cancelling solo map selection then needs no PARTY_INFO type3 clear
        # and cannot announce that a real party was dissolved.

        # Optional s4-like order: 0017 first, then 0005.
        # Default OFF: friend 副本实现A.md / US live empty-node triage prefer
        # 0005 BEFORE 0017 so permission store is filled before select UI paint.
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_S4_ORDER", False) and packets:
            def _op(pkt: bytes) -> int:
                try:
                    return int.from_bytes(pkt[1:3], "little")
                except Exception:
                    return 0xFFFF
            rank = {
                0x0164: 0,
                0x02FD: 1,
                0x02FE: 2,
                0x02E6: 3,
                0x0005: 4,
                0x0017: 5,
                0x0002: 6,
                0x0003: 7,
                0x001A: 8,
                0x050E: 9,
                0x001B: 10,
                0x0009: 11,
            }
            paired = list(zip(packets, notes))
            paired.sort(key=lambda item: rank.get(_op(item[0]), 50))
            packets = [p for p, _n in paired]
            notes = [n for _p, n in paired]
            notes.append("order=s4_with_0164_0017_then_0005")
        else:
            notes.append("order=doc_0164_0005_then_0017")

        # Post-UI permission refresh is only for the ordinary map selector.
        # Dedicated special selectors receive their own 0317/0318 records.
        if (
            _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0005", True)
            and _env_bool("USDOF_GAME_DUNGEON_ENTER_0005_POST_UI", True)
            and entries
        ):
            base_state = max(1, _env_int("USDOF_DUNGEON_PERM_CLEAR_STATE", 1) & 0xFF)
            boost_raw = _env_int("USDOF_DUNGEON_PERM_POST_UI_CLEAR_STATE", 0) & 0xFF
            if boost_raw > 0:
                boost = max(1, boost_raw)
            else:
                boost = min(0xFF, base_state + 1 if base_state < 0xFF else 0xFF)
            if boost <= base_state and base_state < 0xFF:
                boost = base_state + 1
            refresh_rows = [
                (dungeon_id, boost)
                for dungeon_id, _state in entries
                if not is_special_dungeon(dungeon_id)
            ]
            if refresh_rows:
                _append_0005(refresh_rows, tag="post_ui")
            special_rows = len(entries) - len(refresh_rows)
            notes.append(
                "0005_post_ui_state=%d special_rows_omitted=%d"
                % (boost, special_rows)
            )

        # The client owns the special selector separately from S0/0005. Its
        # 0317/0318 readers copy one player-owned account snapshot after 001B;
        # the snapshot itself contains every supported concrete dungeon ID.
        has_special_selector = any(
            is_special_dungeon(dungeon_id) for dungeon_id, _state in entries
        )
        if session is not None:
            try:
                selected_dungeon_id = int(
                    getattr(session, "selected_dungeon_id", 0) or 0
                )
            except (TypeError, ValueError):
                selected_dungeon_id = 0
            if is_special_dungeon(selected_dungeon_id):
                has_special_selector = True
        if has_special_selector:
            self._append_special_dungeon_selector_sync(
                packets,
                notes,
                session=session,
                character_id=character_id,
                player_uid=user_id,
                tag="post_ui",
            )

        return packets, notes

    def handle_0048(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        parsed = parse_0048_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_HANDLE", True)
        grant_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_GRANT", False)
        cid = self._cid(session)
        note = (
            "C1/0048 ENTER_PRE plain_len=%s plain=%s layout=%s b0=%s b1=%s "
            "cid=%s handle=%s grant=%s; gate=%s no_s2c pre_only "
            "chain_file=game/chains/flow-dungeon_enter.py"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["layout"],
                parsed["b0"],
                parsed["b1"],
                cid,
                int(handle_on),
                int(grant_on),
                "ON" if handle_on else "OFF",
            )
        )
        return [], note

    def handle_000f(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        parsed = parse_000f_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_HANDLE", True)
        grant_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_GRANT", False)
        cid = self._cid(session)
        character = self._load_character(cid)
        field0 = parsed.get("field0")
        base = (
            "C1/000F ENTER_SELECT_DUNGEON plain_len=%s plain=%s layout=%s "
            "field0=%s field0_hex=%s cid=%s handle=%s grant=%s "
            "stage3_body=u32 US_doc=副本实现A"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["layout"],
                field0,
                parsed["field0_hex"],
                cid,
                int(handle_on),
                int(grant_on),
            )
        )
        if not handle_on:
            note = (
                "%s; gate=OFF no_s2c; chain_file=game/chains/flow-dungeon_enter.py"
                % base
            )
            return [], note

        # Soft preselect: already in select UI and non-zero id -> cache only.
        if (
            session is not None
            and getattr(session, "in_dungeon_select", False)
            and field0 not in (None, 0, 0xFFFFFFFF)
        ):
            raid_hooks = local_raid_hooks(self.server)
            if raid_hooks is not None:
                raid_hooks.guard_preselect(session, int(field0))
            session.selected_dungeon_id = int(field0) & 0xFFFFFFFF
            note = (
                "%s; gate=ON soft_preselect id=0x%X no_resend_ui; "
                "chain_file=game/chains/flow-dungeon_enter.py"
                % (base, int(field0) & 0xFFFFFFFF)
            )
            return [], note

        packets, reply_notes = self._build_enter_select_packets(
            session=session,
            character_id=cid,
            character=character,
        )
        reply = ", ".join(reply_notes) if reply_notes else "none"
        note = (
            "%s; gate=ON enter_select_ui reply=[%s]; "
            "doc_ref=任务系统思路0164+副本实现A 0005+0017(area FF)+0003+001A+001B(us); "
            "chain_file=game/chains/flow-dungeon_enter.py"
            % (base, reply)
        )
        if grant_on:
            note += "; GRANT_ON select_via_0010"
        return packets, note

    def handle_0010(
        self,
        body: bytes,
        *,
        session: Any = None,
        another_arad_selection: Any = None,
    ) -> tuple[list[bytes], str]:
        if another_arad_selection is None:
            clear_another_arad_session(session)
        parsed = parse_0010_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_HANDLE", True)
        grant_on = _env_bool("USDOF_GAME_DUNGEON_ENTER_GRANT", False)
        cid = self._cid(session)
        if cid is not None and int(cid) > 0:
            try:
                from game.quest.default_unlocks import apply_default_unlocks

                apply_default_unlocks(self.server._repo(), int(cid))
            except Exception:
                pass
        character = self._load_character(cid)
        character_level = int(
            getattr(character, "level", 0)
            or getattr(session, "character_level", 0)
            or 1
        )
        another_arad_level = (
            matched_another_arad_monster_level(character_level)
            if another_arad_selection is not None
            else 0
        )
        dungeon_id = parsed.get("dungeon_id")
        difficulty = int(parsed.get("difficulty") or 0) & 0xFF
        entry_option = int(parsed.get("entry_option") or 0) & 0xFFFF
        base = (
            "C1/0010 SELECT_DUNGEON plain_len=%s plain=%s layout=%s "
            "dungeon_id=%s difficulty=%s entry_option=%s mode2=%s sso=%s extra16=%s extra32=%s "
            "fields=%s cid=%s handle=%s grant=%s stage3_body=17B FORBID_s4_5B_grant"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["layout"],
                dungeon_id,
                difficulty,
                entry_option,
                parsed["mode2"],
                parsed["sso_helper"],
                parsed["extra16"],
                parsed["extra32"],
                parsed["fields_hex"],
                cid,
                int(handle_on),
                int(grant_on),
            )
        )
        if not handle_on:
            return [], (
                "%s; gate=OFF no_s2c; chain_file=game/chains/flow-dungeon_enter.py"
                % base
            )
        if not grant_on:
            return [], (
                "%s; gate=ON no_s2c SELECT_grant_deferred; "
                "chain_file=game/chains/flow-dungeon_enter.py"
                % base
            )
        # Prefer soft-preselect from C1/000F when 0010 plain was not decoded yet
        # (historically codec2 inbound missing -> garbage dungeon_id and map_index=0).
        raid_hooks = local_raid_hooks(self.server)
        preselect = None
        if session is not None:
            try:
                if raid_hooks is not None:
                    preselect = raid_hooks.native_entry_target(session)
                if preselect is None:
                    preselect = getattr(session, "selected_dungeon_id", None)
            except Exception:
                preselect = None
        parsed_id = None if dungeon_id is None else int(dungeon_id) & 0xFFFFFFFF
        if preselect not in (None, 0, 0xFFFFFFFF):
            pre_id = int(preselect) & 0xFFFFFFFF
            if parsed_id in (None, 0, 0xFFFFFFFF) or parsed_id > 0xFFFF or (
                pre_id <= 0xFFFF and parsed_id != pre_id and parsed_id > 0x1000
            ):
                dungeon_id = pre_id
                base += "; dungeon_id_from_soft_preselect=0x%X" % pre_id
        dungeon_id, linked_retry_note = resolve_linked_dungeon_retry(
            session,
            int(dungeon_id or 0),
            difficulty,
        )
        if linked_retry_note:
            base += "; %s" % linked_retry_note
        if dungeon_id in (None, 0, 0xFFFFFFFF):
            return [], (
                "%s; gate=ON grant=ON reject dungeon_id=0 no_START_MAP; "
                "chain_file=game/chains/flow-dungeon_enter.py"
                % base
            )
        if raid_hooks is not None:
            raid_hooks.guard_entry(session, int(dungeon_id))
        special_progress_key = 0
        special_highest_difficulty = 0
        special_account_id = None
        if is_special_dungeon(int(dungeon_id)):
            special_account_id = self._account_id_for_character(cid, character)
            if special_account_id is None:
                return [], (
                    "%s; gate=ON grant=ON reject special_difficulty_account_missing "
                    "dungeon=%d; chain_file=game/chains/flow-dungeon_enter.py"
                    % (base, int(dungeon_id))
                )
            canonical_key = canonical_special_selector_progress_key(int(dungeon_id))
            if canonical_key is None:
                return [], (
                    "%s; gate=ON grant=ON reject special_progress_key_missing "
                    "dungeon=%d; chain_file=game/chains/flow-dungeon_enter.py"
                    % (base, int(dungeon_id))
                )
            special_progress_key = int(canonical_key)
            try:
                special_highest_difficulty = (
                    self.server._repo().get_account_special_dungeon_highest_unlocked(
                        int(special_account_id),
                        special_progress_key,
                    )
                )
            except Exception as exc:
                return [], (
                    "%s; gate=ON grant=ON reject special_selector_state_err=%s:%s "
                    "dungeon=%d progress_key=%d; chain_file=game/chains/flow-dungeon_enter.py"
                    % (
                        base,
                        type(exc).__name__,
                        exc,
                        int(dungeon_id),
                        special_progress_key,
                    )
                )
            special_highest_difficulty = normalize_special_selector_highest_difficulty(
                special_highest_difficulty
            )
            if not special_entry_option_is_unlocked(
                entry_option,
                special_highest_difficulty,
            ):
                return [], (
                    "%s; gate=ON grant=ON reject special_entry_option_locked "
                    "dungeon=%d progress_key=%d selected=%d highest=%d; "
                    "chain_file=game/chains/flow-dungeon_enter.py"
                    % (
                        base,
                        int(dungeon_id),
                        special_progress_key,
                        entry_option,
                        special_highest_difficulty,
                    )
                )
            base += (
                "; special_selector account=%d progress_key=%d selected=%d highest=%d"
                % (
                    special_account_id,
                    special_progress_key,
                    entry_option,
                    special_highest_difficulty,
                )
            )
        if tower_kind_for_dungeon(int(dungeon_id)) == "grief" and cid is not None:
            try:
                repo = self.server._repo()
                account_id = repo.get_account_id_for_character(int(cid))
                if account_id is not None:
                    progress = repo.get_hundred_floor_tower_progress(
                        int(account_id),
                        "grief",
                    )
                    corrected_id = correct_stale_dungeon_request(
                        "grief",
                        int(dungeon_id),
                        progress,
                    )
                    if corrected_id != int(dungeon_id):
                        base += (
                            "; grief_progress_redirect requested=%d cleared_floor=%d next=%d"
                            % (int(dungeon_id), progress, corrected_id)
                        )
                        dungeon_id = corrected_id
            except Exception as exc:
                base += "; grief_progress_redirect_skip err=%r" % (exc,)
        if tower_kind_for_dungeon(int(dungeon_id)) == "despair" and cid is not None:
            try:
                repo = self.server._repo()
                account_id = repo.get_account_id_for_character(int(cid))
                if account_id is not None:
                    progress = repo.get_hundred_floor_tower_progress(
                        int(account_id),
                        "despair",
                    )
                    corrected_id = correct_stale_dungeon_request(
                        "despair",
                        int(dungeon_id),
                        progress,
                    )
                    if corrected_id != int(dungeon_id):
                        base += (
                            "; despair_progress_redirect requested=%d cleared_floor=%d next=%d"
                            % (int(dungeon_id), progress, corrected_id)
                        )
                        dungeon_id = corrected_id
            except Exception as exc:
                base += "; despair_progress_redirect_skip err=%r" % (exc,)
        # 垃圾 id 保护：历史 reserved3 错位会得到 >0xFFFF 或无 .dgn 的 id；
        # 特殊本（2008/2009/2010…）合法但若解析错成 256 则 map=0 闪退。
        try:
            from game.quest.quest_index import get_quest_index as _get_qi

            _dgn_probe = _get_qi().get_dungeon(int(dungeon_id))
            _dgn_path_ok = bool(getattr(_dgn_probe, "path", "") or "")
            _dgn_maze_ok = bool(getattr(_dgn_probe, "mazes", None) or [])
        except Exception:
            _dgn_path_ok = False
            _dgn_maze_ok = False
        if (int(dungeon_id) & 0xFFFFFFFF) > 0xFFFF or not _dgn_path_ok:
            return [], (
                "%s; gate=ON grant=ON reject dungeon_id=0x%X no_pvf_maze "
                "(special/normal dungeon must resolve .dgn+maze; "
                "check parse_0010 17B not reserved3-shift); "
                "chain_file=game/chains/flow-dungeon_enter.py"
                % (base, int(dungeon_id) & 0xFFFFFFFF)
            )

        if session is not None:
            # A run owns at most one post-actor 0321 render commit. Clear the
            # previous run before tower/altar dispatch; ordinary entry prepares
            # the new snapshot after its final MAP is bound below.
            clear_pending_weapon_avatar_render(session)

        tower_handled, tower_packets, tower_note = (
            self.tower_enter.try_enter_death_tower(
                dungeon_id=int(dungeon_id),
                difficulty=difficulty,
                character_id=int(cid or 0),
                session=session,
            )
        )
        if tower_handled:
            clear_altar_session(session)
            recovery_note = self._persist_dungeon_login_recovery(
                character_id=cid,
                session=session,
                character=character,
            )
            return tower_packets, "%s; %s; %s" % (
                base,
                tower_note,
                recovery_note,
            )

        altar_handled, altar_packets, altar_note = self.altar_enter.try_enter_altar(
            dungeon_id=int(dungeon_id),
            difficulty=difficulty,
            character_id=int(cid or 0),
            session=session,
        )
        if altar_handled:
            recovery_note = self._persist_dungeon_login_recovery(
                character_id=cid,
                session=session,
                character=character,
            )
            return altar_packets, "%s; %s; %s" % (
                base,
                altar_note,
                recovery_note,
            )
        clear_altar_session(session)

        if not _dgn_maze_ok:
            return [], (
                "%s; gate=ON grant=ON reject dungeon_id=0x%X no_pvf_maze "
                "(non-tower dungeon must resolve .dgn+maze); "
                "chain_file=game/dungeon/flow-dungeon_enter.py"
                % (base, int(dungeon_id) & 0xFFFFFFFF)
            )

        # Prefer DB truth for maze quest_connection (session may only have finished from 0164).
        active_ids: list[int] = []
        active_pairs: list[tuple[int, int]] = []
        finished_ids: list[int] = []
        if cid is not None:
            try:
                repo = self.server._repo()
                active_pairs = [
                    (int(qid) & 0xFFFF, int(trigger) & 0xFFFFFFFF)
                    for _slot, qid, trigger in repo.list_active_quests_by_accept_time(
                        int(cid)
                    )
                    if int(qid) > 0
                ]
                active_ids = [quest_id for quest_id, _trigger in active_pairs]
                finished_ids = [
                    int(qid) & 0xFFFF
                    for qid, _flag in repo.list_finished_quests(int(cid))
                ]
            except Exception:
                active_ids = []
                active_pairs = []
                finished_ids = []
        if not active_pairs and session is not None:
            for raw_pair in list(getattr(session, "active_quest_pairs", None) or []):
                try:
                    quest_id = int(raw_pair[0]) & 0xFFFF
                    trigger = int(raw_pair[1]) & 0xFFFFFFFF
                except (IndexError, TypeError, ValueError):
                    continue
                if quest_id > 0:
                    active_pairs.append((quest_id, trigger))
        if not active_ids and active_pairs:
            active_ids = [quest_id for quest_id, _trigger in active_pairs]
        if not active_ids and session is not None:
            for qid in list(getattr(session, "active_quest_ids", None) or []):
                try:
                    active_ids.append(int(qid) & 0xFFFF)
                except Exception:
                    pass
        if not finished_ids and session is not None:
            for qid in list(getattr(session, "finished_quest_ids", None) or []):
                try:
                    finished_ids.append(int(qid) & 0xFFFF)
                except Exception:
                    pass
        if session is not None:
            try:
                session.active_quest_ids = list(active_ids)
                session.active_quest_pairs = list(active_pairs)
                session.finished_quest_ids = list(finished_ids)
            except Exception:
                pass

        # Maze select（对照 90qiyuan selectDungeonMaze / df_game GetAppropriateMaze）:
        #   1) active quest connection (type=0) 优先 → 剧情迷宫
        #   2) finished quest connection (type=1)
        #   3) 无 connection 的自由迷宫
        #   4) fallback first maze
        # env QUEST_MAZE=0 强制忽略任务集合 → 永远自由迷宫（仅 A/B）。
        # 例: dungeon 144 + active 1776 → maze1 start(0,0) map 55612；无任务 → maze0 (0,3)/8153。
        quest_maze_on = _env_bool("USDOF_GAME_DUNGEON_QUEST_MAZE", True)
        if another_arad_selection is not None:
            # Crack mode replays the historical free maze. The character's
            # unrelated live quest state must not redirect it into a story
            # maze whose boss is supplied by that quest's local sequence.
            maze_active = []
            maze_finished = []
        else:
            maze_active = active_ids if quest_maze_on else []
            maze_finished = finished_ids if quest_maze_on else []
        boss_variant_pairs = [] if another_arad_selection is not None else active_pairs
        start = resolve_dungeon_start_room(
            int(dungeon_id),
            difficulty=difficulty,
            active_quest_ids=maze_active,
            finished_quest_ids=maze_finished,
            active_quest_pairs=boss_variant_pairs,
        )
        another_arad_endpoint_note = ""
        if another_arad_selection is not None:
            start, another_arad_endpoint_note = correct_another_arad_start_room(
                another_arad_selection,
                start,
            )
        seed = compute_dungeon_seed(int(dungeon_id), int(cid or 0))
        raid_plan = (raid_hooks.prepare_entry_plan(session, start, seed, difficulty, entry_option)
                     if raid_hooks is not None else None)
        ordinary_plan = False
        if raid_plan is None:
            from game.normal_party_run import entry_plan_for
            raid_plan = entry_plan_for(self.server, session, int(dungeon_id), start,
                                       seed, difficulty, entry_option)
            ordinary_plan = raid_plan is not None
        if raid_plan is not None:
            start, seed = raid_plan.start, raid_plan.seed
        map_index = int(start.get("map_index") or 0)
        maze_reason = str(start.get("reason") or "")
        is_tournament_run = bool(start.get("is_tournament_dungeon"))
        if not is_tournament_run:
            try:
                is_tournament_run = bool(is_tournament_dungeon(int(dungeon_id)))
            except Exception:
                is_tournament_run = False
        quest_connected = maze_reason in {
            "active_connection",
            "finished_connection",
            "active_connection_mirror",
            "finished_connection_mirror",
        }
        # map_index=0 / no_maze → 001D 空图，客户端加载闪退（特殊本错 id 的典型症状）
        if map_index <= 0 or maze_reason == "no_maze":
            return [], (
                "%s; gate=ON grant=ON reject no_start_map dungeon_id=%s "
                "map_index=%s reason=%s path=%s; "
                "chain_file=game/chains/flow-dungeon_enter.py"
                % (
                    base,
                    int(dungeon_id) & 0xFFFFFFFF,
                    map_index,
                    maze_reason,
                    start.get("path") or "",
                )
            )
        # Tournament MAPs own their one-cell arena and cannot be overlaid with
        # a random Hell Party room.
        hell_requested = (
            int(parsed.get("mode2") or 0) != 0 and not is_tournament_run
        )
        hell_party_mode = int(parsed.get("sso_helper") or 0)
        if hell_party_mode not in (1, 2):
            hell_party_mode = 1
        hell_room: dict[str, Any] = {
            "found": False,
            "reason": "not_requested",
        }
        if hell_requested:
            hell_room = resolve_hell_party_room(
                int(dungeon_id),
                difficulty=difficulty,
                maze_index=int(start.get("maze_index") or 0),
                hell_party_mode=hell_party_mode,
            )
        bind_hell_party_to_session(session, hell_room)
        hell_enabled = bool(hell_room.get("found"))
        # 001C maze_index = 真实迷宫下标（Stage3/90qiyuan：EntryOption u16 + MazeIndex u8）。
        # 历史错误把 maze 写成 u16 → 客户端 EntryOption=maze、MazeIndex=0 → 永远自由拓扑。
        # 关 001C_MAZE_INDEX 时强制 0，仅作 wire A/B；剧情进本必须 ON。
        mode_flag = int(start.get("maze_index") or 0) & 0xFF
        if not _env_bool("USDOF_GAME_DUNGEON_001C_MAZE_INDEX", True):
            mode_flag = 0
        special_condition_note = "special_conditions=skip_no_session"
        condition_actor_note = "condition_actors=skip_no_session"
        condition_passive_note = "condition_passives=skip_no_session"
        condition_pair_groups: tuple[tuple[tuple[int, int], ...], ...] = ()
        condition_minimap_entries: tuple[tuple[int, int, int], ...] = ()
        if not is_tournament_run:
            clear_tournament_session(session)
        if session is not None:
            # The assigned rooms are part of 001C, so the run must be initialized
            # before building that packet rather than when the start room is loaded.
            init_run_room_state(session)
            if is_tournament_run:
                special_condition_note = "special_conditions=skip_tournament"
            else:
                special_condition_note = initialize_run_special_conditions(
                    session,
                    dungeon_id=int(dungeon_id),
                    maze_index=int(start.get("maze_index") or 0),
                    seed=seed,
                )
                condition_pair_groups = special_condition_minimap_pair_groups(session)
                condition_minimap_entries = special_condition_minimap_entries(session)
        energy_groups = energy_objects.initialize(
            session, dungeon_id, int(start.get("maze_index") or 0), seed=seed)
        if energy_groups:
            if condition_pair_groups:
                raise ValueError("Energy minimap groups conflict with special conditions")
            condition_pair_groups = energy_groups
        body_001c = build_dungeon_info_001c_us(
            int(dungeon_id),
            difficulty,
            boss_x=int(start.get("boss_x") or 0),
            boss_y=int(start.get("boss_y") or 0),
            maze_index=mode_flag,
            entry_option=entry_option,
            hell_x=int(hell_room.get("x") if hell_enabled else 0xFF),
            hell_y=int(hell_room.get("y") if hell_enabled else 0xFF),
            hell_party_enabled=1 if hell_enabled else 0,
            pair_groups=condition_pair_groups,
            value2=0x0B if hell_enabled else 0,
            flag_a=1 if condition_pair_groups else 0,
        )
        # 001D cell A/B (Stage3 sub_16411A0):
        # - start: wire real start cell from .dgn / maze (maze1 -> 0,0 for 144)
        # - ff:    experimental only. s4a12 SendStartMapAsync(0xFF,0xFF) resolves
        #          server-side BEFORE building the packet; wire body uses real x/y.
        # Stage3 uses wire cell for room-table lookup (sub_1B96540 / sub_1BA03E0).
        # cell=0xFF leaves room entry null and soft-fails before C1/0025
        # (historical null write at 0x16415C7 when entry is missing).
        cell_mode = (os.environ.get("USDOF_GAME_DUNGEON_ENTER_001D_CELL") or "start").strip().lower()
        if cell_mode in {"ff", "0xff", "first", "s4a12_ff"}:
            cell_x = 0xFF
            cell_y = 0xFF
            cell_tag = "ff"
        else:
            # default / start / real / resolved / xy
            cell_x = int(start.get("start_x") or 0) & 0xFF
            cell_y = int(start.get("start_y") or 0) & 0xFF
            cell_tag = "start"
        room_flag = max(1, min(2, _env_int("USDOF_GAME_DUNGEON_ENTER_001D_ROOM_FLAG", 1))) & 0xFF
        layered = _env_int("USDOF_GAME_DUNGEON_ENTER_001D_LAYERED", 0) & 0xFF
        party_member = _env_int("USDOF_GAME_DUNGEON_ENTER_001D_PARTY_MEMBER", 0) & 0xFF
        from game.normal_party_run import map_actor_index_for
        party_member = map_actor_index_for(self.server, session, party_member)
        normal_room_map_index = map_index
        is_hell_room = bool(
            hell_enabled
            and int(hell_room.get("x", -1)) == int(cell_x)
            and int(hell_room.get("y", -1)) == int(cell_y)
        )
        monster_map_index = normal_room_map_index
        if is_hell_room:
            map_index = int(hell_room.get("map_index") or map_index)
            monster_map_index = int(
                hell_room.get("normal_map_index") or normal_room_map_index
            )
            layered = 1
        start_layered_map_ids: tuple[int, ...] = ()
        if not is_hell_room:
            start_layered_map_ids = layered_map_ids_for_cell(
                int(dungeon_id),
                int(start.get("maze_index") or 0),
                int(start.get("start_x") or 0),
                int(start.get("start_y") or 0),
            )
        start_has_layered_scene = bool(start_layered_map_ids)
        # 0009 roster uid and 001D row seq share the Stage3 Actor table.
        # Starting every run at seq=1 lets a third APC overwrite cid=3.
        player_actor_key = int(cid or 0) & 0xFFFF
        actor_first_seq = initial_actor_key_base(player_actor_key)
        if raid_plan is not None:
            actor_first_seq = raid_plan.actor_base
        # Use the resolved dungeon's PVF fatigue flags. Raid/tower paths keep
        # their own entry budgets; ordinary loading ACKs debit new rooms.
        if session is not None and (raid_plan is None or ordinary_plan):
            from game.character.fatigue_flow import prepare_run, login_balance, native_body
            if not prepare_run(self.server, session, int(dungeon_id)):
                balance = login_balance(self.server._repo(), int(cid))
                fatigue_packet, _ = self._emit(0, 0x0024, native_body(balance), "codec")
                refused, _ = self._emit(1, 0x0010, b"\x00\x13\x00", "codec")
                return [fatigue_packet, refused], "%s; reject=fatigue_exhausted no_START_MAP" % base
        elif session is not None:
            session._ordinary_fatigue_run = None
        tournament_note = "tournament=skip_not_tournament"
        if is_tournament_run:
            tournament_actors, tournament_note = initialize_tournament_run(
                session,
                dungeon_id=int(dungeon_id),
                basis_level=int(start.get("basis_level") or 0),
                first_seq=actor_first_seq,
            )
            if not tournament_actors:
                return [], (
                    "%s; gate=ON grant=ON reject=tournament_initialization dungeon_id=%s "
                    "map_index=%s detail=%s; chain_file=game/chains/flow-dungeon_enter.py"
                    % (
                        base,
                        int(dungeon_id) & 0xFFFFFFFF,
                        map_index,
                        tournament_note,
                    )
                )
            mons = list(tournament_actors)
        else:
            mons = load_map_monsters(
                monster_map_index,
                dungeon_basic_lv=int(start.get("basis_level") or 0),
                first_seq=actor_first_seq,
                active_quest_ids=active_ids,
                dungeon_id=int(dungeon_id),
                difficulty=difficulty,
            )
        # 进本起点通常无已毁记录；仍走同一过滤路径保持与 MOVE_MAP 对称
        if session is not None and not is_tournament_run:
            mons = filter_destroyed_objects(
                session,
                mons,
                map_index=monster_map_index,
            )
            mons = resequence_monsters(mons, first_seq=actor_first_seq)
        # 剧情 face 预装（全副本通用）：同迷宫其它房带 [face image] 的 mon，
        # 当前 001D 没有则注入 team=0 友方，供 sequence popup 头像（例：2009 开场 69271）。
        if not is_tournament_run:
            mons = inject_story_face_preloads(
                mons,
                dungeon_id=int(dungeon_id),
                map_index=map_index,
                maze_index=int(start.get("maze_index") or 0),
                dungeon_basic_lv=int(start.get("basis_level") or 0),
                first_seq=actor_first_seq,
            )
        if session is not None and not is_tournament_run:
            mons, condition_actor_note = append_room_condition_actors(
                session,
                mons,
                cell_x=int(start.get("start_x") or 0),
                cell_y=int(start.get("start_y") or 0),
                map_index=monster_map_index,
                dungeon_basic_lv=int(start.get("basis_level") or 0),
                first_seq=actor_first_seq,
            )
            mons = resequence_monsters(mons, first_seq=actor_first_seq)
        # 剧情模式：PVF [story mode]/[increase exp rate] a b → (a+b)/100 叠到经验权重；
        # MAX_HP/DEFENSE 写入 mon/session（wire 无 HP 字段，客户端仍按 maze 读 PVF）。
        story_active = bool(start.get("story_mode_active")) or bool(quest_connected)
        story_info = None
        dgn_info = None
        try:
            from game.quest.quest_index import get_quest_index

            dgn_info = get_quest_index().get_dungeon(int(dungeon_id))
            story_info = story_info_from_dungeon(dgn_info)
        except Exception:
            story_info = None
        mons = apply_story_scales_to_monsters(
            mons, story_info, story_active=story_active
        )
        # 先 init 本局 room 状态（会清空旧同伴集合），再绑定新同伴。
        # 顺序硬约束：禁止 init 在 bind 之后，否则会把 companion store 抹掉。
        room_passive_objects = (
            [] if is_tournament_run else load_map_passive_objects(monster_map_index)
        )
        ally_note = "ally_companions=skip"
        if session is not None:
            room_passive_objects.extend(energy_objects.passive_shadows(
                session, dungeon_id, cell_x, cell_y))
            bind_room_passive_objects(
                session,
                room_passive_objects,
                map_index=monster_map_index,
            )
            if is_tournament_run:
                condition_passive_note = "condition_passives=skip_tournament"
                ally_note = "ally_companions=skip_tournament"
            else:
                condition_passive_note = bind_room_condition_passives(
                    session,
                    mons,
                    map_index=monster_map_index,
                )
                mons, ally_note = bind_companions_on_enter(
                    session,
                    mons,
                    map_index=map_index,
                    dungeon_basic_lv=int(start.get("basis_level") or 0),
                    first_seq=actor_first_seq,
                )
                mons = resequence_monsters(mons, first_seq=actor_first_seq)
        if is_hell_room and not is_tournament_run:
            mons = append_hellparty_templates(mons, hell_room)
            mons = resequence_monsters(mons, first_seq=actor_first_seq)
        clear_condition_note = "clear_conditions=skip_no_session"
        if session is not None and not is_tournament_run:
            mons, clear_condition_note = attach_destroy_object_clear_shadows(
                session,
                mons,
                dungeon_id=int(dungeon_id),
                maze_index=int(start.get("maze_index") or 0),
                map_index=int(map_index),
            )
        another_arad_changed = 0
        if another_arad_selection is not None:
            mons, another_arad_changed = apply_another_arad_monster_level(
                mons,
                another_arad_level,
            )
        ridable_objects = []
        if not is_tournament_run:
            ridable_objects = ridable_objects_for_room(
                int(dungeon_id),
                int(start.get("maze_index") or 0),
                cell_x,
                cell_y,
                seed=seed,
            )
        base_w0 = float(start.get("experience_increasing_point") or 1.0)
        story_summary: dict[str, Any] = {
            "story_active": bool(
                story_active and story_info is not None and getattr(story_info, "present", False)
            ),
            "exp_rate_pair": (
                (
                    float(story_info.exp_rate_a),
                    float(0.0 if story_info.exp_rate_b is None else story_info.exp_rate_b),
                )
                if story_info is not None and story_info.exp_rate_a is not None
                else None
            ),
            "story_exp_mult": float(start.get("story_exp_multiplier") or 1.0),
            "combined_weight": float(start.get("experience_weight") or base_w0),
            "base_weight": base_w0,
        }
        tower_kind = tower_kind_for_dungeon(int(dungeon_id))
        cn_tower_admitted = False
        if tower_kind is not None and session is not None:
            from pvf.adventure_group_index import cn_adventure_traits_enabled

            if cn_adventure_traits_enabled():
                from game.quest.quest_index import daily_quest_day_key

                try:
                    repo = self.server._repo()
                    owner = int(repo.get_account_id_for_character(int(cid or 0)) or 0)
                    if owner <= 0 or owner != int(self.server.account_id):
                        refused, _ = self._emit(1, 0x0010, b"\x00\x13\x00", "codec")
                        return [refused], "%s; reject=tower_entry_owner" % base
                    entry_day = daily_quest_day_key()
                    limit = _hundred_floor_progress.tower_entry_limits(repo, owner)[tower_kind]
                    cn_tower_admitted = repo.record_hundred_floor_tower_entry(
                        owner, tower_kind, entry_day, limit=limit
                    )
                    if not cn_tower_admitted:
                        display = _hundred_floor_progress.tower_entry_display(repo, owner, entry_day)
                        refused, _ = self._emit(1, 0x0010, b"\x00\x13\x00", "codec")
                        packet = self.server._codec_response(0, 0x0510, bytes(display))
                        return [refused, packet], "%s; reject=tower_entry_limit limit=%d" % (base, limit)
                except Exception as exc:
                    refused, _ = self._emit(1, 0x0010, b"\x00\x13\x00", "codec")
                    return [refused], "%s; reject=tower_entry_error=%s" % (base, type(exc).__name__)
        recovery_note = self._persist_dungeon_login_recovery(
            character_id=cid,
            session=session,
            character=character,
        )
        recommended_character_level = int(
            getattr(character, "level", 0)
            or getattr(session, "character_level", 0)
            or 0
        )
        recommended_min_level = int(start.get("minimum_required_level") or 0)
        recommended_max_level = int(start.get("basis_level") or 0)
        recommended_dungeon = is_recommended_dungeon_level(
            recommended_character_level,
            recommended_min_level,
            recommended_max_level,
        )
        start_room_cleared = False
        weapon_avatar_render_note = "weapon_avatar_render=skip_no_session"
        if session is not None:
            worldmap_id = None
            try:
                pre_town = getattr(session, "pre_select_town_id", None)
                pre_area = getattr(session, "pre_select_area_id", None)
                if pre_town is not None and pre_area is not None:
                    worldmap_id = resolve_worldmap_id(int(pre_town), int(pre_area))
            except Exception:
                worldmap_id = None
            if worldmap_id is None:
                worldmap_id = worldmap_id_for_dungeon(int(dungeon_id))
            session.in_dungeon_select = False
            session.dungeon_select_active = False
            session.in_dungeon_run = True
            session.dungeon_run_generation = (
                int(getattr(session, "dungeon_run_generation", 0) or 0) + 1
            )
            if cn_tower_admitted:
                session.tower_entry_display_generation = session.dungeon_run_generation
            session.dungeon_death_pending = False
            session.dungeon_death_actor_id = 0
            session.selected_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
            session.selected_difficulty = difficulty
            session.cur_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
            session.cur_dungeon_difficulty = difficulty
            # The special selector's u16 option is independent from ordinary
            # difficulty. Keep its canonical account key and admitted highest
            # snapshot for a verified clear at C1/002E.
            session.dungeon_special_selector_dungeon_id = 0
            session.dungeon_special_selector_progress_key = 0
            session.dungeon_special_selector_entry_option = 0
            session.dungeon_special_selector_highest_unlocked = 0
            if is_special_dungeon(int(dungeon_id)):
                session.dungeon_special_selector_dungeon_id = int(dungeon_id)
                session.dungeon_special_selector_progress_key = int(
                    special_progress_key
                )
                session.dungeon_special_selector_entry_option = entry_option
                session.dungeon_special_selector_highest_unlocked = int(
                    special_highest_difficulty
                )
            if another_arad_selection is not None:
                bind_another_arad_session(
                    session,
                    another_arad_selection,
                    another_arad_level,
                )
            session.dungeon_start_x = int(start.get("start_x") or 0) & 0xFF
            session.dungeon_start_y = int(start.get("start_y") or 0) & 0xFF
            session.dungeon_cell_x = int(start.get("start_x") or 0) & 0xFF
            session.dungeon_cell_y = int(start.get("start_y") or 0) & 0xFF
            session.dungeon_boss_x = int(start.get("boss_x") or 0) & 0xFF
            session.dungeon_boss_y = int(start.get("boss_y") or 0) & 0xFF
            session.dungeon_map_index = map_index
            session.dungeon_normal_map_index = normal_room_map_index
            session.dungeon_special_hell = bool(
                start.get("is_special_hell_dungeon", False)
            )
            # Special-Hell maps have no generated Hell Party room.  Preserve
            # the entry mode so their final boss uses the same PVF roll table.
            session.dungeon_special_hell_party_mode = int(hell_party_mode)
            try:
                from game.drop.wish_service import hydrate_session_wish_items

                hydrate_session_wish_items(
                    self.server,
                    session,
                    character_id=int(cid),
                )
            except Exception:
                session.dungeon_wish_item_ids = ()
            session.dungeon_worldmap_id = (
                int(worldmap_id) if worldmap_id is not None else -1
            )
            session.dungeon_hell_room_active = bool(is_hell_room)
            session.dungeon_hell_hidden_count = int(
                hell_room.get("hidden_count") or 0
            )
            session.dungeon_maze_index = int(start.get("maze_index") or 0)
            session.dungeon_boss_map_variants = dict(
                start.get("boss_map_variants") or {}
            )
            session.dungeon_required_cells = {
                (int(cell[0]), int(cell[1]))
                for cell in tuple(start.get("room_cells") or ())
                if isinstance(cell, (list, tuple)) and len(cell) >= 2
            }
            session.quest_connected = bool(quest_connected)
            session.dungeon_seed = seed
            session.dungeon_clear_phase = 0
            session.dungeon_run_kill_exp = 0
            session.dungeon_run_premium_exp = 0
            session.dungeon_run_growth_exp = 0
            session.dungeon_run_neo_exp = 0
            session.dungeon_run_fatigue_burn_exp = 0
            # The settlement timer starts only after the 0010 run binding is
            # complete.  It is monotonic so wall-clock changes cannot alter a
            # player's recorded dungeon duration.
            session.dungeon_run_started_monotonic = time.monotonic()
            session.dungeon_run_play_time_ms = None
            session.dungeon_run_best_time_ms = None
            # A new run must never inherit a previous settlement's frozen
            # card plan or local-layout flags.
            session.dungeon_card_reward_plan = None
            session.dungeon_card_layout_sent = False
            session.dungeon_card_flipped = False
            session.dungeon_card_free_player_id = 0
            session.dungeon_card_paid_player_id = 0
            session.dungeon_card_auto_flip_armed = False
            session.dungeon_card_auto_flip_deadline_monotonic = 0.0
            session.dungeon_card_auto_flip_fired = False
            session.dungeon_card_auto_flip_run_generation = 0
            # 001C/001D only change dungeon/map state. Stage3 keeps the main
            # character object and its complete USERINFO-backed EXP snapshot.
            session.dungeon_room_mons = list(mons)
            session.dungeon_room_first_seq = actor_first_seq
            room_state = cache_room_state(
                session,
                maze_index=int(start.get("maze_index") or 0),
                cell_x=int(start.get("start_x") or 0),
                cell_y=int(start.get("start_y") or 0),
                map_index=map_index,
                normal_map_index=normal_room_map_index,
                monsters=session.dungeon_room_mons,
                first_seq=actor_first_seq,
                seed=seed,
            )
            if room_state is not None:
                session.dungeon_room_mons = room_state["monsters"]
            # The primary character object key is the placement anchor for a
            # manually discarded item. It must not follow monster DIE seqs.
            session.dungeon_player_actor_key = player_actor_key
            session.dungeon_basis_level = int(start.get("basis_level") or 0)
            session.dungeon_recommended = bool(recommended_dungeon)
            begin_adventure_season1_run(
                session, recommended=recommended_dungeon, raid=raid_plan is not None,
            )
            begin_weekly_attendance_run(
                session, str(start.get("path") or ""),
                recommended=recommended_dungeon, raid=raid_plan is not None,
            )
            # 房间掉落 LCG / 场景槽：每局重新 seed（时间熵），避免多次进本掉落序列固定。
            # maze 拓扑仍用上面的 deterministic `seed`；掉落流必须独立随机化。
            try:
                import time as _time

                entropy = (
                    (int(_time.time() * 1000) & 0xFFFFFFFF)
                    ^ ((int(_time.perf_counter_ns() if hasattr(_time, "perf_counter_ns") else 0) >> 10) & 0xFFFFFFFF)
                    ^ (int(cid or 0) * 0x9E3779B1)
                    ^ (int(dungeon_id) * 0x85EBCA6B)
                ) & 0xFFFFFFFF
                # 注意：不可命名 base —— 会遮蔽上方 C1/0010 诊断字符串 base
                drop_seed_base = (int(seed) ^ 0xD0A1 ^ entropy) & 0xFFFFFFFF
                if drop_seed_base == 0:
                    drop_seed_base = 1
                session.dungeon_drop_lcg_seed = drop_seed_base
            except Exception:
                session.dungeon_drop_lcg_seed = 1
            session.dungeon_drop_scene_slot = 0
            session.dungeon_drops = {}
            session.dungeon_drop_meta = {}
            base_w = float(start.get("experience_increasing_point") or 1.0)
            story_summary = bind_story_to_session(
                session,
                story_info,
                story_active=story_active,
                base_weight=base_w,
            )
            # 兼容：若 resolve 已带 combined weight 且 bind 失败，仍写会话
            if not story_summary.get("combined_weight"):
                session.dungeon_experience_weight = float(
                    start.get("experience_weight") or base_w
                )
            session.dungeon_finish_loading = False
            session.dungeon_guardian_gem_refresh_sent = False
            weapon_avatar_render_note = prepare_pending_weapon_avatar_render(
                self.server,
                session=session,
                character=character,
                dungeon_id=int(dungeon_id),
                map_index=int(map_index),
            )
            # room_state 已在 bind 前 init；此处只处理起始房空门。
            if raid_hooks is not None:
                raid_hooks.bind_shared_room(session)
                mons = list(session.dungeon_room_mons)
                room_passive_objects = list(session.dungeon_room_passive_objects)
            from game.normal_party_run import bind_room
            if bind_room(self.server, session) is not None:
                mons = list(session.dungeon_room_mons)
                room_passive_objects = list(session.dungeon_room_passive_objects)
            # 同格的 DGN layered 场景由客户端随后以 C1/002D 推进，空的
            # 起始层不能被当作已清逻辑格，否则会跳过落地小游戏及其后续层。
            has_blocking = any(bool(m.get("is_blocking", True)) for m in mons)
            if not has_blocking and not start_has_layered_scene:
                start_room_cleared = True
                try:
                    _room_state.mark_room_cleared(
                        session,
                        int(start.get("start_x") or 0),
                        int(start.get("start_y") or 0),
                    )
                    if bool(getattr(session, "another_arad_active", False)):
                        load_chain("helper-another_arad_quest.py").note_another_arad_room_cleared(
                            session,
                            int(start.get("start_x") or 0),
                            int(start.get("start_y") or 0),
                        )
                except Exception:
                    pass

        preload_note = "drop_preload=skip_no_session"
        room_mons_for_wire = mons
        if session is not None:
            if is_tournament_run:
                preload_note = "drop_preload=skip_tournament"
            else:
                preload_note = preload_room_drops_before_visibility(
                    self.server,
                    session,
                    source="dungeon_enter_001d",
                )
            room_mons_for_wire = list(
                getattr(session, "dungeon_room_mons", None) or []
            )

        # This must run after the new-run reset and actor preload above.  The
        # rows are client-owned ground objects, so their keys must remain in
        # ``dungeon_drops`` after START_MAP and must not collide with reserved
        # actor-drop scene slots.
        passive_extra: list[dict[str, Any]] = []
        passive_item_note = (
            "passive_item=skip_tournament"
            if is_tournament_run
            else "passive_item=skip_no_session"
        )
        if session is not None and not is_tournament_run:
            try:
                from game.drop.drop_service import (
                    bind_passive_object_item_entries,
                    generate_passive_object_item_entries,
                )
                from game.quest.quest_index import passive_object_items_for_maze

                spo_items = passive_object_items_for_maze(
                    dgn_info, int(start.get("maze_index") or 0)
                )
                bindings = bind_passive_object_item_entries(spo_items, room_passive_objects)
                if bindings:
                    passive_extra = generate_passive_object_item_entries(
                        session, spo_items, passive_objects=room_passive_objects,
                    )
                preview = [
                    "object%d<-group%d:%d@%d" % (
                        row["object_index"], row["drop_group_index"],
                        row["item_id"], row["drop_rate"],
                    )
                    for row in bindings[:12]
                ]
                passive_item_note = (
                    "passive_item map=%d configured_rows=%d bound_rows=%d "
                    "emitted_rows=%d bindings=%s"
                    % (int(monster_map_index), len(spo_items), len(bindings),
                       len(passive_extra), ";".join(preview) or "-")
                )
            except Exception as exc:
                passive_extra = []
                passive_item_note = "passive_item_error=%s" % type(exc).__name__

        body_001d = build_start_map_001d_us(
            cell_x=cell_x,
            cell_y=cell_y,
            seed=seed,
            map_index=map_index,
            room_flag=room_flag,
            layered=layered,
            monsters=room_mons_for_wire,
            open_door_even_enemy=bool(start.get("open_door_even_enemy")),
            hell_party_mode=hell_party_mode if hell_enabled else 0,
            hell_fog=1 if hell_enabled else 0,
            party_member_index=party_member,
            passive_object_drops=passive_extra,
            ridable_objects=ridable_objects,
        )

        packets: list[bytes] = []
        notes: list[str] = [
            "recommended=%d char_lv=%d range=%d..%d"
            % (
                int(recommended_dungeon),
                recommended_character_level,
                recommended_min_level,
                recommended_max_level,
            )
        ]
        notes.append(weapon_avatar_render_note)
        notes.append(preload_note)
        notes.append(passive_item_note)
        notes.append(tournament_note)
        if another_arad_selection is not None:
            notes.append(
                "another_arad wrapper=%d historical=%d quest=%d difficulty=%d "
                "level=%d changed=%d %s"
                % (
                    int(another_arad_selection.wrapper_dungeon_id),
                    int(another_arad_selection.historical_dungeon_id),
                    int(another_arad_selection.crack_quest_id),
                    difficulty,
                    another_arad_level,
                    another_arad_changed,
                    another_arad_endpoint_note,
                )
            )
            notes.append(
                str(
                    getattr(
                        session,
                        "another_arad_quest_bind_note",
                        "mission_bind_note=missing",
                    )
                )
            )
        # Install the solo actor only for an accepted dungeon. The body is
        # still the selector-era dungeon_id=0 roster, before the real 001C/001D.
        from game.normal_party import current as current_normal_party
        if (session is not None and cid is not None
                and not getattr(session, 'dungeon_party_preloaded', False)
                and current_normal_party(self.server, session) is None
                and not (raid_hooks is not None and raid_hooks.battle.raid_for_session(session) is not None)):
            solo = build_party_info_type0_solo(int(cid) & 0xFFFF, dungeon_id=0, difficulty=0)
            pkt, _ = self._emit(0, 9, solo, "codec")
            packets.append(pkt)
            session.dungeon_party_preloaded = True
            session.dungeon_solo_party_owner = int(cid) & 0xFFFF

        # Optional Stage3 S0/0009 PARTY_INFO before 001C/001D.
        # Friend 副本实现A does NOT send 0009 on SELECT (only 001C->001D).
        # Live 20260717_141144: grant 0009+001C+001D -> client only PIV'd 0009,
        # then channel-full; 0009 handler also calls enter_game_state paths that
        # can yank the client out of select-dungeon. Default OFF; A/B with env=1.
        # Legacy001D tail0 selects the indexed party actor; it is not replaced
        # with each recipient's own slot. Full peer-flow validation is separate.
        if (_env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0009", False) and cid is not None
                and not (raid_hooks is not None and raid_hooks.battle.raid_for_session(session) is not None)):
            uid16 = int(cid) & 0xFFFF
            body_0009 = build_party_info_type0_solo(
                uid16,
                dungeon_id=int(dungeon_id) & 0xFFFF,
                difficulty=difficulty,
            )
            pkt9, mode9 = self._emit(0, 0x0009, body_0009, "codec")
            packets.append(pkt9)
            notes.append(
                "0009/%s/len=%d uid=%d dung=%d diff=%d"
                % (mode9, len(body_0009), uid16, int(dungeon_id) & 0xFFFF, difficulty)
            )
        if another_arad_selection is not None:
            # Stage3 S0/0278 reader 0x01633E90 consumes exactly one u32 and
            # force-projects that Crack quest through 0x0136E7D0.  Install it
            # before the room readers so quest-owned map behavior is active
            # when 001C/001D build the dungeon scene.
            body_0278 = int(another_arad_selection.crack_quest_id).to_bytes(
                4,
                "little",
                signed=False,
            )
            pkt_0278, mode_0278 = self._emit(0, 0x0278, body_0278, "codec")
            packets.append(pkt_0278)
            notes.append(
                "0278/%s/len=%d crack_quest=%d"
                % (
                    mode_0278,
                    len(body_0278),
                    int(another_arad_selection.crack_quest_id),
                )
            )
        pkt_c, mode_c = self._emit(0, 0x001C, body_001c, "codec")
        packets.append(pkt_c)
        notes.append(
            "001C/%s/len=%d difficulty=%d entry_option=%d hell=%d room=(%s,%s) map=%s source=%s waves=%s wave_mode=%s hidden=%s"
            % (
                mode_c,
                len(body_001c),
                difficulty,
                entry_option,
                int(hell_enabled),
                hell_room.get("x") if hell_enabled else "-",
                hell_room.get("y") if hell_enabled else "-",
                hell_room.get("map_index") if hell_enabled else "-",
                hell_room.get("cell_source") if hell_enabled else "-",
                hell_room.get("wave_count") if hell_enabled else 0,
                hell_room.get("wave_mode") if hell_enabled else "-",
                hell_room.get("hidden_count") if hell_enabled else 0,
            )
        )
        if condition_minimap_entries:
            body_022f = build_minimap_icon_info_022f_us(
                condition_minimap_entries
            )
            pkt_022f, mode_022f = self._emit(0, 0x022F, body_022f, "codec")
            packets.append(pkt_022f)
            notes.append(
                "022F/%s/len=%d icons=%d"
                % (mode_022f, len(body_022f), len(condition_minimap_entries))
            )
        pkt_d, mode_d = self._emit(0, 0x001D, body_001d, "codec")
        packets.append(pkt_d)
        if is_tournament_run:
            try:
                body_0183 = build_tournament_info_0183(
                    session,
                    difficulty=difficulty,
                )
                pkt_0183, mode_0183 = self._emit(0, 0x0183, body_0183, "codec")
                packets.append(pkt_0183)
                body_0184 = build_tournament_map_info_0184(
                    cell_x=cell_x,
                    cell_y=cell_y,
                    seed=seed,
                    map_index=map_index,
                    revisit=False,
                )
                pkt_0184, mode_0184 = self._emit(0, 0x0184, body_0184, "codec")
                packets.append(pkt_0184)
                notes.append(
                    "0183/%s/len=%d bracket=16; 0184/%s/len=%d arena_map=%d"
                    % (
                        mode_0183,
                        len(body_0183),
                        mode_0184,
                        len(body_0184),
                        map_index,
                    )
                )
            except Exception as exc:
                return [], (
                    "%s; gate=ON grant=ON reject=tournament_projection type=%s; "
                    "chain_file=game/dungeon/flow-dungeon_enter.py"
                    % (base, type(exc).__name__)
                )
        if (
            start_room_cleared
            and _env_bool("USDOF_GAME_QUEST_CLEAR_MAP_PROGRESS", True)
        ):
            try:
                seen_companions, alive_companions = companion_progress_codes(session)
                quest_packets, clear_map_note = persist_clear_map_progress(
                    self.server,
                    session,
                    context=DungeonClearContext(
                        dungeon_id=int(dungeon_id),
                        difficulty=int(difficulty),
                        map_id=int(normal_room_map_index),
                        seen_companion_codes=seen_companions,
                        alive_companion_codes=alive_companions,
                    ),
                )
                packets.extend(quest_packets)
                notes.append(clear_map_note)
            except Exception as exc:
                notes.append("clear_map err=%r" % (exc,))
        notes.extend(
            [
                special_condition_note,
                condition_actor_note,
                condition_passive_note,
                clear_condition_note,
            ]
        )
        wire_mons = [mon for mon in mons if bool(mon.get("wire_visible", True))]
        notes.append(
            "001D/%s/len=%d/mon=%d/extra=%d/ridable=%d cell=%s(%d,%d) "
            "room_flag=%d layered=%d party=%d "
            "actor_uid=0x%04X actor_base=0x%04X actor_first=%s "
            "hell_mode=%d hell_fog=%d hell_room=%d; %s"
            % (
                mode_d,
                len(body_001d),
                wire_actor_count(mons),
                len(passive_extra),
                len(ridable_objects),
                cell_tag,
                cell_x,
                cell_y,
                room_flag,
                layered,
                party_member,
                player_actor_key,
                actor_first_seq,
                (
                    "0x%04X" % (int(wire_mons[0].get("seq") or 0) & 0xFFFF)
                    if wire_mons
                    else "-"
                ),
                hell_party_mode if hell_enabled else 0,
                1 if hell_enabled else 0,
                int(is_hell_room),
                ally_note,
            )
        )

        # Friend DFOUSServer optional 0117/019F after START_MAP — DO NOT default ON
        # for Stage3. PacketEnum_current_USDOF_Stage3.h:
        #   0x0117 = ENUM_CMDPACKET_LAG_STATISTICS (C->S)
        #   0x019F = ENUM_CMDPACKET_CHARAC_SLOT_EXTEND_EFFECT
        # Live 20260717_140057: grant sent 0009+001C+001D+0117+019F, client only
        # PIV'd 0009 then channel-full disconnect. Keep OFF unless A/B re-proved.
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_0117", False) and cid is not None:
            body_0117 = struct.pack("<I", int(cid) & 0xFFFFFFFF)
            pkt, mode = self._emit(0, 0x0117, body_0117, "codec")
            packets.append(pkt)
            notes.append("0117/%s" % mode)
        if _env_bool("USDOF_GAME_DUNGEON_ENTER_SEND_019F", False):
            body_019f = bytes([0x00, 0x00])
            pkt, mode = self._emit(0, 0x019F, body_019f, "codec")
            packets.append(pkt)
            notes.append("019F/%s" % mode)

        note = (
            "%s; gate=ON grant=ON enter_run maze=%s reason=%s start=(%s,%s) "
            "boss=(%s,%s) map_index=%s map_source=%s seed=0x%08X active=%s finished=%s qc=%s "
            "quest_maze=%s quest_connected=%s mode_flag=%s "
            "license_boss=%s code=%s source=%s "
            "hell_requested=%s hell_enabled=%s hell_reason=%s hell_source=%s "
            "story=%s exp_pair=%s exp_mult=%.3f weight=%.3f reply=[%s]; "
            "doc_ref=90qiyuan selectDungeonMaze + 副本实现A SELECT->001C+001D; "
            "no_default_0117_019F; "
            "chain_file=game/chains/flow-dungeon_enter.py"
            % (
                base,
                start.get("maze_index"),
                start.get("reason"),
                start.get("start_x"),
                start.get("start_y"),
                start.get("boss_x"),
                start.get("boss_y"),
                map_index,
                start.get("map_source") or "maze_map_specification",
                seed,
                ",".join(str(x) for x in active_ids[:8]) if active_ids else "-",
                ",".join(str(x) for x in finished_ids[:8]) if finished_ids else "-",
                start.get("quest_connection"),
                int(quest_maze_on),
                int(quest_connected),
                mode_flag,
                int(bool(start.get("license_boss_present"))),
                int(start.get("license_boss_monster_code") or 0),
                start.get("license_boss_selection_source") or "not_applicable",
                int(hell_requested),
                int(hell_enabled),
                hell_room.get("reason") or ("resolved" if hell_enabled else "-"),
                hell_room.get("cell_source") if hell_enabled else "-",
                int(bool(story_summary.get("story_active"))),
                story_summary.get("exp_rate_pair"),
                float(story_summary.get("story_exp_mult") or 1.0),
                float(
                    story_summary.get("combined_weight")
                    or start.get("experience_weight")
                    or start.get("experience_increasing_point")
                    or 1.0
                ),
                ", ".join(notes + [recovery_note]),
            )
        )
        return packets, note
