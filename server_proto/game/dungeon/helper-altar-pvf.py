"""PVF index for the two Stage3 altar random-wave dungeons."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from pvf import pvf_runtime


_ALTAR_DUNGEON_IDS = frozenset({11006, 11007})
_ALTAR_REWARD_PATH = "etc/bloodclearreward.etc"
_LST_PAIR = re.compile(r"(?m)^\s*(\d+)\s+`?([^`\r\n]+)`?")


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def _tokens(text: str) -> list[str]:
    result: list[str] = []
    for quoted, plain in re.findall(r"`([^`]*)`|(\S+)", str(text or "")):
        token = quoted if quoted != "" else plain
        token = token.strip()
        if token:
            result.append(token)
    return result


def _closed_section(text: str, name: str) -> str:
    match = re.search(
        r"(?is)\["
        + re.escape(name)
        + r"\](.*?)\[/"
        + re.escape(name)
        + r"\]",
        str(text or ""),
    )
    return match.group(1) if match is not None else ""


def _open_section(text: str, name: str) -> str:
    match = re.search(
        r"(?is)\["
        + re.escape(name)
        + r"\](.*?)(?=\r?\n\s*\[[^/]|\Z)",
        str(text or ""),
    )
    return match.group(1) if match is not None else ""


def _first_int(text: str, default: int = 0) -> int:
    for token in _tokens(text):
        try:
            return int(token, 0)
        except ValueError:
            continue
    return int(default)


def _cell_pair(raw_dungeon: str, section_name: str) -> Tuple[int, int]:
    values: list[int] = []
    for token in _tokens(_closed_section(raw_dungeon, section_name)):
        try:
            values.append(int(token, 0))
        except ValueError:
            continue
    if len(values) < 2 or any(value < 0 or value > 0xFF for value in values[:2]):
        raise ValueError(
            "%s must contain a valid u8 cell pair, got %r"
            % (section_name, values[:2])
        )
    return values[0], values[1]


def _lst_paths(lst_path: str, prefix: str) -> Mapping[int, str]:
    result: dict[int, str] = {}
    for raw_id, raw_path in _LST_PAIR.findall(pvf_runtime.read_text(lst_path)):
        entry_id = int(raw_id)
        path = raw_path.strip().replace("\\", "/")
        if not path.lower().startswith(prefix.lower() + "/"):
            path = prefix + "/" + path.lstrip("/")
        result[entry_id] = path
    return MappingProxyType(result)


@dataclass(frozen=True)
class AltarMonsterSource:
    monster_code: int
    template_type: int
    x: int
    y: int
    z: int
    duration_ms: int
    spawn_interval_ms: int
    base_spawn_count: int
    spawn_count_increment: int
    batch_count: int


@dataclass(frozen=True)
class AltarPhaseRow:
    round_index: int
    monster_template_index: int
    delay_ms: int
    scale: float
    flag: int
    concurrent_phase_count: int
    variant: str = ""

    @property
    def candidate_id(self) -> int:
        return int(self.flag)

    @property
    def candidate_row_count(self) -> int:
        return max(1, int(self.concurrent_phase_count))


@dataclass(frozen=True)
class AltarRewardConfig:
    path: str
    gold_candidate_weight: int
    item_candidate_weight: int
    weight_per_progress: int
    gold_amount_weight: float
    reward_exp_weights: Tuple[float, ...]
    entry_count_exp_weights: Tuple[float, ...]
    ultimate_difficulty_points: Tuple[int, int]
    ultimate_reward_probabilities: Mapping[int, Tuple[int, int]]

    @property
    def roll_scale(self) -> int:
        return int(self.weight_per_progress) * 10

    def reward_card_count(self, completed_rounds: int, max_rounds: int) -> int:
        maximum = max(0, int(max_rounds))
        if maximum <= 0:
            return 0
        count = 10 * max(0, int(completed_rounds)) // maximum
        return min(10, max(1, count))

    def classify_candidate(self, completed_rounds: int, roll: int) -> str:
        value = int(roll)
        if not 0 <= value < self.roll_scale:
            raise ValueError("altar reward roll outside configured scale")
        progress = min(10, max(0, int(completed_rounds)))
        gold_end = progress * int(self.gold_candidate_weight)
        item_end = progress * (
            int(self.gold_candidate_weight) + int(self.item_candidate_weight)
        )
        if value < gold_end:
            return "gold"
        if value < item_end:
            return "item"
        return "empty"

    def ultimate_point(self, difficulties: Tuple[int, ...]) -> int:
        total = 0
        for value in difficulties:
            difficulty = int(value)
            if difficulty in {1, 2}:
                total += int(self.ultimate_difficulty_points[difficulty - 1])
        return min(0x7FFFFFFF, max(0, total))


@dataclass(frozen=True)
class AltarMapDefinition:
    map_id: int
    path: str
    monsters: Tuple[AltarMonsterSource, ...]
    phases: Tuple[AltarPhaseRow, ...]

    def phase_variants(self, phase: int) -> Tuple[str, ...]:
        variants = {
            row.variant.upper()
            for row in self.phases
            if row.round_index == int(phase) and row.variant
        }
        return tuple(sorted(variants))

    def rows_for_phase(
        self,
        phase: int,
        variant: str = "",
    ) -> Tuple[Tuple[AltarPhaseRow, AltarMonsterSource], ...]:
        selected_variant = str(variant or "").strip().upper()
        rows: list[Tuple[AltarPhaseRow, AltarMonsterSource]] = []
        for row in self.phases:
            if row.round_index != int(phase):
                continue
            if selected_variant and row.variant.upper() != selected_variant:
                continue
            if not selected_variant and row.variant:
                continue
            rows.append((row, self.monsters[row.monster_template_index]))
        return tuple(rows)

    def candidate_groups_for_phase(
        self,
        phase: int,
        variant: str = "",
    ) -> Tuple[
        Tuple[Tuple[AltarPhaseRow, AltarMonsterSource], ...],
        ...,
    ]:
        selected = self.rows_for_phase(phase, variant)
        groups: list[Tuple[Tuple[AltarPhaseRow, AltarMonsterSource], ...]] = []
        seen_ids: set[int] = set()
        offset = 0
        while offset < len(selected):
            lead = selected[offset][0]
            candidate_id = lead.candidate_id
            row_count = lead.candidate_row_count
            end = offset + row_count
            if end > len(selected):
                raise ValueError(
                    "altar map %d phase %d variant %r candidate %d spans %d rows past %d"
                    % (
                        self.map_id,
                        int(phase),
                        str(variant or ""),
                        candidate_id,
                        row_count,
                        len(selected) - offset,
                    )
                )
            group = selected[offset:end]
            if any(row.candidate_id != candidate_id for row, _source in group):
                raise ValueError(
                    "altar map %d phase %d variant %r candidate %d mixes candidate ids"
                    % (self.map_id, int(phase), str(variant or ""), candidate_id)
                )
            if candidate_id in seen_ids:
                raise ValueError(
                    "altar map %d phase %d variant %r repeats candidate %d"
                    % (self.map_id, int(phase), str(variant or ""), candidate_id)
                )
            seen_ids.add(candidate_id)
            groups.append(group)
            offset = end
        return tuple(groups)


@dataclass(frozen=True)
class AltarDungeonConfig:
    dungeon_id: int
    path: str
    mode: int
    max_rounds: int
    basis_level: int
    start_cell: Tuple[int, int]
    boss_cell: Tuple[int, int]
    start_map: int
    start_maps: Tuple[int, ...]
    middle_maps: Tuple[int, ...]
    boss_map: int
    maps: Mapping[int, AltarMapDefinition]

    def build_map_plan(
        self,
        middle_map: Optional[int] = None,
        *,
        start_map: Optional[int] = None,
    ) -> Tuple[int, ...]:
        if self.mode == 1:
            selected_start = self.start_map if start_map is None else int(start_map)
            selected_middle = int(middle_map or 0)
            if selected_start not in self.start_maps:
                raise ValueError("invalid altar start map: %s" % selected_start)
            if selected_middle not in self.middle_maps:
                raise ValueError("invalid altar middle map: %s" % selected_middle)
            return selected_start, selected_middle, self.boss_map
        return (self.start_map,)


def _parse_monsters(raw_map: str, section_name: str) -> Tuple[AltarMonsterSource, ...]:
    values = _tokens(_closed_section(raw_map, section_name))
    if not values or len(values) % 10:
        raise ValueError(
            "%s token count must be a nonzero multiple of 10, got %d"
            % (section_name, len(values))
        )
    rows: list[AltarMonsterSource] = []
    for offset in range(0, len(values), 10):
        row_index = offset // 10
        try:
            row = [int(value, 0) for value in values[offset : offset + 10]]
        except ValueError as exc:
            raise ValueError("invalid %s row at %d" % (section_name, row_index)) from exc
        (
            monster_code,
            _template_type,
            x,
            y,
            z,
            duration_ms,
            spawn_interval_ms,
            base_spawn_count,
            spawn_count_increment,
            batch_count,
        ) = row
        if not 1 <= monster_code <= 0xFFFFFFFF:
            raise ValueError(
                "%s row %d monster code outside 1..u32"
                % (section_name, row_index)
            )
        if any(value < 0 or value > 0xFFFF for value in (x, y, z)):
            raise ValueError(
                "%s row %d coordinate outside u16" % (section_name, row_index)
            )
        if not 1 <= duration_ms <= 0x7FFFFFFF:
            raise ValueError(
                "%s row %d duration outside 1..i32_max"
                % (section_name, row_index)
            )
        if not 0 <= spawn_interval_ms <= 0xFFFF:
            raise ValueError(
                "%s row %d spawn interval outside u16"
                % (section_name, row_index)
            )
        if (
            not 1 <= base_spawn_count <= 0xFFFF
            or not 0 <= spawn_count_increment <= 0xFFFF
            or not 1 <= batch_count <= 0xFFFF
            or base_spawn_count
            + spawn_count_increment * (batch_count - 1)
            > 0xFFFF
        ):
            raise ValueError(
                "%s row %d spawn counts outside nonzero u16 batches"
                % (section_name, row_index)
            )
        rows.append(AltarMonsterSource(*row))
    return tuple(rows)


def _parse_phases(
    raw_map: str,
    section_name: str,
    monster_count: int,
    with_variant: bool,
) -> Tuple[AltarPhaseRow, ...]:
    values = _tokens(_closed_section(raw_map, section_name))
    width = 7 if with_variant else 6
    if not values or len(values) % width:
        raise ValueError(
            "%s token count must be a nonzero multiple of %d, got %d"
            % (section_name, width, len(values))
        )
    rows: list[AltarPhaseRow] = []
    for offset in range(0, len(values), width):
        raw = values[offset : offset + width]
        try:
            round_index = int(raw[0], 0)
            monster_template_index = int(raw[1], 0)
            delay_ms = int(raw[2], 0)
            scale = float(raw[3])
            candidate_id = int(raw[4], 0)
            candidate_row_count = int(raw[5], 0)
        except ValueError as exc:
            raise ValueError("invalid %s row at %d" % (section_name, offset // width)) from exc
        if not 0 <= monster_template_index < monster_count:
            raise ValueError(
                "%s row %d monster index %d outside 0..%d"
                % (
                    section_name,
                    offset // width,
                    monster_template_index,
                    monster_count - 1,
                )
            )
        wire_scale = int(round(scale * 100.0)) if math.isfinite(scale) else 0
        if (
            not 0 <= round_index <= 0xFF
            or not 0 <= delay_ms <= 0xFFFFFFFF
            or not math.isfinite(scale)
            or not 1 <= wire_scale <= 0xFFFF
        ):
            raise ValueError(
                "%s row %d has invalid wire round/delay/scale"
                % (section_name, offset // width)
            )
        if candidate_id < 0:
            raise ValueError(
                "%s row %d candidate id must be nonnegative"
                % (section_name, offset // width)
            )
        if not 0 <= candidate_row_count <= 10:
            raise ValueError(
                "%s row %d candidate row count outside 0..10"
                % (section_name, offset // width)
            )
        variant = _strip_tick(raw[6]).upper() if with_variant else ""
        if with_variant and variant not in {"A", "B"}:
            raise ValueError(
                "%s row %d has invalid variant %r"
                % (section_name, offset // width, variant)
            )
        rows.append(
            AltarPhaseRow(
                round_index=round_index,
                monster_template_index=monster_template_index,
                delay_ms=delay_ms,
                scale=scale,
                flag=candidate_id,
                concurrent_phase_count=candidate_row_count,
                variant=variant,
            )
        )
    return tuple(rows)


def _parse_map(map_id: int, path: str, mode: int) -> AltarMapDefinition:
    raw_map = pvf_runtime.read_text(path)
    monster_section = "blood monster" if mode == 1 else "ultimate monster"
    phase_section = "blood phase time" if mode == 1 else "ultimate phase time"
    monsters = _parse_monsters(raw_map, monster_section)
    phases = _parse_phases(
        raw_map,
        phase_section,
        len(monsters),
        with_variant=(mode == 2),
    )
    return AltarMapDefinition(
        map_id=int(map_id),
        path=path,
        monsters=monsters,
        phases=phases,
    )


def _owned_map_paths(
    dungeon_id: int,
    mode: int,
    map_paths: Mapping[int, str],
) -> Mapping[int, str]:
    marker = "towerofbloodyfight/" if mode == 1 else "towerofultimate/"
    result: dict[int, str] = {}
    for map_id, path in map_paths.items():
        if marker not in path.replace("\\", "/").lower():
            continue
        raw_map = pvf_runtime.read_text(path)
        owners = {
            int(token, 0)
            for token in _tokens(_closed_section(raw_map, "dungeon"))
            if re.fullmatch(r"\d+", token)
        }
        if int(dungeon_id) in owners:
            result[int(map_id)] = path
    return MappingProxyType(result)


def _parse_config(
    dungeon_id: int,
    dungeon_path: str,
    map_paths: Mapping[int, str],
) -> AltarDungeonConfig:
    raw_dungeon = pvf_runtime.read_text(dungeon_path)
    mode = _first_int(_open_section(raw_dungeon, "blood dungeon"))
    max_rounds = _first_int(_open_section(raw_dungeon, "blood max round"))
    basis_level = _first_int(_open_section(raw_dungeon, "basis level"), 0)
    start_cell = _cell_pair(raw_dungeon, "start map")
    boss_cell = _cell_pair(raw_dungeon, "boss map")
    if (
        mode not in {1, 2}
        or not 1 <= max_rounds <= 0xFF
        or not 1 <= basis_level <= 0xFF
    ):
        raise ValueError(
            "dungeon %d has invalid altar mode/rounds/level: mode=%d rounds=%d level=%d"
            % (dungeon_id, mode, max_rounds, basis_level)
        )
    owned = _owned_map_paths(dungeon_id, mode, map_paths)
    map_spec_ids = tuple(
        int(token, 0)
        for token in _tokens(_closed_section(raw_dungeon, "map specification"))
        if re.fullmatch(r"\d+", token) and int(token, 0) in owned
    )
    if mode == 1:
        middle_maps = tuple(sorted(set(map_spec_ids)))
        if not middle_maps:
            raise ValueError("dungeon %d has no explicit altar middle maps" % dungeon_id)
        start = [
            mid
            for mid, path in owned.items()
            if mid < min(middle_maps) and "start_" in path.lower()
        ]
        start_maps = tuple(sorted(start))
        boss = [
            mid
            for mid, path in owned.items()
            if mid > max(middle_maps) and "boss_" in path.lower()
        ]
        start_map = max(start) if start else 0
        boss_map = min(boss) if boss else 0
        if start_map + 1 != min(middle_maps) or boss_map - 1 != max(middle_maps):
            raise ValueError(
                "dungeon %d altar map boundary invalid: start=%r middle=%r boss=%r"
                % (dungeon_id, start_map, middle_maps, boss_map)
            )
        selected_map_ids = start_maps + middle_maps + (boss_map,)
        parsed_maps = {
            map_id: _parse_map(map_id, owned[map_id], mode)
            for map_id in selected_map_ids
        }
        phase_sets = {
            mid: {row.round_index for row in parsed_maps[mid].phases}
            for mid in parsed_maps
        }
        expected_phases = set(range(5))
        if any(phases != expected_phases for phases in phase_sets.values()):
            raise ValueError("dungeon %d blood phases are not 0..4" % dungeon_id)
    else:
        selected_map_ids = tuple(sorted(set(map_spec_ids)))
        if len(selected_map_ids) != 1:
            raise ValueError(
                "dungeon %d ultimate altar map specification invalid: %r"
                % (dungeon_id, selected_map_ids)
            )
        parsed_maps = {
            map_id: _parse_map(map_id, owned[map_id], mode)
            for map_id in selected_map_ids
        }
        start_map = selected_map_ids[0]
        start_maps = (start_map,)
        middle_maps = ()
        boss_map = start_map
        phases = {row.round_index for row in parsed_maps[start_map].phases}
        if phases != set(range(max_rounds)):
            raise ValueError(
                "dungeon %d ultimate phases mismatch rounds=%d phases=%r"
                % (dungeon_id, max_rounds, sorted(phases))
            )
        for phase in range(max_rounds):
            expected_variants = {"A"} if phase in {0, max_rounds - 1} else {"A", "B"}
            actual_variants = set(parsed_maps[start_map].phase_variants(phase))
            if actual_variants != expected_variants:
                raise ValueError(
                    "dungeon %d ultimate phase %d variants mismatch expected=%r actual=%r"
                    % (
                        dungeon_id,
                        phase,
                        sorted(expected_variants),
                        sorted(actual_variants),
                    )
                )
    for map_info in parsed_maps.values():
        phases = sorted({row.round_index for row in map_info.phases})
        for phase in phases:
            variants = map_info.phase_variants(phase) or ("",)
            for variant in variants:
                groups = map_info.candidate_groups_for_phase(phase, variant)
                if not groups:
                    raise ValueError(
                        "altar map %d phase %d variant %r has no candidate groups"
                        % (map_info.map_id, phase, variant)
                    )
    return AltarDungeonConfig(
        dungeon_id=int(dungeon_id),
        path=dungeon_path,
        mode=mode,
        max_rounds=max_rounds,
        basis_level=basis_level,
        start_cell=start_cell,
        boss_cell=boss_cell,
        start_map=start_map,
        start_maps=start_maps,
        middle_maps=middle_maps,
        boss_map=boss_map,
        maps=MappingProxyType(parsed_maps),
    )


@lru_cache(maxsize=1)
def _altar_index() -> Mapping[int, AltarDungeonConfig]:
    dungeon_paths = _lst_paths("dungeon/dungeon.lst", "dungeon")
    map_paths = _lst_paths("map/map.lst", "map")
    result: dict[int, AltarDungeonConfig] = {}
    for dungeon_id in sorted(_ALTAR_DUNGEON_IDS):
        path = dungeon_paths.get(dungeon_id, "")
        if not path:
            raise ValueError("altar dungeon %d missing from dungeon.lst" % dungeon_id)
        result[dungeon_id] = _parse_config(dungeon_id, path, map_paths)
    return MappingProxyType(result)


def resolve_altar_config(dungeon_id: int) -> Optional[AltarDungeonConfig]:
    return _altar_index().get(int(dungeon_id))


def _numeric_section(text: str, name: str, *, integer: bool) -> Tuple[float, ...]:
    values: list[float] = []
    section = _closed_section(text, name) or _open_section(text, name)
    for token in _tokens(section):
        try:
            value = int(token, 0) if integer else float(token)
        except ValueError as exc:
            raise ValueError("invalid altar reward section [%s]" % name) from exc
        values.append(float(value))
    return tuple(values)


@lru_cache(maxsize=1)
def resolve_altar_reward_config() -> AltarRewardConfig:
    raw = pvf_runtime.read_text(_ALTAR_REWARD_PATH)
    probabilities = tuple(
        int(value)
        for value in _numeric_section(raw, "reward item prob", integer=True)
    )
    gold_weights = _numeric_section(raw, "reward gold weight", integer=False)
    reward_exp = _numeric_section(raw, "reward exp weight", integer=False)
    entry_exp = _numeric_section(
        raw,
        "reward exp inout count weight",
        integer=False,
    )
    difficulty_points = tuple(
        int(value)
        for value in _numeric_section(raw, "ultimate difficulty point", integer=True)
    )
    ultimate_values = tuple(
        int(value)
        for value in _numeric_section(raw, "ultimate reward prob", integer=True)
    )
    if len(probabilities) != 3 or len(gold_weights) != 1:
        raise ValueError("altar reward probability/gold shape invalid")
    if not reward_exp or not entry_exp or len(difficulty_points) != 2:
        raise ValueError("altar reward exp/difficulty shape invalid")
    if not ultimate_values or len(ultimate_values) % 3:
        raise ValueError("altar ultimate reward table shape invalid")
    gold_weight, item_weight, weight_per_progress = probabilities
    if (
        gold_weight < 0
        or item_weight < 0
        or weight_per_progress <= 0
        or gold_weight + item_weight > weight_per_progress
        or gold_weights[0] <= 0
        or not math.isfinite(gold_weights[0])
        or any(not math.isfinite(value) or value < 0 for value in reward_exp)
        or any(not math.isfinite(value) or value < 0 for value in entry_exp)
        or any(value < 0 for value in difficulty_points)
    ):
        raise ValueError("altar reward weights are invalid")
    ultimate: dict[int, Tuple[int, int]] = {}
    for offset in range(0, len(ultimate_values), 3):
        point, item_1251_probability, item_1252_probability = ultimate_values[
            offset : offset + 3
        ]
        if (
            point <= 0
            or point in ultimate
            or item_1251_probability < 0
            or item_1252_probability < 0
            or item_1251_probability + item_1252_probability > 100
        ):
            raise ValueError("altar ultimate reward row is invalid")
        ultimate[point] = (item_1251_probability, item_1252_probability)
    return AltarRewardConfig(
        path=_ALTAR_REWARD_PATH,
        gold_candidate_weight=gold_weight,
        item_candidate_weight=item_weight,
        weight_per_progress=weight_per_progress,
        gold_amount_weight=float(gold_weights[0]),
        reward_exp_weights=tuple(float(value) for value in reward_exp),
        entry_count_exp_weights=tuple(float(value) for value in entry_exp),
        ultimate_difficulty_points=(difficulty_points[0], difficulty_points[1]),
        ultimate_reward_probabilities=MappingProxyType(ultimate),
    )


def warmup_altar_index() -> dict[str, int]:
    index = _altar_index()
    reward = resolve_altar_reward_config()
    maps = {map_id for config in index.values() for map_id in config.maps}
    monster_rows = sum(
        len(map_info.monsters)
        for config in index.values()
        for map_info in config.maps.values()
    )
    phase_rows = sum(
        len(map_info.phases)
        for config in index.values()
        for map_info in config.maps.values()
    )
    candidate_groups = 0
    for config in index.values():
        for map_info in config.maps.values():
            for phase in sorted({row.round_index for row in map_info.phases}):
                variants = map_info.phase_variants(phase) or ("",)
                candidate_groups += sum(
                    len(map_info.candidate_groups_for_phase(phase, variant))
                    for variant in variants
                )
    return {
        "altar_dungeon_count": len(index),
        "altar_map_count": len(maps),
        "altar_monster_row_count": monster_rows,
        "altar_phase_row_count": phase_rows,
        "altar_candidate_group_count": candidate_groups,
        "altar_reward_exp_weight_count": len(reward.reward_exp_weights),
        "altar_reward_entry_weight_count": len(reward.entry_count_exp_weights),
        "altar_ultimate_reward_row_count": len(
            reward.ultimate_reward_probabilities
        ),
    }
