"""Tower-of-death DGN actor materialization on top of ordinary MAP actors.

Tower of Death and Tower of Illusion MAPs use the legacy
``[apc random point]`` tag.  This server materializes one stable
point-budgeted lineup per MAP and sends it in S0/008F; repeated entries never
reroll the APC roster.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Sequence

from game.chain_loader import load_chain
from pvf import pvf_runtime

_map_actors = load_chain("helper-dungeon_map_monsters.py")

_APC_RANDOM_POINT_SECTION = re.compile(
    r"(?is)\[apc random point\](.*?)(?=\n\s*\[|\Z)"
)
_MONSTER_SPAWN_POS_SECTION = re.compile(
    r"(?is)\[monster spawn pos\](.*?)(?=\n\s*\[|\Z)"
)
_CONFIG_APC_LIST_INDEX_START = 64
_FIXED_APC_IDS_BY_MAP: dict[int, tuple[int, ...]] = {
    30007: (5301,),
    30013: (600, 5301),
    30019: (600, 5004, 20002),
    30024: (501, 600, 5004),
    30035: (500, 600, 5004, 5301),
    30038: (501, 600, 5004),
    30039: (501, 600, 5004),
    30040: (501, 600, 5004),
    30041: (501, 600, 5004, 20002),
    30042: (500, 600, 5004, 5301),
    30043: (500, 501, 600, 5301),
    30044: (500, 501, 600, 603),
    30045: (500, 501, 600, 5305),
    31003: (600,),
    31011: (5004,),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n", ""}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except ValueError:
        return int(default)


def _section_first_int(raw: str, pattern: re.Pattern[str]) -> int:
    match = pattern.search(str(raw or ""))
    if match is None:
        return 0
    values = re.findall(r"-?\d+", match.group(1) or "")
    return int(values[0]) if values else 0


@lru_cache(maxsize=512)
def resolve_death_tower_apc_spec(map_index: int) -> tuple[int, int]:
    """Return ``(point_budget, spawn_slots)`` declared by one MAP."""
    path = _map_actors.resolve_map_path(int(map_index or 0))
    if not path:
        return 0, 0
    try:
        raw = pvf_runtime.read_text(path)
    except Exception:
        return 0, 0
    point_budget = _section_first_int(raw, _APC_RANDOM_POINT_SECTION)
    spawn_slots = _section_first_int(raw, _MONSTER_SPAWN_POS_SECTION)
    return max(0, point_budget), max(0, spawn_slots)


def death_tower_fixed_apc_ids(map_index: int) -> tuple[int, ...]:
    return _FIXED_APC_IDS_BY_MAP.get(int(map_index or 0), ())


@lru_cache(maxsize=32)
def resolve_death_tower_fixed_apcs(map_index: int) -> tuple[Any, ...]:
    """Resolve and validate the fixed APC definitions for one tower MAP."""
    mid = int(map_index or 0)
    ids = death_tower_fixed_apc_ids(mid)
    if not ids:
        return ()
    definitions: list[Any] = []
    for code in ids:
        definition = _map_actors.resolve_ai_character_definition(int(code))
        if definition is None or int(definition.appearance_point) <= 0:
            raise RuntimeError(
                "death tower fixed APC definition missing: map=%d code=%d"
                % (mid, code)
            )
        definitions.append(definition)
    point_budget, spawn_slots = resolve_death_tower_apc_spec(mid)
    spent = sum(int(definition.appearance_point) for definition in definitions)
    if len(definitions) > spawn_slots or spent != point_budget:
        raise RuntimeError(
            "death tower fixed APC config mismatch: map=%d count=%d/%d points=%d/%d"
            % (mid, len(definitions), spawn_slots, spent, point_budget)
        )
    return tuple(definitions)


def warmup_death_tower_actor_config() -> dict[str, int]:
    codes: set[int] = set()
    for map_index in sorted(_FIXED_APC_IDS_BY_MAP):
        definitions = resolve_death_tower_fixed_apcs(map_index)
        codes.update(int(definition.code) for definition in definitions)
    return {
        "death_tower_apc_config_map_count": len(_FIXED_APC_IDS_BY_MAP),
        "death_tower_apc_definition_count": len(codes),
    }


def materialize_death_tower_config_apcs(
    *,
    map_index: int,
    first_seq: int,
    existing_count: int,
    actor_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Materialize the MAP's fixed hostile APC lineup."""
    definitions = resolve_death_tower_fixed_apcs(int(map_index or 0))
    if actor_limit is not None:
        definitions = definitions[: max(0, int(actor_limit))]

    selected: list[dict[str, Any]] = []
    for definition in definitions:
        list_index = _CONFIG_APC_LIST_INDEX_START + len(selected)
        selected.append(
            {
                "kind": "death_tower_config_apc",
                "list_index": list_index,
                "template_order": list_index,
                "slot_index": list_index,
                "seq": (int(first_seq) + int(existing_count) + len(selected) + 1)
                & 0xFFFF,
                "code": int(definition.code) & 0xFFFFFFFF,
                "level": int(definition.level) & 0xFF,
                "type": 5,
                "flag0": 0,
                "flag1": 0,
                "extra_state": 0,
                "extra_flag": 0,
                "quest_enemy_type": 2,
                "is_blocking": True,
                "grants_exp": True,
                "killable": True,
                "faction": "monster",
                "role": "enemy",
                "is_hostile": True,
                "is_ally": False,
                "team": 100,
                "x": None,
                "y": None,
                "appearance_point": int(definition.appearance_point),
            }
        )
    return selected


def load_death_tower_map_monsters(
    map_index: int,
    dungeon_basic_lv: int = 0,
    *,
    first_seq: int = 0,
    active_quest_ids: Sequence[int] = (),
    dungeon_id: int = 0,
    difficulty: int = -1,
    template_state: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Load fixed MAP actors, then materialize point-budgeted tower APCs."""
    local_state: dict[str, str] = {}
    state = template_state if template_state is not None else local_state
    actors = _map_actors.load_map_monsters(
        int(map_index or 0),
        dungeon_basic_lv=int(dungeon_basic_lv or 0),
        first_seq=int(first_seq or 0),
        active_quest_ids=active_quest_ids,
        dungeon_id=int(dungeon_id or 0),
        difficulty=int(difficulty),
        template_state=state,
    )
    if state.get("source") == "monsters_off" or not _env_bool(
        "USDOF_DUNGEON_MAP_AI_CHARACTERS", True
    ):
        return actors

    point_budget, spawn_slots = resolve_death_tower_apc_spec(
        int(map_index or 0)
    )
    if point_budget <= 0 or spawn_slots <= 0:
        return actors

    global_limit = _env_int("USDOF_DUNGEON_MONSTER_LIMIT", 0)
    if global_limit > 0:
        spawn_slots = min(spawn_slots, max(0, global_limit - len(actors)))
    fixed_ids = death_tower_fixed_apc_ids(int(map_index or 0))
    if not fixed_ids:
        raise RuntimeError(
            "death tower fixed APC map is not configured: map=%d"
            % int(map_index or 0)
        )
    actors.extend(
        materialize_death_tower_config_apcs(
            map_index=int(map_index or 0),
            first_seq=int(first_seq or 0),
            existing_count=len(actors),
            actor_limit=spawn_slots,
        )
    )
    return actors
