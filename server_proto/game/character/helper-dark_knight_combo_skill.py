"""Stage3 Dark Knight combo-skill page codec and enter-room restore helper.

This is separate from ``character_skills.slot``.  The normal/extension quick
slots and the Dark Knight 6-by-5 combo grid are different client models.

Stage3 authority (DFO.exe.i64, 2026-07-28):

* C1/01FD writer ``sub_1E72CA0``::

    u8 page_index
    u8 column_count
    repeat column_count:
        u16 root_skill_id
        u8 child_count
        repeat child_count:
            u16 child_skill_id
    u8 trailing_mode

* S0/01C0 reader chunk ``0x01643DB0``::

    u8 refresh_mode
    u8 page_count
    repeat page_count:
        <page block without trailing_mode>

The UI has six columns.  Each column carries its root skill id followed by up
to five stage skill ids.  A Stage3 native C1/01FD vector captured on 2026-07-31
contains ``child_count=5``; the root is the column identity, not one of those
five stage entries.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence, Tuple

from game.chain_loader import load_chain


DARK_KNIGHT_JOB = 9
DARK_KNIGHT_COMBO_PAGE_COUNT = 2
DARK_KNIGHT_COMBO_COLUMN_COUNT = 6
DARK_KNIGHT_COMBO_MAX_CHILDREN = 5
DARK_KNIGHT_COMBO_TRAILING_MODES = frozenset({0, 2})
NOTI_DARK_KNIGHT_COMBO_SKILL = 0x01C0
DARK_KNIGHT_COMBO_ROOT_SKILL_IDS = tuple(range(118, 124))
DARK_KNIGHT_COMBO_ROOT_SLOT_BY_SKILL = {
    skill_id: slot for slot, skill_id in enumerate(DARK_KNIGHT_COMBO_ROOT_SKILL_IDS)
}


@dataclass(frozen=True)
class DarkKnightComboColumn:
    root_skill_id: int
    child_skill_ids: Tuple[int, ...]


@dataclass(frozen=True)
class DarkKnightComboPage:
    page_index: int
    columns: Tuple[DarkKnightComboColumn, ...]
    page_body: bytes
    trailing_mode: Optional[int] = None


@lru_cache(maxsize=1)
def load_dark_knight_combo_sets() -> Mapping[int, Tuple[int, ...]]:
    """Load the six native combo groups from the warmed Stage3 PVF.

    The six roots are ``comboset0.skl`` through ``comboset5.skl``.  Their
    ``[combo set]`` sections are the authoritative eligibility/default-order
    source.  Common skills such as Backstep are deliberately absent there and
    therefore never enter a generated combo page.
    """
    skill_data = load_chain("helper-skill_data.py")
    out = {}
    for slot, root_skill_id in enumerate(DARK_KNIGHT_COMBO_ROOT_SKILL_IDS):
        static = skill_data.get_skill_static(DARK_KNIGHT_JOB, root_skill_id)
        path = str(getattr(static, "path", "") or "").replace("\\", "/").lower()
        expected_tail = "comboset%s.skl" % slot
        if static is None or not path.endswith(expected_tail):
            out[root_skill_id] = ()
            continue
        values = []
        seen = set()
        for raw_skill_id in getattr(static, "combo_set_skill_ids", ()) or ():
            skill_id = int(raw_skill_id) & 0xFFFF
            if skill_id <= 0 or skill_id in seen:
                continue
            seen.add(skill_id)
            values.append(skill_id)
        out[root_skill_id] = tuple(values)
    return out


def dark_knight_combo_child_skill_ids() -> frozenset[int]:
    """Return every native skill eligible for generated combo phases."""
    return frozenset(
        skill_id
        for combo_set in load_dark_knight_combo_sets().values()
        for skill_id in combo_set
    )


def is_dark_knight_combo_child_skill(skill_id: int) -> bool:
    return (int(skill_id) & 0xFFFF) in dark_knight_combo_child_skill_ids()


def _skill_id_and_level(item: Any) -> tuple[int, int]:
    if isinstance(item, dict):
        skill_id = int(item.get("skill_id", 0) or 0) & 0xFFFF
        level = max(0, int(item.get("level", 0) or 0)) & 0xFF
    else:
        skill_id = int(getattr(item, "skill_id", 0) or 0) & 0xFFFF
        level = max(0, int(getattr(item, "level", 0) or 0)) & 0xFF
    return skill_id, level


def build_dark_knight_default_page_bodies(skills: Sequence[Any]) -> Tuple[bytes, ...]:
    """Build the two default 6x5 pages from learned skills and PVF combo sets."""
    learned = set()
    for item in skills or ():
        try:
            skill_id, level = _skill_id_and_level(item)
        except Exception:
            continue
        if skill_id > 0 and level > 0:
            learned.add(skill_id)

    combo_sets = load_dark_knight_combo_sets()
    if not all(root_skill_id in learned for root_skill_id in DARK_KNIGHT_COMBO_ROOT_SKILL_IDS):
        return ()
    if not any(combo_sets.values()):
        return ()

    page_bodies = []
    for page_index in range(DARK_KNIGHT_COMBO_PAGE_COUNT):
        body = bytearray((page_index, DARK_KNIGHT_COMBO_COLUMN_COUNT))
        for root_skill_id in DARK_KNIGHT_COMBO_ROOT_SKILL_IDS:
            children = [
                skill_id
                for skill_id in combo_sets.get(root_skill_id, ())
                if skill_id in learned
            ][:DARK_KNIGHT_COMBO_MAX_CHILDREN]
            body += struct.pack("<HB", root_skill_id, len(children))
            for child_skill_id in children:
                body += struct.pack("<H", child_skill_id)
        page_bodies.append(bytes(body))
    return tuple(page_bodies)


def ensure_dark_knight_combo_skill_pages(
    repo: Any,
    character_id: int,
    job: int,
    skills: Sequence[Any],
    *,
    force_reset: bool = False,
) -> tuple[int, str]:
    """Seed or narrowly repair persisted combo pages for one Dark Knight."""
    cid = int(character_id or 0)
    job_i = int(job or 0)
    if job_i != DARK_KNIGHT_JOB:
        return 0, "dark_knight_combo_seed skip=job job=%s" % job_i
    if repo is None or cid <= 0:
        return 0, "dark_knight_combo_seed skip=no_repo_or_cid"

    defaults = build_dark_knight_default_page_bodies(skills)
    if len(defaults) != DARK_KNIGHT_COMBO_PAGE_COUNT:
        return 0, "dark_knight_combo_seed skip=no_pvf_defaults cid=%s" % cid

    rows_by_page = {
        int(row.get("page_index", -1)): row
        for row in repo.load_dark_knight_combo_skill_pages(cid) or ()
    }
    changed = 0
    actions = []
    for page_index, default_body in enumerate(defaults):
        row = rows_by_page.get(page_index)
        target_body = default_body
        action = "seed"
        trailing_mode = 0
        if row is not None and not force_reset:
            trailing_mode = int(row.get("trailing_mode", 0) or 0)
            try:
                saved = parse_dark_knight_combo_page_body(bytes(row.get("body") or b""))
                # Stage3's native writer emits valid sparse pages, including a
                # two-root page with no children.  Parsed pages are therefore
                # player-authored truth and must survive entry byte-for-byte.
                target_body = saved.page_body
                action = "keep"
            except (TypeError, ValueError):
                action = "repair_invalid"
        elif force_reset:
            action = "reset"

        if row is None or force_reset or action.startswith("repair"):
            changed += int(
                repo.upsert_dark_knight_combo_skill_page(
                    cid,
                    page_index=page_index,
                    body=target_body,
                    trailing_mode=trailing_mode if trailing_mode in (0, 2) else 0,
                )
                or 0
            )
        actions.append("p%s=%s" % (page_index, action))

    return changed, (
        "dark_knight_combo_seed cid=%s changed=%s %s"
        % (cid, changed, ",".join(actions))
    )


def _parse_page_prefix(data: bytes) -> tuple[int, Tuple[DarkKnightComboColumn, ...], int]:
    if len(data) < 2:
        raise ValueError("combo page is shorter than page/count")

    page_index = int(data[0])
    if page_index >= DARK_KNIGHT_COMBO_PAGE_COUNT:
        raise ValueError("combo page_index must be 0 or 1")

    column_count = int(data[1])
    if column_count > DARK_KNIGHT_COMBO_COLUMN_COUNT:
        raise ValueError("combo column_count exceeds six")

    offset = 2
    columns = []
    for _column_index in range(column_count):
        if offset + 3 > len(data):
            raise ValueError("combo column header is truncated")
        root_skill_id = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        child_count = int(data[offset])
        offset += 1

        if root_skill_id == 0:
            raise ValueError("combo root_skill_id must be nonzero")
        if child_count > DARK_KNIGHT_COMBO_MAX_CHILDREN:
            raise ValueError("combo child_count exceeds five")
        child_end = offset + child_count * 2
        if child_end > len(data):
            raise ValueError("combo child list is truncated")

        child_skill_ids = tuple(
            struct.unpack_from("<H", data, offset + child_index * 2)[0]
            for child_index in range(child_count)
        )
        if any(skill_id == 0 for skill_id in child_skill_ids):
            raise ValueError("combo child_skill_id must be nonzero")
        offset = child_end
        columns.append(
            DarkKnightComboColumn(
                root_skill_id=root_skill_id,
                child_skill_ids=child_skill_ids,
            )
        )

    return page_index, tuple(columns), offset


def parse_dark_knight_combo_save_body(body: bytes) -> DarkKnightComboPage:
    """Parse one exact C1/01FD Stage3 body."""
    data = bytes(body or b"")
    page_index, columns, offset = _parse_page_prefix(data)
    if offset + 1 != len(data):
        raise ValueError("combo save body must end with one trailing_mode byte")
    trailing_mode = int(data[offset])
    if trailing_mode not in DARK_KNIGHT_COMBO_TRAILING_MODES:
        raise ValueError("combo trailing_mode must be 0 or 2")
    return DarkKnightComboPage(
        page_index=page_index,
        columns=columns,
        page_body=data[:offset],
        trailing_mode=trailing_mode,
    )


def parse_dark_knight_combo_page_body(body: bytes) -> DarkKnightComboPage:
    """Parse the canonical page block stored for S0/01C0 restoration."""
    data = bytes(body or b"")
    page_index, columns, offset = _parse_page_prefix(data)
    if offset != len(data):
        raise ValueError("combo page body has trailing bytes")
    return DarkKnightComboPage(
        page_index=page_index,
        columns=columns,
        page_body=data,
    )


def build_dark_knight_combo_notification(page_bodies: Sequence[bytes]) -> bytes:
    """Build the exact S0/01C0 body from one or two canonical page blocks."""
    by_page = {}
    for page_body in page_bodies or ():
        parsed = parse_dark_knight_combo_page_body(page_body)
        by_page[parsed.page_index] = parsed.page_body
    if not by_page:
        raise ValueError("combo notification requires at least one page")

    ordered = [by_page[page] for page in sorted(by_page)]
    return bytes([0, len(ordered)]) + b"".join(ordered)


def load_dark_knight_combo_notification(
    repo: Any,
    character_id: int,
    job: int,
) -> tuple[Optional[bytes], str]:
    """Load saved pages and build an enter-room S0/01C0 body for job 9 only."""
    cid = int(character_id or 0)
    job_i = int(job or 0)
    if job_i != DARK_KNIGHT_JOB:
        return None, "dark_knight_combo skip=job job=%s" % job_i
    if repo is None or cid <= 0:
        return None, "dark_knight_combo skip=no_repo_or_cid"

    rows = repo.load_dark_knight_combo_skill_pages(cid)
    valid_pages = []
    invalid_pages = 0
    for row in rows or ():
        try:
            parsed = parse_dark_knight_combo_page_body(bytes(row.get("body") or b""))
        except (TypeError, ValueError):
            invalid_pages += 1
            continue
        if int(row.get("page_index", parsed.page_index)) != parsed.page_index:
            invalid_pages += 1
            continue
        valid_pages.append(parsed.page_body)

    if not valid_pages:
        return None, (
            "dark_knight_combo skip=no_saved_pages cid=%s invalid=%s"
            % (cid, invalid_pages)
        )

    body = build_dark_knight_combo_notification(valid_pages)
    return body, (
        "dark_knight_combo S0/01C0 cid=%s pages=%s invalid=%s plain_len=%s"
        % (cid, len(valid_pages), invalid_pages, len(body))
    )


__all__ = [
    "DARK_KNIGHT_JOB",
    "DARK_KNIGHT_COMBO_PAGE_COUNT",
    "DARK_KNIGHT_COMBO_COLUMN_COUNT",
    "DARK_KNIGHT_COMBO_MAX_CHILDREN",
    "DARK_KNIGHT_COMBO_TRAILING_MODES",
    "DARK_KNIGHT_COMBO_ROOT_SKILL_IDS",
    "DARK_KNIGHT_COMBO_ROOT_SLOT_BY_SKILL",
    "NOTI_DARK_KNIGHT_COMBO_SKILL",
    "DarkKnightComboColumn",
    "DarkKnightComboPage",
    "parse_dark_knight_combo_save_body",
    "parse_dark_knight_combo_page_body",
    "build_dark_knight_combo_notification",
    "load_dark_knight_combo_sets",
    "dark_knight_combo_child_skill_ids",
    "is_dark_knight_combo_child_skill",
    "build_dark_knight_default_page_bodies",
    "ensure_dark_knight_combo_skill_pages",
    "load_dark_knight_combo_notification",
]
