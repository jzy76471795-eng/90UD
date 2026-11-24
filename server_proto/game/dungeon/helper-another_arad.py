"""Another Arad (Crack of Dimension) selection and level matching."""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from game.chain_loader import load_chain
from game.quest.quest_index import get_quest_index
from pvf import pvf_runtime


ANOTHER_ARAD_WRAPPER_DUNGEON_ID = 8000
_WRAPPER_PATH = "dungeon/crackofdimension/crackofdimension.dgn"
_CRACK_QUEST_PREFIX = "n_quest/crackofdimension/"
_INT_TOKEN = re.compile(r"-?\d+")
_MAP_COORD = re.compile(r"\((\d+)\s*,\s*(\d+)\)")
_LEGACY_MAX_DIFFICULTY = 4
_DUNGEON_MAP_PARENT_ALIASES = {
    "dungeon/act5/theboywithcoldheart.dgn": ("map/coldboy/_",),
}
_PASSIVE_KINDS = {
    "special_passive",
    "passive_object",
    "passive",
    "map_passive",
}

_ENDPOINT_LOCK = threading.RLock()
_ENDPOINT_READY = False
_ENDPOINTS: dict[tuple[int, int, str, int, int], "AnotherAradEndpoint"] = {}
_START_CELLS: set[tuple[int, int, int, int]] = set()
_BOSS_CELLS: set[tuple[int, int, int, int]] = set()
_ROOM_CELLS: set[tuple[int, int, int, int]] = set()


@dataclass(frozen=True)
class AnotherAradSelection:
    wrapper_dungeon_id: int
    historical_dungeon_id: int
    crack_quest_id: int


@dataclass(frozen=True)
class AnotherAradEndpoint:
    map_index: int
    path: str
    source: str


@dataclass(frozen=True)
class _MapCandidate:
    map_index: int
    path: str
    filename: str
    map_type: str
    dungeon_ids: frozenset[int]
    greed_codes: frozenset[str]
    path_cells: frozenset[tuple[int, int]]


def _section_text(text: str, name: str) -> str:
    match = re.search(
        r"(?is)\[" + re.escape(name) + r"\](.*?)(?=\n\s*\[|\Z)",
        str(text or ""),
    )
    return match.group(1) if match else ""


def _int_values(text: str) -> list[int]:
    return [int(value) for value in _INT_TOKEN.findall(str(text or ""))]


def _greed_rows(text: str) -> list[str]:
    rows: list[str] = []
    for raw_line in _section_text(text, "greed").replace("`", "").splitlines():
        row = "".join(str(raw_line).split()).lower()
        if row:
            rows.append(row)
    return rows


def _maze_cell_greed(block: str) -> dict[tuple[int, int], str]:
    size = _int_values(_section_text(block, "size"))
    if len(size) < 2:
        return {}
    width = max(0, int(size[0]))
    height = max(0, int(size[1]))
    out: dict[tuple[int, int], str] = {}
    for y, row in enumerate(_greed_rows(block)[:height]):
        for x in range(min(width, len(row) // 2)):
            out[(x, y)] = row[x * 2 : x * 2 + 2]
    return out


def _maze_blocks(text: str) -> list[str]:
    blocks = re.findall(r"(?is)\[maze\](.*?)\[/maze\]", str(text or ""))
    if blocks:
        return blocks
    parts = re.split(r"(?is)\[maze\s*info\]", str(text or ""))
    blocks = [part for part in parts[1:] if str(part).strip()]
    return blocks or [str(text or "")]


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _parent_key(path: str) -> str:
    normalized = _normalize_path(path).lower()
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else normalized
    parts = [part for part in parent.split("/") if part]
    if parts and parts[-1].endswith("_r"):
        parts[-1] = parts[-1][:-2]
    return "/".join(parts)


def _dungeon_map_parent(dgn_path: str) -> str:
    filename = _normalize_path(dgn_path).rsplit("/", 1)[-1]
    if "." in filename:
        filename = filename.rsplit(".", 1)[0]
    return _parent_key("map/%s/_" % filename)


def _map_type(text: str) -> str:
    body = _section_text(text, "type")
    tag = re.search(r"\[([^\]]+)\]", body)
    return str(tag.group(1) if tag else body).replace("`", "").strip().lower()


def _map_greed_codes(text: str) -> frozenset[str]:
    values: set[str] = set()
    for row in _greed_rows(text):
        for offset in range(0, len(row) - 1, 2):
            values.add(row[offset : offset + 2])
    return frozenset(values)


def _candidate_map(
    map_index: int,
    path: str,
) -> Optional[_MapCandidate]:
    normalized = _normalize_path(path)
    if not normalized:
        return None
    try:
        text = pvf_runtime.read_text(normalized)
    except Exception:
        return None
    return _MapCandidate(
        map_index=int(map_index),
        path=normalized,
        filename=normalized.rsplit("/", 1)[-1].lower(),
        map_type=_map_type(text),
        dungeon_ids=frozenset(_int_values(_section_text(text, "dungeon"))),
        greed_codes=_map_greed_codes(text),
        path_cells=frozenset(
            (int(left), int(right))
            for left, right in _MAP_COORD.findall(normalized)
        ),
    )


def _select_endpoint(
    candidates: Iterable[_MapCandidate],
    *,
    role: str,
    cell: tuple[int, int],
    greed_code: str,
    fallback_map_index: int,
    excluded_map_indices: Iterable[int] = (),
    require_greed_match: bool = False,
) -> Optional[AnotherAradEndpoint]:
    candidates = tuple(candidates)
    excluded = {int(value) for value in excluded_map_indices if int(value) > 0}
    has_compatible_room = any(
        greed_code in candidate.greed_codes
        and candidate.map_index not in excluded
        and candidate.map_type != "boss" and "boss" not in candidate.filename
        for candidate in candidates
    ) if require_greed_match and role == "room" and greed_code and greed_code != "aa" else False
    ranked: list[tuple[int, int, _MapCandidate, str]] = []
    for candidate in candidates:
        coordinate_match = cell in candidate.path_cells
        greed_match = bool(
            greed_code
            and greed_code != "aa"
            and greed_code in candidate.greed_codes
        )
        is_boss = candidate.map_type == "boss" or "boss" in candidate.filename
        is_start = candidate.map_type == "start" or "start" in candidate.filename
        if role == "boss" and not is_boss:
            continue
        if role == "start" and is_boss:
            continue
        if role == "room" and (
            is_boss or int(candidate.map_index) in excluded
        ):
            continue
        if (
            role == "start"
            and fallback_map_index > 0
            and candidate.map_index != fallback_map_index
            and not is_start
        ):
            continue
        if not coordinate_match and not greed_match and candidate.map_index != fallback_map_index:
            continue
        # A filename coordinate is only a hint. An implicit ordinary room
        # must support the DGN's authored door topology. WhiteLand's two
        # Normal MAP filenames disagree with their [greed] declarations;
        # choosing them by coordinate suppresses the required exits.
        # Explicit [map specification] remains the authoritative override.
        if (has_compatible_room
                and candidate.greed_codes and not greed_match
                and candidate.map_index != fallback_map_index):
            continue

        if role == "boss":
            role_match = is_boss
        elif role == "start":
            role_match = is_start
        else:
            role_match = not is_boss
        score = 0
        score += 1000 if coordinate_match else 0
        score += 500 if greed_match else 0
        score += 400 if role_match else 0
        score += 50 if candidate.map_index == fallback_map_index else 0
        if "hero" in candidate.filename:
            score -= 120
        if "quest" in candidate.filename:
            score -= 80
        if "hell" in candidate.filename or "season" in candidate.filename:
            score -= 500
        sources = []
        if coordinate_match:
            sources.append("path_cell")
        if greed_match:
            sources.append("greed")
        if role_match:
            sources.append("map_role")
        if candidate.map_index == fallback_map_index:
            sources.append("dgn_spec")
        ranked.append(
            (
                score,
                -candidate.map_index,
                candidate,
                "+".join(sources) or "fallback",
            )
        )
    if not ranked:
        return None
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    _score, _negative_id, selected, source = ranked[0]
    return AnotherAradEndpoint(
        map_index=int(selected.map_index),
        path=selected.path,
        source=source,
    )


def _crack_dungeon_ids() -> set[int]:
    index = get_quest_index()
    index.ensure_all_metas_loaded()
    dungeon_ids: set[int] = set()
    for quest_id in list(getattr(index, "sorted_quest_ids", None) or []):
        meta = index.get_meta(int(quest_id))
        if meta is None:
            continue
        path = _normalize_path(getattr(meta, "path", "")).lower()
        if not path.startswith(_CRACK_QUEST_PREFIX):
            continue
        values = _int_values(str(getattr(meta, "int_data", "") or ""))
        if values and int(values[0]) > 0:
            dungeon_ids.add(int(values[0]))
    return dungeon_ids


def warmup_another_arad_index() -> dict[str, int]:
    """Build native room endpoint mappings for Crack history dungeons."""
    global _ENDPOINT_READY
    map_helper = load_chain("helper-dungeon_map_monsters.py")
    start_room = load_chain("helper-dungeon_start_room.py")
    index = get_quest_index()
    map_paths = dict(map_helper._map_lst())  # already warmed by dungeon_map_index
    map_ids_by_parent: dict[str, list[int]] = defaultdict(list)
    for map_index, path in map_paths.items():
        map_ids_by_parent[_parent_key(path)].append(int(map_index))

    candidate_cache: dict[int, Optional[_MapCandidate]] = {}
    endpoints: dict[tuple[int, int, str, int, int], AnotherAradEndpoint] = {}
    start_cells: set[tuple[int, int, int, int]] = set()
    boss_cells: set[tuple[int, int, int, int]] = set()
    room_cells: set[tuple[int, int, int, int]] = set()
    dungeon_count = 0
    maze_count = 0
    candidate_count = 0
    room_candidate_count = 0

    for dungeon_id in sorted(_crack_dungeon_ids()):
        dungeon = index.get_dungeon(int(dungeon_id))
        if not str(getattr(dungeon, "path", "") or "") or not list(
            getattr(dungeon, "mazes", None) or []
        ):
            continue
        try:
            dgn_text = pvf_runtime.read_text(str(dungeon.path))
        except Exception:
            continue
        blocks = _maze_blocks(dgn_text)
        dungeon_count += 1
        for maze_index in range(len(dungeon.mazes)):
            maze = start_room._load_maze_details(int(dungeon_id), int(maze_index))
            if maze is None:
                continue
            maze_count += 1
            block = blocks[maze_index] if maze_index < len(blocks) else blocks[0]
            cell_greed = _maze_cell_greed(block)
            exact_maps: dict[tuple[int, int], int] = {}
            exact_map_candidates: dict[tuple[int, int], set[int]] = defaultdict(set)
            parent_keys = {_dungeon_map_parent(str(dungeon.path))}
            normalized_dgn_path = _normalize_path(str(dungeon.path)).lower()
            parent_keys.update(
                _parent_key(path)
                for path in _DUNGEON_MAP_PARENT_ALIASES.get(
                    normalized_dgn_path,
                    (),
                )
            )
            for spec in list(getattr(maze, "map_specs", None) or []):
                cell = (int(spec.get("x", -1)), int(spec.get("y", -1)))
                raw_map_indices = spec.get("candidates") or (spec.get("map_id"),)
                map_indices: list[int] = []
                for raw_map_index in raw_map_indices:
                    try:
                        map_index_value = int(raw_map_index or 0)
                    except (TypeError, ValueError):
                        continue
                    if map_index_value <= 0:
                        continue
                    map_indices.append(map_index_value)
                    exact_map_candidates[cell].add(map_index_value)
                    parent_keys.add(
                        _parent_key(map_helper.resolve_map_path(map_index_value))
                    )
                if map_indices:
                    exact_maps[cell] = map_indices[0]
            candidate_ids = sorted(
                {
                    map_index_value
                    for parent in parent_keys
                    for map_index_value in map_ids_by_parent.get(parent, [])
                }
            )
            candidates: list[_MapCandidate] = []
            for map_index_value in candidate_ids:
                if map_index_value not in candidate_cache:
                    candidate_cache[map_index_value] = _candidate_map(
                        map_index_value,
                        map_paths.get(map_index_value, ""),
                    )
                candidate = candidate_cache[map_index_value]
                if candidate is None or int(dungeon_id) not in candidate.dungeon_ids:
                    continue
                candidates.append(candidate)
            candidate_count += len(candidates)

            endpoint_cells: set[tuple[int, int]] = set()
            reserved_endpoint_maps: set[int] = set()
            for role, cells, cell_store in (
                ("start", list(getattr(maze, "start_maps", None) or []), start_cells),
                ("boss", list(getattr(maze, "boss_maps", None) or []), boss_cells),
            ):
                for raw_x, raw_y in cells:
                    cell = (int(raw_x), int(raw_y))
                    endpoint_cells.add(cell)
                    cell_store.add(
                        (int(dungeon_id), int(maze_index), cell[0], cell[1])
                    )
                    endpoint = _select_endpoint(
                        candidates,
                        role=role,
                        cell=cell,
                        greed_code=cell_greed.get(cell, ""),
                        fallback_map_index=int(exact_maps.get(cell, 0)),
                    )
                    if endpoint is not None:
                        reserved_endpoint_maps.add(int(endpoint.map_index))
                        endpoints[
                            (
                                int(dungeon_id),
                                int(maze_index),
                                role,
                                cell[0],
                                cell[1],
                            )
                        ] = endpoint

            for cell in endpoint_cells:
                reserved_endpoint_maps.update(exact_map_candidates.get(cell, ()))

            # Early Crack-era DGN files often omit ordinary cells from
            # [map specification]. Resolve those cells from the same authored
            # greed code and MAP ownership relation used by the confirmed
            # native Start/Boss path. This index is consumed only while the
            # session is explicitly inside Another Arad.
            for cell, greed_code in sorted(cell_greed.items()):
                if cell in endpoint_cells or not greed_code or greed_code == "aa":
                    continue
                room_candidate_count += 1
                endpoint = _select_endpoint(
                    candidates,
                    role="room",
                    cell=cell,
                    greed_code=greed_code,
                    fallback_map_index=int(exact_maps.get(cell, 0)),
                    excluded_map_indices=reserved_endpoint_maps,
                    require_greed_match=normalized_dgn_path == "dungeon/act5/whiteland.dgn",
                )
                if endpoint is None:
                    continue
                room_cells.add(
                    (int(dungeon_id), int(maze_index), cell[0], cell[1])
                )
                endpoints[
                    (
                        int(dungeon_id),
                        int(maze_index),
                        "room",
                        cell[0],
                        cell[1],
                    )
                ] = endpoint

    # Some one-room Crack dungeons intentionally use one boss MAP for both
    # entry and settlement. Preserve that shared endpoint for the start role.
    for dungeon_id, maze_index, cell_x, cell_y in start_cells.intersection(
        boss_cells
    ):
        start_key = (dungeon_id, maze_index, "start", cell_x, cell_y)
        boss_key = (dungeon_id, maze_index, "boss", cell_x, cell_y)
        if start_key in endpoints or boss_key not in endpoints:
            continue
        boss_endpoint = endpoints[boss_key]
        endpoints[start_key] = AnotherAradEndpoint(
            map_index=boss_endpoint.map_index,
            path=boss_endpoint.path,
            source=boss_endpoint.source + "+shared_start_boss",
        )

    with _ENDPOINT_LOCK:
        _ENDPOINTS.clear()
        _ENDPOINTS.update(endpoints)
        _START_CELLS.clear()
        _START_CELLS.update(start_cells)
        _BOSS_CELLS.clear()
        _BOSS_CELLS.update(boss_cells)
        _ROOM_CELLS.clear()
        _ROOM_CELLS.update(room_cells)
        _ENDPOINT_READY = True
    result = {
        "crack_dungeon_count": dungeon_count,
        "crack_maze_count": maze_count,
        "crack_endpoint_count": sum(
            1 for key in endpoints if key[2] in {"start", "boss"}
        ),
        "crack_start_cell_count": len(start_cells),
        "crack_boss_cell_count": len(boss_cells),
        "crack_room_candidate_cell_count": room_candidate_count,
        "crack_room_endpoint_count": len(room_cells),
        "crack_room_unresolved_count": room_candidate_count - len(room_cells),
        "crack_all_endpoint_count": len(endpoints),
        "crack_candidate_count": candidate_count,
    }
    # Warm the selected mission/reward domain beside the endpoint index.  The
    # runtime handler then consumes only these shared, PVF-validated records.
    mission = load_chain("helper-another_arad_quest.py")
    result.update(mission.warmup_another_arad_quest_index())
    return result


def _ensure_endpoint_index() -> None:
    if _ENDPOINT_READY:
        return
    with _ENDPOINT_LOCK:
        if not _ENDPOINT_READY:
            warmup_another_arad_index()


def maximum_difficulty(dungeon_id: int) -> int:
    dungeon = get_quest_index().get_dungeon(int(dungeon_id))
    highest = dungeon.permission_clear_state()
    if highest is None:
        highest = _LEGACY_MAX_DIFFICULTY
    return max(0, min(_LEGACY_MAX_DIFFICULTY, int(highest)))


def correct_another_arad_start_room(
    selection: AnotherAradSelection,
    start: dict[str, Any],
) -> Tuple[dict[str, Any], str]:
    _ensure_endpoint_index()
    row = dict(start or {})
    dungeon_id = int(selection.historical_dungeon_id)
    maze_index = int(row.get("maze_index") or 0)
    cell_x = int(row.get("start_x") or 0)
    cell_y = int(row.get("start_y") or 0)
    endpoint = _ENDPOINTS.get(
        (dungeon_id, maze_index, "start", cell_x, cell_y)
    )
    if endpoint is None:
        return row, "start_endpoint=fallback map=%d" % int(row.get("map_index") or 0)
    row["map_index"] = int(endpoint.map_index)
    row["map_source"] = "another_arad_native_start:%s" % endpoint.source
    return row, "start_endpoint=%d source=%s" % (
        endpoint.map_index,
        endpoint.source,
    )


def correct_session_another_arad_room(
    session: Any,
    room: dict[str, Any],
) -> Tuple[dict[str, Any], str]:
    row = dict(room or {})
    if session is None or not bool(getattr(session, "another_arad_active", False)):
        return row, ""
    _ensure_endpoint_index()
    dungeon_id = int(row.get("dungeon_id") or 0)
    historical_id = int(
        getattr(session, "another_arad_historical_dungeon_id", 0) or 0
    )
    if dungeon_id <= 0 or dungeon_id != historical_id:
        return row, "room_endpoint=skip_dungeon_mismatch"
    maze_index = int(row.get("maze_index") or 0)
    cell_x = int(row.get("cell_x") or 0)
    cell_y = int(row.get("cell_y") or 0)
    cell_key = (dungeon_id, maze_index, cell_x, cell_y)
    role = ""
    if cell_key in _BOSS_CELLS:
        role = "boss"
        row["is_boss_cell"] = True
    elif cell_key in _START_CELLS:
        role = "start"
    elif cell_key in _ROOM_CELLS:
        role = "room"
    if not role:
        return row, ""
    endpoint = _ENDPOINTS.get(
        (dungeon_id, maze_index, role, cell_x, cell_y)
    )
    if endpoint is None:
        return row, "%s_endpoint=fallback map=%d" % (
            role,
            int(row.get("map_index") or 0),
        )
    row["map_index"] = int(endpoint.map_index)
    return row, "%s_endpoint=%d source=%s" % (
        role,
        endpoint.map_index,
        endpoint.source,
    )


def is_session_another_arad_boss_endpoint(
    session: Any,
    *,
    cell_x: int,
    cell_y: int,
    map_index: int,
) -> bool:
    """Match the current room against the corrected Crack BOSS endpoint."""
    if session is None or not bool(getattr(session, "another_arad_active", False)):
        return False
    _ensure_endpoint_index()
    dungeon_id = int(
        getattr(session, "another_arad_historical_dungeon_id", 0) or 0
    )
    selected_dungeon_id = int(
        getattr(session, "selected_dungeon_id", 0)
        or getattr(session, "cur_dungeon_id", 0)
        or 0
    )
    if dungeon_id <= 0 or selected_dungeon_id != dungeon_id:
        return False
    maze_index = int(getattr(session, "dungeon_maze_index", 0) or 0)
    cell = (int(cell_x) & 0xFF, int(cell_y) & 0xFF)
    if (dungeon_id, maze_index, cell[0], cell[1]) not in _BOSS_CELLS:
        return False
    endpoint = _ENDPOINTS.get(
        (dungeon_id, maze_index, "boss", cell[0], cell[1])
    )
    return endpoint is not None and int(endpoint.map_index) == (
        int(map_index) & 0xFFFFFFFF
    )


def validate_another_arad_selection(
    historical_dungeon_id: int,
    crack_quest_id: int,
) -> Tuple[Optional[AnotherAradSelection], str]:
    """Validate the client-selected Stage3 ``(dungeon, crack quest)`` pair."""
    dungeon_id = int(historical_dungeon_id)
    quest_id = int(crack_quest_id)
    if dungeon_id <= 0 or dungeon_id > 0xFFFF:
        return None, "historical_dungeon_out_of_range"
    if dungeon_id == ANOTHER_ARAD_WRAPPER_DUNGEON_ID:
        return None, "historical_dungeon_is_wrapper"
    if quest_id <= 0 or quest_id > 0xFFFF:
        return None, "crack_quest_out_of_range"

    index = get_quest_index()
    wrapper = index.get_dungeon(ANOTHER_ARAD_WRAPPER_DUNGEON_ID)
    wrapper_path = str(getattr(wrapper, "path", "") or "").replace("\\", "/").lower()
    if wrapper_path != _WRAPPER_PATH or not list(getattr(wrapper, "mazes", None) or []):
        return None, "wrapper_pvf_missing"

    meta = index.get_meta(quest_id)
    if meta is None:
        return None, "crack_quest_missing"
    quest_path = str(getattr(meta, "path", "") or "").replace("\\", "/").lower()
    if not quest_path.startswith(_CRACK_QUEST_PREFIX):
        return None, "quest_not_crack_of_dimension"
    int_values = [int(value) for value in _INT_TOKEN.findall(str(meta.int_data or ""))]
    if not int_values or int_values[0] != dungeon_id:
        return None, "crack_pair_mismatch"

    dungeon = index.get_dungeon(dungeon_id)
    if not str(getattr(dungeon, "path", "") or ""):
        return None, "historical_dungeon_missing"
    if not list(getattr(dungeon, "mazes", None) or []):
        return None, "historical_dungeon_has_no_maze"

    return (
        AnotherAradSelection(
            wrapper_dungeon_id=ANOTHER_ARAD_WRAPPER_DUNGEON_ID,
            historical_dungeon_id=dungeon_id,
            crack_quest_id=quest_id,
        ),
        "ok",
    )


def matched_monster_level(character_level: int) -> int:
    return max(1, min(255, int(character_level or 0) + 3))


def apply_another_arad_monster_level(
    monsters: Iterable[dict[str, Any]],
    target_level: int,
) -> Tuple[list[dict[str, Any]], int]:
    """Set hostile monster/APC levels while preserving allies and map objects."""
    level = max(1, min(255, int(target_level)))
    rows: list[dict[str, Any]] = []
    changed = 0
    for source in list(monsters or []):
        row = dict(source)
        kind = str(row.get("kind") or "monster").strip().lower()
        actor_type = int(row.get("type") or row.get("mon_type") or 0) & 0xFF
        is_passive = kind in _PASSIVE_KINDS or actor_type == 9
        if bool(row.get("is_hostile", False)) and not is_passive:
            row["level"] = level
            changed += 1
        rows.append(row)
    return rows, changed


def clear_another_arad_session(session: Any) -> None:
    if session is None:
        return
    session.another_arad_active = False
    session.another_arad_wrapper_dungeon_id = 0
    session.another_arad_historical_dungeon_id = 0
    session.another_arad_crack_quest_id = 0
    session.another_arad_monster_level = 0
    try:
        load_chain("helper-another_arad_quest.py").clear_another_arad_quest_state(
            session
        )
    except Exception:
        session.another_arad_quest_state = None


def bind_another_arad_session(
    session: Any,
    selection: AnotherAradSelection,
    monster_level: int,
) -> None:
    if session is None:
        return
    session.another_arad_active = True
    session.another_arad_wrapper_dungeon_id = int(selection.wrapper_dungeon_id)
    session.another_arad_historical_dungeon_id = int(selection.historical_dungeon_id)
    session.another_arad_crack_quest_id = int(selection.crack_quest_id)
    session.another_arad_monster_level = max(1, min(255, int(monster_level)))
    try:
        session.another_arad_quest_bind_note = (
            load_chain("helper-another_arad_quest.py").bind_another_arad_quest_state(
                session,
                quest_id=int(selection.crack_quest_id),
                historical_dungeon_id=int(selection.historical_dungeon_id),
            )
        )
    except Exception as exc:
        session.another_arad_quest_state = None
        session.another_arad_quest_bind_note = "mission_bind_error=%s" % type(exc).__name__


def apply_session_another_arad_level(
    session: Any,
    monsters: Iterable[dict[str, Any]],
) -> Tuple[list[dict[str, Any]], int, int]:
    rows = list(monsters or [])
    if session is None or not bool(getattr(session, "another_arad_active", False)):
        return rows, 0, 0
    level = int(getattr(session, "another_arad_monster_level", 0) or 0)
    if level <= 0:
        return rows, 0, 0
    adjusted, changed = apply_another_arad_monster_level(rows, level)
    return adjusted, level, changed
