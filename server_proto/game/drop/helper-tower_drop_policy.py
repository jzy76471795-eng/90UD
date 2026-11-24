# -*- coding: utf-8 -*-
"""Valley of Fallen Souls ordinary-drop policy.

Quest reward items are generated later by ``opcode0027`` and are deliberately
outside this module.  This policy only controls the ordinary monster-drop
pipeline used for gold, equipment, common materials and configured mob items.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Optional

from game.chain_loader import load_chain
from game.drop.dnf_lcg import DnfLcg
from game.drop.equip_stackable_pools import is_registered_drop_item
from game.drop.models import DropInfo, MonsterDropResult
from game.quest.quest_index import get_quest_index
from pvf import pvf_runtime

_map_actors = load_chain("helper-dungeon_map_monsters.py")
_death_tower_actors = load_chain("helper-death_tower_actors.py")

DROP_SCOPE_ORDINARY = "ordinary"
DROP_SCOPE_DEATH_TOWER = "death_tower_items"
DROP_SCOPE_QUEST_ONLY = "quest_only"

_DEATH_TOWER_DUNGEON_ID = 11000
_TOWER_DUNGEON_PATH_PREFIX = "dungeon/towers/"
_DROP_DENOMINATOR = 10000


def resolve_tower_drop_scope(dungeon_id: int) -> str:
    """Classify one dungeon using the boot-warmed PVF dungeon path."""
    did = int(dungeon_id)
    if did == _DEATH_TOWER_DUNGEON_ID:
        return DROP_SCOPE_DEATH_TOWER
    if did <= 0:
        return DROP_SCOPE_ORDINARY
    dungeon = get_quest_index().get_dungeon(did)
    normalized = (
        str(getattr(dungeon, "path", "") or "")
        .replace("\\", "/")
        .strip("/")
        .lower()
    )
    if normalized.startswith(_TOWER_DUNGEON_PATH_PREFIX):
        return DROP_SCOPE_QUEST_ONLY
    return DROP_SCOPE_ORDINARY


@lru_cache(maxsize=1)
def death_tower_item_template_ids() -> tuple[int, ...]:
    """Return every current-PVF temporary item used by Tower of Death.

    The source mirrors the drop producer: only ``[death tower item]`` rows on
    AI characters physically used by dungeon ``11000`` are included.  This
    deliberately excludes quest rewards and ordinary tower-map materials.
    """
    dungeon = get_quest_index().get_dungeon(_DEATH_TOWER_DUNGEON_ID)
    map_indexes = tuple(
        int(value)
        for value in getattr(dungeon, "death_tower_map_indexes", ()) or ()
        if int(value) > 0
    )
    codes: set[int] = set()
    for map_index in map_indexes:
        codes.update(
            int(code)
            for code in _death_tower_actors.death_tower_fixed_apc_ids(map_index)
            if int(code) > 0
        )
        path = _map_actors.resolve_map_path(map_index)
        if not path:
            continue
        try:
            raw_map = pvf_runtime.read_text(path)
        except Exception:
            continue
        codes.update(
            int(row.get("code") or 0)
            for row in _map_actors.parse_map_ai_character_tokens(raw_map)
            if int(row.get("code") or 0) > 0
        )

    item_ids: set[int] = set()
    for code in codes:
        definition = _map_actors.resolve_ai_character_definition(code)
        for item_id, drop_rate in getattr(definition, "death_tower_items", ()) or ():
            if int(item_id) > 0 and int(drop_rate) > 0:
                item_ids.add(int(item_id) & 0xFFFFFFFF)
    return tuple(sorted(item_ids))


def warmup_death_tower_item_index() -> dict[str, int]:
    """Warm the temporary-item identity before a Game session can use it."""
    return {"death_tower_temporary_item_count": len(death_tower_item_template_ids())}


def generate_death_tower_item_drops(
    *,
    ai_character_code: int,
    lcg: DnfLcg,
    slot_counter: int,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> tuple[MonsterDropResult, int]:
    """Roll the killed AIC's PVF ``[death tower item]`` rows."""
    candidate_is_registered = allow_item or is_registered_drop_item
    seed_before = int(lcg.seed)
    slot = int(slot_counter) & 0xFFFF
    drops: list[DropInfo] = []
    definition = _map_actors.resolve_ai_character_definition(
        int(ai_character_code)
    )
    rows = tuple(
        getattr(definition, "death_tower_items", ()) or ()
        if definition is not None
        else ()
    )
    for item_id, configured_rate in rows:
        rate = max(0, min(_DROP_DENOMINATOR, int(configured_rate)))
        if int(item_id) <= 0 or rate <= 0:
            continue
        if not candidate_is_registered(int(item_id)):
            continue
        if int(lcg.next(_DROP_DENOMINATOR)) >= rate:
            continue
        slot = (slot + 1) & 0xFFFF
        if slot == 0:
            slot = 1
        drops.append(
            DropInfo(
                scene_slot=slot,
                template_id=int(item_id) & 0xFFFFFFFF,
                stack_count=1,
                source="tower_item",
            )
        )
    result = MonsterDropResult(
        drops=drops,
        seed_before=seed_before,
        seed_after=int(lcg.seed),
        note="tower_item rows=%d hits=%d" % (len(rows), len(drops)),
    )
    return result, slot


def empty_tower_ordinary_drop_result(lcg: DnfLcg) -> MonsterDropResult:
    """Return a diagnostic result for tower maps whose only drops are quests."""
    seed = int(lcg.seed)
    return MonsterDropResult(
        seed_before=seed,
        seed_after=seed,
        note="tower_quest_only",
    )
