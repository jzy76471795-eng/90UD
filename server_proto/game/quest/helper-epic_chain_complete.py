"""Warmed PVF indexes for no-reward main-quest completion paths.

The regular 0022 diagnostic switch retains its chapter/pre-quest grouping.
The Stage3 task-complete button uses C1/0315 and groups modern epic quests by
their PVF parent directory plus filename area token, so an area may span two
``epic_<level>`` prefixes (for example Ghent Outskirts 2118..2130).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

from game.quest.quest_index import get_quest_index


_EPIC_CHAPTER_RE = re.compile(r"(?:^|/)epic_(\d+)_[^/]+\.qst$", re.IGNORECASE)
_EPIC_AREA_RE = re.compile(
    r"^(.+)/epic_(\d+)_(.+?)_(\d+)(?:_[a-z][^/]*)?\.qst$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EpicChainCompletion:
    """Result of a no-reward epic chapter completion transaction."""

    is_epic: bool
    chapter: Optional[int]
    quest_ids: Tuple[int, ...]
    ok: bool
    finished_added: int
    note: str
    chain_key: str = ""


@dataclass(frozen=True)
class EpicAreaCompletion:
    """Result of one C1/0315 no-reward area completion transaction."""

    is_epic: bool
    area_key: str
    quest_ids: Tuple[int, ...]
    ok: bool
    finished_added: int
    note: str


def epic_chapter_from_path(path: str) -> Optional[int]:
    """Return the PVF epic chapter number encoded in a quest path."""

    normalized = str(path or "").replace("\\", "/").strip().lower()
    match = _EPIC_CHAPTER_RE.search(normalized)
    if match is None:
        return None
    try:
        chapter = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return chapter if chapter > 0 else None


def epic_area_key_from_path(path: str) -> Optional[str]:
    """Return ``parent/epic_*_<area>`` for a modern epic quest path.

    Some PVF branches append a named variant after the numeric sequence, such
    as ``_sub02`` or ``_angel``. The variant remains part of the same area.
    """

    normalized = str(path or "").replace("\\", "/").strip(" /").lower()
    match = _EPIC_AREA_RE.fullmatch(normalized)
    if match is None:
        return None
    parent = match.group(1).strip("/")
    area = match.group(3).strip("_")
    try:
        chapter = int(match.group(2))
        sequence = int(match.group(4))
    except (TypeError, ValueError):
        return None
    if not parent or not area or chapter <= 0 or sequence <= 0:
        return None
    return "%s/epic_*_%s" % (parent, area)


def _is_main_quest(meta: Any) -> bool:
    return str(getattr(meta, "grade", "") or "").strip().lower() == "[epic]"


def _is_current_main_quest(quest_index: Any, quest_id: int, meta: Any) -> bool:
    """Apply the warmed episode-sequence identity to EpicQuest consumers."""
    if not _is_main_quest(meta):
        return False
    sequence_ids = getattr(quest_index, "epic_sequence_ids", None)
    if sequence_ids is None:
        return True
    return (int(quest_id) & 0xFFFF) in sequence_ids


def _main_quest_chain_cache(quest_index: Any) -> dict[int, tuple[str, Optional[int], Tuple[int, ...]]]:
    """Build and cache every PVF main-quest chain for one warmed QuestIndex."""

    cached = getattr(quest_index, "_usdof_main_quest_chain_cache", None)
    if isinstance(cached, dict):
        return cached

    quest_index.ensure_all_metas_loaded()
    metas = getattr(quest_index, "metas", {}) or {}
    main_ids = {
        int(raw_id) & 0xFFFF
        for raw_id, meta in metas.items()
        if int(raw_id) > 0
        and _is_current_main_quest(quest_index, int(raw_id), meta)
    }
    by_id: dict[int, tuple[str, Optional[int], Tuple[int, ...]]] = {}
    modern_groups: dict[int, set[int]] = {}
    for qid in main_ids:
        chapter = epic_chapter_from_path(getattr(metas.get(qid), "path", ""))
        if chapter is not None:
            modern_groups.setdefault(chapter, set()).add(qid)
    for chapter, ids in modern_groups.items():
        members = tuple(sorted(ids))
        record = ("epic_%s" % chapter, chapter, members)
        for qid in members:
            by_id[qid] = record

    # Legacy files do not carry a stable epic_<chapter> filename.  Their
    # parsed [pre quest] graph is the narrowest available chain boundary.
    legacy_ids = main_ids.difference(by_id)
    links: dict[int, set[int]] = {qid: set() for qid in legacy_ids}
    for qid in legacy_ids:
        meta = metas.get(qid)
        for group in getattr(meta, "pre_groups", ()) or ():
            for parent_raw in group or ():
                parent = int(parent_raw) & 0xFFFF
                if parent in legacy_ids:
                    links[qid].add(parent)
                    links[parent].add(qid)
    unseen = set(legacy_ids)
    while unseen:
        first = min(unseen)
        component: set[int] = set()
        pending = [first]
        unseen.remove(first)
        while pending:
            qid = pending.pop()
            component.add(qid)
            for linked in links.get(qid, ()):
                if linked in unseen:
                    unseen.remove(linked)
                    pending.append(linked)
        members = tuple(sorted(component))
        record = ("legacy_%s" % members[0], None, members)
        for qid in members:
            by_id[qid] = record

    try:
        setattr(quest_index, "_usdof_main_quest_chain_cache", by_id)
    except Exception:
        pass
    return by_id


def _main_quest_area_cache(quest_index: Any) -> dict[int, tuple[str, Tuple[int, ...]]]:
    """Build and cache modern epic area membership for one warmed QuestIndex."""

    cached = getattr(quest_index, "_usdof_main_quest_area_cache", None)
    if isinstance(cached, dict):
        return cached

    quest_index.ensure_all_metas_loaded()
    metas = getattr(quest_index, "metas", {}) or {}
    groups: dict[str, set[int]] = {}
    for raw_id, meta in metas.items():
        qid = int(raw_id) & 0xFFFF
        if qid <= 0 or not _is_current_main_quest(quest_index, qid, meta):
            continue
        area_key = epic_area_key_from_path(getattr(meta, "path", ""))
        if area_key:
            groups.setdefault(area_key, set()).add(qid)

    by_id: dict[int, tuple[str, Tuple[int, ...]]] = {}
    for area_key, ids in groups.items():
        members = tuple(sorted(ids))
        record = (area_key, members)
        for qid in members:
            by_id[qid] = record
    try:
        setattr(quest_index, "_usdof_main_quest_area_cache", by_id)
    except Exception:
        pass
    return by_id


def warmup_main_quest_chain_index(quest_index: Any = None) -> dict[str, int]:
    """Prebuild chapter and C1/0315 area indexes during mandatory warmup."""

    index = quest_index if quest_index is not None else get_quest_index()
    cache = _main_quest_chain_cache(index)
    area_cache = _main_quest_area_cache(index)
    unique_chains = {record[0] for record in cache.values()}
    modern = {record[0] for record in cache.values() if record[1] is not None}
    unique_areas = {record[0] for record in area_cache.values()}
    return {
        "ok": 1,
        "main_quest_count": len(cache),
        "main_chain_count": len(unique_chains),
        "chapter_chain_count": len(modern),
        "area_quest_count": len(area_cache),
        "area_chain_count": len(unique_areas),
    }


def resolve_main_quest_chain(
    quest_index: Any,
    quest_id: int,
) -> tuple[str, Optional[int], Tuple[int, ...]]:
    """Resolve the warmed PVF main-quest chain containing ``quest_id``."""

    qid = int(quest_id) & 0xFFFF
    try:
        record = _main_quest_chain_cache(quest_index).get(qid)
    except Exception:
        return "", None, ()
    return record if record is not None else ("", None, ())


def resolve_character_main_quest_ids(
    quest_index: Any,
    *,
    job: int,
    level: int,
    grow_type: int = 0,
) -> Tuple[int, ...]:
    """Return PVF main quests applicable to one character at this level.

    This is the audit/write boundary for the local level-90 fixture. It uses
    the same job, target-character, grow-branch and minimum-level rules as the
    warmed quest index, while deliberately ignoring prerequisite completion so
    the repair tool can close an already flattened character state.
    """

    from game.quest.quest_index import (
        _matches_job,
        _matches_target_character,
        _split_grow_type,
    )

    quest_index.ensure_all_metas_loaded()
    level_i = int(level)
    grow_i = int(grow_type)
    first_grow, _awakening = _split_grow_type(grow_i)
    result: list[int] = []
    for raw_id, meta in (getattr(quest_index, "metas", {}) or {}).items():
        qid = int(raw_id) & 0xFFFF
        min_level = int(getattr(meta, "min_level", 0) or 0)
        if (
            qid <= 0
            or not _is_current_main_quest(quest_index, qid, meta)
            or not (0 < min_level < level_i)
        ):
            continue
        if not _matches_target_character(
            getattr(meta, "target_character", ""),
            int(job),
            grow_type=grow_i,
            rules=getattr(meta, "target_character_rules", None) or (),
        ):
            continue
        if not _matches_job(getattr(meta, "job", ""), int(job)):
            continue
        jcq = int(getattr(meta, "jcq", 0) or 0)
        meta_grow = int(getattr(meta, "grow_type", -1) or -1)
        if meta_grow != -1 and jcq not in (1, 10, 20) and grow_i >= 0:
            if meta_grow != int(first_grow):
                continue
        result.append(qid)
    return tuple(sorted(set(result)))


def resolve_main_quest_area(
    quest_index: Any,
    quest_id: int,
) -> tuple[str, Tuple[int, ...]]:
    """Resolve the warmed modern epic area containing ``quest_id``."""

    qid = int(quest_id) & 0xFFFF
    try:
        record = _main_quest_area_cache(quest_index).get(qid)
    except Exception:
        return "", ()
    return record if record is not None else ("", ())


def select_task_complete_main_quest_id(
    *,
    requested_quest_id: int,
    active_quest_ids: Sequence[int] = (),
    finished_quest_ids: Sequence[int] = (),
    quest_index: Any = None,
) -> Optional[int]:
    """Legacy selector retained for explicit chapter-completion A/B tests.

    This helper is not part of C1/0315 handling. C1/001F remains the regular
    accept-quest path, and the production area resolver uses the exact quest ID
    carried by C1/0315.
    """

    index = quest_index if quest_index is not None else get_quest_index()
    requested = int(requested_quest_id) & 0xFFFF
    key, _chapter, members = resolve_main_quest_chain(index, requested)
    if not key or not members:
        try:
            return requested if index.get_meta(requested) is not None else None
        except Exception:
            return None
    finished = {int(raw) & 0xFFFF for raw in finished_quest_ids if int(raw) > 0}
    if any(qid not in finished for qid in members):
        return requested
    for raw in active_quest_ids:
        candidate = int(raw) & 0xFFFF
        candidate_key, _candidate_chapter, candidate_members = resolve_main_quest_chain(
            index, candidate
        )
        if candidate_key and candidate_members:
            return candidate
    return requested


def resolve_epic_chapter_quest_ids(
    quest_index: Any,
    quest_id: int,
) -> tuple[Optional[int], Tuple[int, ...]]:
    """Compatibility view of ``resolve_main_quest_chain`` for chapter callers."""

    _key, chapter, quest_ids = resolve_main_quest_chain(quest_index, quest_id)
    return chapter, quest_ids


def complete_epic_chain_without_rewards(
    repo: Any,
    *,
    character_id: int,
    quest_id: int,
    quest_index: Any = None,
    allow_single_task: bool = False,
) -> EpicChainCompletion:
    """Mark one PVF main chain, or an allowed single task, without rewards."""

    index = quest_index if quest_index is not None else get_quest_index()
    chain_key, chapter, quest_ids = resolve_main_quest_chain(index, quest_id)
    if not chain_key or not quest_ids:
        qid = int(quest_id) & 0xFFFF
        try:
            exists = index.get_meta(qid) is not None
        except Exception:
            exists = False
        if bool(allow_single_task) and exists:
            chain_key, chapter, quest_ids = "single_%s" % qid, None, (qid,)
        else:
            return EpicChainCompletion(
                is_epic=False,
                chapter=None,
                quest_ids=(),
                ok=False,
                finished_added=0,
                note="not_main_quest_chain",
            )

    ok, detail = repo.grant_default_unlock_progress(
        int(character_id),
        quest_ids=quest_ids,
    )
    added = int((detail or {}).get("finished_added", 0) or 0)
    return EpicChainCompletion(
        is_epic=True,
        chapter=chapter,
        quest_ids=quest_ids,
        ok=bool(ok),
        finished_added=max(0, added),
        note=str((detail or {}).get("reason", "unknown")),
        chain_key=chain_key,
    )


def complete_epic_area_without_rewards(
    repo: Any,
    *,
    character_id: int,
    character_level: int,
    quest_id: int,
    quest_index: Any = None,
) -> EpicAreaCompletion:
    """Finish one eligible PVF epic area atomically without rewards."""

    index = quest_index if quest_index is not None else get_quest_index()
    qid = int(quest_id)
    level = int(character_level)
    try:
        target_meta = index.get_meta(qid)
    except Exception:
        target_meta = None
    if target_meta is None or not _is_current_main_quest(index, qid, target_meta):
        return EpicAreaCompletion(False, "", (), False, 0, "not_epic_quest")

    target_min_level = int(getattr(target_meta, "min_level", 0) or 0)
    if target_min_level <= 0 or target_min_level >= level:
        return EpicAreaCompletion(
            True,
            "",
            (),
            False,
            0,
            "target_level_not_below_character",
        )

    area_key, area_ids = resolve_main_quest_area(index, qid)
    if not area_key or not area_ids:
        return EpicAreaCompletion(True, "", (), False, 0, "area_not_found")

    eligible = []
    for member_id in area_ids:
        try:
            meta = index.get_meta(member_id)
            min_level = int(getattr(meta, "min_level", 0) or 0)
        except Exception:
            continue
        if _is_current_main_quest(index, member_id, meta) and 0 < min_level < level:
            eligible.append(int(member_id) & 0xFFFF)
    quest_ids = tuple(sorted(set(eligible)))
    if qid not in quest_ids:
        return EpicAreaCompletion(True, area_key, (), False, 0, "target_not_eligible")

    ok, detail = repo.grant_default_unlock_progress(
        int(character_id),
        quest_ids=quest_ids,
    )
    added = int((detail or {}).get("finished_added", 0) or 0)
    return EpicAreaCompletion(
        is_epic=True,
        area_key=area_key,
        quest_ids=quest_ids,
        ok=bool(ok),
        finished_added=max(0, added),
        note=str((detail or {}).get("reason", "unknown")),
    )
