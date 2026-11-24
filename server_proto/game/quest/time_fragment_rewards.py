"""Stage3 Time Fragment rewards for Dark Knight and Creator.

The authoritative slot-to-quest mapping is ``etc/questpiece.etc``.  Its region
rewards live under ``n_quest/time_piece/``.  Their display names are localized
by the PVF, so they are not a stable identity field; the adjacent
``time_characterNN`` quests are chain steps, not permanent rewards.  Derive
and validate the reward set from the shared Stage3 PVF so USERINFO1 receives
the frozen client's actual special-reward quest IDs rather than a copied
quest-ID range.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Iterable

from game.quest import quest_rewards


DARK_KNIGHT_JOB_ID = 9
CREATOR_JOB_ID = 10
TIME_FRAGMENT_JOB_IDS = frozenset((DARK_KNIGHT_JOB_ID, CREATOR_JOB_ID))
TIME_FRAGMENT_COUNT_NOTIFY_OPCODE = 0x00C1

_TIME_FRAGMENT_INDEX_PATH = "etc/questpiece.etc"
_TIME_FRAGMENT_PARAMETER_PATH = "n_quest/questparameter.etc"
_TIME_FRAGMENT_QUEST_PATH_PREFIX = "n_quest/time_piece/"
_TARGET_CHARACTER_JOB_IDS = {
    "[demonic swordman]": DARK_KNIGHT_JOB_ID,
    "[creator mage]": CREATOR_JOB_ID,
}
_LOCK = threading.RLock()
_INDEX: dict[int, "TimeFragmentReward"] | None = None
_EFFECT_ROWS: tuple["TimeFragmentEffectRow", ...] | None = None


@dataclass(frozen=True)
class TimeFragmentReward:
    """One external-character Time Fragment quest recognized by Stage3."""

    quest_id: int
    piece_index: int
    quest_name: str
    source_path: str
    target_job_ids: frozenset[int]


@dataclass(frozen=True)
class TimeFragmentEffect:
    """One property entry inside a Stage3 ``[piece reward]`` row."""

    property_name: str
    unit: str
    value: float


@dataclass(frozen=True)
class TimeFragmentEffectRow:
    """One cumulative Time Fragment effect tier from questParameter.etc."""

    required_count: int
    effects: tuple[TimeFragmentEffect, ...]


def _normalise_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lower()


def _target_job_ids(rows: Iterable[str]) -> frozenset[int]:
    jobs: set[int] = set()
    for raw in rows or ():
        tag = quest_rewards._strip_tick(str(raw)).strip().lower()
        job_id = _TARGET_CHARACTER_JOB_IDS.get(tag)
        if job_id is not None:
            jobs.add(int(job_id))
    return frozenset(jobs)


def _piece_quest_rows(rows: Iterable[str]) -> tuple[tuple[int, int], ...]:
    tokens = [quest_rewards._strip_tick(str(raw)).strip() for raw in rows or ()]
    if not tokens or len(tokens) % 2:
        raise ValueError("Stage3 quest-piece list must contain index/quest-id pairs")
    result: list[tuple[int, int]] = []
    seen_indices: set[int] = set()
    seen_quest_ids: set[int] = set()
    for offset in range(0, len(tokens), 2):
        try:
            piece_index = int(tokens[offset], 0)
            quest_id = int(tokens[offset + 1], 0) & 0xFFFF
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid Stage3 quest-piece pair at offset %d" % offset
            ) from exc
        if piece_index <= 0 or quest_id <= 0:
            raise ValueError(
                "non-positive Stage3 quest-piece pair at offset %d" % offset
            )
        if piece_index in seen_indices or quest_id in seen_quest_ids:
            raise ValueError("duplicate Stage3 quest-piece pair at offset %d" % offset)
        seen_indices.add(piece_index)
        seen_quest_ids.add(quest_id)
        result.append((piece_index, quest_id))
    return tuple(result)


def _piece_reward_rows(text: str) -> tuple[TimeFragmentEffectRow, ...]:
    lines = [str(raw).strip() for raw in (text or "").splitlines() if str(raw).strip()]
    rows: list[TimeFragmentEffectRow] = []
    offset = 0
    while offset < len(lines):
        if lines[offset].lower() != "[piece reward]":
            offset += 1
            continue
        offset += 1
        if offset >= len(lines):
            raise ValueError("unterminated Stage3 [piece reward] row")
        try:
            required_count = int(quest_rewards._strip_tick(lines[offset]), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Stage3 [piece reward] required count") from exc
        offset += 1

        effects: list[TimeFragmentEffect] = []
        seen_properties: set[str] = set()
        while offset < len(lines) and lines[offset].lower() != "[/piece reward]":
            property_token = lines[offset]
            if not property_token.startswith("[") or not property_token.endswith("]"):
                raise ValueError(
                    "invalid Stage3 [piece reward] property token: %s" % property_token
                )
            property_name = property_token[1:-1].strip().lower()
            if not property_name or property_name in seen_properties:
                raise ValueError(
                    "duplicate or empty Stage3 [piece reward] property: %s"
                    % property_name
                )
            if offset + 2 >= len(lines):
                raise ValueError("truncated Stage3 [piece reward] property")
            unit = quest_rewards._strip_tick(lines[offset + 1]).strip()
            if unit not in {"+", "%"}:
                raise ValueError(
                    "invalid Stage3 [piece reward] unit for %s: %s"
                    % (property_name, unit)
                )
            try:
                value = float(quest_rewards._strip_tick(lines[offset + 2]))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "invalid Stage3 [piece reward] value for %s" % property_name
                ) from exc
            if not math.isfinite(value):
                raise ValueError(
                    "non-finite Stage3 [piece reward] value for %s" % property_name
                )
            seen_properties.add(property_name)
            effects.append(
                TimeFragmentEffect(
                    property_name=property_name,
                    unit=unit,
                    value=value,
                )
            )
            offset += 3

        if offset >= len(lines) or lines[offset].lower() != "[/piece reward]":
            raise ValueError("unterminated Stage3 [piece reward] row")
        if required_count != len(rows) + 1:
            raise ValueError(
                "Stage3 [piece reward] counts must be contiguous: expected %d, got %d"
                % (len(rows) + 1, required_count)
            )
        if not effects:
            raise ValueError(
                "Stage3 [piece reward] count %d has no effects" % required_count
            )
        rows.append(
            TimeFragmentEffectRow(
                required_count=required_count,
                effects=tuple(effects),
            )
        )
        offset += 1
    if not rows:
        raise ValueError("Stage3 questParameter has no [piece reward] rows")
    return tuple(rows)


def warmup_time_fragment_reward_index() -> dict[str, int]:
    """Build the two-external-class Time Fragment reward index from PVF."""
    global _EFFECT_ROWS, _INDEX
    with _LOCK:
        if _INDEX is None or _EFFECT_ROWS is None:
            reader, _paths, _param = quest_rewards._ensure_index()
            piece_sections = quest_rewards._parse_sections(
                reader.read_text(_TIME_FRAGMENT_INDEX_PATH)
            )
            piece_rows = _piece_quest_rows(piece_sections.get("piece quest list", ()))
            effect_rows = _piece_reward_rows(
                reader.read_text(_TIME_FRAGMENT_PARAMETER_PATH)
            )
            rewards: dict[int, TimeFragmentReward] = {}
            for piece_index, quest_id in piece_rows:
                sections, resolved_path = quest_rewards._read_quest_sections(quest_id)
                path = _normalise_path(resolved_path)
                if not path.startswith(
                    _TIME_FRAGMENT_QUEST_PATH_PREFIX
                ) or not path.endswith(".qst"):
                    raise ValueError(
                        "Stage3 quest-piece %d has unexpected path: %s"
                        % (quest_id, resolved_path)
                    )
                quest_name = quest_rewards._strip_tick(
                    quest_rewards._section_first(sections, "name")
                ).strip()
                if not quest_name:
                    raise ValueError(
                        "Stage3 quest-piece %d has an empty display name" % quest_id
                    )
                target_jobs = _target_job_ids(sections.get("target character", ()))
                if target_jobs != TIME_FRAGMENT_JOB_IDS:
                    raise ValueError(
                        "Stage3 quest-piece %d has unexpected target jobs: %r"
                        % (quest_id, sorted(target_jobs))
                    )
                rewards[quest_id] = TimeFragmentReward(
                    quest_id=quest_id,
                    piece_index=piece_index,
                    quest_name=quest_name,
                    source_path=path,
                    target_job_ids=target_jobs,
                )
            piece_indices = tuple(sorted(reward.piece_index for reward in rewards.values()))
            effect_counts = tuple(row.required_count for row in effect_rows)
            if piece_indices != effect_counts:
                raise ValueError(
                    "Stage3 quest-piece/effect tiers differ: quests=%r effects=%r"
                    % (piece_indices, effect_counts)
                )
            _INDEX = rewards
            _EFFECT_ROWS = effect_rows
        return {
            "ok": 1,
            "time_fragment_quest_count": len(_INDEX),
            "time_fragment_job_count": len(TIME_FRAGMENT_JOB_IDS),
            "time_fragment_effect_row_count": len(_EFFECT_ROWS),
        }


def get_time_fragment_reward_index() -> dict[int, TimeFragmentReward]:
    warmup_time_fragment_reward_index()
    with _LOCK:
        return dict(_INDEX or {})


def get_time_fragment_effect_rows() -> tuple[TimeFragmentEffectRow, ...]:
    warmup_time_fragment_reward_index()
    with _LOCK:
        return tuple(_EFFECT_ROWS or ())


def cumulative_time_fragment_effects(fragment_count: int) -> dict[tuple[str, str], float]:
    """Mirror Stage3 0x030AF210's cumulative property-map merge."""
    try:
        count = max(0, int(fragment_count))
    except (TypeError, ValueError):
        count = 0
    totals: dict[tuple[str, str], float] = {}
    for row in get_time_fragment_effect_rows():
        if row.required_count > count:
            break
        for effect in row.effects:
            key = (effect.property_name, effect.unit)
            totals[key] = float(totals.get(key, 0.0)) + float(effect.value)
    return totals


def finished_time_fragment_reward_ids(
    finished_quest_ids: Iterable[int] | None,
    *,
    job_id: int,
) -> tuple[int, ...]:
    """Return the completed Time Fragment IDs for one supported job."""
    try:
        job = int(job_id)
    except (TypeError, ValueError):
        return ()
    if job not in TIME_FRAGMENT_JOB_IDS:
        return ()
    index = get_time_fragment_reward_index()
    matched: set[int] = set()
    for raw_id in finished_quest_ids or ():
        try:
            quest_id = int(raw_id) & 0xFFFF
        except (TypeError, ValueError):
            continue
        reward = index.get(quest_id)
        if reward is not None and job in reward.target_job_ids:
            matched.add(quest_id)
    return tuple(sorted(matched))


def finished_time_fragment_count(
    finished_quest_ids: Iterable[int] | None,
    *,
    job_id: int,
) -> int:
    """Return the USERINFO1 Time Fragment count for one character."""
    return len(
        finished_time_fragment_reward_ids(
            finished_quest_ids,
            job_id=job_id,
        )
    )


def build_time_fragment_count_notify_body(fragment_count: int) -> bytes:
    """Build the verified S0/00C1 single-u8 body without scheduling it."""
    count = int(fragment_count)
    if not 0 <= count <= 0xFF:
        raise ValueError("Time Fragment count must fit u8: %r" % fragment_count)
    return bytes((count,))


def is_time_fragment_reward_quest(quest_id: int, *, job_id: int) -> bool:
    """Whether finishing this quest requires an immediate USERINFO1 refresh."""
    try:
        qid = int(quest_id) & 0xFFFF
        job = int(job_id)
    except (TypeError, ValueError):
        return False
    reward = get_time_fragment_reward_index().get(qid)
    return reward is not None and job in reward.target_job_ids


def reset_time_fragment_reward_index_for_tests() -> None:
    global _EFFECT_ROWS, _INDEX
    with _LOCK:
        _INDEX = None
        _EFFECT_ROWS = None


__all__ = [
    "CREATOR_JOB_ID",
    "DARK_KNIGHT_JOB_ID",
    "TIME_FRAGMENT_COUNT_NOTIFY_OPCODE",
    "TIME_FRAGMENT_JOB_IDS",
    "TimeFragmentEffect",
    "TimeFragmentEffectRow",
    "TimeFragmentReward",
    "build_time_fragment_count_notify_body",
    "cumulative_time_fragment_effects",
    "finished_time_fragment_count",
    "finished_time_fragment_reward_ids",
    "get_time_fragment_effect_rows",
    "get_time_fragment_reward_index",
    "is_time_fragment_reward_quest",
    "reset_time_fragment_reward_index_for_tests",
    "warmup_time_fragment_reward_index",
]
