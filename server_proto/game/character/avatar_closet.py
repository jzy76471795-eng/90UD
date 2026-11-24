"""PVF-backed Avatar Closet expansion rules."""
from __future__ import annotations

from collections.abc import Callable

from game.character.item_metadata import ItemMetadata, resolve_item_metadata


DEFAULT_UNLOCKED_SET_COUNT = 2
MAX_UNLOCKED_SET_COUNT = 4

_EXPANSION_SET_COUNT_BY_SOURCE_PATH = {
    "stackable/dfo/cash/2017/0613/avatar_closet/closet_expand_1.stk": 2,
    "stackable/dfo/cash/2017/0613/avatar_closet/closet_expand_2.stk": 3,
    "stackable/dfo/cash/2017/0613/avatar_closet/closet_expand_3.stk": 4,
}

# These IDs only narrow the three metadata lookups. Identity is accepted after
# the current PVF source path matches the exact closet script above.
KNOWN_EXPANSION_ITEM_IDS = (50022562, 50022563, 50022564)


def _normalized_source_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("/").lower()


def expansion_unlocked_set_count(metadata: ItemMetadata | None) -> int | None:
    if metadata is None:
        return None
    source_path = _normalized_source_path(getattr(metadata, "source_path", ""))
    target = _EXPANSION_SET_COUNT_BY_SOURCE_PATH.get(source_path)
    if target is None:
        return None
    return int(target)


def resolved_expansion_item_targets(
    resolver: Callable[[int], ItemMetadata] = resolve_item_metadata,
) -> dict[int, int]:
    resolved: dict[int, int] = {}
    for item_id in KNOWN_EXPANSION_ITEM_IDS:
        metadata = resolver(int(item_id))
        target = expansion_unlocked_set_count(metadata)
        if target is not None:
            resolved[int(item_id)] = int(target)
    return resolved


__all__ = [
    "DEFAULT_UNLOCKED_SET_COUNT",
    "KNOWN_EXPANSION_ITEM_IDS",
    "MAX_UNLOCKED_SET_COUNT",
    "expansion_unlocked_set_count",
    "resolved_expansion_item_targets",
]
