"""C1/0x002E SET_PLAY_RESULT — dungeon clear settlement (副本实现A.md §5.6).

S2C order (业务对齐 副本实现A + Stage3 sub_1645DA0):
  S0/0022 PLAY_RESULT
  S0/0025 EXP                 (默认 78B Stage3 + idx9 codec；累计 total + SP/TP)
  S0/0023 CLEAR_DUNGEON_REWARD (Stage3 完整体 + ShowResult=1；默认 ON)
  S1/0045 + S1/0046           结算布局（默认 ON；维修商人 + 右上角回城/下一图）

结算经验先在本处理器落库，再追加一次 Stage3 78B codec `S0/0025` 权威累计值；如需
复现旧版不发刷新包的行为，可显式设置 `USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_EXP=0`。

Clear exp (default **s4a12**，见 helper-character_exp.compute_clear_reward_exp)::
  clear_base = floor(GetExpRewardBase(basic_lv) * dgn_weight * difficulty_rate)
  score_bonus = floor(clear_base * clear_rank_bonus_rate)
  adventure_bonus = floor((clear_base + score_bonus) * adventure_percent / 100)
  consumable_bonus = floor(clear_base * active_pvf_exp_bonus_rate)
  DB 只再加 clear_base+score_bonus+consumable_bonus+premium_bonus+
  fatigue_burn_bonus+adventure_bonus
  （杀怪经验已在 DIE 落库，不双计；premium 与冒险团加成不复利）

通关总经验由 ``0023`` 的基础、Rank、杀怪和加成槽共同展示；``0022``
只承载本次/最佳通关毫秒，不再复用经验总额。

日常 ``USDOF_GAME_DUNGEON_SKIP_CARD_FLIP=0``（交互翻牌默认启用）::
  **仍发** 0045/0046 —— 这是 Stage3 进入「通关结算态」的关键包
  （live 日志：缺它们时只有评分 ESC，无维修商人/右上角按钮，且仍可乱走重刷 boss）。
  free 金币冻结在本局 reward plan，等待 C1/0047 免费翻牌后落库 + S0/000E。
  仅显式设 SKIP=1 时才绕过交互并在本包直接发奖。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Optional, Tuple

from game.chain_loader import load_chain
from game.quest.quest_clear import DungeonClearContext, apply_dungeon_clear_to_active_quests
from game.quest.adventure_season1 import record_clear as record_adventure_season1_clear
from game.quest.weekly_attendance import record_clear as record_weekly_attendance_clear
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_levelup_acceptable_quest_refresh,
    pack_set_trigger_ack_packets,
    resolve_selected_character_detail,
)
from game.shop.premium_contract_index import get_active_premium_effects
from game.character.character_db import (
    is_virtual_inventory_grant,
    parse_virtual_premium_grant,
)
from game.character import exp_bonus_consumable as _exp_bonus_consumable
from game.character.adventure_group import (
    calculate_adventure_group_benefits_for_account,
    record_adventure_group_dungeon_clear,
)
from game.dungeon.clear_reward import build_clear_reward_plan

_payloads = load_chain("helper-dungeon_payloads.py")
_exp = load_chain("helper-character_exp.py")
_activity_state = load_chain("helper-activity_state.py")
_sp = load_chain("helper-skill_points.py")
_level_titles = load_chain("helper-level_milestone_titles.py")
_levelup_userinfo1 = load_chain("helper-dungeon_levelup_userinfo1.py")
_worldmap = load_chain("helper-dungeon_worldmap.py")
_ally_companions = load_chain("helper-dungeon_ally_companions.py")
_room_state = load_chain("helper-dungeon_room_state.py")
_hundred_floor_progress = load_chain("helper-hundred_floor_tower_progress.py")
_special_selector = load_chain("helper-special_dungeon_selector.py")
_mirror_mission = load_chain("helper-another_arad_quest.py")
_card_auto_flip = load_chain("flow-card_reward_auto_flip.py")
build_play_result_0022 = _payloads.build_play_result_0022
build_clear_dungeon_reward_0023_stub = _payloads.build_clear_dungeon_reward_0023_stub
build_exp_notification_0025 = _payloads.build_exp_notification_0025
build_card_select_right_state_0045 = _payloads.build_card_select_right_state_0045
build_card_layout_ack_0046 = _payloads.build_card_layout_ack_0046
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_wallet_gold_update_000e = _payloads.build_wallet_gold_update_000e
compute_clear_reward_exp = _exp.compute_clear_reward_exp
grant_exp_to_character = _exp.grant_exp_to_character
resolve_skill_point_protocol_state = _sp.resolve_skill_point_protocol_state
worldmap_id_for_dungeon = _worldmap.worldmap_id_for_dungeon
companion_progress_codes = _ally_companions.companion_progress_codes
cleared_cells_snapshot = _room_state.cleared_cells_snapshot
floor_for_dungeon = _hundred_floor_progress.floor_for_dungeon
build_grief_progress_050e = _hundred_floor_progress.build_grief_progress_050e
is_special_dungeon = _special_selector.is_special_dungeon
build_special_selector_sync_packets = _special_selector.build_selector_sync_packets
canonical_special_selector_progress_key = (
    _special_selector.canonical_progress_key_for_dungeon
)
next_special_selector_highest_difficulty = _special_selector.next_highest_difficulty
normalize_special_selector_highest_difficulty = (
    _special_selector.normalize_highest_difficulty
)
special_entry_option_is_unlocked = _special_selector.special_entry_option_is_unlocked
special_selector_state_context = _special_selector.selector_state_context


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def _cid(session: Any) -> int:
    if session is None:
        return 0
    for attr in ("selected_character_id", "character_id", "cid"):
        val = getattr(session, attr, None)
        if val is not None:
            try:
                cid = int(val)
                if cid > 0:
                    return cid
            except Exception:
                pass
    return 0


def _extract_client_rank_point(body: bytes) -> int:
    """s4a12 ExtractClientRankPoint: body[10] if long enough else body[0]."""
    raw = bytes(body or b"")
    if not raw:
        return 0
    if len(raw) > 10:
        return int(raw[10]) & 0xFF
    return int(raw[0]) & 0xFF


def _cleared_hell_party(session: Any) -> bool:
    if int(getattr(session, "dungeon_clear_phase", 0) or 0) != 1:
        return False
    if getattr(session, "dungeon_hell_enabled", False):
        room = _room_state.get_room_state(
            session,
            maze_index=session.dungeon_maze_index,
            cell_x=session.dungeon_hell_x,
            cell_y=session.dungeon_hell_y,
            map_index=session.dungeon_hell_map_index,
        )
        if room is None:
            return False
        actors = [actor for actor in room["monsters"] if actor.get("is_hell_party_actor")]
        return bool(actors) and all(actor.get("killed") for actor in actors)
    if not getattr(session, "dungeon_special_hell", False):
        return False
    # Standalone special-Hell stages have no generated Hell Party room.
    if session.dungeon_cell_x != session.dungeon_boss_x or session.dungeon_cell_y != session.dungeon_boss_y:
        return False
    blockers = [actor for actor in session.dungeon_room_mons if actor.get("is_blocking", True)]
    return bool(blockers) and all(actor.get("killed") for actor in blockers)


def advance_special_dungeon_selector_progress(
    server: Any,
    session: Any,
    *,
    character_id: int,
    dungeon_id: int,
    clear_phase: int,
) -> Tuple[Optional[int], str]:
    """Persist one verified highest special-selector clear for the account."""
    did = int(dungeon_id)
    if not is_special_dungeon(did):
        return None, "special_selector skip=normal_dungeon"
    if int(clear_phase) < 1:
        return None, "special_selector skip=clear_phase_%d" % int(clear_phase)
    if session is None:
        return None, "special_selector skip=no_session"
    run_dungeon_id = int(
        getattr(session, "dungeon_special_selector_dungeon_id", 0) or 0
    )
    if run_dungeon_id != did:
        return None, "special_selector skip=run_mismatch run=%d dungeon=%d" % (
            run_dungeon_id,
            did,
        )
    canonical_key = canonical_special_selector_progress_key(did)
    if canonical_key is None:
        return None, "special_selector skip=missing_progress_key"
    try:
        run_progress_key = int(
            getattr(session, "dungeon_special_selector_progress_key", 0) or 0
        )
    except (TypeError, ValueError):
        run_progress_key = 0
    if run_progress_key != int(canonical_key):
        return None, (
            "special_selector skip=progress_key_mismatch run=%d expected=%d"
            % (run_progress_key, int(canonical_key))
        )
    entered_highest = normalize_special_selector_highest_difficulty(
        getattr(session, "dungeon_special_selector_highest_unlocked", 0)
    )
    try:
        entry_option = int(
            getattr(session, "dungeon_special_selector_entry_option", 0) or 0
        )
    except (TypeError, ValueError):
        return None, "special_selector skip=invalid_option_value"
    if int(character_id) <= 0:
        return None, "special_selector skip=character_missing"
    try:
        repo = server._repo()
        account_id = repo.get_account_id_for_character(int(character_id))
        if account_id is None:
            raise ValueError("character account missing")
        saved_highest = repo.get_account_special_dungeon_highest_unlocked(
            int(account_id),
            int(canonical_key),
        )
        highest = max(
            entered_highest,
            normalize_special_selector_highest_difficulty(saved_highest),
        )
        session.dungeon_special_selector_highest_unlocked = highest
    except Exception as exc:
        return None, "special_selector err=%s:%s" % (type(exc).__name__, exc)

    if not special_entry_option_is_unlocked(entry_option, highest):
        return highest, (
            "special_selector skip=invalid_option selected=%d highest=%d"
            % (entry_option, highest)
        )
    if entry_option != highest:
        return highest, (
            "special_selector skip=not_highest selected=%d highest=%d"
            % (entry_option, highest)
        )
    next_highest = next_special_selector_highest_difficulty(highest, entry_option)
    if next_highest <= highest:
        return highest, (
            "special_selector capped selected=%d highest=%d"
            % (entry_option, highest)
        )
    try:
        persisted_highest = (
            repo.advance_account_special_dungeon_highest_unlocked(
                int(account_id),
                int(canonical_key),
                next_highest,
            )
        )
        session.dungeon_special_selector_highest_unlocked = int(persisted_highest)
        return int(persisted_highest), (
            "special_selector account=%d progress_key=%d dungeon=%d selected=%d "
            "highest=%d->%d"
            % (
                int(account_id),
                int(canonical_key),
                did,
                entry_option,
                highest,
                int(persisted_highest),
            )
        )
    except Exception as exc:
        return None, "special_selector err=%s:%s" % (type(exc).__name__, exc)


class SetPlayResult002EService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_DUNGEON_SET_PLAY_RESULT_HANDLE", True)
        base = (
            "C1/002E SET_PLAY_RESULT plain_len=%s handle=%s"
            % (len(body or b""), int(handle_on))
        )
        if not handle_on:
            return [], base + "; gate=OFF"

        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        phase = int(getattr(session, "dungeon_clear_phase", 0) or 0) if session is not None else 0
        if not in_run:
            return [], base + "; reject=not_in_dungeon_run"
        if phase >= 2:
            return [], base + "; ignore=already_settled phase=%s" % phase
        if (
            session is not None
            and bool(getattr(session, "another_arad_active", False))
            and phase != 1
        ):
            return [], base + "; reject=mirror_clear_phase_%s" % phase

        cid = _cid(session)
        user_key = cid & 0xFFFF
        if session is not None:
            try:
                selector_player_uid = int(
                    getattr(session, "special_dungeon_selector_player_uid", 0) or 0
                )
            except (TypeError, ValueError):
                selector_player_uid = 0
            if 0 < selector_player_uid <= 0xFFFF:
                user_key = selector_player_uid
        dungeon_id = int(
            getattr(session, "selected_dungeon_id", 0)
            or getattr(session, "cur_dungeon_id", 0)
            or 0
        )
        kill_exp = int(getattr(session, "dungeon_run_kill_exp", 0) or 0) if session else 0
        kill_growth_contract_exp = (
            int(getattr(session, "dungeon_run_growth_exp", 0) or 0)
            if session
            else 0
        )
        kill_neo_premium_exp = (
            int(getattr(session, "dungeon_run_neo_exp", 0) or 0)
            if session
            else 0
        )
        kill_fatigue_burning_exp = (
            int(getattr(session, "dungeon_run_fatigue_burn_exp", 0) or 0)
            if session
            else 0
        )
        basic_lv = 1
        if session is not None:
            basic_lv = int(getattr(session, "dungeon_basis_level", 0) or 0)
            if basic_lv <= 0:
                mons = list(getattr(session, "dungeon_room_mons", None) or [])
                if mons:
                    basic_lv = max(1, int(mons[0].get("level") or 1))
                else:
                    basic_lv = 1
        difficulty = 0
        weight = 1.0
        if session is not None:
            difficulty = int(
                getattr(session, "selected_difficulty", None)
                or getattr(session, "cur_dungeon_difficulty", 0)
                or 0
            ) & 0xFF
            try:
                weight = float(
                    getattr(session, "dungeon_experience_weight", 1.0) or 1.0
                )
            except Exception:
                weight = 1.0
            if weight < 0:
                weight = 1.0

        play_time_ms = int(getattr(session, "dungeon_run_play_time_ms", 0) or 0)
        best_time_ms = int(getattr(session, "dungeon_run_best_time_ms", 0) or 0)
        time_note = "dungeon_time=0ms best=0ms"
        # A run's timer is frozen exactly once at the first clear transition.
        # Repeated 002E packets are rejected above and therefore cannot update
        # either the current or the persisted best value.
        if phase == 1 and play_time_ms <= 0:
            started = getattr(session, "dungeon_run_started_monotonic", None)
            if started is not None:
                try:
                    elapsed_seconds = time.monotonic() - float(started)
                    if elapsed_seconds > 0:
                        play_time_ms = max(
                            1,
                            min(0xFFFFFFFF, int(elapsed_seconds * 1000)),
                        )
                except (TypeError, ValueError, OverflowError):
                    play_time_ms = 0
            if play_time_ms > 0 and cid > 0 and dungeon_id > 0:
                try:
                    time_result = self.server._repo().record_dungeon_best_time(
                        int(cid), int(dungeon_id), int(difficulty), int(play_time_ms)
                    )
                    best_time_ms = int(time_result["best_time_ms"])
                    time_note = "dungeon_time=%dms best=%dms accepted=%d" % (
                        play_time_ms,
                        best_time_ms,
                        int(bool(time_result["accepted"])),
                    )
                except Exception as exc:
                    best_time_ms = play_time_ms
                    time_note = "dungeon_time=%dms best=%dms record_err=%s" % (
                        play_time_ms,
                        best_time_ms,
                        type(exc).__name__,
                    )
            elif play_time_ms > 0:
                best_time_ms = play_time_ms
                time_note = "dungeon_time=%dms best=%dms no_record_key" % (
                    play_time_ms,
                    best_time_ms,
                )
            session.dungeon_run_play_time_ms = play_time_ms
            session.dungeon_run_best_time_ms = best_time_ms

        mirror_claim = None
        mirror_claim_note = "mirror_mission=skip"
        if (
            cid > 0
            and session is not None
            and bool(getattr(session, "another_arad_active", False))
            and phase == 1
        ):
            try:
                mirror_claim = _mirror_mission.settle_and_claim_another_arad_reward(
                    self.server,
                    session,
                    character_id=int(cid),
                )
                mirror_claim_note = mirror_claim.note
            except Exception as exc:
                mirror_claim_note = "mirror_mission err=%s" % type(exc).__name__
        elif session is not None and bool(
            getattr(session, "another_arad_active", False)
        ):
            mirror_claim_note = "mirror_mission=skip_clear_phase_%d" % phase

        client_rank = _extract_client_rank_point(body)
        clear_parts = compute_clear_reward_exp(
            basic_lv,
            experience_weight=weight,
            difficulty=difficulty,
            client_rank_point=client_rank,
        )
        clear_base = int(clear_parts["clear_base"])
        score_bonus = int(clear_parts["score_bonus"])
        clear_core = min(0xFFFFFFFF, clear_base + score_bonus)
        consumable_exp_bonus = 0
        consumable_exp_rate_ppm = 0
        consumable_exp_note = "consumable_exp=0@0ppm"
        if cid > 0 and clear_base > 0:
            try:
                active_exp_effect = (
                    _exp_bonus_consumable.get_active_exp_bonus_effect(
                        self.server._repo(),
                        int(cid),
                    )
                )
                if active_exp_effect is not None:
                    consumable_exp_rate_ppm = max(
                        0,
                        int(active_exp_effect.bonus_rate_ppm),
                    )
                    consumable_exp_bonus = active_exp_effect.compute_bonus_exp(
                        clear_base
                    )
                    consumable_exp_note = (
                        "consumable_exp=%d@%dppm item=%d expires_at=%d"
                        % (
                            consumable_exp_bonus,
                            consumable_exp_rate_ppm,
                            int(active_exp_effect.source_item_id),
                            int(active_exp_effect.expires_at),
                        )
                    )
            except Exception as exc:
                consumable_exp_note = "consumable_exp_err=%r" % (exc,)
        premium_exp_bonus = 0
        growth_contract_exp_bonus = 0
        neo_premium_exp_bonus = 0
        premium_exp_percent = 0
        premium_note = "premium_exp=0@0%"
        if cid > 0 and clear_base > 0:
            try:
                premium_effects = get_active_premium_effects(
                    self.server._repo(),
                    int(getattr(self.server, "account_id", 1) or 1),
                    session=session,
                )
                premium_exp_percent = max(
                    0, int(premium_effects.bonus_exp_percent)
                )
                premium_exp_bonus = premium_effects.compute_bonus_exp(clear_base)
                growth_contract_exp_bonus = premium_effects.compute_growth_contract_exp(
                    clear_base
                )
                neo_premium_exp_bonus = premium_effects.compute_neo_premium_exp(
                    clear_base
                )
                premium_note = "premium_exp=%d@%d%% growth=%d neo=%d" % (
                    premium_exp_bonus,
                    premium_exp_percent,
                    growth_contract_exp_bonus,
                    neo_premium_exp_bonus,
                )
            except Exception as exc:
                premium_note = "premium_exp_err=%r" % (exc,)

        fatigue_burn_bonus = 0
        fatigue_burn_note = "fatigue_burn_exp=0@0%"
        if cid > 0 and clear_base > 0:
            try:
                fatigue_burn_bonus = (
                    _activity_state.compute_fatigue_burning_exp_bonus(
                        self.server._repo(),
                        int(cid),
                        clear_base,
                        session=session,
                    )
                )
                fatigue_burn_note = "fatigue_burn_exp=%d@%d%%" % (
                    fatigue_burn_bonus,
                    (
                        _activity_state.FATIGUE_BURNING_EXP_PERCENT
                        if fatigue_burn_bonus > 0
                        else 0
                    ),
                )
            except Exception as exc:
                fatigue_burn_note = "fatigue_burn_exp_err=%r" % (exc,)

        adventure_exp_bonus = 0
        adventure_exp_percent = 0
        adventure_exp_note = "adventure_exp=0@0%"
        if cid > 0 and clear_core > 0:
            try:
                repo = self.server._repo()
                account_id = repo.get_account_id_for_character(int(cid))
                if account_id is None:
                    raise ValueError("character account missing")
                adventure_benefits = calculate_adventure_group_benefits_for_account(
                    repo,
                    int(account_id),
                )
                adventure_exp_percent = max(
                    0,
                    int(adventure_benefits.clear_result_exp_percent),
                )
                adventure_exp_bonus = (
                    int(clear_core) * int(adventure_exp_percent) // 100
                )
                adventure_exp_note = "adventure_exp=%d@%d%%" % (
                    adventure_exp_bonus,
                    adventure_exp_percent,
                )
            except Exception as exc:
                adventure_exp_note = "adventure_exp_err=%r" % (exc,)
        clear_total = min(
            0xFFFFFFFF,
            int(clear_core)
            + int(consumable_exp_bonus)
            + int(premium_exp_bonus)
            + int(fatigue_burn_bonus)
            + int(adventure_exp_bonus),
        )
        settlement_exp_total = min(0xFFFFFFFF, int(clear_total) + int(kill_exp))

        packets: list[bytes] = []
        notes: list[str] = [
            time_note,
            consumable_exp_note,
            premium_note,
            fatigue_burn_note,
            adventure_exp_note,
        ]
        if mirror_claim is not None:
            notes.append(mirror_claim_note)
            if mirror_claim.granted:
                try:
                    body_mirror_000e = build_item_list_update_000e(
                        mirror_claim.granted,
                        list_type=0,
                    )
                    packets.append(
                        self.server._codec_response(0, 0x000E, body_mirror_000e)
                    )
                    notes.append(
                        "mirror_reward_000e granted=%d mailed=%d item=%d"
                        % (
                            len(mirror_claim.granted),
                            len(mirror_claim.mailed),
                            int(mirror_claim.reward_item_id),
                        )
                    )
                except Exception as exc:
                    notes.append("mirror_reward_000e err=%s" % type(exc).__name__)

        body_0022 = build_play_result_0022(
            play_time_ms=play_time_ms,
            best_time_ms=best_time_ms,
            user_key=user_key,
        )
        pkt, mode = self._emit(0, 0x0022, body_0022, "codec")
        packets.append(pkt)
        notes.append(
            "0022/%s/len=%d play_time_ms=%d best_time_ms=%d "
            "settlement_exp_total=%d clear_base=%d score_bonus=%d "
            "consumable_bonus=%d premium_bonus=%d fatigue_burn_bonus=%d "
            "adventure_bonus=%d kill_exp=%d "
            "kill_growth=%d kill_neo=%d kill_fatigue=%d "
            "basic_lv=%d weight=%.3f diff=%d rank=%d rank_idx=%s"
            % (
                mode,
                len(body_0022),
                play_time_ms,
                best_time_ms,
                settlement_exp_total,
                clear_base,
                score_bonus,
                consumable_exp_bonus,
                premium_exp_bonus,
                fatigue_burn_bonus,
                adventure_exp_bonus,
                kill_exp,
                kill_growth_contract_exp,
                kill_neo_premium_exp,
                kill_fatigue_burning_exp,
                basic_lv,
                weight,
                difficulty,
                clear_parts["rank_point"],
                clear_parts["rank_bonus_index"],
            )
        )

        # PVF [condition under clear]: the quest module decides whether this
        # settled run satisfies each active quest.  002E only supplies facts
        # owned by this session and persists the resulting residual values.
        if _env_bool("USDOF_GAME_QUEST_CLEAR_PROGRESS", True) and cid > 0:
            try:
                session_worldmap_id = getattr(session, "dungeon_worldmap_id", None)
                worldmap_id = (
                    int(session_worldmap_id)
                    if session_worldmap_id is not None
                    else -1
                )
                if worldmap_id < 0:
                    resolved_worldmap = worldmap_id_for_dungeon(dungeon_id)
                    worldmap_id = (
                        int(resolved_worldmap)
                        if resolved_worldmap is not None
                        else -1
                    )
                seen_companions, alive_companions = companion_progress_codes(session)
                required_room_cells = frozenset(
                    (int(cell[0]), int(cell[1]))
                    for cell in list(
                        getattr(session, "dungeon_required_cells", None) or []
                    )
                    if isinstance(cell, (list, tuple)) and len(cell) >= 2
                )
                cleared_room_cells = frozenset(cleared_cells_snapshot(session))
                clear_context = DungeonClearContext(
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                    map_id=int(getattr(session, "dungeon_map_index", 0) or 0),
                    worldmap_id=worldmap_id,
                    hell_party_enabled=bool(
                        getattr(session, "dungeon_hell_enabled", False)
                    ),
                    recommended_dungeon=bool(
                        getattr(session, "dungeon_recommended", False)
                    ),
                    seen_companion_codes=seen_companions,
                    alive_companion_codes=alive_companions,
                    required_room_cells=required_room_cells,
                    cleared_room_cells=cleared_room_cells,
                )
                repo = self.server._repo()
                active_rows = repo.list_active_quests(int(cid))
                active_pairs = [
                    (int(quest_id), int(trigger))
                    for _slot, quest_id, trigger in active_rows
                ]
                progress = apply_dungeon_clear_to_active_quests(
                    active_pairs,
                    context=clear_context,
                )
                updated_pairs: list[tuple[int, int]] = []
                update_notes: list[str] = []
                for quest_id, _old, new, progress_note in progress:
                    ok, update_note = repo.update_active_quest_trigger(
                        int(cid), int(quest_id), int(new)
                    )
                    if ok:
                        updated_pairs.append((int(quest_id), int(new)))
                        update_notes.append(progress_note)
                    else:
                        update_notes.append(
                            "clear q=%s db_skip=%s" % (quest_id, update_note)
                        )
                if updated_pairs:
                    active_after = [
                        (int(quest_id), int(trigger))
                        for _slot, quest_id, trigger in repo.list_active_quests(int(cid))
                    ]
                    finished_after = [
                        int(quest_id)
                        for quest_id, _flag in repo.list_finished_quests(int(cid))
                    ]
                    bind_session_quest_state(
                        session,
                        active_pairs=active_after,
                        finished_ids=finished_after,
                    )
                    quest_packets, quest_note = pack_set_trigger_ack_packets(
                        self.server,
                        updated_pairs,
                    )
                    packets.extend(quest_packets)
                    notes.append(
                        "quest_clear n=%s wm=%s hell=%s %s %s"
                        % (
                            len(updated_pairs),
                            worldmap_id,
                            int(clear_context.hell_party_enabled),
                            quest_note,
                            " | ".join(update_notes),
                        )
                    )
                else:
                    notes.append(
                        "quest_clear n=0 dungeon=%s wm=%s hell=%s"
                        % (
                            dungeon_id,
                            worldmap_id,
                            int(clear_context.hell_party_enabled),
                        )
                    )
            except Exception as exc:
                notes.append("quest_clear err=%r" % (exc,))
        else:
            notes.append("quest_clear skip=gate_or_cid")

        # Freeze the ordinary card rewards once here.  The card index remains
        # client-local on Stage3; every later C1/0047 must consume this exact
        # plan instead of rolling a second time.
        cleared_cells = tuple(cleared_cells_snapshot(session))
        total_room_count = len(
            tuple(getattr(session, "dungeon_required_cells", None) or ())
        )
        visited_room_count = len(cleared_cells)
        if total_room_count <= 0:
            total_room_count = max(1, visited_room_count)
        if visited_room_count <= 0:
            visited_room_count = 1
        try:
            card_seed = (
                int(getattr(session, "dungeon_drop_lcg_seed", 0) or 0)
                ^ int(getattr(session, "dungeon_seed", 0) or 0)
                ^ ((int(dungeon_id) * 0x9E3779B1) & 0xFFFFFFFF)
                ^ ((int(cid) * 0x85EBCA6B) & 0xFFFFFFFF)
            ) & 0xFFFFFFFF
            card_plan = build_clear_reward_plan(
                dungeon_level=basic_lv,
                difficulty=difficulty,
                visited_room_count=visited_room_count,
                total_room_count=total_room_count,
                seed=card_seed or 1,
            )
            notes.append(
                "card_pvf level=%s diff=%s rooms=%s/%s seed=%08X->%08X "
                "free=%s+%sx%s paid=%sx%s cost=%s"
                % (
                    basic_lv,
                    difficulty,
                    visited_room_count,
                    total_room_count,
                    card_plan.seed_before,
                    card_plan.seed_after,
                    card_plan.free_gold,
                    card_plan.free_item_id,
                    card_plan.free_item_count,
                    card_plan.paid_item_id,
                    card_plan.paid_item_count,
                    card_plan.paid_cost,
                )
            )
        except Exception as card_plan_exc:
            # Startup warmup is hard-fail.  This guard keeps ad-hoc unit
            # sessions from inventing a non-PVF fallback reward.
            from game.dungeon.clear_reward import ClearRewardPlan

            card_plan = ClearRewardPlan()
            notes.append(
                "card_pvf err=%s:%s"
                % (type(card_plan_exc).__name__, card_plan_exc)
            )

        free_gold = max(0, int(card_plan.free_gold))
        free_item_id = max(0, int(card_plan.free_item_id))
        free_item_count = max(0, int(card_plan.free_item_count))
        paid_item_id = max(0, int(card_plan.paid_item_id))
        paid_item_count = max(0, int(card_plan.paid_item_count))
        paid_card_cost = max(0, int(card_plan.paid_cost))
        # Persist clear total only (kills already applied on DIE).
        # SKIP_CARD_FLIP=1 grants this frozen free side below.  The legacy
        # gold_delta mode is retained only when there is no PVF item to avoid
        # an immediate gold duplicate followed by C1/0047.
        skip_card = _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_FLIP", False)
        gold_delta = 0
        if (
            _env_bool("USDOF_GAME_DUNGEON_PLAY_RESULT_GRANT_GOLD", False)
            and not skip_card
            and free_item_id <= 0
        ):
            # 旧兼容：结算走 grant_exp 的 gold_delta；skip 路径改用 grant_wallet_gold+000E
            gold_delta = free_gold
        level_after = None
        exp_after = None
        expertise_after = None
        levels_gained = 0
        level_title_grants = []
        if (
            _env_bool("USDOF_GAME_DUNGEON_PLAY_RESULT_APPLY_EXP", True)
            and cid > 0
            and (clear_total > 0 or gold_delta > 0)
        ):
            try:
                result = grant_exp_to_character(
                    self.server._repo(),
                    int(cid),
                    int(clear_total),
                    gold_delta=gold_delta,
                    session=session,
                )
                level_after = result.get("new_level")
                exp_after = result.get("exp")
                expertise_after = result.get("expertise_exp")
                levels_gained = int(result.get("levels_gained") or 0)
                notes.append(
                    "db_clear ok=%s level=%s->%s exp=%s ordinary_added=%s "
                    "expertise=%s expertise_added=%s gold=%s clear_total=%s "
                    "(base=%s score=%s consumable=%s premium=%s "
                    "fatigue_burn=%s adventure=%s) "
                    "free_gold_plan=%s grant_gold_now=%s skip_card=%s"
                    % (
                        result.get("ok"),
                        result.get("old_level"),
                        result.get("new_level"),
                        result.get("exp"),
                        result.get("ordinary_exp_added"),
                        result.get("expertise_exp"),
                        result.get("expertise_exp_added"),
                        result.get("gold"),
                        clear_total,
                        clear_base,
                        score_bonus,
                        consumable_exp_bonus,
                        premium_exp_bonus,
                        fatigue_burn_bonus,
                        adventure_exp_bonus,
                        free_gold,
                        gold_delta,
                        int(skip_card),
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
                notes.append("db_clear err=%r" % (exc,))

        levelup_exp_level = None
        if _env_bool("USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_EXP", False):
            try:
                if (
                    level_after is None
                    or exp_after is None
                    or expertise_after is None
                ):
                    if cid > 0:
                        try:
                            lv0, exp0, expertise0 = (
                                self.server._repo().get_character_progression(
                                    int(cid)
                                )
                            )
                            exp_after = int(exp0 or 0)
                            level_after = int(lv0 or 1)
                            expertise_after = int(expertise0 or 0)
                        except Exception:
                            pass
                    if level_after is None:
                        level_after = int(
                            getattr(session, "character_level", None)
                            or getattr(session, "level", None)
                            or 1
                        )
                    if exp_after is None:
                        exp_after = int(getattr(session, "character_exp", 0) or 0)
                    if expertise_after is None:
                        expertise_after = int(
                            getattr(session, "character_expertise_exp", 0) or 0
                        )
                layout = (
                    os.environ.get("USDOF_GAME_DUNGEON_EXP_NOTI_LAYOUT") or "stage3_78"
                )
                lv_i = max(1, min(255, int(level_after or 1)))
                exp_i = int(exp_after or 0) & 0xFFFFFFFF
                expertise_i = max(0, int(expertise_after or 0))
                sp_state = resolve_skill_point_protocol_state(
                    level=lv_i,
                    character_id=int(cid or 0),
                    repo=self.server._repo() if cid > 0 else None,
                    session=session,
                )
                # exp_i is absolute authority; detail fields describe only
                # this clear delta so the native toast can expand every real
                # contribution without changing the persisted total.
                body_0025 = build_exp_notification_0025(
                    lv_i,
                    exp_i,
                    expertise_exp=expertise_i,
                    page0_sp=sp_state.page0_sp,
                    page1_sp=sp_state.page1_sp,
                    page0_tp=sp_state.page0_tp,
                    page1_tp=sp_state.page1_tp,
                    pvp_victory_point=0,
                    growth_contract_bonus_exp=growth_contract_exp_bonus,
                    neo_premium_bonus_exp=neo_premium_exp_bonus,
                    fatigue_burn_bonus_exp=fatigue_burn_bonus,
                    gain_exp=consumable_exp_bonus,
                    pairs=(
                        [(135, adventure_exp_bonus)]
                        if adventure_exp_bonus > 0
                        else None
                    ),
                    layout=layout,
                )
                pkt_e, mode_e = self._emit(0, 0x0025, body_0025, "codec")
                packets.append(pkt_e)
                if int(levels_gained or 0) > 0:
                    levelup_exp_level = lv_i
                plain_len = len(body_0025)
                wire_total = len(pkt_e)
                cipher_len = max(0, wire_total - 16)
                if bool(getattr(self.server, "diagnostic_trace", False)):
                    try:
                        self.server.log(
                            "[Game] send clear-exp-0025 flag=0x00 opcode=0x0025 "
                            "plain=0x%04x cipher=0x%04x total=0x%04x mode=%s "
                            "layout=%s level=%s total_exp=%s expertise_exp=%s "
                            "sp=%s/%s hex=%s"
                            % (
                                plain_len,
                                cipher_len,
                                wire_total,
                                mode_e,
                                layout,
                                lv_i,
                                exp_i,
                                expertise_i,
                                sp_state.page0_sp,
                                sp_state.page1_sp,
                                body_0025.hex(" "),
                            )
                        )
                    except Exception:
                        pass
                notes.append(
                    "0025/%s/len=%d level=%s exp=%s expertise=%s layout=%s "
                    "sp=%s/%s "
                    "wire plain=0x%04x cipher=0x%04x total=0x%04x"
                    % (
                        mode_e,
                        plain_len,
                        lv_i,
                        exp_i,
                        expertise_i,
                        layout,
                        sp_state.page0_sp,
                        sp_state.page1_sp,
                        plain_len,
                        cipher_len,
                        wire_total,
                    )
                )
                if session is not None:
                    try:
                        session.character_level = lv_i
                        session.character_exp = exp_i
                        session.character_expertise_exp = expertise_i
                        session.client_exp_primed = True
                    except Exception:
                        pass
            except Exception as exc:
                notes.append("0025_EXP err=%r" % (exc,))
        else:
            notes.append("0025/skip=settlement_exp_refresh_off")

        # A clear-reward level-up has the same acceptable-quest eligibility
        # change as a monster-kill level-up.  Emit only the established 0015
        # full rebuild; active/finished state is unchanged by this path.
        if int(levels_gained or 0) > 0 and cid > 0:
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

        if levelup_exp_level is not None and cid > 0:
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

        body_0023 = None
        if _env_bool("USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_0023", True):
            # Stage3 sub_1645DA0 完整体；show_result=1 打开结果窗，否则点回城不发包。
            # The first card group projects the frozen free side.  The post
            # group commission is the lower-card gold price; real ownership
            # still changes only through C1/0047 (or explicit SKIP mode).
            body_0023 = build_clear_dungeon_reward_0023_stub(
                clear_base_exp=clear_base,
                score_bonus_exp=score_bonus,
                monster_exp=kill_exp,
                neo_premium_exp=neo_premium_exp_bonus,
                adventure_exp=adventure_exp_bonus,
                potion_exp=consumable_exp_bonus,
                growth_contract_exp=growth_contract_exp_bonus,
                fatigue_burning_exp=fatigue_burn_bonus,
                kill_growth_contract_exp=kill_growth_contract_exp,
                kill_neo_premium_exp=kill_neo_premium_exp,
                kill_fatigue_burning_exp=kill_fatigue_burning_exp,
                boss_exp=0,
                gold=free_gold,
                free_item_id=free_item_id,
                free_item_count=free_item_count,
                paid_card_cost=paid_card_cost,
                show_result=1,
            )
            pkt3, mode3 = self._emit(0, 0x0023, body_0023, "codec")
            packets.append(pkt3)
            # ShowResult 相对 tail: pretail(4)+value0(4)+show(1) → 倒数第 18 字节
            show_off = max(0, len(body_0023) - 18)
            show_b = body_0023[show_off] if body_0023 else 0
            notes.append(
                "0023/%s/len=%d show_result=%s free=%s+%sx%s paid=%sx%s cost=%s "
                "stage3_full=1"
                % (
                    mode3,
                    len(body_0023),
                    show_b,
                    free_gold,
                    free_item_id,
                    free_item_count,
                    paid_item_id,
                    paid_item_count,
                    paid_card_cost,
                )
            )

        free_gold_delivered = bool(gold_delta > 0)
        free_item_delivered = False

        # 结算布局 0045/0046：Stage3 进入通关结算态（维修商人、右上角回城/下一图）。
        # live 20260718：SKIP 时若省略，客户端仅有评分 ESC，并仍可 MOVE_MAP 重刷 boss；
        # 随后 C1/0045 empty 无 handler。故 **始终发布局**；SKIP 只绕过交互 0047。
        if _env_bool("USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_CARD_LAYOUT", True):
            try:
                body_45 = build_card_select_right_state_0045(1)
                pkt45, mode45 = self._emit(1, 0x0045, body_45, "codec")
                packets.append(pkt45)
                body_46 = build_card_layout_ack_0046()
                pkt46, mode46 = self._emit(1, 0x0046, body_46, "codec")
                packets.append(pkt46)
                notes.append(
                    "0045/%s/len=%d 0046/%s/len=%d card_layout=1 skip_flip=%s"
                    % (mode45, len(body_45), mode46, len(body_46), int(skip_card))
                )
                if session is not None:
                    session.dungeon_card_layout_sent = True
                    if skip_card:
                        # 视作免交互翻完，避免卡在 0047；金币见下方 skip_deliver
                        session.dungeon_card_flipped = True
            except Exception as exc:
                notes.append("card_layout err=%r" % (exc,))
        else:
            notes.append("card_layout skip=SEND_CARD_LAYOUT=0")
            if session is not None and skip_card:
                try:
                    session.dungeon_card_flipped = True
                except Exception:
                    pass

        # SKIP：结算直接领取冻结的免费侧（金币和物品），但仍按真实
        # list0 条目刷新。日常交互模式不会走这里。
        free_delivered = bool(
            free_gold_delivered and (free_item_id <= 0 or free_item_delivered)
        )
        if (
            skip_card
            and cid > 0
            and _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_DELIVER_REWARD", True)
        ):
            try:
                repo = self.server._repo()
                update_entries: list[tuple[int, ...]] = []
                if free_gold > 0 and not free_gold_delivered:
                    ok_g, note_g, wallet_total = repo.grant_wallet_gold(cid, free_gold)
                    free_gold_delivered = bool(ok_g)
                    notes.append(
                        "skip_deliver_free gold_ok=%s amt=%s wallet=%s %s"
                        % (int(ok_g), free_gold, wallet_total, note_g)
                    )
                    if ok_g:
                        update_entries.append(
                            (
                                0,
                                0,
                                _payloads.clamp_wallet_gold_for_client(wallet_total),
                            )
                        )
                elif free_gold <= 0:
                    free_gold_delivered = True

                if free_item_id > 0 and free_item_count > 0:
                    ok_i, note_i, slot = repo.grant_inventory_item(
                        cid, free_item_id, free_item_count
                    )
                    free_item_delivered = bool(
                        ok_i
                        and (
                            int(slot) >= 0
                            or is_virtual_inventory_grant(note_i)
                        )
                    )
                    notes.append(
                        "skip_deliver_free item_ok=%s id=%s cnt=%s slot=%s %s"
                        % (
                            int(ok_i),
                            free_item_id,
                            free_item_count,
                            slot,
                            note_i,
                        )
                    )
                    premium_notice = parse_virtual_premium_grant(note_i)
                    if premium_notice is not None:
                        if session is not None:
                            try:
                                session.account_premium_effects = None
                            except Exception:
                                pass
                        helper = load_chain("helper-system_reward_activation.py")
                        packets.append(
                            self.server._codec_response(
                                0,
                                0x0042,
                                helper.build_premium_activation_notice(
                                    *premium_notice
                                ),
                            )
                        )
                        notes.append(
                            "0042_premium type=%d remaining=%d"
                            % (int(premium_notice[0]), int(premium_notice[1]))
                        )
                    if free_item_delivered and int(slot) >= 0:
                        row = repo.get_inventory_item_at(
                            cid,
                            list_type=0,
                            slot_index=int(slot),
                        )
                        is_equipment = str((row or {}).get("item_kind") or "") == "equipment"
                        value = int(
                            (row or {}).get(
                                "instance_value" if is_equipment else "stack_count",
                                free_item_count,
                            )
                            or free_item_count
                        )
                        durability = int((row or {}).get("durability") or 0)
                        projected_item_id = int(
                            (row or {}).get("item_template_id", free_item_id)
                            or free_item_id
                        )
                        update_entries.append(
                            (
                                int(slot) & 0xFFFF,
                                projected_item_id & 0xFFFFFFFF,
                                value,
                                durability,
                            )
                        )
                else:
                    free_item_delivered = True

                free_delivered = bool(free_gold_delivered and free_item_delivered)
                if update_entries and _env_bool(
                    "USDOF_GAME_DUNGEON_SKIP_CARD_SEND_000E", True
                ):
                    body_000e = build_item_list_update_000e(
                        update_entries,
                        list_type=0,
                    )
                    pkt_e, mode_e = self._emit(0, 0x000E, body_000e, "codec")
                    packets.append(pkt_e)
                    notes.append(
                        "000E/%s/len=%d skip_card_entries=%s"
                        % (mode_e, len(body_000e), len(update_entries))
                    )
            except Exception as exc:
                notes.append("skip_deliver_free err=%r" % (exc,))
                free_delivered = False

        if is_special_dungeon(dungeon_id):
            _special_highest, special_note = (
                advance_special_dungeon_selector_progress(
                    self.server,
                    session,
                    character_id=cid,
                    dungeon_id=dungeon_id,
                    clear_phase=phase,
                )
            )
            notes.append(special_note)
            if _special_highest is not None:
                if 0 < user_key <= 0xFFFF and cid > 0:
                    context, context_note = special_selector_state_context(
                        self.server._repo(),
                        session,
                        user_key,
                        character_id=cid,
                    )
                    if context is None:
                        notes.append(
                            "special_selector_sync tag=settlement uid=%d %s"
                            % (user_key, context_note)
                        )
                    else:
                        player_uid, selected_cid, account_id, progress = context
                        try:
                            state_packet, records_packet = (
                                build_special_selector_sync_packets(
                                    self.server,
                                    player_uid,
                                    progress,
                                )
                            )
                            packets.extend((state_packet, records_packet))
                            notes.append(
                                "special_selector_sync tag=settlement account=%d "
                                "uid=%d cid=%d highest=%d keys=%d "
                                "reply=[S0/0317,S0/0318]"
                                % (
                                    account_id,
                                    player_uid,
                                    selected_cid,
                                    _special_highest,
                                    len(progress),
                                )
                            )
                        except Exception as exc:
                            notes.append(
                                "special_selector_sync tag=settlement uid=%d err=%s:%s"
                                % (player_uid, type(exc).__name__, exc)
                            )
                else:
                    notes.append(
                        "special_selector_sync tag=settlement skip=player_uid_or_cid "
                        "uid=%d cid=%d"
                        % (user_key, cid)
                    )

        grief_tower_cleared = False
        if cid > 0:
            grief_floor = floor_for_dungeon("grief", dungeon_id)
            if grief_floor is not None:
                try:
                    repo = self.server._repo()
                    account_id = repo.get_account_id_for_character(cid)
                    if account_id is None:
                        raise ValueError("character account missing")
                    grief_progress = repo.advance_hundred_floor_tower_progress(
                        account_id,
                        "grief",
                        grief_floor,
                    )
                    grief_tower_cleared = grief_floor == grief_progress == 100
                    body_050e = build_grief_progress_050e(grief_progress)
                    packet_050e, mode_050e = self._emit(
                        0,
                        0x050E,
                        body_050e,
                        "codec",
                    )
                    packets.append(packet_050e)
                    notes.append(
                        "050E/%s/len=%d grief_floor=%d cleared_floor=%d"
                        % (
                            mode_050e,
                            len(body_050e),
                            grief_floor,
                            grief_progress,
                        )
                    )
                except Exception as grief_progress_exc:
                    notes.append(
                        "grief_progress err=%s:%s"
                        % (
                            type(grief_progress_exc).__name__,
                            grief_progress_exc,
                        )
                    )

        despair_floor_cleared = False
        if cid > 0:
            despair_floor = floor_for_dungeon("despair", dungeon_id)
            if despair_floor is not None:
                try:
                    repo = self.server._repo()
                    account_id = repo.get_account_id_for_character(cid)
                    if account_id is None:
                        raise ValueError("character account missing")
                    despair_progress = repo.advance_hundred_floor_tower_progress(
                        account_id,
                        "despair",
                        despair_floor,
                    )
                    despair_floor_cleared = despair_progress >= despair_floor
                    notes.append(
                        "despair_progress floor=%d cleared_floor=%d no_0328"
                        % (despair_floor, despair_progress)
                    )
                except Exception as despair_progress_exc:
                    notes.append(
                        "despair_progress err=%s:%s"
                        % (
                            type(despair_progress_exc).__name__,
                            despair_progress_exc,
                        )
                    )

        if cid > 0:
            try:
                repo = self.server._repo()
                account_id = repo.get_account_id_for_character(cid)
                if account_id is None:
                    raise ValueError("character account missing")
                adventure_stats, special_stat = record_adventure_group_dungeon_clear(
                    repo,
                    account_id,
                    dungeon_id=dungeon_id,
                    recommended_dungeon=bool(
                        getattr(session, "dungeon_recommended", False)
                    ),
                )
                notes.append(
                    "adventure_clear account=%d dungeon=%d recommended=%d special=%s "
                    "optimal=%d anton=%d luke=%d despair=%d grief=%d"
                    % (
                        account_id,
                        dungeon_id,
                        int(bool(getattr(session, "dungeon_recommended", False))),
                        special_stat,
                        int(adventure_stats.get("optimal_dungeon_clears", 0)),
                        int(adventure_stats.get("anton_phase2_clears", 0)),
                        int(adventure_stats.get("luke_phase2_clears", 0)),
                        int(adventure_stats.get("tower_despair_clears", 0)),
                        int(adventure_stats.get("tower_grief_clears", 0)),
                    )
                )
            except Exception as adventure_clear_exc:
                notes.append(
                    "adventure_clear err=%s:%s"
                    % (type(adventure_clear_exc).__name__, adventure_clear_exc)
                )

        if session is not None:
            try:
                if record_weekly_attendance_clear(self.server, session):
                    notes.append("weekly_attendance_clear=1")
            except (OSError, ValueError, sqlite3.Error) as exc:
                notes.append("weekly_attendance_clear_error=%s:%s" % (type(exc).__name__, exc))
            try:
                if record_adventure_season1_clear(
                    self.server, session, despair_floor_cleared=despair_floor_cleared,
                    grief_tower_cleared=grief_tower_cleared,
                    hell_party_cleared=_cleared_hell_party(session),
                ):
                    notes.append("adventure_season1_clear=1")
            except (OSError, ValueError, sqlite3.Error) as exc:
                notes.append(f"adventure_season1_clear_error={type(exc).__name__}:{exc}")
            session.dungeon_clear_phase = 2
            try:
                session.dungeon_clear_exp_base = clear_base
                session.dungeon_clear_exp_score_bonus = score_bonus
                session.dungeon_clear_exp_consumable_bonus = consumable_exp_bonus
                session.dungeon_clear_exp_premium_bonus = premium_exp_bonus
                session.dungeon_clear_exp_growth_bonus = growth_contract_exp_bonus
                session.dungeon_clear_exp_neo_bonus = neo_premium_exp_bonus
                session.dungeon_clear_exp_fatigue_burn_bonus = fatigue_burn_bonus
                session.dungeon_clear_exp_adventure_bonus = adventure_exp_bonus
                session.dungeon_clear_exp_total = clear_total
                # Persist the exact PVF plan.  Do not regenerate in 0047:
                # the client sends no physical card index on the wire.
                session.dungeon_free_card_gold = free_gold
                stored_plan = card_plan.as_session_dict(
                    free_delivered=bool(free_delivered)
                )
                stored_plan["free_gold_delivered"] = bool(free_gold_delivered)
                stored_plan["free_item_delivered"] = bool(free_item_delivered)
                stored_plan["free_delivered"] = bool(free_delivered)
                session.dungeon_card_reward_plan = stored_plan
                notes.append(_card_auto_flip.arm_card_reward_auto_flip(session))
                # 通关后房间清空：MOVE_MAP 见 phase>=1 时 mon=[]
                session.dungeon_room_mons = []
                notes.append(
                    "card_reward_plan free=%s+%sx%s paid=%sx%s cost=%s "
                    "pre_delivered=%s skip_card=%s"
                    % (
                        free_gold,
                        free_item_id,
                        free_item_count,
                        paid_item_id,
                        paid_item_count,
                        paid_card_cost,
                        int(free_delivered),
                        int(skip_card),
                    )
                )
            except Exception:
                pass

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
                                "play-result delayed level_title i=%s %s"
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

        from game.normal_party_cards import stage_result
        packets, shared_card_note = stage_result(self.server, session, packets, body_0023)
        if shared_card_note:
            notes.append(shared_card_note)
        return packets, (
            "%s; phase %s->2 reply=[%s]; doc_ref=副本实现A §5.6 + s4a12 clear exp; "
            "chain_file=game/chains/opcode002e-set_play_result.py"
            % (base, phase, ", ".join(notes))
        )
