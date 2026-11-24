"""Explicit JSON snapshot for the fully warmed quest/dungeon index.

The snapshot is a derived artifact bound by :mod:`pvf.index_cache` to the
complete Script.pvf identity.  It contains only primitive JSON values and is
validated before being installed into the normal QuestIndex instance.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any


CACHE_DOMAIN = "quest_index"
CACHE_SEMANTIC_VERSION = 4  # Preserve maze-local passive-item tables.
CACHE_SCHEMA = 1


def _dump(index: Any) -> dict[str, Any]:
    return {
        "schema": CACHE_SCHEMA,
        "source": str(index.source or "shared_pvf"),
        "quest_paths": [[int(key), str(value)] for key, value in sorted(index.quest_paths.items())],
        "metas": [asdict(index.metas[key]) for key in sorted(index.metas)],
        "sorted_quest_ids": [int(value) for value in index.sorted_quest_ids],
        "connect_quest_units": [asdict(value) for value in index.connect_quest_units],
        "episode_quest_units": [asdict(value) for value in index.episode_quest_units],
        "epic_sequence_ids": sorted(int(value) for value in index.epic_sequence_ids),
        "unsequenced_epic_ids": sorted(int(value) for value in index.unsequenced_epic_ids),
        "epic_sequence_positions": [
            [int(key), int(value[0]), int(value[1])]
            for key, value in sorted(index.epic_sequence_positions.items())
        ],
        "dungeon_paths": [[int(key), str(value)] for key, value in sorted(index.dungeon_paths.items())],
        "dungeon_cache": [asdict(index.dungeon_cache[key]) for key in sorted(index.dungeon_cache)],
        "training_npcs": [
            [int(level), sorted(int(value) for value in quest_ids)]
            for level, quest_ids in sorted(index.training_npcs.items())
        ],
        "local_unlock_ids": [int(value) for value in index.local_unlock_ids],
        "worldmap_condition_quest_ids": sorted(int(value) for value in index.worldmap_condition_quest_ids),
        "worldmap_progression_quest_ids": sorted(int(value) for value in index.worldmap_progression_quest_ids),
        "town_permission_quest_ids": sorted(int(value) for value in index.town_permission_quest_ids),
        "map_unlock_quest_ids": sorted(int(value) for value in index.map_unlock_quest_ids),
        "metas_fully_loaded": bool(getattr(index, "_metas_fully_loaded", False)),
        "dungeons_fully_loaded": bool(getattr(index, "_dungeons_fully_loaded", False)),
    }


def _tuple_pair_map(value: Any) -> dict[str, tuple[float, float]]:
    if not isinstance(value, dict):
        raise ValueError("story mode table must be an object")
    result: dict[str, tuple[float, float]] = {}
    for key, pair in value.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("story mode table pair is invalid")
        result[str(key)] = (float(pair[0]), float(pair[1]))
    return result


def _restore_story_mode(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("story mode payload is invalid")
    from game.chain_loader import load_chain

    StoryModeInfo = load_chain("helper-dungeon_story_mode.py").StoryModeInfo

    return StoryModeInfo(
        present=bool(value.get("present", False)),
        difficulty_size=int(value.get("difficulty_size", 0)),
        exp_rate_a=(
            None
            if value.get("exp_rate_a") is None
            else float(value.get("exp_rate_a"))
        ),
        exp_rate_b=(
            None
            if value.get("exp_rate_b") is None
            else float(value.get("exp_rate_b"))
        ),
        max_hp=_tuple_pair_map(value.get("max_hp", {})),
        defense=_tuple_pair_map(value.get("defense", {})),
        quest_list=[int(item) for item in value.get("quest_list", [])],
    )


def _restore_meta(value: Any, quest_paths: dict[int, str]) -> Any:
    if not isinstance(value, dict):
        raise ValueError("quest meta payload is invalid")
    from game.quest.quest_index import (
        EnemyRewardEntry,
        MonsterRewardEntry,
        QuestMeta,
        TargetCharacterRule,
    )

    raw = dict(value)
    quest_id = int(raw.get("quest_id")) & 0xFFFF
    path = str(raw.get("path") or "")
    if quest_paths.get(quest_id) != path:
        raise ValueError("quest meta path mismatch for id=%d" % quest_id)
    raw["quest_id"] = quest_id
    raw["path"] = path
    raw["target_character_rules"] = [
        TargetCharacterRule(**dict(item))
        for item in raw.get("target_character_rules", [])
    ]
    raw["dungeon_info"] = tuple(int(item) for item in raw.get("dungeon_info", []))
    raw["dialog_map_ids"] = tuple(int(item) for item in raw.get("dialog_map_ids", []))
    raw["dialog_apc_codes"] = tuple(int(item) for item in raw.get("dialog_apc_codes", []))
    raw["pre_groups"] = [
        [int(item) for item in group] for group in raw.get("pre_groups", [])
    ]
    raw["pre_required_answers"] = [
        (int(item[0]), int(item[1])) for item in raw.get("pre_required_answers", [])
    ]
    raw["collision"] = [int(item) for item in raw.get("collision", [])]
    raw["monster_rewards"] = [
        MonsterRewardEntry(**dict(item)) for item in raw.get("monster_rewards", [])
    ]
    raw["enemy_rewards"] = [
        EnemyRewardEntry(**dict(item)) for item in raw.get("enemy_rewards", [])
    ]
    return QuestMeta(**raw)


def _restore_dungeon(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("dungeon payload is invalid")
    from game.quest.quest_index import (
        DungeonFileInfo,
        LinkedDungeonNext,
        MazeClearCondition,
        MazeInfo,
        MazeRidableObject,
        MazeRidableScript,
        SpecialPassiveObjectItem,
    )

    raw = dict(value)
    mazes = []
    for maze_value in raw.get("mazes", []):
        maze = dict(maze_value)
        maze["quest_connection"] = (
            None
            if maze.get("quest_connection") is None
            else tuple(int(item) for item in maze.get("quest_connection", []))
        )
        maze["clear_conditions"] = [
            MazeClearCondition(**dict(item))
            for item in maze.get("clear_conditions", [])
        ]
        maze["start_maps"] = [
            (int(item[0]), int(item[1])) for item in maze.get("start_maps", [])
        ]
        maze["boss_maps"] = [
            (int(item[0]), int(item[1])) for item in maze.get("boss_maps", [])
        ]
        ridable = maze.get("ridable_script")
        if ridable is not None:
            ridable = dict(ridable)
            ridable["objects"] = tuple(
                MazeRidableObject(**dict(item))
                for item in ridable.get("objects", [])
            )
            ridable = MazeRidableScript(**ridable)
        maze["ridable_script"] = ridable
        local_items = maze.get("special_passive_object_items")
        maze["special_passive_object_items"] = (
            None if local_items is None else [
                SpecialPassiveObjectItem(**dict(item)) for item in local_items
            ]
        )
        mazes.append(MazeInfo(**maze))
    raw["mazes"] = mazes
    raw["difficulty_levels"] = tuple(int(item) for item in raw.get("difficulty_levels", []))
    raw["death_tower_map_indexes"] = tuple(
        int(item) for item in raw.get("death_tower_map_indexes", [])
    )
    raw["special_passive_object_items"] = [
        SpecialPassiveObjectItem(**dict(item))
        for item in raw.get("special_passive_object_items", [])
    ]
    raw["boss_entrance_hunt_monsters"] = tuple(
        int(item) for item in raw.get("boss_entrance_hunt_monsters", [])
    )
    raw["boss_entrance_summon_monsters"] = tuple(
        int(item) for item in raw.get("boss_entrance_summon_monsters", [])
    )
    raw["linked_prev_dungeon_ids"] = tuple(
        int(item) for item in raw.get("linked_prev_dungeon_ids", [])
    )
    raw["linked_next_dungeons"] = tuple(
        LinkedDungeonNext(**dict(item))
        for item in raw.get("linked_next_dungeons", [])
    )
    raw["story_mode"] = _restore_story_mode(raw.get("story_mode"))
    return DungeonFileInfo(**raw)


def install(index: Any, payload: Any) -> None:
    if not isinstance(payload, dict) or int(payload.get("schema", -1)) != CACHE_SCHEMA:
        raise ValueError("quest index cache schema mismatch")
    quest_paths = {
        int(pair[0]) & 0xFFFF: str(pair[1])
        for pair in payload.get("quest_paths", [])
    }
    if len(quest_paths) != len(payload.get("quest_paths", [])) or not quest_paths:
        raise ValueError("quest index cache paths are invalid")
    metas = {}
    for value in payload.get("metas", []):
        meta = _restore_meta(value, quest_paths)
        if meta.quest_id in metas:
            raise ValueError("duplicate quest meta id=%d" % meta.quest_id)
        metas[meta.quest_id] = meta
    if set(metas) != set(quest_paths):
        raise ValueError("quest index cache meta/path count mismatch")

    from game.quest.quest_index import ConnectQuestUnit

    def restore_connect(value: Any) -> Any:
        row = dict(value)
        row["level_section"] = tuple(int(item) for item in row.get("level_section", []))
        row["sequence"] = tuple(int(item) for item in row.get("sequence", []))
        return ConnectQuestUnit(**row)

    connect = tuple(
        restore_connect(value) for value in payload.get("connect_quest_units", [])
    )
    episode = tuple(
        restore_connect(value) for value in payload.get("episode_quest_units", [])
    )
    if not episode:
        raise ValueError("quest index cache has no episode units")
    dungeon_paths = {
        int(pair[0]): str(pair[1]) for pair in payload.get("dungeon_paths", [])
    }
    dungeon_cache = {}
    for value in payload.get("dungeon_cache", []):
        dungeon = _restore_dungeon(value)
        if dungeon.dungeon_id in dungeon_cache or not dungeon.path:
            raise ValueError("duplicate or empty dungeon cache entry")
        if dungeon_paths.get(dungeon.dungeon_id) != dungeon.path:
            raise ValueError("dungeon path mismatch for id=%d" % dungeon.dungeon_id)
        dungeon_cache[dungeon.dungeon_id] = dungeon
    if set(dungeon_cache) != set(dungeon_paths):
        raise ValueError("quest index cache dungeon count mismatch")

    index.quest_paths = quest_paths
    index.metas = metas
    index.sorted_quest_ids = [int(item) & 0xFFFF for item in payload.get("sorted_quest_ids", [])]
    if index.sorted_quest_ids != sorted(quest_paths):
        raise ValueError("quest index cache sorted IDs mismatch")
    index.connect_quest_units = connect
    index.episode_quest_units = episode
    index.epic_sequence_ids = {int(item) & 0xFFFF for item in payload.get("epic_sequence_ids", [])}
    index.unsequenced_epic_ids = {int(item) & 0xFFFF for item in payload.get("unsequenced_epic_ids", [])}
    index.epic_sequence_positions = {
        int(item[0]) & 0xFFFF: (int(item[1]), int(item[2]))
        for item in payload.get("epic_sequence_positions", [])
    }
    index.dungeon_paths = dungeon_paths
    index.dungeon_cache = dungeon_cache
    index.training_npcs = {
        int(item[0]): {int(value) & 0xFFFF for value in item[1]}
        for item in payload.get("training_npcs", [])
    }
    index.local_unlock_ids = [int(item) & 0xFFFF for item in payload.get("local_unlock_ids", [])]
    index.worldmap_condition_quest_ids = {
        int(item) & 0xFFFF for item in payload.get("worldmap_condition_quest_ids", [])
    }
    index.worldmap_progression_quest_ids = {
        int(item) & 0xFFFF for item in payload.get("worldmap_progression_quest_ids", [])
    }
    index.town_permission_quest_ids = {
        int(item) & 0xFFFF for item in payload.get("town_permission_quest_ids", [])
    }
    index.map_unlock_quest_ids = {
        int(item) & 0xFFFF for item in payload.get("map_unlock_quest_ids", [])
    }
    index.acceptable_cache = {}
    index.source = "shared_pvf"
    index.loaded = True
    index._metas_fully_loaded = bool(payload.get("metas_fully_loaded", False))
    index._dungeons_fully_loaded = bool(payload.get("dungeons_fully_loaded", False))
    if not index._metas_fully_loaded or not index._dungeons_fully_loaded:
        raise ValueError("quest index cache is not fully warmed")


def to_payload(index: Any) -> dict[str, Any]:
    payload = _dump(index)
    # Fail early on non-JSON values before handing the payload to the generic
    # cache envelope.  This also prevents accidental future object leakage.
    import json

    json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return payload


__all__ = [
    "CACHE_DOMAIN",
    "CACHE_SCHEMA",
    "CACHE_SEMANTIC_VERSION",
    "install",
    "to_payload",
]
