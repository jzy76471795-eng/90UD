"""Boot-warmed Stage3 circle-dungeon entry difficulty index."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from pvf import pvf_runtime


CIRCLE_DUNGEON_INFO_PATH = "etc/circledungeoninfo.etc"
_INT_TOKEN = re.compile(r"-?\d+")
_GROUP_BLOCK = re.compile(
    r"(?is)\[circle\s+dungeon\s+list\s+info\]"
    r"(.*?)"
    r"\[/circle\s+dungeon\s+list\s+info\]"
)
_LOCK = threading.RLock()
_INDEX: Optional["CircleDungeonInfoIndex"] = None


def _open_section(text: str, name: str) -> str:
    match = re.search(
        r"(?is)\["
        + re.escape(name)
        + r"\](.*?)(?=\r?\n\s*\[[^/]|\Z)",
        str(text or ""),
    )
    return match.group(1) if match is not None else ""


def _closed_section(text: str, name: str) -> str:
    match = re.search(
        r"(?is)\["
        + re.escape(name)
        + r"\](.*?)\[/"
        + re.escape(name)
        + r"\]",
        str(text or ""),
    )
    return match.group(1) if match is not None else ""


def _int_values(text: str) -> list[int]:
    return [int(value) for value in _INT_TOKEN.findall(str(text or ""))]


@dataclass(frozen=True)
class CircleDungeonInfoIndex:
    difficulty_by_pair: Mapping[tuple[int, int], int]
    group_count: int

    @property
    def entry_count(self) -> int:
        return len(self.difficulty_by_pair)

    def difficulty_for(self, dungeon_id: int, circle_id: int) -> Optional[int]:
        return self.difficulty_by_pair.get((int(dungeon_id), int(circle_id)))


def parse_circle_dungeon_info(text: str) -> CircleDungeonInfoIndex:
    groups = list(_GROUP_BLOCK.findall(str(text or "")))
    if not groups:
        raise ValueError("circle dungeon info has no list groups")

    difficulty_by_pair: dict[tuple[int, int], int] = {}
    for group_index, block in enumerate(groups):
        difficulties = _int_values(_open_section(block, "difficulty level"))
        if len(difficulties) != 1 or not 0 <= difficulties[0] <= 4:
            raise ValueError(
                "circle dungeon group %d has invalid difficulty level %r"
                % (group_index, difficulties)
            )
        difficulty = int(difficulties[0])
        values = _int_values(_closed_section(block, "Dungeon"))
        if not values or len(values) % 2:
            raise ValueError(
                "circle dungeon group %d has invalid Dungeon pair count %d"
                % (group_index, len(values))
            )
        for offset in range(0, len(values), 2):
            dungeon_id = int(values[offset])
            circle_id = int(values[offset + 1])
            if not 0 < dungeon_id <= 0xFFFFFFFF:
                raise ValueError(
                    "circle dungeon group %d has invalid dungeon id %d"
                    % (group_index, dungeon_id)
                )
            if not 0 < circle_id <= 0xFFFFFFFF:
                raise ValueError(
                    "circle dungeon group %d has invalid circle id %d"
                    % (group_index, circle_id)
                )
            pair = (dungeon_id, circle_id)
            previous = difficulty_by_pair.get(pair)
            if previous is not None:
                raise ValueError(
                    "circle dungeon pair %d/%d is duplicated at difficulties %d and %d"
                    % (dungeon_id, circle_id, previous, difficulty)
                )
            difficulty_by_pair[pair] = difficulty

    return CircleDungeonInfoIndex(
        difficulty_by_pair=MappingProxyType(dict(difficulty_by_pair)),
        group_count=len(groups),
    )


def warmup_circle_dungeon_info_index() -> dict[str, int | str]:
    global _INDEX
    parsed = parse_circle_dungeon_info(
        pvf_runtime.read_text(CIRCLE_DUNGEON_INFO_PATH)
    )
    with _LOCK:
        _INDEX = parsed
    return {
        "circle_group_count": parsed.group_count,
        "circle_entry_count": parsed.entry_count,
        "circle_source": CIRCLE_DUNGEON_INFO_PATH,
    }


def resolve_circle_dungeon_difficulty(
    dungeon_id: int,
    circle_id: int,
) -> Optional[int]:
    with _LOCK:
        index = _INDEX
    if index is None:
        return None
    return index.difficulty_for(dungeon_id, circle_id)
