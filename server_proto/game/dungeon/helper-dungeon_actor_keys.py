"""Dungeon Actor ObjectKey allocation.

Stage3 stores both PARTY_INFO member ``uid`` and the S0/001D actor-row
``seq`` in the same u16 Actor table.  The first dungeon object therefore
must not reuse the local party member key.
"""
from __future__ import annotations

from typing import Optional


# 90qiyuan's paired protocol names this first object key 0x0192.  The
# START_MAP builder accepts a base and emits ``base + 1`` for its first row.
ACTOR_KEY_FLOOR_BASE = 0x0191
ACTOR_KEY_MAX = 0xFFFE
START_MAP_MAX_ACTORS = 0xFF
ACTOR_KEY_HIGHEST_SAFE_BASE = ACTOR_KEY_MAX - START_MAP_MAX_ACTORS


def initial_actor_key_base(player_actor_key: Optional[int]) -> int:
    """Return the START_MAP sequence base for a new dungeon run.

    Keep the official floor for normal low keys.  When the player's Actor
    key is in the allocatable upper range, begin immediately after it so the
    contiguous actor rows cannot overwrite the player's table entry.  A
    player key too near the u16 ceiling falls back to the official floor so
    even a full 255-row START_MAP avoids 0xFFFF and wraparound.
    """
    uid = int(player_actor_key or 0) & 0xFFFF
    if ACTOR_KEY_FLOOR_BASE < uid <= ACTOR_KEY_HIGHEST_SAFE_BASE:
        return uid
    return ACTOR_KEY_FLOOR_BASE
