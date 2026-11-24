"""PVF-driven hidden boss maze selection for licensed dungeons.

The frozen Dark Elf Ruins data authors one ordinary maze and three Gruff
mazes for every applicable three/four-star dungeon.  The local single-player
rule deliberately replaces the original cumulative/monthly counters with one
independent one-in-four roll when a free maze is selected.
"""
from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from game.chain_loader import load_chain
from pvf import pvf_runtime


LICENSE_DUNGEON_INFO_PATH = "etc/dungeonetc/licensedungeoninfo.etc"
DARK_ELF_DUNGEON_EVENT_PATH = "event/universal/darkelfdgnevent.evt"

_TOKEN_SPLIT = re.compile(r"[\s,]+")
_LOCK = threading.RLock()
_RULES: dict[int, "LicenseDungeonBossRule"] = {}
_CONFIG: "LicenseDungeonBossConfig | None" = None
_WARMED = False


@dataclass(frozen=True)
class LicenseDungeonBossConfig:
    license_by_dungeon: tuple[tuple[int, int], ...]
    apply_licenses: tuple[int, ...]
    boss_monster_code: int
    # Raw five-column rows are retained for the Stage3 0x02FD snapshot.  The
    # hidden-boss selector only needs the first two meaningful columns.
    license_rows: tuple[tuple[int, int, int, int, int], ...] = ()


@dataclass(frozen=True)
class LicenseDungeonBossRule:
    dungeon_id: int
    license_level: int
    boss_monster_code: int
    ordinary_maze_indices: tuple[int, ...]
    boss_maze_indices: tuple[int, ...]
    boss_map_ids: tuple[int, ...]


@dataclass(frozen=True)
class LicenseDungeonBossSelection:
    maze_index: int
    eligible: bool = False
    boss_present: bool = False
    license_level: int = 0
    boss_monster_code: int = 0
    source: str = "not_applicable"


def _tag_body(text: str, tag: str) -> str:
    source = str(text or "")
    opening = re.search(
        r"(?im)^\s*\[" + re.escape(tag) + r"\]\s*$",
        source,
    )
    if opening is None:
        return ""
    tail = source[opening.end() :]
    closing = re.search(
        r"(?im)^\s*\[/" + re.escape(tag) + r"\]\s*$",
        tail,
    )
    if closing is not None:
        return tail[: closing.start()].strip()
    next_tag = re.search(r"(?m)^\s*\[", tail)
    if next_tag is not None:
        tail = tail[: next_tag.start()]
    return tail.strip()


def _int_values(text: str) -> tuple[int, ...]:
    values: list[int] = []
    for raw in _TOKEN_SPLIT.split(str(text or "")):
        token = raw.strip().strip("`")
        if not token:
            continue
        try:
            values.append(int(token, 0))
        except ValueError:
            continue
    return tuple(values)


def parse_license_dungeon_boss_config(
    license_info_text: str,
    event_text: str,
) -> LicenseDungeonBossConfig:
    """Parse only the PVF fields that identify eligible Gruff mazes."""
    rows = _int_values(_tag_body(license_info_text, "dungeon license info"))
    if not rows or len(rows) % 5:
        raise ValueError(
            "license dungeon info must be a non-empty fixed five-field table"
        )

    license_by_dungeon: dict[int, int] = {}
    license_rows: list[tuple[int, int, int, int, int]] = []
    for offset in range(0, len(rows), 5):
        group = int(rows[offset])
        dungeon_id = int(rows[offset + 1])
        license_level = int(rows[offset + 2])
        field4 = int(rows[offset + 3])
        field5 = int(rows[offset + 4])
        if group <= 0:
            raise ValueError(
                "invalid license dungeon row at offset %d: group=%d" %
                (offset, group)
            )
        if dungeon_id <= 0 or license_level <= 0:
            raise ValueError(
                "invalid license dungeon row at offset %d: dungeon=%d license=%d"
                % (offset, dungeon_id, license_level)
            )
        previous = license_by_dungeon.get(dungeon_id)
        if previous is not None and previous != license_level:
            raise ValueError(
                "conflicting license levels for dungeon %d: %d/%d"
                % (dungeon_id, previous, license_level)
            )
        license_by_dungeon[dungeon_id] = license_level
        license_rows.append((group, dungeon_id, license_level, field4, field5))

    apply_licenses = tuple(
        dict.fromkeys(
            value
            for value in _int_values(
                _tag_body(license_info_text, "groop apply licence")
            )
            if int(value) > 0
        )
    )
    if not apply_licenses:
        raise ValueError("groop apply licence is empty")

    boss_values = _int_values(_tag_body(event_text, "boss index"))
    boss_monster_code = int(boss_values[0]) if boss_values else 0
    if boss_monster_code <= 0:
        raise ValueError("dark elf dungeon event boss index is missing")

    return LicenseDungeonBossConfig(
        license_by_dungeon=tuple(sorted(license_by_dungeon.items())),
        apply_licenses=apply_licenses,
        boss_monster_code=boss_monster_code,
        license_rows=tuple(license_rows),
    )


def license_dungeon_permission_records() -> tuple[tuple[int, int, int], ...]:
    """Return one current highest-license row per PVF dungeon group.

    Stage3's 0x02FD reader stores triples ``(dungeon_id, license_level,
    field3)``.  The client uses only ``license_level`` for star access; the
    third value is opaque to the confirmed star-access branch and starts at
    zero in the local single-player snapshot.  The group/dungeon/level source
    remains the warmed ``LicenseDungeonInfo.etc`` table rather than a
    hard-coded node list.
    """
    with _LOCK:
        config = _CONFIG if _WARMED else None
    if config is None or not config.license_rows:
        return ()

    highest_by_group: dict[int, tuple[int, int]] = {}
    for group, dungeon_id, license_level, _field4, _field5 in config.license_rows:
        previous = highest_by_group.get(int(group))
        candidate = (int(license_level), int(dungeon_id))
        if previous is None or candidate > previous:
            highest_by_group[int(group)] = candidate
    return tuple(
        (dungeon_id, license_level, 0)
        for license_level, dungeon_id in sorted(
            highest_by_group.values(), key=lambda value: value[1]
        )
    )


def _maze_map_ids(maze: Any) -> tuple[int, ...]:
    map_ids: list[int] = []
    seen: set[int] = set()
    for spec in list(getattr(maze, "map_specs", None) or []):
        if not isinstance(spec, Mapping):
            continue
        candidates = spec.get("candidates") or (spec.get("map_id"),)
        for raw_map_id in candidates:
            try:
                map_id = int(raw_map_id or 0)
            except (TypeError, ValueError):
                continue
            if map_id > 0 and map_id not in seen:
                seen.add(map_id)
                map_ids.append(map_id)
    return tuple(map_ids)


def build_license_dungeon_boss_rules(
    config: LicenseDungeonBossConfig,
    dungeons: Mapping[int, Any],
    actor_codes_for_map: Callable[[int], set[int]],
) -> dict[int, LicenseDungeonBossRule]:
    """Bind eligible license tiers to the authored ordinary/Gruff mazes."""
    apply_licenses = {int(value) for value in config.apply_licenses}
    rules: dict[int, LicenseDungeonBossRule] = {}
    actor_cache: dict[int, set[int]] = {}

    def actor_codes(map_id: int) -> set[int]:
        mid = int(map_id)
        if mid not in actor_cache:
            actor_cache[mid] = {
                int(code) for code in actor_codes_for_map(mid) if int(code) > 0
            }
        return actor_cache[mid]

    for dungeon_id, license_level in config.license_by_dungeon:
        if int(license_level) not in apply_licenses:
            continue
        dungeon = dungeons.get(int(dungeon_id))
        if dungeon is None:
            raise ValueError(
                "licensed boss dungeon is missing from dungeon index: %d"
                % int(dungeon_id)
            )
        mazes = list(getattr(dungeon, "mazes", None) or [])
        if not mazes:
            raise ValueError(
                "licensed boss dungeon has no maze: %d" % int(dungeon_id)
            )

        ordinary_maze_indices: list[int] = []
        boss_maze_indices: list[int] = []
        boss_map_ids: list[int] = []
        seen_boss_maps: set[int] = set()
        for maze in mazes:
            maze_index = int(getattr(maze, "index", 0) or 0)
            map_ids = _maze_map_ids(maze)
            if not map_ids:
                raise ValueError(
                    "licensed boss dungeon maze has no MAP candidates: dungeon=%d maze=%d"
                    % (int(dungeon_id), maze_index)
                )
            matching_maps = tuple(
                map_id
                for map_id in map_ids
                if int(config.boss_monster_code) in actor_codes(map_id)
            )
            if matching_maps:
                boss_maze_indices.append(maze_index)
                for map_id in matching_maps:
                    if map_id not in seen_boss_maps:
                        seen_boss_maps.add(map_id)
                        boss_map_ids.append(map_id)
            else:
                ordinary_maze_indices.append(maze_index)

        if len(ordinary_maze_indices) != 1 or len(boss_maze_indices) != 3:
            raise ValueError(
                "licensed boss maze shape changed: dungeon=%d ordinary=%s boss=%s"
                % (
                    int(dungeon_id),
                    ordinary_maze_indices,
                    boss_maze_indices,
                )
            )
        rules[int(dungeon_id)] = LicenseDungeonBossRule(
            dungeon_id=int(dungeon_id),
            license_level=int(license_level),
            boss_monster_code=int(config.boss_monster_code),
            ordinary_maze_indices=tuple(ordinary_maze_indices),
            boss_maze_indices=tuple(boss_maze_indices),
            boss_map_ids=tuple(boss_map_ids),
        )

    if not rules:
        raise ValueError("license dungeon boss config produced no applicable rules")
    return rules


def choose_license_dungeon_boss_maze(
    rule: LicenseDungeonBossRule,
    default_maze_index: int,
    *,
    selection_reason: str,
    rng: Any = None,
) -> LicenseDungeonBossSelection:
    """Apply the local one-in-four rule to one already-selected free maze."""
    default_index = int(default_maze_index)
    if str(selection_reason or "").strip().lower() != "no_connection":
        return LicenseDungeonBossSelection(
            maze_index=default_index,
            eligible=False,
            boss_present=False,
            license_level=int(rule.license_level),
            boss_monster_code=int(rule.boss_monster_code),
            source="preserve_connected_maze",
        )
    if default_index not in rule.ordinary_maze_indices:
        return LicenseDungeonBossSelection(
            maze_index=default_index,
            eligible=False,
            boss_present=default_index in rule.boss_maze_indices,
            license_level=int(rule.license_level),
            boss_monster_code=int(rule.boss_monster_code),
            source="preserve_nonordinary_maze",
        )

    random_source = rng if rng is not None else random
    if int(random_source.randrange(4)) != 0:
        return LicenseDungeonBossSelection(
            maze_index=int(rule.ordinary_maze_indices[0]),
            eligible=True,
            boss_present=False,
            license_level=int(rule.license_level),
            boss_monster_code=int(rule.boss_monster_code),
            source="miss_3_of_4",
        )
    selected = int(random_source.choice(rule.boss_maze_indices))
    return LicenseDungeonBossSelection(
        maze_index=selected,
        eligible=True,
        boss_present=True,
        license_level=int(rule.license_level),
        boss_monster_code=int(rule.boss_monster_code),
        source="hit_1_of_4",
    )


def select_license_dungeon_boss_maze(
    dungeon_id: int,
    default_maze_index: int,
    *,
    selection_reason: str,
    rng: Any = None,
) -> LicenseDungeonBossSelection:
    with _LOCK:
        rule = _RULES.get(int(dungeon_id)) if _WARMED else None
    if rule is None:
        return LicenseDungeonBossSelection(maze_index=int(default_maze_index))
    return choose_license_dungeon_boss_maze(
        rule,
        int(default_maze_index),
        selection_reason=selection_reason,
        rng=rng,
    )


def get_license_dungeon_boss_rule(
    dungeon_id: int,
) -> LicenseDungeonBossRule | None:
    with _LOCK:
        return _RULES.get(int(dungeon_id)) if _WARMED else None


def warmup_license_dungeon_boss_index() -> dict[str, int]:
    """Build the complete license-boss index from the shared PVF runtime."""
    global _CONFIG, _WARMED

    license_text = pvf_runtime.read_text(LICENSE_DUNGEON_INFO_PATH)
    event_text = pvf_runtime.read_text(DARK_ELF_DUNGEON_EVENT_PATH)
    config = parse_license_dungeon_boss_config(license_text, event_text)

    from game.quest.quest_index import get_quest_index

    quest_index = get_quest_index()
    quest_index.ensure_all_dungeons_loaded()
    map_actors = load_chain("helper-dungeon_map_monsters.py")
    rules = build_license_dungeon_boss_rules(
        config,
        quest_index.dungeon_cache,
        map_actors.collect_map_actor_codes,
    )
    with _LOCK:
        _RULES.clear()
        _RULES.update(rules)
        _CONFIG = config
        _WARMED = True
    return {
        "license_boss_dungeon_count": len(rules),
        "license_boss_maze_count": sum(
            len(rule.boss_maze_indices) for rule in rules.values()
        ),
        "license_boss_map_count": len(
            {
                map_id
                for rule in rules.values()
                for map_id in rule.boss_map_ids
            }
        ),
        "license_boss_monster_code": int(config.boss_monster_code),
    }
