"""Runtime policy for the current character's persisted content blocks.

Quest packet builders are intentionally shared by several flows and do not all
receive a repository or session argument.  The local server owns one active
Game session, so the selected character's persisted rule set is installed here
before those shared builders run.  Item drops keep the same rule set on the
session object as well, which avoids SQLite reads on the dungeon hot path.
"""
from __future__ import annotations

import threading
from typing import Any, Iterable


_lock = threading.RLock()
_active_character_id = 0
_active_quest_ids = frozenset()
_active_item_ids = frozenset()


def _normalized_ids(values: Iterable[int], *, maximum: int) -> frozenset[int]:
    normalized: set[int] = set()
    for value in values or ():
        try:
            content_id = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < content_id <= int(maximum):
            normalized.add(content_id)
    return frozenset(normalized)


def install_character_content_blocks(
    character_id: int,
    *,
    quest_ids: Iterable[int] = (),
    item_ids: Iterable[int] = (),
    session: Any = None,
) -> tuple[frozenset[int], frozenset[int]]:
    """Install one character's effective rules for shared quest/drop paths."""
    quests = _normalized_ids(quest_ids, maximum=0xFFFF)
    items = _normalized_ids(item_ids, maximum=0xFFFFFFFF)
    with _lock:
        global _active_character_id, _active_quest_ids, _active_item_ids
        _active_character_id = max(0, int(character_id or 0))
        _active_quest_ids = quests
        _active_item_ids = items
    if session is not None:
        session.content_block_character_id = max(0, int(character_id or 0))
        session.blocked_quest_ids = quests
        session.blocked_drop_item_ids = items
    return quests, items


def activate_character_content_blocks(
    repo: Any,
    character_id: int,
    *,
    session: Any = None,
) -> tuple[frozenset[int], frozenset[int]]:
    """Load persisted rules and make them active for the selected character."""
    cid = int(character_id or 0)
    if cid <= 0:
        return install_character_content_blocks(
            0,
            quest_ids=(),
            item_ids=(),
            session=session,
        )
    load_rules = getattr(repo, "list_character_content_block_ids", None)
    if not callable(load_rules):
        # Focused protocol fixtures can intentionally expose only the quest
        # methods they exercise.  Rule activation is database-backed, so a
        # fixture without the repository API has no persisted rules to install.
        return install_character_content_blocks(
            cid,
            quest_ids=(),
            item_ids=(),
            session=session,
        )
    quest_ids, item_ids = load_rules(cid)
    return install_character_content_blocks(
        cid,
        quest_ids=quest_ids,
        item_ids=item_ids,
        session=session,
    )


def active_blocked_quest_ids() -> frozenset[int]:
    with _lock:
        return _active_quest_ids


def active_blocked_item_ids() -> frozenset[int]:
    with _lock:
        return _active_item_ids


def is_quest_blocked(quest_id: int) -> bool:
    quest_id = int(quest_id) & 0xFFFF
    with _lock:
        return quest_id in _active_quest_ids


def is_session_item_drop_blocked(session: Any, item_id: int) -> bool:
    """Return whether an item must be omitted from a ground-drop result."""
    iid = int(item_id) & 0xFFFFFFFF
    if iid <= 0:
        return False
    session_ids = getattr(session, "blocked_drop_item_ids", None)
    if session_ids is not None:
        try:
            return iid in session_ids
        except TypeError:
            pass
    with _lock:
        return iid in _active_item_ids


__all__ = [
    "activate_character_content_blocks",
    "active_blocked_item_ids",
    "active_blocked_quest_ids",
    "install_character_content_blocks",
    "is_quest_blocked",
    "is_session_item_drop_blocked",
]
