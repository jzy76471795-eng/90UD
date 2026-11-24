"""Account progress helpers for the two 100-floor tower dungeon ranges."""
from __future__ import annotations

import re
import struct
from typing import Optional


TOWER_RANGES = {
    "grief": (5115, 5214),
    "despair": (11008, 11107),
}


def tower_kind_for_dungeon(dungeon_id: int) -> Optional[str]:
    did = int(dungeon_id)
    for tower_kind, (first_dungeon, last_dungeon) in TOWER_RANGES.items():
        if first_dungeon <= did <= last_dungeon:
            return tower_kind
    return None


def floor_for_dungeon(tower_kind: str, dungeon_id: int) -> Optional[int]:
    kind = str(tower_kind or "").strip().lower()
    tower_range = TOWER_RANGES.get(kind)
    if tower_range is None:
        raise ValueError("unknown hundred-floor tower kind: %r" % tower_kind)
    did = int(dungeon_id)
    first_dungeon, last_dungeon = tower_range
    if not first_dungeon <= did <= last_dungeon:
        return None
    return did - first_dungeon + 1


def dungeon_for_floor(tower_kind: str, floor: int) -> int:
    kind = str(tower_kind or "").strip().lower()
    tower_range = TOWER_RANGES.get(kind)
    if tower_range is None:
        raise ValueError("unknown hundred-floor tower kind: %r" % tower_kind)
    value = int(floor)
    if not 1 <= value <= 100:
        raise ValueError("invalid hundred-floor tower floor: %r" % floor)
    return tower_range[0] + value - 1


def correct_stale_dungeon_request(
    tower_kind: str,
    dungeon_id: int,
    cleared_floor: int,
) -> int:
    """Route an already-cleared floor to the next uncleared floor."""
    requested_floor = floor_for_dungeon(tower_kind, dungeon_id)
    progress = max(0, min(100, int(cleared_floor)))
    if requested_floor is None or progress <= 0 or progress >= 100:
        return int(dungeon_id)
    if requested_floor <= progress:
        return dungeon_for_floor(tower_kind, progress + 1)
    return int(dungeon_id)


def build_grief_progress_050e(cleared_floor: int) -> bytes:
    """Stage3 S0/050E reader consumes exactly one little-endian u16."""
    progress = max(0, min(100, int(cleared_floor)))
    return struct.pack("<H", progress)


def tower_entry_display(repo, account_id: int, entry_day: str) -> tuple[int, int, int, int]:
    """Stage3 0545/0578/0510 use remaining and total, in despair/grief order."""
    from pvf import pvf_runtime

    used = repo.get_hundred_floor_tower_entries(account_id, entry_day)
    result = []
    for kind in ("despair", "grief"):
        try:
            text = pvf_runtime.read_text(f"etc/towerof{kind}.etc")
        except KeyError:
            result.extend((0, 0))
            continue
        match = re.search(r"(?im)^\[account enterable max count\]\s*\n\s*(\d+)\s*$", text)
        total = int(match[1]) if match is not None else 0
        if not 0 <= total <= 127:
            raise ValueError(f"invalid account tower limit for {kind}")
        result.extend((max(0, total - used.get(kind, 0)), total))
    return tuple(result)
