"""Dungeon start-room resolver (副本实现A.md §4.5).

Uses shared PVF via quest_index / pvf_runtime only.
"""

from __future__ import annotations

import random
import re
from typing import Any, Mapping, Optional, Sequence

from game.quest.quest_index import MazeInfo, get_quest_index
from game.quest.quest_residual import hunt_kill_difficulty_matches, unpack_trigger_channels

_TOKEN_SPLIT = re.compile(r"[\s,]+")

_PUNGJIN_TRAINING_DUNGEON_ID = 5000
_PUNGJIN_TRAINING_DGN_PATH = "dungeon/poongjintrainingroom/poongjintrainingroom.dgn"
_PUNGJIN_TRAINING_START_MAP_ID = 36250
_PUNGJIN_TRAINING_START_MAP_PATH = "map/poongjintrainingroom/11100(0,0)start.map"


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def parse_map_specifications(raw: str) -> list[dict[str, Any]]:
    """Parse [map specification] tokens: type x y mapId [extraIds...]."""
    tokens = [_strip_tick(tok) for tok in _TOKEN_SPLIT.split(str(raw or "")) if tok.strip()]
    out: list[dict[str, Any]] = []
    i = 0
    while i + 3 < len(tokens):
        typ = tokens[i].lower()
        try:
            x = int(tokens[i + 1], 0)
            y = int(tokens[i + 2], 0)
            map_id = int(tokens[i + 3], 0)
        except ValueError:
            i += 1
            continue
        candidates = [map_id]
        j = i + 4
        while j < len(tokens):
            try:
                candidates.append(int(tokens[j], 0))
                j += 1
            except ValueError:
                break
        out.append(
            {
                "type": typ,
                "x": x,
                "y": y,
                "map_id": candidates[0],
                "candidates": candidates,
            }
        )
        i = j
    return out


def _section_text(block: str, key: str) -> str:
    pattern = re.compile(
        r"(?is)\[" + re.escape(key) + r"\](.*?)(?=\n\s*\[|\[/" + re.escape(key) + r"\]|\Z)"
    )
    m = pattern.search(block or "")
    return m.group(1).strip() if m else ""


def _parse_int_list(text: str) -> list[int]:
    out: list[int] = []
    for tok in _TOKEN_SPLIT.split(str(text or "")):
        tok = _strip_tick(tok)
        if not tok:
            continue
        try:
            out.append(int(tok, 0))
        except ValueError:
            continue
    return out


def _normalized_quest_type(value: str) -> str:
    text = _strip_tick(str(value or "")).strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text


def _map_spec_candidates(spec: Mapping[str, Any]) -> tuple[int, ...]:
    """Return one PVF candidate list without deduplicating its weights."""
    raw_ids = spec.get("candidates") or (spec.get("map_id"),)
    out: list[int] = []
    for raw_id in raw_ids:
        try:
            map_id = int(raw_id or 0)
        except (TypeError, ValueError):
            continue
        if map_id > 0:
            out.append(map_id & 0xFFFFFFFF)
    return tuple(out)


def _unfinished_quest_actor_target_groups(
    dungeon_id: int,
    difficulty: int,
    active_quest_pairs: Sequence[tuple[int, int]],
) -> tuple[tuple[int, ...], ...]:
    """Return unfinished per-task actor targets scoped to this dungeon run.

    Hunt targets retain their per-channel residual semantics.  Both PVF reward
    tables are material sources, so an unfinished task can also bind a BOSS
    MAP through a matching ``[monster reward item]`` or ``[enemy reward item]``
    row.  A code from the same task is counted only once.  Group order is the
    active-task order supplied by the dungeon entry flow.
    """
    target_groups: list[tuple[int, ...]] = []
    try:
        index = get_quest_index()
    except Exception:
        return ()
    for raw_pair in list(active_quest_pairs or ()):
        try:
            quest_id = int(raw_pair[0]) & 0xFFFF
            trigger = int(raw_pair[1]) & 0xFFFFFFFF
        except (IndexError, TypeError, ValueError):
            continue
        if quest_id <= 0 or trigger == 0:
            continue
        meta = index.get_meta(quest_id)
        if meta is None:
            continue
        targets: list[int] = []
        seen_codes: set[int] = set()
        if _normalized_quest_type(meta.quest_type) == "hunt monster":
            remaining = unpack_trigger_channels(trigger)
            values = _parse_int_list(meta.int_data)
            for channel, offset in enumerate(range(0, len(values) - 3, 4)):
                if channel >= len(remaining):
                    break
                if int(remaining[channel]) <= 0:
                    continue
                target_dungeon = int(values[offset])
                target_difficulty = int(values[offset + 1])
                target_code = int(values[offset + 2])
                if target_code <= 0 or target_code in seen_codes:
                    continue
                if target_dungeon != -1 and target_dungeon != int(dungeon_id):
                    continue
                if not hunt_kill_difficulty_matches(meta, target_difficulty, int(difficulty)):
                    continue
                seen_codes.add(target_code)
                targets.append(target_code)
        for entry in list(getattr(meta, "monster_rewards", None) or ()):
            try:
                target_code = int(entry.monster_code)
                target_dungeon = int(entry.dungeon_id)
                target_difficulty = int(entry.difficulty)
            except (AttributeError, TypeError, ValueError):
                continue
            if target_code <= 0 or target_code in seen_codes:
                continue
            if target_dungeon != -1 and target_dungeon != int(dungeon_id):
                continue
            if target_difficulty >= 0 and target_difficulty != int(difficulty):
                continue
            seen_codes.add(target_code)
            targets.append(target_code)
        for entry in list(getattr(meta, "enemy_rewards", None) or ()):
            try:
                target_code = int(entry.enemy_code)
                target_dungeon = int(entry.dungeon_id)
                target_difficulty = int(entry.difficulty)
            except (AttributeError, TypeError, ValueError):
                continue
            if target_code <= 0 or target_code in seen_codes:
                continue
            if target_dungeon != -1 and target_dungeon != int(dungeon_id):
                continue
            if target_difficulty >= 0 and target_difficulty != int(difficulty):
                continue
            seen_codes.add(target_code)
            targets.append(target_code)
        if targets:
            target_groups.append(tuple(targets))
    return tuple(target_groups)


def _unfinished_quest_actor_targets(
    dungeon_id: int,
    difficulty: int,
    active_quest_pairs: Sequence[tuple[int, int]],
) -> tuple[int, ...]:
    """Return the legacy flattened view of unfinished task actor targets."""
    return tuple(
        target_code
        for target_group in _unfinished_quest_actor_target_groups(
            dungeon_id,
            difficulty,
            active_quest_pairs,
        )
        for target_code in target_group
    )


def _choose_boss_map_variant(
    candidates: Sequence[int],
    *,
    quest_targets: Sequence[int],
    quest_target_groups: Sequence[Sequence[int]] = (),
) -> int:
    """Choose the first task group that actually matches a BOSS candidate."""
    pool = tuple(int(map_id) & 0xFFFFFFFF for map_id in candidates if int(map_id) > 0)
    if not pool:
        return 0
    groups: list[tuple[int, ...]] = []
    for raw_group in quest_target_groups:
        group: list[int] = []
        for raw_target in raw_group:
            try:
                target_code = int(raw_target)
            except (TypeError, ValueError):
                continue
            if target_code > 0:
                group.append(target_code)
        if group:
            groups.append(tuple(group))
    targets: list[int] = []
    for raw_target in quest_targets:
        try:
            target_code = int(raw_target)
        except (TypeError, ValueError):
            continue
        if target_code > 0:
            targets.append(target_code)
    target_groups = groups or ([tuple(targets)] if targets else [])
    if target_groups:
        try:
            from game.chain_loader import load_chain

            collect_actor_codes = load_chain(
                "helper-dungeon_map_monsters.py"
            ).collect_map_actor_codes
            actor_codes_by_map: dict[int, set[int]] = {}
            for map_id in pool:
                if map_id not in actor_codes_by_map:
                    actor_codes_by_map[map_id] = collect_actor_codes(map_id)
            for priority_targets in target_groups:
                scores = [
                    sum(
                        1
                        for target_code in priority_targets
                        if target_code in actor_codes_by_map[map_id]
                    )
                    for map_id in pool
                ]
                best_score = max(scores)
                if best_score > 0:
                    return int(pool[scores.index(best_score)])
        except Exception:
            pass
    return int(random.choice(pool))


def _build_boss_map_variant_plan_for_maze(
    dungeon_id: int,
    *,
    difficulty: int,
    maze: MazeInfo | None,
    active_quest_pairs: Sequence[tuple[int, int]],
) -> dict[tuple[str, int, int], int]:
    """Select the current maze's first BOSS endpoint once for this run."""
    if maze is None:
        return {}
    boss_maps = list(getattr(maze, "boss_maps", None) or [])
    if not boss_maps:
        return {}
    boss_x, boss_y = int(boss_maps[0][0]), int(boss_maps[0][1])
    quest_target_groups = _unfinished_quest_actor_target_groups(
        int(dungeon_id),
        int(difficulty),
        active_quest_pairs,
    )
    variants: dict[tuple[str, int, int], int] = {}
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            map_type = str(spec.get("type") or "").strip().lower()
            cell_x = int(spec.get("x", -1))
            cell_y = int(spec.get("y", -1))
        except (AttributeError, TypeError, ValueError):
            continue
        if map_type != "boss" or (cell_x, cell_y) != (boss_x, boss_y):
            continue
        candidates = _map_spec_candidates(spec)
        if len(candidates) > 1:
            variants[(map_type, cell_x, cell_y)] = _choose_boss_map_variant(
                candidates,
                quest_targets=(),
                quest_target_groups=quest_target_groups,
            )
    return variants


def build_dungeon_boss_map_variant_plan(
    dungeon_id: int,
    *,
    difficulty: int = 0,
    maze_index: int = 0,
    active_quest_pairs: Sequence[tuple[int, int]] = (),
) -> dict[tuple[str, int, int], int]:
    """Build a per-run BOSS MAP plan for one already-selected maze."""
    maze = _load_maze_details(int(dungeon_id), int(maze_index))
    return _build_boss_map_variant_plan_for_maze(
        int(dungeon_id),
        difficulty=int(difficulty),
        maze=maze,
        active_quest_pairs=active_quest_pairs,
    )


def boss_cells_for_maze(dungeon_id: int, maze_index: int) -> frozenset[tuple[int, int]]:
    """Return every PVF-declared BOSS cell, including alternate endpoints."""
    maze = _load_maze_details(int(dungeon_id), int(maze_index))
    if maze is None:
        return frozenset()
    return frozenset(
        (int(raw_x) & 0xFF, int(raw_y) & 0xFF)
        for raw_x, raw_y in list(getattr(maze, "boss_maps", None) or [])
    )


def boss_map_candidates_for_cell(
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    *,
    boss_map_variants: Mapping[tuple[str, int, int], int] | None = None,
) -> tuple[int, ...]:
    """Return the selected PVF BOSS MAP candidates for one concrete cell."""
    maze = _load_maze_details(int(dungeon_id), int(maze_index))
    if maze is None:
        return ()
    cell = (int(cell_x) & 0xFF, int(cell_y) & 0xFF)
    if cell not in boss_cells_for_maze(int(dungeon_id), int(maze_index)):
        return ()
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            if (
                str(spec.get("type") or "").strip().lower() != "boss"
                or (int(spec.get("x")), int(spec.get("y"))) != cell
            ):
                continue
        except Exception:
            continue
        raw = spec.get("candidates") or (spec.get("map_id"),)
        candidates = tuple(
            int(value) & 0xFFFFFFFF
            for value in raw
            if int(value or 0) > 0
        )
        planned = 0
        if boss_map_variants:
            try:
                planned = int(
                    boss_map_variants.get(("boss", cell[0], cell[1]), 0) or 0
                )
            except Exception:
                planned = 0
        if planned in candidates:
            return (planned,)
        return candidates
    return ()


def is_boss_endpoint_room(
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    map_index: int,
    *,
    boss_map_variants: Mapping[tuple[str, int, int], int] | None = None,
) -> bool:
    """Require both a PVF BOSS cell and its physical BOSS MAP."""
    candidates = boss_map_candidates_for_cell(
        int(dungeon_id),
        int(maze_index),
        int(cell_x),
        int(cell_y),
        boss_map_variants=boss_map_variants,
    )
    return int(map_index) & 0xFFFFFFFF in candidates


def _enrich_maze_from_block(maze: MazeInfo, block: str) -> MazeInfo:
    start_vals = _parse_int_list(_section_text(block, "start map"))
    starts: list[tuple[int, int]] = []
    for i in range(0, len(start_vals) - 1, 2):
        starts.append((int(start_vals[i]), int(start_vals[i + 1])))
    if starts:
        maze.start_maps = starts

    boss_vals = _parse_int_list(_section_text(block, "boss map"))
    bosses: list[tuple[int, int]] = []
    for i in range(0, len(boss_vals) - 1, 2):
        bosses.append((int(boss_vals[i]), int(boss_vals[i + 1])))
    maze.boss_maps = bosses

    maze.map_specs = parse_map_specifications(_section_text(block, "map specification"))
    return maze


def _load_maze_details(dungeon_id: int, maze_index: int) -> MazeInfo | None:
    idx = get_quest_index()
    info = idx.get_dungeon(int(dungeon_id))
    if not info.mazes:
        return None
    if maze_index < 0 or maze_index >= len(info.mazes):
        maze_index = 0
    maze = info.mazes[maze_index]
    if (getattr(maze, "map_specs", None) or getattr(maze, "boss_maps", None)) and maze.start_maps:
        return maze
    path = info.path
    if not path:
        return maze
    try:
        text = idx._read_text(path)
    except Exception:
        return maze
    blocks = re.findall(r"(?is)\[maze\](.*?)\[/maze\]", text)
    if not blocks:
        parts = re.split(r"(?is)\[maze\s*info\]", text)
        blocks = [p for p in parts[1:] if str(p).strip()]
    if not blocks:
        blocks = [text]
    block = blocks[maze_index] if maze_index < len(blocks) else blocks[0]
    block = re.split(r"(?is)\[maze\s*info\]|\[maze\]", block, maxsplit=1)[0]
    return _enrich_maze_from_block(maze, block)


def resolve_map_id_for_cell(
    maze: MazeInfo | None,
    cell_x: int,
    cell_y: int,
    *,
    exact_only: bool = False,
    prefer_boss: bool = False,
    boss_map_variants: Mapping[tuple[str, int, int], int] | None = None,
) -> int:
    """Resolve a cell, using its paired boss template only for the selected endpoint."""
    if maze is None:
        return 0
    specs = list(getattr(maze, "map_specs", None) or [])
    type_order = (
        ("boss", "map", "quest", None)
        if prefer_boss
        else ("map", "quest", None, "boss")
    )
    for prefer_type in type_order:
        for spec in specs:
            if int(spec.get("x", -1)) != int(cell_x) or int(spec.get("y", -1)) != int(cell_y):
                continue
            typ = str(spec.get("type") or "").lower()
            if prefer_type is None:
                if typ == "boss" and boss_map_variants:
                    planned = boss_map_variants.get(
                        ("boss", int(cell_x), int(cell_y))
                    )
                    try:
                        planned_map_id = int(planned or 0)
                    except (TypeError, ValueError):
                        planned_map_id = 0
                    if planned_map_id in _map_spec_candidates(spec):
                        return planned_map_id
                mid = int(spec.get("map_id") or 0)
                if mid > 0:
                    return mid
            elif typ == prefer_type:
                if typ == "boss" and boss_map_variants:
                    planned = boss_map_variants.get(
                        ("boss", int(cell_x), int(cell_y))
                    )
                    try:
                        planned_map_id = int(planned or 0)
                    except (TypeError, ValueError):
                        planned_map_id = 0
                    if planned_map_id in _map_spec_candidates(spec):
                        return planned_map_id
                mid = int(spec.get("map_id") or 0)
                if mid > 0:
                    return mid
    if exact_only:
        return 0
    for spec in specs:
        mid = int(spec.get("map_id") or 0)
        if mid > 0:
            return mid
    return 0


def layered_map_ids_for_cell(
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
) -> tuple[int, ...]:
    """Return the PVF layered-map sequence for one logical maze cell.

    The ordinary ``map``/``boss`` specification remains the cell's initial
    map.  A sibling ``layered`` specification is a sequence selected only
    after the client asks to advance the local layered scene.
    """
    maze = _load_maze_details(int(dungeon_id), int(maze_index))
    if maze is None:
        return ()
    target_x = int(cell_x) & 0xFF
    target_y = int(cell_y) & 0xFF
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            if (
                str(spec.get("type") or "").strip().lower() != "layered"
                or int(spec.get("x", -1)) != target_x
                or int(spec.get("y", -1)) != target_y
            ):
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        raw_ids = spec.get("candidates") or (spec.get("map_id"),)
        map_ids: list[int] = []
        for raw_id in raw_ids:
            try:
                map_id = int(raw_id or 0)
            except (TypeError, ValueError):
                continue
            if map_id > 0:
                map_ids.append(map_id & 0xFFFFFFFF)
        if map_ids:
            return tuple(map_ids)
    return ()


_LAYERED_MAP_CELL_ATTR = "dungeon_layered_map_cell"
_LAYERED_MAP_INDEX_ATTR = "dungeon_layered_map_index"


def reset_layered_map_transition(session: Any) -> None:
    """Clear the current PVF layered-scene cursor for this dungeon run."""
    if session is None:
        return
    try:
        setattr(session, _LAYERED_MAP_CELL_ATTR, None)
        setattr(session, _LAYERED_MAP_INDEX_ATTR, -1)
    except Exception:
        pass


def advance_layered_map_transition(
    session: Any,
    *,
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    request_flag: int,
) -> tuple[Optional[int], str]:
    """Advance one PVF ``layered`` map after a Stage3 MOVE_MAP request.

    Stage3 C1/002D requests observed in the Cartel Command landing sequence
    carry ``1`` at body offset 10.  The flag only selects an already-declared
    DGN layered map; it never manufactures a task- or map-specific route.

    A positive result is the next physical MAP. ``0`` means this request is
    not a declared layered transition and the normal maze route should run.
    ``None`` means a valid layered plan was exhausted; callers must leave the
    current physical MAP intact instead of falling back to the cell's base
    MAP.
    """
    try:
        flag = int(request_flag or 0) & 0xFF
    except (TypeError, ValueError):
        flag = 0
    if flag != 1:
        reset_layered_map_transition(session)
        return 0, "layered=skip request=%d" % flag
    if session is None:
        return 0, "layered=skip no_session"

    map_ids = layered_map_ids_for_cell(
        int(dungeon_id),
        int(maze_index),
        int(cell_x),
        int(cell_y),
    )
    if not map_ids:
        reset_layered_map_transition(session)
        return 0, "layered=skip no_plan cell=(%d,%d)" % (
            int(cell_x) & 0xFF,
            int(cell_y) & 0xFF,
        )

    key = (
        int(dungeon_id) & 0xFFFFFFFF,
        int(maze_index),
        int(cell_x) & 0xFF,
        int(cell_y) & 0xFF,
    )
    raw_key = getattr(session, _LAYERED_MAP_CELL_ATTR, None)
    try:
        previous_key = tuple(raw_key) if isinstance(raw_key, (tuple, list)) else ()
    except TypeError:
        previous_key = ()
    try:
        raw_index = getattr(session, _LAYERED_MAP_INDEX_ATTR, -1)
        previous_index = int(-1 if raw_index is None else raw_index)
    except (TypeError, ValueError):
        previous_index = -1
    if previous_key != key:
        previous_index = -1
    next_index = previous_index + 1
    if next_index >= len(map_ids):
        return None, "layered=exhausted cell=(%d,%d) index=%d count=%d" % (
            key[2],
            key[3],
            previous_index,
            len(map_ids),
        )

    selected_map_id = int(map_ids[next_index])
    try:
        setattr(session, _LAYERED_MAP_CELL_ATTR, key)
        setattr(session, _LAYERED_MAP_INDEX_ATTR, next_index)
    except Exception as exc:
        return 0, "layered=state_store_error type=%s" % type(exc).__name__
    return selected_map_id, "layered=advance cell=(%d,%d) index=%d/%d map=%d" % (
        key[2],
        key[3],
        next_index,
        len(map_ids),
        selected_map_id,
    )


def is_active_layered_map_transition(
    session: Any,
    *,
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    map_index: int,
) -> bool:
    """Return whether ``map_index`` is the selected physical DGN layer.

    Layered maps share a maze cell with its ordinary topology map.  They keep
    their own actor cache, but must not complete that logical maze cell or
    advance normal clear-map quests.
    """
    if session is None or int(map_index or 0) <= 0:
        return False
    key = (
        int(dungeon_id) & 0xFFFFFFFF,
        int(maze_index),
        int(cell_x) & 0xFF,
        int(cell_y) & 0xFF,
    )
    raw_key = getattr(session, _LAYERED_MAP_CELL_ATTR, None)
    try:
        active_key = tuple(raw_key) if isinstance(raw_key, (tuple, list)) else ()
    except TypeError:
        active_key = ()
    if active_key != key:
        return False
    try:
        raw_index = getattr(session, _LAYERED_MAP_INDEX_ATTR, -1)
        active_index = int(-1 if raw_index is None else raw_index)
    except (TypeError, ValueError):
        return False
    map_ids = layered_map_ids_for_cell(
        int(dungeon_id),
        int(maze_index),
        int(cell_x),
        int(cell_y),
    )
    return (
        0 <= active_index < len(map_ids)
        and int(map_ids[active_index]) == (int(map_index) & 0xFFFFFFFF)
    )


def maze_room_cells(maze: MazeInfo | None) -> tuple[tuple[int, int], ...]:
    """Return every concrete cell in one PVF maze.

    Start and boss cells are included independently because older quest mazes
    can omit either endpoint from ``[map specification]``.
    """
    if maze is None:
        return ()
    cells: set[tuple[int, int]] = set()
    for raw_x, raw_y in list(getattr(maze, "start_maps", None) or []):
        cells.add((int(raw_x), int(raw_y)))
    for raw_x, raw_y in list(getattr(maze, "boss_maps", None) or []):
        cells.add((int(raw_x), int(raw_y)))
    for spec in list(getattr(maze, "map_specs", None) or []):
        try:
            if int(spec.get("map_id") or 0) <= 0:
                continue
            cells.add((int(spec.get("x")), int(spec.get("y"))))
        except Exception:
            continue
    return tuple(sorted(cells))


def _resolve_tower_map_id_for_cell(
    dungeon_id: int,
    dungeon_path: str,
    maze: MazeInfo | None,
    cell_x: int,
    cell_y: int,
) -> int:
    if maze is None:
        return 0
    cell = (int(cell_x), int(cell_y))
    start_cells = {
        (int(raw_x), int(raw_y))
        for raw_x, raw_y in list(getattr(maze, "start_maps", None) or [])
    }
    boss_cells = {
        (int(raw_x), int(raw_y))
        for raw_x, raw_y in list(getattr(maze, "boss_maps", None) or [])
    }
    if cell not in start_cells and cell not in boss_cells:
        return 0
    excluded_map_ids = [
        int(spec.get("map_id") or 0)
        for spec in list(getattr(maze, "map_specs", None) or [])
        if int(spec.get("map_id") or 0) > 0
    ]
    try:
        from game.chain_loader import load_chain

        map_index_helper = load_chain("helper-dungeon_map_monsters.py")
        return int(
            map_index_helper.resolve_tower_dungeon_cell_map(
                int(dungeon_id),
                dungeon_path,
                is_start_cell=cell in start_cells,
                is_boss_cell=cell in boss_cells,
                excluded_map_ids=excluded_map_ids,
            )
            or 0
        )
    except Exception:
        return 0


def _resolve_pungjin_training_start_map_id(
    dungeon_id: int,
    dungeon_path: str,
    cell_x: int,
    cell_y: int,
) -> int:
    """Resolve the lone PVF map omitted by the 5000 one-cell maze.

    The frozen PVF's ``PoongjinTrainingRoom.dgn`` names the (0,0) maze cell
    but has no ``[map specification]``. Its ``map/map.lst`` maps id 36250 to
    the matching start script. Keep the exception bound to both PVF paths and
    the declared cell so other incomplete dungeon data still rejects map 0.
    """
    if int(dungeon_id) != _PUNGJIN_TRAINING_DUNGEON_ID:
        return 0
    if int(cell_x) != 0 or int(cell_y) != 0:
        return 0
    normalized_path = str(dungeon_path or "").replace("\\", "/").strip("/").lower()
    if normalized_path != _PUNGJIN_TRAINING_DGN_PATH:
        return 0
    try:
        from game.chain_loader import load_chain

        map_index = load_chain("helper-dungeon_map_monsters.py")
        resolved_path = map_index.resolve_map_path(_PUNGJIN_TRAINING_START_MAP_ID)
        normalized_map_path = str(resolved_path or "").replace("\\", "/").strip("/").lower()
        if normalized_map_path != _PUNGJIN_TRAINING_START_MAP_PATH:
            return 0
        return _PUNGJIN_TRAINING_START_MAP_ID
    except Exception:
        return 0


def _is_special_hell_dungeon(dgn: Any, maze: Any) -> bool:
    """Recognize explicit particular-Hell dungeons and legacy Hell mazes.

    Metrocenter's particular-Hell DGN carries its escape-Hell marker in
    the last maze only. Its first selectable maze must use the same final
    boss reward boundary. Ordinary optional Hell Party rooms stay separate.
    """
    if dgn is None:
        return False
    dungeon_type = str(getattr(dgn, "dungeon_type", "") or "").strip().lower()
    if dungeon_type == "particular hell":
        return bool(getattr(dgn, "has_escape_hell", False))
    return bool(
        getattr(dgn, "hell_dungeon", False)
        and getattr(maze, "escape_hell", False)
    )


def resolve_dungeon_start_room(
    dungeon_id: int,
    *,
    difficulty: int = 0,
    active_quest_ids: Sequence[int] = (),
    finished_quest_ids: Sequence[int] = (),
    active_quest_pairs: Sequence[tuple[int, int]] = (),
    boss_map_variants: Mapping[tuple[str, int, int], int] | None = None,
) -> dict[str, Any]:
    """Resolve start cell / boss cell / map_index for SELECT grant."""
    idx = get_quest_index()
    maze_index, maze, reason = idx.select_dungeon_maze(
        int(dungeon_id),
        difficulty=int(difficulty),
        active_quest_ids=active_quest_ids,
        finished_quest_ids=finished_quest_ids,
    )
    from game.chain_loader import load_chain

    license_boss = load_chain("helper-dungeon_license_boss.py")
    license_boss_selection = license_boss.select_license_dungeon_boss_maze(
        int(dungeon_id),
        int(maze_index if maze_index >= 0 else 0),
        selection_reason=str(reason or ""),
    )
    maze_index = int(license_boss_selection.maze_index)
    maze = _load_maze_details(int(dungeon_id), int(maze_index if maze_index >= 0 else 0))
    if boss_map_variants is None:
        selected_boss_map_variants = _build_boss_map_variant_plan_for_maze(
            int(dungeon_id),
            difficulty=int(difficulty),
            maze=maze,
            active_quest_pairs=active_quest_pairs,
        )
    else:
        selected_boss_map_variants = dict(boss_map_variants)
    start_x = 0
    start_y = 0
    if maze is not None and maze.start_maps:
        start_x, start_y = int(maze.start_maps[0][0]), int(maze.start_maps[0][1])
    boss_x = 0
    boss_y = 0
    if maze is not None and getattr(maze, "boss_maps", None):
        boss_x, boss_y = int(maze.boss_maps[0][0]), int(maze.boss_maps[0][1])
    dgn = idx.get_dungeon(int(dungeon_id))
    tournament_spec = None
    try:
        from game.chain_loader import load_chain

        tournament = load_chain("helper-dungeon_tournament.py")
        tournament_spec = tournament.tournament_spec_for_dungeon(int(dungeon_id))
    except Exception:
        tournament_spec = None
    qc = list(maze.quest_connection) if maze and maze.quest_connection else None
    start_is_boss = (start_x, start_y) == (boss_x, boss_y)
    map_index = resolve_map_id_for_cell(
        maze,
        start_x,
        start_y,
        exact_only=True,
        prefer_boss=start_is_boss,
        boss_map_variants=selected_boss_map_variants,
    )
    map_source = "maze_map_specification" if map_index > 0 else ""
    if map_index <= 0:
        try:
            from game.chain_loader import load_chain

            map_index_helper = load_chain("helper-dungeon_map_monsters.py")
            connected_quests: list[int] = []
            if qc and len(qc) >= 2 and int(qc[0]) == 0:
                connected_quests.append(int(qc[1]))
            else:
                connected_quests.extend(int(q) for q in active_quest_ids or ())
            map_index = map_index_helper.resolve_quest_companion_start_map(
                int(dungeon_id),
                connected_quests,
            )
        except Exception:
            map_index = 0
        if map_index > 0:
            map_source = "quest_companion_native_map"
    if map_index <= 0:
        try:
            from game.chain_loader import load_chain

            map_index_helper = load_chain("helper-dungeon_map_monsters.py")
            map_index = map_index_helper.resolve_omitted_start_map(
                int(dungeon_id),
                int(maze_index if maze_index >= 0 else 0),
                start_x,
                start_y,
            )
        except Exception:
            map_index = 0
        if map_index > 0:
            map_source = "map_native_start_area"
    tournament_reverse_start = False
    if map_index <= 0 and tournament_spec is not None:
        try:
            from game.chain_loader import load_chain

            tournament = load_chain("helper-dungeon_tournament.py")
            map_index = tournament.resolve_tournament_map_for_cell(
                int(dungeon_id),
                start_x,
                start_y,
            )
        except Exception:
            map_index = 0
        if map_index > 0:
            map_source = "tournament_map_dungeon_reverse"
            tournament_reverse_start = True
    tower_reverse_start = False
    if map_index <= 0:
        map_index = _resolve_tower_map_id_for_cell(
            int(dungeon_id),
            str(getattr(dgn, "path", "") or ""),
            maze,
            start_x,
            start_y,
        )
        if map_index > 0:
            map_source = "tower_map_dungeon_reverse"
            tower_reverse_start = True
    if map_index <= 0:
        map_index = resolve_map_id_for_cell(
            maze,
            start_x,
            start_y,
            prefer_boss=start_is_boss,
            boss_map_variants=selected_boss_map_variants,
        )
        if map_index > 0:
            map_source = "legacy_first_map_specification"
    if map_index <= 0:
        map_index = _resolve_pungjin_training_start_map_id(
            int(dungeon_id),
            str(getattr(dgn, "path", "") or ""),
            start_x,
            start_y,
        )
        if map_index > 0:
            map_source = "pungjin_training_map_lst"
    base_w = float(getattr(dgn, "experience_increasing_point", 1.0) or 1.0)
    story = getattr(dgn, "story_mode", None)
    try:
        from game.chain_loader import load_chain

        _story = load_chain("helper-dungeon_story_mode.py")
        story_active = _story.is_story_run(
            maze_index=int(maze_index if maze_index >= 0 else 0),
            quest_connection=qc,
            reason=str(reason or ""),
        )
        story_mult = _story.resolve_story_exp_multiplier(story, story_active=story_active)
        combined_w = _story.combine_experience_weight(
            base_w, story, story_active=story_active
        )
    except Exception:
        story_active = False
        story_mult = 1.0
        combined_w = base_w
    return {
        "dungeon_id": int(dungeon_id),
        "difficulty": int(difficulty) & 0xFF,
        "maze_index": int(maze_index if maze_index >= 0 else 0),
        "reason": (
            "tower_map_dungeon_reverse"
            if tower_reverse_start
            else "tournament_map_dungeon_reverse"
            if tournament_reverse_start
            else reason
        ),
        "start_x": int(start_x) & 0xFF,
        "start_y": int(start_y) & 0xFF,
        "boss_x": int(boss_x) & 0xFF,
        "boss_y": int(boss_y) & 0xFF,
        "map_index": int(map_index) & 0xFFFFFFFF,
        "map_source": map_source,
        "boss_map_variants": selected_boss_map_variants,
        "license_boss_present": bool(license_boss_selection.boss_present),
        "license_boss_monster_code": int(
            license_boss_selection.boss_monster_code
        ),
        "license_boss_license_level": int(license_boss_selection.license_level),
        "license_boss_selection_source": str(license_boss_selection.source),
        "room_cells": maze_room_cells(maze),
        "open_door_even_enemy": bool(
            getattr(maze, "open_door_even_enemy", False)
        ),
        "basis_level": int(getattr(dgn, "basis_level", 0) or 0),
        "minimum_required_level": int(getattr(dgn, "minimum_required_level", 0) or 0),
        "experience_increasing_point": base_w,
        "story_mode_active": bool(story_active),
        "story_exp_multiplier": float(story_mult),
        "experience_weight": float(combined_w),
        "maze_name": getattr(maze, "name", "") if maze else "",
        "quest_connection": qc,
        "path": getattr(dgn, "path", ""),
        "is_tournament_dungeon": bool(tournament_spec is not None),
        "tournament_round_count": int(
            getattr(tournament_spec, "round_count", 0) or 0
        ),
        "tournament_start_area": tuple(
            getattr(tournament_spec, "start_area", ()) or ()
        ),
        "tournament_round_fatigue": int(
            getattr(tournament_spec, "round_fatigue", 0) or 0
        ),
        "tournament_enter_without_fatigue": bool(
            getattr(tournament_spec, "enter_without_fatigue", False)
        ),
        "is_special_hell_dungeon": _is_special_hell_dungeon(dgn, maze),
    }


def compute_dungeon_seed(dungeon_id: int, character_id: int) -> int:
    """s4a12-style seed from 副本实现A.md §4.2."""
    seed = (0x12345678 ^ ((int(dungeon_id) & 0xFFFFFFFF) << 8) ^ (int(character_id) & 0xFFFFFFFF)) & 0xFFFFFFFF
    seed &= ~0x40000
    return seed & 0xFFFFFFFF


def compute_room_seed(
    dungeon_id: int,
    cell_x: int,
    cell_y: int,
    character_id: int,
) -> int:
    """MOVE_MAP room seed from 副本实现A.md §5.4."""
    seed = (
        0x12345678
        ^ ((int(dungeon_id) & 0xFFFFFFFF) << 8)
        ^ ((int(cell_x) & 0xFF) << 16)
        ^ ((int(cell_y) & 0xFF) << 24)
        ^ (int(character_id) & 0xFFFFFFFF)
    ) & 0xFFFFFFFF
    seed &= ~0x40000
    return seed & 0xFFFFFFFF


def resolve_dungeon_room(
    dungeon_id: int,
    cell_x: int,
    cell_y: int,
    *,
    difficulty: int = 0,
    maze_index: int | None = None,
    active_quest_ids: Sequence[int] = (),
    finished_quest_ids: Sequence[int] = (),
    active_quest_pairs: Sequence[tuple[int, int]] = (),
    boss_map_variants: Mapping[tuple[str, int, int], int] | None = None,
) -> dict[str, Any]:
    """Resolve arbitrary maze cell → map_index (MOVE_MAP / 副本实现A §5.4)."""
    idx = get_quest_index()
    if maze_index is None:
        maze_index, _maze, reason = idx.select_dungeon_maze(
            int(dungeon_id),
            difficulty=int(difficulty),
            active_quest_ids=active_quest_ids,
            finished_quest_ids=finished_quest_ids,
        )
    else:
        reason = "session_maze"
    maze_i = int(maze_index if maze_index is not None and int(maze_index) >= 0 else 0)
    maze = _load_maze_details(int(dungeon_id), maze_i)
    if boss_map_variants is None:
        selected_boss_map_variants = _build_boss_map_variant_plan_for_maze(
            int(dungeon_id),
            difficulty=int(difficulty),
            maze=maze,
            active_quest_pairs=active_quest_pairs,
        )
    else:
        selected_boss_map_variants = dict(boss_map_variants)
    cx = int(cell_x) & 0xFF
    cy = int(cell_y) & 0xFF
    boss_x = 0
    boss_y = 0
    if maze is not None and getattr(maze, "boss_maps", None):
        boss_x, boss_y = int(maze.boss_maps[0][0]), int(maze.boss_maps[0][1])
    current_is_boss = (cx, cy) == (boss_x, boss_y)
    map_index = resolve_map_id_for_cell(
        maze,
        cx,
        cy,
        exact_only=True,
        prefer_boss=current_is_boss,
        boss_map_variants=selected_boss_map_variants,
    )
    dgn = idx.get_dungeon(int(dungeon_id))
    tournament_spec = None
    try:
        from game.chain_loader import load_chain

        tournament = load_chain("helper-dungeon_tournament.py")
        tournament_spec = tournament.tournament_spec_for_dungeon(int(dungeon_id))
    except Exception:
        tournament_spec = None
    map_source = "maze_map_specification" if map_index > 0 else ""
    if map_index <= 0:
        try:
            from game.chain_loader import load_chain

            map_index_helper = load_chain("helper-dungeon_map_monsters.py")
            map_index = map_index_helper.resolve_omitted_start_map(
                int(dungeon_id),
                maze_i,
                cx,
                cy,
            )
        except Exception:
            map_index = 0
        if map_index > 0:
            map_source = "map_native_start_area"
    if map_index <= 0 and tournament_spec is not None:
        try:
            from game.chain_loader import load_chain

            tournament = load_chain("helper-dungeon_tournament.py")
            map_index = tournament.resolve_tournament_map_for_cell(
                int(dungeon_id),
                cx,
                cy,
            )
        except Exception:
            map_index = 0
        if map_index > 0:
            map_source = "tournament_map_dungeon_reverse"
    if map_index <= 0:
        map_index = _resolve_tower_map_id_for_cell(
            int(dungeon_id),
            str(getattr(dgn, "path", "") or ""),
            maze,
            cx,
            cy,
        )
        if map_index > 0:
            map_source = "tower_map_dungeon_reverse"
    if map_index <= 0:
        map_index = resolve_map_id_for_cell(
            maze,
            cx,
            cy,
            prefer_boss=current_is_boss,
            boss_map_variants=selected_boss_map_variants,
        )
        if map_index > 0:
            map_source = "legacy_first_map_specification"
    base_w = float(getattr(dgn, "experience_increasing_point", 1.0) or 1.0)
    story = getattr(dgn, "story_mode", None)
    qc = (
        list(maze.quest_connection)
        if maze is not None and getattr(maze, "quest_connection", None)
        else None
    )
    try:
        from game.chain_loader import load_chain

        _story = load_chain("helper-dungeon_story_mode.py")
        story_active = _story.is_story_run(
            maze_index=maze_i,
            quest_connection=qc,
            reason=str(reason or ""),
        )
        story_mult = _story.resolve_story_exp_multiplier(story, story_active=story_active)
        combined_w = _story.combine_experience_weight(
            base_w, story, story_active=story_active
        )
    except Exception:
        story_active = False
        story_mult = 1.0
        combined_w = base_w
    return {
        "dungeon_id": int(dungeon_id),
        "difficulty": int(difficulty) & 0xFF,
        "maze_index": maze_i,
        "reason": reason,
        "cell_x": cx,
        "cell_y": cy,
        "boss_x": int(boss_x) & 0xFF,
        "boss_y": int(boss_y) & 0xFF,
        "map_index": int(map_index) & 0xFFFFFFFF,
        "map_source": map_source,
        "boss_map_variants": selected_boss_map_variants,
        "open_door_even_enemy": bool(
            getattr(maze, "open_door_even_enemy", False)
        ),
        "basis_level": int(getattr(dgn, "basis_level", 0) or 0),
        "minimum_required_level": int(getattr(dgn, "minimum_required_level", 0) or 0),
        "experience_increasing_point": base_w,
        "story_mode_active": bool(story_active),
        "story_exp_multiplier": float(story_mult),
        "experience_weight": float(combined_w),
        "quest_connection": qc,
        "is_tournament_dungeon": bool(tournament_spec is not None),
        "tournament_round_count": int(
            getattr(tournament_spec, "round_count", 0) or 0
        ),
        "tournament_start_area": tuple(
            getattr(tournament_spec, "start_area", ()) or ()
        ),
        "tournament_round_fatigue": int(
            getattr(tournament_spec, "round_fatigue", 0) or 0
        ),
        "tournament_enter_without_fatigue": bool(
            getattr(tournament_spec, "enter_without_fatigue", False)
        ),
        "is_boss_cell": current_is_boss,
        "is_special_hell_dungeon": _is_special_hell_dungeon(dgn, maze),
    }
