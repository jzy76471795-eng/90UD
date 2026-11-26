"""PVF-driven quest-completion ticket batching through normal quest services."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.content_blocking import activate_character_content_blocks
from game.quest.personality_rewards import get_personality_reward_index
from game.quest.quest_index import get_quest_index
from game.quest.quest_session import (
    list_today_daily_completion_ids,
    resolve_acceptable_ids,
    resolve_selected_character_detail,
)
from game.quest.time_fragment_rewards import get_time_fragment_reward_index


QUEST_CLEAR_ACTION = "[quest clear]"
ACHIEVEMENT_CLEAR_ACTION = "[achievement quest clear]"
ANY_QUEST_CLEAR_ACTION = "[any quest clear]"
FIRST_AWAKEN_CLEAR_ACTION = "[first awaken clear]"
SECOND_AWAKEN_CLEAR_ACTION = "[second awaken clear]"

SUPPORTED_QUEST_COMPLETION_ACTIONS = frozenset(
    {
        QUEST_CLEAR_ACTION,
        ACHIEVEMENT_CLEAR_ACTION,
        ANY_QUEST_CLEAR_ACTION,
        FIRST_AWAKEN_CLEAR_ACTION,
        SECOND_AWAKEN_CLEAR_ACTION,
    }
)

SIDE_QUEST_GRADE = "[side]"
ACHIEVEMENT_QUEST_GRADE = "[achievement]"
EPIC_QUEST_GRADE = "[epic]"
SYSTEM_QUEST_GRADE = "[system]"
REALIZATION_QUEST_GRADE = "[realization]"

# The Stage3 earring ticket points only at these two quests. The product scope
# intentionally leaves that ticket inactive while retaining generic
# ``[any quest clear]`` support for other PVF-authored quest lists.
UNSUPPORTED_ANY_QUEST_CLEAR_SETS = frozenset({(2635, 2636)})


def _normalize_tag(value: str) -> str:
    raw = str(value or "").strip().strip("`").lower()
    tags = re.findall(r"\[([^\[\]]+)\]", raw)
    if tags:
        return "[%s]" % " ".join(tags[-1].strip().split())
    normalized = " ".join(raw.split())
    return "[%s]" % normalized if normalized else ""


@dataclass(frozen=True)
class QuestCompletionTicketRule:
    action_type: str
    action_values: tuple[int, ...] = ()
    quest_ids: tuple[int, ...] = ()
    grade_tags: frozenset[str] = frozenset()
    awakening_jcq: int = 0
    include_realization: bool = False

    @classmethod
    def from_action(
        cls,
        action_type: str,
        action_values: tuple[int, ...] | list[int] = (),
        *,
        allow_cn_earring: bool = False,
    ) -> "QuestCompletionTicketRule | None":
        action = _normalize_tag(action_type)
        values = tuple(int(value) for value in (action_values or ()))
        if action == QUEST_CLEAR_ACTION and not values:
            return cls(
                action_type=action,
                grade_tags=frozenset(
                    (
                        EPIC_QUEST_GRADE,
                        SIDE_QUEST_GRADE,
                        SYSTEM_QUEST_GRADE,
                        ACHIEVEMENT_QUEST_GRADE,
                    )
                ),
            )
        if action == ACHIEVEMENT_CLEAR_ACTION and not values:
            return cls(
                action_type=action,
                grade_tags=frozenset((ACHIEVEMENT_QUEST_GRADE,)),
            )
        if action == ANY_QUEST_CLEAR_ACTION:
            quest_ids = tuple(
                dict.fromkeys(
                    int(value) & 0xFFFF
                    for value in values
                    if 0 < int(value) <= 0xFFFF
                )
            )
            if not quest_ids or len(quest_ids) != len(values):
                return None
            if quest_ids in UNSUPPORTED_ANY_QUEST_CLEAR_SETS and not allow_cn_earring:
                return None
            return cls(
                action_type=action,
                action_values=values,
                quest_ids=quest_ids,
            )
        if action == FIRST_AWAKEN_CLEAR_ACTION and values in ((1,), (1, 1)):
            return cls(
                action_type=action,
                action_values=values,
                awakening_jcq=2,
                include_realization=True,
            )
        if action == SECOND_AWAKEN_CLEAR_ACTION and values in ((0,), (0, 1)):
            return cls(
                action_type=action,
                action_values=values,
                awakening_jcq=3,
                include_realization=True,
            )
        return None

    @property
    def is_external_clear(self) -> bool:
        return self.action_type == QUEST_CLEAR_ACTION

    @property
    def target_awakening(self) -> int:
        if self.awakening_jcq == 2:
            return 1
        if self.awakening_jcq == 3:
            return 2
        return 0

    def matches(
        self,
        quest_id: int,
        meta: Any,
        *,
        personality_quest_ids: frozenset[int] = frozenset(),
        time_fragment_quest_ids: frozenset[int] = frozenset(),
    ) -> bool:
        qid = int(quest_id) & 0xFFFF
        if qid <= 0 or meta is None:
            return False
        if self.quest_ids:
            return qid in self.quest_ids
        grade = _normalize_tag(getattr(meta, "grade", ""))
        if self.awakening_jcq:
            if int(getattr(meta, "jcq", 0) or 0) != self.awakening_jcq:
                return False
            return self.include_realization or grade != REALIZATION_QUEST_GRADE
        if grade in self.grade_tags:
            return True
        return bool(
            self.is_external_clear
            and (
                qid in personality_quest_ids
                or qid in time_fragment_quest_ids
            )
        )


@dataclass(frozen=True)
class QuestCompletionBatchResult:
    completed_quest_ids: tuple[int, ...] = ()
    failed_quest_ids: tuple[int, ...] = ()
    skipped_quest_ids: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed_quest_ids

    @property
    def changed(self) -> bool:
        return bool(self.completed_quest_ids)


def _current_character_state(
    server: Any,
    session: Any,
    *,
    fallback_job: int,
    fallback_level: int,
    fallback_grow_type: int,
) -> tuple[int, int, int]:
    detail = resolve_selected_character_detail(server, session)
    return (
        int(detail.get("job") if detail.get("job") is not None else fallback_job),
        max(
            1,
            int(
                detail.get("level")
                if detail.get("level") is not None
                else fallback_level
            ),
        ),
        int(
            detail.get("grow_type")
            if detail.get("grow_type") is not None
            else fallback_grow_type
        )
        & 0xFF,
    )


def auto_complete_quest_completion_ticket(
    server: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int,
    rule: QuestCompletionTicketRule,
    session: Any = None,
    quest_index: Any = None,
    accept_service: Any = None,
    finish_service: Any = None,
) -> QuestCompletionBatchResult:
    """Complete matching active/acceptable quests without per-quest packets."""
    cid = int(character_id)
    if cid <= 0:
        return QuestCompletionBatchResult(
            failed_quest_ids=(0,),
            notes=("invalid_character",),
        )
    if not isinstance(rule, QuestCompletionTicketRule):
        return QuestCompletionBatchResult(
            failed_quest_ids=(0,),
            notes=("invalid_rule",),
        )

    initial_awakening = (int(grow_type) >> 4) & 0x0F
    original_first_grow = int(grow_type) & 0x0F
    if rule.target_awakening and initial_awakening >= rule.target_awakening:
        return QuestCompletionBatchResult(
            notes=("awakening_already_complete=%d" % initial_awakening,),
        )

    repo = server._repo()
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

    personality_ids: frozenset[int] = frozenset()
    time_fragment_ids: frozenset[int] = frozenset()
    if rule.is_external_clear:
        personality_ids = frozenset(
            int(value) & 0xFFFF
            for value in get_personality_reward_index()
        )
        time_fragment_ids = frozenset(
            int(qid) & 0xFFFF
            for qid, reward in get_time_fragment_reward_index().items()
            if int(job) in getattr(reward, "target_job_ids", frozenset())
        )

    completed: list[int] = []
    completed_set: set[int] = set()
    failed: set[int] = set()
    skipped: set[int] = set()
    notes: list[str] = []

    def add_note(value: str) -> None:
        if len(notes) < 64:
            notes.append(str(value))

    max_passes = max(1, len(getattr(index, "metas", {}) or {}) + 1)
    current_job = int(job)
    current_level = max(1, int(level))
    current_grow = int(grow_type) & 0xFF
    if not injected_index:
        activate_character_content_blocks(repo, cid, session=session)
    for _pass_index in range(max_passes):
        current_job, current_level, current_grow = _current_character_state(
            server,
            session,
            fallback_job=current_job,
            fallback_level=current_level,
            fallback_grow_type=current_grow,
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
                job=current_job,
                grow_type=current_grow,
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                active_ids=active_ids,
                daily_completed_ids=daily_completed_ids,
            )
        else:
            acceptable_ids = resolve_acceptable_ids(
                level=current_level,
                job=current_job,
                grow_type=current_grow,
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                active_ids=active_ids,
                daily_completed_ids=daily_completed_ids,
            )

        candidates: list[int] = []
        candidate_seen: set[int] = set()
        for raw_quest_id in active_ids + list(acceptable_ids):
            qid = int(raw_quest_id) & 0xFFFF
            if (
                qid <= 0
                or qid in candidate_seen
                or qid in failed
                or qid in skipped
            ):
                continue
            meta = index.get_meta(qid)
            # A mainline completion ticket must not choose or switch a job.
            # Normal quest acceptance deliberately exposes alternate first-job
            # branches; that menu policy is not suitable for batch clearing.
            if rule.is_external_clear and int(getattr(meta, 'jcq', 0) or 0) in (1, 10, 20):
                quest_branch = int(getattr(meta, 'grow_type', -1))
                if quest_branch <= 0 and not injected_index:
                    from game.quest.quest_rewards import resolve_quest_reward
                    reward=resolve_quest_reward(qid,player_level=current_level,player_job=current_job,player_grow_type=current_grow)
                    if int(reward.chain_type or 0)==1:
                        quest_branch=int(reward.grow_number or 0)
                if quest_branch > 0 and quest_branch != original_first_grow:
                    skipped.add(qid)
                    add_note('preserve_profession skip=%d branch=%d original=%d' % (qid,quest_branch,original_first_grow))
                    continue
            if int(getattr(meta, "min_level", 1) or 1) > current_level:
                continue
            if rule.matches(
                qid,
                meta,
                personality_quest_ids=personality_ids,
                time_fragment_quest_ids=time_fragment_ids,
            ):
                candidates.append(qid)
                candidate_seen.add(qid)
        if not candidates:
            break

        pass_completed = 0
        current_active = set(active_ids)
        for qid in candidates:
            if qid not in current_active:
                _packets, accept_note = accept.handle_accept_quest_001f(
                    struct.pack("<H", qid),
                    session=session,
                    force_accept=False,
                    reward_policy="state_only",
                    emit_packets=False,
                )
                add_note("accept %d %s" % (qid, accept_note))
                current_active = {
                    int(row[1]) & 0xFFFF for row in repo.list_active_quests(cid)
                }
                if qid not in current_active:
                    if "reject=" in str(accept_note):
                        skipped.add(qid)
                    else:
                        failed.add(qid)
                    continue

            _packets, finish_note = finish.handle_finish_quest_0022(
                struct.pack("<H", qid),
                session=session,
                force_complete=True,
                reward_policy="state_only",
                emit_packets=False,
            )
            add_note("finish %d %s" % (qid, finish_note))
            if repo.is_quest_finished(cid, qid):
                current_active.discard(qid)
                if qid not in completed_set:
                    completed.append(qid)
                    completed_set.add(qid)
                pass_completed += 1
            else:
                failed.add(qid)

        if pass_completed <= 0:
            break

    if rule.target_awakening and completed:
        _job, _level, final_grow = _current_character_state(
            server,
            session,
            fallback_job=current_job,
            fallback_level=current_level,
            fallback_grow_type=current_grow,
        )
        final_awakening = (int(final_grow) >> 4) & 0x0F
        if final_awakening < rule.target_awakening:
            failed.add(completed[-1])
            add_note(
                "awakening_postcondition=%d<%d"
                % (final_awakening, rule.target_awakening)
            )

    if len(notes) >= 64:
        notes.append("notes_truncated")
    return QuestCompletionBatchResult(
        completed_quest_ids=tuple(completed),
        failed_quest_ids=tuple(sorted(failed)),
        skipped_quest_ids=tuple(sorted(skipped)),
        notes=tuple(notes),
    )


__all__ = [
    "ACHIEVEMENT_CLEAR_ACTION",
    "ANY_QUEST_CLEAR_ACTION",
    "FIRST_AWAKEN_CLEAR_ACTION",
    "QUEST_CLEAR_ACTION",
    "SECOND_AWAKEN_CLEAR_ACTION",
    "SUPPORTED_QUEST_COMPLETION_ACTIONS",
    "QuestCompletionBatchResult",
    "QuestCompletionTicketRule",
    "auto_complete_quest_completion_ticket",
]
