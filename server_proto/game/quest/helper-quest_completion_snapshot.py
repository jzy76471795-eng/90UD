"""Authoritative finished-quest snapshot shared by USERINFO1 paths.

Every USERINFO1 resend must use the same database view.  Keeping the full
finished set and the PVF-derived personality subset together prevents one
path from silently sending an empty personality list after a database read
failure or from using a stale session-only quest list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.quest.personality_rewards import finished_personality_reward_ids
from game.quest.profession_completion_guard import filter_profession_completion_rows


@dataclass(frozen=True)
class FinishedQuestSnapshot:
    """Finished quest IDs and the Stage3 personality subset for one character."""

    character_id: int
    finished_quest_ids: tuple[int, ...]
    personality_quest_ids: tuple[int, ...]

    @property
    def finished_count(self) -> int:
        return len(self.finished_quest_ids)

    @property
    def personality_count(self) -> int:
        return len(self.personality_quest_ids)

    def describe(self) -> str:
        ids = ",".join(str(value) for value in self.personality_quest_ids) or "-"
        return (
            "quest_snapshot cid=%s finished=%s personality=%s personality_ids=%s"
            % (self.character_id, self.finished_count, self.personality_count, ids)
        )


def load_finished_quest_snapshot(repo: Any, character_id: int) -> FinishedQuestSnapshot:
    """Read the complete non-zero DB finished set and derive personality IDs."""

    cid = int(character_id)
    rows = filter_profession_completion_rows(repo,cid,repo.list_finished_quests(cid))
    finished = tuple(
        sorted(
            {
                int(quest_id) & 0xFFFF
                for quest_id, _flag in rows
                if int(quest_id) > 0 and int(_flag) != 0
            }
        )
    )
    personality = tuple(finished_personality_reward_ids(finished))
    return FinishedQuestSnapshot(
        character_id=cid,
        finished_quest_ids=finished,
        personality_quest_ids=personality,
    )


__all__ = ["FinishedQuestSnapshot", "load_finished_quest_snapshot"]
