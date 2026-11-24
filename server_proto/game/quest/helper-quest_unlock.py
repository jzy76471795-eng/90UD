"""Local map unlock helper (task doc 7.1)."""
from __future__ import annotations

from typing import Any

from game.quest.quest_common import active_pairs_from_rows, build_clear_quest_list_0164
from game.quest.quest_session import (
    bind_session_quest_state,
    ensure_local_map_unlock_quests,
    hydrate_quest_lists,
    project_client_finished_quest_snapshot,
)


def ensure_unlock_and_finished_ids(
    server: Any,
    *,
    character_id: int,
    job: int = 0,
    level: int = 1,
    grow_type: int = 0,
    session: Any = None,
    full_hydrate: bool = False,
) -> dict[str, object]:
    """Ensure unlock seeds then return finished/active snapshot.

    Default is lightweight (no acceptable-list PVF recompute) for enter-select.
    Set full_hydrate=True for START_MAP-style 0015/023F/0164 body rebuild.
    """
    unlock = ensure_local_map_unlock_quests(server, character_id=int(character_id))
    if full_hydrate:
        data = hydrate_quest_lists(
            server,
            character_id=int(character_id),
            job=int(job),
            level=int(level or 1),
            grow_type=int(grow_type or 0),
            session=session,
            ensure_unlock=False,
        )
        data["unlock"] = unlock
        return data

    repo = server._repo()
    active_rows = repo.list_active_quests(int(character_id))
    active_pairs = active_pairs_from_rows(active_rows)
    finished_projection = project_client_finished_quest_snapshot(
        repo,
        int(character_id),
    )
    finished_ids = list(finished_projection["finished_ids"])
    expired_daily_finished_ids = list(
        finished_projection["expired_daily_finished_ids"]
    )
    bind_session_quest_state(
        session,
        active_pairs=active_pairs,
        finished_ids=finished_ids,
    )
    return {
        "active_pairs": active_pairs,
        "finished_ids": finished_ids,
        "acceptable_ids": [],
        "body_0015": b"",
        "body_023f": b"",
        "body_0164": build_clear_quest_list_0164(finished_ids),
        "unlock": unlock,
        "daily_challenge_claimed_ids": list(
            finished_projection["daily_challenge_claimed_ids"]
        ),
        "today_clear_grade_ids": list(
            finished_projection["today_clear_grade_ids"]
        ),
        "expired_daily_finished_ids": expired_daily_finished_ids,
    }
