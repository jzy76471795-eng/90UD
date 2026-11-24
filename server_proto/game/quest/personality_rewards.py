"""Permanent personality-skill rewards decoded from quest PVF data.

The Stage3 quest scripts mark these rewards with ``[special reward status]``.
Their reward names are localized by the client, so this module deliberately
identifies the ordinary personality sequence from the stable PVF structure:
``grade=[side]`` plus a non-empty special-status block.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Iterable

from game.quest import quest_rewards


_LOCK = threading.RLock()
_INDEX: dict[int, "PersonalityReward"] | None = None


@dataclass(frozen=True)
class PersonalityStatus:
    """One permanent status operation stored in a quest script."""

    name: str
    operator: str
    value: int


@dataclass(frozen=True)
class PersonalityReward:
    quest_id: int
    source_path: str
    statuses: tuple[PersonalityStatus, ...]


def _normalise_tag(value: str) -> str:
    return quest_rewards._strip_tick(value).strip().lower()


def _parse_statuses(rows: Iterable[str]) -> tuple[PersonalityStatus, ...]:
    tokens = [_normalise_tag(row) for row in rows if str(row).strip()]
    out: list[PersonalityStatus] = []
    index = 0
    while index + 2 < len(tokens):
        name = tokens[index]
        operator = tokens[index + 1]
        raw_value = tokens[index + 2]
        index += 3
        if not name or operator not in {"+", "%"}:
            continue
        try:
            value = int(raw_value, 0)
        except (TypeError, ValueError):
            continue
        out.append(PersonalityStatus(name=name, operator=operator, value=value))
    return tuple(out)


def warmup_personality_reward_index() -> dict[str, int]:
    """Build the ordinary personality-reward index from the shared PVF cache."""
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _reader, paths, _param = quest_rewards._ensure_index()
            rewards: dict[int, PersonalityReward] = {}
            for raw_quest_id, source_path in paths.items():
                quest_id = int(raw_quest_id) & 0xFFFF
                sections, resolved_path = quest_rewards._read_quest_sections(quest_id)
                if not sections:
                    continue
                # The alternate Circle reward (quest 14820) has the same
                # status as its side-quest predecessor and must not stack.
                if _normalise_tag(quest_rewards._section_first(sections, "grade")) != "[side]":
                    continue
                statuses = _parse_statuses(sections.get("special reward status", ()))
                if not statuses:
                    continue
                rewards[quest_id] = PersonalityReward(
                    quest_id=quest_id,
                    source_path=resolved_path or str(source_path),
                    statuses=statuses,
                )
            _INDEX = rewards
        status_count = sum(len(reward.statuses) for reward in _INDEX.values())
        return {
            "ok": 1,
            "personality_quest_count": len(_INDEX),
            "personality_status_count": status_count,
        }


def get_personality_reward_index() -> dict[int, PersonalityReward]:
    warmup_personality_reward_index()
    with _LOCK:
        return dict(_INDEX or {})


def finished_personality_rewards(
    finished_quest_ids: Iterable[int] | None,
) -> tuple[PersonalityReward, ...]:
    """Return each matched ordinary reward once, independent of DB row order."""
    index = get_personality_reward_index()
    seen: set[int] = set()
    for raw_id in finished_quest_ids or ():
        try:
            quest_id = int(raw_id) & 0xFFFF
        except (TypeError, ValueError):
            continue
        if quest_id in index:
            seen.add(quest_id)
    return tuple(index[quest_id] for quest_id in sorted(seen))


def finished_personality_reward_ids(
    finished_quest_ids: Iterable[int] | None,
) -> tuple[int, ...]:
    """Return the deduplicated special-reward quest ids in stable wire order."""
    return tuple(int(reward.quest_id) & 0xFFFF for reward in finished_personality_rewards(finished_quest_ids))


def aggregate_personality_statuses(
    finished_quest_ids: Iterable[int] | None,
) -> dict[str, int]:
    """Aggregate PVF permanent values for the character's finished quests.

    All current personality scripts use additive values. The operator remains
    part of ``PersonalityStatus`` so a future non-additive format is retained
    explicitly instead of being silently treated as a different stat.
    """
    totals: dict[str, int] = {}
    for reward in finished_personality_rewards(finished_quest_ids):
        for status in reward.statuses:
            if status.operator not in {"+", "%"}:
                continue
            totals[status.name] = int(totals.get(status.name, 0)) + int(status.value)
    return totals


def reset_personality_reward_index_for_tests() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


__all__ = [
    "PersonalityReward",
    "PersonalityStatus",
    "aggregate_personality_statuses",
    "finished_personality_reward_ids",
    "finished_personality_rewards",
    "get_personality_reward_index",
    "reset_personality_reward_index_for_tests",
    "warmup_personality_reward_index",
]
