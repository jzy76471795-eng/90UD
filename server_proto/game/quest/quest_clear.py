# -*- coding: utf-8 -*-
"""PVF dungeon-clear and target-map quest progress.

Only conditions with facts the local dungeon session owns are evaluated here:
``[clear map]`` target map / companion survival, ``[clear a dungeon]`` sub
type 6 target dungeon pairs, ordinary clear (sub type 6), all-room clear (sub
type 11), and Hell Party clear (sub type 23).  Other settlement sub types
require timing, hit-count, score, party, or consumable facts and stay unchanged
until their authoritative inputs are present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from game.quest.quest_index import QuestMeta, get_quest_index
from game.quest.quest_residual import decrement_trigger_channel


@dataclass(frozen=True)
class DungeonClearContext:
    dungeon_id: int
    difficulty: int
    map_id: int = -1
    worldmap_id: int = -1
    hell_party_enabled: bool = False
    recommended_dungeon: bool = False
    seen_companion_codes: frozenset[int] = frozenset()
    alive_companion_codes: frozenset[int] = frozenset()
    required_room_cells: frozenset[tuple[int, int]] = frozenset()
    cleared_room_cells: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True)
class ClearConditionRule:
    channel: int
    dungeon_selector: int
    min_difficulty: int
    required_count: int
    require_hell_party: bool = False
    worldmap_selector: int = -1
    target_map_selector: int = -1
    companion_selector: int = -1
    require_all_rooms: bool = False


def _normalize_type(tag: str) -> str:
    value = (tag or "").strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    return value


def _parse_int_data(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"-?\d+", text or "")]


def _positive_count(value: int | None) -> int:
    return max(1, int(value or 0))


def parse_condition_under_clear_rules(meta: QuestMeta) -> list[ClearConditionRule]:
    """Parse clear rules from one PVF quest without quest-id special cases.

    ``clear map`` uses one target map id.  Sub type 0 adds a companion APC id
    that must have appeared from the native map and remain alive.  The frozen
    PVF has one older row with an auxiliary value that is not an APC id; its
    sole ``<apc::...>`` dialog actor supplies the same map-native companion
    relation.

    ``[clear a dungeon]`` ``sub type=6`` rows use repeated
    ``dungeon, minDifficulty`` pairs.  The pairs are alternatives for one
    completion channel: clearing any matching dungeon completes the task.

    ``[condition under clear]`` ``sub type=6`` rows use
    ``dungeon, minDifficulty, count``.  A number of older PVF rows omit the
    count; those mean one clear.

    ``sub type=11`` requires every concrete room in the selected quest maze.

    ``sub type=23`` is Hell Party.  ``-2, worldmap, minDifficulty, count``
    targets a region; ``-1, minDifficulty, count`` targets any Hell Party.
    """
    quest_type = _normalize_type(meta.quest_type)
    if quest_type not in {"condition under clear", "clear map", "clear a dungeon"}:
        return []
    values = _parse_int_data(meta.int_data)
    if not values:
        return []

    subtype = int(meta.sub_type)
    rules: list[ClearConditionRule] = []
    if quest_type == "clear map":
        if subtype not in {-1, 0, 1} or int(values[0]) <= 0:
            return []
        dungeon_info = tuple(getattr(meta, "dungeon_info", None) or ())
        dungeon_selector = int(dungeon_info[0]) if dungeon_info else -1
        min_difficulty = int(dungeon_info[1]) if len(dungeon_info) > 1 else -1
        companion_selector = -1
        if subtype == 0 and len(values) > 1 and int(values[1]) > 0:
            companion_selector = int(values[1])
        elif len(values) > 1:
            dialog_apcs = tuple(getattr(meta, "dialog_apc_codes", None) or ())
            if len(dialog_apcs) == 1 and int(dialog_apcs[0]) > 0:
                companion_selector = int(dialog_apcs[0])
        return [
            ClearConditionRule(
                channel=0,
                dungeon_selector=dungeon_selector,
                min_difficulty=min_difficulty,
                required_count=1,
                target_map_selector=int(values[0]),
                companion_selector=companion_selector,
            )
        ]

    if quest_type == "clear a dungeon":
        # The PVF encodes a set of alternative target dungeons as
        # (dungeon_id, minimum_difficulty) pairs.  They all decrement the
        # same sole completion channel, rather than requiring every pair.
        if subtype != 6 or len(values) < 2 or len(values) % 2:
            return []
        for offset in range(0, len(values), 2):
            selector = int(values[offset])
            if selector <= 0:
                return []
            rules.append(
                ClearConditionRule(
                    channel=0,
                    dungeon_selector=selector,
                    min_difficulty=int(values[offset + 1]),
                    required_count=1,
                )
            )
        return rules

    if subtype == 23:
        # Region-scoped Hell Party: (-2, worldmap, difficulty, count) * n.
        if values[0] == -2:
            for channel, off in enumerate(range(0, len(values) - 3, 4)):
                if channel >= 3:
                    break
                if values[off] != -2:
                    continue
                rules.append(
                    ClearConditionRule(
                        channel=channel,
                        dungeon_selector=-1,
                        worldmap_selector=int(values[off + 1]),
                        min_difficulty=int(values[off + 2]),
                        required_count=_positive_count(values[off + 3]),
                        require_hell_party=True,
                    )
                )
            return rules

        # Any-Hell (or an explicit dungeon) rows: (selector, difficulty,
        # count) * n.  Example: ``-1 -1 1``.
        for channel, off in enumerate(range(0, len(values), 3)):
            if channel >= 3:
                break
            selector = int(values[off])
            difficulty = int(values[off + 1]) if off + 1 < len(values) else -1
            count = int(values[off + 2]) if off + 2 < len(values) else 1
            rules.append(
                ClearConditionRule(
                    channel=channel,
                    dungeon_selector=selector,
                    min_difficulty=difficulty,
                    required_count=_positive_count(count),
                    require_hell_party=True,
                )
            )
        return rules

    if subtype == 11:
        # Frozen epic PVF: dungeon, minimumDifficulty, reserved(-1).  The
        # condition message for all eight rows explicitly requires every room
        # in the selected quest maze to be cleared.
        if len(values) < 2:
            return []
        return [
            ClearConditionRule(
                channel=0,
                dungeon_selector=int(values[0]),
                min_difficulty=int(values[1]),
                required_count=1,
                require_all_rooms=True,
            )
        ]

    if subtype != 6:
        return []

    # Ordinary clear: (dungeon selector, minimum difficulty, count) * n.
    # Old quests often contain only ``dungeon, difficulty`` and imply count=1.
    for channel, off in enumerate(range(0, len(values), 3)):
        if channel >= 3:
            break
        selector = int(values[off])
        difficulty = int(values[off + 1]) if off + 1 < len(values) else -1
        count = int(values[off + 2]) if off + 2 < len(values) else 1
        rules.append(
            ClearConditionRule(
                channel=channel,
                dungeon_selector=selector,
                min_difficulty=difficulty,
                required_count=_positive_count(count),
            )
        )
    return rules


def matches_clear_condition(rule: ClearConditionRule, context: DungeonClearContext) -> bool:
    if rule.require_hell_party and not bool(context.hell_party_enabled):
        return False

    selector = int(rule.dungeon_selector)
    if selector >= 0 and selector != int(context.dungeon_id):
        return False
    if selector == -3 and not bool(context.recommended_dungeon):
        return False
    if selector not in {-1, -2, -3} and selector < 0:
        return False

    target_worldmap = int(rule.worldmap_selector)
    if target_worldmap >= 0 and target_worldmap != int(context.worldmap_id):
        return False
    if int(rule.min_difficulty) >= 0 and int(context.difficulty) < int(rule.min_difficulty):
        return False
    if rule.require_all_rooms:
        required = {
            (int(cell[0]), int(cell[1]))
            for cell in context.required_room_cells
            if isinstance(cell, (list, tuple)) and len(cell) >= 2
        }
        cleared = {
            (int(cell[0]), int(cell[1]))
            for cell in context.cleared_room_cells
            if isinstance(cell, (list, tuple)) and len(cell) >= 2
        }
        if not required or not required.issubset(cleared):
            return False
    target_map = int(rule.target_map_selector)
    if target_map >= 0 and target_map != int(context.map_id):
        return False
    companion = int(rule.companion_selector)
    if companion >= 0:
        seen = {int(code) for code in context.seen_companion_codes}
        alive = {int(code) for code in context.alive_companion_codes}
        if companion not in seen or companion not in alive:
            return False
    return True


def _apply_clear_rules_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    context: DungeonClearContext,
    quest_types: frozenset[str],
) -> list[tuple[int, int, int, str]]:
    index = get_quest_index()
    changed: list[tuple[int, int, int, str]] = []
    for raw_qid, raw_trigger in active_pairs or ():
        quest_id = int(raw_qid) & 0xFFFF
        old = int(raw_trigger) & 0xFFFFFFFF
        if quest_id <= 0 or old == 0:
            continue
        meta = index.get_meta(quest_id)
        if meta is None:
            continue
        quest_type = _normalize_type(meta.quest_type)
        if quest_type not in quest_types:
            continue
        for rule in parse_condition_under_clear_rules(meta):
            if not matches_clear_condition(rule, context):
                continue
            new = decrement_trigger_channel(old, rule.channel)
            if new == old:
                continue
            note = (
                "clear q=%s type=%s subtype=%s ch=%s %s->%s dungeon=%s map=%s "
                "worldmap=%s diff=%s hell=%s target=(%s,%s,%s,%s,%s) rooms=%s/%s"
                % (
                    quest_id,
                    quest_type,
                    int(meta.sub_type),
                    int(rule.channel),
                    old,
                    new,
                    int(context.dungeon_id),
                    int(context.map_id),
                    int(context.worldmap_id),
                    int(context.difficulty),
                    int(bool(context.hell_party_enabled)),
                    int(rule.dungeon_selector),
                    int(rule.target_map_selector),
                    int(rule.companion_selector),
                    int(rule.worldmap_selector),
                    int(rule.min_difficulty),
                    len(context.cleared_room_cells),
                    len(context.required_room_cells),
                )
            )
            changed.append((quest_id, old, new, note))
            break
    return changed


def apply_dungeon_clear_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    context: DungeonClearContext,
) -> list[tuple[int, int, int, str]]:
    """Apply final settlement facts to active clear-condition quests."""
    return _apply_clear_rules_to_active_quests(
        active_pairs,
        context=context,
        quest_types=frozenset(
            {"condition under clear", "clear map", "clear a dungeon"}
        ),
    )


def apply_clear_map_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    context: DungeonClearContext,
) -> list[tuple[int, int, int, str]]:
    """Apply one proven room-clear fact only to ``[clear map]`` quests."""
    return _apply_clear_rules_to_active_quests(
        active_pairs,
        context=context,
        quest_types=frozenset({"clear map"}),
    )


def persist_clear_map_progress(
    server: Any,
    session: Any,
    *,
    context: DungeonClearContext,
) -> tuple[list[bytes], str]:
    """Persist room-clear progress and emit S1/0021 trigger notifications."""
    character_id = 0
    for name in ("selected_character_id", "character_id", "cid"):
        try:
            value = int(getattr(session, name, 0) or 0)
        except Exception:
            value = 0
        if value > 0:
            character_id = value
            break
    if character_id <= 0:
        return [], "clear_map skip=no_character"

    repo = server._repo()
    active_rows = repo.list_active_quests(character_id)
    active_pairs = [
        (int(quest_id), int(trigger))
        for _slot, quest_id, trigger in active_rows
    ]
    changed = apply_clear_map_to_active_quests(active_pairs, context=context)
    if not changed:
        return [], "clear_map n=0 map=%s" % int(context.map_id)

    updated: list[tuple[int, int]] = []
    notes: list[str] = []
    for quest_id, _old, new, progress_note in changed:
        ok, update_note = repo.update_active_quest_trigger(
            character_id,
            int(quest_id),
            int(new),
        )
        if ok:
            updated.append((int(quest_id), int(new)))
            notes.append(progress_note)
        else:
            notes.append("clear q=%s db_skip=%s" % (quest_id, update_note))
    if not updated:
        return [], "clear_map n=0 %s" % " | ".join(notes)

    from game.quest.quest_session import (
        bind_session_quest_state,
        pack_set_trigger_ack_packets,
    )

    active_after = [
        (int(quest_id), int(trigger))
        for _slot, quest_id, trigger in repo.list_active_quests(character_id)
    ]
    finished_after = [
        int(quest_id)
        for quest_id, _flag in repo.list_finished_quests(character_id)
    ]
    bind_session_quest_state(
        session,
        active_pairs=active_after,
        finished_ids=finished_after,
    )
    packets, packet_note = pack_set_trigger_ack_packets(server, updated)
    return packets, "clear_map n=%s %s %s" % (
        len(updated),
        packet_note,
        " | ".join(notes),
    )
