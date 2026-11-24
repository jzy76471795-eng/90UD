"""C1/0x0027 DIE_MONSTER — 副本实现A.md §5.5.2 击杀链.

Friend 顺序（权威，与同客户端 DFOUSServer / 副本实现A 对齐）::

  1. 按 seq 查 dungeon_room_mons
  2. 有效击杀只消费房间 actor 发布前已物化的地面掉落结果
  3. 普通 actor 发 S0/0x0026 DIE_MONSTER（可带 116B drop 行；防卡怪）
     客户端动作创建的 passive object 保留本地 ``[last action]``，不强制消亡
  4. 若不在 run / 未知 seq / 已 killed → 提前返回
  5. 标记 killed=true
  6. 经验：落库 + S0/0x0025 EXP（服务端权威累计值）
  7. 升级 skillinfo：默认 **不发** S0/0013
  8. BOSS 终死时，同房已发布的存活敌对小怪复用本链立即消亡
  9. 房清 + boss 条件 → S0/0x001F ENABLE_CLEAR
 10. 可选任务怪掉落 bridge（与地面掉落独立）

S0/0025 语义（业务对齐 副本实现A；包体以 Stage3 IDA sub_166CB70 为准）::

  - layout 默认 **stage3_78**（IDA 完整读序的空 pair 78B；旧布局仅显式 A/B）
  - body 写 **累计 total_exp** + SP0/SP1/TP0/TP1 绝对余额 + PVP=0
  - TCP wire 走 idx9 codec；客户端入队前解密后交给 flag0 reader
"""
from __future__ import annotations
from game.raid.local_bindings import local_raid_hooks

import os
import random
from contextlib import nullcontext
from typing import Any

from game.chain_loader import load_chain
from game.normal_party_run import note_actor_death as note_party_actor_death
from game.normal_party_run import owned_death_row as owned_party_death_row
from game.character.initial_skills import get_initial_skills_for_job
from game.character.payloads import build_skillinfo_from_pvf
from game.quest.quest_common import env_bool
from control.contracts import ControlApiError
from game.quest.quest_clear import DungeonClearContext, persist_clear_map_progress
from game.quest.quest_index import get_quest_index
from game.quest.quest_residual import (
    apply_kill_to_active_quests,
    apply_seeking_to_active_quests,
    consume_seeking_capture_ground_drop,
    normalize_enemy_rank,
    resolve_seeking_capture_target_die,
)
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_levelup_acceptable_quest_refresh,
    pack_quest_refresh_packets,
    pack_set_trigger_ack_packets,
    resolve_selected_character_detail,
)
from game.drop.room_drop_preload import consume_preloaded_actor_drops
from game.drop.room_drop_preload import preload_room_drops_before_visibility
from game.drop.drop_service import create_and_register_session_item_drop

_payloads = load_chain("helper-dungeon_payloads.py")
_altar_payloads = load_chain("helper-altar-payloads.py")
_exp = load_chain("helper-character_exp.py")
_activity_state = load_chain("helper-activity_state.py")
_sp = load_chain("helper-skill_points.py")
_level_titles = load_chain("helper-level_milestone_titles.py")
_room_state = load_chain("helper-dungeon_room_state.py")
_start_room = load_chain("helper-dungeon_start_room.py")
_map_mons = load_chain("helper-dungeon_map_monsters.py")
_ally_companions = load_chain("helper-dungeon_ally_companions.py")
_hell_party = load_chain("helper-dungeon_hell_party.py")
_special_hell = load_chain("helper-dungeon_special_hell.py")
_clear_conditions = load_chain("helper-dungeon_clear_conditions.py")
_special_conditions = load_chain("helper-dungeon_special_conditions.py")
_tournament = load_chain("helper-dungeon_tournament.py")
_another_arad = load_chain("helper-another_arad.py")
_mirror_mission = load_chain("helper-another_arad_quest.py")
_levelup_userinfo1 = load_chain("helper-dungeon_levelup_userinfo1.py")
build_monster_die_0026 = _payloads.build_monster_die_0026
build_blood_wave_clear_ack_00ff = (
    _altar_payloads.build_blood_wave_clear_ack_00ff
)
build_enable_clear_001f = _payloads.build_enable_clear_001f
build_complete_condition_pass_gate_0138_us = (
    _payloads.build_complete_condition_pass_gate_0138_us
)
build_exp_notification_0025 = _payloads.build_exp_notification_0025
build_item_list_update_000e = _payloads.build_item_list_update_000e
compute_kill_exp = _exp.compute_kill_exp
grant_exp_to_character = _exp.grant_exp_to_character
read_character_level = _exp.read_character_level
resolve_skill_point_protocol_state = _sp.resolve_skill_point_protocol_state
SkillPointProtocolState = _sp.SkillPointProtocolState
mark_room_cleared = _room_state.mark_room_cleared
is_active_layered_map_transition = _start_room.is_active_layered_map_transition
actor_is_hostile = _map_mons.actor_is_hostile
actor_is_ally = _map_mons.actor_is_ally
actor_counts_for_combat = _map_mons.actor_counts_for_combat
is_passive_actor = _map_mons.is_passive_actor
mark_object_destroyed = _room_state.mark_object_destroyed
mark_companion_killed = _ally_companions.mark_companion_killed
mark_companion_killed_from_die = _ally_companions.mark_companion_killed_from_die
companion_progress_codes = _ally_companions.companion_progress_codes
hell_party_reward_roll_count_on_death = _hell_party.hell_party_reward_roll_count_on_death
hell_party_reward_roll_count_for_mode = _hell_party.hell_party_reward_roll_count_for_mode
is_special_hell_final_boss = _special_hell.is_special_hell_final_boss
resolve_destroy_object_clear_shadow = (
    _clear_conditions.resolve_destroy_object_clear_shadow
)
from game.shop.premium_contract_index import get_active_premium_effects
observe_dungeon_clear_condition = (
    _clear_conditions.observe_dungeon_clear_condition
)
dungeon_clear_condition_status = _clear_conditions.dungeon_clear_condition_status
dungeon_clear_condition_source = _clear_conditions.dungeon_clear_condition_source
observe_condition_actor_killed = _special_conditions.observe_condition_actor_killed
consume_complete_condition_pass_gate = (
    _special_conditions.consume_complete_condition_pass_gate
)
advance_tournament_round = _tournament.advance_tournament_round
build_tournament_clear_reward_0185 = _tournament.build_tournament_clear_reward_0185
can_accept_tournament_actor_death = _tournament.can_accept_tournament_actor_death


def _parse_die_monster_body(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" "),
        "seq": None,
        "user_id": None,
        "monster_code": None,
        "dungeon_id": None,
        "difficulty": None,
        "layout": "unknown",
        "runtime_object_key": None,
        "owner_object_key": None,
    }
    if len(raw) >= 4:
        out["runtime_object_key"] = int.from_bytes(raw[0:4], "little")
    if len(raw) >= 6:
        out["owner_object_key"] = int.from_bytes(raw[4:6], "little")
    # A two-byte body is unambiguously the Stage3 actor sequence.  Runtime
    # actor keys share the character actor table and may occupy the high half
    # of u16, so do not apply the legacy <0x8000 heuristic to this exact form.
    if len(raw) == 2:
        seq = int.from_bytes(raw, "little")
        if seq not in (0, 0xFFFF):
            out["seq"] = seq
            out["layout"] = "u16_seq"
            return out
    views = [raw]
    if len(raw) >= 5:
        views.append(raw[3:])

    for view in views:
        if len(view) >= 2:
            seq = int.from_bytes(view[0:2], "little")
            if seq > 0 and seq < 0x8000:
                out["seq"] = seq
                out["layout"] = "u16_seq"
                if len(view) >= 4:
                    out["user_id"] = int.from_bytes(view[2:4], "little")
                    out["layout"] = "u16_seq+u16_uid"
                break
        if len(view) >= 4:
            code = int.from_bytes(view[0:4], "little")
            if code > 0:
                out["monster_code"] = code
                out["layout"] = "u32_monster"
                if len(view) >= 8:
                    out["dungeon_id"] = int.from_bytes(view[4:8], "little")
                    out["layout"] = "u32_monster+u32_dungeon"
                if len(view) >= 10:
                    out["difficulty"] = int.from_bytes(view[8:10], "little")
                    out["layout"] = "u32_monster+u32_dungeon+u16_diff"
                if out.get("seq") is None:
                    out["seq"] = code & 0xFFFF
                break
    return out


def _session_skill_point_state(session: Any) -> Any:
    if session is None:
        return None
    remain_sp = getattr(session, "skill_remain_sp", None)
    remain_tp = getattr(session, "skill_remain_tp", None)
    if not isinstance(remain_sp, int) or not isinstance(remain_tp, int):
        return None
    return SkillPointProtocolState(
        page0_sp=max(0, int(remain_sp)) & 0xFFFF,
        page1_sp=max(0, int(remain_sp)) & 0xFFFF,
        page0_tp=max(0, int(remain_tp)) & 0xFFFF,
        page1_tp=max(0, int(remain_tp)) & 0xFFFF,
    )


def _resolve_kill_skill_point_state(
    *,
    level: int,
    levels_gained: int,
    character_id: int,
    repo: Any,
    session: Any,
) -> tuple[Any, str]:
    state = (
        _session_skill_point_state(session)
        if int(levels_gained) <= 0
        else None
    )
    if state is not None:
        return state, "session"
    state = resolve_skill_point_protocol_state(
        level=int(level),
        character_id=int(character_id),
        repo=repo,
        session=session,
    )
    if session is not None:
        try:
            session.skill_remain_sp = int(state.page0_sp)
            session.skill_remain_tp = int(state.page0_tp)
        except Exception:
            pass
    return state, "ledger"


def _find_mon(session: Any, seq: int | None) -> dict[str, Any] | None:
    if session is None or seq is None:
        return None
    mons = list(getattr(session, "dungeon_room_mons", None) or [])
    mons.extend(
        list(getattr(session, "dungeon_room_passive_objects", None) or [])
    )
    for mon in mons:
        try:
            if str(mon.get("kind") or "").lower() == "clear_condition_passive_shadow":
                continue
            if int(mon.get("seq") or 0) == int(seq):
                return mon
        except Exception:
            continue
    return None


def _find_mon_by_code(
    session: Any,
    code: int | None,
    *,
    prefer_passive: bool = True,
) -> dict[str, Any] | None:
    """按 object/monster code 找未击杀房间行。

    Stage3 砸毁建筑时 DIE 常走 ``u32_monster`` layout：payload 是 **object code**
    （如 52853），不是房间 seq → ``_find_mon(seq)`` 失败。
    优先未 killed 的 passive；同 code 多实例取第一个存活。
    """
    if session is None or code is None:
        return None
    try:
        c = int(code)
    except Exception:
        return None
    if c <= 0:
        return None
    mons = list(getattr(session, "dungeon_room_mons", None) or [])
    mons.extend(
        list(getattr(session, "dungeon_room_passive_objects", None) or [])
    )
    passive_hit: dict[str, Any] | None = None
    any_hit: dict[str, Any] | None = None
    for mon in mons:
        try:
            if str(mon.get("kind") or "").lower() == "clear_condition_passive_shadow":
                continue
            if bool(mon.get("killed")):
                continue
            mc = int(mon.get("code") or mon.get("monster_code") or 0)
            if mc != c:
                continue
            if any_hit is None:
                any_hit = mon
            if prefer_passive and is_passive_actor(mon):
                passive_hit = mon
                break
        except Exception:
            continue
    if prefer_passive and passive_hit is not None:
        return passive_hit
    return any_hit


def _resolve_die_mon_row(session: Any, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """DIE 解析房间 actor：先 seq，再 object code（建筑专用 layout）。"""
    mon = _find_mon(session, parsed.get("seq"))
    if mon is not None:
        return mon
    # u32_monster layout：seq 常被填成 code 低 16 位，房间表无此 seq
    code = parsed.get("monster_code")
    if code is None:
        try:
            seq = parsed.get("seq")
            if seq is not None and int(seq) > 0x8000:
                # 大 seq 更像 code 误填
                code = int(seq)
        except Exception:
            code = None
    if code is None:
        # layout 把 code 当 seq 时：用 seq 当 code 再试（52853 等）
        try:
            seq_i = int(parsed.get("seq") or 0)
            if seq_i > 0:
                code = seq_i
        except Exception:
            code = None
    mon = _find_mon_by_code(session, code, prefer_passive=True)
    if mon is None and session is not None and code is not None:
        # Keep client-created passive deaths idempotent.  The ordinary lookup
        # intentionally excludes killed rows, but a duplicate notification for
        # one of these objects must not fall through to a generic 0026 packet
        # that can interrupt its native [last action].
        for row in list(
            getattr(session, "dungeon_room_passive_objects", None) or []
        ):
            try:
                row_code = int(row.get("code") or row.get("monster_code") or 0)
            except (TypeError, ValueError):
                continue
            if (
                row_code == int(code)
                and bool(row.get("killed"))
                and _preserves_client_passive_last_action(row)
            ):
                mon = row
                break
    if mon is not None and parsed.get("monster_code") is None:
        try:
            parsed["monster_code"] = int(mon.get("code") or mon.get("monster_code") or 0)
        except Exception:
            pass
    return mon


def _preserves_client_passive_last_action(mon: dict[str, Any] | None) -> bool:
    """Whether a client-created passive must finish its native death action."""
    if not mon or not is_passive_actor(mon):
        return False
    source_kind = str(mon.get("passive_source_kind") or "").strip().lower()
    if source_kind in {"action_created", "randomized_energy"} and not bool(mon.get("wire_visible", True)):
        return True
    # Frozen Energy Containment walls create hatchery warps; the large
    # transmission has the barrier terminal action. They are static geometry,
    # bound as passive shadows, never S001D actors. A code-as-sequence S0026
    # must not terminate that action. Keep ordinary passives unchanged.
    return (not mon.get("seq") and not mon.get("runtime_object_key")
            and (mon.get("runtime_map_index"), mon.get("code")) in {
                (19103, 18863), (19119, 18892), (19116, 18893), (19100, 18862),
                (19120, 18864)})


def _resolve_quest_drop_scope(
    session: Any,
    parsed: dict[str, Any] | None,
) -> tuple[int, int]:
    """任务掉落匹配用的 (dungeon_id, difficulty)。

    **优先 session 当前副本**（S4A12 QuestDropService 用 dungeonIndex，不读 DIE 包）。

    Stage3 建筑 DIE 常走 ``u32_monster+u32_dungeon+u16_diff``：包体第二 u32
    **不是** PVF 副本 id（常见是 map index / 其它字段）。若优先 parsed.dungeon_id，
    会把 ``[enemy reward item] dungeon=160`` 滤成 none（1849 砸 52853 即此病）。
    普通怪 DIE 走 ``u16_seq`` 无 dungeon 字段 → 旧代码碰巧走 session 才正常。
    """
    parsed = parsed or {}
    dungeon_id = -1
    if session is not None:
        for attr in ("cur_dungeon_id", "selected_dungeon_id"):
            try:
                val = getattr(session, attr, None)
                if val is not None and int(val) > 0:
                    dungeon_id = int(val)
                    break
            except Exception:
                continue
    if dungeon_id <= 0:
        try:
            pkt = parsed.get("dungeon_id")
            if pkt is not None and int(pkt) > 0:
                dungeon_id = int(pkt)
        except Exception:
            pass
    if dungeon_id <= 0:
        dungeon_id = -1

    difficulty = -1
    if session is not None:
        for attr in (
            "cur_dungeon_difficulty",
            "selected_difficulty",
            "dungeon_difficulty",
        ):
            try:
                val = getattr(session, attr, None)
                if val is not None:
                    difficulty = int(val)
                    break
            except Exception:
                continue
    if difficulty < 0:
        try:
            if parsed.get("difficulty") is not None:
                difficulty = int(parsed.get("difficulty"))
        except Exception:
            difficulty = -1
    return int(dungeon_id), int(difficulty)


def _cid(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    cid = int(val)
                    if cid > 0:
                        return cid
                except Exception:
                    pass
    try:
        detail = resolve_selected_character_detail(server, session)
        cid = detail.get("character_id")
        if cid is not None and int(cid) > 0:
            return int(cid)
    except Exception:
        pass
    return None


class DieMonster0027Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        with self._stage("codec"):
            return self.server._codec_response(flag, opcode, body), "codec"

    def _stage(self, name: str) -> Any:
        metrics = getattr(self.server, "hotpath_metrics", None)
        return metrics.stage(name) if metrics is not None else nullcontext()

    def _append_empty_death_projection(
        self,
        packets: list[bytes],
        notes: list[str],
        *,
        seq: int,
        reason: str,
    ) -> None:
        """Append the confirmed zero-drop S0/0026 terminal projection."""
        seq_i = int(seq)
        if seq_i <= 0 or seq_i > 0xFFFF:
            notes.append("0026=skip_invalid_seq seq=%s reason=%s" % (seq_i, reason))
            return
        with self._stage("packet_build"):
            body_0026 = build_monster_die_0026(seq_i)
        pkt26, mode26 = self._emit(0, 0x0026, body_0026, "codec")
        packets.append(pkt26)
        notes.append(
            "0026/%s/len=%d seq=%d drops=0 reason=%s"
            % (mode26, len(body_0026), seq_i, reason)
        )

    @staticmethod
    def _boss_cleanup_target_seqs(
        session: Any,
        defeated_mon: dict[str, Any],
        *,
        layered_scene_active: bool,
    ) -> tuple[tuple[int, ...], str]:
        """Select ordinary live mobs retired by a terminal BOSS death.

        Stage3 identifies the ordinary BOSS ranks as monster type 3 and APC
        type 8.  Modes with their own wave/floor owner keep their established
        progression, while ordinary rooms reuse this handler for every
        already-published hostile small monster.
        """

        defeated_type = int(
            defeated_mon.get("type") or defeated_mon.get("mon_type") or 0
        )
        if defeated_type not in (3, 8):
            return (), ""
        if int(getattr(session, "dungeon_clear_phase", 0) or 0) != 0:
            return (), "boss_cleanup=skip clear_phase"
        if layered_scene_active:
            return (), "boss_cleanup=skip layered_scene"
        if bool(getattr(session, "altar_active", False)):
            return (), "boss_cleanup=skip owner=altar"
        if (
            bool(defeated_mon.get("tournament_actor"))
            or bool(getattr(session, "dungeon_tournament_active", False))
            or bool(getattr(session, "dungeon_tournament_completed", False))
        ):
            return (), "boss_cleanup=skip owner=tournament"
        tower_kind = str(getattr(session, "dungeon_tower_kind", "") or "").strip()
        if tower_kind:
            return (), "boss_cleanup=skip owner=tower:%s" % tower_kind
        if bool(defeated_mon.get("is_hell_party_actor")):
            return (), "boss_cleanup=skip owner=hell_party"
        if bool(getattr(session, "another_arad_active", False)):
            try:
                is_mirror_endpoint = (
                    _another_arad.is_session_another_arad_boss_endpoint(
                        session,
                        cell_x=int(getattr(session, "dungeon_cell_x", 0) or 0),
                        cell_y=int(getattr(session, "dungeon_cell_y", 0) or 0),
                        map_index=int(
                            getattr(session, "dungeon_map_index", 0) or 0
                        ),
                    )
                )
            except Exception:
                is_mirror_endpoint = False
            if not is_mirror_endpoint:
                return (), "boss_cleanup=skip owner=another_arad_non_endpoint"

        targets: set[int] = set()
        for actor in list(getattr(session, "dungeon_room_mons", None) or []):
            if actor is defeated_mon or bool(actor.get("killed")):
                continue
            if not bool(actor.get("wire_visible", True)):
                continue
            if not bool(actor.get("is_blocking", True)):
                continue
            if not bool(actor.get("killable", True)):
                continue
            if bool(actor.get("is_hell_party_actor")):
                continue
            if not actor_is_hostile(actor) or not actor_counts_for_combat(actor):
                continue
            actor_type = int(actor.get("type") or actor.get("mon_type") or 0)
            if actor_type in (3, 8):
                continue
            try:
                seq = int(actor.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if 0 < seq <= 0xFFFF:
                targets.add(seq)
        return tuple(sorted(targets)), "boss_cleanup=targets:%d" % len(targets)

    def _force_boss_room_cleanup(
        self,
        *,
        session: Any,
        defeated_mon: dict[str, Any],
        layered_scene_active: bool,
    ) -> tuple[list[bytes], str]:
        targets, selection_note = self._boss_cleanup_target_seqs(
            session,
            defeated_mon,
            layered_scene_active=layered_scene_active,
        )
        if not selection_note or not targets:
            return [], selection_note

        packets: list[bytes] = []
        killed: list[int] = []
        skipped: list[int] = []
        for seq in targets:
            reply, _note = self.handle_die_monster_0027(
                int(seq).to_bytes(2, "little"),
                session=session,
                defer_altar_progression=True,
                defer_room_clear=True,
            )
            packets.extend(reply)
            actor = _find_mon(session, seq)
            if actor is not None and bool(actor.get("killed")):
                killed.append(seq)
            else:
                skipped.append(seq)
        return packets, (
            "boss_cleanup targets=%d killed=%d skipped=%d killed_seqs=%s "
            "skipped_seqs=%s"
            % (
                len(targets),
                len(killed),
                len(skipped),
                killed,
                skipped,
            )
        )

    def handle_control_kill_all_monsters(
        self,
        *,
        session: Any,
    ) -> tuple[list[bytes], dict[str, Any], str]:
        """Kill every live hostile blocking actor in the current room.

        This intentionally reuses the established C1/0027 death service for
        each proven room sequence number.  The control plane never invents an
        S2C death packet, so drops, quest residuals, experience, and clear
        gates remain on the ordinary authoritative chain.
        """

        if session is None or not bool(getattr(session, "in_dungeon_run", False)):
            raise ControlApiError("NOT_IN_DUNGEON", "current client is not in a dungeon")
        if not bool(getattr(session, "dungeon_finish_loading", False)):
            raise ControlApiError('DUNGEON_LOADING', 'dungeon is still loading')
        if int(getattr(session, "dungeon_clear_phase", 0) or 0) >= 1:
            raise ControlApiError("DUNGEON_SETTLING", "dungeon clear or settlement is active")

        raid_hooks = local_raid_hooks(self.server)
        if raid_hooks is not None and getattr(session, 'selected_channel_id', None) in (23, 32):
            binding = raid_hooks.bridge.current(session)
            if binding is None:
                raise ControlApiError('RAID_RUN_UNAVAILABLE', 'owned raid run is unavailable')
            if not raid_hooks._group(binding).accepted:
                raise ControlApiError('DUNGEON_LOADING', 'dungeon is still loading')

        target_rows: list[tuple[int, dict[str, Any]]] = []
        for mon in list(getattr(session, "dungeon_room_mons", None) or []):
            try:
                seq = int(mon.get("seq") or 0)
            except Exception:
                continue
            if seq <= 0 or bool(mon.get("killed")):
                continue
            # "all monsters" is the room-clear combat population.  This
            # excludes allies, neutral actors, and passive quest objects.
            if not bool(mon.get("is_blocking", True)):
                continue
            if not actor_is_hostile(mon) or not actor_counts_for_combat(mon):
                continue
            target_rows.append((seq, mon))

        # Hell Party rows are sent to Stage3 as hidden, ordered templates.  A
        # single control call must not remove later orders before the client
        # has promoted those templates into the current visible wave.
        regular_rows = [
            row for row in target_rows if not bool(row[1].get("is_hell_party_actor"))
        ]
        hell_rows = [
            row for row in target_rows if bool(row[1].get("is_hell_party_actor"))
        ]
        target_scope = "room"
        deferred_hell_count = 0
        if regular_rows and hell_rows:
            target_rows = regular_rows
            target_scope = "non_hell"
            deferred_hell_count = len(hell_rows)
        elif hell_rows:
            current_order = min(
                int(mon.get("hell_party_order") or 0) for _seq, mon in hell_rows
            )
            target_rows = [
                row
                for row in hell_rows
                if int(row[1].get("hell_party_order") or 0) == current_order
            ]
            target_scope = "hell_order_%d" % current_order
            deferred_hell_count = len(hell_rows) - len(target_rows)

        targets = sorted({seq for seq, _mon in target_rows})
        packets: list[bytes] = []
        notes: list[str] = []
        altar_killed_seqs: list[int] = []
        killed = 0
        skipped = 0
        for seq in targets:
            # C1/0027's u16 sequence reader is established by the current
            # Stage3 handler; this invokes its normal server-domain chain.
            body = int(seq).to_bytes(2, "little")
            if raid_hooks is None:
                reply, note = self.handle_die_monster_0027(body, session=session,
                    defer_altar_progression=True)
            else:
                reply, note = raid_hooks.handle_opcode(0x0027,
                    self.handle_die_monster_0027, body, session=session,
                    defer_altar_progression=True)
            packets.extend(reply)
            notes.append("seq=%s %s" % (seq, note))
            mon = _find_mon(session, seq)
            if mon is not None and bool(mon.get("killed")):
                killed += 1
                if (
                    bool(getattr(session, "altar_active", False))
                    and str(mon.get("kind") or "") == "altar_wave"
                ):
                    altar_killed_seqs.append(int(seq) & 0xFFFF)
            else:
                skipped += 1

        if altar_killed_seqs:
            altar_flow = load_chain("flow-altar-random.py").AltarRandomFlow(self.server)
            altar_round_before = int(getattr(session, "altar_round", 0) or 0)
            progression_packets, progression_note = (
                altar_flow.record_confirmed_wave_deaths(
                    altar_killed_seqs,
                    session=session,
                    source="control",
                )
            )
            altar_round_after = int(getattr(session, "altar_round", 0) or 0)
            if altar_round_after > altar_round_before:
                packets.append(
                    self.server._codec_response(
                        1,
                        0x00FF,
                        build_blood_wave_clear_ack_00ff(0),
                    ),
                )
                notes.append(
                    "altar_control_wave_ack S1/00FF u32=0 after_deaths=1 "
                    "round=%d->%d" % (altar_round_before, altar_round_after)
                )
            packets.extend(progression_packets)
            notes.append("altar_control_progress %s" % progression_note)

        result = {
            "killed_count": killed,
            "skipped_count": skipped,
            "target_count": len(targets),
            "cell": {
                "x": int(getattr(session, "dungeon_cell_x", 0) or 0),
                "y": int(getattr(session, "dungeon_cell_y", 0) or 0),
            },
            "clear_phase": int(getattr(session, "dungeon_clear_phase", 0) or 0),
        }
        return packets, result, "control kill_all targets=%s scope=%s deferred_hell=%s; %s" % (
            len(targets),
            target_scope,
            deferred_hell_count,
            " | ".join(notes) if notes else "no_live_hostile_blocking_actor",
        )

    def _quest_drop_packets(
        self,
        parsed: dict[str, Any],
        *,
        session: Any,
        base: str,
        enemy_type: int = 0,
    ) -> tuple[list[bytes], str]:
        with self._stage("quest_processing"):
            return self._quest_drop_packets_impl(
                parsed,
                session=session,
                base=base,
                enemy_type=enemy_type,
            )

    def _quest_drop_packets_impl(
        self,
        parsed: dict[str, Any],
        *,
        session: Any,
        base: str,
        enemy_type: int = 0,
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_DUNGEON_QUEST_MONSTER_DROP", True):
            return [], base + "; quest_drop=OFF"
        mon_row = _resolve_die_mon_row(session, parsed)
        monster = parsed.get("monster_code")
        if monster is None and mon_row is not None:
            monster = mon_row.get("code") or mon_row.get("monster_code")
        # 建筑 DIE layout 常把 code 放在 seq 字段
        if (monster is None or int(monster) <= 0) and parsed.get("seq") is not None:
            try:
                seq_as_code = int(parsed.get("seq") or 0)
                if seq_as_code > 0 and mon_row is None:
                    # 仅当 seq 不像正常房间序号时才当 code
                    if seq_as_code >= 0x1000 or _find_mon(session, seq_as_code) is None:
                        monster = seq_as_code
            except Exception:
                pass
        if monster is None or int(monster) <= 0:
            return [], base + "; quest_drop=no_monster"
        cid = _cid(session, self.server)
        if cid is None:
            return [], base + "; quest_drop=no_cid"
        repo = self.server._repo()
        active_rows = repo.list_active_quests(int(cid))
        active_ids = [int(q) for _s, q, _t in active_rows]
        # 副本 scope 必须优先 session（建筑 DIE 包体 dungeon 字段不可信）
        dungeon_id, difficulty = _resolve_quest_drop_scope(session, parsed)
        qidx = get_quest_index()
        quest_drop_rate_bonus = 0
        try:
            premium_effects = get_active_premium_effects(
                repo,
                int(getattr(self.server, "account_id", 1) or 1),
                session=session,
            )
            quest_drop_rate_bonus = max(
                0, int(premium_effects.quest_item_drop_rate_percent)
            )
        except Exception:
            premium_effects = None
        # enemy domain：1=monster 2=ai 3=passive（S4A12 QuestDropProvider）
        etype = int(enemy_type or 0)
        if etype <= 0 and mon_row is not None:
            etype = int(mon_row.get("quest_enemy_type") or 0)
            if etype <= 0 and is_passive_actor(mon_row):
                etype = 3
            elif etype <= 0:
                kind = str(mon_row.get("kind") or "").lower()
                mon_type = int(mon_row.get("type") or mon_row.get("mon_type") or 0)
                if kind in {"ai_character", "ai", "apc"} or mon_type in {5, 6, 7, 8}:
                    etype = 2
                elif mon_type == 9:
                    etype = 3
                else:
                    etype = 1
        # 通用：monster_reward ∪ enemy_reward（含 type=1 写在 enemy 表的 seeking 主线）
        drops, reward_src = qidx.resolve_quest_kill_drops(
            active_ids,
            source_code=int(monster),
            enemy_type=int(etype),
            dungeon_id=dungeon_id,
            difficulty=difficulty,
        )
        if not drops:
            return [], (
                base
                + "; quest_drop=none active=%s code=%s etype=%s dgn=%s diff=%s %s"
                % (
                    len(active_ids),
                    int(monster),
                    etype,
                    dungeon_id,
                    difficulty,
                    reward_src,
                )
            )
        # 持有量：max_stack 封顶（S4A12 QuestDropProvider.RollDrop）；tuple 第 4 项即 max
        held_map = repo.count_inventory_items_by_template(
            int(cid),
            list_type=0,
            item_ids=[int(x[0]) for x in drops if int(x[0]) > 0],
        )
        pairs: list[tuple[int, int]] = []
        for drop in drops:
            item_id = int(drop[0])
            count = int(drop[1])
            base_rate = int(drop[2])
            rate = (
                premium_effects.compute_quest_item_drop_rate(base_rate)
                if premium_effects is not None
                else max(0, min(100, base_rate))
            )
            max_stack = int(drop[3]) if len(drop) >= 4 else -1
            iid = int(item_id)
            held = int(held_map.get(iid, 0) or 0)
            ms = int(max_stack)
            if ms != -1 and held >= ms:
                continue
            # S4: for k in count: if rand(100)<rate: actual++
            actual = 0
            attempts = max(1, int(count))
            for _k in range(attempts):
                if random.randint(0, 99) < rate:
                    actual += 1
            if actual <= 0:
                continue
            if ms != -1 and held + actual > ms:
                actual = ms - held
            if actual <= 0:
                continue
            pairs.append((iid, int(actual)))
            held_map[iid] = held + int(actual)
        if not pairs:
            return [], (
                base
                + "; quest_drop=rolled_miss n=%s rate_bonus=%s%% %s"
                % (len(drops), quest_drop_rate_bonus, reward_src)
            )
        g, m, note = repo.grant_quest_items(
            int(cid), pairs, quest_id=0, reason="quest_kill_drop"
        )
        packets: list[bytes] = []
        # 实时刷背包：S0/000E 108B 条目（默认 ON；旧实现 pack 长度错误且默认关）
        if g and env_bool("USDOF_GAME_QUEST_DROP_SEND_000E", True):
            bag_by_slot: dict[int, dict] = {}
            try:
                bag_by_slot = {
                    int(r["slot_index"]): r
                    for r in repo.list_inventory_items(int(cid), list_type=0)
                }
            except Exception:
                bag_by_slot = {}
            update_rows: list[tuple[int, int, int]] = []
            for slot, item_id, _placed in g:
                row = bag_by_slot.get(int(slot))
                if row is not None:
                    stack = max(0, int(row.get("stack_count", 0) or 0)) & 0xFFFFFFFF
                    tid = int(row.get("item_template_id", item_id) or item_id) & 0xFFFFFFFF
                else:
                    stack = max(0, int(_placed)) & 0xFFFFFFFF
                    tid = int(item_id) & 0xFFFFFFFF
                update_rows.append((int(slot) & 0xFFFF, tid, stack))
            if update_rows:
                body_000e = build_item_list_update_000e(update_rows, list_type=0)
                packets.append(self.server._codec_response(0, 0x000E, body_000e))
                note = (note + "; 000E_n=%s" % len(update_rows)).strip("; ")
        # 掉落进包后同步 seeking residual
        seek_note = "seeking_skip"
        if g and env_bool("USDOF_GAME_QUEST_RESIDUAL_SEEKING", True):
            seek_packets, seek_note = self._sync_seeking_progress(
                int(cid),
                session=session,
                item_filter=[int(i) for i, _c in pairs],
            )
            packets.extend(seek_packets)
        if session is not None:
            session.last_quest_drop = {
                "monster": int(monster),
                "enemy_type": int(etype),
                "items": pairs,
                "src": reward_src,
            }
        return packets, (
            "%s; quest_drop granted=%s mailed=%s code=%s etype=%s dgn=%s diff=%s "
            "rate_bonus=%s%% %s %s; %s"
            % (
                base,
                len(g),
                len(m),
                int(monster),
                etype,
                dungeon_id,
                difficulty,
                quest_drop_rate_bonus,
                reward_src,
                note,
                seek_note,
            )
        )

    def _sync_seeking_progress(
        self,
        cid: int,
        *,
        session: Any,
        item_filter: list[int] | None = None,
    ) -> tuple[list[bytes], str]:
        repo = self.server._repo()
        active_rows = repo.list_active_quests(int(cid))
        pairs = [(int(q), int(t)) for _s, q, t in active_rows]
        held = repo.count_inventory_items_by_template(int(cid), list_type=0)
        changed = apply_seeking_to_active_quests(
            pairs,
            held_count_by_item=held,
            item_filter=item_filter,
        )
        if not changed:
            return [], "seeking=none"
        ack_pairs: list[tuple[int, int]] = []
        for qid, _old, new, _note in changed:
            repo.update_active_quest_trigger(int(cid), int(qid), int(new))
            ack_pairs.append((int(qid), int(new)))
        if session is not None:
            rows = repo.list_active_quests(int(cid))
            bind_session_quest_state(
                session,
                active_pairs=[(int(q), int(tr)) for _s, q, tr in rows],
                finished_ids=[int(q) for q, _f in repo.list_finished_quests(int(cid))],
            )
        packets: list[bytes] = []
        notes: list[str] = ["seeking n=%s" % len(changed)]
        # S1/0021：trigger→0 时客户端 sub_10434A0(qid,1) 点亮可交闪烁（023F 不够）
        if env_bool("USDOF_GAME_QUEST_RESIDUAL_SEND_0021", True) and ack_pairs:
            ack_pkts, ack_note = pack_set_trigger_ack_packets(self.server, ack_pairs)
            packets.extend(ack_pkts)
            notes.append(ack_note)
        # 023F 默认 OFF：全量 rebuild 可能冲掉刚点亮的动效
        if env_bool("USDOF_GAME_QUEST_RESIDUAL_REFRESH_023F", False):
            detail = resolve_selected_character_detail(self.server, session)
            refresh, rnote = pack_quest_refresh_packets(
                self.server,
                character_id=int(cid),
                job=int(detail.get("job") or 0),
                level=int(detail.get("level") or 1),
                grow_type=int(detail.get("grow_type") or 0),
                session=session,
                include_0015=False,
                include_023f=True,
                include_0164=False,
            )
            packets.extend(refresh)
            notes.append(rnote)
        return packets, "; ".join(notes)

    def _apply_kill_residual(
        self,
        *,
        session: Any,
        monster_code: int,
        dungeon_id: int,
        difficulty: int,
        enemy_type: int = 1,
        enemy_rank: int = -1,
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_QUEST_RESIDUAL_KILL", True):
            return [], "kill_residual=OFF"
        cid = _cid(session, self.server)
        if cid is None or int(monster_code) <= 0:
            return [], "kill_residual=skip"
        repo = self.server._repo()
        active_rows = repo.list_active_quests(int(cid))
        pairs = [(int(q), int(t)) for _s, q, t in active_rows]
        changed = apply_kill_to_active_quests(
            pairs,
            monster_code=int(monster_code),
            dungeon_id=int(dungeon_id),
            difficulty=int(difficulty),
            enemy_type=int(enemy_type),
            enemy_rank=int(enemy_rank),
        )
        if not changed:
            return [], "kill_residual=none active=%s mon=%s" % (len(pairs), monster_code)
        notes: list[str] = []
        ack_pairs: list[tuple[int, int]] = []
        for qid, _old, new, note in changed:
            repo.update_active_quest_trigger(int(cid), int(qid), int(new))
            notes.append(note)
            ack_pairs.append((int(qid), int(new)))
        if session is not None:
            rows = repo.list_active_quests(int(cid))
            bind_session_quest_state(
                session,
                active_pairs=[(int(q), int(tr)) for _s, q, tr in rows],
                finished_ids=[int(q) for q, _f in repo.list_finished_quests(int(cid))],
            )
        packets: list[bytes] = []
        # S1/0021 单任务更新：new_trigger==0 → 可交图标闪烁（IDA sub_160E080）
        if env_bool("USDOF_GAME_QUEST_RESIDUAL_SEND_0021", True) and ack_pairs:
            ack_pkts, ack_note = pack_set_trigger_ack_packets(self.server, ack_pairs)
            packets.extend(ack_pkts)
            notes.append(ack_note)
        if env_bool("USDOF_GAME_QUEST_RESIDUAL_REFRESH_023F", False):
            detail = resolve_selected_character_detail(self.server, session)
            refresh, rnote = pack_quest_refresh_packets(
                self.server,
                character_id=int(cid),
                job=int(detail.get("job") or 0),
                level=int(detail.get("level") or 1),
                grow_type=int(detail.get("grow_type") or 0),
                session=session,
                include_0015=False,
                include_023f=True,
                include_0164=False,
            )
            packets.extend(refresh)
            notes.append(rnote)
        return packets, "kill_residual %s" % ("; ".join(notes))

    def handle_die_monster_0027(
        self,
        body: bytes,
        *,
        session: Any = None,
        defer_altar_progression: bool = False,
        defer_room_clear: bool = False,
    ) -> tuple[list[bytes], str]:
        """副本实现A §5.5.2：先 0026 消亡，再经验落库 / 可选 0025 / 升级 0013 / 清房 001F。"""
        parsed = _parse_die_monster_body(body)
        combat_on = env_bool("USDOF_GAME_DUNGEON_DIE_COMBAT_HANDLE", True)
        persist_exp = env_bool("USDOF_GAME_DUNGEON_DIE_PERSIST_EXP", True)
        base = (
            "C1/0027 DIE_MONSTER layout=%s len=%s seq=%s mon=%s combat=%s persist_exp=%s"
            % (
                parsed["layout"],
                parsed.get("raw_len"),
                parsed.get("seq"),
                parsed.get("monster_code"),
                int(combat_on),
                int(persist_exp),
            )
        )

        packets: list[bytes] = []
        notes: list[str] = []
        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        shared_row = owned_party_death_row(self.server, session) if in_run else None
        seq = parsed.get("seq") if shared_row is None else shared_row.get("seq")
        layered_scene_active = False
        if in_run:
            try:
                layered_scene_active = is_active_layered_map_transition(
                    session,
                    dungeon_id=int(
                        getattr(session, "selected_dungeon_id", 0)
                        or getattr(session, "cur_dungeon_id", 0)
                        or 0
                    ),
                    maze_index=int(
                        getattr(session, "dungeon_maze_index", 0) or 0
                    ),
                    cell_x=int(getattr(session, "dungeon_cell_x", 0) or 0),
                    cell_y=int(getattr(session, "dungeon_cell_y", 0) or 0),
                    map_index=int(getattr(session, "dungeon_map_index", 0) or 0),
                )
            except Exception:
                layered_scene_active = False

        if not combat_on:
            notes.append("combat=OFF")
            if seq is not None:
                self._append_empty_death_projection(
                    packets,
                    notes,
                    seq=int(seq),
                    reason="combat_disabled_terminal_reply",
                )
            q_packets, q_note = self._quest_drop_packets(parsed, session=session, base=base)
            packets.extend(q_packets)
            notes.append(q_note.split(";")[-1].strip() if ";" in q_note else q_note)
            return packets, "%s; reply=[%s]; chain_file=game/chains/opcode0027-die_monster.py" % (
                base,
                ", ".join(notes),
            )

        if shared_row is None and (seq is None or int(seq) <= 0):
            notes.append("skip_combat no_seq")
            q_packets, q_note = self._quest_drop_packets(parsed, session=session, base=base)
            packets.extend(q_packets)
            notes.append(q_note.split(";")[-1].strip() if ";" in q_note else q_note)
            return packets, "%s; reply=[%s]; chain_file=game/chains/opcode0027-die_monster.py" % (
                base,
                ", ".join(notes),
            )

        seq_i = int(seq or 0)

        # 预取 mon / 已物化掉落（只消费缓存再发 0026；未知 seq 仍发空 0026 防卡怪）
        # 建筑 DIE 可能是 u32_monster(code) 而非 u16_seq → 用 _resolve_die_mon_row
        clear_phase = (
            int(getattr(session, "dungeon_clear_phase", 0) or 0) if session is not None else 0
        )
        mon = shared_row
        if in_run and mon is None:
            mon, clear_shadow_note = resolve_destroy_object_clear_shadow(
                session,
                parsed,
            )
            if mon is not None:
                notes.append(clear_shadow_note)
            else:
                if clear_shadow_note != "clear_shadow=skip_no_state":
                    notes.append(clear_shadow_note)
                mon = _resolve_die_mon_row(session, parsed)
        if mon is not None and parsed.get("monster_code") is None:
            try:
                parsed["monster_code"] = int(
                    mon.get("code") or mon.get("monster_code") or 0
                )
            except Exception:
                pass
        mon_level = 1
        mon_type = 0
        mon_code = int(parsed.get("monster_code") or 0)
        # 解析到 mon 后，0026 消亡应用 mon 的真实 seq（不是 code 误填）
        try:
            mon_seq = int(mon.get("seq") or 0) if mon is not None else 0
            if mon_seq > 0:
                seq_i = mon_seq
                parsed["seq"] = mon_seq
        except Exception:
            pass
        if mon is not None and bool(mon.get("tournament_actor")):
            allowed, tournament_gate_note = can_accept_tournament_actor_death(
                session,
                actor=mon,
            )
            if not allowed:
                notes.append(tournament_gate_note)
                # Control death can arrive before the client's native terminal
                # event. Reproject an already-dead actor without repeating any
                # rewards or bracket transition.
                if bool(mon.get("killed")):
                    self._append_empty_death_projection(
                        packets,
                        notes,
                        seq=seq_i,
                        reason="tournament_terminal_replay",
                    )
                return packets, (
                    "%s; reply=[%s]; chain_file=game/chains/opcode0027-die_monster.py"
                    % (base, ", ".join(notes))
                )
        difficulty = 0
        dungeon_id = -1
        drop_rows: list = []
        owner_actor = 0
        if session is not None:
            try:
                owner_actor = int(
                    getattr(session, "user_id", None)
                    or getattr(session, "actor_id", None)
                    or getattr(session, "selected_character_id", None)
                    or 0
                ) & 0xFFFF
            except Exception:
                owner_actor = 0

        companion_die_code = 0
        if in_run:
            try:
                companion_die_code = int(
                    mark_companion_killed_from_die(
                        session,
                        # Native actors resolve by code.  Sequence fallback is
                        # reserved for a client-carried ally absent from this
                        # room's server-side actor table; room seq values can
                        # otherwise be reused by an unrelated actor.
                        seq=seq_i if mon is None else 0,
                        monster_code=mon_code,
                    )
                    or 0
                )
            except Exception:
                companion_die_code = 0
            if companion_die_code > 0:
                notes.append(
                    "ally_companion_killed code=%d source=die_identity"
                    % companion_die_code
                )

        # 敌友分流（通用）：队友/中立 APC 仍可收到客户端 DIE 并消亡，
        # 但禁止走掉落/经验。可破坏建筑（type9）走 combat 链以计 hunt residual，
        # 建筑不走 monseter 池，改走独立 object + DGN 专属材料池。
        # 真源：map [ai character] faction / [monster team] / [passive object]。
        mon_hostile = actor_is_hostile(mon) if mon is not None else False
        mon_ally = actor_is_ally(mon) if mon is not None else False
        mon_combat = actor_counts_for_combat(mon) if mon is not None else False
        mon_passive = is_passive_actor(mon) if mon is not None else False
        preserve_passive_last_action = _preserves_client_passive_last_action(mon)
        clear_condition_matched = False
        clear_condition_complete = False

        # 友方同伴死亡：会话标记 alive=False，后续 MOVE_MAP 不再跨房注入
        if mon is not None and in_run and mon_ally and not mon_hostile:
            try:
                c_code = int(
                    mon.get("code") or mon.get("monster_code") or mon.get("script_id") or 0
                )
            except Exception:
                c_code = 0
            if (
                c_code > 0
                and companion_die_code <= 0
                and mark_companion_killed(session, c_code)
            ):
                notes.append("ally_companion_killed code=%d" % c_code)

        can_combat = (
            in_run
            and mon is not None
            and not bool(mon.get("killed"))
            and mon_combat
            and not (
                clear_phase >= 2
                and env_bool("USDOF_GAME_DUNGEON_DIE_IGNORE_AFTER_SETTLE", True)
            )
        )
        if mon is not None and in_run and not mon_combat:
            notes.append(
                "non_hostile_actor role=%s faction=%s ally=%s hostile=%s skip_combat_rewards"
                % (
                    mon.get("role") or "",
                    mon.get("faction") or "",
                    int(mon_ally),
                    int(mon_hostile),
                )
            )
        if can_combat and mon is not None:
            mon_level = int(mon.get("level") or 1)
            mon_type = int(mon.get("type") or mon.get("mon_type") or 0)
            mon_code = int(
                mon.get("code")
                or mon.get("monster_code")
                or parsed.get("monster_code")
                or 0
            )
            if mon_code > 0:
                parsed["monster_code"] = mon_code
            difficulty = int(
                getattr(session, "selected_difficulty", None)
                or getattr(session, "cur_dungeon_difficulty", 0)
                or 0
            ) & 0xFF
            dungeon_id = int(
                parsed.get("dungeon_id")
                or getattr(session, "cur_dungeon_id", -1)
                or getattr(session, "selected_dungeon_id", -1)
                or -1
            )
            # Ground-drop rolls were materialized before this room's actor packet
            # (or before a conditional summon became visible). DIE only consumes
            # that cache.
            if mon_passive:
                if preserve_passive_last_action:
                    # The object already exists and dies in the client.  Its PVF
                    # [last action] may create the rescued/converted actor, so a
                    # server DIE notification must not retire it prematurely.
                    notes.append("object_drop=skip_client_last_action")
                elif env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
                    try:
                        with self._stage("drop_preload_consume"):
                            drop_result, preload_note = (
                                consume_preloaded_actor_drops(
                                    session,
                                    mon,
                                )
                            )
                        drop_rows = list(drop_result.drops or [])
                        notes.append(
                            "object_drop n=%d gold=%s seed=%s->%s %s; %s"
                            % (
                                len(drop_rows),
                                drop_result.gold_amount,
                                drop_result.seed_before,
                                drop_result.seed_after,
                                drop_result.note,
                                preload_note,
                            )
                        )
                    except Exception as exc:
                        notes.append("object_drop_err=%r" % (exc,))
                        drop_rows = []
                else:
                    notes.append("object_drop=OFF")
            elif env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
                try:
                    hell_reward_roll_count = int(
                        mon.get("hell_reward_roll_count") or 0
                    )
                    if bool(mon.get("is_hell_party_actor")):
                        hell_reward_roll_count = hell_party_reward_roll_count_on_death(
                            session,
                            mon,
                        )
                    special_hell_final_boss = is_special_hell_final_boss(session, mon)
                    if special_hell_final_boss:
                        hell_party_mode = int(
                            getattr(session, "dungeon_special_hell_party_mode", 1) or 1
                        )
                        hell_reward_roll_count = hell_party_reward_roll_count_for_mode(
                            hell_party_mode
                        )
                        notes.append(
                            "special_hell_final_boss=1 mode=%d rolls=%d wish_items=%d"
                            % (
                                hell_party_mode,
                                hell_reward_roll_count,
                                len(getattr(session, "dungeon_wish_item_ids", ()) or ()),
                            )
                        )

                    with self._stage("drop_preload_consume"):
                        drop_result, preload_note = consume_preloaded_actor_drops(
                            session,
                            mon,
                            release_group_reward=hell_reward_roll_count > 0,
                            special_hell_final_boss=special_hell_final_boss,
                        )
                    drop_rows = list(drop_result.drops or [])
                    notes.append(
                        "item_drop n=%d gold=%s seed=%s->%s %s; %s"
                        % (
                            len(drop_rows),
                            drop_result.gold_amount,
                            drop_result.seed_before,
                            drop_result.seed_after,
                            drop_result.note,
                            preload_note,
                        )
                    )
                except Exception as exc:
                    notes.append("item_drop_err=%r" % (exc,))
                    drop_rows = []

        # Capture tasks use the same terminal-event ownership as ordinary
        # QuestDrop sources. Older paths may have armed the exact target from
        # C1/002C; native Stage3 throw scripts do not emit that packet, so a
        # verified active capture quest may also claim its PVF catch-item DIE.
        if can_combat and mon is not None:
            capture, capture_note = consume_seeking_capture_ground_drop(
                session,
                target_seq=int(seq_i),
                target_code=int(mon_code),
                target_actor=mon,
            )
            if capture is None and capture_note == "capture_drop=none":
                try:
                    cid = _cid(session, self.server)
                    active_pairs = []
                    if cid is not None:
                        active_pairs = [
                            (int(quest_id), int(trigger))
                            for _slot, quest_id, trigger in self.server._repo().list_active_quests(
                                int(cid)
                            )
                        ]
                    capture, capture_note = resolve_seeking_capture_target_die(
                        active_pairs,
                        session=session,
                        target_seq=int(seq_i),
                        target_code=int(mon_code),
                        target_actor=mon,
                    )
                except Exception as exc:
                    capture = None
                    capture_note = "capture_death=error type=%s" % type(exc).__name__
            if capture is not None:
                try:
                    capture_drop = create_and_register_session_item_drop(
                        session,
                        template_id=int(capture.spec.output_item_id),
                        stack_count=int(capture.spec.output_count),
                        source="seeking_capture",
                        quest_id=int(capture.spec.quest_id),
                    )
                    if capture_drop is None:
                        notes.append("capture_drop=error scene_slot_unavailable")
                    else:
                        drop_rows.append(capture_drop)
                        notes.append(
                            "%s ground=%d@%d count=%d"
                            % (
                                capture_note,
                                int(capture_drop.template_id),
                                int(capture_drop.wire_object_key),
                                int(capture_drop.stack_count),
                            )
                        )
                except Exception as exc:
                    notes.append("capture_drop=error type=%s" % type(exc).__name__)
            elif capture_note not in {"capture_drop=none", "capture_death=none"}:
                notes.append(capture_note)

        # §5.5.2 step 2: 普通 actor 发 0026 消亡；客户端动作创建的
        # passive object 自己执行 [last action]，强制消亡会截断获救等演出。
        if shared_row is not None and (not shared_row.get("wire_visible", True) or seq_i <= 0):
            notes.append("0026=skip_non_wire_shared_actor")
        elif preserve_passive_last_action:
            notes.append(
                "0026=skip_client_last_action code=%d" % int(mon_code or seq_i)
            )
        else:
            with self._stage("packet_build"):
                body_0026 = build_monster_die_0026(
                    seq_i,
                    drops=drop_rows,
                    owner_actor_id=owner_actor,
                )
            pkt26, mode26 = self._emit(0, 0x0026, body_0026, "codec")
            packets.append(pkt26)
            notes.append(
                "0026/%s/len=%d seq=%d drops=%d"
                % (mode26, len(body_0026), seq_i, len(drop_rows))
            )
            # 供玩家丢弃 0026 锚点：客户端此时对该 seq 实体仍有效
            if session is not None and seq_i > 0:
                try:
                    from game.drop.drop_service import note_die_entity_seq

                    note_die_entity_seq(session, seq_i)
                except Exception:
                    pass

        # §5.5.2 step 3: 不在 run / 未知 / 已杀 → 提前返回。
        # 客户端动作 passive 的 0026 已在上方按 PVF last action 语义跳过。
        if not in_run:
            notes.append("early_return not_in_run")
            q_packets, q_note = self._quest_drop_packets(parsed, session=session, base=base)
            packets.extend(q_packets)
            notes.append(q_note.split(";")[-1].strip() if ";" in q_note else q_note)
            return packets, "%s; reply=[%s]; doc_ref=副本实现A §5.5.2; chain_file=game/chains/opcode0027-die_monster.py" % (
                base,
                ", ".join(notes),
            )

        # 已结算(phase>=2)：只消亡 0026，不再发经验/二次 ENABLE_CLEAR（防重刷 boss）
        if clear_phase >= 2 and env_bool("USDOF_GAME_DUNGEON_DIE_IGNORE_AFTER_SETTLE", True):
            notes.append("early_return settled phase=%s no_exp" % clear_phase)
            return packets, (
                "%s; reply=[%s]; doc_ref=副本实现A §5.5.2; "
                "chain_file=game/chains/opcode0027-die_monster.py"
                % (base, ", ".join(notes))
            )

        if mon is None:
            notes.append("early_return seq_not_in_room_mons")
            q_packets, q_note = self._quest_drop_packets(parsed, session=session, base=base)
            packets.extend(q_packets)
            notes.append(q_note.split(";")[-1].strip() if ";" in q_note else q_note)
            return packets, "%s; reply=[%s]; doc_ref=副本实现A §5.5.2; chain_file=game/chains/opcode0027-die_monster.py" % (
                base,
                ", ".join(notes),
            )

        if bool(mon.get("killed")):
            # A repeated death notification acknowledges the same actor; it
            # must not roll another task material after the first real kill.
            notes.append("early_return already_killed quest_drop=skip_duplicate")
            return packets, "%s; reply=[%s]; doc_ref=副本实现A §5.5.2; chain_file=game/chains/opcode0027-die_monster.py" % (
                base,
                ", ".join(notes),
            )

        # §5.5.2 step 4: mark killed
        mon["killed"] = True
        raid_hooks = local_raid_hooks(self.server)
        if raid_hooks is not None:
            raid_hooks.note_actor_death(session, mon)
        if shared_row is None:
            note_party_actor_death(self.server, session, mon)
        tournament_transition = None
        if bool(mon.get("tournament_actor")) and can_combat:
            try:
                tournament_transition, tournament_note = advance_tournament_round(
                    session,
                    defeated_actor=mon,
                )
                notes.append(tournament_note)
            except Exception as exc:
                notes.append("tournament=advance_error type=%s" % type(exc).__name__)
        if layered_scene_active:
            notes.append("layered_scene=1 suppress=maze_conditions")
        else:
            condition_actor_kill_note = observe_condition_actor_killed(session, mon)
            if condition_actor_kill_note != "boss_condition=skip":
                notes.append(condition_actor_kill_note)
            if consume_complete_condition_pass_gate(session):
                with self._stage("packet_build"):
                    body_0138 = build_complete_condition_pass_gate_0138_us()
                pkt_0138, mode_0138 = self._emit(0, 0x0138, body_0138, "codec")
                packets.append(pkt_0138)
                notes.append(
                    "0138/%s COMPLETE_CONDITION_PASS_GATE len=%d"
                    % (mode_0138, len(body_0138))
                )
        # 补齐 mon 字段（友方路径 can_combat=false 时上面未解析）
        if mon_code <= 0:
            mon_code = int(
                mon.get("code")
                or mon.get("monster_code")
                or parsed.get("monster_code")
                or 0
            )
        if mon_type <= 0:
            mon_type = int(mon.get("type") or mon.get("mon_type") or 0)
        if mon_level <= 1 and mon.get("level") is not None:
            mon_level = int(mon.get("level") or mon_level or 1)
        if dungeon_id < 0:
            dungeon_id = int(
                parsed.get("dungeon_id")
                or getattr(session, "cur_dungeon_id", -1)
                or getattr(session, "selected_dungeon_id", -1)
                or -1
            )
        difficulty = int(
            difficulty
            or getattr(session, "selected_difficulty", None)
            or getattr(session, "cur_dungeon_difficulty", 0)
            or 0
        ) & 0xFF

        if can_combat and mon is not None and bool(
            getattr(session, "another_arad_active", False)
        ):
            try:
                notes.append(
                    _mirror_mission.note_another_arad_actor_death(
                        session,
                        mon,
                        actor_seq=int(seq_i),
                        map_index=int(getattr(session, "dungeon_map_index", 0) or 0),
                    )
                )
            except Exception as exc:
                notes.append("mirror_mission_kill err=%s" % type(exc).__name__)

        # Quest scope always comes from the active session first.  Some
        # passive-object DIE layouts carry a map-like value in their packet
        # dungeon slot, which is not the PVF dungeon id.
        residual_dungeon_id, residual_difficulty = _resolve_quest_drop_scope(
            session,
            parsed,
        )

        # mon_level / mon_type / mon_code / difficulty / dungeon_id 已解析
        # 杀怪 residual：
        #   - 敌对 actor：hunt monster / hunt enemy type1|2
        #   - 可破坏建筑（type9 / passive）：hunt enemy type=3（不要求 is_hostile）
        #   - 队友 APC / 中立非建筑：不计任务进度
        # quest enemy domain（S4A12 QuestDropProvider）：
        #   1=monster  2=ai character  3=passive object
        # 与 wire mon rank（0 normal / 1 champ / 3 boss / 5..8 APC / 9 special）不同命名空间。
        residual_enemy_type = 0
        if mon_combat and (mon_hostile or mon_passive):
            enemy_type = int(mon.get("quest_enemy_type") or 0)
            if enemy_type <= 0:
                kind = str(mon.get("kind") or "").lower()
                if mon_passive or kind in {
                    "special_passive",
                    "passive_object",
                    "passive",
                    "map_passive",
                } or mon_type == 9:
                    enemy_type = 3
                elif kind in {"ai_character", "ai", "apc"} or mon_type in {5, 6, 7, 8}:
                    enemy_type = 2
                else:
                    enemy_type = 1
            residual_enemy_type = int(enemy_type)
            enemy_rank = normalize_enemy_rank(mon_type, enemy_type)
            with self._stage("quest_processing"):
                k_packets, k_note = self._apply_kill_residual(
                    session=session,
                    monster_code=mon_code,
                    dungeon_id=residual_dungeon_id,
                    difficulty=residual_difficulty,
                    enemy_type=enemy_type,
                    enemy_rank=enemy_rank,
                )
            packets.extend(k_packets)
            notes.append(
                    "%s enemy_type=%s enemy_rank=%s kind=%s role=%s faction=%s passive=%s"
                % (
                    k_note,
                    enemy_type,
                    enemy_rank,
                    mon.get("kind") or "monster",
                    mon.get("role") or "",
                    mon.get("faction") or "",
                    int(mon_passive),
                )
            )
            # 本局记录已摧毁建筑：重进同图不复活（蚁卵/蚁穴等）
            if mon_passive and session is not None:
                try:
                    mid = int(
                        mon.get("runtime_map_index")
                        or getattr(session, "dungeon_normal_map_index", 0)
                        or getattr(session, "dungeon_map_index", 0)
                        or 0
                    )
                    ox = int(mon.get("x") or 0)
                    oy = int(mon.get("y") or 0)
                    if mark_object_destroyed(
                        session,
                        map_index=mid,
                        object_code=mon_code,
                        x=ox,
                        y=oy,
                    ):
                        notes.append(
                            "destroyed_obj map=%s code=%s xy=(%s,%s)"
                            % (mid, mon_code, ox, oy)
                        )
                    else:
                        notes.append(
                            "destroyed_obj already map=%s code=%s" % (mid, mon_code)
                        )
                except Exception as exc:
                    notes.append("destroyed_obj err=%r" % (exc,))
        else:
            notes.append(
                "kill_residual=skip_non_hostile kind=%s role=%s faction=%s"
                % (
                    mon.get("kind") or "monster",
                    mon.get("role") or "",
                    mon.get("faction") or "",
                )
            )
        if not layered_scene_active:
            try:
                (
                    clear_condition_matched,
                    clear_condition_complete,
                    clear_condition_note,
                ) = observe_dungeon_clear_condition(session, mon)
                if clear_condition_matched:
                    notes.append(clear_condition_note)
            except Exception as exc:
                notes.append("clear_condition err=%r" % (exc,))
        weight = 1.0
        try:
            weight = float(getattr(session, "dungeon_experience_weight", 1.0) or 1.0)
        except Exception:
            weight = 1.0
        if weight < 0:
            weight = 1.0

        cid = _cid(session, self.server)
        char_level = 1
        if cid is not None:
            try:
                char_level = read_character_level(
                    self.server._repo(), int(cid), session=session
                )
            except Exception:
                char_level = 1

        # step 5: BaseExp * weight * difficulty * mon_type * penalty (friend)
        #         calc 模式省略 penalty；见 helper-character_exp
        # 敌友：仅 is_hostile 且 grants_exp 给经验；队友/中立 APC 永远 0
        grants_exp = bool(mon.get("grants_exp", True)) and mon_hostile and mon_combat
        if not grants_exp:
            base_gained = 0
        else:
            base_gained = compute_kill_exp(
                char_level,
                mon_level,
                experience_weight=weight,
                difficulty=difficulty,
                mon_type=mon_type,
            )
        premium_exp_bonus = 0
        growth_contract_exp_bonus = 0
        neo_premium_exp_bonus = 0
        premium_exp_percent = 0
        if base_gained > 0 and cid is not None:
            try:
                premium_effects = get_active_premium_effects(
                    self.server._repo(),
                    int(getattr(self.server, "account_id", 1) or 1),
                    session=session,
                )
                premium_exp_percent = max(
                    0, int(premium_effects.bonus_exp_percent)
                )
                premium_exp_bonus = premium_effects.compute_bonus_exp(base_gained)
                growth_contract_exp_bonus = (
                    premium_effects.compute_growth_contract_exp(base_gained)
                )
                neo_premium_exp_bonus = premium_effects.compute_neo_premium_exp(
                    base_gained
                )
            except Exception as exc:
                notes.append("premium_exp_err=%r" % (exc,))
        fatigue_burn_bonus = 0
        if base_gained > 0 and cid is not None:
            try:
                fatigue_burn_bonus = (
                    _activity_state.compute_fatigue_burning_exp_bonus(
                        self.server._repo(),
                        int(cid),
                        base_gained,
                        session=session,
                    )
                )
            except Exception as exc:
                notes.append("fatigue_burning_exp_err=%r" % (exc,))
        gained = min(
            0xFFFFFFFF,
            int(base_gained)
            + int(premium_exp_bonus)
            + int(fatigue_burn_bonus),
        )
        try:
            session.dungeon_run_kill_exp = int(
                getattr(session, "dungeon_run_kill_exp", 0) or 0
            ) + gained
            session.dungeon_run_premium_exp = int(
                getattr(session, "dungeon_run_premium_exp", 0) or 0
            ) + premium_exp_bonus
            session.dungeon_run_growth_exp = int(
                getattr(session, "dungeon_run_growth_exp", 0) or 0
            ) + growth_contract_exp_bonus
            session.dungeon_run_neo_exp = int(
                getattr(session, "dungeon_run_neo_exp", 0) or 0
            ) + neo_premium_exp_bonus
            session.dungeon_run_fatigue_burn_exp = int(
                getattr(session, "dungeon_run_fatigue_burn_exp", 0) or 0
            ) + fatigue_burn_bonus
        except Exception:
            pass
        notes.append(
            "kill seq=%d type=%d lv=%d weight=%.3f diff=%d base=%d "
            "premium=%d@%d%% growth=%d neo=%d fatigue_burn=%d@%d%% gained=%d "
            "kill_exp=%s char_lv=%s"
            % (
                seq_i,
                mon_type,
                mon_level,
                weight,
                difficulty,
                base_gained,
                premium_exp_bonus,
                premium_exp_percent,
                growth_contract_exp_bonus,
                neo_premium_exp_bonus,
                fatigue_burn_bonus,
                (
                    _activity_state.FATIGUE_BURNING_EXP_PERCENT
                    if fatigue_burn_bonus > 0
                    else 0
                ),
                gained,
                getattr(session, "dungeon_run_kill_exp", 0),
                char_level,
            )
        )

        # §5.5.2 step 6: write DB exp/level
        exp_after = None
        level_after = None
        expertise_after = None
        levels_gained = 0
        level_title_grants = []
        if persist_exp and cid is not None and gained > 0:
            try:
                with self._stage("exp_persist"):
                    result = grant_exp_to_character(
                        self.server._repo(),
                        int(cid),
                        gained,
                        session=session,
                    )
                exp_after = result.get("exp")
                level_after = result.get("new_level")
                expertise_after = result.get("expertise_exp")
                levels_gained = int(result.get("levels_gained") or 0)
                notes.append(
                    "db_exp delta=%s ok=%s level=%s->%s exp=%s ordinary_added=%s "
                    "expertise=%s expertise_added=%s"
                    % (
                        gained,
                        result.get("ok"),
                        result.get("old_level"),
                        result.get("new_level"),
                        result.get("exp"),
                        result.get("ordinary_exp_added"),
                        result.get("expertise_exp"),
                        result.get("expertise_exp_added"),
                    )
                )
                if bool(result.get("ok")) and _level_titles.has_crossed_level_title_threshold(
                    int(result.get("old_level") or 1),
                    int(result.get("new_level") or 1),
                ):
                    level_title_grants, level_title_note = (
                        _level_titles.claim_level_title_rewards(
                            self.server._repo(),
                            character_id=int(cid),
                            previous_level=int(result.get("old_level") or 1),
                            current_level=int(result.get("new_level") or 1),
                        )
                    )
                    notes.append(level_title_note)
            except Exception as exc:
                notes.append("db_exp err=%r" % (exc,))

        # §5.5.2 step 7: S0/0025 EXP。DB 更新后只发一次服务端权威累计值，
        # 由 Stage3 原生 reader 计算本次增量并同步等级、SP/TP。
        send_exp_ui = env_bool("USDOF_GAME_DUNGEON_DIE_SEND_EXP", True) and gained > 0
        only_on_levelup = env_bool(
            "USDOF_GAME_DUNGEON_DIE_0025_ONLY_ON_LEVELUP", False
        )
        if send_exp_ui and only_on_levelup and int(levels_gained or 0) <= 0:
            notes.append(
                "0025_EXP skip=same_level levels_gained=0 "
                "(DB still has exp; set DIE_0025_ONLY_ON_LEVELUP=0 to restore per-kill UI)"
            )
            send_exp_ui = False
        levelup_exp_level = None
        if send_exp_ui:
            try:
                if (
                    level_after is None
                    or exp_after is None
                    or expertise_after is None
                ):
                    level_after = int(
                        getattr(session, "character_level", None)
                        or getattr(session, "level", None)
                        or char_level
                        or 1
                    )
                    if cid is not None:
                        try:
                            lv0, exp0, expertise0 = (
                                self.server._repo().get_character_progression(
                                    int(cid)
                                )
                            )
                            exp_after = int(exp0 or 0)
                            level_after = int(lv0 or level_after or 1)
                            expertise_after = int(expertise0 or 0)
                        except Exception:
                            exp_after = int(
                                getattr(session, "character_exp", 0) or 0
                            )
                            expertise_after = int(
                                getattr(session, "character_expertise_exp", 0) or 0
                            )
                    else:
                        exp_after = int(getattr(session, "character_exp", 0) or 0)
                        expertise_after = int(
                            getattr(session, "character_expertise_exp", 0) or 0
                        )

                exp_after_i = int(exp_after or 0) & 0xFFFFFFFF
                level_after_i = max(1, min(255, int(level_after or 1)))
                expertise_after_i = max(0, int(expertise_after or 0))
                layout = (
                    os.environ.get("USDOF_GAME_DUNGEON_EXP_NOTI_LAYOUT") or "stage3_78"
                )
                # Stage3 78B + 真实 SP/TP 绝对余额 + PVP=0
                sp_state, sp_source = _resolve_kill_skill_point_state(
                    level=level_after_i,
                    levels_gained=levels_gained,
                    character_id=int(cid or 0),
                    repo=self.server._repo() if cid is not None else None,
                    session=session,
                )
                with self._stage("packet_build"):
                    # total_exp is absolute authority; fixed detail rows carry
                    # only this kill's verified Growth/Neo/Fatigue components.
                    body_0025 = build_exp_notification_0025(
                        level_after_i,
                        exp_after_i,
                        expertise_exp=expertise_after_i,
                        page0_sp=sp_state.page0_sp,
                        page1_sp=sp_state.page1_sp,
                        page0_tp=sp_state.page0_tp,
                        page1_tp=sp_state.page1_tp,
                        pvp_victory_point=0,
                        growth_contract_bonus_exp=growth_contract_exp_bonus,
                        neo_premium_bonus_exp=neo_premium_exp_bonus,
                        fatigue_burn_bonus_exp=fatigue_burn_bonus,
                        layout=layout,
                    )
                pkt_e, mode_e = self._emit(0, 0x0025, body_0025, "codec")
                packets.append(pkt_e)
                if int(levels_gained or 0) > 0:
                    levelup_exp_level = level_after_i
                plain_len = len(body_0025)
                wire_total = len(pkt_e)
                cipher_len = max(0, wire_total - 16)  # 16B S2C header
                if bool(getattr(self.server, "diagnostic_trace", False)):
                    body_hex = body_0025.hex(" ")
                    try:
                        self.server.log(
                            "[Game] send kill-exp-0025 flag=0x00 opcode=0x0025 "
                            "plain=0x%04x cipher=0x%04x total=0x%04x mode=%s "
                            "layout=%s level=%s total_exp=%s expertise_exp=%s "
                            "gained=%s growth_bonus=%s neo_bonus=%s premium_total=%s "
                            "fatigue_burn_bonus=%s "
                            "sp=%s/%s tp=%s/%s hex=%s"
                            % (
                                plain_len,
                                cipher_len,
                                wire_total,
                                mode_e,
                                layout,
                                level_after_i,
                                exp_after_i,
                                expertise_after_i,
                                gained,
                                growth_contract_exp_bonus,
                                neo_premium_exp_bonus,
                                premium_exp_bonus,
                                fatigue_burn_bonus,
                                sp_state.page0_sp,
                                sp_state.page1_sp,
                                sp_state.page0_tp,
                                sp_state.page1_tp,
                                body_hex,
                            )
                        )
                    except Exception:
                        pass
                notes.append(
                    "0025/%s/len=%d level=%s exp=%s expertise=%s layout=%s "
                    "gained=%s growth_bonus=%s neo_bonus=%s premium_total=%s "
                    "fatigue_burn_bonus=%s "
                    "levels_gained=%s sp=%s/%s tp=%s/%s sp_source=%s "
                    "wire plain=0x%04x cipher=0x%04x total=0x%04x"
                    % (
                        mode_e,
                        plain_len,
                        level_after_i,
                        exp_after_i,
                        expertise_after_i,
                        layout,
                        gained,
                        growth_contract_exp_bonus,
                        neo_premium_exp_bonus,
                        premium_exp_bonus,
                        fatigue_burn_bonus,
                        levels_gained,
                        sp_state.page0_sp,
                        sp_state.page1_sp,
                        sp_state.page0_tp,
                        sp_state.page1_tp,
                        sp_source,
                        plain_len,
                        cipher_len,
                        wire_total,
                    )
                )
                if session is not None:
                    try:
                        session.character_level = level_after_i
                        session.character_exp = exp_after_i
                        session.character_expertise_exp = expertise_after_i
                        session.client_exp_primed = True
                    except Exception:
                        pass
            except Exception as exc:
                notes.append("0025_EXP err=%r" % (exc,))

        # S0/0015 is the Stage3 full rebuild for the acceptable-quest UI.
        # Refresh once for each persisted level boundary, after the current
        # character level has been committed, rather than waiting for a
        # character re-entry to hydrate the list.
        if int(levels_gained or 0) > 0 and cid is not None:
            try:
                quest_detail = resolve_selected_character_detail(self.server, session)
                refresh_cid = int(quest_detail.get("character_id") or 0)
                if refresh_cid != int(cid):
                    notes.append(
                        "levelup_quest_refresh skip=character_mismatch cid=%s selected=%s"
                        % (cid, refresh_cid)
                    )
                else:
                    quest_packets, quest_note = (
                        pack_levelup_acceptable_quest_refresh(
                            self.server,
                            character_id=refresh_cid,
                            job=int(quest_detail.get("job") or 0),
                            level=int(level_after or quest_detail.get("level") or 1),
                            grow_type=int(quest_detail.get("grow_type") or 0),
                            session=session,
                        )
                    )
                    packets.extend(quest_packets)
                    notes.append("levelup_quest_refresh %s" % quest_note)
            except Exception as exc:
                notes.append("levelup_quest_refresh err=%r" % (exc,))

        if levelup_exp_level is not None and cid is not None:
            refresh_packets, refresh_note = (
                _levelup_userinfo1.pack_dungeon_levelup_userinfo1_refresh(
                    self.server,
                    session=session,
                    character_id=int(cid),
                    expected_level=int(levelup_exp_level),
                )
            )
            packets.extend(refresh_packets)
            notes.append(refresh_note)

        # §5.5.2 step 8: 升级 skillinfo
        # 历史 bug：默认发空 0013（u32 len=0）→ 客户端整表覆盖技能树，技能栏/已学技能全清。
        # 等级与累计经验由 0025 原生 reader 更新；空 0013 会毁技能树，禁止再发。
        # 默认 OFF。仅当显式开启且能拿到 job 初始技能 JSON 时，才下发非空 skillinfo。
        # 注意：尚无「已学技能」DB 快照，非空包也只是 starter 树，不能还原玩家加点。
        if levels_gained > 0 and env_bool("USDOF_GAME_DUNGEON_DIE_SEND_SKILLINFO", False):
            try:
                job = 0
                if session is not None:
                    job = int(getattr(session, "character_job", 0) or 0)
                if job <= 0 and cid is not None:
                    try:
                        _e, _g, _gw, job_db, _gt = self.server._repo().get_character_economy(
                            int(cid)
                        )
                        job = int(job_db or 0)
                    except Exception:
                        job = 0
                skills = get_initial_skills_for_job(job) if job > 0 else ()
                lv_for_sp = max(
                    1,
                    int(
                        getattr(session, "character_level", None)
                        or level_after
                        or char_level
                        or 1
                    ),
                )
                try:
                    skill_sp = resolve_skill_point_protocol_state(
                        level=lv_for_sp,
                        character_id=int(cid or 0),
                        repo=self.server._repo() if cid is not None else None,
                        session=session,
                    )
                except Exception:
                    skill_sp = None
                # 升级时优先 DB 槽位（若已持久化）
                if env_bool("USDOF_GAME_SKILLS_PERSIST", True) and cid is not None:
                    try:
                        from game.chain_loader import load_chain as _lc_skills

                        _sh = _lc_skills("helper-character_skills.py")
                        skills_db, _sn = _sh.resolve_skills_for_character(
                            self.server._repo(),
                            int(cid),
                            int(job),
                            page_header=int(getattr(skill_sp, "page0_sp", 0) or 0),
                            seed_if_empty=True,
                        )
                        if skills_db:
                            skills = skills_db
                    except Exception:
                        pass
                if skills:
                    with self._stage("packet_build"):
                        body_0013 = build_skillinfo_from_pvf(
                            skills,
                            page0_sp=int(getattr(skill_sp, "page0_sp", 0) or 0),
                            page1_sp=int(getattr(skill_sp, "page1_sp", 0) or 0),
                            page0_tp=int(getattr(skill_sp, "page0_tp", 0) or 0),
                            page1_tp=int(getattr(skill_sp, "page1_tp", 0) or 0),
                        )
                    pkt_s, mode_s = self._emit(0, 0x0013, body_0013, "codec")
                    packets.append(pkt_s)
                    notes.append(
                        "0013/%s/len=%d levels_gained=%s skillinfo=db_or_json "
                        "job=%s n=%s sp=%s/%s tp=%s/%s"
                        % (
                            mode_s,
                            len(body_0013),
                            levels_gained,
                            job,
                            len(skills),
                            int(getattr(skill_sp, "page0_sp", 0) or 0),
                            int(getattr(skill_sp, "page1_sp", 0) or 0),
                            int(getattr(skill_sp, "page0_tp", 0) or 0),
                            int(getattr(skill_sp, "page1_tp", 0) or 0),
                        )
                    )
                else:
                    notes.append(
                        "0013_skillinfo skip=no_initial_skills levels_gained=%s job=%s "
                        "(never send empty — clears client skill tree)"
                        % (levels_gained, job)
                    )
            except Exception as exc:
                notes.append("0013_skillinfo err=%r" % (exc,))

        # Tournament actors are all present in the opening 001D.  Stage3's
        # native bracket consumes the terminal DIE events against the initial
        # 0183 projection; only the final round opens the card page.
        if tournament_transition is not None:
            try:
                if bool(tournament_transition.get("completed")):
                    with self._stage("tournament_final_reward"):
                        body_tournament_0185 = build_tournament_clear_reward_0185(
                            session
                        )
                    pkt_tournament_0185, mode_tournament_0185 = self._emit(
                        0,
                        0x0185,
                        body_tournament_0185,
                        "codec",
                    )
                    packets.append(pkt_tournament_0185)
                    notes.append(
                        "0185/%s tournament_final rounds=%d exp=%d len=%d"
                        % (
                            mode_tournament_0185,
                            int(
                                tournament_transition.get("completed_round")
                                or 0
                            ),
                            int(
                                getattr(
                                    session,
                                    "dungeon_tournament_reward_exp",
                                    0,
                                )
                                or 0
                            ),
                            len(body_tournament_0185),
                        )
                    )
                elif int(tournament_transition.get("completed_round") or 0) > 0:
                    notes.append(
                        "tournament=round_complete round=%d next=%d native_bracket"
                        % (
                            int(
                                tournament_transition.get("completed_round")
                                or 0
                            ),
                            int(tournament_transition.get("next_round") or 0),
                        )
                    )
            except Exception as exc:
                notes.append(
                    "tournament=projection_error type=%s" % type(exc).__name__
                )

        if not defer_room_clear:
            cleanup_packets, cleanup_note = self._force_boss_room_cleanup(
                session=session,
                defeated_mon=mon,
                layered_scene_active=layered_scene_active,
            )
            packets.extend(cleanup_packets)
            if cleanup_note:
                notes.append(cleanup_note)

        # §5.5.2 step 10–11: 房清 + boss → ENABLE_CLEAR
        # 通关看 is_blocking：普通敌对怪 + 敌对 APC（含 BOSS type=8）；
        # 友方/中立 APC 与 special type9 不挡门。
        mons = list(getattr(session, "dungeon_room_mons", None) or [])
        blocking = [m for m in mons if bool(m.get("is_blocking", True))]
        alive = sum(1 for m in blocking if not m.get("killed"))
        # 有挡路目标且全死 → 清房；纯友方 APC/空房不靠 DIE 清（进房时 mark）
        cleared = bool(blocking) and alive == 0
        tournament_controls_clear = bool(mon.get("tournament_actor")) and (
            bool(getattr(session, "dungeon_tournament_active", False))
            or bool(getattr(session, "dungeon_tournament_completed", False))
        )
        altar_controls_clear = bool(
            getattr(session, "altar_active", False)
            and str(mon.get("kind") or "") == "altar_wave"
        )
        logical_room_cleared = (
            cleared
            and not layered_scene_active
            and not tournament_controls_clear
            and not altar_controls_clear
            and not defer_room_clear
        )
        is_death_tower = (
            session is not None
            and bool(getattr(session, "in_dungeon_run", False))
            and str(getattr(session, "dungeon_tower_kind", "") or "").lower()
            == "death"
        )
        cell_x = int(getattr(session, "dungeon_cell_x", 0) or 0) & 0xFF
        cell_y = int(getattr(session, "dungeon_cell_y", 0) or 0) & 0xFF
        # 本局临时记录：清完的格子再进不再刷怪（离开地图时 reset）
        if logical_room_cleared and not is_death_tower:
            try:
                if mark_room_cleared(session, cell_x, cell_y):
                    notes.append("room_state mark_cleared cell=(%d,%d)" % (cell_x, cell_y))
                else:
                    notes.append("room_state already_cleared cell=(%d,%d)" % (cell_x, cell_y))
            except Exception as exc:
                notes.append("room_state mark err=%r" % (exc,))
        boss_x = int(getattr(session, "dungeon_boss_x", 0) or 0) & 0xFF
        boss_y = int(getattr(session, "dungeon_boss_y", 0) or 0) & 0xFF
        end_point = logical_room_cleared and (cell_x, cell_y) == (boss_x, boss_y)
        # mon rank 3=boss 怪；wire APC type 8=boss APC（须击败才通关）
        boss_type = mon_type in (3, 8)
        phase = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        (
            clear_condition_controls,
            clear_condition_complete,
            clear_condition_progress,
            clear_condition_total,
        ) = dungeon_clear_condition_status(session)
        mirror_strict_boss = False
        mirror_boss_defeated = False
        if bool(getattr(session, "another_arad_active", False)):
            try:
                mirror_strict_boss = (
                    _another_arad.is_session_another_arad_boss_endpoint(
                        session,
                        cell_x=cell_x,
                        cell_y=cell_y,
                        map_index=int(
                            getattr(session, "dungeon_map_index", 0) or 0
                        ),
                    )
                )
                mirror_boss_defeated = any(
                    bool(actor.get("killed"))
                    and actor_is_hostile(actor)
                    and actor_counts_for_combat(actor)
                    and int(actor.get("type") or actor.get("mon_type") or 0)
                    in (3, 8)
                    for actor in mons
                )
            except Exception as exc:
                notes.append("mirror_boss_endpoint err=%s" % type(exc).__name__)
                mirror_strict_boss = False
                mirror_boss_defeated = False
        mirror_active = bool(getattr(session, "another_arad_active", False))
        default_clear = (
            (
                logical_room_cleared
                and mirror_strict_boss
                and mirror_boss_defeated
            )
            if mirror_active
            else (end_point or (logical_room_cleared and boss_type))
        ) and not clear_condition_controls
        condition_clear = (
            logical_room_cleared
            and clear_condition_controls
            and clear_condition_complete
            and (
                not mirror_active
                or (mirror_strict_boss and mirror_boss_defeated)
            )
        )
        if altar_controls_clear:
            notes.append(
                "altar=clear_owned active=%d scheduled=%d suppress=001F"
                % (
                    len(getattr(session, "altar_active_monster_seqs", ()) or ()),
                    max(
                        0,
                        len(getattr(session, "altar_schedule", ()) or ())
                        - int(getattr(session, "altar_schedule_index", 0) or 0),
                    ),
                )
            )
        elif tournament_controls_clear:
            notes.append(
                "tournament=clear_owned active=%d completed=%d suppress=001F"
                % (
                    int(bool(getattr(session, "dungeon_tournament_active", False))),
                    int(bool(getattr(session, "dungeon_tournament_completed", False))),
                )
            )
        elif cleared and defer_room_clear:
            notes.append("room_clear=deferred source=boss_cleanup")
        elif cleared and layered_scene_active:
            notes.append("layered_scene=1 suppress=logical_room_clear")
        elif cleared and is_death_tower:
            session.dungeon_tower_floor_cleared = True
            notes.append(
                "death_tower floor=%s/%s cleared=1 suppress=001F "
                "await=009F(2),002D dialogue=client_pvf_tower_events"
                % (
                    getattr(session, "dungeon_tower_floor", None),
                    getattr(session, "dungeon_tower_final_floor", None),
                )
            )
        elif (condition_clear or default_clear) and phase == 0:
            session.dungeon_clear_phase = 1
            if raid_hooks is not None:
                raid_hooks.note_clear_gate(session, mon)
            with self._stage("packet_build"):
                body_001f = build_enable_clear_001f(0)
            pkt_f, mode_f = self._emit(0, 0x001F, body_001f, "codec")
            packets.append(pkt_f)
            notes.append(
                "001F/%s ENABLE_CLEAR source=%s end_point=%s boss_type=%s "
                "mirror_boss_endpoint=%d mirror_boss_defeated=%d "
                "clear_condition=%d/%d"
                % (
                    mode_f,
                    (
                        dungeon_clear_condition_source(session)
                        if condition_clear
                        else "room"
                    ),
                    end_point,
                    boss_type,
                    int(mirror_strict_boss),
                    int(mirror_boss_defeated),
                    clear_condition_progress,
                    clear_condition_total,
                )
            )
        else:
            notes.append(
                "room alive=%d cleared=%s phase=%s end=%s "
                "mirror_boss_endpoint=%d mirror_boss_defeated=%d "
                "clear_condition_controls=%d progress=%d/%d complete=%d"
                % (
                    alive,
                    cleared,
                    phase,
                    end_point,
                    int(mirror_strict_boss),
                    int(mirror_boss_defeated),
                    int(clear_condition_controls),
                    clear_condition_progress,
                    clear_condition_total,
                    int(clear_condition_complete),
                )
            )

        if logical_room_cleared:
            from game.quest.no_result_clear import persist_licensed_no_result_clear
            try:
                licensed_packets, licensed_note = persist_licensed_no_result_clear(self.server, session)
                packets.extend(licensed_packets)
                if licensed_note != "no_result_clear=not_applicable":
                    notes.append(licensed_note)
            except Exception as exc:
                notes.append("no_result_clear=error type=%s" % type(exc).__name__)

        if logical_room_cleared and bool(
            getattr(session, "another_arad_active", False)
        ):
            try:
                notes.append(
                    _mirror_mission.note_another_arad_room_cleared(
                        session, cell_x, cell_y
                    )
                )
            except Exception as exc:
                notes.append("mirror_mission_room err=%s" % type(exc).__name__)

        if (
            logical_room_cleared
            and not is_death_tower
            and env_bool("USDOF_GAME_QUEST_CLEAR_MAP_PROGRESS", True)
        ):
            try:
                seen_companions, alive_companions = companion_progress_codes(session)
                clear_map_packets, clear_map_note = persist_clear_map_progress(
                    self.server,
                    session,
                    context=DungeonClearContext(
                        dungeon_id=int(
                            getattr(session, "selected_dungeon_id", 0)
                            or getattr(session, "cur_dungeon_id", 0)
                            or 0
                        ),
                        difficulty=int(
                            getattr(session, "selected_difficulty", 0)
                            or getattr(session, "cur_dungeon_difficulty", 0)
                            or 0
                        ),
                        map_id=int(getattr(session, "dungeon_map_index", 0) or 0),
                        seen_companion_codes=seen_companions,
                        alive_companion_codes=alive_companions,
                    ),
                )
                packets.extend(clear_map_packets)
                notes.append(clear_map_note)
            except Exception as exc:
                notes.append("clear_map err=%r" % (exc,))

        q_packets, q_note = self._quest_drop_packets(
            parsed,
            session=session,
            base=base,
            enemy_type=int(residual_enemy_type or 0),
        )
        packets.extend(q_packets)
        notes.append(q_note.split(";")[-1].strip() if ";" in q_note else q_note)

        if level_title_grants:
            try:
                title_packets, title_note = _level_titles.pack_level_title_grant_packets(
                    self.server,
                    grants=level_title_grants,
                )
                if title_packets and hasattr(self.server, "queue_delayed_response_packets"):
                    self.server.queue_delayed_response_packets(
                        [
                            (
                                packet,
                                "die delayed level_title i=%s %s"
                                % (index, title_note if index == 0 else ""),
                            )
                            for index, packet in enumerate(title_packets)
                        ]
                    )
                    notes.append(title_note + ";delayed=1")
                elif title_packets:
                    packets.extend(title_packets)
                    notes.append(title_note + ";delayed=0")
                else:
                    notes.append(title_note)
            except Exception as exc:
                notes.append("level_title_packets err=%r" % (exc,))

        if altar_controls_clear:
            if defer_altar_progression:
                notes.append(
                    "altar_wave_deaths source=0027 deferred=1 seq=%d" % seq_i
                )
            else:
                altar_flow = load_chain("flow-altar-random.py").AltarRandomFlow(
                    self.server
                )
                progression_packets, progression_note = (
                    altar_flow.record_confirmed_wave_deaths(
                        (seq_i,),
                        session=session,
                        source="0027",
                    )
                )
                packets.extend(progression_packets)
                notes.append(progression_note)

        note = (
            "%s; reply=[%s]; doc_ref=副本实现A §5.5.2; "
            "chain_file=game/chains/opcode0027-die_monster.py"
            % (base, ", ".join(notes) if notes else "none")
        )
        return packets, note
