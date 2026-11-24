"""C1/0x002D MOVE_MAP — dungeon room change (副本实现A.md §5.4).

Request (US body longer; first two bytes effective):
  u8 nextX
  u8 nextY

Reply: S0/0x001D full START_MAP on first visit, short room-state form on revisit
(no re-send 001C).
"""
from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.drop.room_drop_preload import preload_room_drops_before_visibility
from game.quest.quest_clear import DungeonClearContext, persist_clear_map_progress
from pvf import pvf_runtime
from game.raid.local_bindings import local_raid_hooks

_payloads = load_chain("helper-dungeon_payloads.py")
_start_room = load_chain("helper-dungeon_start_room.py")
_map_mons = load_chain("helper-dungeon_map_monsters.py")
_room_state = load_chain("helper-dungeon_room_state.py")
_story_mode = load_chain("helper-dungeon_story_mode.py")
_ally_companions = load_chain("helper-dungeon_ally_companions.py")
_hell_party = load_chain("helper-dungeon_hell_party.py")
_clear_conditions = load_chain("helper-dungeon_clear_conditions.py")
_special_conditions = load_chain("helper-dungeon_special_conditions.py")
_tower_enter = load_chain("flow-tower_enter.py")
_another_arad = load_chain("helper-another_arad.py")
_mirror_mission = load_chain("helper-another_arad_quest.py")
_altar_random = load_chain("flow-altar-random.py")

build_start_map_001d_us = _payloads.build_start_map_001d_us
build_start_map_revisit_001d_us = _payloads.build_start_map_revisit_001d_us
resolve_dungeon_room = _start_room.resolve_dungeon_room
advance_layered_map_transition = _start_room.advance_layered_map_transition
is_active_layered_map_transition = _start_room.is_active_layered_map_transition
reset_layered_map_transition = _start_room.reset_layered_map_transition
compute_room_seed = _start_room.compute_room_seed
compute_dungeon_seed = _start_room.compute_dungeon_seed
_ridable_objects = load_chain("helper-dungeon_ridable_objects.py")
ridable_objects_for_room = _ridable_objects.ridable_objects_for_room
from game.dungeon import energy_objects
load_map_monsters = _map_mons.load_map_monsters
load_map_passive_objects = _map_mons.load_map_passive_objects
map_actor_template_cache_source = _map_mons.map_actor_template_cache_source
inject_story_face_preloads = _map_mons.inject_story_face_preloads
resolve_map_path = _map_mons.resolve_map_path
parse_event_monster_positions = _map_mons.parse_event_monster_positions
is_room_cleared = _room_state.is_room_cleared
mark_room_cleared = _room_state.mark_room_cleared
filter_destroyed_objects = _room_state.filter_destroyed_objects
bind_room_passive_objects = _room_state.bind_room_passive_objects
resequence_monsters = _room_state.resequence_monsters
cache_current_room_state = _room_state.cache_current_room_state
cache_room_state = _room_state.cache_room_state
get_room_state = _room_state.get_room_state
next_room_first_seq = _room_state.next_room_first_seq
apply_story_scales_to_monsters = _story_mode.apply_story_scales_to_monsters
story_info_from_dungeon = _story_mode.story_info_from_dungeon
apply_companions_on_move_map = _ally_companions.apply_companions_on_move_map
companion_progress_codes = _ally_companions.companion_progress_codes
get_session_hell_room = _hell_party.get_session_hell_room
is_session_hell_room = _hell_party.is_session_hell_room
append_hellparty_templates = _hell_party.append_hellparty_templates
attach_destroy_object_clear_shadows = (
    _clear_conditions.attach_destroy_object_clear_shadows
)
wire_actor_count = _clear_conditions.wire_actor_count
append_room_condition_actors = _special_conditions.append_room_condition_actors
bind_room_condition_passives = _special_conditions.bind_room_condition_passives
apply_session_another_arad_level = _another_arad.apply_session_another_arad_level
correct_session_another_arad_room = _another_arad.correct_session_another_arad_room


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    return int(str(raw).strip(), 0)


def _mount_hardt_completed_revisit_ridable_objects(
    session: Any,
    *,
    dungeon_id: int,
    map_index: int,
    room_was_visited: bool,
) -> tuple[list[dict[str, int]], str]:
    """Build the narrow 001D actor A/B for Mount Hardt's completed gate."""
    if int(dungeon_id) != 2005 or int(map_index) != 17060 or not room_was_visited:
        return [], ""
    if not bool(getattr(session, "dungeon_boss_condition_complete", False)):
        return [], "mount_hardt_001d_actor_ab=skip condition_complete=0"

    path = resolve_map_path(int(map_index))
    if not path:
        return [], "mount_hardt_001d_actor_ab=skip map_path_missing"
    try:
        positions = parse_event_monster_positions(pvf_runtime.read_text(path))
    except Exception as exc:
        return [], "mount_hardt_001d_actor_ab=skip map_read_error=%r" % (exc,)
    if not positions:
        return [], "mount_hardt_001d_actor_ab=skip event_position_missing"

    pos_x, pos_y = positions[0]
    rows = [
        {
            "pos_x": int(pos_x),
            "pos_y": int(pos_y),
            "object_index": 69200,
            "faction": 200,
            "reserved": 0,
        }
    ]
    return rows, (
        "mount_hardt_001d_actor_ab=append ridable=1 object=69200 pos=(%d,%d)"
        % (int(pos_x), int(pos_y))
    )


def parse_002d_plain(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "next_x": None,
        "next_y": None,
        "layered_request": 0,
        "layout": "unknown",
    }
    # tolerate leading reserved3 codec pad
    views = [raw]
    if len(raw) >= 5:
        views.append(raw[3:])
    for view in views:
        if len(view) >= 2:
            out["next_x"] = view[0] & 0xFF
            out["next_y"] = view[1] & 0xFF
            # Stage3 C1/002D uses byte 10 for the request that advances a
            # DGN [map specification] layered scene.  Short control/test
            # bodies keep the ordinary zero value.
            if len(view) > 10:
                out["layered_request"] = view[10] & 0xFF
            out["layout"] = "u8x_u8y"
            break
    return out


class MoveMap002DService:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.tower_enter = _tower_enter.TowerEnterFlow(server)
        self.altar_random = _altar_random.AltarRandomFlow(server)

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        with self._stage("codec"):
            return self.server._codec_response(flag, opcode, body), "codec"

    def _stage(self, name: str) -> Any:
        metrics = getattr(self.server, "hotpath_metrics", None)
        return metrics.stage(name) if metrics is not None else nullcontext()

    def handle_control_move_adjacent(
        self,
        *,
        direction: str,
        session: Any,
    ) -> tuple[list[bytes], dict[str, Any], str]:
        """Move exactly one resolvable maze cell for the local control API."""

        if session is None or not bool(getattr(session, "in_dungeon_run", False)):
            raise ControlApiError("NOT_IN_DUNGEON", "current client is not in a dungeon")
        if int(getattr(session, "dungeon_clear_phase", 0) or 0) >= 1:
            raise ControlApiError("DUNGEON_SETTLING", "dungeon settlement is active")
        if bool(getattr(session, "altar_active", False)):
            raise ControlApiError(
                "ALTAR_CLIENT_TRANSITION_REQUIRED",
                "altar room changes require a client-generated move request",
            )

        direction_key = str(direction or "").strip().lower()
        deltas = {
            "left": (-1, 0),
            "right": (1, 0),
            "up": (0, -1),
            "down": (0, 1),
        }
        delta = deltas.get(direction_key)
        if delta is None:
            raise ControlApiError("INVALID_PARAMS", "direction must be left, right, up, or down", status=400)

        dungeon_id = int(
            getattr(session, "selected_dungeon_id", None)
            or getattr(session, "cur_dungeon_id", 0)
            or 0
        )
        if dungeon_id <= 0:
            raise ControlApiError("NOT_IN_DUNGEON", "current client has no active dungeon")
        old_x = int(getattr(session, "dungeon_cell_x", 0) or 0)
        old_y = int(getattr(session, "dungeon_cell_y", 0) or 0)
        next_x = old_x + int(delta[0])
        next_y = old_y + int(delta[1])
        if not (0 <= next_x <= 0xFF and 0 <= next_y <= 0xFF):
            raise ControlApiError("NO_ADJACENT_ROOM", "direction leaves the dungeon maze")

        difficulty = int(getattr(session, "selected_difficulty", 0) or 0) & 0xFF
        maze_index = getattr(session, "dungeon_maze_index", None)
        room = resolve_dungeon_room(
            dungeon_id,
            next_x,
            next_y,
            difficulty=difficulty,
            maze_index=int(maze_index) if maze_index is not None else None,
            boss_map_variants=getattr(session, "dungeon_boss_map_variants", None),
        )
        if int(room.get("map_index") or 0) <= 0:
            raise ControlApiError("NO_ADJACENT_ROOM", "no dungeon room exists in that direction")

        packets, note = self.handle(bytes((next_x, next_y)), session=session)
        if not packets:
            raise ControlApiError("MOVE_REJECTED", note)
        return packets, {
            "direction": direction_key,
            "from": {"x": old_x, "y": old_y},
            "to": {"x": next_x, "y": next_y},
            "map_index": int(getattr(session, "dungeon_map_index", 0) or 0),
        }, "control move_adjacent; " + note

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        parsed = parse_002d_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_MOVE_MAP_HANDLE", True)
        base = (
            "C1/002D MOVE_MAP plain_len=%s plain=%s next=(%s,%s) layered_request=%s handle=%s"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["next_x"],
                parsed["next_y"],
                parsed["layered_request"],
                int(handle_on),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF no_s2c"

        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        dungeon_id = 0
        if session is not None:
            dungeon_id = int(
                getattr(session, "selected_dungeon_id", None)
                or getattr(session, "cur_dungeon_id", 0)
                or 0
            )
        if not in_run or dungeon_id <= 0:
            return [], base + "; reject=not_in_dungeon_run dungeon_id=%s" % dungeon_id

        next_x = parsed.get("next_x")
        next_y = parsed.get("next_y")
        if next_x is None or next_y is None:
            return [], base + "; reject=bad_xy"

        altar_handled, altar_packets, altar_note = self.altar_random.handle_move_map(
            next_x=int(next_x),
            next_y=int(next_y),
            session=session,
        )
        if altar_handled:
            return altar_packets, "%s; %s" % (base, altar_note)

        is_death_tower = (
            str(getattr(session, "dungeon_tower_kind", "") or "").lower()
            == "death"
        )
        if is_death_tower:
            if (int(next_x), int(next_y)) != (0xFF, 0xFF):
                return [], (
                    "%s; death_tower reject=unexpected_sentinel expected=(255,255)"
                    % base
                )
            packets, tower_note = self.tower_enter.advance_death_tower_floor(
                session=session
            )
            return packets, "%s; %s" % (base, tower_note)

        clear_phase = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        if clear_phase >= 1 and _env_bool(
            "USDOF_GAME_DUNGEON_MOVE_MAP_LOCK_AFTER_CLEAR", True
        ):
            return [], "%s; reject=dungeon_clear_locked phase=%s" % (
                base,
                clear_phase,
            )

        difficulty = int(getattr(session, "selected_difficulty", 0) or 0) & 0xFF
        maze_index = getattr(session, "dungeon_maze_index", None)
        cid = int(getattr(session, "selected_character_id", 0) or 0)
        current_layered_scene_active = is_active_layered_map_transition(
            session,
            dungeon_id=int(dungeon_id),
            maze_index=int(maze_index) if maze_index is not None else 0,
            cell_x=int(getattr(session, "dungeon_cell_x", 0) or 0),
            cell_y=int(getattr(session, "dungeon_cell_y", 0) or 0),
            map_index=int(getattr(session, "dungeon_map_index", 0) or 0),
        )

        with self._stage("room_parse"):
            room = resolve_dungeon_room(
                dungeon_id,
                int(next_x),
                int(next_y),
                difficulty=difficulty,
                maze_index=int(maze_index) if maze_index is not None else None,
                boss_map_variants=getattr(session, "dungeon_boss_map_variants", None),
            )
        room, another_arad_endpoint_note = correct_session_another_arad_room(
            session,
            room,
        )
        map_index = int(room.get("map_index") or 0)
        if map_index <= 0:
            return [], (
                "%s; reject=no_map_index cell=(%s,%s) maze=%s"
                % (base, next_x, next_y, room.get("maze_index"))
            )
        normal_room_map_index = map_index
        hell_room = get_session_hell_room(session)
        is_hell_room = is_session_hell_room(session, int(next_x), int(next_y))
        monster_map_index = normal_room_map_index
        layered_map_index = 0
        layered_note = "layered=skip"
        if is_hell_room and hell_room:
            map_index = int(hell_room.get("map_index") or map_index)
            monster_map_index = int(
                hell_room.get("normal_map_index") or normal_room_map_index
            )
            reset_layered_map_transition(session)
            layered_note = "layered=skip hell_room"
        elif not is_hell_room:
            layered_map_index, layered_note = advance_layered_map_transition(
                session,
                dungeon_id=int(dungeon_id),
                maze_index=int(room.get("maze_index") or 0),
                cell_x=int(next_x),
                cell_y=int(next_y),
                request_flag=int(parsed.get("layered_request") or 0),
            )
            if layered_map_index is None:
                return [], "%s; reject=%s" % (base, layered_note)
            if layered_map_index > 0:
                # A DGN layer is a real MAP script, unlike the Hell Party
                # overlay.  Its actors, passive objects and task context use
                # the selected layer rather than the topology's base map.
                map_index = int(layered_map_index)
                normal_room_map_index = int(layered_map_index)
                monster_map_index = int(layered_map_index)
        layered_scene_active = layered_map_index > 0

        # 离开当前房前：若挡路怪已清完则记入 session，回来不再刷怪
        # （友方/中立 AI、special passive is_blocking=false；敌对 AI 挡门）
        try:
            cur_x = int(getattr(session, "dungeon_cell_x", 0) or 0)
            cur_y = int(getattr(session, "dungeon_cell_y", 0) or 0)
            cur_mons = list(getattr(session, "dungeon_room_mons", None) or [])
            blocking = [m for m in cur_mons if bool(m.get("is_blocking", True))]
            if (
                blocking
                and all(m.get("killed") for m in blocking)
                and not current_layered_scene_active
            ):
                mark_room_cleared(session, cur_x, cur_y)
                if bool(getattr(session, "another_arad_active", False)):
                    _mirror_mission.note_another_arad_room_cleared(
                        session, cur_x, cur_y
                    )
        except Exception:
            pass

        # Persist the room being left before binding the target.  A revisit is
        # keyed by the exact maze/cell/map variant and keeps the original seed,
        # actor keys and mutable killed/passive-object state.
        cache_current_room_state(session)
        cached_room = get_room_state(
            session,
            maze_index=int(getattr(session, "dungeon_maze_index", 0) or 0),
            cell_x=int(next_x),
            cell_y=int(next_y),
            map_index=map_index,
        )
        room_was_visited = cached_room is not None
        if cached_room is not None:
            first_seq = int(cached_room.get("first_seq") or 0)
            seed = int(cached_room.get("seed") or 0) & 0xFFFFFFFF
        else:
            first_seq = next_room_first_seq(session)
            seed = compute_room_seed(dungeon_id, int(next_x), int(next_y), cid)
        basis = int(room.get("basis_level") or 0)
        raid_hooks = local_raid_hooks(self.server)
        if raid_hooks is not None:
            shared_plan = raid_hooks.prepare_room_plan(session,
                (int(maze_index or 0), int(next_x), int(next_y), int(map_index)))
            if shared_plan is not None:
                seed, first_seq = shared_plan
        from game.normal_party_run import room_plan_for
        ordinary_plan = room_plan_for(self.server, session,
            (int(maze_index or 0), int(next_x), int(next_y), int(map_index)))
        if ordinary_plan is not None:
            seed, first_seq = ordinary_plan
        clear_phase = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        # phase>=1：ENABLE_CLEAR 已发 / 结算中；禁止再刷怪（live：通关后回 boss 房 mon=4 可再杀）
        freeze_mons = clear_phase >= 1 and _env_bool(
            "USDOF_GAME_DUNGEON_MOVE_MAP_FREEZE_AFTER_CLEAR", True
        )
        # 本局已清理过的房间：临时记录，再进 mon=0
        room_was_cleared = (
            not layered_scene_active
            and is_room_cleared(session, int(next_x), int(next_y))
            and _env_bool("USDOF_GAME_DUNGEON_ROOM_CLEAR_PERSIST", True)
        )
        passive_extra = None
        ally_note = "ally_companions=skip"
        condition_actor_note = "condition_actors=skip"
        condition_passive_note = "condition_passives=skip"
        actor_template_source = "skip"
        if freeze_mons:
            # 通关结算后：空 mon，不注入同伴（避免结算 UI 多 actor）
            mons = []
            ally_note = "ally_companions=skip_freeze"
            condition_actor_note = "condition_actors=skip_freeze"
        elif cached_room is not None:
            mons = list(cached_room.get("monsters") or [])
            ally_note = "ally_companions=room_cache"
            condition_actor_note = "condition_actors=room_cache"
            actor_template_source = "room_cache"
        elif room_was_cleared:
            # 已清房：不重刷敌对/建筑。
            # 友方跨房 mon 克隆默认 OFF（闪退证伪）；INJECT=0 时 mons 保持空。
            mons = []
            mons, ally_note = apply_companions_on_move_map(
                session,
                mons,
                map_index=int(map_index),
                dungeon_basic_lv=basis,
                first_seq=first_seq,
                freeze_mons=False,
            )
            mons = resequence_monsters(mons, first_seq=first_seq)
            condition_actor_note = "condition_actors=skip_cleared"
        else:
            template_state: dict[str, str] = {}
            expected_template_source = map_actor_template_cache_source(
                monster_map_index
            )
            with self._stage("actor_template"):
                with self._stage(
                    "actor_template_%s" % expected_template_source
                ):
                    mons = load_map_monsters(
                        monster_map_index,
                        dungeon_basic_lv=basis,
                        first_seq=first_seq,
                        active_quest_ids=list(
                            getattr(session, "active_quest_ids", None) or []
                        ),
                        dungeon_id=dungeon_id,
                        difficulty=difficulty,
                        template_state=template_state,
                    )
            actor_template_source = template_state.get("source", "unknown")
            # 本局已摧毁的建筑（蚁卵/蚁穴等）不重刷
            with self._stage("session_overlay"):
                mons = filter_destroyed_objects(
                    session,
                    mons,
                    map_index=monster_map_index,
                )
                mons = resequence_monsters(mons, first_seq=first_seq)
                mons, condition_actor_note = append_room_condition_actors(
                    session,
                    mons,
                    cell_x=int(next_x),
                    cell_y=int(next_y),
                    map_index=monster_map_index,
                    dungeon_basic_lv=basis,
                    first_seq=first_seq,
                )
                mons = resequence_monsters(mons, first_seq=first_seq)
            # 过图同样预装 maze 内 face mon（其它地区地下城剧情对话框通用）
            maze_index = int(getattr(session, "dungeon_maze_index", 0) or 0)
            with self._stage("session_overlay"):
                mons = inject_story_face_preloads(
                    mons,
                    dungeon_id=int(dungeon_id),
                    map_index=int(map_index),
                    maze_index=maze_index,
                    dungeon_basic_lv=basis,
                    first_seq=first_seq,
                )
            # 友方：harvest 本房 + 累加克隆本局已见未死/未叛变同伴（INJECT 默认 ON）
            with self._stage("session_overlay"):
                mons, ally_note = apply_companions_on_move_map(
                    session,
                    mons,
                    map_index=int(map_index),
                    dungeon_basic_lv=basis,
                    first_seq=first_seq,
                    freeze_mons=False,
                )
                mons = resequence_monsters(mons, first_seq=first_seq)
        room_passive_objects = load_map_passive_objects(monster_map_index)
        room_passive_objects.extend(energy_objects.passive_shadows(
            session, dungeon_id, next_x, next_y))
        with self._stage("session_overlay"):
            bind_room_passive_objects(
                session,
                room_passive_objects,
                map_index=monster_map_index,
            )
            condition_passive_note = bind_room_condition_passives(
                session,
                mons,
                map_index=monster_map_index,
            )
        # 剧情 HP/DEF 缩放挂到 mon（与进本一致）；经验权重保持会话已算好的 combined
        story_active = bool(getattr(session, "dungeon_story_mode", False)) or bool(
            getattr(session, "quest_connected", False)
        )
        story_info = None
        dgn_info = None
        try:
            from game.quest.quest_index import get_quest_index

            dgn_info = get_quest_index().get_dungeon(int(dungeon_id))
            story_info = story_info_from_dungeon(dgn_info)
        except Exception:
            story_info = None
        with self._stage("session_overlay"):
            mons = apply_story_scales_to_monsters(
                mons, story_info, story_active=story_active
            )
        if (
            is_hell_room
            and not freeze_mons
            and not room_was_cleared
            and not room_was_visited
        ):
            with self._stage("session_overlay"):
                mons = append_hellparty_templates(mons, hell_room)
                mons = resequence_monsters(mons, first_seq=first_seq)
        clear_condition_note = "clear_conditions=skip"
        if not freeze_mons and not room_was_cleared and not room_was_visited:
            with self._stage("session_overlay"):
                mons, clear_condition_note = attach_destroy_object_clear_shadows(
                    session,
                    mons,
                    dungeon_id=int(dungeon_id),
                    maze_index=int(getattr(session, "dungeon_maze_index", 0) or 0),
                    map_index=int(map_index),
                )
        with self._stage("session_overlay"):
            mons, another_arad_level, another_arad_changed = (
                apply_session_another_arad_level(session, mons)
            )
        # DGN group ids select rules; MAP [item] references bind them to the
        # special-passive instance ids required by the client's 001D table.
        try:
            from game.drop.drop_service import generate_passive_object_item_entries
            from game.quest.quest_index import passive_object_items_for_maze

            spo_items = passive_object_items_for_maze(
                dgn_info, int(room.get("maze_index") or 0)
            )
            if spo_items and not freeze_mons and not room_was_visited:
                passive_extra = generate_passive_object_item_entries(
                    session, spo_items, passive_objects=room_passive_objects,
                )
        except Exception:
            passive_extra = None
        mount_hardt_actor_note = ""
        if room_was_visited:
            ridable_objects = []
            if not freeze_mons:
                ridable_objects, mount_hardt_actor_note = (
                    _mount_hardt_completed_revisit_ridable_objects(
                        session,
                        dungeon_id=int(dungeon_id),
                        map_index=int(map_index),
                        room_was_visited=True,
                    )
                )
        else:
            ridable_objects = ridable_objects_for_room(
                int(dungeon_id),
                int(room.get("maze_index") or 0),
                int(next_x),
                int(next_y),
                seed=(energy_objects.run_seed(session) if int(dungeon_id) == 218
                      else compute_dungeon_seed(int(dungeon_id), cid)),
            )
        party_member = _env_int("USDOF_GAME_DUNGEON_ENTER_001D_PARTY_MEMBER", 0) & 0xFF
        from game.normal_party_run import map_actor_index_for
        party_member = map_actor_index_for(self.server, session, party_member)
        room_flag = max(1, min(2, _env_int("USDOF_GAME_DUNGEON_ENTER_001D_ROOM_FLAG", 1))) & 0xFF
        extra_n = len(passive_extra or [])
        ridable_n = len(ridable_objects)
        hell_party_mode = int(
            getattr(session, "dungeon_hell_party_mode", 0) or 0
        ) & 0xFF
        hell_enabled = bool(getattr(session, "dungeon_hell_enabled", False))

        # session: field names historically "start" mean current cell
        session.dungeon_start_x = int(next_x) & 0xFF
        session.dungeon_start_y = int(next_y) & 0xFF
        session.dungeon_cell_x = int(next_x) & 0xFF
        session.dungeon_cell_y = int(next_y) & 0xFF
        session.dungeon_map_index = map_index
        session.dungeon_normal_map_index = normal_room_map_index
        session.dungeon_hell_room_active = bool(is_hell_room)
        session.dungeon_seed = seed
        session.dungeon_room_mons = list(mons)
        session.dungeon_room_first_seq = first_seq
        bound_room = cache_room_state(
            session,
            maze_index=int(getattr(session, "dungeon_maze_index", 0) or 0),
            cell_x=int(next_x),
            cell_y=int(next_y),
            map_index=map_index,
            normal_map_index=normal_room_map_index,
            monsters=session.dungeon_room_mons,
            first_seq=first_seq,
            seed=seed,
        )
        if bound_room is not None:
            session.dungeon_room_mons = bound_room["monsters"]
        if raid_hooks is not None:
            raid_hooks.bind_shared_room(session)
            mons = list(session.dungeon_room_mons)
        from game.normal_party_run import bind_room
        if bind_room(self.server, session) is not None:
            mons = list(session.dungeon_room_mons)
        session.dungeon_finish_loading = False
        if room.get("basis_level") is not None:
            session.dungeon_basis_level = int(room.get("basis_level") or 0)
        # 经验权重：剧情进本已把 base×story 写入 session；MOVE_MAP 勿用裸 base 覆盖
        if getattr(session, "dungeon_experience_weight", None) is None:
            if room.get("experience_weight") is not None:
                session.dungeon_experience_weight = float(room.get("experience_weight") or 1.0)
            elif room.get("experience_increasing_point") is not None:
                session.dungeon_experience_weight = float(
                    room.get("experience_increasing_point") or 1.0
                )
        if room.get("boss_x") is not None:
            session.dungeon_boss_x = int(room.get("boss_x") or 0) & 0xFF
            session.dungeon_boss_y = int(room.get("boss_y") or 0) & 0xFF
        # 目标房本身无挡路怪（空房 / 仅 APC）→ 视为已清
        has_blocking = any(bool(m.get("is_blocking", True)) for m in mons)
        if not has_blocking and not freeze_mons and not layered_scene_active:
            try:
                mark_room_cleared(session, int(next_x), int(next_y))
                if bool(getattr(session, "another_arad_active", False)):
                    _mirror_mission.note_another_arad_room_cleared(
                        session, int(next_x), int(next_y)
                    )
            except Exception:
                pass

        preload_note = preload_room_drops_before_visibility(
            self.server,
            session,
            source="move_map_001d",
        )
        room_mons_for_wire = list(
            getattr(session, "dungeon_room_mons", None) or []
        )
        with self._stage("packet_build"):
            if room_was_visited and not freeze_mons:
                body_001d = build_start_map_revisit_001d_us(
                    cell_x=int(next_x) & 0xFF,
                    cell_y=int(next_y) & 0xFF,
                    seed=seed,
                    party_member_index=party_member,
                    ridable_objects=ridable_objects,
                )
            else:
                body_001d = build_start_map_001d_us(
                    cell_x=int(next_x) & 0xFF,
                    cell_y=int(next_y) & 0xFF,
                    seed=seed,
                    map_index=map_index,
                    room_flag=room_flag,
                    layered=1 if (is_hell_room or layered_scene_active) else 0,
                    monsters=room_mons_for_wire,
                    open_door_even_enemy=bool(
                        room.get("open_door_even_enemy")
                    ),
                    hell_party_mode=hell_party_mode if hell_enabled else 0,
                    hell_fog=1 if hell_enabled else 0,
                    party_member_index=party_member,
                    passive_object_drops=passive_extra,
                    ridable_objects=ridable_objects,
                )

        pkt, mode = self._emit(0, 0x001D, body_001d, "codec")
        response_packets = [pkt]
        clear_map_note = "clear_map skip=blocking_or_frozen"
        if (
            not layered_scene_active
            and not has_blocking
            and not freeze_mons
            and not room_was_visited
            and _env_bool("USDOF_GAME_QUEST_CLEAR_MAP_PROGRESS", True)
        ):
            try:
                seen_companions, alive_companions = companion_progress_codes(session)
                quest_packets, clear_map_note = persist_clear_map_progress(
                    self.server,
                    session,
                    context=DungeonClearContext(
                        dungeon_id=dungeon_id,
                        difficulty=difficulty,
                        map_id=normal_room_map_index,
                        seen_companion_codes=seen_companions,
                        alive_companion_codes=alive_companions,
                    ),
                )
                response_packets.extend(quest_packets)
            except Exception as exc:
                clear_map_note = "clear_map err=%r" % (exc,)
        elif layered_scene_active:
            clear_map_note = "clear_map skip=layered_scene"
        note = (
            "%s; grant 001D/%s len=%d mon=%d logical=%d extra=%d ridable=%d "
            "first_seq=%d map=%s "
            "seed=0x%08X boss_cell=%s clear_phase=%s freeze_mons=%s "
            "room_cleared=%s room_revisit=%s hell_enabled=%s hell_room=%s hell_mode=%s "
            "normal_map=%s layered_scene=%s layered_note=%s actor_template=%s; %s; %s; %s; %s; %s; %s; "
            "chain_file=game/chains/opcode002d-move_map.py"
            % (
                base,
                mode,
                len(body_001d),
                wire_actor_count(mons),
                len(mons),
                extra_n,
                ridable_n,
                first_seq,
                map_index,
                seed,
                bool(room.get("is_boss_cell")),
                clear_phase,
                int(freeze_mons),
                int(room_was_cleared),
                int(room_was_visited),
                int(hell_enabled),
                int(is_hell_room),
                hell_party_mode if hell_enabled else 0,
                monster_map_index,
                int(layered_scene_active),
                layered_note,
                actor_template_source,
                preload_note,
                ally_note,
                condition_actor_note,
                condition_passive_note,
                clear_condition_note,
                clear_map_note,
            )
        )
        if another_arad_level > 0:
            note += "; another_arad level=%d changed=%d" % (
                another_arad_level,
                another_arad_changed,
            )
            if another_arad_endpoint_note:
                note += " %s" % another_arad_endpoint_note
        if mount_hardt_actor_note:
            note += "; %s" % mount_hardt_actor_note
        return response_packets, note
