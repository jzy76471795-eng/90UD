"""PVF-backed character level-up ticket identity and limits."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime


LEVEL_UP_TICKET_ACTION_TYPE = "[level up ticket]"


def _section_body(text: str, key: str) -> str:
    match = re.search(
        r"\[" + re.escape(key) + r"\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)",
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _first_token(body: str) -> str:
    match = re.search(r"`([^`]*)`", str(body or ""))
    if match is not None:
        return match.group(1).strip().lower()
    for line in str(body or "").splitlines():
        token = line.strip().strip("`")
        if token:
            return token.lower()
    return ""


def _single_int(text: str, key: str) -> int | None:
    body = _section_body(text, key)
    match = re.search(r"-?\d+", body)
    return int(match.group(0)) if match is not None else None


@dataclass(frozen=True)
class LevelUpTicketRule:
    item_id: int
    maximum_level: int
    levelup_range: int
    source_path: str = ""
    minimum_level: int = 1

    def __post_init__(self) -> None:
        item_id = int(self.item_id) & 0xFFFFFFFF
        maximum_level = int(self.maximum_level)
        minimum_level = int(self.minimum_level)
        levelup_range = int(self.levelup_range)
        if item_id <= 0:
            raise ValueError("level-up ticket item id must be positive")
        if not 1 <= minimum_level <= maximum_level <= 255:
            raise ValueError(
                "level-up ticket maximum level is invalid item=%d level=%d"
                % (item_id, maximum_level)
            )
        if not 1 <= levelup_range <= 255:
            raise ValueError(
                "level-up ticket range is invalid item=%d range=%d"
                % (item_id, levelup_range)
            )
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "maximum_level", maximum_level)
        object.__setattr__(self, "minimum_level", minimum_level)
        object.__setattr__(self, "levelup_range", levelup_range)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    def allows_current_level(self, level: int, *, character_cap: int) -> bool:
        current = max(1, int(level))
        cap = max(1, int(character_cap))
        return self.minimum_level <= current <= self.maximum_level and current < cap

    def target_level(
        self,
        current_level: int,
        *,
        character_cap: int,
        level_increment: int,
    ) -> int | None:
        """Resolve one PVF range roll without turning maximum level into a cap."""
        current = max(1, int(current_level))
        cap = max(1, int(character_cap))
        increment = int(level_increment)
        if not self.allows_current_level(current, character_cap=cap):
            return None
        if not 1 <= increment <= int(self.levelup_range):
            raise ValueError(
                "level-up ticket increment is outside PVF range item=%d"
                % int(self.item_id)
            )
        return min(cap, current + increment)


def parse_level_up_ticket_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> LevelUpTicketRule | None:
    """Return a rule only for the PVF-authored level-up action type."""
    action_type = _first_token(_section_body(text, "action type"))
    if action_type != LEVEL_UP_TICKET_ACTION_TYPE:
        return None
    maximum_level = _single_int(text, "maximum level")
    levelup_range = _single_int(text, "levelup range")
    if maximum_level is None or levelup_range is None:
        raise ValueError(
            "level-up ticket fields are incomplete item=%d source=%s"
            % (int(item_id), source_path or "-")
        )
    return LevelUpTicketRule(
        item_id=int(item_id),
        maximum_level=maximum_level,
        levelup_range=levelup_range,
        source_path=source_path,
        minimum_level=_single_int(text, "minimum level") or 1,
    )


@lru_cache(maxsize=4096)
def resolve_level_up_ticket_rule(item_id: int) -> LevelUpTicketRule | None:
    """Resolve one requested item through the shared PVF reader and exact script."""
    iid = int(item_id) & 0xFFFFFFFF
    metadata = resolve_item_metadata(iid)
    if not metadata.is_stackable or not metadata.source_path:
        return None
    text = pvf_runtime.read_text(metadata.source_path)
    return parse_level_up_ticket_rule(
        iid,
        text,
        source_path=metadata.source_path,
    )


def clear_level_up_ticket_rule_cache() -> None:
    resolve_level_up_ticket_rule.cache_clear()


__all__ = [
    "LEVEL_UP_TICKET_ACTION_TYPE",
    "LevelUpTicketRule",
    "clear_level_up_ticket_rule_cache",
    "parse_level_up_ticket_rule",
    "resolve_level_up_ticket_rule",
]
