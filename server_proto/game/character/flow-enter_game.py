"""Game 进入游戏请求链兼容入口。

本文件保存进入游戏/进入赛利亚房间阶段的跨包会话状态，并作为旧接口的兼容聚合层。
Select Channel / Exit Game 常驻基线：`docs/baseline_select_channel_exit_20260714.md`。
Exit = 近期 C->S 出现 0x00C2/0x0505/0x0506；否则 0x057F 走 Channel settle。
入口收包仍在 ``game_server.py``，由 ``game.packet_router`` 按 opcode 路由到这里。

编码拆分规则：
- 具体同一发/收包动作产生的链路必须继续拆到 ``enter_seria_room_XX_*.py``。
- 当前已拆分：
  - ``enter_seria_room_01_select_character_ack.py``：SELECT_CHARACTER 绑定角色 +
    最小 ``S->C flag0/opcode=0x0004`` ACK。
  - ``enter_seria_room_02_bind_town_object_0324.py``：SELECT_CHARACTER 后先发
    ``S->C flag0/opcode=0x0324``，让客户端创建/绑定 n2500 城镇对象，再进入
    0x0004 ACK。
- 后续若确认 ``S->C 0x000D`` item list、``S->C 0x0002`` 角色状态或
  town/room 入口包，必须新增同前缀递增编号文件，例如
  ``enter_seria_room_03_item_list.py``，不能继续堆进本文件。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from game.chain_loader import load_chain
EnterSeriaRoomSelectCharacterAckService = load_chain('flow-select_character.py').EnterSeriaRoomSelectCharacterAckService


@dataclass
class GameSessionState:
    """单个 Game TCP 连接的会话状态。"""

    account_id: int | None = None
    account_m_id: str | None = None
    login_session_id: str | None = None
    selected_character_id: int | None = None
    selected_slot: int | None = None
    selected_character_name: str | None = None
    rename_return_select_requested: bool = False
    chat_selection_generation: int = 0
    # Full owner cid of this connection's strict Anton temporary town spawn.
    anton_waiting_position_character_id: int | None = None
    luke_waiting_position_character_id: int | None = None
    # Authoritative character snapshot bound by START_MAP USERINFO setup.
    character_job: int | None = None
    character_grow_type: int | None = None
    character_visibility_bits: int | None = None
    character_level: int | None = None
    character_exp: int | None = None
    character_expertise_exp: int | None = None
    skill_remain_sp: int | None = None
    skill_remain_tp: int | None = None
    client_exp_primed: bool = False
    # Persisted fatigue-burning activity switch; hydrated after character bind.
    fatigue_burning_enabled: bool = False
    fatigue_burning_hydrated: bool = False
    fatigue_burning_character_id: int = 0
    # PVF channel_id recovered from local Game listen port (base+channel_id).
    selected_channel_id: int | None = None
    # Set after S->C 0x0323/0x020C were successfully queued for this move/exit.
    channel_move_settled: bool = False
    # Last 0x057F settle profile: "channel" | "exit" | "none" | None
    # Used so post-Channel C->S 0x0003 closes Game TCP WITHOUT SuccessAck
    # (Exit Game SuccessAck path must stay intact).
    last_057f_profile: str | None = None
    last_057f_at: float | None = None
    # Channel-select settle already sent early SuccessAck/0x0003 and may close TCP.
    # Suppress duplicate SuccessAck on a late C->S 0x0003 for the same leave.
    channel_leave_early_ack_sent: bool = False
    channel_leave_close_after_settle: bool = False
    # True when this Game TCP opened charselect via short-leave reentry
    # (USERINFO fallback / force 0x0004). Select Channel then packs list
    # count=2 (server mode=2) which must NOT be treated as Exit Game.
    reentry_forced_charselect: bool = False
    # Rolling C->S opcodes on this TCP (for Exit vs Select Channel discrimination).
    recent_c2s_opcodes: list = None  # type: ignore[assignment]
    enter_game_pending: bool = False
    # START_MAP 进赛丽亚 delayed 链：进行中 / 已发完。
    # 用于回页时复位、避免半城镇态 + 角色页 chrome 叠层导致鼠标点不了 UI。
    enter_seria_in_progress: bool = False
    enter_seria_settled: bool = False
    # settled 后是否已做完整装备栏补刷（008C U1+000E 终态；防同一次进房重复）
    enter_seria_equip_refreshed: bool = False
    # post-settle 仅 000E 是否已发（不阻塞 008C 终态）
    enter_seria_post_settle_000e_sent: bool = False
    # 进房 UI 稳定边界后的祈愿快照补发；每次角色绑定只允许一次。
    enter_seria_wish_snapshot_post_settle_sent: bool = False
    last_select_character_at: float | None = None
    # Live town placement after SET_USER_AREA / SET_USER_POSITION.
    cur_town_id: int | None = None
    cur_area_id: int | None = None
    cur_pos_x: int | None = None
    cur_pos_y: int | None = None
    cur_direction: int | None = None
    cur_area_state: int | None = None
    # Quest session (task doc 5.1): DB is authority; session is hot cache.
    active_quest_ids: list | None = None
    active_quest_pairs: list | None = None
    finished_quest_ids: list | None = None
    last_quest_id: int | None = None
    quest_connected: bool = False
    quest_hydrated: bool = False
    # One server-confirmed quest completion may produce one client-local
    # clear-quest-by-grade report for each selected Daily Challenge entry.
    daily_challenge_clear_grade_event: dict | None = None
    # A claimed Daily entry can emit one stale C1/0021 after its reward ACK.
    # The correction is keyed by the current ledger identity and is sent once
    # per Game session; subsequent stale reports are discarded.
    daily_challenge_claimed_snapshot_ids: set = field(default_factory=set)
    daily_challenge_claimed_snapshot_identity: tuple[str, str] | None = None
    # Consecutive C1/001A requests from one-click disassembly arrive as one
    # TCP burst.  Hold them briefly so the server can settle one atomic batch.
    pending_disjoint_item_bodies: list[bytes] = field(default_factory=list)
    pending_disjoint_item_deadline: float = 0.0
    # Dungeon runtime scratch used by quest-drop / maze select / SELECT grant.
    cur_dungeon_id: int | None = None
    cur_dungeon_difficulty: int | None = None
    last_quest_drop: dict | None = None
    in_dungeon_select: bool = False
    dungeon_select_active: bool = False
    in_dungeon_run: bool = False
    # A death report arms exactly one revive-coin request for the current
    # dungeon actor.  It is cleared on revive and whenever a run is rebound.
    dungeon_death_pending: bool = False
    dungeon_death_actor_id: int = 0
    selected_dungeon_id: int | None = None
    selected_difficulty: int | None = None
    # C1/0356/0357 carry the player object's u16 UID. Per-run endless progress
    # is keyed independently by the selected dungeon's canonical account key.
    special_dungeon_selector_player_uid: int = 0
    dungeon_special_selector_dungeon_id: int = 0
    dungeon_special_selector_progress_key: int = 0
    dungeon_special_selector_entry_option: int = 0
    dungeon_special_selector_highest_unlocked: int = 0
    dungeon_start_x: int | None = None
    dungeon_start_y: int | None = None
    dungeon_cell_x: int | None = None
    dungeon_cell_y: int | None = None
    dungeon_boss_x: int | None = None
    dungeon_boss_y: int | None = None
    dungeon_map_index: int | None = None
    dungeon_normal_map_index: int | None = None
    dungeon_maze_index: int | None = None
    # Per-run selected MAP for weighted PVF BOSS candidate pools.
    dungeon_boss_map_variants: dict | None = None
    # Worldmap selected for this run; used by PVF region-clear quests.
    dungeon_worldmap_id: int | None = None
    dungeon_seed: int | None = None
    dungeon_clear_phase: int = 0
    dungeon_run_kill_exp: int = 0
    dungeon_run_premium_exp: int = 0
    dungeon_run_growth_exp: int = 0
    dungeon_run_neo_exp: int = 0
    dungeon_run_fatigue_burn_exp: int = 0
    dungeon_run_started_monotonic: float | None = None
    adventure_season1_run: tuple[str, int, int, bool, bool] | None = None
    weekly_attendance_run: tuple[str, int, int] | None = None
    dungeon_run_play_time_ms: int | None = None
    dungeon_run_best_time_ms: int | None = None
    # Monotonic within this Game TCP.  A linked-dungeon retry is valid only for
    # the run generation that armed it and is consumed by the next 0010.
    dungeon_run_generation: int = 0
    # First run-bound S0/0156 snapshot for the weapon-avatar reinstall. The
    # helper attaches the restore snapshot and its PIV state to this session.
    dungeon_weapon_avatar_render_pending: bytes | None = None
    dungeon_weapon_avatar_render_restore_pending: bytes | None = None
    dungeon_weapon_avatar_render_character_id: int | None = None
    dungeon_weapon_avatar_render_dungeon_id: int | None = None
    dungeon_weapon_avatar_render_map_index: int | None = None
    dungeon_weapon_avatar_render_run_generation: int = 0
    dungeon_weapon_avatar_render_remove_sent: bool = False
    dungeon_weapon_avatar_render_sent: bool = False
    linked_dungeon_retry_pending: Any = None
    # Ordinary clear-card settlement. Stage3 C1/0045 is the client-owned
    # timer signal that reuses the confirmed C1/0047 free-card path.
    dungeon_card_reward_plan: Any = None
    dungeon_card_layout_sent: bool = False
    dungeon_card_flipped: bool = False
    dungeon_card_free_player_id: int = 0
    dungeon_card_paid_player_id: int = 0
    dungeon_card_auto_flip_armed: bool = False
    dungeon_card_auto_flip_deadline_monotonic: float = 0.0
    dungeon_card_auto_flip_fired: bool = False
    dungeon_card_auto_flip_run_generation: int = 0
    dungeon_room_mons: list | None = None
    # Client-owned passive geometry is not resent in 001D.  Keep its PVF
    # identity server-side so DIE can resolve ordinary destructible objects.
    dungeon_room_passive_objects: list | None = None
    dungeon_boss_condition_config_key: tuple | None = None
    dungeon_boss_condition_assignments: list | None = None
    dungeon_boss_condition_complete: bool = False
    dungeon_boss_condition_gate_pending: bool = False
    dungeon_room_first_seq: int = 0
    dungeon_room_states: dict | None = None
    dungeon_room_sequence_cursor: int = 0
    dungeon_cleared_cells: set | None = None
    dungeon_required_cells: set | None = None
    # Primary character Actor key for player-originated ground drops.
    dungeon_player_actor_key: int = 0
    # 地下城地面掉落（DIE 0026 行；拾取后续）
    dungeon_drop_lcg_seed: int = 0
    dungeon_drop_scene_slot: int = 0
    dungeon_drops: dict | None = None
    dungeon_drop_meta: dict | None = None
    dungeon_basis_level: int = 0
    # Character-scoped Temporal Rift wish equipment, hydrated at bind time.
    dungeon_wish_item_ids: tuple = ()
    # Timed Blood/Ultimate altar schedule, active actors, selection, and result.
    altar_active: bool = False
    altar_dungeon_id: int = 0
    altar_mode: int = 0
    altar_max_rounds: int = 0
    altar_round: int = 0
    altar_local_phase: int = 0
    altar_physical_stage: int = 0
    altar_map_plan: tuple = ()
    altar_waiting_ready: bool = False
    altar_waiting_move_map: bool = False
    altar_pending_physical_stage: int = -1
    altar_waiting_selection: bool = False
    altar_selection_deadline: float = 0.0
    altar_schedule: tuple = ()
    altar_schedule_index: int = 0
    altar_schedule_variant: str = ""
    altar_schedule_candidate_id: int = -1
    altar_schedule_candidate_count: int = 0
    altar_round_selection: int = 0
    altar_active_monster_seqs: tuple = ()
    altar_wave_monster_seqs: tuple = ()
    altar_wave_variant: str = ""
    altar_next_monster_seq: int = 0
    altar_allocated_monster_seqs: tuple = ()
    altar_final_clear_pending: bool = False
    altar_last_selection: int = 0
    altar_completed_difficulties: tuple = ()
    altar_started_monotonic: float = 0.0
    altar_settlement_deadline: float = 0.0
    altar_settlement_rewards: tuple = ()
    altar_rewards_committed: bool = False
    altar_result_groups_sent: bool = False
    altar_reward_seed: int = 0
    # Tournament maps keep a fixed 16-team bracket and publish all four
    # player-path actor groups in the opening START_MAP.
    dungeon_tournament_active: bool = False
    dungeon_tournament_completed: bool = False
    dungeon_tournament_dungeon_id: int = 0
    dungeon_tournament_map_index: int = 0
    dungeon_tournament_seed: int = 0
    dungeon_tournament_round: int = 0
    dungeon_tournament_current_seq: int = 0
    dungeon_tournament_current_seqs: tuple = ()
    dungeon_tournament_first_actor_seq: int = 0
    dungeon_tournament_party_limit: int = 0
    dungeon_tournament_rounds: tuple = ()
    dungeon_tournament_path_actors: tuple = ()
    dungeon_tournament_opponents: tuple = ()
    dungeon_tournament_completed_rounds: int = 0
    dungeon_tournament_reward_exp: int = 0
    dungeon_tournament_rewards: tuple = ()
    dungeon_tournament_reward_selection: Any = None
    dungeon_tournament_round_fatigue: int = 0
    dungeon_tournament_enter_without_fatigue: bool = False
    # Another Arad uses dungeon 8000 as the UI wrapper, then runs the
    # client-selected historical dungeon with hostile actors at char level + 3.
    another_arad_active: bool = False
    another_arad_wrapper_dungeon_id: int = 0
    another_arad_historical_dungeon_id: int = 0
    another_arad_crack_quest_id: int = 0
    another_arad_monster_level: int = 0
    # One selected Crack-of-Dimension mission.  Progress/timer/reward claim
    # are intentionally session-local and never enter the persistent quest DB.
    another_arad_quest_state: Any = None
    # Death Tower uses an ordered floor list and a dedicated 009F/002D state
    # machine instead of ordinary maze cells and 001F settlement.
    dungeon_tower_kind: str | None = None
    dungeon_tower_floor: int = 0
    dungeon_tower_final_floor: int = 0
    dungeon_tower_maps: tuple = ()
    dungeon_tower_stage_state: int = 0
    dungeon_tower_stage_metrics: tuple = ()
    dungeon_tower_floor_cleared: bool = False
    dungeon_tower_awaiting_move: bool = False
    dungeon_tower_settled: bool = False
    dungeon_tower_max_clear_items: int = 0
    # Tower-only list 0x1C. Counts live only for the current Game run.
    dungeon_tower_inventory: dict | None = None
    dungeon_tower_persistent_main_slots: set | None = None
    # Cera package deltas wait for the first S1/00A0 PIV on this connection.
    cera_package_refresh_pending: list | None = None
    cera_package_refresh_character_id: int | None = None
    cera_package_reward_notice_piv_confirmed: bool = False

    def clear_anton_waiting_position(self) -> None:
        """Discard only old marked raid placements before rebind/charselect."""
        owner = self.anton_waiting_position_character_id
        luke_owner = self.luke_waiting_position_character_id
        self.anton_waiting_position_character_id = None
        self.luke_waiting_position_character_id = None
        if (type(owner) is int and owner > 0) or (type(luke_owner) is int and luke_owner > 0):
            for name in (
                "cur_town_id", "cur_area_id", "cur_pos_x", "cur_pos_y", "cur_direction", "cur_area_state",
                "pre_select_town_id", "pre_select_area_id", "pre_select_pos_x", "pre_select_pos_y",
                "pre_select_direction", "pre_select_area_state",
            ):
                setattr(self, name, None)

    def bind_selected_character(self, *, character_id: int, slot: int, name: str) -> None:
        self.dungeon_party_preloaded = False
        self.dungeon_solo_party_owner = None
        self.chat_selection_generation += 1
        self.clear_anton_waiting_position()
        self.selected_character_id = int(character_id)
        self.selected_slot = int(slot)
        self.selected_character_name = str(name)
        self.character_job = None
        self.character_grow_type = None
        self.character_visibility_bits = None
        self.character_level = None
        self.character_exp = None
        self.character_expertise_exp = None
        self.skill_remain_sp = None
        self.skill_remain_tp = None
        self.client_exp_primed = False
        self.fatigue_burning_enabled = False
        self.fatigue_burning_hydrated = False
        self.fatigue_burning_character_id = int(character_id)
        self.enter_game_pending = True
        self.enter_seria_wish_snapshot_post_settle_sent = False
        self.last_select_character_at = time.time()
        # Rebind clears quest scratch so leftover state cannot freeze Seria loading.
        # 与 quest_session.clear_session_quest_scratch 同语义（session 自有字段直接清）
        self.active_quest_ids = []
        self.active_quest_pairs = []
        self.finished_quest_ids = []
        self.last_quest_id = None
        self.quest_connected = False
        self.quest_hydrated = False
        self.daily_challenge_clear_grade_event = None
        self.daily_challenge_claimed_snapshot_ids = set()
        self.daily_challenge_claimed_snapshot_identity = None
        self.cur_dungeon_id = None
        self.cur_dungeon_difficulty = None
        self.last_quest_drop = None
        self.in_dungeon_select = False
        self.dungeon_select_active = False
        self.in_dungeon_run = False
        self.dungeon_death_pending = False
        self.dungeon_death_actor_id = 0
        self.selected_dungeon_id = None
        self.selected_difficulty = None
        self.special_dungeon_selector_player_uid = 0
        self.dungeon_special_selector_dungeon_id = 0
        self.dungeon_special_selector_progress_key = 0
        self.dungeon_special_selector_entry_option = 0
        self.dungeon_special_selector_highest_unlocked = 0
        self.dungeon_start_x = None
        self.dungeon_start_y = None
        self.dungeon_cell_x = None
        self.dungeon_cell_y = None
        self.dungeon_boss_x = None
        self.dungeon_boss_y = None
        self.dungeon_map_index = None
        self.dungeon_normal_map_index = None
        self.dungeon_maze_index = None
        self.dungeon_boss_map_variants = {}
        self.dungeon_worldmap_id = None
        self.dungeon_seed = None
        self.dungeon_clear_phase = 0
        self.dungeon_run_kill_exp = 0
        self.dungeon_run_premium_exp = 0
        self.dungeon_run_growth_exp = 0
        self.dungeon_run_neo_exp = 0
        self.dungeon_run_fatigue_burn_exp = 0
        self.dungeon_run_started_monotonic = None
        self.adventure_season1_run = None
        self.weekly_attendance_run = None
        self.dungeon_run_play_time_ms = None
        self.dungeon_run_best_time_ms = None
        self.dungeon_run_generation = 0
        self.dungeon_weapon_avatar_render_pending = None
        self.dungeon_weapon_avatar_render_restore_pending = None
        self.dungeon_weapon_avatar_render_character_id = None
        self.dungeon_weapon_avatar_render_dungeon_id = None
        self.dungeon_weapon_avatar_render_map_index = None
        self.dungeon_weapon_avatar_render_run_generation = 0
        self.dungeon_weapon_avatar_render_remove_sent = False
        self.dungeon_weapon_avatar_render_sent = False
        self.linked_dungeon_retry_pending = None
        self.dungeon_card_reward_plan = None
        self.dungeon_card_layout_sent = False
        self.dungeon_card_flipped = False
        self.dungeon_card_free_player_id = 0
        self.dungeon_card_paid_player_id = 0
        self.dungeon_card_auto_flip_armed = False
        self.dungeon_card_auto_flip_deadline_monotonic = 0.0
        self.dungeon_card_auto_flip_fired = False
        self.dungeon_card_auto_flip_run_generation = 0
        self.dungeon_room_mons = []
        self.dungeon_room_passive_objects = []
        self.dungeon_boss_condition_config_key = None
        self.dungeon_boss_condition_assignments = []
        self.dungeon_boss_condition_complete = False
        self.dungeon_boss_condition_gate_pending = False
        self.dungeon_room_first_seq = 0
        self.dungeon_room_states = {}
        self.dungeon_room_sequence_cursor = 0
        self.dungeon_cleared_cells = set()
        self.dungeon_required_cells = set()
        self.dungeon_player_actor_key = 0
        self.dungeon_drop_lcg_seed = 0
        self.dungeon_drop_scene_slot = 0
        self.dungeon_drops = {}
        self.dungeon_drop_meta = {}
        self.dungeon_basis_level = 0
        self.dungeon_wish_item_ids = ()
        self.altar_active = False
        self.altar_dungeon_id = 0
        self.altar_mode = 0
        self.altar_max_rounds = 0
        self.altar_round = 0
        self.altar_local_phase = 0
        self.altar_physical_stage = 0
        self.altar_map_plan = ()
        self.altar_waiting_ready = False
        self.altar_waiting_move_map = False
        self.altar_pending_physical_stage = -1
        self.altar_waiting_selection = False
        self.altar_selection_deadline = 0.0
        self.altar_schedule = ()
        self.altar_schedule_index = 0
        self.altar_schedule_variant = ""
        self.altar_schedule_candidate_id = -1
        self.altar_schedule_candidate_count = 0
        self.altar_round_selection = 0
        self.altar_active_monster_seqs = ()
        self.altar_wave_monster_seqs = ()
        self.altar_wave_variant = ""
        self.altar_next_monster_seq = 0
        self.altar_allocated_monster_seqs = ()
        self.altar_final_clear_pending = False
        self.altar_last_selection = 0
        self.altar_completed_difficulties = ()
        self.altar_started_monotonic = 0.0
        self.altar_settlement_deadline = 0.0
        self.altar_settlement_rewards = ()
        self.altar_rewards_committed = False
        self.altar_result_groups_sent = False
        self.altar_reward_seed = 0
        self.dungeon_tournament_active = False
        self.dungeon_tournament_completed = False
        self.dungeon_tournament_dungeon_id = 0
        self.dungeon_tournament_map_index = 0
        self.dungeon_tournament_seed = 0
        self.dungeon_tournament_round = 0
        self.dungeon_tournament_current_seq = 0
        self.dungeon_tournament_current_seqs = ()
        self.dungeon_tournament_first_actor_seq = 0
        self.dungeon_tournament_party_limit = 0
        self.dungeon_tournament_rounds = ()
        self.dungeon_tournament_path_actors = ()
        self.dungeon_tournament_opponents = ()
        self.dungeon_tournament_completed_rounds = 0
        self.dungeon_tournament_reward_exp = 0
        self.dungeon_tournament_rewards = ()
        self.dungeon_tournament_reward_selection = None
        self.dungeon_tournament_round_fatigue = 0
        self.dungeon_tournament_enter_without_fatigue = False
        self.another_arad_active = False
        self.another_arad_wrapper_dungeon_id = 0
        self.another_arad_historical_dungeon_id = 0
        self.another_arad_crack_quest_id = 0
        self.another_arad_monster_level = 0
        self.another_arad_quest_state = None
        self.dungeon_tower_kind = None
        self.dungeon_tower_floor = 0
        self.dungeon_tower_final_floor = 0
        self.dungeon_tower_maps = ()
        self.dungeon_tower_stage_state = 0
        self.dungeon_tower_stage_metrics = ()
        self.dungeon_tower_floor_cleared = False
        self.dungeon_tower_awaiting_move = False
        self.dungeon_tower_settled = False
        self.dungeon_tower_max_clear_items = 0
        self.dungeon_tower_inventory = {}
        self.dungeon_tower_persistent_main_slots = set()
        self.cera_package_refresh_pending = []
        self.cera_package_refresh_character_id = None

    def note_c2s_opcode(self, opcode: int) -> None:
        """Record a C->S opcode for Exit/Select Channel discrimination."""
        if self.recent_c2s_opcodes is None:
            self.recent_c2s_opcodes = []
        self.recent_c2s_opcodes.append(int(opcode) & 0xFFFF)
        # Keep a short window; Exit Game prep is immediately before 0x057F.
        if len(self.recent_c2s_opcodes) > 24:
            self.recent_c2s_opcodes = self.recent_c2s_opcodes[-24:]

    def recent_has_exit_prep(self) -> bool:
        """True if client recently sent Exit-Game prep opcodes before 0x057F.

        Live Stage3 (2026-07-14 170221):
        - Select Channel: ... 0x01BA/0x008C then 0x057F; 0x0003 reserved=6f403e
        - Exit Game on reentry charselect: ... 0x00C2, 0x0505, 0x0506 then 0x057F;
          0x0003 reserved=fddcbf
        Without this, reentry_forced_charselect + list_count=2 misclassifies Exit
        as channel leave (channel is full).
        """
        ops = self.recent_c2s_opcodes or []
        # Ignore the trailing 0x057F itself if already recorded.
        window = [op for op in ops[-12:] if op != 0x057F]
        exit_prep = {0x00C2, 0x0505, 0x0506}
        return any(op in exit_prep for op in window)


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


def _env_hex_bytes(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    normalized = "".join(ch for ch in normalized if ch not in " \t\r\n:_-")
    if len(normalized) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits: {raw!r}")
    return bytes.fromhex(normalized)


class EnterGameChainService:
    """进入游戏 pending 阶段的兼容聚合服务。

    中文维护规则：
    - 这里允许保存跨包会话和做兼容转发。
    - 新增“进入赛利亚房间”包时，不要继续把实现堆在这里；按
      ``enter_seria_room_02_*``、``enter_seria_room_03_*`` 递增文件拆分。
    """

    def __init__(self, server: Any) -> None:
        self.server = server
        self.select_character_ack_chain = EnterSeriaRoomSelectCharacterAckService(server)

    @staticmethod
    def select_character_by_slot(characters: list[Any], requested_slot: int) -> Any | None:
        """兼容包装；真实逻辑在 enter_seria_room_01_select_character_ack.py。"""
        return EnterSeriaRoomSelectCharacterAckService.select_character_by_slot(characters, requested_slot)

    def handle_select_character(
        self,
        body: bytes,
        *,
        session: GameSessionState,
    ) -> tuple[list[bytes], str]:
        """兼容包装；真实逻辑在 enter_seria_room_01_select_character_ack.py。"""
        return self.select_character_ack_chain.handle_select_character(body, session=session)

    def resolve_session_channel_id(self, session: GameSessionState) -> tuple[int, str]:
        """Resolve the channel_id that S->C 0x020C must echo.

        Prefer the Game listen port binding (unique multi-channel ports) so
        CHANNEL_MOVE_SUCCESS matches the channel the client actually entered.
        """

        selected = getattr(session, "selected_channel_id", None)
        if selected is not None:
            return int(selected) & 0xFFFF, "session.selected_channel_id"

        bound = getattr(session, "channel_id", None)
        if bound is not None:
            return int(bound) & 0xFFFF, "session.channel_id"

        # Optional: derive from local listen port when multi-port is enabled.
        local_port = getattr(session, "local_port", None)
        if local_port is not None:
            try:
                from channel.channel_info import channel_id_from_game_port

                cid = channel_id_from_game_port(
                    int(local_port),
                    base_port=int(os.environ.get("USDOF_CHANNELINFO_PORT_BASE", "10052") or "10052"),
                )
                if cid is not None:
                    return int(cid) & 0xFFFF, f"local_port={local_port}"
            except Exception:
                pass

        if os.environ.get("USDOF_GAME_CHANNEL_INDEX"):
            return int(os.environ["USDOF_GAME_CHANNEL_INDEX"], 0) & 0xFFFF, "USDOF_GAME_CHANNEL_INDEX"
        if os.environ.get("USDOF_CHANNELINFO_FIRST_CHANNEL_ID"):
            return int(os.environ["USDOF_CHANNELINFO_FIRST_CHANNEL_ID"], 0) & 0xFFFF, "USDOF_CHANNELINFO_FIRST_CHANNEL_ID"
        return 10, "default"

    def build_channel_move_success_packets(
        self,
        *,
        session: GameSessionState,
        include_0323: bool = True,
        include_020c: bool = True,
        include_0244: bool | None = None,
    ) -> tuple[list[bytes], str, int]:
        """Build S->C channel-move settle packets for charselect Channel/Exit.

        IDA (Stage3 sentinelfix):
        - S->C 0x0323 / ``0x016633E0``: selecting-scene list (u8 count; ``00`` = empty). Must be codec5-encoded on Stage3 (raw body is not accepted; no PIV).
        - S->C 0x020C / ``0x0171F970``: CHANNEL_MOVE_SUCCESS (codec6 on Stage3); clears pending at
          ``sub_1720760()+0x28/+0x2C`` **only when** ``n2500`` is non-null and
          ``n2500->vfunc76() == body u16 channel_id``.
        - S->C flag1/0x0244 / ``0x0171E570``: success flag force-clears pending
          at +0x28/+0x2C and closes UI object 42. Needed on charselect because
          0x020C only clears pending when n2500 is non-null and channel matches.
        - S->C 0x0208 / ``0x0171E5B0``: do not send on charselect Channel. With
          pending set and n2500 null it calls enter_game_state(..., 20) (region)
          and leaves Changing channels / channel full dialogs. Keep off unless
          an in-town n2500 path is being A/B tested.
        """

        channel_id, source = self.resolve_session_channel_id(session)
        packets: list[bytes] = []
        parts: list[str] = []

        if include_0323 and not _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_0323", True):
            include_0323 = False
        if include_0323:
            scene_body = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_0323_BODY")
            if scene_body is None:
                scene_body = b"\x00"
            scene_mode = os.environ.get("USDOF_GAME_CHANNEL_MOVE_0323_MODE", "codec").strip().lower()
            if scene_mode in {"codec", "encoded"}:
                scene_packet = self.server._codec_response(0, 0x0323, scene_body)
                scene_mode_note = "codec"
            else:
                scene_packet = self.server._raw_response(0, 0x0323, scene_body)
                scene_mode_note = "raw"
            packets.append(scene_packet)
            parts.append(f"S->C 0x0323 mode={scene_mode_note} body={scene_body.hex(' ')}")

        if include_020c and not _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_020C", True):
            include_020c = False
        if include_020c:
            success_body = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_020C_BODY")
            if success_body is None:
                success_body = (channel_id & 0xFFFF).to_bytes(2, "little")
                source_note = source
            else:
                channel_id = int.from_bytes(success_body[:2], "little") if len(success_body) >= 2 else channel_id
                source_note = "override-body"
            success_mode = os.environ.get("USDOF_GAME_CHANNEL_MOVE_020C_MODE", "codec").strip().lower()
            if success_mode in {"codec", "encoded"}:
                success_packet = self.server._codec_response(0, 0x020C, success_body)
                success_mode_note = "codec"
            else:
                success_packet = self.server._raw_response(0, 0x020C, success_body)
                success_mode_note = "raw"
            packets.append(success_packet)
            parts.append(
                f"S->C 0x020C mode={success_mode_note} body={success_body.hex(' ')} "
                f"channel_id={channel_id} source={source_note}"
            )

        # 0x0244 is CMD-side success (IDA sub_171E570 / sub_1AAEA80 enum 580).
        # Exit profile forces include_0244=True. Channel path defaults from USDOF_GAME_CHANNEL_MOVE_SEND_0244 (resident=1).
        extra_note = ""
        send_0208 = _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_0208", False)
        if send_0208:
            body_0208 = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_0208_BODY")
            if body_0208 is None:
                body_0208 = b""
            mode_0208 = os.environ.get("USDOF_GAME_CHANNEL_MOVE_0208_MODE", "raw").strip().lower()
            if mode_0208 in {"codec", "encoded"}:
                packet_0208 = self.server._codec_response(0, 0x0208, body_0208)
                mode_0208_note = "codec"
            else:
                packet_0208 = self.server._raw_response(0, 0x0208, body_0208)
                mode_0208_note = "raw"
            packets.append(packet_0208)
            extra_note += (
                f" + S->C 0x0208 mode={mode_0208_note} body={body_0208.hex(' ') or '<empty>'}"
            )


        # CMD 0x0242 / sub_171DF50: success (a2!=0) calls sub_1720910() THEN
        # still shows msg id 22362 via sub_278B220 unless sub_1907C80(4032).
        # That residual popup is the "channel is full" dialog after Changing
        # channels is cleared by 0x0244. Keep OFF for Channel settle.
        send_0242 = _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_0242", False)
        if send_0242:
            body_0242 = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_0242_BODY")
            if body_0242 is None:
                body_0242 = bytes([1])  # success flag
            mode_0242 = os.environ.get("USDOF_GAME_CHANNEL_MOVE_0242_MODE", "codec").strip().lower()
            if mode_0242 in {"codec", "encoded"}:
                packet_0242 = self.server._codec_response(1, 0x0242, body_0242)
                mode_0242_note = "codec"
            else:
                packet_0242 = self.server._raw_response(1, 0x0242, body_0242)
                mode_0242_note = "raw"
            packets.append(packet_0242)
            extra_note += (
                f" + S->C flag1 0x0242 mode={mode_0242_note} body={body_0242.hex(' ')}"
            )

        if include_0244 is None:
            send_0244 = _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_0244", False)
        else:
            send_0244 = bool(include_0244)
        if send_0244:
            body_0244 = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_0244_BODY")
            if body_0244 is None:
                body_0244 = bytes([1])
            mode_0244 = os.environ.get("USDOF_GAME_CHANNEL_MOVE_0244_MODE", "codec").strip().lower()
            if mode_0244 in {"codec", "encoded"}:
                packet_0244 = self.server._codec_response(1, 0x0244, body_0244)
                mode_0244_note = "codec"
            else:
                packet_0244 = self.server._raw_response(1, 0x0244, body_0244)
                mode_0244_note = "raw"
            packets.append(packet_0244)
            extra_note += (
                f" + S->C flag1 0x0244 mode={mode_0244_note} body={body_0244.hex(' ')}"
            )

        # Early CMD SuccessAck flag1/0x0003 on 0x057F Channel settle (default OFF here;
        # standard_local_flow enables it). IDA 2026-07-14:
        # leave shows msg44444/UI828 when netflag +1093 set; CMD op3 (0x15FE1B0)
        # -> 1ABA3A0 clears +1093 (n5==5) and UI828 vfunc closes residual dialog.
        # Must pair BEFORE/WITH 0x0244. Exit Game does not use this early path.
        send_early_0003 = _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_EARLY_0003", False)
        if send_early_0003:
            body_0003 = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_EARLY_0003_BODY")
            if body_0003 is None:
                body_0003 = bytes([1])
            mode_0003 = os.environ.get(
                "USDOF_GAME_CHANNEL_MOVE_EARLY_0003_MODE", "codec"
            ).strip().lower()
            flag_0003 = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_EARLY_0003_FLAG", 1))
            if mode_0003 in {"codec", "encoded"}:
                packet_0003 = self.server._codec_response(flag_0003, 0x0003, body_0003)
                mode_0003_note = "codec"
            else:
                packet_0003 = self.server._raw_response(flag_0003, 0x0003, body_0003)
                mode_0003_note = "raw"
            packets.append(packet_0003)
            session.channel_leave_early_ack_sent = True
            extra_note += (
                f" + S->C flag{flag_0003} 0x0003 early-SuccessAck "
                f"mode={mode_0003_note} body={body_0003.hex(' ')}"
            )

        # Optional CMD flag1/0x0007 on 0x057F settle (default OFF).
        # x32dbg 20260714_090203: early 0007 immediately hits teardown_1ABE3E0 +
        # full_clear_ui42_ui175, then ChannelInfo parse-fail msg44460 (0x15AF169).
        # Opcode 7 = RETURN_SELECT_CHARACTER (0x16080B0), NOT channel-list return.
        # Standard flow keeps USDOF_GAME_CHANNEL_MOVE_SEND_0007=0; only 0x0244 settle.
        send_0007 = _env_bool("USDOF_GAME_CHANNEL_MOVE_SEND_0007", False)
        if send_0007:
            body_0007 = _env_hex_bytes("USDOF_GAME_CHANNEL_MOVE_0007_BODY")
            if body_0007 is None:
                body_0007 = bytes([1])
            mode_0007 = os.environ.get("USDOF_GAME_CHANNEL_MOVE_0007_MODE", "codec").strip().lower()
            flag_0007 = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_0007_FLAG", 1))
            if mode_0007 in {"codec", "encoded"}:
                packet_0007 = self.server._codec_response(flag_0007, 0x0007, body_0007)
                mode_0007_note = "codec"
            else:
                packet_0007 = self.server._raw_response(flag_0007, 0x0007, body_0007)
                mode_0007_note = "raw"
            packets.append(packet_0007)
            extra_note += (
                f" + S->C flag{flag_0007} 0x0007 mode={mode_0007_note} body={body_0007.hex()} "
            )

        # Reorder settle packets.
        # Evidence: 0x0244 (sub_171E570) force-clears pending and closes UI object 42
        # ("Changing channels"). Cold first Select Channel left UI42 open when 0244 was only last;
        # later reentries auto-closed. Allow the same opcode token more than once so order can
        # bookend: early 0244 (dismiss UI42) + 0323/020C + trailing 0244 (re-dismiss after list mutate).
        order_raw = os.environ.get("USDOF_GAME_CHANNEL_MOVE_ORDER", "0244,0323,020C,0244").strip().lower()
        if packets and order_raw:
            def _pkt_op(pkt: bytes) -> int:
                return int.from_bytes(pkt[1:3], "little") if len(pkt) >= 3 else -1

            token_to_op = {
                "0323": 0x0323,
                "323": 0x0323,
                "020c": 0x020C,
                "20c": 0x020C,
                "0208": 0x0208,
                "208": 0x0208,
                "0242": 0x0242,
                "242": 0x0242,
                "0244": 0x0244,
                "244": 0x0244,
                "0007": 0x0007,
                "7": 0x0007,
                "0003": 0x0003,
                "3": 0x0003,
            }
            by_op: dict[int, bytes] = {}
            for pkt in packets:
                by_op[_pkt_op(pkt)] = pkt
            new_packets: list[bytes] = []
            used_count: dict[int, int] = {}
            # Bookend 0244 only when settle has other ops (0323/020C). Exit-only 0244 must stay single.
            allow_0244_bookend = len(by_op) >= 2 and 0x0244 in by_op
            for tok in order_raw.replace(";", ",").split(","):
                tok = tok.strip()
                op = token_to_op.get(tok)
                if op is None or op not in by_op:
                    continue
                count = used_count.get(op, 0)
                if count >= 1:
                    if not (op == 0x0244 and allow_0244_bookend and count < 2):
                        continue
                # Clone so bookend 0244 can appear twice without dropping other ops.
                new_packets.append(bytes(by_op[op]))
                used_count[op] = count + 1
            for op in list(used_count.keys()):
                by_op.pop(op, None)
            # append any leftovers in original relative order
            for pkt in packets:
                op = _pkt_op(pkt)
                if op in by_op:
                    new_packets.append(by_op.pop(op))
            packets = new_packets

            # Rebuild note in final send order (parts was build order, not settle order).
            def _note_for_op(op: int) -> str | None:
                if op == 0x0323:
                    for part in parts:
                        if "0x0323" in part:
                            return part
                if op == 0x020C:
                    for part in parts:
                        if "0x020C" in part:
                            return part
                if op == 0x0244:
                    return "S->C flag1 0x0244"
                if op == 0x0242:
                    return "S->C flag1 0x0242"
                if op == 0x0208:
                    for part in parts:
                        if "0x0208" in part:
                            return part
                if op == 0x0007:
                    return "S->C 0x0007"
                if op == 0x0003:
                    return "S->C early-SuccessAck 0x0003"
                return None

            ordered_parts: list[str] = []
            for pkt in packets:
                n = _note_for_op(_pkt_op(pkt))
                if n:
                    ordered_parts.append(n)
            if ordered_parts:
                parts = ordered_parts

        if packets:
            session.channel_move_settled = True
            # Channel leave: optional close after settle so reentry does not wait
            # on a second C->S 0x0003 (Exit Game still uses 0x0003 alone).
            if session.last_057f_profile == "channel" and _env_bool(
                "USDOF_GAME_CHANNEL_MOVE_CLOSE_AFTER_SETTLE", False
            ):
                session.channel_leave_close_after_settle = True
                extra_note += " + close-Game-TCP-after-settle"
        note = " + ".join(parts) + extra_note if parts else ("<no settle packets>" + extra_note)
        return packets, note, channel_id

    def handle_channel_move_selecting_scene_057f(
        self,
        body: bytes,
        *,
        session: GameSessionState,
    ) -> tuple[list[bytes], str]:
        """处理 C->S 0x057F。

        注意：角色页 **Channel** 与 **Exit Game** 是两个按钮。
        Channel：C->S 0x057F settle 由 env 门控；常驻 A/B 为 flag1/0x0244 only。
        Exit Game：关键收尾在 C->S 0x0003 → SuccessAck + 关 TCP（保持不变）。
        若 Exit 也会先发 0x057F，channel settle 里带 0x0244 可同时清 pending，
        不改 0x0003 的 SuccessAck/关连接逻辑。

        settle profile (USDOF_GAME_057F_SETTLE_PROFILE):
        - `channel` (default/resident for Channel button): S->C 0x0323 + 0x020C
          Never enable 0x0208 (state 20). SEND_0323/020C default off for residual-full A/B.
          Client typically then sends 0x0003; EXIT SuccessAck + close unchanged.
        - `exit`: flag1/0x0244 only (pure Exit path; Channel button will show
          "channel is full" if left on this profile).
        - `none`: no S->C on 0x057F (0x0003 still does EXIT SuccessAck + close)
        """
        mode = int.from_bytes(body[:4], "little") if len(body) >= 4 else None
        if session.enter_game_pending and not _env_bool("USDOF_GAME_057F_DURING_ENTER_SEND_CHANNEL_MOVE", False):
            return [], (
                "CHANNEL_MOVE_SELECTING_SCENE/0x057f observed during enter-game "
                f"plain_len={len(body)} mode={mode if mode is not None else '<none>'} "
                f"selected_character_id={session.selected_character_id} "
                f"selected_slot={session.selected_slot} "
                f"name={session.selected_character_name!r}; "
                "suppressed channel-move response during enter-game; "
                "set USDOF_GAME_057F_DURING_ENTER_SEND_CHANNEL_MOVE=1 to force legacy S->C 0x0323/0x020C"
            )

        # Live USDOF Stage3 plain body (IDA 0x2C3FD20 / x32dbg 0x02C40466):
        #   first byte is list count v77, NOT Exit-vs-Channel enum.
        #   cold Select/Exit: count=3 plain~136; reentry Select/Exit: count=2 plain~96
        #   Exit vs Select: pre-opcodes 0x00C2/0x0505/0x0506 (Exit prep), not list_count.
        profile = os.environ.get("USDOF_GAME_057F_SETTLE_PROFILE", "channel").strip().lower()
        session.last_057f_at = time.time()
        list_count = body[0] if body else None
        reentry_charselect = bool(getattr(session, "reentry_forced_charselect", False))
        force_mode2_channel = _env_bool("USDOF_GAME_057F_MODE2_AS_CHANNEL", False)
        exit_prep = bool(session.recent_has_exit_prep())
        # Live Stage3 (2026-07-14 170824):
        #   Select Channel: NO 0x00C2/0x0505/0x0506 before 0x057F; 0x0003 reserved=6f403e
        #     cold count=3; reentry count=2
        #   Exit Game: HAS 0x00C2/0x0505/0x0506 before 0x057F; 0x0003 reserved=fddcbf
        #     cold count=3 (same as Select!); reentry count=2
        # list_count alone cannot distinguish Exit vs Select. Exit prep does.
        treat_as_exit = (
            exit_prep
            and not force_mode2_channel
            and _env_bool("USDOF_GAME_057F_EXIT_PREP_AS_EXIT", True)
        )
        if treat_as_exit:
            session.last_057f_profile = "exit"
            session.channel_move_settled = True
            session.channel_leave_early_ack_sent = False
            session.channel_leave_close_after_settle = False
            send_0244 = _env_bool("USDOF_GAME_EXIT_057F_SEND_0244", True)
            if send_0244:
                packets, move_note, _channel_id = self.build_channel_move_success_packets(
                    session=session,
                    include_0323=False,
                    include_020c=False,
                    include_0244=True,
                )
            else:
                packets, move_note = [], "no S->C on exit mode=2 (SuccessAck on 0x0003 only)"
            none = chr(60) + "none" + chr(62)
            return packets, (
                "CHANNEL_MOVE_SELECTING_SCENE/0x057f observed "
                f"plain_len={len(body)} mode={mode if mode is not None else none} "
                f"list_count={list_count if list_count is not None else none} "
                f"reentry_forced_charselect={int(reentry_charselect)} "
                f"exit_prep={int(exit_prep)}; "
                f"EXIT-GAME path (not channel leave); sent {move_note}"
            )

        if profile in {"none", "off", "0", "silent"} and mode != 3 and not reentry_charselect:
            session.channel_move_settled = True  # allow EXIT to close without re-inject
            session.last_057f_profile = "none"
            return [], (
                "CHANNEL_MOVE_SELECTING_SCENE/0x057f observed "
                f"plain_len={len(body)} mode={mode if mode is not None else '<none>'}; "
                "settle profile=none (no S->C; EXIT will only close Game TCP)"
            )

        if (
            profile in {"exit", "exit-only", "leave", "exit-release"}
            and mode != 3
            and not reentry_charselect
        ):
            # Explicit env exit profile (non-mode3 only; never on reentry charselect).
            session.last_057f_profile = "exit"
            packets, move_note, _channel_id = self.build_channel_move_success_packets(
                session=session,
                include_0323=False,
                include_020c=False,
                include_0244=True,
            )
            return packets, (
                "CHANNEL_MOVE_SELECTING_SCENE/0x057f observed "
                f"plain_len={len(body)} mode={mode if mode is not None else '<none>'}; "
                f"settle profile=exit-cmd-clear; sent {move_note}"
            )

        # Channel button resident path (mode=3, or unknown mode with channel profile).
        session.last_057f_profile = "channel"
        session.channel_leave_early_ack_sent = False
        session.channel_leave_close_after_settle = False
        packets, move_note, _channel_id = self.build_channel_move_success_packets(session=session)
        enter_note = ""
        if session.enter_game_pending:
            enter_note = (
                f" during enter-game override selected_character_id={session.selected_character_id} "
                f"selected_slot={session.selected_slot}; "
            )
        reentry_note = ""
        if reentry_charselect and list_count == 2 and not exit_prep:
            reentry_note = (
                "reentry_forced_charselect=1 list_count=2 exit_prep=0 treated as CHANNEL "
                "(not Exit; IDA list count not button enum); "
            )
        none = chr(60) + "none" + chr(62)
        return packets, (
            "CHANNEL_MOVE_SELECTING_SCENE/0x057f observed "
            f"plain_len={len(body)} mode={mode if mode is not None else none} "
            f"list_count={list_count if list_count is not None else none} "
            f"reentry_forced_charselect={int(reentry_charselect)}; "
            f"{reentry_note}"
            f"{enter_note}"
            f"sent {move_note}"
        )


    @staticmethod
    def classify_followup(opcode: int, plain_len: int, session: GameSessionState) -> str:
        """只分类记录，不猜测回包。"""
        return (
            f"enter-game follow-up C->S opcode=0x{opcode:04x} "
            f"plain_len={plain_len} selected_character_id={session.selected_character_id} "
            f"selected_slot={session.selected_slot} "
            f"name={session.selected_character_name!r}; "
            "captured for next IDA/x32dbg pass; no S->C response sent in this first-round state split"
        )
