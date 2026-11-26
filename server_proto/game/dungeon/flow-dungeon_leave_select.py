"""Leave dungeon-select UI / return to village (USDOF Stage3).

C2S (live + 副本实现A.md):
  0x0084 BACK_2_VILLAGE  empty body (confirmed live when clicking return)
  0x002A GIVEUP_GAME     same leave-select path (US dual register)

S2C:
  [only for C1/002A] S1/002A body {0x01} completes GIVEUP_GAME
  S0/0003 USER_STATE (state=1; US can stall on 0)
  S0/0017 USER_AREA  restore pre_select town/area/pos
  S0/0018 AREA_USERS self roster
  S0/00CA body {0x00}
  [from in-run/settlement] S0/000D list0 + USERINFO0 + USERINFO1 + S0/000E list3
    + S0/01E8|01E9 + S0/01EA guild status
    —— 副本→城镇后客户端会重建/清空快捷栏与装备槽；list0 先重建主背包，
       S4A12 回城补 subtype0，Stage3 装备栏还须 USERINFO1/166AED0 建槽
       + 000E list3 更新。

Do not use s4a12 short permission bodies here.
"""
from __future__ import annotations

import os
from typing import Any

from game.character.adventure_group import (
    calculate_adventure_group_for_character,
)
from game.character.native_loadout_bridge import abort_native_loadout_plan
from game.chain_loader import load_chain
from game.character.payloads import (
    build_area_users,
    build_item_list_mode0,
    build_user_area,
    build_userinfo_subtype0_min,
    build_userinfo_subtype1_min,
)
from game.character.inventory_item_view import InventoryItemView
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.guild.guild_repository import GuildRepository

_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_enter_select_user_state = _dungeon_payloads.build_enter_select_user_state
_room_state = load_chain("helper-dungeon_room_state.py")
reset_run_room_state = _room_state.reset_run_room_state
_another_arad = load_chain("helper-another_arad.py")
clear_another_arad_session = _another_arad.clear_another_arad_session
_quest_completion = load_chain("helper-quest_completion_snapshot.py")
_tower_item_cleanup = load_chain("helper-death_tower_item_cleanup.py")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip(), 0)
    except Exception:
        return default


class DungeonLeaveSelectService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _cid(self, session: Any) -> int | None:
        if session is None:
            return None
        for attr in ("character_id", "selected_character_id", "cid"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    return int(val)
                except Exception:
                    pass
        return None

    def _load_character(self, character_id: int | None) -> Any | None:
        if character_id is None:
            return None
        repo = getattr(self.server, "character_repo", None)
        if repo is None:
            return None
        try:
            return repo.get_character(int(character_id))
        except Exception:
            try:
                return repo.get(int(character_id))
            except Exception:
                return None

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str) -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def _load_character_record(self, cid: int | None) -> Any | None:
        """完整 CharacterRecord（含 equipped_entries / name_bytes），供 USERINFO 用。"""
        if cid is None or int(cid) <= 0:
            return None
        repo = getattr(self.server, "character_repo", None)
        if repo is None:
            try:
                repo = self.server._repo()
            except Exception:
                repo = None
        if repo is None:
            return None
        account_id = int(getattr(self.server, "account_id", 1) or 1)
        try:
            for row in repo.list_by_account(account_id):
                if int(getattr(row, "character_id", -1) or -1) == int(cid):
                    return row
        except Exception:
            pass
        try:
            if hasattr(repo, "get_character"):
                return repo.get_character(int(cid))
        except Exception:
            pass
        return None

    def _append_town_equip_resync(
        self,
        packets: list[bytes],
        notes: list[str],
        *,
        cid: int | None,
        leave_kind: str,
    ) -> None:
        """副本内/结算回城后补发主背包、装备外观与装备栏。

        日志 2026-07-19 CCC：leave 只发 0003/0017/0018/00CA → 城镇装备槽空；
        DB character_equipped_entries 仍在。S4A12 ReturnToVillage 补 subtype0；
        Stage3 装备栏还要 U1/166AED0 + 000E list3。

        2026-07-26 live：结算回城后未重发 list0，下一次进本客户端不再发
        C1/002C；重新选角的完整 000D list0 可立即恢复。list0 必须在 USERINFO
        重同步前发出，沿用 START_MAP 的主背包构造器与容器参数。
        """
        if not _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_EQUIP_RESYNC", True):
            notes.append("equip_resync=off")
            return
        if leave_kind not in {"settlement", "in_run"}:
            notes.append("equip_resync=skip leave_kind=%s" % leave_kind)
            return
        if cid is None or int(cid) <= 0:
            notes.append("equip_resync=skip no_cid")
            return

        repo = getattr(self.server, "character_repo", None)
        if repo is None:
            try:
                repo = self.server._repo()
            except Exception:
                repo = None
        if repo is None:
            notes.append("equip_resync=skip no_repo")
            return

        rec = self._load_character_record(int(cid))
        if rec is None:
            notes.append("equip_resync=skip no_record cid=%s" % cid)
            return

        # 0) 000D list0：重建主背包和快捷栏，避免副本回城后本地消耗品状态残留。
        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_MAIN_ITEMLIST", True):
            try:
                raw_items, list_param16 = repo.build_itemlist_mode0_rows(
                    int(cid), list_type=0
                )
                item_rows = [InventoryItemView(row) for row in list(raw_items or ())]
                main_body = build_item_list_mode0(
                    item_rows,
                    list_type=0,
                    list_param16=int(list_param16),
                )
                pkt, used = self._emit(0, 0x000D, main_body, "codec")
                packets.append(pkt)
                notes.append(
                    "000D_MAIN/%s/n=%d list_param16=%d"
                    % (used, len(item_rows), int(list_param16))
                )
            except Exception as exc:
                notes.append("000D_MAIN_err=%r" % (exc,))

        equip_helper = load_chain("helper-userinfo_equipment.py")
        equipment, equip_note = equip_helper.resolve_enter_room_equipment(
            rec,
            enabled=True,
            backfill_from_pvf=False,
            repo=repo,
        )

        # 1) USERINFO0：外观 19B（S4A12 回城 subtype0）
        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_USERINFO0", True):
            try:
                guild_id = 0
                guild_name = ""
                db_path = getattr(repo, "db_path", None)
                if db_path is not None:
                    guild = GuildRepository(db_path).get_guild_for_character(
                        int(cid)
                    )
                    if guild is not None:
                        guild_id = int(guild.guild_id)
                        guild_name = guild.display_name
                body_u0 = build_userinfo_subtype0_min(
                    rec,
                    equipment,
                    guild_name=guild_name,
                    guild_id=guild_id,
                )
                pkt, used = self._emit(0, 0x0002, body_u0, "codec")
                packets.append(pkt)
                notes.append(
                    "USERINFO0/%s/len=%d %s guild_id=%s guild=%r"
                    % (used, len(body_u0), equip_note, guild_id, guild_name)
                )
                visibility_helper = load_chain("helper-avatar_visibility.py")
                visibility_body = visibility_helper.build_avatar_visibility_body(
                    int(cid),
                    int(getattr(rec, "visibility_bits", 3)) & 0xFF,
                )
                visibility_packet, visibility_used = self._emit(
                    0, 0x0165, visibility_body, "codec"
                )
                packets.append(visibility_packet)
                notes.append(
                    "AVATAR_VISIBILITY/%s/bits=0x%02x"
                    % (
                        visibility_used,
                        int(getattr(rec, "visibility_bits", 3)) & 0xFF,
                    )
                )
            except Exception as exc:
                notes.append("USERINFO0_err=%r" % (exc,))

        guild_status_rows = ()

        # 2) USERINFO1：166AED0 建装备槽（必须带穿戴；空 count 会清空栏）
        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_USERINFO1", True):
            try:
                exp = int(getattr(rec, "exp", 0) or 0) & 0xFFFFFFFF
                level = max(1, int(getattr(rec, "level", 1) or 1))
                try:
                    exp_db, lv_db = repo.get_character_exp_level(int(cid))
                    exp = int(exp_db or 0) & 0xFFFFFFFF
                    if int(lv_db or 0) > 0:
                        level = max(1, int(lv_db))
                        try:
                            rec.level = level
                            rec.exp = exp
                        except Exception:
                            pass
                except Exception:
                    pass

                skills: tuple = ()
                skill_n = 0
                skill_note = "skills=none"
                if _env_bool("USDOF_GAME_DUNGEON_LEAVE_USERINFO1_SKILLS", True):
                    try:
                        skills_helper = load_chain("helper-character_skills.py")
                        skill_list, skill_note = skills_helper.resolve_skills_for_character(
                            repo,
                            int(cid),
                            int(getattr(rec, "job", 0) or 0),
                            seed_if_empty=False,
                        )
                        skills = tuple(skill_list or ())
                        skill_n = len(skills)
                    except Exception as skill_exc:
                        skill_note = "skills_err=%r" % (skill_exc,)
                        skills = ()
                        skill_n = 0

                quest_snapshot = _quest_completion.load_finished_quest_snapshot(repo, int(cid))
                from game.quest.time_fragment_rewards import finished_time_fragment_count

                time_fragment_count = finished_time_fragment_count(
                    quest_snapshot.finished_quest_ids,
                    job_id=int(getattr(rec, "job", 0) or 0),
                )
                adventure_progression = calculate_adventure_group_for_character(
                    repo,
                    rec,
                )
                adventure_group_level = int(adventure_progression.adventure_level)
                guild_membership_active = has_active_guild_membership(repo, rec)
                body_u1 = build_userinfo_subtype1_min(
                    rec,
                    exp=int(exp),
                    skills=skills,
                    entry_flag=0,
                    equipment=equipment,
                    finished_quest_ids=quest_snapshot.finished_quest_ids,
                    adventure_group_level=adventure_group_level,
                    guild_membership_active=guild_membership_active,
                )
                pkt, used = self._emit(0, 0x0002, body_u1, "codec")
                packets.append(pkt)
                notes.append(
                    "USERINFO1/%s/len=%d equip_n=%d skill_n=%d %s %s "
                    "time_fragments=%d adventure_level=%d %s"
                    % (
                        used,
                        len(body_u1),
                        len(equipment),
                        skill_n,
                        equip_note,
                        skill_note,
                        time_fragment_count,
                        adventure_group_level,
                        quest_snapshot.describe(),
                    )
                )
                guild_status_rows = pack_guild_membership_status_rows(
                    self.server,
                    guild_membership_active,
                )
            except Exception as exc:
                notes.append("USERINFO1_err=%r" % (exc,))

        # 3) 000E list3：更新装备栏格子（须在 U1 建槽之后）
        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_EQUIP_ITEMLIST", True):
            try:
                bar_helper = load_chain("helper-equip_itemlist.py")
                equip_body, equip_n, equip_bar_note = (
                    bar_helper.build_equipment_bar_update_000e_body(repo, int(cid))
                )
                if equip_body:
                    pkt, used = self._emit(0, 0x000E, equip_body, "codec")
                    packets.append(pkt)
                    notes.append(
                        "000E_EQUIP/%s/n=%d %s"
                        % (used, equip_n, equip_bar_note)
                    )
                else:
                    notes.append("000E_EQUIP skip empty n=%d" % equip_n)
            except Exception as exc:
                notes.append("000E_EQUIP_err=%r" % (exc,))

        # Keep guild mutation outside the USERINFO1 -> 000E equipment-slot gap.
        packets.extend(packet for packet, _note in guild_status_rows)
        notes.extend(note for _packet, note in guild_status_rows)

    def _restore_snap(self, session: Any, character: Any | None) -> dict[str, int]:
        # Prefer pre_select_* cached on enter-select.
        town = None
        area = None
        x = None
        y = None
        direction = None
        state = None
        if session is not None:
            town = getattr(session, "pre_select_town_id", None)
            area = getattr(session, "pre_select_area_id", None)
            x = getattr(session, "pre_select_pos_x", None)
            y = getattr(session, "pre_select_pos_y", None)
            direction = getattr(session, "pre_select_direction", None)
            state = getattr(session, "pre_select_area_state", None)
            if town is None:
                town = getattr(session, "cur_town_id", None)
            if area is None or int(area) == 0xFF:
                # never restore select limbo
                area = getattr(session, "pre_select_area_id", None)
            if x is None:
                x = getattr(session, "cur_pos_x", None)
            if y is None:
                y = getattr(session, "cur_pos_y", None)
            if direction is None:
                direction = getattr(session, "cur_direction", None)
            if state is None:
                state = getattr(session, "cur_area_state", None)

        if character is not None:
            if town is None:
                town = getattr(character, "town_id", 1)
            if area is None or int(area) == 0xFF:
                area = getattr(character, "area_id", 0)
            if x is None:
                x = getattr(character, "pos_x", 474)
            if y is None:
                y = getattr(character, "pos_y", 234)
            if direction is None:
                direction = getattr(character, "direction", 5)
            if state is None:
                state = getattr(character, "area_state", 3)

        town_i = int(town if town is not None else 1) & 0xFF
        area_i = int(area if area is not None else 0) & 0xFF
        if area_i == 0xFF:
            area_i = 0
        return {
            "town_id": town_i,
            "area_id": area_i,
            "pos_x": int(x if x is not None else 474) & 0xFFFF,
            "pos_y": int(y if y is not None else 234) & 0xFFFF,
            "direction": int(direction if direction is not None else 5) & 0xFF,
            "state": int(state if state is not None else 3) & 0xFF,
        }

    def _apply_restore(self, session: Any, snap: dict[str, int], user_state: int) -> None:
        if session is None:
            return
        # N0018 resets deferred native actor placement even when the town
        # itself is reused. Every return is a new recipient projection scope.
        session.town_scene_generation = int(getattr(session, 'town_scene_generation', 0) or 0) + 1
        session.cur_town_id = snap["town_id"]
        session.cur_area_id = snap["area_id"]
        session.cur_pos_x = snap["pos_x"]
        session.cur_pos_y = snap["pos_y"]
        session.cur_direction = snap["direction"]
        session.cur_area_state = snap["state"]
        session.user_state = user_state
        session.in_dungeon_select = False
        session.dungeon_select_active = False
        session.dungeon_select_area_id = snap["area_id"]
        # clear run-ish flags if present.  The linked-dungeon retry capability
        # and run generation intentionally survive this one restore; the next
        # C1/0010 consumes the capability whether it matches or not.
        for attr in (
            "in_dungeon_run",
            "selected_dungeon_id",
            "selected_difficulty",
            "dungeon_map_index",
            "dungeon_maze_index",
            "dungeon_start_x",
            "dungeon_start_y",
            "dungeon_cell_x",
            "dungeon_cell_y",
            "dungeon_boss_x",
            "dungeon_boss_y",
            "dungeon_seed",
            "dungeon_clear_phase",
            "dungeon_run_kill_exp",
            "dungeon_run_premium_exp",
            "dungeon_run_growth_exp",
            "dungeon_run_neo_exp",
            "dungeon_run_fatigue_burn_exp",
            "dungeon_room_first_seq",
            "clear_phase",
        ):
            if hasattr(session, attr):
                try:
                    setattr(session, attr, 0 if attr != "in_dungeon_run" else False)
                except Exception:
                    pass
        session.dungeon_run_started_monotonic = None
        session.adventure_season1_run = None
        session.weekly_attendance_run = None
        session.dungeon_death_pending = False
        session.dungeon_death_actor_id = 0
        session.dungeon_run_play_time_ms = None
        session.dungeon_run_best_time_ms = None
        # A pending initial weapon-avatar commit belongs only to this run.
        session.dungeon_weapon_avatar_render_pending = None
        session.dungeon_weapon_avatar_render_character_id = None
        session.dungeon_weapon_avatar_render_dungeon_id = None
        session.dungeon_weapon_avatar_render_map_index = None
        session.dungeon_weapon_avatar_render_run_generation = 0
        session.dungeon_weapon_avatar_render_sent = False
        if hasattr(session, "dungeon_room_mons"):
            session.dungeon_room_mons = []
        if hasattr(session, "dungeon_room_passive_objects"):
            session.dungeon_room_passive_objects = []
        if hasattr(session, "dungeon_drops"):
            session.dungeon_drops = {}
        if hasattr(session, "dungeon_drop_scene_slot"):
            session.dungeon_drop_scene_slot = 0
        if hasattr(session, "dungeon_drop_lcg_seed"):
            session.dungeon_drop_lcg_seed = 0
        if hasattr(session, "dungeon_drop_meta"):
            session.dungeon_drop_meta = {}
        session.dungeon_card_auto_flip_armed = False
        session.dungeon_card_auto_flip_deadline_monotonic = 0.0
        session.dungeon_card_auto_flip_fired = False
        session.dungeon_card_auto_flip_run_generation = 0
        clear_another_arad_session(session)
        # 离开地图：重置本局房间清理记录（下次进本重新刷怪）
        reset_run_room_state(session)

    def handle_leave(self, body: bytes, *, session: Any = None, opcode: int = 0x0084) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_DUNGEON_LEAVE_HANDLE", True)
        cid = self._cid(session)
        character = self._load_character(cid)
        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        clear_phase = (
            int(getattr(session, "dungeon_clear_phase", 0) or 0) if session is not None else 0
        )
        settlement = in_run and clear_phase >= 2
        if not handle_on:
            return [], (
                "C1/%04X leave_select plain_len=%s cid=%s gate=OFF no_s2c; "
                "chain_file=game/chains/flow-dungeon_leave_select.py"
                % (int(opcode) & 0xFFFF, len(body or b""), cid)
            )

        user_id = 1
        if character is not None:
            for attr in ("user_id", "id", "character_id"):
                val = getattr(character, attr, None)
                if val is not None:
                    try:
                        user_id = int(val) & 0xFFFF
                        break
                    except Exception:
                        pass
        if cid is not None:
            user_id = int(cid) & 0xFFFF

        # 教程本检测须在 restore 前：回城强制赛丽亚房间，禁止落副本门口。
        was_tutorial = False
        run_dungeon = 0
        tutorial_complete_note = "tutorial_complete=skip"
        try:
            _tut = load_chain("helper-tutorial_dungeon.py")
            was_tutorial = bool(
                getattr(session, "is_tutorial_dungeon_run", False)
            ) if session else False
            if session is not None:
                run_dungeon = int(
                    getattr(session, "tutorial_dungeon_id", 0)
                    or getattr(session, "cur_dungeon_id", 0)
                    or getattr(session, "selected_dungeon_id", 0)
                    or 0
                )
            if not was_tutorial and run_dungeon > 0:
                was_tutorial = bool(_tut.is_tutorial_dungeon_id(run_dungeon))
        except Exception:
            was_tutorial = False

        snap = self._restore_snap(session, character)
        if was_tutorial:
            try:
                _tut_flow = load_chain("flow-tutorial_first_enter.py")
                town_hint = snap.get("town_id") or 1
                if character is not None:
                    ct = int(getattr(character, "town_id", 0) or 0)
                    if ct > 0:
                        town_hint = ct
                forced = _tut_flow.force_tutorial_return_spawn(
                    session, town_id=int(town_hint)
                )
                snap = {
                    "town_id": forced["town_id"],
                    "area_id": forced["area_id"],
                    "pos_x": forced["pos_x"],
                    "pos_y": forced["pos_y"],
                    "direction": forced["direction"],
                    "state": forced["state"],
                }
                tutorial_complete_note = (
                    "tutorial_return_seria town=%s area=%s pos=(%s,%s)"
                    % (
                        snap["town_id"],
                        snap["area_id"],
                        snap["pos_x"],
                        snap["pos_y"],
                    )
                )
            except Exception as spawn_exc:
                tutorial_complete_note = "tutorial_return_seria_fail:%r" % (spawn_exc,)

        # 副本实现A：回城 USER_STATE 用 1；结算态同样保持 1（s4a12 用 0 会藏角色，US 勿照搬）
        user_state = _env_int("USDOF_GAME_DUNGEON_LEAVE_USER_STATE", 0x01) & 0xFF
        native_loadout_note = ""
        if session is not None:
            try:
                native_loadout_note = abort_native_loadout_plan(
                    session,
                    reason="leave_dungeon",
                )
            except OSError as exc:
                native_loadout_note = (
                    "native_loadout_abort_ack_write_fail:%s" % type(exc).__name__
                )
        self._apply_restore(session, snap, user_state)
        if session is not None:
            try:
                session.dungeon_card_layout_sent = False
            except Exception:
                pass

        # optional DB persist restored pos
        from game.raid.landing import is_raid_waiting_position
        waiting_position = is_raid_waiting_position(session, cid)
        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_PERSIST", True) and cid is not None and not waiting_position:
            repo = getattr(self.server, "character_repo", None)
            if repo is None:
                try:
                    repo = self.server._repo()
                except Exception:
                    repo = None
            if repo is not None:
                try:
                    if hasattr(repo, "update_position"):
                        # CharacterRepository.update_position is keyword-only after cid.
                        persisted = repo.update_position(
                            int(cid),
                            town_id=snap["town_id"],
                            area_id=snap["area_id"],
                            pos_x=snap["pos_x"],
                            pos_y=snap["pos_y"],
                            direction=snap["direction"],
                            area_state=snap["state"],
                        )
                        if persisted and hasattr(repo, "clear_dungeon_login_recovery"):
                            repo.clear_dungeon_login_recovery(int(cid))
                except Exception:
                    pass

        packets: list[bytes] = []
        notes: list[str] = []
        # The solo/tutorial selector publishes party 1 only to satisfy the
        # native START_MAP actor lookup. It is not persistent membership.
        # Remove it before returning to town, without touching owned raid
        # parties or a preload belonging to a previous character selection.
        if (session is not None
                and getattr(session, 'dungeon_party_preloaded', False) is True
                and getattr(session, 'dungeon_solo_party_owner', None) == cid):
            from game.raid.local_bindings import local_raid_hooks
            hooks = local_raid_hooks(self.server)
            owned = hooks is not None and hooks.battle.raid_for_session(session) is not None
            if not owned:
                clear = load_chain('helper-party_info.py').build_current_namespace_party_clear(party_id=1)
                packet, used = self._emit(0, 9, clear, 'codec')
                packets.append(packet)
                notes.append('temporary solo PARTY_INFO cleared before town')
            session.dungeon_party_preloaded = False
            session.dungeon_solo_party_owner = None
        if native_loadout_note:
            notes.append(native_loadout_note)
        if was_tutorial and tutorial_complete_note.startswith("tutorial_return_seria"):
            notes.append(tutorial_complete_note)

        # Tower drops are session-only.  Do this before the existing 000D list0
        # rebuild so a normal dungeon exit removes their client-side icons too.
        try:
            repo = getattr(self.server, "character_repo", None)
            if repo is None:
                repo = self.server._repo()
            cleanup = _tower_item_cleanup.cleanup_death_tower_items(repo, int(cid or 0))
            notes.append(cleanup.note())
        except Exception as cleanup_exc:
            notes.append("death_tower_temp_items_cleanup=error %r" % (cleanup_exc,))

        if (int(opcode) & 0xFFFF) == 0x002A:
            # Stage3 sets dungeon_state+0x81E while sending GIVEUP_GAME. Only the
            # S1/002A response callback clears it; town notifications do not.
            ack_002a = b"\x01"
            pkt, used = self._emit(1, 0x002A, ack_002a, "codec")
            packets.append(pkt)
            notes.append(
                "S1/002A mode=%s plain=%d wire=%d success=1"
                % (used, len(ack_002a), len(pkt))
            )

        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_0003", True):
            body_0003 = build_enter_select_user_state(user_id, user_state)
            flag = _env_int("USDOF_GAME_DUNGEON_LEAVE_0003_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_LEAVE_0003_MODE", "codec")
            pkt, used = self._emit(flag, 0x0003, body_0003, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0003 mode=%s plain=%d wire=%d uid=0x%04X state=0x%02X"
                % (flag, used, len(body_0003), len(pkt), user_id, user_state)
            )

        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_0017", True):
            body_0017 = build_user_area(
                character_id=user_id,
                town_id=snap["town_id"],
                area_id=snap["area_id"],
                pos_x=snap["pos_x"],
                pos_y=snap["pos_y"],
                direction=snap["direction"],
                state=snap["state"],
            )
            flag = _env_int("USDOF_GAME_DUNGEON_LEAVE_0017_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_LEAVE_0017_MODE", "codec")
            pkt, used = self._emit(flag, 0x0017, body_0017, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0017 mode=%s plain=%d wire=%d town=%d area=%d pos=(%d,%d)"
                % (
                    flag,
                    used,
                    len(body_0017),
                    len(pkt),
                    snap["town_id"],
                    snap["area_id"],
                    snap["pos_x"],
                    snap["pos_y"],
                )
            )

        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_0018", True):
            body_0018 = build_area_users(
                character_id=user_id,
                town_id=snap["town_id"],
                area_id=snap["area_id"],
                pos_x=snap["pos_x"],
                pos_y=snap["pos_y"],
                direction=snap["direction"],
                state=snap["state"],
            )
            flag = _env_int("USDOF_GAME_DUNGEON_LEAVE_0018_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_LEAVE_0018_MODE", "codec")
            pkt, used = self._emit(flag, 0x0018, body_0018, mode)
            packets.append(pkt)
            notes.append(
                "S%d/0018 mode=%s plain=%d wire=%d"
                % (flag, used, len(body_0018), len(pkt))
            )

        if _env_bool("USDOF_GAME_DUNGEON_LEAVE_SEND_00CA", True):
            body_00ca = b"\x00"
            flag = _env_int("USDOF_GAME_DUNGEON_LEAVE_00CA_FLAG", 0) & 0xFF
            mode = os.environ.get("USDOF_GAME_DUNGEON_LEAVE_00CA_MODE", "codec")
            pkt, used = self._emit(flag, 0x00CA, body_00ca, mode)
            packets.append(pkt)
            notes.append(
                "S%d/00CA mode=%s plain=%d wire=%d"
                % (flag, used, len(body_00ca), len(pkt))
            )

        leave_kind = "settlement" if settlement else ("in_run" if in_run else "select_or_town")

        # 副本内/结算回城：补 USERINFO0 + USERINFO1 + 000E list3（防装备槽清空）
        self._append_town_equip_resync(
            packets, notes, cid=cid, leave_kind=leave_kind
        )

        # 教程本离开 → 标记 tutorial_state=2（对照 90qiyuan tutorial_completed=1）
        try:
            _tut = load_chain("helper-tutorial_dungeon.py")
            if was_tutorial and cid is not None and in_run:
                repo = self.server._repo()
                ok = _tut.mark_tutorial_complete(repo, int(cid))
                _tut.bind_session_tutorial_flags(session, dungeon_id=0, active=False)
                complete_bit = "tutorial_complete=%s cid=%s dungeon=%s" % (
                    "ok" if ok else "fail",
                    cid,
                    run_dungeon,
                )
                notes.append(complete_bit)
                if tutorial_complete_note.startswith("tutorial_return_seria"):
                    tutorial_complete_note = (
                        tutorial_complete_note + "; " + complete_bit
                    )
                else:
                    tutorial_complete_note = complete_bit
        except Exception as tut_exc:
            tutorial_complete_note = "tutorial_complete_err=%r" % (tut_exc,)
            notes.append(tutorial_complete_note)

        note = (
            "leave_kind=%s phase_was=%s; " % (leave_kind, clear_phase)
        )
        note = note + (
            "C1/%04X leave_select plain_len=%s cid=%s gate=ON restore town=%d area=%d "
            "pos=(%d,%d) reply=[%s]; %s; doc_ref=副本实现A 0003+0017+0018+00CA; "
            "chain_file=game/chains/flow-dungeon_leave_select.py"
            % (
                int(opcode) & 0xFFFF,
                len(body or b""),
                cid,
                snap["town_id"],
                snap["area_id"],
                snap["pos_x"],
                snap["pos_y"],
                ", ".join(notes),
                tutorial_complete_note,
            )
        )
        return packets, note

    def handle_0084(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        try:
            return self.handle_leave(body, session=session, opcode=0x0084)
        except Exception as exc:
            return [], (
                "C1/0084 leave_select ERROR %s; chain_file=game/chains/flow-dungeon_leave_select.py"
                % exc
            )

    def handle_002a(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        try:
            return self.handle_leave(body, session=session, opcode=0x002A)
        except Exception as exc:
            return [], (
                "C1/002A leave_select ERROR %s; chain_file=game/chains/flow-dungeon_leave_select.py"
                % exc
            )
