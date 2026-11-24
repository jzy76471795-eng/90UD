"""Level milestone title acquisition through the proven titlebook flow.

The two Stage3 titlebook entries are ordinary ``[item]`` quest rewards in
PVF, so routing them through generic item delivery would put them in list0.
This helper keeps the quest-completion state, titlebook slot, and 0168
acquisition animation on the titlebook path instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from game.chain_loader import load_chain
from game.titlebook.titlebook_index import get_titlebook_index


@dataclass(frozen=True)
class LevelTitleMilestone:
    threshold_level: int
    quest_id: int
    item_id: int


@dataclass(frozen=True)
class LevelTitleGrant:
    character_id: int
    threshold_level: int
    quest_id: int
    item_id: int
    category: int
    book_index: int
    row: dict[str, int]


# Stage3 shared-PVF evidence, 2026-08-02:
#   level 70 -> title item 26628 -> titlebook quest 6531 -> general slot 46
#   level 85 -> title item 100331240 -> titlebook quest 6697 -> general slot 65
LEVEL_TITLE_MILESTONES: tuple[LevelTitleMilestone, ...] = (
    LevelTitleMilestone(70, 6531, 26628),
    LevelTitleMilestone(85, 6697, 100331240),
)

_title_grant = load_chain("helper-titlebook_grant.py")


def has_crossed_level_title_threshold(previous_level: int, current_level: int) -> bool:
    """Return whether this level transition can unlock either fixed milestone."""
    old = max(1, int(previous_level or 1))
    new = max(1, int(current_level or 1))
    return any(old < milestone.threshold_level <= new for milestone in LEVEL_TITLE_MILESTONES)


def should_backfill_level_title_rewards(current_level: int) -> bool:
    """Return whether an existing character is eligible for the one-time repair.

    New rewards are issued on the exact 70/85 threshold transitions.  The
    historical repair intentionally starts only above level 85, where both
    milestones must already have been crossed.
    """
    return max(1, int(current_level or 1)) > 85


def claim_level_title_rewards(
    repo: Any,
    *,
    character_id: int,
    previous_level: int,
    current_level: int,
    backfill: bool = False,
) -> tuple[list[LevelTitleGrant], str]:
    """Persist only missing milestone titles and their acquisition-quest flags.

    Normal level-up only examines crossed thresholds.  Backfill is bounded to
    the same two milestones, so a high-level character login never scans the
    quest catalog or the whole titlebook index.
    """
    cid = int(character_id)
    old = max(1, int(previous_level or 1))
    new = max(1, int(current_level or 1))
    mode = "backfill" if backfill else "levelup"
    if cid <= 0:
        return [], "level_title %s skip=character_missing" % mode
    if backfill and not should_backfill_level_title_rewards(new):
        return [], "level_title backfill skip=level_not_above_85 level=%s" % new

    candidates = [
        milestone
        for milestone in LEVEL_TITLE_MILESTONES
        if milestone.threshold_level <= new
        and (backfill or old < milestone.threshold_level <= new)
    ]
    if not candidates:
        return [], "level_title %s skip=no_threshold old=%s new=%s" % (
            mode,
            old,
            new,
        )

    index = get_titlebook_index()
    try:
        book_rows = repo.list_titlebook_items(cid)
        worn = repo.get_equipped_at_slot(cid, 13)
    except Exception as exc:
        return [], "level_title %s lookup_error=%s" % (mode, type(exc).__name__)

    owned_item_ids = {
        int(row.get("item_id", 0) or 0)
        for row in book_rows
        if int(row.get("item_id", 0) or 0) > 0
    }
    occupied_slots = {
        (int(row.get("category", 0) or 0), int(row.get("book_index", 0) or 0)): int(
            row.get("item_id", 0) or 0
        )
        for row in book_rows
        if int(row.get("item_id", 0) or 0) > 0
    }
    worn_item_id = int((worn or {}).get("item_id", 0) or 0)
    if worn_item_id > 0:
        owned_item_ids.add(worn_item_id)

    grants: list[LevelTitleGrant] = []
    notes: list[str] = []
    for milestone in candidates:
        slot = index.find_by_item_id(milestone.item_id)
        if slot is None:
            notes.append("lv%s:item%s:skip=no_slot" % (milestone.threshold_level, milestone.item_id))
            continue
        if (
            not slot.is_open
            or int(slot.quest_id) != int(milestone.quest_id)
            or not slot.allows_item(milestone.item_id)
        ):
            notes.append(
                "lv%s:item%s:skip=slot_mismatch q=%s cat=%s idx=%s"
                % (
                    milestone.threshold_level,
                    milestone.item_id,
                    int(slot.quest_id),
                    int(slot.category),
                    int(slot.index),
                )
            )
            continue
        if milestone.item_id in owned_item_ids:
            notes.append("lv%s:item%s:owned" % (milestone.threshold_level, milestone.item_id))
            continue

        slot_key = (int(slot.category), int(slot.index))
        slot_item = occupied_slots.get(slot_key, 0)
        if slot_item > 0 and slot_item != milestone.item_id:
            notes.append(
                "lv%s:item%s:skip=slot_occupied:%s"
                % (milestone.threshold_level, milestone.item_id, slot_item)
            )
            continue

        granted_rows, grant_note = _title_grant.grant_title_rewards(
            repo,
            character_id=cid,
            quest_id=milestone.quest_id,
            title_item_ids=(milestone.item_id,),
        )
        matched_rows = [
            dict(row)
            for row in granted_rows
            if int(row.get("item_id", 0) or 0) == milestone.item_id
        ]
        if not matched_rows:
            notes.append(
                "lv%s:item%s:grant_empty:%s"
                % (milestone.threshold_level, milestone.item_id, grant_note)
            )
            continue

        quest_ok, quest_note = repo.mark_quest_finished(
            cid,
            milestone.quest_id,
            flag_value=1,
        )
        for row in matched_rows:
            grants.append(
                LevelTitleGrant(
                    character_id=cid,
                    threshold_level=milestone.threshold_level,
                    quest_id=milestone.quest_id,
                    item_id=milestone.item_id,
                    category=int(row.get("category", slot.category) or 0),
                    book_index=int(row.get("book_index", slot.index) or 0),
                    row=row,
                )
            )
        owned_item_ids.add(milestone.item_id)
        occupied_slots[slot_key] = milestone.item_id
        notes.append(
            "lv%s:item%s:granted q=%s quest=%s"
            % (
                milestone.threshold_level,
                milestone.item_id,
                milestone.quest_id,
                quest_note if quest_ok else "mark_fail:" + quest_note,
            )
        )

    return grants, "level_title %s old=%s new=%s %s" % (
        mode,
        old,
        new,
        ";".join(notes) if notes else "noop",
    )


def pack_level_title_grant_packets(
    server: Any,
    *,
    grants: Sequence[LevelTitleGrant],
) -> tuple[list[bytes], str]:
    """Build the existing titlebook completion notification for each grant."""
    packets: list[bytes] = []
    notes: list[str] = []
    for grant in grants:
        title_packets, title_note = _title_grant.pack_title_grant_packets(
            server,
            character_id=int(grant.character_id),
            quest_id=int(grant.quest_id),
            granted_rows=(grant.row,),
        )
        packets.extend(title_packets)
        notes.append(
            "lv%s:q%s:item%s:%s"
            % (
                grant.threshold_level,
                grant.quest_id,
                grant.item_id,
                title_note,
            )
        )
    return packets, "level_title_packets n=%s %s" % (
        len(packets),
        ";".join(notes) if notes else "none",
    )


__all__ = [
    "LEVEL_TITLE_MILESTONES",
    "LevelTitleGrant",
    "LevelTitleMilestone",
    "claim_level_title_rewards",
    "has_crossed_level_title_threshold",
    "pack_level_title_grant_packets",
    "should_backfill_level_title_rewards",
]
