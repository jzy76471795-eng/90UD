"""PVF-driven dungeon maze clear-condition state.

The selected maze owns one run-wide AND-set of ``[clear condition]`` rows.
Monster/APC/BOSS conditions advance from server-owned C1/0027 actor deaths by
PVF code; they do not depend on the actor's wire rank.  This matters for maps
whose target is emitted as an ordinary type-0 monster.

Stage3 owns passive objects embedded in ``.map`` files.  They are not
START_MAP actors, but their destruction is reported through C1/0027 with the
passive-object code as the local key.  ``[destroy object]`` therefore keeps a
server-only room shadow so the death handler can validate ownership without
duplicating the object on the wire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from game.chain_loader import load_chain
from game.quest.quest_index import MazeClearCondition, get_quest_index


_map_mons = load_chain("helper-dungeon_map_monsters.py")
_room_state = load_chain("helper-dungeon_room_state.py")
load_map_passive_objects = _map_mons.load_map_passive_objects
is_passive_actor = _map_mons.is_passive_actor
is_object_destroyed = _room_state.is_object_destroyed

DESTROY_OBJECT = "destroy object"
HUNT_MONSTER = "hunt monster"
HUNT_APC = "hunt apc"
HUNT_BOSS = "hunt boss"
_SUPPORTED_DEATH_KINDS = frozenset(
    {DESTROY_OBJECT, HUNT_MONSTER, HUNT_APC, HUNT_BOSS}
)
_SHADOW_KIND = "clear_condition_passive_shadow"


@dataclass
class DestroyObjectClearState:
    """Compatibility name for the run-wide maze clear-condition state."""

    dungeon_id: int
    maze_index: int
    map_index: int
    conditions: tuple[MazeClearCondition, ...]
    progress: list[int] = field(default_factory=list)
    bound: bool = False
    boss_map_ids: tuple[int, ...] = ()
    destroy_bound: bool = False

    def __post_init__(self) -> None:
        if len(self.progress) != len(self.conditions):
            self.progress = [0 for _condition in self.conditions]

    @property
    def total_required(self) -> int:
        return sum(max(0, int(condition.count)) for condition in self.conditions)

    @property
    def current_progress(self) -> int:
        return sum(max(0, int(value)) for value in self.progress)

    @property
    def complete(self) -> bool:
        return bool(self.conditions) and all(
            int(self.progress[index]) >= int(condition.count)
            for index, condition in enumerate(self.conditions)
        )

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(_condition_kind(condition) for condition in self.conditions)
        )

    def has_kind(self, kind: str) -> bool:
        expected = _normalize_kind(kind)
        return any(
            _condition_kind(condition) == expected for condition in self.conditions
        )

    def remaining_by_target(self, kind: str = DESTROY_OBJECT) -> dict[int, int]:
        expected = _normalize_kind(kind)
        remaining: dict[int, int] = {}
        for index, condition in enumerate(self.conditions):
            if _condition_kind(condition) != expected:
                continue
            count = max(0, int(condition.count) - int(self.progress[index]))
            target_id = int(condition.target_id)
            remaining[target_id] = remaining.get(target_id, 0) + count
        return remaining

    def observe(self, kind: str, target_id: int) -> int:
        expected = _normalize_kind(kind)
        target = int(target_id)
        advanced = 0
        for index, condition in enumerate(self.conditions):
            if _condition_kind(condition) != expected:
                continue
            if int(condition.target_id) != target:
                continue
            if int(self.progress[index]) >= int(condition.count):
                continue
            self.progress[index] += 1
            advanced += 1
        return advanced


def _normalize_kind(kind: str) -> str:
    return str(kind or "").strip().lower()


def _condition_kind(condition: MazeClearCondition) -> str:
    return _normalize_kind(getattr(condition, "kind", ""))


def wire_actor_count(mons: Iterable[dict] | None) -> int:
    return sum(
        1
        for mon in list(mons or [])
        if bool(mon.get("wire_visible", True))
    )


def _maze_clear_conditions(
    dungeon_id: int,
    maze_index: int,
) -> tuple[tuple[MazeClearCondition, ...], tuple[int, ...], str]:
    try:
        dungeon = get_quest_index().get_dungeon(int(dungeon_id))
    except Exception as exc:
        return (), (), "index_error=%r" % (exc,)
    mazes = list(getattr(dungeon, "mazes", None) or [])
    index = int(maze_index)
    if index < 0 or index >= len(mazes):
        return (), (), "maze_out_of_range=%d/%d" % (index, len(mazes))
    maze = mazes[index]
    conditions = tuple(
        condition
        for condition in tuple(getattr(maze, "clear_conditions", None) or ())
        if _condition_kind(condition)
        and int(getattr(condition, "target_id", 0)) > 0
        and int(getattr(condition, "count", 0)) > 0
    )
    if not conditions:
        return (), (), "none"
    unsupported = sorted(
        {
            _condition_kind(condition)
            for condition in conditions
            if _condition_kind(condition) not in _SUPPORTED_DEATH_KINDS
        }
    )
    if unsupported:
        return (), (), "unsupported=%s" % ",".join(unsupported)

    boss_cells = {
        (int(cell[0]), int(cell[1]))
        for cell in list(getattr(maze, "boss_maps", None) or [])
        if len(cell) >= 2
    }
    boss_map_ids: set[int] = set()
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            map_id = int(spec.get("map_id") or 0)
            cell = (int(spec.get("x") or 0), int(spec.get("y") or 0))
        except (TypeError, ValueError):
            continue
        if map_id <= 0:
            continue
        if str(spec.get("type") or "").strip().lower() == "boss" or cell in boss_cells:
            boss_map_ids.add(map_id)
    if any(_condition_kind(condition) == DESTROY_OBJECT for condition in conditions) and not boss_map_ids:
        return (), (), "destroy_object_no_boss_map"
    kinds = tuple(dict.fromkeys(_condition_kind(condition) for condition in conditions))
    return conditions, tuple(sorted(boss_map_ids)), "+".join(kinds)


def _ensure_state(
    session: Any,
    *,
    dungeon_id: int,
    maze_index: int,
    map_index: int,
) -> tuple[DestroyObjectClearState | None, str]:
    if session is None:
        return None, "no_session"
    dungeon = int(dungeon_id)
    maze = int(maze_index)
    conditions, boss_map_ids, reason = _maze_clear_conditions(dungeon, maze)
    if not conditions:
        return None, reason

    existing = getattr(session, "dungeon_clear_condition_state", None)
    if (
        isinstance(existing, DestroyObjectClearState)
        and int(existing.dungeon_id) == dungeon
        and int(existing.maze_index) == maze
        and tuple(existing.conditions) == tuple(conditions)
    ):
        return existing, "reuse"
    state = DestroyObjectClearState(
        dungeon_id=dungeon,
        maze_index=maze,
        map_index=0,
        conditions=conditions,
        boss_map_ids=tuple(boss_map_ids),
    )
    # The maze state is authoritative immediately so kill/APC/BOSS progress is
    # not lost before a later passive-object room is reached.  The passive
    # branch has its own readiness bit and controls settlement only after its
    # templates have been proven below.
    state.bound = True
    state.destroy_bound = not state.has_kind(DESTROY_OBJECT)
    try:
        session.dungeon_clear_condition_state = state
    except Exception:
        return None, "state_store_failed"
    return state, "init"


def _row_code(row: dict) -> int:
    try:
        return int(row.get("code") or row.get("monster_code") or 0)
    except (TypeError, ValueError):
        return 0


def _row_xy(row: dict) -> tuple[int, int]:
    try:
        x = int(row.get("x") or 0)
    except (TypeError, ValueError):
        x = 0
    try:
        y = int(row.get("y") or 0)
    except (TypeError, ValueError):
        y = 0
    return x, y


def _row_identity(row: dict) -> tuple[int, int, int]:
    x, y = _row_xy(row)
    return _row_code(row), x, y


def _decorate_condition_object(
    row: dict,
    *,
    dungeon_id: int,
    maze_index: int,
    map_index: int,
) -> dict:
    row["clear_condition_object"] = True
    row["clear_condition_dungeon_id"] = int(dungeon_id)
    row["clear_condition_maze_index"] = int(maze_index)
    row["clear_condition_map_index"] = int(map_index)
    row["is_blocking"] = True
    return row


def _build_shadow(
    template: dict,
    *,
    dungeon_id: int,
    maze_index: int,
    map_index: int,
) -> dict:
    code = _row_code(template)
    x, y = _row_xy(template)
    local_key = int(code) & 0xFFFF
    packet_index = int(
        template.get("packet_index")
        if template.get("packet_index") is not None
        else template.get("slot_index") or 0
    )
    row = {
        "kind": _SHADOW_KIND,
        "template_order": 0,
        "slot_index": packet_index,
        "packet_index": packet_index,
        "seq": local_key,
        "runtime_object_key": local_key,
        "code": int(code),
        "level": 0,
        "type": 9,
        "aux": 0,
        "aux2": 0,
        "flag0": 0,
        "flag1": 0,
        "extra_state": 0,
        "extra_flag": 0,
        "x": int(x),
        "y": int(y),
        "role": "neutral",
        "faction": "neutral",
        "is_hostile": False,
        "is_ally": False,
        "is_blocking": True,
        "grants_exp": False,
        "killable": True,
        "killed": False,
        "quest_enemy_type": 3,
        "wire_visible": False,
        "passive_source_kind": str(template.get("kind") or ""),
        "runtime_map_index": int(map_index),
    }
    return _decorate_condition_object(
        row,
        dungeon_id=dungeon_id,
        maze_index=maze_index,
        map_index=map_index,
    )


def attach_destroy_object_clear_shadows(
    session: Any,
    mons: Iterable[dict] | None,
    *,
    dungeon_id: int,
    maze_index: int,
    map_index: int,
) -> tuple[list[dict], str]:
    """Bind the maze state and any passive shadows owned by this room."""
    out = [dict(mon) for mon in list(mons or [])]
    current_map = int(map_index)
    state, state_note = _ensure_state(
        session,
        dungeon_id=int(dungeon_id),
        maze_index=int(maze_index),
        map_index=current_map,
    )
    if state is None:
        return out, "clear_conditions=%s" % state_note

    condition_kinds = "+".join(state.kinds)
    if not state.has_kind(DESTROY_OBJECT):
        return out, (
            "clear_conditions=%s state=%s progress=%d/%d"
            % (
                condition_kinds,
                state_note,
                state.current_progress,
                state.total_required,
            )
        )
    if state.complete:
        return out, "clear_conditions=%s complete progress=%d/%d" % (
            condition_kinds,
            state.current_progress,
            state.total_required,
        )

    if current_map not in set(int(value) for value in state.boss_map_ids):
        return out, (
            "clear_conditions=%s state=%s progress=%d/%d "
            "destroy_object_not_boss_map=%d"
            % (
                condition_kinds,
                state_note,
                state.current_progress,
                state.total_required,
                current_map,
            )
        )

    remaining = state.remaining_by_target(DESTROY_OBJECT)
    existing_candidates: dict[int, list[dict]] = {
        target: [] for target in remaining
    }
    template_candidates: dict[int, list[dict]] = {
        target: [] for target in remaining
    }
    existing_identities: set[tuple[int, int, int]] = set()
    for row in out:
        identity = _row_identity(row)
        existing_identities.add(identity)
        code = identity[0]
        if code not in remaining or bool(row.get("killed")):
            continue
        if not is_passive_actor(row):
            continue
        if (int(code) & 0xFFFF) == 0:
            continue

        existing_candidates[code].append(row)
    for template in load_map_passive_objects(current_map):
        code = _row_code(template)
        if code not in remaining:
            continue
        if (int(code) & 0xFFFF) == 0:
            continue
        identity = _row_identity(template)
        if identity in existing_identities:
            continue
        x, y = identity[1], identity[2]
        if is_object_destroyed(
            session,
            map_index=current_map,
            object_code=code,
            x=x,
            y=y,
        ):
            continue
        existing_identities.add(identity)
        template_candidates[code].append(template)

    insufficient = {
        target: int(remaining[target])
        - len(existing_candidates[target])
        - len(template_candidates[target])
        for target in remaining
        if len(existing_candidates[target]) + len(template_candidates[target])
        < int(remaining[target])
    }
    if insufficient:
        state.destroy_bound = False
        missing = ",".join(
            "%d:%d" % (target, insufficient[target])
            for target in sorted(insufficient)
        )
        return out, (
            "clear_conditions=destroy_object_unbound map=%d missing=%s"
            % (current_map, missing)
        )

    try:
        session.dungeon_clear_condition_state = state
    except Exception:
        return out, "clear_conditions=state_store_failed"
    state.destroy_bound = True
    state.map_index = current_map

    wire_bound = 0
    shadow_codes: list[int] = []
    for target in sorted(remaining):
        needed = int(remaining[target])
        for row in existing_candidates[target][:needed]:
            _decorate_condition_object(
                row,
                dungeon_id=int(dungeon_id),
                maze_index=int(maze_index),
                map_index=current_map,
            )
            wire_bound += int(bool(row.get("wire_visible", True)))
            needed -= 1
        for template in template_candidates[target][:needed]:
            out.append(
                _build_shadow(
                    template,
                    dungeon_id=int(dungeon_id),
                    maze_index=int(maze_index),
                    map_index=current_map,
                )
            )
            shadow_codes.append(target)

    targets = ",".join(
        "%d:%d" % (target, remaining[target]) for target in sorted(remaining)
    )
    shadows = ",".join(str(code) for code in shadow_codes) or "none"
    return out, (
        "clear_conditions=%s state=%s progress=%d/%d targets=%s "
        "wire_bound=%d shadows=%s"
        % (
            condition_kinds,
            state_note,
            state.current_progress,
            state.total_required,
            targets,
            wire_bound,
            shadows,
        )
    )


def _session_matches_state(
    session: Any,
    state: DestroyObjectClearState,
) -> tuple[bool, str]:
    if session is None or not bool(getattr(session, "in_dungeon_run", False)):
        return False, "not_in_run"
    dungeon_id = int(
        getattr(session, "cur_dungeon_id", 0)
        or getattr(session, "selected_dungeon_id", 0)
        or 0
    )
    raw_maze_index = getattr(session, "dungeon_maze_index", None)
    try:
        maze_index = int(raw_maze_index) if raw_maze_index is not None else -1
    except (TypeError, ValueError):
        maze_index = -1
    if dungeon_id != int(state.dungeon_id):
        return False, "dungeon_mismatch=%d/%d" % (dungeon_id, state.dungeon_id)
    if maze_index != int(state.maze_index):
        return False, "maze_mismatch=%d/%d" % (maze_index, state.maze_index)
    if not bool(state.bound):
        return False, "not_bound"
    return True, "owned"


def resolve_destroy_object_clear_shadow(
    session: Any,
    parsed: dict[str, Any],
) -> tuple[dict | None, str]:
    """Resolve a client-owned passive death only inside its exact run scope."""
    state = getattr(session, "dungeon_clear_condition_state", None)
    if not isinstance(state, DestroyObjectClearState):
        return None, "clear_shadow=skip_no_state"
    owned, reason = _session_matches_state(session, state)
    if not owned:
        return None, "clear_shadow=skip_%s" % reason
    if not state.has_kind(DESTROY_OBJECT):
        return None, "clear_shadow=skip_no_destroy_condition"
    if not bool(state.destroy_bound):
        return None, "clear_shadow=skip_destroy_not_bound"
    if int(parsed.get("raw_len") or 0) < 4:
        return None, "clear_shadow=skip_short_body"

    current_map = int(getattr(session, "dungeon_map_index", 0) or 0)
    if int(state.map_index) != current_map:
        return None, "clear_shadow=skip_map_mismatch=%d/%d" % (
            current_map,
            int(state.map_index),
        )
    key_candidates: set[int] = set()
    for name in ("runtime_object_key", "seq", "monster_code"):
        try:
            value = parsed.get(name)
            if value is not None and int(value) > 0:
                key_candidates.add(int(value) & 0xFFFF)
        except (TypeError, ValueError):
            continue
    if not key_candidates:
        return None, "clear_shadow=skip_no_key"

    for mon in list(getattr(session, "dungeon_room_mons", None) or []):
        if str(mon.get("kind") or "").lower() != _SHADOW_KIND:
            continue
        if bool(mon.get("killed")):
            continue
        if int(mon.get("clear_condition_dungeon_id") or -1) != int(state.dungeon_id):
            continue
        try:
            mon_maze_index = int(mon["clear_condition_maze_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if mon_maze_index != int(state.maze_index):
            continue
        if int(mon.get("clear_condition_map_index") or 0) != current_map:
            continue
        code = _row_code(mon)
        if (int(code) & 0xFFFF) not in key_candidates:
            continue
        return mon, (
            "clear_shadow=owned dungeon=%d maze=%d map=%d code=%d xy=(%d,%d)"
            % (
                state.dungeon_id,
                state.maze_index,
                current_map,
                code,
                _row_xy(mon)[0],
                _row_xy(mon)[1],
            )
        )
    return None, "clear_shadow=skip_not_bound map=%d keys=%s" % (
        current_map,
        ",".join(str(value) for value in sorted(key_candidates)),
    )


def _event_condition_kinds(mon: dict) -> tuple[str, ...]:
    kinds: list[str] = [HUNT_BOSS]
    try:
        enemy_type = int(mon.get("quest_enemy_type") or 0)
    except (TypeError, ValueError):
        enemy_type = 0
    actor_kind = str(mon.get("kind") or "").strip().lower()
    hostile = bool(mon.get("is_hostile", True))
    if hostile and (
        enemy_type == 1
        or actor_kind in {"monster", "mob"}
        and not is_passive_actor(mon)
    ):
        kinds.append(HUNT_MONSTER)
    if hostile and (
        enemy_type == 2 or actor_kind in {"ai_character", "ai", "apc"}
    ):
        kinds.append(HUNT_APC)
    if bool(mon.get("clear_condition_object")):
        kinds.append(DESTROY_OBJECT)
    return tuple(dict.fromkeys(kinds))


def observe_dungeon_clear_condition(
    session: Any,
    mon: dict,
) -> tuple[bool, bool, str]:
    """Advance every matching maze condition for one owned actor death."""
    state = getattr(session, "dungeon_clear_condition_state", None)
    if not isinstance(state, DestroyObjectClearState):
        return False, False, "clear_condition=skip_no_state"
    owned, reason = _session_matches_state(session, state)
    if not owned:
        return False, state.complete, "clear_condition=skip_%s" % reason
    if bool(mon.get("clear_condition_observed")):
        return False, state.complete, "clear_condition=duplicate"

    code = _row_code(mon)
    if code <= 0:
        return False, state.complete, "clear_condition=skip_no_code"

    event_kinds = _event_condition_kinds(mon)
    observations: list[tuple[str, int]] = []
    for kind in event_kinds:
        if kind == DESTROY_OBJECT:
            current_map = int(getattr(session, "dungeon_map_index", 0) or 0)
            try:
                mon_maze_index = int(mon["clear_condition_maze_index"])
            except (KeyError, TypeError, ValueError):
                mon_maze_index = -1
            if (
                int(mon.get("clear_condition_dungeon_id") or -1)
                != int(state.dungeon_id)
                or mon_maze_index != int(state.maze_index)
                or int(mon.get("clear_condition_map_index") or 0) != current_map
            ):
                continue
        progressed = state.observe(kind, code)
        if progressed > 0:
            observations.append((kind, progressed))
    if not observations:
        return False, state.complete, "clear_condition=skip_target code=%d" % code
    mon["clear_condition_observed"] = True
    details = " | ".join(
        "clear_condition=observe kind=%s target=%d increment=%d"
        % (kind, code, progressed)
        for kind, progressed in observations
    )
    return True, state.complete, "%s progress=%d/%d complete=%d" % (
        details,
        state.current_progress,
        state.total_required,
        int(state.complete),
    )


def observe_destroy_object_clear_condition(
    session: Any,
    mon: dict,
) -> tuple[bool, bool, str]:
    """Compatibility wrapper for the former passive-only observer."""
    if not bool(mon.get("clear_condition_object")):
        state = getattr(session, "dungeon_clear_condition_state", None)
        complete = bool(
            isinstance(state, DestroyObjectClearState) and state.complete
        )
        return False, complete, "clear_condition=skip_unbound_object"
    return observe_dungeon_clear_condition(session, mon)


def dungeon_clear_condition_status(session: Any) -> tuple[bool, bool, int, int]:
    state = getattr(session, "dungeon_clear_condition_state", None)
    if not isinstance(state, DestroyObjectClearState):
        return False, False, 0, 0
    owned, _reason = _session_matches_state(session, state)
    if not owned:
        return False, False, state.current_progress, state.total_required
    if state.has_kind(DESTROY_OBJECT) and not bool(state.destroy_bound):
        return False, False, state.current_progress, state.total_required
    return True, state.complete, state.current_progress, state.total_required


def dungeon_clear_condition_source(session: Any) -> str:
    state = getattr(session, "dungeon_clear_condition_state", None)
    if not isinstance(state, DestroyObjectClearState):
        return "room"
    if state.kinds == (DESTROY_OBJECT,):
        return "destroy_object"
    return "maze_condition"


def destroy_object_clear_status(session: Any) -> tuple[bool, bool, int, int]:
    state = getattr(session, "dungeon_clear_condition_state", None)
    if not isinstance(state, DestroyObjectClearState) or not state.has_kind(
        DESTROY_OBJECT
    ):
        return False, False, 0, 0
    owned, _reason = _session_matches_state(session, state)
    if not owned:
        return False, False, state.current_progress, state.total_required
    if not bool(state.destroy_bound):
        return False, False, state.current_progress, state.total_required
    current_map = int(getattr(session, "dungeon_map_index", 0) or 0)
    if int(state.map_index) != current_map:
        return False, False, state.current_progress, state.total_required
    return True, state.complete, state.current_progress, state.total_required
