"""Dungeon quest-connection maze select helper (task doc 7.2)."""
from __future__ import annotations

from typing import Sequence

from game.quest.quest_index import get_quest_index


def resolve_dungeon_start_info_with_quests(
    dungeon_id: int,
    *,
    difficulty: int = 0,
    active_quest_ids: Sequence[int] = (),
    finished_quest_ids: Sequence[int] = (),
) -> dict:
    idx = get_quest_index()
    maze_index, maze, reason = idx.select_dungeon_maze(
        int(dungeon_id),
        difficulty=int(difficulty),
        active_quest_ids=active_quest_ids,
        finished_quest_ids=finished_quest_ids,
    )
    start = None
    if maze is not None and maze.start_maps:
        start = maze.start_maps[0]
    return {
        "dungeon_id": int(dungeon_id),
        "maze_index": int(maze_index),
        "reason": reason,
        "quest_connection": list(maze.quest_connection) if maze and maze.quest_connection else None,
        "start_map": start,
        "maze_name": getattr(maze, "name", "") if maze else "",
    }
