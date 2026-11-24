"""PVF-backed initial instance state for Aiken customability equipment."""
from __future__ import annotations

import re
import secrets
import threading
import math
from dataclasses import dataclass
from typing import Any, Callable

from game.character.customability_state import (
    CUSTOMABILITY_OPTION_COUNT,
    CustomAbilityState,
    merge_customability_state,
)
from pvf import pvf_runtime


CUSTOMABILITY_LIST_PATH = "etc/customability/individualcustomability.lst"
CUSTOMABILITY_INFO_PATH = "etc/customability/individualcustominfo.etc"
PART_SET_CUSTOMABILITY_LIST_PATH = "etc/customability/partsetcustomability.lst"
PART_SET_CUSTOMABILITY_INFO_PATH = "etc/customability/partsetcustominfo.etc"
AIKEN_CUSTOM_ABILITY_TYPE = "warriors dungeon"

_GRADE_VALUE_BY_LABEL = {"D": 0, "C": 1, "B": 2, "A": 3}
_LST_PAIR_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_LOCK = threading.RLock()
_INDEX: "CustomAbilityIndex | None" = None


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().replace("`", "").lower().split())


def _closed_sections(text: str, name: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"\[%s\]\s*(.*?)\s*\[/%s\]"
            % (re.escape(name), re.escape(name)),
            str(text or ""),
            re.IGNORECASE | re.DOTALL,
        )
    )


def _one_closed_section(text: str, name: str) -> str:
    sections = _closed_sections(text, name)
    if len(sections) != 1:
        raise ValueError(
            "expected one [%s] section, found %d" % (name, len(sections))
        )
    return sections[0]


def _field_body(text: str, name: str) -> str:
    match = re.search(
        r"\[%s\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)" % re.escape(name),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _field_tick(text: str, name: str) -> str:
    body = _field_body(text, name)
    match = re.search(r"`([^`]*)`", body)
    if match is not None:
        return _normalize_text(match.group(1))
    for line in body.splitlines():
        if line.strip():
            return _normalize_text(line)
    return ""


def _integer_tokens(text: str) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"(?m)^\s*(-?\d+)\s*$", text))


def _option_paths(list_text: str) -> dict[int, str]:
    paths: dict[int, str] = {}
    for raw_id, raw_path in _LST_PAIR_RE.findall(str(list_text or "")):
        option_id = int(raw_id)
        relative = str(raw_path or "").replace("\\", "/").strip().lstrip("/")
        if not 1 <= option_id <= 0xFF or not relative:
            raise ValueError("invalid customability option row id=%d" % option_id)
        if option_id in paths:
            raise ValueError("duplicate customability option id=%d" % option_id)
        path = (
            relative
            if relative.lower().startswith("etc/customability/")
            else "etc/customability/" + relative
        )
        paths[option_id] = path
    if not paths:
        raise ValueError("customability option list is empty")
    return paths


def _validate_option_script(
    option_id: int,
    path: str,
    text: str,
    *,
    require_all_grades: bool = True,
) -> None:
    labels: set[str] = set()
    parameters = _closed_sections(text, "parameter")
    if not parameters:
        raise ValueError(
            "customability option has no [parameter] rows id=%d path=%s"
            % (option_id, path)
        )
    for body in parameters:
        label = _field_tick(body, "custom grade").upper()
        if label not in _GRADE_VALUE_BY_LABEL:
            raise ValueError(
                "invalid customability grade id=%d path=%s grade=%r"
                % (option_id, path, label)
            )
        if label in labels:
            raise ValueError(
                "duplicate customability grade id=%d path=%s grade=%s"
                % (option_id, path, label)
            )
        labels.add(label)
    if require_all_grades and labels != set(_GRADE_VALUE_BY_LABEL):
        raise ValueError(
            "customability option grades must be A/B/C/D id=%d path=%s actual=%s"
            % (option_id, path, sorted(labels))
        )


@dataclass(frozen=True)
class CustomAbilityOptionGroup:
    key: str
    option_ids: tuple[int, ...]


@dataclass(frozen=True)
class CustomAbilityMaterialCost:
    item_id: int
    count: int


@dataclass(frozen=True)
class CustomAbilityPartSetGroup:
    key: str
    option_ids: tuple[int, ...]
    required_part_count: int = 0


@dataclass(frozen=True)
class CustomAbilityPartSetConfig:
    part_set_index: int
    min_point: int
    max_point: int
    min_part_count: int
    max_part_count: int
    max_option_count: int
    max_points_by_grade_and_count: dict[tuple[int, int], int]
    material_item_id: int
    material_rate: float
    option_paths_by_id: dict[int, str]
    option_points_by_id: dict[int, int]
    option_groups: tuple[CustomAbilityPartSetGroup, ...]

    def group_for_option(
        self,
        option_id: int,
    ) -> CustomAbilityPartSetGroup | None:
        selected = int(option_id)
        for group in self.option_groups:
            if selected in group.option_ids:
                return group
        return None

    def maximum_points(self, grade: int, part_count: int) -> int | None:
        return self.max_points_by_grade_and_count.get(
            (int(grade), int(part_count))
        )

    def total_points(self, option_ids: tuple[int, ...] | list[int]) -> int | None:
        try:
            points = tuple(self.option_points_by_id[int(value)] for value in option_ids)
        except (KeyError, TypeError, ValueError):
            return None
        return sum(points)

    def effective_points(
        self,
        option_ids: tuple[int, ...] | list[int],
        maximum_points: int | None,
    ) -> int | None:
        if maximum_points is None or int(maximum_points) < 0:
            return None
        try:
            points = tuple(self.option_points_by_id[int(value)] for value in option_ids)
        except (KeyError, TypeError, ValueError):
            return None
        remaining = int(maximum_points)
        effective = 0
        for point in points:
            contribution = (int(point) * (remaining + 1000)) // (int(point) + 1000)
            remaining -= contribution
            if remaining < 0:
                return None
            effective += contribution
        return effective

    def material_count_for_points(self, effective_points: int | None) -> int | None:
        if effective_points is None or int(effective_points) < 0:
            return None
        return int(math.floor(int(effective_points) * self.material_rate))


@dataclass(frozen=True)
class CustomAbilityIndex:
    custom_ability_type: str
    option_paths_by_id: dict[int, str]
    option_groups: tuple[CustomAbilityOptionGroup, ...]
    grade_weights: tuple[tuple[int, int], ...]
    option_materials_by_id: dict[int, tuple[CustomAbilityMaterialCost, ...]]
    upgrade_max: int
    upgrade_materials_by_target_grade: tuple[CustomAbilityMaterialCost, ...]
    part_set: CustomAbilityPartSetConfig | None = None

    @property
    def total_grade_weight(self) -> int:
        return sum(weight for _grade, weight in self.grade_weights)

    def group_for_option(self, option_id: int) -> CustomAbilityOptionGroup | None:
        selected = int(option_id)
        for group in self.option_groups:
            if selected in group.option_ids:
                return group
        return None

    def options_share_group(self, left_option_id: int, right_option_id: int) -> bool:
        left = self.group_for_option(left_option_id)
        right = self.group_for_option(right_option_id)
        return left is not None and right is not None and left.key == right.key

    def material_cost(
        self,
        option_id: int,
        grade: int,
    ) -> CustomAbilityMaterialCost | None:
        costs = self.option_materials_by_id.get(int(option_id))
        grade_value = int(grade)
        if costs is None or not 0 <= grade_value < len(costs):
            return None
        return costs[grade_value]

    def upgrade_material_cost(
        self,
        target_grade: int,
    ) -> CustomAbilityMaterialCost | None:
        grade_value = int(target_grade)
        if (
            not 1 <= grade_value < self.upgrade_max
            or grade_value >= len(self.upgrade_materials_by_target_grade)
        ):
            return None
        return self.upgrade_materials_by_target_grade[grade_value]

    def draw_state(
        self,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> CustomAbilityState:
        options: list[int] = []
        for _index in range(CUSTOMABILITY_OPTION_COUNT):
            group_index = int(randbelow(len(self.option_groups)))
            if not 0 <= group_index < len(self.option_groups):
                raise ValueError("randbelow returned an out-of-range group index")
            group = self.option_groups[group_index]
            option_index = int(randbelow(len(group.option_ids)))
            if not 0 <= option_index < len(group.option_ids):
                raise ValueError("randbelow returned an out-of-range option index")
            options.append(group.option_ids[option_index])

        total = self.total_grade_weight
        ticket = int(randbelow(total))
        if not 0 <= ticket < total:
            raise ValueError("randbelow returned an out-of-range grade ticket")
        for grade, weight in self.grade_weights:
            if ticket < weight:
                return CustomAbilityState((options[0], options[1]), grade)
            ticket -= weight
        raise RuntimeError("customability grade draw exhausted its weight table")


def _option_materials(
    info_text: str,
    option_paths: dict[int, str],
) -> dict[int, tuple[CustomAbilityMaterialCost, ...]]:
    result: dict[int, tuple[CustomAbilityMaterialCost, ...]] = {}
    for body in _closed_sections(info_text, "info"):
        index_tokens = _integer_tokens(_field_body(body, "index"))
        if len(index_tokens) != 1:
            raise ValueError("customability material info requires one option index")
        option_id = int(index_tokens[0])
        if option_id not in option_paths:
            raise ValueError(
                "customability material info references unknown option id=%d"
                % option_id
            )
        if option_id in result:
            raise ValueError(
                "duplicate customability material info option id=%d" % option_id
            )

        costs_by_grade: dict[int, CustomAbilityMaterialCost] = {}
        for material_body in _closed_sections(body, "material"):
            label = _field_tick(material_body, "grade").upper()
            if label not in _GRADE_VALUE_BY_LABEL:
                raise ValueError(
                    "invalid customability material grade option=%d grade=%r"
                    % (option_id, label)
                )
            grade = _GRADE_VALUE_BY_LABEL[label]
            values = _integer_tokens(
                _one_closed_section(material_body, "list")
            )
            if len(values) != 2 or values[0] <= 0 or values[1] <= 0:
                raise ValueError(
                    "invalid customability material list option=%d grade=%s"
                    % (option_id, label)
                )
            if grade in costs_by_grade:
                raise ValueError(
                    "duplicate customability material grade option=%d grade=%s"
                    % (option_id, label)
                )
            costs_by_grade[grade] = CustomAbilityMaterialCost(
                item_id=int(values[0]),
                count=int(values[1]),
            )

        if set(costs_by_grade) != set(_GRADE_VALUE_BY_LABEL.values()):
            raise ValueError(
                "customability material grades must be A/B/C/D option=%d"
                % option_id
            )
        result[option_id] = tuple(
            costs_by_grade[grade]
            for grade in range(len(_GRADE_VALUE_BY_LABEL))
        )

    missing = set(option_paths) - set(result)
    if missing:
        raise ValueError(
            "customability options have no material info ids=%s" % sorted(missing)
        )
    return result


def _upgrade_materials(
    info_text: str,
) -> tuple[int, tuple[CustomAbilityMaterialCost, ...]]:
    max_tokens = _integer_tokens(_field_body(info_text, "upgrade max"))
    if len(max_tokens) != 1:
        raise ValueError("customability upgrade max requires one integer")
    upgrade_max = int(max_tokens[0])
    if upgrade_max != len(_GRADE_VALUE_BY_LABEL):
        raise ValueError(
            "Stage3 customability upgrade max must be %d, got %d"
            % (len(_GRADE_VALUE_BY_LABEL), upgrade_max)
        )

    costs_by_grade: dict[int, CustomAbilityMaterialCost] = {}
    for body in _closed_sections(info_text, "upgrade info"):
        label = _field_tick(body, "grade").upper()
        if label not in _GRADE_VALUE_BY_LABEL:
            raise ValueError("invalid customability upgrade grade=%r" % label)
        grade = _GRADE_VALUE_BY_LABEL[label]
        if grade in costs_by_grade:
            raise ValueError("duplicate customability upgrade grade=%s" % label)
        values = _integer_tokens(_one_closed_section(body, "assign material"))
        if len(values) != 2 or values[0] <= 0 or values[1] <= 0:
            raise ValueError(
                "invalid customability upgrade material grade=%s" % label
            )
        costs_by_grade[grade] = CustomAbilityMaterialCost(
            item_id=int(values[0]),
            count=int(values[1]),
        )
    if set(costs_by_grade) != set(_GRADE_VALUE_BY_LABEL.values()):
        raise ValueError("customability upgrades must define A/B/C/D")
    return upgrade_max, tuple(
        costs_by_grade[grade] for grade in range(upgrade_max)
    )


def _one_positive_integer(text: str, name: str) -> int:
    values = _integer_tokens(_field_body(text, name))
    if len(values) != 1 or values[0] <= 0:
        raise ValueError("part-set %s requires one positive integer" % name)
    return int(values[0])


def _part_set_max_points(
    point_body: str,
    *,
    min_part_count: int,
    max_part_count: int,
) -> dict[tuple[int, int], int]:
    raw_tokens = re.findall(
        r"`([^`]*)`|(?m:^\s*(-?\d+)\s*$)",
        _one_closed_section(point_body, "max point table"),
    )
    tokens = [tick if tick else number for tick, number in raw_tokens]
    if not tokens or len(tokens) % 3:
        raise ValueError("part-set max point table is truncated")
    result: dict[tuple[int, int], int] = {}
    for offset in range(0, len(tokens), 3):
        label = str(tokens[offset]).strip().upper()
        if label not in _GRADE_VALUE_BY_LABEL:
            raise ValueError("unknown part-set max point grade=%r" % label)
        grade = _GRADE_VALUE_BY_LABEL[label]
        part_count = int(tokens[offset + 1])
        points = int(tokens[offset + 2])
        key = (grade, part_count)
        if (
            key in result
            or not min_part_count <= part_count <= max_part_count
            or points <= 0
        ):
            raise ValueError(
                "invalid part-set max point row grade=%s count=%d points=%d"
                % (label, part_count, points)
            )
        result[key] = points
    expected = {
        (grade, part_count)
        for grade in _GRADE_VALUE_BY_LABEL.values()
        for part_count in range(min_part_count, max_part_count + 1)
    }
    if set(result) != expected:
        raise ValueError(
            "part-set max point table must define every grade/count pair"
        )
    return result


def _part_set_config(reader: Any) -> CustomAbilityPartSetConfig:
    option_paths = _option_paths(
        reader.read_text(PART_SET_CUSTOMABILITY_LIST_PATH)
    )
    expected_option_ids = set(range(1, 253))
    if set(option_paths) != expected_option_ids:
        raise ValueError(
            "part-set option list must map IDs 1..252 actual=%d"
            % len(option_paths)
        )
    for option_id, path in sorted(option_paths.items()):
        _validate_option_script(
            option_id,
            path,
            reader.read_text(path),
            require_all_grades=False,
        )

    info_text = reader.read_text(PART_SET_CUSTOMABILITY_INFO_PATH)
    point_body = _one_closed_section(info_text, "point info")
    part_set_index = _one_positive_integer(point_body, "part set index")
    min_point = _one_positive_integer(point_body, "min point")
    max_point = _one_positive_integer(point_body, "max point")
    min_part_count = _one_positive_integer(point_body, "min part count")
    max_part_count = _one_positive_integer(point_body, "max part count")
    max_option_count = _one_positive_integer(point_body, "enchant max count")
    if (
        part_set_index > 0xFF
        or max_point < min_point
        or not 1 <= min_part_count <= max_part_count <= 0xFF
        or not 1 <= max_option_count <= 5
    ):
        raise ValueError("part-set point info is outside Stage3 bounds")
    max_points = _part_set_max_points(
        point_body,
        min_part_count=min_part_count,
        max_part_count=max_part_count,
    )

    material_lines = tuple(
        line.strip()
        for line in _one_closed_section(
            point_body,
            "material rate by point",
        ).splitlines()
        if line.strip()
    )
    if len(material_lines) != 2:
        raise ValueError("part-set material rate requires item ID and rate")
    try:
        material_item_id = int(material_lines[0])
        material_rate = float(material_lines[1])
    except (TypeError, ValueError):
        raise ValueError("part-set material rate is invalid")
    if material_item_id <= 0 or not math.isfinite(material_rate) or material_rate <= 0:
        raise ValueError("part-set material rate must be positive")

    spend_tokens = _integer_tokens(
        _one_closed_section(info_text, "spend grade point")
    )
    if not spend_tokens or len(spend_tokens) % 2:
        raise ValueError("part-set spend grade point table is truncated")
    option_points: dict[int, int] = {}
    for offset in range(0, len(spend_tokens), 2):
        option_id = int(spend_tokens[offset])
        points = int(spend_tokens[offset + 1])
        if option_id in option_points or option_id not in option_paths or points <= 0:
            raise ValueError(
                "invalid part-set option points id=%d points=%d"
                % (option_id, points)
            )
        option_points[option_id] = points
    if set(option_points) != set(option_paths):
        raise ValueError("part-set point table must define every option")

    groups: list[CustomAbilityPartSetGroup] = []
    seen_keys: set[str] = set()
    covered: set[int] = set()
    for body in _closed_sections(info_text, "group"):
        key = _field_tick(body, "key")
        option_ids = _integer_tokens(_one_closed_section(body, "indexes"))
        if not key or key in seen_keys or not option_ids:
            raise ValueError("invalid or duplicate part-set group key=%r" % key)
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("duplicate option within part-set group key=%s" % key)
        unknown = set(option_ids) - set(option_paths)
        overlap = set(option_ids) & covered
        if unknown or overlap:
            raise ValueError(
                "invalid part-set group options key=%s unknown=%s overlap=%s"
                % (key, sorted(unknown), sorted(overlap))
            )
        conditions = _closed_sections(body, "condition")
        if len(conditions) > 1:
            raise ValueError("part-set group has multiple conditions key=%s" % key)
        required_part_count = 0
        if conditions:
            raw_condition = re.findall(
                r"`([^`]*)`|(?m:^\s*(-?\d+)\s*$)",
                conditions[0],
            )
            condition = [tick if tick else number for tick, number in raw_condition]
            if (
                len(condition) != 2
                or _normalize_text(condition[0]) != "part count"
            ):
                raise ValueError("unsupported part-set condition key=%s" % key)
            required_part_count = int(condition[1])
            if not min_part_count <= required_part_count <= max_part_count:
                raise ValueError("invalid part-set count condition key=%s" % key)
        seen_keys.add(key)
        covered.update(option_ids)
        groups.append(
            CustomAbilityPartSetGroup(
                key=key,
                option_ids=option_ids,
                required_part_count=required_part_count,
            )
        )
    if covered != set(option_paths):
        raise ValueError("part-set options must belong to exactly one group")

    return CustomAbilityPartSetConfig(
        part_set_index=part_set_index,
        min_point=min_point,
        max_point=max_point,
        min_part_count=min_part_count,
        max_part_count=max_part_count,
        max_option_count=max_option_count,
        max_points_by_grade_and_count=max_points,
        material_item_id=material_item_id,
        material_rate=material_rate,
        option_paths_by_id=option_paths,
        option_points_by_id=option_points,
        option_groups=tuple(groups),
    )


def build_customability_index(reader: Any) -> CustomAbilityIndex:
    option_paths = _option_paths(reader.read_text(CUSTOMABILITY_LIST_PATH))
    for option_id, path in sorted(option_paths.items()):
        _validate_option_script(option_id, path, reader.read_text(path))

    info_text = reader.read_text(CUSTOMABILITY_INFO_PATH)
    custom_type = _field_tick(info_text, "custom ability type")
    if custom_type != AIKEN_CUSTOM_ABILITY_TYPE:
        raise ValueError(
            "unexpected individual customability type=%r" % custom_type
        )

    groups: list[CustomAbilityOptionGroup] = []
    seen_keys: set[str] = set()
    covered_options: set[int] = set()
    for body in _closed_sections(info_text, "group"):
        key = _field_tick(body, "key")
        option_ids = _integer_tokens(_one_closed_section(body, "indexes"))
        if not key or key in seen_keys or not option_ids:
            raise ValueError(
                "invalid or duplicate customability option group key=%r" % key
            )
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("duplicate option within customability group key=%s" % key)
        missing = tuple(
            option_id for option_id in option_ids if option_id not in option_paths
        )
        if missing:
            raise ValueError(
                "customability group references unknown options key=%s ids=%s"
                % (key, missing)
            )
        seen_keys.add(key)
        covered_options.update(option_ids)
        groups.append(CustomAbilityOptionGroup(key, option_ids))
    if not groups:
        raise ValueError("customability option groups are empty")
    unused = set(option_paths) - covered_options
    if unused:
        raise ValueError(
            "customability options are not assigned to a group ids=%s"
            % sorted(unused)
        )

    probability_tokens = re.findall(
        r"`([^`]*)`|(?m:^\s*(-?\d+)\s*$)",
        _one_closed_section(info_text, "grade probability"),
    )
    flattened = [tick if tick else number for tick, number in probability_tokens]
    if len(flattened) % 2:
        raise ValueError("customability grade probability table is truncated")
    grade_weights: list[tuple[int, int]] = []
    seen_grades: set[int] = set()
    for offset in range(0, len(flattened), 2):
        label = flattened[offset].strip().upper()
        if label not in _GRADE_VALUE_BY_LABEL:
            raise ValueError("unknown customability probability grade=%r" % label)
        grade = _GRADE_VALUE_BY_LABEL[label]
        weight = int(flattened[offset + 1])
        if grade in seen_grades or weight <= 0:
            raise ValueError("invalid customability probability grade=%s" % label)
        seen_grades.add(grade)
        grade_weights.append((grade, weight))
    if seen_grades != set(_GRADE_VALUE_BY_LABEL.values()):
        raise ValueError("customability probabilities must define A/B/C/D")

    option_materials = _option_materials(info_text, option_paths)
    upgrade_max, upgrade_materials = _upgrade_materials(info_text)
    part_set = _part_set_config(reader)

    return CustomAbilityIndex(
        custom_ability_type=custom_type,
        option_paths_by_id=option_paths,
        option_groups=tuple(groups),
        grade_weights=tuple(grade_weights),
        option_materials_by_id=option_materials,
        upgrade_max=upgrade_max,
        upgrade_materials_by_target_grade=upgrade_materials,
        part_set=part_set,
    )


def install_customability_index(index: CustomAbilityIndex) -> None:
    if not isinstance(index, CustomAbilityIndex):
        raise TypeError("customability index has an invalid type")
    if (
        not index.option_groups
        or index.total_grade_weight <= 0
        or set(index.option_materials_by_id) != set(index.option_paths_by_id)
        or index.upgrade_max != len(_GRADE_VALUE_BY_LABEL)
        or len(index.upgrade_materials_by_target_grade) != index.upgrade_max
        or (
            index.part_set is not None
            and set(index.part_set.option_paths_by_id)
            != set(index.part_set.option_points_by_id)
        )
    ):
        raise ValueError("customability index is incomplete")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_customability_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_customability_index() -> CustomAbilityIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("customability index has not been warmed")
    return index


def warmup_customability_index() -> dict[str, int | str]:
    from config import PVF_PATH

    reader = pvf_runtime.get_shared_reader(PVF_PATH)
    if reader is None or not pvf_runtime.is_memory_prepared(PVF_PATH):
        raise RuntimeError(
            "customability warmup requires the prepared shared PVF reader"
        )
    index = build_customability_index(reader)
    install_customability_index(index)
    return {
        "ok": 1,
        "custom_ability_type": index.custom_ability_type,
        "option_count": len(index.option_paths_by_id),
        "group_count": len(index.option_groups),
        "grade_count": len(index.grade_weights),
        "grade_weight_total": index.total_grade_weight,
        "material_option_count": len(index.option_materials_by_id),
        "upgrade_max": index.upgrade_max,
        "upgrade_material_count": len(index.upgrade_materials_by_target_grade),
        "part_set_option_count": len(index.part_set.option_paths_by_id),
        "part_set_group_count": len(index.part_set.option_groups),
        "part_set_max_point_rows": len(
            index.part_set.max_points_by_grade_and_count
        ),
    }


def create_initial_customability_extra_json(
    metadata: Any,
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> str:
    custom_type = _normalize_text(getattr(metadata, "custom_ability_type", ""))
    if custom_type != AIKEN_CUSTOM_ABILITY_TYPE:
        return "{}"
    if str(getattr(metadata, "item_kind", "")) != "equipment":
        raise ValueError("customability type is only valid on equipment")
    count = int(getattr(metadata, "custom_ability_count", 0) or 0)
    if count != CUSTOMABILITY_OPTION_COUNT:
        raise ValueError(
            "Stage3 customability equipment requires exactly %d options, got %d"
            % (CUSTOMABILITY_OPTION_COUNT, count)
        )
    state = get_customability_index().draw_state(randbelow=randbelow)
    return merge_customability_state(
        "{}",
        state.individual_options,
        state.grade,
    )


__all__ = [
    "AIKEN_CUSTOM_ABILITY_TYPE",
    "CUSTOMABILITY_INFO_PATH",
    "CUSTOMABILITY_LIST_PATH",
    "PART_SET_CUSTOMABILITY_INFO_PATH",
    "PART_SET_CUSTOMABILITY_LIST_PATH",
    "CustomAbilityIndex",
    "CustomAbilityMaterialCost",
    "CustomAbilityOptionGroup",
    "CustomAbilityPartSetConfig",
    "CustomAbilityPartSetGroup",
    "build_customability_index",
    "clear_customability_index",
    "create_initial_customability_extra_json",
    "get_customability_index",
    "install_customability_index",
    "warmup_customability_index",
]
