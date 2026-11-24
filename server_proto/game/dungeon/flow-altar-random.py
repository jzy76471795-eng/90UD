"""PVF-owned timed waves for the Stage3 endless and ultimate altars."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from game.chain_loader import load_chain
from game.dungeon.clear_reward import build_clear_reward_plan
from game.drop.dnf_lcg import DnfLcg


_altar_pvf = load_chain("helper-altar-pvf.py")
_payloads = load_chain("helper-altar-payloads.py")
_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
_actor_keys = load_chain("helper-dungeon_actor_keys.py")

AltarWaveActor = _payloads.AltarWaveActor
build_start_blood_map_00d3 = _payloads.build_start_blood_map_00d3
build_blood_monster_spawn_00d2 = _payloads.build_blood_monster_spawn_00d2
build_blood_round_interval_00d7 = _payloads.build_blood_round_interval_00d7
build_ultimate_difficulty_prompt_0129 = (
    _payloads.build_ultimate_difficulty_prompt_0129
)
build_ultimate_difficulty_resolved_0129 = (
    _payloads.build_ultimate_difficulty_resolved_0129
)
build_blood_result_summary_00d0 = _payloads.build_blood_result_summary_00d0
build_blood_result_groups_00d1 = _payloads.build_blood_result_groups_00d1
build_dungeon_info_001c_us = _dungeon_payloads.build_dungeon_info_001c_us
build_start_map_001d_us_empty = _dungeon_payloads.build_start_map_001d_us_empty
resolve_altar_config = _altar_pvf.resolve_altar_config
resolve_altar_reward_config = _altar_pvf.resolve_altar_reward_config
initial_actor_key_base = _actor_keys.initial_actor_key_base


_ALTAR_EFFECT_SELECTOR = 19
_ULTIMATE_SELECTION_TIMEOUT_SECONDS = 10
_SETTLEMENT_RANKING_SECONDS = 8.0
_MAX_CONCURRENT_PHASES = 10


@dataclass
class AltarScheduledWave:
    deadline_monotonic: float
    phase_index: int
    batch_index: int
    spawn_count: int
    phase: Any
    source: Any
    is_final_map_wave: bool = False


def _nonzero_seed(rng: Any) -> int:
    seed = int(rng.getrandbits(32)) & 0xFFFFFFFF
    return seed or 1


def _clamp_u16(value: float) -> int:
    return max(0, min(0xFFFF, int(round(float(value)))))


def _setattr(session: Any, name: str, value: Any) -> None:
    if session is not None:
        setattr(session, name, value)


def _altar_run_active(session: Any) -> bool:
    return bool(
        session is not None
        and getattr(session, "altar_active", False)
        and getattr(session, "in_dungeon_run", False)
    )


def _character_id(session: Any) -> int:
    if session is None:
        return 0
    for name in ("selected_character_id", "character_id", "cid"):
        try:
            value = int(getattr(session, name, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def clear_altar_session(session: Any) -> None:
    if session is None:
        return
    defaults = {
        "altar_active": False,
        "altar_dungeon_id": 0,
        "altar_mode": 0,
        "altar_max_rounds": 0,
        "altar_round": 0,
        "altar_local_phase": 0,
        "altar_physical_stage": 0,
        "altar_map_plan": (),
        "altar_waiting_ready": False,
        "altar_waiting_move_map": False,
        "altar_pending_physical_stage": -1,
        "altar_waiting_selection": False,
        "altar_selection_deadline": 0.0,
        "altar_schedule": (),
        "altar_schedule_index": 0,
        "altar_schedule_variant": "",
        "altar_schedule_candidate_id": -1,
        "altar_schedule_candidate_count": 0,
        "altar_round_selection": 0,
        "altar_active_monster_seqs": (),
        "altar_wave_monster_seqs": (),
        "altar_wave_variant": "",
        "altar_next_monster_seq": 0,
        "altar_allocated_monster_seqs": (),
        "altar_final_clear_pending": False,
        "altar_last_selection": 0,
        "altar_completed_difficulties": (),
        "altar_started_monotonic": 0.0,
        "altar_settlement_deadline": 0.0,
        "altar_settlement_rewards": (),
        "altar_rewards_committed": False,
        "altar_result_groups_sent": False,
        "altar_reward_seed": 0,
    }
    for name, value in defaults.items():
        setattr(session, name, value)


class AltarRandomFlow:
    def __init__(self, server: Any, rng: Optional[Any] = None) -> None:
        self.server = server
        self.rng = rng if rng is not None else random.SystemRandom()

    def _emit(self, opcode: int, body: bytes, *, flag: int = 0) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _choose_map_plan(self, config: Any) -> tuple[int, ...]:
        if config.mode == 1:
            return config.build_map_plan(
                self.rng.choice(config.middle_maps),
                start_map=self.rng.choice(config.start_maps),
            )
        return config.build_map_plan()

    def _dungeon_context_packet(
        self,
        *,
        config: Any,
        difficulty: int,
    ) -> tuple[bytes, str]:
        body = build_dungeon_info_001c_us(
            config.dungeon_id,
            difficulty,
            boss_x=config.boss_cell[0],
            boss_y=config.boss_cell[1],
            maze_index=0,
            entry_option=0,
        )
        return self._emit(0x001C, body), (
            "altar_dungeon_context packet=001C dungeon=%d difficulty=%d "
            "start=(%d,%d) boss=(%d,%d) maze=0"
            % (
                config.dungeon_id,
                int(difficulty) & 0xFF,
                config.start_cell[0],
                config.start_cell[1],
                config.boss_cell[0],
                config.boss_cell[1],
            )
        )

    @staticmethod
    def _cell_for_stage(mode: int, stage: int) -> tuple[int, int]:
        if int(mode) == 1:
            return int(stage) & 0xFF, 0
        return 0, 0

    def _map_context_packets(
        self,
        *,
        session: Any,
        config: Any,
        stage: int,
    ) -> tuple[list[bytes], str]:
        map_plan = tuple(int(value) for value in getattr(session, "altar_map_plan", ()))
        if not 0 <= int(stage) < len(map_plan):
            raise ValueError("altar physical stage outside map plan")
        map_id = map_plan[int(stage)]
        cell_x, cell_y = self._cell_for_stage(config.mode, stage)
        seed = _nonzero_seed(self.rng)
        start_map_body = build_start_map_001d_us_empty(
            cell_x=cell_x,
            cell_y=cell_y,
            seed=seed,
            map_index=map_id,
            room_flag=1,
            map_index_width="u32",
            party_member_index=0,
        )
        blood_map_body = build_start_blood_map_00d3(
            cell_x=cell_x,
            cell_y=cell_y,
            seed=seed,
            mode=config.mode,
            map_id=map_id,
            global_flag=0,
            primary_rows=(),
            secondary_rows=(),
            tail=0,
        )
        _setattr(session, "altar_physical_stage", int(stage))
        _setattr(session, "altar_waiting_ready", True)
        _setattr(session, "altar_waiting_move_map", False)
        _setattr(session, "altar_pending_physical_stage", -1)
        _setattr(session, "altar_waiting_selection", False)
        _setattr(session, "altar_selection_deadline", 0.0)
        _setattr(session, "altar_schedule", ())
        _setattr(session, "altar_schedule_index", 0)
        _setattr(session, "altar_schedule_candidate_id", -1)
        _setattr(session, "altar_schedule_candidate_count", 0)
        _setattr(session, "altar_active_monster_seqs", ())
        _setattr(session, "altar_wave_monster_seqs", ())
        _setattr(session, "dungeon_map_index", map_id)
        _setattr(session, "dungeon_cell_x", cell_x)
        _setattr(session, "dungeon_cell_y", cell_y)
        _setattr(session, "dungeon_seed", seed)
        _setattr(session, "dungeon_finish_loading", False)
        _setattr(session, "dungeon_room_mons", [])
        return [
            self._emit(0x001D, start_map_body),
            self._emit(0x00D3, blood_map_body),
        ], (
            "altar_map_context round=%d/%d stage=%d map=%d cell=(%d,%d) "
            "seed=0x%08X mode=%d packets=001D->00D3 wait=0025_then_0102"
            % (
                int(getattr(session, "altar_round", 0)) + 1,
                config.max_rounds,
                int(stage),
                map_id,
                cell_x,
                cell_y,
                seed,
                config.mode,
            )
        )

    def handle_move_map(
        self,
        *,
        next_x: int,
        next_y: int,
        session: Any,
    ) -> tuple[bool, list[bytes], str]:
        if not _altar_run_active(session):
            return False, [], "altar_move scope=inactive"

        config = resolve_altar_config(
            int(getattr(session, "altar_dungeon_id", 0) or 0)
        )
        if config is None:
            return True, [], "altar_move reject=missing_config"
        if int(config.mode) != 1:
            return True, [], "altar_move reject=single_map_mode mode=%d" % config.mode
        if not bool(getattr(session, "altar_waiting_move_map", False)):
            return True, [], "altar_move reject=not_waiting_for_physical_map"

        old_stage = int(getattr(session, "altar_physical_stage", 0) or 0)
        pending_stage = int(
            getattr(session, "altar_pending_physical_stage", -1)
        )
        map_plan = tuple(
            int(value) for value in getattr(session, "altar_map_plan", ())
        )
        completed = int(getattr(session, "altar_round", 0) or 0)
        expected_stage = old_stage + 1
        if (
            pending_stage != expected_stage
            or not 0 <= pending_stage < len(map_plan)
            or completed != pending_stage * 5
        ):
            return True, [], (
                "altar_move reject=invalid_transition old_stage=%d pending_stage=%d "
                "completed=%d map_count=%d"
                % (old_stage, pending_stage, completed, len(map_plan))
            )
        if tuple(getattr(session, "altar_schedule", ()) or ()) or tuple(
            getattr(session, "altar_active_monster_seqs", ()) or ()
        ):
            return True, [], "altar_move reject=round_still_active"

        expected_cell = self._cell_for_stage(config.mode, pending_stage)
        received_cell = (int(next_x) & 0xFF, int(next_y) & 0xFF)
        if received_cell != expected_cell:
            return True, [], (
                "altar_move reject=unexpected_cell received=(%d,%d) expected=(%d,%d)"
                % (
                    received_cell[0],
                    received_cell[1],
                    expected_cell[0],
                    expected_cell[1],
                )
            )

        packets, note = self._map_context_packets(
            session=session,
            config=config,
            stage=pending_stage,
        )
        return True, packets, (
            "altar_move accepted stage=%d->%d cell=(%d,%d); %s"
            % (
                old_stage,
                pending_stage,
                received_cell[0],
                received_cell[1],
                note,
            )
        )

    def try_enter_altar(
        self,
        *,
        dungeon_id: int,
        difficulty: int,
        character_id: int,
        session: Any,
    ) -> tuple[bool, list[bytes], str]:
        config = resolve_altar_config(int(dungeon_id))
        if config is None:
            return False, [], "not_altar_dungeon"
        map_plan = self._choose_map_plan(config)
        actor_base = initial_actor_key_base(int(character_id or 0))
        started = time.monotonic()
        clear_altar_session(session)
        _setattr(session, "altar_active", True)
        _setattr(session, "altar_dungeon_id", config.dungeon_id)
        _setattr(session, "altar_mode", config.mode)
        _setattr(session, "altar_max_rounds", config.max_rounds)
        _setattr(session, "altar_round", 0)
        _setattr(session, "altar_local_phase", 0)
        _setattr(session, "altar_map_plan", map_plan)
        _setattr(session, "altar_next_monster_seq", (actor_base + 1) & 0xFFFF)
        _setattr(session, "altar_started_monotonic", started)
        _setattr(session, "altar_reward_seed", _nonzero_seed(self.rng))
        _setattr(session, "in_dungeon_select", False)
        _setattr(session, "dungeon_select_active", False)
        _setattr(session, "in_dungeon_run", True)
        _setattr(session, "selected_dungeon_id", config.dungeon_id)
        _setattr(session, "selected_difficulty", int(difficulty) & 0xFF)
        _setattr(session, "cur_dungeon_id", config.dungeon_id)
        _setattr(session, "cur_dungeon_difficulty", int(difficulty) & 0xFF)
        _setattr(session, "dungeon_basis_level", config.basis_level)
        _setattr(session, "dungeon_start_x", config.start_cell[0])
        _setattr(session, "dungeon_start_y", config.start_cell[1])
        _setattr(session, "dungeon_boss_x", config.boss_cell[0])
        _setattr(session, "dungeon_boss_y", config.boss_cell[1])
        _setattr(session, "dungeon_maze_index", 0)
        _setattr(session, "dungeon_clear_phase", 0)
        _setattr(session, "dungeon_room_first_seq", actor_base)
        _setattr(session, "dungeon_player_actor_key", int(character_id or 0) & 0xFFFF)
        _setattr(session, "dungeon_required_cells", set())
        _setattr(session, "dungeon_cleared_cells", set())
        dungeon_packet, dungeon_note = self._dungeon_context_packet(
            config=config,
            difficulty=difficulty,
        )
        map_packets, map_note = self._map_context_packets(
            session=session,
            config=config,
            stage=0,
        )
        return True, [dungeon_packet, *map_packets], (
            "altar_enter dungeon=%d mode=%d rounds=%d map_plan=%s "
            "actor_base=0x%04X; %s; %s"
            % (
                config.dungeon_id,
                config.mode,
                config.max_rounds,
                ",".join(str(value) for value in map_plan),
                actor_base & 0xFFFF,
                dungeon_note,
                map_note,
            )
        )

    @staticmethod
    def _round_map_and_phase(session: Any, config: Any) -> tuple[int, int, int]:
        round_index = int(getattr(session, "altar_round", 0) or 0)
        stage = int(getattr(session, "altar_physical_stage", 0) or 0)
        map_plan = tuple(int(value) for value in getattr(session, "altar_map_plan", ()))
        if not 0 <= stage < len(map_plan):
            raise ValueError("altar stage has no selected map")
        local_phase = round_index % 5 if config.mode == 1 else round_index
        return map_plan[stage], local_phase, round_index

    def _build_round_schedule(
        self,
        *,
        session: Any,
        config: Any,
        selection: int,
        started_monotonic: float,
    ) -> tuple[list[AltarScheduledWave], int, str, int, int]:
        map_id, local_phase, round_index = self._round_map_and_phase(session, config)
        map_info = config.maps[map_id]
        variant = ""
        if config.mode == 2:
            if round_index in {0, config.max_rounds - 1}:
                variant = "A"
                selection = 1
            else:
                if int(selection) not in {1, 2}:
                    raise ValueError("ultimate altar round requires A/B selection")
                variant = "A" if int(selection) == 1 else "B"
        candidate_groups = map_info.candidate_groups_for_phase(local_phase, variant)
        if not candidate_groups:
            raise ValueError(
                "altar map %d phase %d variant %r has no candidate groups"
                % (map_id, local_phase, variant)
            )
        selected = list(self.rng.choice(candidate_groups))
        candidate_id = int(selected[0][0].candidate_id)

        waves: list[AltarScheduledWave] = []
        elapsed_ms = 0
        index = 0
        while index < len(selected):
            concurrent = min(
                _MAX_CONCURRENT_PHASES,
                len(selected) - index,
                int(selected[index][0].candidate_row_count),
            )
            group_end = elapsed_ms
            tail_source = None
            for concurrent_index in range(concurrent):
                phase_index = index + concurrent_index
                phase, source = selected[phase_index]
                batches = max(1, int(source.batch_count))
                interval_ms = max(0, int(source.spawn_interval_ms))
                for batch_index in range(batches):
                    spawn_count = int(source.base_spawn_count) + (
                        int(source.spawn_count_increment) * batch_index
                    )
                    if int(source.monster_code) <= 0 or spawn_count <= 0:
                        continue
                    delay_ms = (
                        elapsed_ms
                        + max(0, int(phase.delay_ms))
                        + interval_ms * batch_index
                    )
                    waves.append(
                        AltarScheduledWave(
                            deadline_monotonic=(
                                float(started_monotonic) + delay_ms / 1000.0
                            ),
                            phase_index=phase_index,
                            batch_index=batch_index,
                            spawn_count=min(0xFFFF, spawn_count),
                            phase=phase,
                            source=source,
                        )
                    )
                candidate_end = (
                    elapsed_ms
                    + max(0, int(phase.delay_ms))
                    + interval_ms * (batches - 1)
                )
                if tail_source is None or candidate_end >= group_end:
                    group_end = candidate_end
                    tail_source = source
            if tail_source is not None:
                tail_delay = max(
                    0,
                    int(tail_source.duration_ms)
                    - max(0, int(tail_source.spawn_interval_ms))
                    * max(1, int(tail_source.batch_count)),
                )
                elapsed_ms = group_end + tail_delay
            index += concurrent

        waves.sort(
            key=lambda wave: (
                wave.deadline_monotonic,
                wave.phase_index,
                wave.batch_index,
            )
        )
        phase_numbers = {int(row.round_index) for row in map_info.phases}
        if waves and local_phase == max(phase_numbers):
            waves[-1].is_final_map_wave = True
        return (
            waves,
            max(0, int(selected[0][0].delay_ms)),
            variant,
            candidate_id,
            len(candidate_groups),
        )

    def _start_round(
        self,
        *,
        session: Any,
        config: Any,
        selection: int,
        now_monotonic: float,
    ) -> tuple[bytes, str]:
        if tuple(getattr(session, "altar_active_monster_seqs", ())):
            raise ValueError("altar cannot start a round with active actors")
        (
            waves,
            initial_delay_ms,
            variant,
            candidate_id,
            candidate_count,
        ) = self._build_round_schedule(
            session=session,
            config=config,
            selection=selection,
            started_monotonic=now_monotonic,
        )
        if not waves:
            raise ValueError("altar round produced no scheduled waves")
        round_index = int(getattr(session, "altar_round", 0) or 0)
        _setattr(session, "altar_schedule", tuple(waves))
        _setattr(session, "altar_schedule_index", 0)
        _setattr(session, "altar_schedule_variant", variant)
        _setattr(session, "altar_schedule_candidate_id", candidate_id)
        _setattr(session, "altar_schedule_candidate_count", candidate_count)
        _setattr(session, "altar_wave_variant", variant)
        _setattr(session, "altar_round_selection", int(selection))
        _setattr(session, "altar_local_phase", round_index % 5 if config.mode == 1 else round_index)
        body = build_blood_round_interval_00d7(round_index, initial_delay_ms)
        map_id, local_phase, _unused_round = self._round_map_and_phase(
            session,
            config,
        )
        return self._emit(0x00D7, body), (
            "altar_round_start round=%d/%d map=%d local_phase=%d variant=%s "
            "candidate=%d candidates=%d waves=%d first_delay_ms=%d packet=00D7"
            % (
                round_index + 1,
                config.max_rounds,
                map_id,
                local_phase,
                variant or "fixed",
                candidate_id,
                candidate_count,
                len(waves),
                initial_delay_ms,
            )
        )

    def handle_ready(
        self,
        body: bytes,
        *,
        session: Any,
        now_monotonic: Optional[float] = None,
    ) -> tuple[list[bytes], str]:
        ack = self._emit(0x0102, b"\x01", flag=1)
        if body:
            return [ack], "altar 0102 ACK only reject=nonempty_body len=%d" % len(body)
        if not _altar_run_active(session):
            return [ack], "altar 0102 ACK only scope=inactive"
        if not bool(getattr(session, "altar_waiting_ready", False)):
            return [ack], "altar 0102 ACK only duplicate_ready"
        config = resolve_altar_config(int(getattr(session, "altar_dungeon_id", 0) or 0))
        if config is None:
            return [ack], "altar 0102 ACK only reject=missing_config"
        _setattr(session, "altar_waiting_ready", False)
        packet, note = self._start_round(
            session=session,
            config=config,
            selection=1 if config.mode == 2 else 0,
            now_monotonic=(
                time.monotonic() if now_monotonic is None else float(now_monotonic)
            ),
        )
        return [ack, packet], "altar 0102 ACK success=1; " + note

    def _allocate_sequence(self, session: Any) -> int:
        next_seq = int(getattr(session, "altar_next_monster_seq", 0) or 0) & 0xFFFF
        allocated = {
            int(value) & 0xFFFF
            for value in getattr(session, "altar_allocated_monster_seqs", ())
        }
        player_key = int(getattr(session, "dungeon_player_actor_key", 0) or 0) & 0xFFFF
        for _unused in range(0x10000):
            seq = next_seq & 0xFFFF
            next_seq = (next_seq + 1) & 0xFFFF
            if seq in {0, 0xFFFF, player_key} or seq in allocated:
                continue
            allocated.add(seq)
            _setattr(session, "altar_next_monster_seq", next_seq)
            _setattr(session, "altar_allocated_monster_seqs", tuple(sorted(allocated)))
            return seq
        raise ValueError("altar actor sequence space exhausted")

    def _materialize_wave(
        self,
        *,
        session: Any,
        config: Any,
        scheduled: AltarScheduledWave,
    ) -> tuple[bytes, str]:
        round_index = int(getattr(session, "altar_round", 0) or 0)
        source = scheduled.source
        monster_type = 3 if (
            scheduled.is_final_map_wave
            and round_index == int(config.max_rounds) - 1
        ) else 0
        scale = _clamp_u16(float(scheduled.phase.scale) * 100.0)
        actors: list[Any] = []
        room_mons = list(getattr(session, "dungeon_room_mons", None) or [])
        active = {
            int(value) & 0xFFFF
            for value in getattr(session, "altar_active_monster_seqs", ())
        }
        for _unused in range(int(scheduled.spawn_count)):
            seq = self._allocate_sequence(session)
            actor = AltarWaveActor(
                effect_selector=_ALTAR_EFFECT_SELECTOR,
                monster_seq=seq,
                monster_code=int(source.monster_code),
                monster_type=monster_type,
                level=int(config.basis_level),
                scale=scale,
                x=int(source.x),
                y=int(source.y),
                z=int(source.z),
            )
            actors.append(actor)
            active.add(seq)
            room_mons.append(
                {
                    "seq": seq,
                    "code": int(source.monster_code),
                    "monster_code": int(source.monster_code),
                    "kind": "altar_wave",
                    "is_blocking": True,
                    "is_hostile": True,
                    "grants_exp": False,
                    "killable": True,
                    "killed": False,
                    "type": monster_type,
                    "mon_type": monster_type,
                    "level": int(config.basis_level),
                    "scale": scale,
                    "x": int(source.x),
                    "y": int(source.y),
                    "z": int(source.z),
                    "template_type": int(source.template_type),
                    "candidate_id": int(scheduled.phase.candidate_id),
                    "phase_index": int(scheduled.phase_index),
                    "batch_index": int(scheduled.batch_index),
                    "variant": str(getattr(session, "altar_schedule_variant", "") or ""),
                }
            )
        _setattr(session, "dungeon_room_mons", room_mons)
        _setattr(session, "altar_active_monster_seqs", tuple(sorted(active)))
        _setattr(session, "altar_wave_monster_seqs", tuple(sorted(active)))
        tail = 0 if scheduled.is_final_map_wave else max(
            0,
            min(0xFFFF, int(source.spawn_interval_ms)),
        )
        body = build_blood_monster_spawn_00d2(actors, tail_state=tail)
        return self._emit(0x00D2, body), (
            "altar_wave_due round=%d/%d candidate=%d phase_index=%d batch=%d "
            "actors=%d seqs=%s tail=%d final_map_wave=%d"
            % (
                round_index + 1,
                config.max_rounds,
                int(getattr(session, "altar_schedule_candidate_id", -1)),
                scheduled.phase_index,
                scheduled.batch_index,
                len(actors),
                ",".join(str(actor.monster_seq) for actor in actors),
                tail,
                int(scheduled.is_final_map_wave),
            )
        )

    def validate_wave_deaths(
        self,
        monster_seqs: Sequence[int],
        *,
        session: Any,
    ) -> tuple[bool, str]:
        if not _altar_run_active(session):
            return False, "altar 00FF scope=inactive"
        if bool(getattr(session, "altar_waiting_ready", False)):
            return False, "altar 00FF reject=map_not_ready"
        received = tuple(int(value) & 0xFFFF for value in monster_seqs)
        if not received:
            return False, "altar 00FF reject=empty_death_list"
        if len(received) != len(set(received)):
            return False, "altar 00FF reject=duplicate_death_seq"
        active = {
            int(value) & 0xFFFF
            for value in getattr(session, "altar_active_monster_seqs", ())
        }
        unknown = tuple(value for value in received if value not in active)
        if unknown:
            return False, (
                "altar 00FF reject=death_list_not_active active=%s received=%s"
                % (tuple(sorted(active)), received)
            )
        return True, "altar 00FF accepted_subset=%s" % (received,)

    def _prompt_selection(
        self,
        *,
        session: Any,
        now_monotonic: float,
    ) -> tuple[bytes, str]:
        completed = int(getattr(session, "altar_round", 0) or 0)
        _setattr(session, "altar_waiting_selection", True)
        _setattr(
            session,
            "altar_selection_deadline",
            float(now_monotonic) + _ULTIMATE_SELECTION_TIMEOUT_SECONDS,
        )
        body = build_ultimate_difficulty_prompt_0129(
            completed,
            _ULTIMATE_SELECTION_TIMEOUT_SECONDS,
        )
        return self._emit(0x0129, body), (
            "altar_selection_prompt completed=%d timeout=%ds packet=0129"
            % (completed, _ULTIMATE_SELECTION_TIMEOUT_SECONDS)
        )

    def _resolve_selection(
        self,
        selection: int,
        *,
        session: Any,
        now_monotonic: float,
        source: str,
    ) -> tuple[list[bytes], str]:
        value = int(selection)
        if value not in {1, 2}:
            raise ValueError("ultimate selection must be 1 or 2")
        config = resolve_altar_config(int(getattr(session, "altar_dungeon_id", 0) or 0))
        if config is None or int(config.mode) != 2:
            raise ValueError("ultimate selection has no active config")
        _setattr(session, "altar_waiting_selection", False)
        _setattr(session, "altar_selection_deadline", 0.0)
        _setattr(session, "altar_last_selection", value)
        interval, interval_note = self._start_round(
            session=session,
            config=config,
            selection=value,
            now_monotonic=now_monotonic,
        )
        packets = [interval]
        packet_note = "S1/013D" if source == "client" else "S0/0129"
        if source != "client":
            packets.insert(
                0,
                self._emit(
                    0x0129,
                    build_ultimate_difficulty_resolved_0129(value),
                ),
            )
        return packets, (
            "altar_selection_resolved source=%s value=%d confirm=%s; %s"
            % (source, value, packet_note, interval_note)
        )

    def handle_selection(
        self,
        selection: int,
        *,
        session: Any,
        now_monotonic: Optional[float] = None,
    ) -> tuple[bool, list[bytes], str]:
        if not _altar_run_active(session):
            return False, [], "altar 013D scope=inactive"
        value = int(selection)
        if value not in {1, 2}:
            return False, [], "altar 013D reject=selection value=%d" % value
        if not bool(getattr(session, "altar_waiting_selection", False)):
            return False, [], "altar 013D reject=no_active_prompt"
        packets, note = self._resolve_selection(
            value,
            session=session,
            now_monotonic=(
                time.monotonic() if now_monotonic is None else float(now_monotonic)
            ),
            source="client",
        )
        return True, packets, "altar 013D accepted selection=%d; %s" % (value, note)

    def _prepare_settlement_rewards(self, session: Any, config: Any) -> tuple[tuple[str, int, int], ...]:
        reward_config = resolve_altar_reward_config()
        completed = int(getattr(session, "altar_round", 0) or 0)
        count = reward_config.reward_card_count(completed, config.max_rounds)
        seed = int(getattr(session, "altar_reward_seed", 0) or 1) & 0xFFFFFFFF
        lcg = DnfLcg(seed)
        rewards: list[tuple[str, int, int]] = []
        for _unused in range(count):
            kind = reward_config.classify_candidate(
                completed,
                lcg.next(reward_config.roll_scale),
            )
            candidate = build_clear_reward_plan(
                dungeon_level=config.basis_level,
                difficulty=int(getattr(session, "selected_difficulty", 0) or 0),
                visited_room_count=1,
                total_room_count=1,
                seed=lcg.seed,
            )
            lcg = DnfLcg(candidate.seed_after)
            if kind == "gold":
                amount = int(
                    max(0, candidate.free_gold)
                    * float(reward_config.gold_amount_weight)
                )
                rewards.append(("gold", 0, min(0x7FFFFFFF, amount)))
            elif kind == "item" and candidate.free_item_id > 0 and candidate.free_item_count > 0:
                rewards.append(
                    (
                        "item",
                        int(candidate.free_item_id),
                        int(candidate.free_item_count),
                    )
                )
            else:
                rewards.append(("empty", -1, 0))

        if config.mode == 2:
            point = reward_config.ultimate_point(
                tuple(
                    int(value)
                    for value in getattr(session, "altar_completed_difficulties", ())
                )
            )
            probabilities = reward_config.ultimate_reward_probabilities.get(point)
            if probabilities is not None:
                item_1251_probability, item_1252_probability = probabilities
                roll = lcg.next(100)
                if item_1252_probability > 0 and roll < item_1252_probability:
                    rewards.append(("item", 1252, 1))
                elif (
                    item_1251_probability > 0
                    and roll < item_1252_probability + item_1251_probability
                ):
                    rewards.append(("item", 1251, 1))
        _setattr(session, "altar_reward_seed", lcg.seed)
        return tuple(rewards)

    def _commit_settlement_rewards(self, session: Any) -> tuple[tuple[str, int, int], str]:
        planned = [
            (str(kind), int(item_id), int(count))
            for kind, item_id, count in getattr(session, "altar_settlement_rewards", ())
        ]
        if bool(getattr(session, "altar_rewards_committed", False)):
            return tuple(planned), "reward_commit=idempotent"
        character_id = _character_id(session)
        repo_factory = getattr(self.server, "_repo", None)
        if character_id <= 0 or not callable(repo_factory):
            _setattr(session, "altar_rewards_committed", True)
            return tuple(planned), "reward_commit=projection_only no_repo_or_character"

        repo = repo_factory()
        notes: list[str] = []
        gold_total = sum(count for kind, _item_id, count in planned if kind == "gold")
        gold_ok = True
        if gold_total > 0:
            try:
                gold_ok, gold_note, wallet_total = repo.grant_wallet_gold(
                    character_id,
                    gold_total,
                )
            except Exception as exc:
                gold_ok = False
                gold_note = "exception=%s:%s" % (type(exc).__name__, exc)
                wallet_total = 0
            notes.append(
                "gold_ok=%d amount=%d wallet=%d %s"
                % (int(gold_ok), gold_total, int(wallet_total), gold_note)
            )
        committed: list[tuple[str, int, int]] = []
        for kind, item_id, count in planned:
            if kind == "gold":
                committed.append((kind, item_id, count) if gold_ok else ("empty", -1, 0))
                continue
            if kind != "item" or item_id <= 0 or count <= 0:
                committed.append(("empty", -1, 0))
                continue
            try:
                ok, item_note, slot = repo.grant_inventory_item(
                    character_id,
                    item_id,
                    count,
                )
            except Exception as exc:
                ok = False
                item_note = "exception=%s:%s" % (type(exc).__name__, exc)
                slot = -1
            notes.append(
                "item_ok=%d id=%d count=%d slot=%d %s"
                % (int(ok), item_id, count, int(slot), item_note)
            )
            committed.append((kind, item_id, count) if ok else ("empty", -1, 0))
        result = tuple(committed)
        _setattr(session, "altar_settlement_rewards", result)
        _setattr(session, "altar_rewards_committed", True)
        return result, "reward_commit=" + (" | ".join(notes) if notes else "empty")

    def _complete_round(
        self,
        *,
        session: Any,
        config: Any,
        now_monotonic: float,
    ) -> tuple[list[bytes], str]:
        current_round = int(getattr(session, "altar_round", 0) or 0)
        if config.mode == 2:
            selection = int(getattr(session, "altar_round_selection", 0) or 0)
            completed_difficulties = list(
                int(value)
                for value in getattr(session, "altar_completed_difficulties", ())
            )
            if selection in {1, 2}:
                completed_difficulties.append(selection)
            _setattr(
                session,
                "altar_completed_difficulties",
                tuple(completed_difficulties),
            )
        completed = current_round + 1
        _setattr(session, "altar_round", completed)
        _setattr(session, "altar_schedule", ())
        _setattr(session, "altar_schedule_index", 0)
        _setattr(session, "altar_schedule_candidate_id", -1)
        _setattr(session, "altar_schedule_candidate_count", 0)
        _setattr(session, "altar_wave_monster_seqs", ())

        if completed >= config.max_rounds:
            _setattr(session, "altar_final_clear_pending", True)
            _setattr(session, "dungeon_clear_phase", 2)
            rewards = self._prepare_settlement_rewards(session, config)
            _setattr(session, "altar_settlement_rewards", rewards)
            _setattr(
                session,
                "altar_settlement_deadline",
                float(now_monotonic) + _SETTLEMENT_RANKING_SECONDS,
            )
            started = float(getattr(session, "altar_started_monotonic", now_monotonic) or now_monotonic)
            play_time_ms = max(0, int((float(now_monotonic) - started) * 1000.0))
            summary = build_blood_result_summary_00d0(
                play_time_ms=play_time_ms,
                current_round=completed,
                best_time_ms=play_time_ms,
                best_round=completed,
                max_round=config.max_rounds,
                reward_exp=0,
            )
            return [self._emit(0x00D0, summary)], (
                "altar_dungeon_complete rounds=%d play_time_ms=%d packet=00D0 "
                "reward_cards=%d next=00D1@8s"
                % (completed, play_time_ms, len(rewards))
            )

        if config.mode == 1:
            old_stage = int(getattr(session, "altar_physical_stage", 0) or 0)
            next_stage = completed // 5
            if next_stage != old_stage:
                _setattr(session, "altar_waiting_move_map", True)
                _setattr(session, "altar_pending_physical_stage", next_stage)
                return [], (
                    "altar_physical_map_clear completed=%d stage=%d next_stage=%d "
                    "packet=none native_gate=00D2_tail0 "
                    "wait=C1/002D_then_00D3"
                    % (completed, old_stage, next_stage)
                )
            packet, note = self._start_round(
                session=session,
                config=config,
                selection=0,
                now_monotonic=now_monotonic,
            )
            return [packet], note

        if completed == config.max_rounds - 1:
            packet, note = self._start_round(
                session=session,
                config=config,
                selection=1,
                now_monotonic=now_monotonic,
            )
            return [packet], note
        packet, note = self._prompt_selection(
            session=session,
            now_monotonic=now_monotonic,
        )
        return [packet], note

    def record_confirmed_wave_deaths(
        self,
        monster_seqs: Sequence[int],
        *,
        session: Any,
        source: str,
        now_monotonic: Optional[float] = None,
    ) -> tuple[list[bytes], str]:
        source_name = str(source or "server")
        if not _altar_run_active(session):
            return [], "altar_wave_deaths source=%s scope=inactive" % source_name

        received = {int(value) & 0xFFFF for value in monster_seqs}
        active = {
            int(value) & 0xFFFF
            for value in getattr(session, "altar_active_monster_seqs", ())
        }
        consumed = active.intersection(received)
        ignored = received.difference(active)
        if not consumed:
            return [], (
                "altar_wave_deaths source=%s consumed=0 ignored=%d active=%d"
                % (source_name, len(ignored), len(active))
            )

        active.difference_update(consumed)
        room_mons = list(getattr(session, "dungeon_room_mons", None) or [])
        for row in room_mons:
            try:
                if int(row.get("seq") or 0) in consumed:
                    row["killed"] = True
            except (AttributeError, TypeError, ValueError):
                continue
        _setattr(session, "dungeon_room_mons", room_mons)
        _setattr(session, "altar_active_monster_seqs", tuple(sorted(active)))
        _setattr(session, "altar_wave_monster_seqs", tuple(sorted(active)))

        state_note = (
            "altar_wave_deaths source=%s consumed=%d ignored=%d"
            % (source_name, len(consumed), len(ignored))
        )
        schedule = tuple(getattr(session, "altar_schedule", ()) or ())
        schedule_index = int(getattr(session, "altar_schedule_index", 0) or 0)
        if schedule_index < len(schedule):
            return [], (
                "%s remaining_active=%d remaining_scheduled=%d"
                % (state_note, len(active), len(schedule) - schedule_index)
            )
        if active:
            return [], "%s remaining_active=%d schedule=exhausted" % (
                state_note,
                len(active),
            )
        config = resolve_altar_config(int(getattr(session, "altar_dungeon_id", 0) or 0))
        if config is None:
            return [], "%s reject=missing_config" % state_note
        packets, note = self._complete_round(
            session=session,
            config=config,
            now_monotonic=(
                time.monotonic() if now_monotonic is None else float(now_monotonic)
            ),
        )
        return packets, "%s; %s" % (state_note, note)

    def handle_wave_clear(
        self,
        monster_seqs: Sequence[int],
        *,
        session: Any,
        now_monotonic: Optional[float] = None,
        validated: bool = False,
    ) -> tuple[list[bytes], str]:
        if not validated:
            accepted, validation_note = self.validate_wave_deaths(
                monster_seqs,
                session=session,
            )
            if not accepted:
                return [], validation_note
        else:
            validation_note = "altar 00FF prevalidated"
        packets, note = self.record_confirmed_wave_deaths(
            monster_seqs,
            session=session,
            source="00FF",
            now_monotonic=now_monotonic,
        )
        return packets, "%s; %s" % (validation_note, note)

    def collect_due_packets(
        self,
        *,
        session: Any,
        now_monotonic: Optional[float] = None,
    ) -> tuple[list[bytes], str]:
        if not _altar_run_active(session):
            return [], "altar_pump=inactive"
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        config = resolve_altar_config(int(getattr(session, "altar_dungeon_id", 0) or 0))
        if config is None:
            return [], "altar_pump=missing_config"
        packets: list[bytes] = []
        notes: list[str] = []

        if bool(getattr(session, "altar_waiting_selection", False)):
            deadline = float(getattr(session, "altar_selection_deadline", 0.0) or 0.0)
            if deadline > 0 and now >= deadline:
                selection = int(self.rng.choice((1, 2)))
                resolved_packets, resolved_note = self._resolve_selection(
                    selection,
                    session=session,
                    now_monotonic=now,
                    source="timeout",
                )
                packets.extend(resolved_packets)
                notes.append(resolved_note)

        settlement_deadline = float(
            getattr(session, "altar_settlement_deadline", 0.0) or 0.0
        )
        if (
            settlement_deadline > 0
            and now >= settlement_deadline
            and not bool(getattr(session, "altar_result_groups_sent", False))
        ):
            rewards, commit_note = self._commit_settlement_rewards(session)
            rows = [
                (0 if kind == "gold" else item_id, count)
                if kind in {"gold", "item"}
                else (-1, 0)
                for kind, item_id, count in rewards
            ]
            body = build_blood_result_groups_00d1(
                current_round=int(getattr(session, "altar_round", 0) or 0),
                max_round=config.max_rounds,
                groups=(rows, (), (), ()),
            )
            packets.append(self._emit(0x00D1, body))
            _setattr(session, "altar_result_groups_sent", True)
            _setattr(session, "altar_settlement_deadline", 0.0)
            notes.append("altar_result_groups packet=00D1 cards=%d; %s" % (len(rows), commit_note))

        schedule = tuple(getattr(session, "altar_schedule", ()) or ())
        index = int(getattr(session, "altar_schedule_index", 0) or 0)
        while index < len(schedule) and now >= float(schedule[index].deadline_monotonic):
            packet, note = self._materialize_wave(
                session=session,
                config=config,
                scheduled=schedule[index],
            )
            packets.append(packet)
            notes.append(note)
            index += 1
            _setattr(session, "altar_schedule_index", index)
        if not packets:
            return [], "altar_pump=pending"
        return packets, "; ".join(notes)


def collect_due_altar_packets(
    server: Any,
    *,
    session: Any,
    now_monotonic: Optional[float] = None,
) -> tuple[list[bytes], str]:
    try:
        return AltarRandomFlow(server).collect_due_packets(
            session=session,
            now_monotonic=now_monotonic,
        )
    except Exception as exc:
        return [], "altar_pump=error type=%s detail=%s" % (
            type(exc).__name__,
            exc,
        )
