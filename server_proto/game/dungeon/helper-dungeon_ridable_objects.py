"""PVF-driven interactive vehicle selection for dungeon START_MAP packets."""
from __future__ import annotations

import random
import re
from functools import lru_cache

from game.quest import quest_index
from game.quest.quest_index import MazeRidableObject, MazeRidableScript
from pvf import pvf_runtime


_RIDABLE_MONSTER_TYPE = re.compile(
    r"(?im)^\s*\[monster\s+object\s+type\]\s*\r?\n"
    r"\s*`?\s*(\[[^\]\r\n]+\])\s*`?\s*$"
)


@lru_cache(maxsize=1)
def _monster_script_paths() -> dict[int, str]:
    """Return the shared-PVF monster ID to script-path index."""
    from game.drop.pvf_section import parse_lst_entries

    text = pvf_runtime.read_text("monster/monster.lst")
    paths: dict[int, str] = {}
    for raw_code, raw_path in parse_lst_entries(text or ""):
        code = int(raw_code)
        path = str(raw_path or "").replace("\\", "/").strip("/")
        if code <= 0 or not path:
            continue
        if not path.lower().startswith("monster/"):
            path = "monster/" + path
        paths[code] = path
    return paths


@lru_cache(maxsize=4096)
def is_pvf_ridable_monster(object_index: int) -> bool:
    """Return whether the monster script explicitly declares a ride object.

    Randomized-object indices share their numeric namespace with other PVF
    lists.  Resolving through ``monster/monster.lst`` first prevents a same-ID
    passive object from changing the meaning of a DGN ride declaration.
    """
    path = _monster_script_paths().get(int(object_index), "")
    if not path:
        return False
    text = pvf_runtime.read_text(path)
    match = _RIDABLE_MONSTER_TYPE.search(str(text or ""))
    return bool(
        match is not None
        and str(match.group(1) or "").strip().lower() == "[ridable object]"
    )


def is_pvf_ridable_candidate(obj: MazeRidableObject) -> bool:
    """Return whether one selected DGN object is a neutral PVF ride object."""
    object_index = int(obj.object_index)
    return (
        object_index > 0
        and int(obj.faction) == 200
        and is_pvf_ridable_monster(object_index)
    )


def warmup_ridable_object_index() -> dict[str, int]:
    """Prime every randomized-object monster classification before Game use."""
    from game.dungeon.energy_objects import program
    program()
    index = quest_index.get_quest_index()
    index.ensure_all_dungeons_loaded()
    candidate_codes = {
        int(obj.object_index)
        for dungeon in index.dungeon_cache.values()
        for maze in list(getattr(dungeon, "mazes", None) or ())
        if (script := getattr(maze, "ridable_script", None)) is not None
        for obj in tuple(script.objects or ())
        if int(obj.object_index) > 0
    }
    if not _monster_script_paths():
        raise RuntimeError("monster/monster.lst produced an empty ride index")
    ridable_codes = {
        code for code in candidate_codes if is_pvf_ridable_monster(code)
    }
    return {
        "ridable_random_object_candidate_count": len(candidate_codes),
        "ridable_monster_count": len(ridable_codes),
    }


def select_maze_ridable_objects(
    script: MazeRidableScript | None,
    *,
    seed: int,
) -> list[MazeRidableObject]:
    """Select one run-stable subset while preserving full-list PVF order."""
    if script is None:
        return []
    candidates = list(script.objects or ())
    select_count = max(0, int(script.select_count))
    if select_count <= 0 or select_count >= len(candidates):
        return candidates
    rng = random.Random(int(seed) & 0xFFFFFFFF)
    rng.shuffle(candidates)
    return candidates[:select_count]


def ridable_objects_for_room(
    dungeon_id: int,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    *,
    seed: int,
) -> list[dict[str, int]]:
    """Return the selected interactive vehicles belonging to one maze cell."""
    if int(dungeon_id) == 218:
        from game.dungeon.energy_objects import room_objects
        return room_objects(dungeon_id, maze_index, cell_x, cell_y, seed=seed)
    try:
        dungeon = quest_index.get_quest_index().get_dungeon(int(dungeon_id))
    except Exception:
        return []
    mazes = list(getattr(dungeon, "mazes", None) or [])
    maze_i = int(maze_index)
    if maze_i < 0 or maze_i >= len(mazes):
        return []
    script = getattr(mazes[maze_i], "ridable_script", None)
    selected = select_maze_ridable_objects(script, seed=int(seed))
    room_x = int(cell_x)
    room_y = int(cell_y)
    return [
        {
            "map_x": int(obj.map_x),
            "map_y": int(obj.map_y),
            "object_index": int(obj.object_index),
            "pos_x": int(obj.pos_x),
            "pos_y": int(obj.pos_y),
            "faction": int(obj.faction),
        }
        for obj in selected
        if int(obj.map_x) == room_x
        and int(obj.map_y) == room_y
        and is_pvf_ridable_candidate(obj)
    ]
