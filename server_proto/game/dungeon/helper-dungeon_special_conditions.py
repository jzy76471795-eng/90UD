"""PVF-driven special-dungeon room-condition actors.

Some DGN files declare dungeon-wide ``[boss room entrance condition]``
targets.  Most MAP files keep matching actors under ``[monster condition]``
and a run assigns them to eligible non-start/non-boss rooms.  Other maps keep
fixed targets in their ordinary ``[monster]`` table.  Those existing actors
are marked when their room is entered; they are never duplicated on the wire.

The actor's native ``[proc action]`` may create a passive object used by a
quest.  Those objects stay client-created; this module only registers a
server-side passive row so C1/0027 can resolve its code as hunt-enemy type 3.
"""
from __future__ import annotations

import posixpath
import re
import threading
from dataclasses import dataclass
from typing import Any, Sequence

from game.chain_loader import load_chain
from pvf import pvf_runtime

_map_actors = load_chain("helper-dungeon_map_monsters.py")


@dataclass(frozen=True)
class ConditionPassiveSpawn:
    object_code: int
    offset_x: int = 0
    offset_y: int = 0
    offset_z: int = 0


@dataclass(frozen=True)
class ConditionRoomCandidate:
    cell_x: int
    cell_y: int
    map_ids: tuple[int, ...]
    condition_codes: tuple[int, ...]
    map_monster_codes: tuple[int, ...] = ()


@dataclass(frozen=True)
class BossEntranceConditionConfig:
    dungeon_id: int
    maze_index: int
    target_codes: tuple[int, ...]
    candidates: tuple[ConditionRoomCandidate, ...]
    passive_spawns: tuple[tuple[int, tuple[ConditionPassiveSpawn, ...]], ...]

    def spawns_for(self, monster_code: int) -> tuple[ConditionPassiveSpawn, ...]:
        code = int(monster_code)
        for source_code, spawns in self.passive_spawns:
            if int(source_code) == code:
                return tuple(spawns)
        return ()

    def required_assignment_count(self) -> int:
        """Return the run-owned targets needed before the local gate opens."""
        condition_codes = {
            int(code)
            for candidate in self.candidates
            for code in candidate.condition_codes
        }
        required = 0
        seen_direct_codes: set[int] = set()
        for raw_code in self.target_codes:
            code = int(raw_code)
            if code <= 0:
                continue
            if code in condition_codes:
                # Hidden condition targets retain the DGN declaration's
                # one-assignment-per-entry behavior, including duplicates.
                required += 1
                continue
            if code in seen_direct_codes:
                continue
            seen_direct_codes.add(code)
            direct_count = 0
            for candidate in self.candidates:
                # A DGN cell with alternate MAP templates cannot prove which
                # ordinary actor instance the client received, so it remains
                # outside this fixed-target path.
                if len(candidate.map_ids) != 1:
                    continue
                direct_count += sum(
                    1
                    for candidate_code in candidate.map_monster_codes
                    if int(candidate_code) == code
                )
            # Keep the pre-existing fail-closed behavior for a declared code
            # with no usable PVF actor, while ordinary fixed targets count one
            # time per exact source actor.
            required += max(1, direct_count)
        return required


_LOCK = threading.RLock()
_CONFIGS: dict[tuple[int, int], BossEntranceConditionConfig] = {}
_WARMED = False


def _first_tag_body(text: str, tag: str) -> str:
    match = re.search(
        r"(?im)^\s*`?\[" + re.escape(tag) + r"\]`?\s*$",
        str(text or ""),
    )
    if match is None:
        return ""
    tail = str(text or "")[match.end() :]
    next_tag = re.search(r"(?m)^\s*`?\[", tail)
    if next_tag is not None:
        tail = tail[: next_tag.start()]
    return tail.strip()


def _first_int(text: str, tag: str, default: int = 0) -> int:
    body = _first_tag_body(text, tag)
    match = re.search(r"-?\d+", body)
    return int(match.group(0)) if match is not None else int(default)


def parse_created_basepos_passives(action_text: str) -> tuple[ConditionPassiveSpawn, ...]:
    """Return passive objects created relative to the condition actor."""
    out: list[ConditionPassiveSpawn] = []
    seen: set[tuple[int, int, int, int]] = set()
    for match in re.finditer(
        r"(?is)\[create\s+passiveobject\](.*?)"
        r"\[/create\s+passiveobject\]",
        str(action_text or ""),
    ):
        block = match.group(1) or ""
        if re.search(r"(?i)\[use\s+my\s+basepos\]", block) is None:
            continue
        object_code = _first_int(block, "index")
        if object_code <= 0:
            continue
        values = [int(value) for value in re.findall(r"-?\d+", _first_tag_body(block, "pos"))]
        offset_x = values[0] if len(values) >= 1 else 0
        offset_y = values[1] if len(values) >= 2 else 0
        offset_z = values[2] if len(values) >= 3 else 0
        key = (object_code, offset_x, offset_y, offset_z)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ConditionPassiveSpawn(
                object_code=object_code,
                offset_x=offset_x,
                offset_y=offset_y,
                offset_z=offset_z,
            )
        )
    return tuple(out)


def _condition_actor_passive_spawns(
    monster_code: int,
) -> tuple[ConditionPassiveSpawn, ...]:
    monster_path = _map_actors.resolve_monster_script_path(int(monster_code))
    if not monster_path:
        return ()
    monster_text = pvf_runtime.read_text(monster_path)
    proc_action = _first_tag_body(monster_text, "proc action").strip("` ")
    if not proc_action:
        return ()
    action_path = posixpath.normpath(
        posixpath.join(posixpath.dirname(monster_path), proc_action.replace("\\", "/"))
    )
    try:
        action_text = pvf_runtime.read_text(action_path)
    except Exception:
        return ()
    return parse_created_basepos_passives(action_text)


def build_boss_entrance_condition_config(
    dungeon: Any,
    maze: Any,
) -> BossEntranceConditionConfig | None:
    dungeon_id = int(getattr(dungeon, "dungeon_id", 0) or 0)
    maze_index = int(getattr(maze, "index", 0) or 0)
    target_codes = tuple(
        int(code)
        for code in tuple(getattr(dungeon, "boss_entrance_hunt_monsters", ()) or ())
        if int(code) > 0
    )
    if dungeon_id <= 0 or not target_codes:
        return None

    excluded = {
        (int(point[0]), int(point[1]))
        for point in (
            list(getattr(maze, "start_maps", None) or [])
            + list(getattr(maze, "boss_maps", None) or [])
        )
        if isinstance(point, (list, tuple)) and len(point) >= 2
    }
    by_cell: dict[tuple[int, int], list[int]] = {}
    for spec in list(getattr(maze, "map_specs", None) or []):
        if str(spec.get("type") or "").strip().lower() != "map":
            continue
        cell = (int(spec.get("x") or 0), int(spec.get("y") or 0))
        if cell in excluded:
            continue
        map_ids = [
            int(map_id)
            for map_id in list(spec.get("candidates") or [spec.get("map_id")])
            if int(map_id or 0) > 0
        ]
        bucket = by_cell.setdefault(cell, [])
        for map_id in map_ids:
            if map_id not in bucket:
                bucket.append(map_id)

    candidates: list[ConditionRoomCandidate] = []
    wanted = set(target_codes)
    for (cell_x, cell_y), map_ids in by_cell.items():
        condition_codes: list[int] = []
        map_monster_codes: list[int] = []
        for map_id in map_ids:
            for code in _map_actors.map_condition_monster_codes(map_id):
                if int(code) in wanted and int(code) not in condition_codes:
                    condition_codes.append(int(code))
            for code in _map_actors.map_monster_codes(map_id):
                if int(code) in wanted:
                    map_monster_codes.append(int(code))
        if not condition_codes and not map_monster_codes:
            continue
        candidates.append(
            ConditionRoomCandidate(
                cell_x=cell_x,
                cell_y=cell_y,
                map_ids=tuple(map_ids),
                condition_codes=tuple(condition_codes),
                map_monster_codes=tuple(map_monster_codes),
            )
        )

    passive_spawns = tuple(
        (code, _condition_actor_passive_spawns(code))
        for code in dict.fromkeys(target_codes)
    )
    return BossEntranceConditionConfig(
        dungeon_id=dungeon_id,
        maze_index=maze_index,
        target_codes=target_codes,
        candidates=tuple(candidates),
        passive_spawns=passive_spawns,
    )


def warmup_special_condition_index() -> dict[str, int]:
    """Build every DGN/MAP special-condition config before listeners start."""
    from game.quest.quest_index import get_quest_index

    quest_index = get_quest_index()
    quest_index.ensure_all_dungeons_loaded()
    configs: dict[tuple[int, int], BossEntranceConditionConfig] = {}
    declared_dungeons = 0
    target_count = 0
    candidate_count = 0
    passive_spawn_count = 0
    for dungeon in list(quest_index.dungeon_cache.values()):
        targets = tuple(getattr(dungeon, "boss_entrance_hunt_monsters", ()) or ())
        if not targets:
            continue
        declared_dungeons += 1
        for maze in list(getattr(dungeon, "mazes", None) or []):
            config = build_boss_entrance_condition_config(dungeon, maze)
            if config is None or not config.candidates:
                continue
            configs[(config.dungeon_id, config.maze_index)] = config
            target_count += len(config.target_codes)
            candidate_count += len(config.candidates)
            passive_spawn_count += sum(
                len(spawns) for _code, spawns in config.passive_spawns
            )
    global _WARMED
    with _LOCK:
        _CONFIGS.clear()
        _CONFIGS.update(configs)
        _WARMED = True
    return {
        "special_condition_declared_dungeons": declared_dungeons,
        "special_condition_configs": len(configs),
        "special_condition_targets": target_count,
        "special_condition_candidates": candidate_count,
        "special_condition_passive_spawns": passive_spawn_count,
    }


def clear_special_condition_index() -> None:
    global _WARMED
    with _LOCK:
        _CONFIGS.clear()
        _WARMED = False


def get_boss_entrance_condition_config(
    dungeon_id: int,
    maze_index: int,
) -> BossEntranceConditionConfig | None:
    with _LOCK:
        if not _WARMED:
            return None
        return _CONFIGS.get((int(dungeon_id), int(maze_index)))


def _next_assignment_roll(state: int) -> int:
    return (int(state) * 1103515245 + 12345) & 0xFFFFFFFF


def initialize_run_special_conditions(
    session: Any,
    *,
    dungeon_id: int,
    maze_index: int,
    seed: int,
) -> str:
    if session is None:
        return "special_conditions=skip_no_session"
    config = get_boss_entrance_condition_config(dungeon_id, maze_index)
    if config is None:
        session.dungeon_boss_condition_config_key = None
        session.dungeon_boss_condition_assignments = []
        session.dungeon_boss_condition_complete = False
        session.dungeon_boss_condition_gate_pending = False
        return "special_conditions=none"

    assignments: list[dict[str, Any]] = []
    available = list(config.candidates)
    state = (
        int(seed)
        ^ (int(dungeon_id) * 0x9E3779B1)
        ^ (int(maze_index) * 0x85EBCA6B)
    ) & 0xFFFFFFFF
    next_assignment_id = 0
    direct_codes_seen: set[int] = set()
    for monster_code in config.target_codes:
        monster_code = int(monster_code)
        supported = [
            candidate
            for candidate in available
            if int(monster_code) in candidate.condition_codes
        ]
        if not supported:
            supported = [
                candidate
                for candidate in config.candidates
                if int(monster_code) in candidate.condition_codes
            ]
        if supported:
            state = _next_assignment_roll(state)
            candidate = supported[state % len(supported)]
            if candidate in available:
                available.remove(candidate)
            assignments.append(
                {
                    "assignment_id": int(next_assignment_id),
                    "monster_code": int(monster_code),
                    "cell_x": int(candidate.cell_x) & 0xFF,
                    "cell_y": int(candidate.cell_y) & 0xFF,
                    "map_ids": tuple(candidate.map_ids),
                    "actor_source": "monster_condition",
                    "show_minimap": True,
                    "killed": False,
                }
            )
            next_assignment_id += 1
            continue

        # Fixed entrance targets are already in normal MAP actor rows.  They
        # are selected only from unambiguous PVF cells and retain declaration
        # order, so one condition assignment maps to one visible actor.
        if monster_code in direct_codes_seen:
            continue
        direct_codes_seen.add(monster_code)
        for candidate in config.candidates:
            if len(candidate.map_ids) != 1:
                continue
            occurrence_count = sum(
                1
                for candidate_code in candidate.map_monster_codes
                if int(candidate_code) == monster_code
            )
            for source_occurrence in range(occurrence_count):
                assignments.append(
                    {
                        "assignment_id": int(next_assignment_id),
                        "monster_code": int(monster_code),
                        "cell_x": int(candidate.cell_x) & 0xFF,
                        "cell_y": int(candidate.cell_y) & 0xFF,
                        "map_ids": tuple(candidate.map_ids),
                        "actor_source": "map_monster",
                        "source_occurrence": int(source_occurrence),
                        # Fixed Dungeon minimap icons come from PVF.  001C
                        # and 022F overlays are reserved for random targets.
                        "show_minimap": False,
                        "killed": False,
                    }
                )
                next_assignment_id += 1

    session.dungeon_boss_condition_config_key = (
        int(dungeon_id),
        int(maze_index),
    )
    session.dungeon_boss_condition_assignments = assignments
    session.dungeon_boss_condition_complete = False
    session.dungeon_boss_condition_gate_pending = False
    detail = ",".join(
        "%d@%d,%d"
        % (
            int(row["monster_code"]),
            int(row["cell_x"]),
            int(row["cell_y"]),
        )
        for row in assignments
    )
    return "special_conditions=%d [%s]" % (len(assignments), detail)


def _session_config(session: Any) -> BossEntranceConditionConfig | None:
    key = getattr(session, "dungeon_boss_condition_config_key", None)
    if not isinstance(key, (tuple, list)) or len(key) < 2:
        return None
    return get_boss_entrance_condition_config(int(key[0]), int(key[1]))


def special_condition_minimap_pair_groups(
    session: Any,
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return the run-assigned target rooms in the Stage3 001C group shape."""
    if _session_config(session) is None:
        return ()
    points: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for assignment in list(
        getattr(session, "dungeon_boss_condition_assignments", None) or []
    ):
        if not bool(assignment.get("show_minimap", True)):
            continue
        if "cell_x" not in assignment or "cell_y" not in assignment:
            continue
        point = (
            int(assignment["cell_x"]) & 0xFF,
            int(assignment["cell_y"]) & 0xFF,
        )
        if point in seen:
            continue
        seen.add(point)
        points.append(point)
    return (tuple(points),) if points else ()


def special_condition_minimap_entries(
    session: Any,
) -> tuple[tuple[int, int, int], ...]:
    """Return Stage3 022F room/icon keys for this run's condition actors."""
    if _session_config(session) is None:
        return ()
    entries: list[tuple[int, int, int]] = []
    for assignment in list(
        getattr(session, "dungeon_boss_condition_assignments", None) or []
    ):
        if not bool(assignment.get("show_minimap", True)):
            continue
        if (
            "cell_x" not in assignment
            or "cell_y" not in assignment
            or int(assignment.get("monster_code") or 0) <= 0
        ):
            continue
        entries.append(
            (
                int(assignment["cell_x"]) & 0xFF,
                int(assignment["cell_y"]) & 0xFF,
                int(assignment["monster_code"]) & 0xFFFFFFFF,
            )
        )
    return tuple(entries)


def append_room_condition_actors(
    session: Any,
    monsters: Sequence[dict[str, Any]],
    *,
    cell_x: int,
    cell_y: int,
    map_index: int,
    dungeon_basic_lv: int,
    first_seq: int,
) -> tuple[list[dict[str, Any]], str]:
    rows = list(monsters or [])
    config = _session_config(session)
    assignments = list(
        getattr(session, "dungeon_boss_condition_assignments", None) or []
    )
    if config is None or not assignments:
        return rows, "condition_actors=none"

    added = 0
    missing: list[int] = []
    for assignment in assignments:
        if bool(assignment.get("killed")):
            continue
        if (
            int(assignment.get("cell_x") or 0) != (int(cell_x) & 0xFF)
            or int(assignment.get("cell_y") or 0) != (int(cell_y) & 0xFF)
        ):
            continue
        monster_code = int(assignment.get("monster_code") or 0)
        actor_source = str(
            assignment.get("actor_source") or "monster_condition"
        ).strip().lower()
        if actor_source == "map_monster":
            source_rows = [
                row
                for row in rows
                if str(row.get("kind") or "").strip().lower() == "monster"
                and int(row.get("code") or 0) == monster_code
            ]
            source_occurrence = int(assignment.get("source_occurrence") or 0)
            if source_occurrence < 0 or source_occurrence >= len(source_rows):
                missing.append(monster_code)
                continue
            actor = source_rows[source_occurrence]
            actor["special_condition_assignment_id"] = int(
                assignment.get("assignment_id") or 0
            )
            actor["special_condition_target"] = True
            added += 1
            continue
        templates = _map_actors.load_map_condition_monsters(
            int(map_index),
            [monster_code],
            dungeon_basic_lv=int(dungeon_basic_lv),
            first_seq=int(first_seq) + len(rows),
        )
        if not templates:
            missing.append(monster_code)
            continue
        actor = dict(templates[0])
        actor["special_condition_assignment_id"] = int(
            assignment.get("assignment_id") or 0
        )
        actor["special_condition_target"] = True
        rows.append(actor)
        added += 1
    note = "condition_actors=%d" % added
    if missing:
        note += " missing=%s" % ",".join(str(code) for code in missing)
    return rows, note


def bind_room_condition_passives(
    session: Any,
    monsters: Sequence[dict[str, Any]],
    *,
    map_index: int,
) -> str:
    """Register action-created objects without adding them to 001D.

    These passives belong to the DGN boss-entrance mechanism and therefore
    exist independently of quest acceptance.  The established type-3 quest
    residual path still decides whether destroying one advances a quest.
    """
    config = _session_config(session)
    if session is None or config is None:
        return "condition_passives=none"
    rows = list(getattr(session, "dungeon_room_passive_objects", None) or [])
    existing = {
        (
            int(row.get("code") or 0),
            int(row.get("x") or 0),
            int(row.get("y") or 0),
        )
        for row in rows
    }
    added = 0
    for actor in list(monsters or []):
        if not bool(actor.get("special_condition_target")):
            continue
        monster_code = int(actor.get("code") or 0)
        for spawn_index, spawn in enumerate(config.spawns_for(monster_code)):
            pos_x = int(actor.get("x") or 0) + int(spawn.offset_x)
            pos_y = int(actor.get("y") or 0) + int(spawn.offset_y)
            key = (int(spawn.object_code), pos_x, pos_y)
            if key in existing:
                continue
            existing.add(key)
            rows.append(
                {
                    "kind": "passive_object",
                    "passive_source_kind": "action_created",
                    "code": int(spawn.object_code),
                    "x": pos_x,
                    "y": pos_y,
                    "z": int(actor.get("z") or 0) + int(spawn.offset_z),
                    "flags": 0,
                    "type": 9,
                    "quest_enemy_type": 3,
                    "faction": "neutral",
                    "role": "neutral",
                    "is_hostile": False,
                    "is_ally": False,
                    "is_blocking": False,
                    "grants_exp": False,
                    "killable": True,
                    "packet_index": int(
                        actor.get("special_condition_assignment_id") or 0
                    )
                    * 16
                    + spawn_index,
                    "slot_index": spawn_index,
                    "template_order": 0,
                    "level": 0,
                    "runtime_map_index": int(map_index) & 0xFFFFFFFF,
                    "action_owner_code": monster_code,
                    "killed": False,
                    "wire_visible": False,
                }
            )
            added += 1
    if rows:
        room_state = load_chain("helper-dungeon_room_state.py")
        rows = room_state.filter_destroyed_objects(
            session,
            rows,
            map_index=int(map_index),
        )
    session.dungeon_room_passive_objects = rows
    return "condition_passives=%d" % added


def observe_condition_actor_killed(
    session: Any,
    actor: dict[str, Any] | None,
) -> str:
    if session is None or not actor or not bool(actor.get("special_condition_target")):
        return "boss_condition=skip"
    assignment_id = int(actor.get("special_condition_assignment_id") or 0)
    assignments = list(
        getattr(session, "dungeon_boss_condition_assignments", None) or []
    )
    matched = False
    for assignment in assignments:
        if int(assignment.get("assignment_id") or 0) != assignment_id:
            continue
        if not bool(assignment.get("killed")):
            assignment["killed"] = True
            matched = True
        break
    killed = sum(1 for assignment in assignments if bool(assignment.get("killed")))
    config = _session_config(session)
    required = (
        config.required_assignment_count()
        if config is not None
        else len(assignments)
    )
    complete = bool(required) and len(assignments) >= required and killed >= required
    was_complete = bool(getattr(session, "dungeon_boss_condition_complete", False))
    session.dungeon_boss_condition_complete = complete
    if complete and not was_complete:
        session.dungeon_boss_condition_gate_pending = True
    if not matched:
        return "boss_condition=duplicate %d/%d" % (killed, len(assignments))
    return "boss_condition=%d/%d complete=%d" % (
        killed,
        len(assignments),
        int(complete),
    )


def consume_complete_condition_pass_gate(session: Any) -> bool:
    """Consume the one-shot Stage3 gate notification for this run."""
    if session is None or not bool(
        getattr(session, "dungeon_boss_condition_gate_pending", False)
    ):
        return False
    session.dungeon_boss_condition_gate_pending = False
    return True
