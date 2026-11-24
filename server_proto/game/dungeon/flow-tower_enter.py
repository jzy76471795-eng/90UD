"""Dedicated Stage3 entry flow for DGN ``tower of death`` maps."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.drop.room_drop_preload import preload_room_drops_before_visibility
from game.quest.quest_index import get_quest_index

_payloads = load_chain("helper-tower_payloads.py")
_tower_actors = load_chain("helper-death_tower_actors.py")
_start_room = load_chain("helper-dungeon_start_room.py")
_actor_keys = load_chain("helper-dungeon_actor_keys.py")
_clear_conditions = load_chain("helper-dungeon_clear_conditions.py")
_tower_inventory = load_chain("helper-death_tower_inventory.py")

build_death_tower_info_008e = _payloads.build_death_tower_info_008e
build_death_tower_floor_008f = _payloads.build_death_tower_floor_008f
load_map_monsters = _tower_actors.load_death_tower_map_monsters
compute_dungeon_seed = _start_room.compute_dungeon_seed
initial_actor_key_base = _actor_keys.initial_actor_key_base
wire_actor_count = _clear_conditions.wire_actor_count

_INITIAL_ENTRY_DUNGEONS = frozenset({11000, 11001})


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    return int(str(raw).strip(), 0)


@dataclass(frozen=True)
class DeathTowerConfig:
    dungeon_id: int
    maps: tuple[int, ...]
    basis_level: int
    max_clear_items: int

    @property
    def final_floor(self) -> int:
        return len(self.maps)


def resolve_death_tower_config(dungeon_id: int) -> DeathTowerConfig | None:
    info = get_quest_index().get_dungeon(int(dungeon_id))
    if (
        info is None
        or str(getattr(info, "dungeon_type", "")).strip().lower()
        != "tower of death"
    ):
        return None
    maps = tuple(int(value) for value in getattr(info, "death_tower_map_indexes", ()) or ())
    if not maps or any(value <= 0 for value in maps):
        return None
    return DeathTowerConfig(
        dungeon_id=int(dungeon_id),
        maps=maps,
        basis_level=max(1, int(getattr(info, "basis_level", 0) or 1)),
        max_clear_items=max(
            0, int(getattr(info, "tower_max_clear_item_num", 0) or 0)
        ),
    )


class TowerEnterFlow:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(0, opcode, body)

    def try_enter_death_tower(
        self,
        *,
        dungeon_id: int,
        difficulty: int,
        character_id: int,
        session: Any,
    ) -> tuple[bool, list[bytes], str]:
        """Return ``handled=False`` for ordinary DGN and tower packets otherwise."""
        if int(dungeon_id) not in _INITIAL_ENTRY_DUNGEONS:
            return False, [], "death_tower_not_enabled_initially"
        config = resolve_death_tower_config(int(dungeon_id))
        if config is None:
            return False, [], "not_death_tower"

        floor = 1
        map_id = config.maps[0]
        # Keep the first Stage3 live hypothesis narrow and reproducible.  The
        # mode/buff bytes remain independently overridable until live confirms
        # the values selected by this frozen client.
        mode = _env_int("USDOF_GAME_DEATH_TOWER_INFO_MODE", 0) & 0xFF
        buff_type = _env_int("USDOF_GAME_DEATH_TOWER_BUFF_TYPE", 11) & 0xFF
        seed = compute_dungeon_seed(int(dungeon_id), int(character_id))
        if seed == 0:
            seed = 1

        player_actor_key = int(character_id or 0) & 0xFFFF
        actor_first_seq = initial_actor_key_base(player_actor_key)
        monsters = load_map_monsters(
            map_id,
            dungeon_basic_lv=config.basis_level,
            first_seq=actor_first_seq,
            active_quest_ids=list(getattr(session, "active_quest_ids", None) or []),
            dungeon_id=int(dungeon_id),
            difficulty=int(difficulty) & 0xFF,
        )
        info_body = build_death_tower_info_008e(
            config.dungeon_id,
            config.final_floor,
            mode=mode,
            buff_type=buff_type,
        )
        session.in_dungeon_select = False
        session.dungeon_select_active = False
        session.in_dungeon_run = True
        session.selected_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
        session.selected_difficulty = int(difficulty) & 0xFF
        session.cur_dungeon_id = int(dungeon_id) & 0xFFFFFFFF
        session.cur_dungeon_difficulty = int(difficulty) & 0xFF
        session.dungeon_map_index = map_id
        session.dungeon_seed = seed
        session.dungeon_basis_level = config.basis_level
        session.dungeon_finish_loading = False
        session.dungeon_clear_phase = 0
        session.dungeon_room_mons = list(monsters)
        session.dungeon_room_first_seq = actor_first_seq
        session.dungeon_player_actor_key = player_actor_key
        session.dungeon_required_cells = {(0, 0)}
        session.dungeon_cleared_cells = set()
        session.dungeon_tower_kind = "death"
        session.dungeon_tower_floor = floor
        session.dungeon_tower_final_floor = config.final_floor
        session.dungeon_tower_maps = config.maps
        session.dungeon_tower_stage_state = 0
        session.dungeon_tower_stage_metrics = ()
        session.dungeon_tower_floor_cleared = False
        session.dungeon_tower_awaiting_move = False
        session.dungeon_tower_settled = False
        session.dungeon_tower_max_clear_items = config.max_clear_items

        occupied_main_slots: list[int] = []
        occupancy_note = "main_slots=unavailable"
        try:
            rows = self.server._repo().list_inventory_items(
                int(character_id), list_type=0
            )
            occupied_main_slots = [
                int(row.get("slot_index", -1))
                for row in rows
                if int(row.get("slot_index", -1)) >= 0
            ]
            occupancy_note = "main_slots=%d" % len(occupied_main_slots)
        except Exception as exc:
            occupancy_note = "main_slots_error=%s" % type(exc).__name__
        reserved_count = _tower_inventory.initialize_death_tower_inventory(
            session,
            occupied_main_slots=occupied_main_slots,
        )
        preload_note = preload_room_drops_before_visibility(
            self.server,
            session,
            source="death_tower_enter_008f",
        )
        floor_body = build_death_tower_floor_008f(
            floor,
            seed,
            map_id,
            monsters=session.dungeon_room_mons,
            items=(),
        )

        packets = [
            self._emit(0x008E, info_body),
            self._emit(0x008F, floor_body),
        ]
        note = (
            "death_tower_enter dungeon=%d floor=%d/%d map=%d seed=0x%08X "
            "monsters=%d actor_uid=0x%04X actor_base=0x%04X actor_first=0x%04X "
            "info_mode=%d buff_type=%d packets=008E,008F "
            "temporary_list=0x1C reserved=%d %s; %s "
            "initial_scope=11000,11001 tickets_and_daily_limits=out_of_scope"
            % (
                config.dungeon_id,
                floor,
                config.final_floor,
                map_id,
                seed,
                len(monsters),
                player_actor_key,
                actor_first_seq,
                (actor_first_seq + 1) & 0xFFFF,
                mode,
                buff_type,
                reserved_count,
                occupancy_note,
                preload_note,
            )
        )
        return True, packets, note

    def advance_death_tower_floor(
        self,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        """Advance one confirmed Stage3 death-tower floor via S0/008F."""
        floor = int(getattr(session, "dungeon_tower_floor", 0) or 0)
        final_floor = int(getattr(session, "dungeon_tower_final_floor", 0) or 0)
        maps = tuple(int(value) for value in getattr(session, "dungeon_tower_maps", ()) or ())
        if floor <= 0 or final_floor <= 0 or len(maps) != final_floor:
            return [], (
                "death_tower_advance reject=invalid_runtime floor=%d final=%d maps=%d"
                % (floor, final_floor, len(maps))
            )
        if floor >= final_floor:
            return [], (
                "death_tower_advance reject=final_floor floor=%d/%d "
                "settlement_owned_by_009F_command2"
                % (floor, final_floor)
            )
        if not bool(getattr(session, "dungeon_tower_floor_cleared", False)):
            return [], (
                "death_tower_advance reject=floor_not_cleared floor=%d/%d"
                % (floor, final_floor)
            )
        if (
            int(getattr(session, "dungeon_tower_stage_state", 0) or 0) != 2
            or not bool(getattr(session, "dungeon_tower_awaiting_move", False))
        ):
            return [], (
                "death_tower_advance reject=await_009F_command2 floor=%d/%d state=%s"
                % (
                    floor,
                    final_floor,
                    getattr(session, "dungeon_tower_stage_state", None),
                )
            )

        next_floor = floor + 1
        map_id = maps[next_floor - 1]
        seed = random.getrandbits(32) & 0xFFFFFFFF
        if seed == 0:
            seed = 1
        dungeon_id = int(
            getattr(session, "selected_dungeon_id", 0)
            or getattr(session, "cur_dungeon_id", 0)
            or 0
        )
        difficulty = int(
            getattr(session, "selected_difficulty", 0)
            or getattr(session, "cur_dungeon_difficulty", 0)
            or 0
        ) & 0xFF
        basis_level = max(1, int(getattr(session, "dungeon_basis_level", 0) or 1))
        previous_monsters = list(getattr(session, "dungeon_room_mons", None) or [])
        actor_first_seq = int(
            getattr(session, "dungeon_room_first_seq", 0) or 0
        ) + wire_actor_count(previous_monsters)
        monsters = load_map_monsters(
            map_id,
            dungeon_basic_lv=basis_level,
            first_seq=actor_first_seq,
            active_quest_ids=list(getattr(session, "active_quest_ids", None) or []),
            dungeon_id=dungeon_id,
            difficulty=difficulty,
        )
        session.dungeon_tower_floor = next_floor
        session.dungeon_map_index = map_id
        session.dungeon_seed = seed
        session.dungeon_finish_loading = False
        session.dungeon_clear_phase = 0
        session.dungeon_room_mons = list(monsters)
        session.dungeon_room_first_seq = actor_first_seq
        session.dungeon_tower_stage_state = 0
        session.dungeon_tower_stage_metrics = ()
        session.dungeon_tower_floor_cleared = False
        session.dungeon_tower_awaiting_move = False
        session.dungeon_drops = {}
        session.dungeon_drop_meta = {}
        session.dungeon_drop_scene_slot = 0
        preload_note = preload_room_drops_before_visibility(
            self.server,
            session,
            source="death_tower_advance_008f",
        )
        floor_body = build_death_tower_floor_008f(
            next_floor,
            seed,
            map_id,
            monsters=session.dungeon_room_mons,
            items=(),
        )

        packet = self._emit(0x008F, floor_body)
        return [packet], (
            "death_tower_advance floor=%d/%d map=%d seed=0x%08X monsters=%d "
            "actor_base=0x%04X actor_first=0x%04X packet=008F %s wait=C1/0025 "
            "dialogue=client_pvf_on_complete_then_on_start"
            % (
                next_floor,
                final_floor,
                map_id,
                seed,
                len(monsters),
                actor_first_seq & 0xFFFF,
                (actor_first_seq + 1) & 0xFFFF,
                preload_note,
            )
        )
