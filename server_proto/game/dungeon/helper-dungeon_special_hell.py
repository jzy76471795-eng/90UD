# -*- coding: utf-8 -*-
"""PVF special-Hell final-boss reward boundary."""
from __future__ import annotations

from typing import Any


def is_special_hell_final_boss(session: Any, actor: Any) -> bool:
    """True only for the final blocking actor in a PVF special-Hell boss room."""
    if session is None or not bool(getattr(session, "dungeon_special_hell", False)):
        return False
    if actor is None or not bool(actor.get("is_blocking", True)):
        return False
    if (
        int(getattr(session, "dungeon_cell_x", -1)) != int(getattr(session, "dungeon_boss_x", -2))
        or int(getattr(session, "dungeon_cell_y", -1)) != int(getattr(session, "dungeon_boss_y", -2))
    ):
        return False
    for candidate in list(getattr(session, "dungeon_room_mons", ()) or ()):
        if candidate is actor or bool(candidate.get("killed")):
            continue
        if bool(candidate.get("is_blocking", True)):
            return False
    return True
