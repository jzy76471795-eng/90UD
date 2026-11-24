"""PVF-authoritative linked-dungeon continuation for Stage3 settlement retry.

Attempt 1/3 (2026-08-02): Stage3 live sends committed C1/0048
``state=1, option=0`` and then its native retry path requests the source
dungeon again with C1/0010.  The server records one transition at settlement
and consumes it at that next request.  No additional S2C opcode is introduced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.quest.quest_index import get_quest_index


@dataclass(frozen=True)
class LinkedDungeonRetry:
    source_dungeon_id: int
    target_dungeon_id: int
    difficulty: int
    run_generation: int
    character_id: int


def _session_int(session: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(session, name, default) or default)
    except (TypeError, ValueError):
        return int(default)


def _clear_pending(session: Any) -> bool:
    if session is None:
        return False
    existed = getattr(session, "linked_dungeon_retry_pending", None) is not None
    session.linked_dungeon_retry_pending = None
    return bool(existed)


def clear_linked_dungeon_retry(session: Any, *, reason: str) -> str:
    existed = _clear_pending(session)
    return "linked_next clear=%d reason=%s" % (int(existed), str(reason or "unknown"))


def _deterministic_next(index: Any, source_dungeon_id: int) -> tuple[Any, str]:
    source = index.get_dungeon(int(source_dungeon_id))
    rows = tuple(getattr(source, "linked_next_dungeons", ()) or ())
    if not rows:
        return None, "no_next"
    if len(rows) != 1:
        return None, "non_deterministic_rows=%d" % len(rows)
    row = rows[0]
    target_id = int(getattr(row, "target_dungeon_id", 0) or 0)
    weight = int(getattr(row, "weight", 0) or 0)
    condition = int(getattr(row, "condition", 0) or 0)
    if target_id <= 0 or weight != 100 or condition != -1:
        return None, "unsupported_next=%d/%d/%d" % (
            target_id,
            weight,
            condition,
        )
    target = index.get_dungeon(target_id)
    prev_ids = tuple(
        int(value)
        for value in (getattr(target, "linked_prev_dungeon_ids", ()) or ())
    )
    if int(source_dungeon_id) not in prev_ids:
        return None, "target_prev_mismatch target=%d prev=%s" % (
            target_id,
            ",".join(str(value) for value in prev_ids) or "-",
        )
    if not (getattr(target, "path", "") or ""):
        return None, "target_missing_path=%d" % target_id
    if not (getattr(target, "mazes", ()) or ()):
        return None, "target_missing_maze=%d" % target_id
    return row, "ok"


def arm_linked_dungeon_retry(
    session: Any,
    *,
    quest_index: Any = None,
) -> str:
    """Replace any prior pending transition with the current run's PVF next."""
    _clear_pending(session)
    if session is None:
        return "linked_next arm=0 reason=no_session"
    if not bool(getattr(session, "in_dungeon_run", False)):
        return "linked_next arm=0 reason=not_in_run"
    if _session_int(session, "dungeon_clear_phase") < 2:
        return "linked_next arm=0 reason=not_settled"

    source_id = _session_int(session, "cur_dungeon_id")
    if source_id <= 0:
        source_id = _session_int(session, "selected_dungeon_id")
    if source_id <= 0:
        return "linked_next arm=0 reason=no_source"
    difficulty = _session_int(session, "cur_dungeon_difficulty") & 0xFF
    if getattr(session, "cur_dungeon_difficulty", None) is None:
        difficulty = _session_int(session, "selected_difficulty") & 0xFF

    index = quest_index if quest_index is not None else get_quest_index()
    try:
        row, reason = _deterministic_next(index, source_id)
    except Exception as exc:
        return "linked_next arm=0 source=%d reason=index_%s" % (
            source_id,
            type(exc).__name__,
        )
    if row is None:
        return "linked_next arm=0 source=%d reason=%s" % (source_id, reason)

    target_id = int(row.target_dungeon_id)
    pending = LinkedDungeonRetry(
        source_dungeon_id=source_id,
        target_dungeon_id=target_id,
        difficulty=difficulty,
        run_generation=_session_int(session, "dungeon_run_generation"),
        character_id=_session_int(session, "selected_character_id"),
    )
    session.linked_dungeon_retry_pending = pending
    return (
        "linked_next attempt=1/3 arm=1 source=%d target=%d difficulty=%d generation=%d"
        % (
            pending.source_dungeon_id,
            pending.target_dungeon_id,
            pending.difficulty,
            pending.run_generation,
        )
    )


def resolve_linked_dungeon_retry(
    session: Any,
    requested_dungeon_id: int,
    difficulty: int,
    *,
    quest_index: Any = None,
) -> tuple[int, str]:
    """Consume one pending transition and return the effective dungeon id."""
    requested = int(requested_dungeon_id) & 0xFFFFFFFF
    if session is None:
        return requested, ""
    pending = getattr(session, "linked_dungeon_retry_pending", None)
    if pending is None:
        return requested, ""
    # Every SELECT attempt consumes the capability, including a mismatch.  A
    # stale retry must never redirect a later manual dungeon selection.
    session.linked_dungeon_retry_pending = None
    if not isinstance(pending, LinkedDungeonRetry):
        return requested, "linked_next discard=invalid_pending"

    checks = (
        (requested == pending.source_dungeon_id, "source_mismatch"),
        ((int(difficulty) & 0xFF) == pending.difficulty, "difficulty_mismatch"),
        (
            _session_int(session, "dungeon_run_generation")
            == pending.run_generation,
            "generation_mismatch",
        ),
        (
            _session_int(session, "selected_character_id") == pending.character_id,
            "character_mismatch",
        ),
    )
    for matched, reason in checks:
        if not matched:
            return requested, (
                "linked_next discard=%s requested=%d source=%d target=%d"
                % (
                    reason,
                    requested,
                    pending.source_dungeon_id,
                    pending.target_dungeon_id,
                )
            )

    index = quest_index if quest_index is not None else get_quest_index()
    try:
        row, reason = _deterministic_next(index, pending.source_dungeon_id)
    except Exception as exc:
        return requested, "linked_next discard=index_%s" % type(exc).__name__
    if row is None or int(row.target_dungeon_id) != pending.target_dungeon_id:
        return requested, "linked_next discard=pvf_%s" % reason

    return pending.target_dungeon_id, (
        "linked_next attempt=1/3 redirect source=%d target=%d difficulty=%d generation=%d"
        % (
            pending.source_dungeon_id,
            pending.target_dungeon_id,
            pending.difficulty,
            pending.run_generation,
        )
    )
