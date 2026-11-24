# -*- coding: utf-8 -*-
"""Account-wide adventure-group progression and overview statistics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from game.character.character_stats import decode_grow_type
from game.chain_loader import load_chain
from pvf.adventure_group_index import (
    AdventureGroupBenefits,
    AdventureGroupSummary,
    get_adventure_group_index,
)


EXPERTISE_CHARACTER_LEVEL = 90
ADVENTURE_GROUP_EFFECT_MIN_LEVEL = 2
_STANDARD_EXPERTISE_GROUPS = (
    (0, (1, 2, 3, 4)),
    (11, (1, 2, 3, 4)),
    (7, (1, 2, 3, 4)),
    (1, (1, 2, 3, 4)),
    (2, (1, 2, 3, 4)),
    (5, (1, 2, 3, 4)),
    (8, (1, 2, 3, 4, 5)),
    (3, (1, 2, 3, 4)),
    (4, (1, 2, 3, 4)),
    (14, (1, 2, 3, 4)),
    (6, (1, 2, 3, 4)),
    (12, (1, 2, 3, 4)),
    (13, (1, 2)),
)
EXPERTISE_BRANCH_ORDER = tuple(
    (job_key, grow_key)
    for job_key, grow_keys in _STANDARD_EXPERTISE_GROUPS
    for grow_key in grow_keys
) + ((10, 1), (9, 1))
_EXPERTISE_BRANCH_RANK = {
    branch: index for index, branch in enumerate(EXPERTISE_BRANCH_ORDER)
}
# Frozen Script.pvf terminal rows: AntonHeart, Luke_Dark_1,
# ZTowerOfDespair100, and Grief/5214_100 respectively.
SPECIAL_CLEAR_STAT_BY_DUNGEON_ID = {
    220: "anton_phase2_clears",
    5062: "luke_phase2_clears",
    11107: "tower_despair_clears",
    5214: "tower_grief_clears",
}


@dataclass(frozen=True)
class AdventureGroupCounters:
    consecutive_login_days: int = 0
    optimal_dungeon_clears: int = 0
    anton_phase2_clears: int = 0
    luke_phase2_clears: int = 0
    rank_arena_wins: int = 0
    tower_despair_clears: int = 0
    tower_grief_clears: int = 0


@dataclass(frozen=True)
class AdventureGroupExpertiseSlot:
    job_key: int
    grow_key: int
    expertise_level: int
    character_id: int = 0
    character_name: str = ""


@dataclass(frozen=True)
class AdventureGroupOverview:
    created_date_yyyymmdd: int
    character_count: int
    used_points: int
    progression: AdventureGroupSummary
    counters: AdventureGroupCounters
    expertise_slots: tuple[AdventureGroupExpertiseSlot, ...] = ()

    def describe(self) -> str:
        return (
            "created=%d characters=%d used_points=%d %s "
            "login=%d optimal=%d anton=%d luke=%d arena=%d despair=%d grief=%d "
            "expertise_slots=%d"
            % (
                self.created_date_yyyymmdd,
                self.character_count,
                self.used_points,
                self.progression.describe(),
                self.counters.consecutive_login_days,
                self.counters.optimal_dungeon_clears,
                self.counters.anton_phase2_clears,
                self.counters.luke_phase2_clears,
                self.counters.rank_arena_wins,
                self.counters.tower_despair_clears,
                self.counters.tower_grief_clears,
                len(self.expertise_slots),
            )
        )


def _row_value(row: Any, name: str, default: Any = 0) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def is_recommended_dungeon_level(
    character_level: int,
    minimum_required_level: int,
    basis_level: int,
) -> bool:
    """PVF optimal-level rule: entry level through recommended basis level."""
    level = int(character_level or 0)
    minimum = int(minimum_required_level or 0)
    maximum = int(basis_level or 0)
    return level > 0 and minimum > 0 and maximum >= minimum and minimum <= level <= maximum


def calculate_expertise_slots(
    characters: Iterable[Any],
    character_exp_thresholds: Sequence[int] = (),
) -> tuple[AdventureGroupExpertiseSlot, ...]:
    """Project characters onto the 53 Stage3 Expertise descriptor branches."""
    # Retained only for callers compiled against the earlier adapter signature.
    del character_exp_thresholds
    index = get_adventure_group_index()
    best: dict[tuple[int, int], AdventureGroupExpertiseSlot] = {}
    for row in characters:
        character_level = _nonnegative_int(_row_value(row, "level"))
        if character_level < EXPERTISE_CHARACTER_LEVEL:
            continue
        try:
            job_key = int(_row_value(row, "job"))
            packed_grow_type = int(_row_value(row, "grow_type"))
        except (TypeError, ValueError):
            continue
        if job_key in (9, 10):
            grow_key = 1
        else:
            grow_key = int(decode_grow_type(packed_grow_type)[0])
        key = (job_key, grow_key)
        if key not in _EXPERTISE_BRANCH_RANK:
            continue

        expertise_exp = _nonnegative_int(_row_value(row, "expertise_exp"))
        expertise_level = index.expertise_level_for_exp(expertise_exp)
        if expertise_level <= 0:
            continue
        slot = AdventureGroupExpertiseSlot(
            job_key=job_key,
            grow_key=grow_key,
            expertise_level=expertise_level,
            character_id=_nonnegative_int(_row_value(row, "character_id")),
            character_name=str(_row_value(row, "name", "") or ""),
        )
        previous = best.get(key)
        if previous is None or (
            slot.expertise_level,
            -slot.character_id,
        ) > (
            previous.expertise_level,
            -previous.character_id,
        ):
            best[key] = slot
    return tuple(
        best[key]
        for key in EXPERTISE_BRANCH_ORDER
        if key in best
    )


def load_adventure_group_overview(
    repo: Any,
    account_id: int,
) -> AdventureGroupOverview:
    aid = int(account_id)
    if aid <= 0:
        raise ValueError("adventure-group account id is missing")
    loader = getattr(repo, "load_adventure_group_account_snapshot", None)
    if not callable(loader):
        raise AttributeError("repository has no adventure-group snapshot reader")
    raw = loader(aid)
    if not raw:
        raise ValueError("adventure-group account does not exist: %d" % aid)

    characters = tuple(raw.get("characters") or ())
    exp_helper = load_chain("helper-character_exp.py")
    character_exp_thresholds = tuple(exp_helper.load_exp_thresholds())
    index = get_adventure_group_index()
    progression = index.calculate_progress(
        (
            (
                _nonnegative_int(_row_value(row, "level")),
                _nonnegative_int(_row_value(row, "exp")),
            )
            for row in characters
        ),
        character_exp_thresholds,
    )
    created_date = _nonnegative_int(raw.get("created_date_yyyymmdd"))
    if not 10_000_000 <= created_date <= 99_999_999:
        raise ValueError("invalid account creation date: %r" % created_date)
    counters = AdventureGroupCounters(
        consecutive_login_days=_nonnegative_int(raw.get("consecutive_login_days")),
        optimal_dungeon_clears=_nonnegative_int(raw.get("optimal_dungeon_clears")),
        anton_phase2_clears=_nonnegative_int(raw.get("anton_phase2_clears")),
        luke_phase2_clears=_nonnegative_int(raw.get("luke_phase2_clears")),
        rank_arena_wins=_nonnegative_int(raw.get("rank_arena_wins")),
        tower_despair_clears=_nonnegative_int(raw.get("tower_despair_clears")),
        tower_grief_clears=_nonnegative_int(raw.get("tower_grief_clears")),
    )
    return AdventureGroupOverview(
        created_date_yyyymmdd=created_date,
        character_count=len(characters),
        used_points=_nonnegative_int(raw.get("used_points")),
        progression=progression,
        counters=counters,
        expertise_slots=calculate_expertise_slots(
            characters,
        ),
    )


def record_adventure_group_login(
    repo: Any,
    account_id: int,
    *,
    date_yyyymmdd: int | None = None,
) -> dict[str, int]:
    day = (
        int(datetime.now().strftime("%Y%m%d"))
        if date_yyyymmdd is None
        else int(date_yyyymmdd)
    )
    return dict(repo.record_adventure_group_login(int(account_id), day))


def record_adventure_group_dungeon_clear(
    repo: Any,
    account_id: int,
    *,
    dungeon_id: int,
    recommended_dungeon: bool,
) -> tuple[dict[str, int], str]:
    special_column = SPECIAL_CLEAR_STAT_BY_DUNGEON_ID.get(int(dungeon_id))
    deltas = {
        "optimal_dungeon_clears": 1 if recommended_dungeon else 0,
        "anton_phase2_clears": 1 if special_column == "anton_phase2_clears" else 0,
        "luke_phase2_clears": 1 if special_column == "luke_phase2_clears" else 0,
        "tower_despair_clears": 1 if special_column == "tower_despair_clears" else 0,
        "tower_grief_clears": 1 if special_column == "tower_grief_clears" else 0,
    }
    snapshot = repo.increment_adventure_group_statistics(
        int(account_id),
        **deltas,
    )
    return dict(snapshot), special_column or "none"


def calculate_adventure_group_for_account(
    repo: Any,
    account_id: int,
) -> AdventureGroupSummary:
    aid = int(account_id)
    if aid <= 0:
        raise ValueError("adventure-group account id is missing")

    progress_loader = getattr(repo, "list_character_progress_by_account", None)
    if callable(progress_loader):
        progress = progress_loader(aid)
    else:
        roster_loader = getattr(repo, "list_by_account", None)
        if not callable(roster_loader):
            raise AttributeError("repository has no account character progress reader")
        progress = [
            (
                int(getattr(row, "level", 0) or 0),
                int(getattr(row, "exp", 0) or 0),
            )
            for row in roster_loader(aid)
        ]
    exp_helper = load_chain("helper-character_exp.py")
    return get_adventure_group_index().calculate_progress(
        progress,
        exp_helper.load_exp_thresholds(),
    )


def calculate_adventure_group_for_character(
    repo: Any,
    character: Any,
) -> AdventureGroupSummary:
    account_id = int(getattr(character, "account_id", 0) or 0)
    if account_id <= 0:
        character_id = int(getattr(character, "character_id", 0) or 0)
        account_id = int(repo.get_account_id_for_character(character_id) or 0)
    return calculate_adventure_group_for_account(repo, account_id)


def calculate_adventure_group_benefits_for_account(
    repo: Any,
    account_id: int,
) -> AdventureGroupBenefits:
    progression = calculate_adventure_group_for_account(repo, account_id)
    return get_adventure_group_index().benefits_for_level(
        progression.adventure_level
    )


def calculate_adventure_group_benefits_for_character(
    repo: Any,
    character: Any,
) -> AdventureGroupBenefits:
    progression = calculate_adventure_group_for_character(repo, character)
    return get_adventure_group_index().benefits_for_level(
        progression.adventure_level
    )


def project_adventure_group_effect_level(adventure_level: int) -> int:
    """Project the account level onto Stage3's USERINFO1 effect field.

    The frozen client resolves this field through the PVF benefit table before
    rebuilding type ``0x65`` and status ``135``. Level zero has no table row,
    so the native reader returns before creating an effect or HUD status. The
    first non-zero benefit is level two; projecting levels zero and one to zero
    therefore preserves their attributes while keeping the HUD path inactive.
    """

    level = int(adventure_level)
    if not 0 <= level <= 0xFFFFFFFF:
        raise ValueError(
            "adventure-group effect level must fit u32: %r" % adventure_level
        )
    if level < ADVENTURE_GROUP_EFFECT_MIN_LEVEL:
        return 0
    return level


__all__ = [
    "ADVENTURE_GROUP_EFFECT_MIN_LEVEL",
    "AdventureGroupCounters",
    "AdventureGroupExpertiseSlot",
    "AdventureGroupOverview",
    "EXPERTISE_BRANCH_ORDER",
    "EXPERTISE_CHARACTER_LEVEL",
    "SPECIAL_CLEAR_STAT_BY_DUNGEON_ID",
    "calculate_adventure_group_benefits_for_account",
    "calculate_adventure_group_benefits_for_character",
    "calculate_expertise_slots",
    "calculate_adventure_group_for_account",
    "calculate_adventure_group_for_character",
    "is_recommended_dungeon_level",
    "load_adventure_group_overview",
    "project_adventure_group_effect_level",
    "record_adventure_group_dungeon_clear",
    "record_adventure_group_login",
]
