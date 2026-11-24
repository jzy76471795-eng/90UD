"""Automatic side-quest completion for character level-up tickets."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any

from game.content_blocking import activate_character_content_blocks
from game.chain_loader import load_chain
from game.quest.quest_index import get_quest_index
from game.quest.quest_session import (
    list_today_daily_completion_ids,
    resolve_acceptable_ids,
)


SIDE_QUEST_GRADE = "[side]"


def _grade_tag(value: str) -> str:
    raw = str(value or "").strip().strip("`").lower()
    tags = re.findall(r"\[([^\[\]]+)\]", raw)
    if tags:
        return "[%s]" % tags[-1].strip().lower()
    return "[%s]" % raw if raw and not raw.startswith("[") else raw


def _is_eligible_side_meta(meta: Any, *, level: int) -> bool:
    return bool(
        meta is not None
        and _grade_tag(getattr(meta, "grade", "")) == SIDE_QUEST_GRADE
        and int(getattr(meta, "min_level", 1) or 1) <= int(level)
    )


@dataclass(frozen=True)
class SideQuestAutoCompletionResult:
    completed_quest_ids: tuple[int, ...] = ()
    failed_quest_ids: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed_quest_ids


def auto_complete_level_up_ticket_side_quests(
    server: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    session: Any = None,
    quest_index: Any = None,
    accept_service: Any = None,
    finish_service: Any = None,
) -> SideQuestAutoCompletionResult:
    """Complete every currently eligible ``[side]`` quest through 001F/0022.

    The loop is intentionally iterative: completing one prerequisite rebuilds
    eligibility before the next pass. Internal calls suppress per-quest packet
    storms, while persistence, dependency synchronization and Gold settlement
    remain owned by the normal quest services.
    """
    cid = int(character_id)
    current_level = max(1, int(level))
    if cid <= 0:
        return SideQuestAutoCompletionResult(
            failed_quest_ids=(0,),
            notes=("invalid_character",),
        )

    repo = server._repo()
    # A supplied index is intentionally a narrow test seam.  The production
    # path must use the same resolver that builds the client's 0015 task bar.
    injected_index = quest_index is not None
    index = quest_index if injected_index else get_quest_index()
    index.ensure_all_metas_loaded()
    accept = accept_service
    if accept is None:
        accept = load_chain("opcode001f-accept_quest.py").AcceptQuest001FService(
            server
        )
    finish = finish_service
    if finish is None:
        finish = load_chain("opcode0022-finish_quest.py").FinishQuest0022Service(
            server
        )

    completed: list[int] = []
    failed: set[int] = set()
    notes: list[str] = []
    max_passes = max(1, len(getattr(index, "metas", {}) or {}) + 1)
    for _pass_index in range(max_passes):
        if not injected_index:
            # The acceptable resolver reads the selected character's effective
            # content blocks from the repository-backed runtime state.  Install
            # them before every pass so newly unlocked tasks see current rules.
            activate_character_content_blocks(
                repo,
                cid,
                session=session,
            )
        active_rows = repo.list_active_quests(cid)
        finished_rows = repo.list_finished_quests(cid)
        active_ids = [int(row[1]) & 0xFFFF for row in active_rows]
        finished_ids = [int(row[0]) & 0xFFFF for row in finished_rows]
        finished_flags = {
            int(quest_id) & 0xFFFF: int(flag) & 0xFF
            for quest_id, flag in finished_rows
            if int(flag) != 0
        }
        daily_completed_ids = list_today_daily_completion_ids(repo, cid)
        if injected_index:
            acceptable_ids = index.compute_acceptable(
                level=current_level,
                job=int(job),
                grow_type=int(grow_type),
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                active_ids=active_ids,
                daily_completed_ids=daily_completed_ids,
            )
        else:
            acceptable_ids = resolve_acceptable_ids(
                level=current_level,
                job=int(job),
                grow_type=int(grow_type),
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                active_ids=active_ids,
                daily_completed_ids=daily_completed_ids,
            )

        candidates: list[int] = []
        for quest_id in active_ids + list(acceptable_ids):
            qid = int(quest_id) & 0xFFFF
            if qid <= 0 or qid in candidates or qid in failed:
                continue
            if _is_eligible_side_meta(index.get_meta(qid), level=current_level):
                candidates.append(qid)
        if not candidates:
            break

        pass_completed = 0
        for qid in candidates:
            current_active = {
                int(row[1]) & 0xFFFF for row in repo.list_active_quests(cid)
            }
            if qid not in current_active:
                _packets, accept_note = accept.handle_accept_quest_001f(
                    struct.pack("<H", qid),
                    session=session,
                    force_accept=False,
                    reward_policy="gold_only",
                    emit_packets=False,
                )
                notes.append("accept %d %s" % (qid, accept_note))
                current_active = {
                    int(row[1]) & 0xFFFF for row in repo.list_active_quests(cid)
                }
                if qid not in current_active:
                    failed.add(qid)
                    continue

            _packets, finish_note = finish.handle_finish_quest_0022(
                struct.pack("<H", qid),
                session=session,
                force_complete=True,
                reward_policy="gold_only",
                emit_packets=False,
            )
            notes.append("finish %d %s" % (qid, finish_note))
            if repo.is_quest_finished(cid, qid):
                if qid not in completed:
                    completed.append(qid)
                pass_completed += 1
            else:
                failed.add(qid)

        if pass_completed <= 0:
            break

    return SideQuestAutoCompletionResult(
        completed_quest_ids=tuple(completed),
        failed_quest_ids=tuple(sorted(failed)),
        notes=tuple(notes),
    )


__all__ = [
    "SIDE_QUEST_GRADE",
    "SideQuestAutoCompletionResult",
    "auto_complete_level_up_ticket_side_quests",
]
