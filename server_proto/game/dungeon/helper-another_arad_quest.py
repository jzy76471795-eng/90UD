"""Session-local Another Arad mission progress and special rewards.

The Crack-of-Dimension selector binds exactly one PVF mission to one dungeon
run.  These missions are deliberately not inserted into the character's
persistent active-quest table: their progress, timer and claim idempotence
live on ``GameSessionState`` until the run is left or replaced.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.quest.quest_index import QuestMeta, get_quest_index
from game.shop.booster_item_index import parse_booster_item_text
from pvf import pvf_runtime


_CRACK_QUEST_PREFIX = "n_quest/crackofdimension/"
_CRACK_REWARD_TYPE = "crack of dimension"
_SPECIAL_REWARD_CANDIDATE_IDS = (10100301, 10099396)
_INT_TOKEN = re.compile(r"-?\d+")
_QUEST_LOCK = threading.RLock()
_QUEST_SPECS: dict[tuple[int, int], "AnotherAradQuestSpec"] = {}
_REWARD_LOCK = threading.RLock()
_REWARD_CANDIDATES: tuple["AnotherAradRewardCandidate", ...] = ()
_SELECTED_REWARD: Optional["AnotherAradRewardCandidate"] = None


@dataclass(frozen=True)
class AnotherAradHuntRequirement:
    dungeon_selector: int
    min_difficulty: int
    actor_selector: int
    required_count: int
    enemy_type: int = 0


@dataclass(frozen=True)
class AnotherAradQuestSpec:
    quest_id: int
    dungeon_id: int
    name: str
    kind: str
    hunt_requirements: tuple[AnotherAradHuntRequirement, ...] = ()
    min_difficulty: int = -1
    required_location_count: int = 0
    time_limit_seconds: int = 0
    require_no_revive: bool = False


@dataclass
class AnotherAradQuestState:
    spec: AnotherAradQuestSpec
    started_monotonic: float
    accepted: bool = False
    hunt_remaining: list[int] = field(default_factory=list)
    observed_deaths: set[tuple[int, int, int, int]] = field(default_factory=set)
    cleared_cells: set[tuple[int, int]] = field(default_factory=set)
    revive_used: bool = False
    completed: bool = False
    completion_reason: str = ""
    settlement_evaluated: bool = False
    reward_claimed: bool = False
    reward_item_id: int = 0
    reward_granted: tuple[tuple[int, int, int], ...] = ()
    reward_mailed: tuple[tuple[int, int], ...] = ()

    @property
    def progress(self) -> int:
        if self.spec.kind == "hunt":
            required = sum(req.required_count for req in self.spec.hunt_requirements)
            return max(0, required - sum(max(0, value) for value in self.hunt_remaining))
        if self.spec.kind == "locations":
            return len(self.cleared_cells)
        return int(self.completed)

    @property
    def required(self) -> int:
        if self.spec.kind == "hunt":
            return sum(req.required_count for req in self.spec.hunt_requirements)
        if self.spec.kind == "locations":
            return self.spec.required_location_count
        return 1


@dataclass(frozen=True)
class AnotherAradRewardCandidate:
    item_id: int
    name: str
    source_path: str
    has_usable_deadline: bool
    reward_group_count: int
    reward_draw_count: int
    reward_candidate_count: int
    restriction_count: int

    def priority_key(self) -> tuple[int, int, int, int, int, int]:
        """Sort by the user-defined priority, then stable item identity."""
        return (
            0 if self.has_usable_deadline else 1,
            int(self.reward_draw_count),
            int(self.reward_group_count),
            int(self.reward_candidate_count),
            -int(self.restriction_count),
            -int(self.item_id),
        )


@dataclass(frozen=True)
class AnotherAradRewardClaim:
    handled: bool
    completed: bool
    already_claimed: bool = False
    reward_item_id: int = 0
    granted: tuple[tuple[int, int, int], ...] = ()
    mailed: tuple[tuple[int, int], ...] = ()
    note: str = ""


def _normalize_tag(value: str) -> str:
    text = str(value or "").replace("`", "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return " ".join(text.split())


def _int_values(value: str) -> list[int]:
    return [int(token) for token in _INT_TOKEN.findall(str(value or ""))]


def _section_body(text: str, tag: str) -> str:
    match = re.search(
        r"(?is)(?:^|\n)\s*\["
        + re.escape(tag)
        + r"\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
        str(text or ""),
    )
    return match.group(1).strip() if match else ""


def _reward_type(meta: QuestMeta) -> str:
    return _normalize_tag(_section_body(str(meta.raw_text or ""), "reward type"))


def build_another_arad_quest_spec(
    quest_id: int,
    historical_dungeon_id: int,
) -> tuple[Optional[AnotherAradQuestSpec], str]:
    """Parse the selected Crack mission from the boot-warmed quest index."""
    qid = int(quest_id) & 0xFFFF
    dungeon_id = int(historical_dungeon_id)
    with _QUEST_LOCK:
        cached = _QUEST_SPECS.get((qid, dungeon_id))
    if cached is not None:
        return cached, "ok cached"
    meta = get_quest_index().get_meta(qid)
    if meta is None:
        return None, "mission_missing"
    path = str(meta.path or "").replace("\\", "/").strip("/").lower()
    if not path.startswith(_CRACK_QUEST_PREFIX):
        return None, "mission_not_crack"
    if _reward_type(meta) != _CRACK_REWARD_TYPE:
        return None, "mission_reward_type=%s" % (_reward_type(meta) or "missing")

    quest_type = _normalize_tag(meta.quest_type)
    values = _int_values(meta.int_data)
    name = str(meta.name or "")
    if quest_type in {"hunt monster", "hunt enemy"}:
        stride = 5 if quest_type == "hunt enemy" else 4
        if not values or len(values) % stride:
            return None, "hunt_shape=%d" % len(values)
        requirements: list[AnotherAradHuntRequirement] = []
        for offset in range(0, len(values), stride):
            selector_dungeon = int(values[offset])
            min_difficulty = int(values[offset + 1])
            actor_selector = int(values[offset + 2])
            enemy_type = int(values[offset + 3]) if stride == 5 else 0
            count = int(values[offset + 4]) if stride == 5 else int(values[offset + 3])
            if count <= 0:
                return None, "hunt_count=%d" % count
            if selector_dungeon not in {-1, dungeon_id}:
                return None, "hunt_dungeon_mismatch=%d" % selector_dungeon
            requirements.append(
                AnotherAradHuntRequirement(
                    dungeon_selector=selector_dungeon,
                    min_difficulty=min_difficulty,
                    actor_selector=actor_selector,
                    required_count=count,
                    enemy_type=enemy_type,
                )
            )
        return (
            AnotherAradQuestSpec(
                quest_id=qid,
                dungeon_id=dungeon_id,
                name=name,
                kind="hunt",
                hunt_requirements=tuple(requirements),
            ),
                "ok hunt type=%s channels=%d required=%d"
                % (
                    quest_type,
                    len(requirements),
                    sum(row.required_count for row in requirements),
                ),
        )

    if quest_type != "condition under clear":
        return None, "mission_type=%s" % (quest_type or "missing")
    subtype = int(meta.sub_type)
    if subtype in {4, 6}:
        if len(values) != 2 or int(values[0]) != dungeon_id:
            return None, "clear_shape_or_dungeon"
        return (
            AnotherAradQuestSpec(
                quest_id=qid,
                dungeon_id=dungeon_id,
                name=name,
                kind="clear",
                min_difficulty=int(values[1]),
                require_no_revive=subtype == 4,
            ),
            "ok clear subtype=%d" % subtype,
        )
    if subtype == 11:
        if len(values) != 3 or int(values[0]) != dungeon_id or int(values[2]) <= 0:
            return None, "locations_shape_or_dungeon"
        return (
            AnotherAradQuestSpec(
                quest_id=qid,
                dungeon_id=dungeon_id,
                name=name,
                kind="locations",
                min_difficulty=int(values[1]),
                required_location_count=int(values[2]),
            ),
            "ok locations required=%d" % int(values[2]),
        )
    if subtype == 0:
        if len(values) != 3 or int(values[0]) != dungeon_id or int(values[2]) <= 0:
            return None, "timed_shape_or_dungeon"
        return (
            AnotherAradQuestSpec(
                quest_id=qid,
                dungeon_id=dungeon_id,
                name=name,
                kind="timed_clear",
                min_difficulty=int(values[1]),
                time_limit_seconds=int(values[2]),
            ),
            "ok timed limit=%ds" % int(values[2]),
        )
    return None, "clear_subtype=%d" % subtype


def bind_another_arad_quest_state(
    session: Any,
    *,
    quest_id: int,
    historical_dungeon_id: int,
    now_monotonic: Optional[float] = None,
) -> str:
    if session is None:
        return "mission_bind=no_session"
    spec, note = build_another_arad_quest_spec(quest_id, historical_dungeon_id)
    if spec is None:
        session.another_arad_quest_state = None
        return "mission_bind=reject %s" % note
    started = time.monotonic() if now_monotonic is None else float(now_monotonic)
    session.another_arad_quest_state = AnotherAradQuestState(
        spec=spec,
        started_monotonic=started,
        hunt_remaining=[row.required_count for row in spec.hunt_requirements],
    )
    return "mission_bind=ok q=%d kind=%s required=%d %s" % (
        spec.quest_id,
        spec.kind,
        session.another_arad_quest_state.required,
        note,
    )


def clear_another_arad_quest_state(session: Any) -> None:
    if session is not None:
        session.another_arad_quest_state = None


def get_another_arad_quest_state(session: Any) -> Optional[AnotherAradQuestState]:
    state = getattr(session, "another_arad_quest_state", None) if session is not None else None
    return state if isinstance(state, AnotherAradQuestState) else None


def accept_another_arad_quest_state(
    session: Any,
    quest_id: int,
    *,
    now_monotonic: Optional[float] = None,
) -> tuple[bool, str]:
    """Accept only the Crack quest already bound to this dungeon run."""
    state = get_another_arad_quest_state(session)
    if state is None or not bool(getattr(session, "another_arad_active", False)):
        return False, "mirror_mission_accept=skip_no_active_state"
    qid = int(quest_id) & 0xFFFF
    if qid != int(state.spec.quest_id) or qid != int(
        getattr(session, "another_arad_crack_quest_id", 0) or 0
    ):
        return False, "mirror_mission_accept=skip_mismatch q=%d active=%d" % (
            qid,
            state.spec.quest_id,
        )
    if state.accepted:
        return True, "mirror_mission_accept=duplicate q=%d" % qid
    state.accepted = True
    state.started_monotonic = (
        time.monotonic() if now_monotonic is None else float(now_monotonic)
    )
    return True, "mirror_mission_accept=ok q=%d" % qid


def note_another_arad_revive_used(session: Any) -> str:
    state = get_another_arad_quest_state(session)
    if state is None:
        return "mirror_mission_revive=skip"
    if not state.accepted:
        return "mirror_mission_revive=skip_not_accepted q=%d" % state.spec.quest_id
    state.revive_used = True
    return "mirror_mission_revive=1 q=%d" % state.spec.quest_id


def note_another_arad_room_cleared(session: Any, cell_x: int, cell_y: int) -> str:
    state = get_another_arad_quest_state(session)
    if state is None:
        return "mirror_mission_room=skip"
    if not state.accepted:
        return "mirror_mission_room=skip_not_accepted q=%d" % state.spec.quest_id
    cell = (int(cell_x) & 0xFF, int(cell_y) & 0xFF)
    before = len(state.cleared_cells)
    state.cleared_cells.add(cell)
    return "mirror_mission_room q=%d cell=(%d,%d) progress=%d/%d duplicate=%d" % (
        state.spec.quest_id,
        cell[0],
        cell[1],
        state.progress,
        state.required,
        int(len(state.cleared_cells) == before),
    )


def _actor_rank(actor: dict[str, Any]) -> int:
    return int(actor.get("type") or actor.get("mon_type") or actor.get("rank") or 0)


def _actor_code(actor: dict[str, Any]) -> int:
    return int(actor.get("code") or actor.get("monster_code") or 0)


def _actor_enemy_type(actor: dict[str, Any]) -> int:
    explicit = int(actor.get("quest_enemy_type") or 0)
    if explicit > 0:
        return explicit
    kind = str(actor.get("kind") or "").strip().lower()
    rank = _actor_rank(actor)
    if kind in {"special_passive", "passive_object", "passive", "map_passive"} or rank == 9:
        return 3
    if kind in {"ai_character", "ai", "apc"} or rank in {5, 6, 7, 8}:
        return 2
    return 1


def actor_matches_another_arad_selector(
    selector: int,
    actor: dict[str, Any],
) -> bool:
    """Apply the frozen Stage3 selector predicates recovered from IDA."""
    value = int(selector)
    rank = _actor_rank(actor)
    if value == -5:
        return bool(actor.get("is_hostile", False)) and bool(
            actor.get("is_blocking", True)
        )
    if value == -3:
        return rank in {3, 8}
    if value == -11:
        return rank in {1, 2, 3, 6, 7, 8} or bool(
            actor.get("is_special_enemy", False)
        )
    if value > 0:
        return _actor_code(actor) == value
    return False


def note_another_arad_actor_death(
    session: Any,
    actor: dict[str, Any],
    *,
    actor_seq: int,
    map_index: int,
) -> str:
    state = get_another_arad_quest_state(session)
    if state is None or state.spec.kind != "hunt":
        return "mirror_mission_kill=skip"
    if not state.accepted:
        return "mirror_mission_kill=skip_not_accepted q=%d" % state.spec.quest_id
    code = _actor_code(actor)
    death_key = (
        int(getattr(session, "dungeon_run_generation", 0) or 0),
        int(map_index) & 0xFFFFFFFF,
        int(actor_seq) & 0xFFFF,
        code & 0xFFFFFFFF,
    )
    if death_key in state.observed_deaths:
        return "mirror_mission_kill=duplicate q=%d seq=%d" % (
            state.spec.quest_id,
            int(actor_seq),
        )
    state.observed_deaths.add(death_key)

    dungeon_id = int(
        getattr(session, "selected_dungeon_id", 0)
        or getattr(session, "cur_dungeon_id", 0)
        or 0
    )
    difficulty = int(
        getattr(session, "selected_difficulty", 0)
        or getattr(session, "cur_dungeon_difficulty", 0)
        or 0
    )
    matched_channel = -1
    for channel, requirement in enumerate(state.spec.hunt_requirements):
        if channel >= len(state.hunt_remaining) or state.hunt_remaining[channel] <= 0:
            continue
        if requirement.dungeon_selector >= 0 and requirement.dungeon_selector != dungeon_id:
            continue
        if requirement.min_difficulty >= 0 and difficulty < requirement.min_difficulty:
            continue
        if requirement.enemy_type > 0 and requirement.enemy_type != _actor_enemy_type(actor):
            continue
        if not actor_matches_another_arad_selector(requirement.actor_selector, actor):
            continue
        state.hunt_remaining[channel] -= 1
        matched_channel = channel
        break
    if state.hunt_remaining and all(value <= 0 for value in state.hunt_remaining):
        state.completed = True
        state.completion_reason = "hunt_complete"
    return (
        "mirror_mission_kill q=%d selector_match=%d code=%d rank=%d "
        "progress=%d/%d complete=%d"
        % (
            state.spec.quest_id,
            matched_channel,
            code,
            _actor_rank(actor),
            state.progress,
            state.required,
            int(state.completed),
        )
    )


def evaluate_another_arad_settlement(
    session: Any,
    *,
    now_monotonic: Optional[float] = None,
) -> tuple[bool, str]:
    state = get_another_arad_quest_state(session)
    if state is None:
        return False, "mirror_mission_settle=skip"
    if not state.accepted:
        return False, (
            "mirror_mission_settle=skip_not_accepted q=%d" % state.spec.quest_id
        )
    if state.settlement_evaluated:
        return state.completed, (
            "mirror_mission_settle=duplicate q=%d complete=%d reason=%s"
            % (state.spec.quest_id, int(state.completed), state.completion_reason or "-")
        )

    state.settlement_evaluated = True
    dungeon_id = int(
        getattr(session, "selected_dungeon_id", 0)
        or getattr(session, "cur_dungeon_id", 0)
        or 0
    )
    difficulty = int(
        getattr(session, "selected_difficulty", 0)
        or getattr(session, "cur_dungeon_difficulty", 0)
        or 0
    )
    scope_ok = dungeon_id == state.spec.dungeon_id and (
        state.spec.min_difficulty < 0 or difficulty >= state.spec.min_difficulty
    )
    now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
    elapsed = max(0.0, now_value - float(state.started_monotonic))

    if state.spec.kind == "clear":
        state.completed = bool(scope_ok and (not state.spec.require_no_revive or not state.revive_used))
        state.completion_reason = (
            "clear_complete"
            if state.completed
            else "revive_used"
            if state.revive_used
            else "clear_scope_mismatch"
        )
    elif state.spec.kind == "timed_clear":
        state.completed = bool(scope_ok and elapsed <= state.spec.time_limit_seconds)
        state.completion_reason = "timed_complete" if state.completed else "timed_expired"
    elif state.spec.kind == "locations":
        state.completed = bool(
            scope_ok and len(state.cleared_cells) >= state.spec.required_location_count
        )
        state.completion_reason = "locations_complete" if state.completed else "locations_incomplete"
    elif state.spec.kind == "hunt" and not state.completed:
        state.completion_reason = "hunt_incomplete"

    return state.completed, (
        "mirror_mission_settle q=%d kind=%s progress=%d/%d elapsed=%.3f "
        "limit=%d complete=%d reason=%s"
        % (
            state.spec.quest_id,
            state.spec.kind,
            state.progress,
            state.required,
            elapsed,
            state.spec.time_limit_seconds,
            int(state.completed),
            state.completion_reason or "-",
        )
    )


def _restriction_count(metadata: ItemMetadata, text: str) -> int:
    restrictions = len(tuple(metadata.impossible_contents or ()))
    usable_jobs = {_normalize_tag(value) for value in metadata.usable_jobs or ()}
    if usable_jobs and usable_jobs != {"all"}:
        restrictions += 1
    if int(metadata.minimum_level or 0) > 1:
        restrictions += 1
    if _int_values(_section_body(text, "maximum level")):
        restrictions += 1
    if _int_values(_section_body(text, "daily delete item")):
        restrictions += 1
    return restrictions


def parse_another_arad_reward_candidate(item_id: int) -> AnotherAradRewardCandidate:
    metadata = resolve_item_metadata(int(item_id))
    if metadata.item_kind != "stackable" or not metadata.source_path:
        raise ValueError("special reward candidate missing stackable PVF item=%d" % int(item_id))
    text = pvf_runtime.read_text(metadata.source_path)
    definition = parse_booster_item_text(
        int(item_id),
        text,
        stackable_type=metadata.stackable_type,
        source_path=metadata.source_path,
    )
    if not definition.groups or not any(
        int(group.draw_count) > 0 and bool(group.candidates)
        for group in definition.groups
    ):
        raise ValueError(
            "special reward candidate has no drawable reward item=%d"
            % int(item_id)
        )
    return AnotherAradRewardCandidate(
        item_id=int(item_id) & 0xFFFFFFFF,
        name=str(metadata.name or ""),
        source_path=str(metadata.source_path),
        has_usable_deadline=bool(
            int(metadata.usable_period_days or 0) > 0
            or int(metadata.expiration_timestamp or 0) > 0
        ),
        reward_group_count=len(definition.groups),
        reward_draw_count=sum(max(0, int(group.draw_count)) for group in definition.groups),
        reward_candidate_count=sum(len(group.candidates) for group in definition.groups),
        restriction_count=_restriction_count(metadata, text),
    )


def select_another_arad_reward_candidate(
    candidates: Iterable[AnotherAradRewardCandidate],
) -> AnotherAradRewardCandidate:
    rows = tuple(candidates)
    if not rows:
        raise ValueError("Another Arad reward candidate set is empty")
    return max(rows, key=lambda row: row.priority_key())


def warmup_another_arad_quest_index() -> dict[str, int]:
    global _REWARD_CANDIDATES, _SELECTED_REWARD
    index = get_quest_index()
    index.ensure_all_metas_loaded()
    specs: dict[tuple[int, int], AnotherAradQuestSpec] = {}
    kind_counts: dict[str, int] = {}
    errors: list[str] = []
    unscoped_count = 0
    for raw_quest_id in list(getattr(index, "sorted_quest_ids", None) or []):
        quest_id = int(raw_quest_id)
        meta = index.get_meta(quest_id)
        if meta is None:
            continue
        path = str(meta.path or "").replace("\\", "/").strip("/").lower()
        if not path.startswith(_CRACK_QUEST_PREFIX):
            continue
        if _reward_type(meta) != _CRACK_REWARD_TYPE:
            continue
        values = _int_values(meta.int_data)
        if not values or int(values[0]) <= 0:
            # These narrative Crack quests use dungeon selector -1 and are
            # outside the validated 02B6 (historical dungeon, quest) pairs.
            unscoped_count += 1
            continue
        dungeon_id = int(values[0])
        spec, note = build_another_arad_quest_spec(quest_id, dungeon_id)
        if spec is None:
            errors.append("q%d:%s" % (quest_id, note))
            continue
        specs[(spec.quest_id, spec.dungeon_id)] = spec
        kind_counts[spec.kind] = kind_counts.get(spec.kind, 0) + 1
    if errors:
        raise RuntimeError(
            "Another Arad mission PVF validation failed count=%d first=%s"
            % (len(errors), errors[0])
        )
    with _QUEST_LOCK:
        _QUEST_SPECS.clear()
        _QUEST_SPECS.update(specs)
    with _REWARD_LOCK:
        rows = tuple(
            parse_another_arad_reward_candidate(item_id)
            for item_id in _SPECIAL_REWARD_CANDIDATE_IDS
        )
        selected = select_another_arad_reward_candidate(rows)
        _REWARD_CANDIDATES = rows
        _SELECTED_REWARD = selected
        return {
            "another_arad_mission_count": len(specs),
            "another_arad_mission_hunt_count": kind_counts.get("hunt", 0),
            "another_arad_mission_clear_count": kind_counts.get("clear", 0),
            "another_arad_mission_timed_count": kind_counts.get("timed_clear", 0),
            "another_arad_mission_location_count": kind_counts.get("locations", 0),
            "another_arad_mission_unscoped_count": unscoped_count,
            "another_arad_reward_candidate_count": len(rows),
            "another_arad_reward_item_id": int(selected.item_id),
        }


def selected_another_arad_reward_candidate() -> AnotherAradRewardCandidate:
    with _REWARD_LOCK:
        selected = _SELECTED_REWARD
    if selected is None:
        warmup_another_arad_quest_index()
        with _REWARD_LOCK:
            selected = _SELECTED_REWARD
    if selected is None:
        raise RuntimeError("Another Arad reward index did not initialize")
    return selected


def claim_another_arad_reward(
    server: Any,
    session: Any,
    *,
    character_id: int,
) -> AnotherAradRewardClaim:
    state = get_another_arad_quest_state(session)
    if state is None:
        return AnotherAradRewardClaim(False, False, note="mirror_reward=skip_no_state")
    if not state.accepted:
        return AnotherAradRewardClaim(
            True,
            False,
            note="mirror_reward=skip_not_accepted q=%d" % state.spec.quest_id,
        )
    if not state.settlement_evaluated:
        return AnotherAradRewardClaim(
            True,
            False,
            note="mirror_reward=skip_unsettled q=%d" % state.spec.quest_id,
        )
    if not state.completed:
        return AnotherAradRewardClaim(True, False, note="mirror_reward=skip_incomplete")
    if state.reward_claimed:
        return AnotherAradRewardClaim(
            True,
            True,
            already_claimed=True,
            reward_item_id=state.reward_item_id,
            granted=state.reward_granted,
            mailed=state.reward_mailed,
            note="mirror_reward=already_claimed q=%d item=%d" % (
                state.spec.quest_id,
                state.reward_item_id,
            ),
        )
    candidate = selected_another_arad_reward_candidate()
    granted, mailed, detail = server._repo().grant_quest_items(
        int(character_id),
        [(candidate.item_id, 1)],
        quest_id=state.spec.quest_id,
        reason="another_arad_mission_reward",
    )
    if not granted and not mailed:
        return AnotherAradRewardClaim(
            True,
            True,
            reward_item_id=candidate.item_id,
            note="mirror_reward=grant_empty %s" % detail,
        )
    state.reward_claimed = True
    state.reward_item_id = int(candidate.item_id)
    state.reward_granted = tuple(granted)
    state.reward_mailed = tuple(mailed)
    return AnotherAradRewardClaim(
        True,
        True,
        reward_item_id=candidate.item_id,
        granted=tuple(granted),
        mailed=tuple(mailed),
        note=(
            "mirror_reward=claimed q=%d item=%d permanent=%d groups=%d draws=%d "
            "restrictions=%d granted=%d mailed=%d detail=%s"
            % (
                state.spec.quest_id,
                candidate.item_id,
                int(not candidate.has_usable_deadline),
                candidate.reward_group_count,
                candidate.reward_draw_count,
                candidate.restriction_count,
                len(granted),
                len(mailed),
                detail,
            )
        ),
    )


def settle_and_claim_another_arad_reward(
    server: Any,
    session: Any,
    *,
    character_id: int,
    now_monotonic: Optional[float] = None,
) -> AnotherAradRewardClaim:
    completed, settlement_note = evaluate_another_arad_settlement(
        session,
        now_monotonic=now_monotonic,
    )
    state = get_another_arad_quest_state(session)
    if state is None:
        return AnotherAradRewardClaim(False, False, note=settlement_note)
    if not completed:
        return AnotherAradRewardClaim(True, False, note=settlement_note)
    claim = claim_another_arad_reward(
        server,
        session,
        character_id=int(character_id),
    )
    return AnotherAradRewardClaim(
        handled=claim.handled,
        completed=claim.completed,
        already_claimed=claim.already_claimed,
        reward_item_id=claim.reward_item_id,
        granted=claim.granted,
        mailed=claim.mailed,
        note="%s; %s" % (settlement_note, claim.note),
    )


__all__ = [
    "AnotherAradHuntRequirement",
    "AnotherAradQuestSpec",
    "AnotherAradQuestState",
    "AnotherAradRewardCandidate",
    "AnotherAradRewardClaim",
    "accept_another_arad_quest_state",
    "actor_matches_another_arad_selector",
    "bind_another_arad_quest_state",
    "build_another_arad_quest_spec",
    "claim_another_arad_reward",
    "clear_another_arad_quest_state",
    "evaluate_another_arad_settlement",
    "get_another_arad_quest_state",
    "note_another_arad_actor_death",
    "note_another_arad_revive_used",
    "note_another_arad_room_cleared",
    "parse_another_arad_reward_candidate",
    "select_another_arad_reward_candidate",
    "selected_another_arad_reward_candidate",
    "settle_and_claim_another_arad_reward",
    "warmup_another_arad_quest_index",
]
