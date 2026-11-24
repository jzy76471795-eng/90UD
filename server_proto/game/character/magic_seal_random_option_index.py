"""PVF-derived pools for Stage3 magic-seal unseal and option changes."""
from __future__ import annotations

import posixpath
import re
import secrets
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from pvf import pvf_runtime


OVERALL_PATH = "etc/randomoption/randomizedoptionoverall2.etc"
SELECTION_PATH = "etc/randomoption/optiongroupselection.etc"
GROUPING_PATH = "etc/randomoption/optiongrouping.etc"
OPTION_LIST_PATH = "etc/randomoption/randomoption.lst"

_SECTION_TEMPLATE = r"\[{name}\]\s*(.*?)\[/{name}\]"
_TOKEN_RE = re.compile(r"`([^`]*)`|(-?\d+(?:\.\d+)?)")
_LST_PAIR_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_ELEMENT_OPTION_NAMES = {
    244: "fireelement",
    245: "waterelement",
    246: "darkelement",
    247: "lightelement",
}
_ARMOR_PREFIXES = {
    "cloth": "cl",
    "leather": "lt",
    "larmor": "la",
    "harmor": "ha",
    "plate": "mt",
}
_WEAPON_KEY_ALIASES = {
    "boxglove": "bglove",
    "hsword": "lswd",
    "beamsword": "beamswd",
    "twinsword": "twinswd",
    "chakram": "chakraweapon",
}
_LOCK = threading.RLock()
_INDEX: "MagicSealRandomOptionIndex | None" = None
MAX_RANDOM_OPTION_COUNT = 3


def _normalize_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = text.strip("`[](){}<> ")
    for token in ("_", "-", " "):
        text = text.replace(token, "")
    return text


def _sections(text: str, name: str) -> list[str]:
    return re.findall(
        _SECTION_TEMPLATE.format(name=re.escape(str(name))),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )


def _one_section(text: str, name: str) -> str:
    matches = _sections(text, name)
    if len(matches) != 1:
        raise ValueError(
            "expected one [%s] section, found %d" % (name, len(matches))
        )
    return matches[0]


def _typed_tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(str(text or "")):
        if match.group(1) is not None:
            out.append(("text", match.group(1)))
        else:
            out.append(("number", match.group(2)))
    return out


def _integer_value(token: tuple[str, str]) -> int | None:
    if token[0] != "number":
        return None
    try:
        value = Decimal(token[1])
    except InvalidOperation:
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


def _integer_tokens(text: str, *, context: str) -> list[int]:
    out: list[int] = []
    for token in _typed_tokens(text):
        value = _integer_value(token)
        if value is None:
            if token[0] == "text":
                continue
            raise ValueError("non-integer token in %s: %r" % (context, token[1]))
        out.append(value)
    return out


def _encode_option_scalar(raw: str) -> int:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("invalid random-option scalar=%r" % raw) from exc
    if not value.is_finite():
        raise ValueError("non-finite random-option scalar=%r" % raw)
    encoded = int(value)
    encoded = max(-128, min(255, encoded))
    return encoded & 0xFF


@dataclass(frozen=True)
class WeightedOptionID:
    option_id: int
    weight: int


@dataclass(frozen=True)
class MagicSealOptionLevel:
    level: int
    value1: int
    value2: int


@dataclass(frozen=True)
class MagicSealOptionDefinition:
    option_id: int
    levels: tuple[MagicSealOptionLevel, ...]
    source_path: str

    def values_for_level(self, target_level: int) -> tuple[int, int]:
        chosen: MagicSealOptionLevel | None = None
        level = max(0, int(target_level))
        for row in self.levels:
            if row.level > level:
                break
            chosen = row
        if chosen is None:
            raise ValueError(
                "option %d has no value tier for target level %d"
                % (self.option_id, level)
            )
        return int(chosen.value1), int(chosen.value2)


@dataclass(frozen=True)
class MagicSealModificationCost:
    level: int
    common_cost: int
    unique_cost: int


@dataclass(frozen=True)
class MagicSealEquipmentRule:
    rarity: int
    minimum_level: int
    equipment_key: str
    group_ids: tuple[int, int, int]
    gold_cost: int


@dataclass(frozen=True)
class MagicSealRandomOptionCandidate:
    option_type: int
    value1: int
    value2: int
    weight: int

    def as_entry(self) -> dict[str, int]:
        return {
            "type": int(self.option_type) & 0xFF,
            "value1": int(self.value1) & 0xFF,
            "value2": int(self.value2) & 0xFF,
        }


@dataclass(frozen=True)
class MagicSealRandomOptionIndex:
    unseal_selections: dict[int, dict[str, tuple[int, int, int]]]
    modified_selections: dict[int, dict[str, tuple[int, int, int]]]
    groups: dict[int, tuple[WeightedOptionID, ...]]
    options: dict[int, MagicSealOptionDefinition]
    modification_costs: tuple[MagicSealModificationCost, ...]

    def _resolve_equipment_from(
        self,
        metadata: Any,
        selections_by_rarity: dict[int, dict[str, tuple[int, int, int]]],
    ) -> MagicSealEquipmentRule | None:
        if not bool(getattr(metadata, "is_equipment", False)):
            return None
        rarity = int(getattr(metadata, "rarity", 0) or 0)
        selections = selections_by_rarity.get(rarity)
        if not selections:
            return None
        key = ""
        group_ids: tuple[int, int, int] | None = None
        for candidate in equipment_selection_keys(
            getattr(metadata, "source_path", ""),
            getattr(metadata, "equipment_type", ""),
        ):
            selected = selections.get(candidate)
            if selected is not None:
                key = candidate
                group_ids = selected
                break
        if group_ids is None:
            return None
        minimum_level = max(0, int(getattr(metadata, "minimum_level", 0) or 0))
        return MagicSealEquipmentRule(
            rarity=rarity,
            minimum_level=minimum_level,
            equipment_key=key,
            group_ids=group_ids,
            gold_cost=self.modification_gold_cost(rarity, minimum_level),
        )

    def resolve_equipment(self, metadata: Any) -> MagicSealEquipmentRule | None:
        """Resolve the Seria single-option modification groups."""
        return self._resolve_equipment_from(metadata, self.modified_selections)

    def resolve_unseal_equipment(
        self,
        metadata: Any,
    ) -> MagicSealEquipmentRule | None:
        """Resolve the initial unseal groups from ``[choose option group]``."""
        return self._resolve_equipment_from(metadata, self.unseal_selections)

    def modification_gold_cost(self, rarity: int, target_level: int) -> int:
        chosen: MagicSealModificationCost | None = None
        level = max(0, int(target_level))
        for row in self.modification_costs:
            if row.level > level:
                break
            chosen = row
        if chosen is None:
            raise ValueError(
                "random-option modification cost missing rarity=%d level=%d"
                % (int(rarity), level)
            )
        return (
            int(chosen.unique_cost)
            if int(rarity) >= 3
            else int(chosen.common_cost)
        )

    def candidates(
        self,
        metadata: Any,
        *,
        option_index: int,
        excluded_types: Iterable[int] = (),
        unseal: bool = False,
    ) -> tuple[MagicSealRandomOptionCandidate, ...]:
        rule = (
            self.resolve_unseal_equipment(metadata)
            if unseal
            else self.resolve_equipment(metadata)
        )
        index = int(option_index)
        if rule is None:
            return ()
        if index < 0 or index >= len(rule.group_ids):
            return ()
        group_id = int(rule.group_ids[index])
        weighted = self.groups.get(group_id, ())
        excluded = {int(value) & 0xFF for value in excluded_types}
        out: list[MagicSealRandomOptionCandidate] = []
        for entry in weighted:
            definition = self.options.get(entry.option_id)
            if definition is None:
                raise RuntimeError(
                    "random-option definition missing option=%d" % entry.option_id
                )
            if definition.option_id in excluded:
                continue
            value1, value2 = definition.values_for_level(rule.minimum_level)
            out.append(
                MagicSealRandomOptionCandidate(
                    option_type=definition.option_id,
                    value1=value1,
                    value2=value2,
                    weight=entry.weight,
                )
            )
        return tuple(out)

    @staticmethod
    def _roll_candidate(
        candidates: tuple[MagicSealRandomOptionCandidate, ...],
        *,
        option_index: int,
        pool_name: str,
        rng: Any = None,
    ) -> MagicSealRandomOptionCandidate:
        if not candidates:
            raise RuntimeError(
                "magic-seal %s pool is empty option_index=%d"
                % (pool_name, int(option_index))
            )
        total = sum(max(0, int(candidate.weight)) for candidate in candidates)
        if total <= 0:
            raise RuntimeError(
                "magic-seal %s pool has no positive weights" % pool_name
            )
        roll = (
            int(rng.randrange(total))
            if rng is not None
            else int(secrets.randbelow(total))
        )
        cursor = 0
        for candidate in candidates:
            cursor += int(candidate.weight)
            if roll < cursor:
                return candidate
        return candidates[-1]

    def roll_modified(
        self,
        metadata: Any,
        *,
        option_index: int,
        excluded_types: Iterable[int] = (),
        rng: Any = None,
    ) -> MagicSealRandomOptionCandidate:
        candidates = self.candidates(
            metadata,
            option_index=option_index,
            excluded_types=excluded_types,
        )
        return self._roll_candidate(
            candidates,
            option_index=option_index,
            pool_name="modified",
            rng=rng,
        )

    def roll_unseal_options(
        self,
        metadata: Any,
        *,
        max_count: int = MAX_RANDOM_OPTION_COUNT,
        rng: Any = None,
    ) -> tuple[MagicSealRandomOptionCandidate, ...]:
        """Roll one to three distinct options for a newly unsealed item.

        Stage3 exposes three random-option slots.  Each slot keeps its own
        PVF group and weight table, while the option count is uniformly rolled
        from the available range.  Types selected by earlier slots are removed
        from later pools so Seria can safely replace any one slot afterward.
        """
        rule = self.resolve_unseal_equipment(metadata)
        if rule is None:
            raise RuntimeError("magic-seal equipment pool is missing")
        capacity = min(
            MAX_RANDOM_OPTION_COUNT,
            len(rule.group_ids),
            max(0, int(max_count)),
        )
        if capacity <= 0:
            raise RuntimeError("magic-seal option capacity is empty")
        option_count = 1 + (
            int(rng.randrange(capacity))
            if rng is not None
            else int(secrets.randbelow(capacity))
        )
        selected: list[MagicSealRandomOptionCandidate] = []
        used_types: set[int] = set()
        for option_index in range(option_count):
            candidates = self.candidates(
                metadata,
                option_index=option_index,
                excluded_types=used_types,
                unseal=True,
            )
            candidate = self._roll_candidate(
                candidates,
                option_index=option_index,
                pool_name="unseal",
                rng=rng,
            )
            option_type = int(candidate.option_type) & 0xFF
            if option_type <= 0 or option_type in used_types:
                raise RuntimeError(
                    "magic-seal unseal produced duplicate option type=%d"
                    % option_type
                )
            selected.append(candidate)
            used_types.add(option_type)
        return tuple(selected)


def equipment_selection_keys(
    source_path: object,
    equipment_type: object,
) -> tuple[str, ...]:
    keys: set[str] = set()

    def add(value: object) -> None:
        normalized = _normalize_key(value)
        if normalized:
            keys.add(normalized)

    normalized_type = _normalize_key(equipment_type)
    add(normalized_type)
    segments = str(source_path or "").lower().replace("\\", "/").split("/")
    for index, segment in enumerate(segments):
        if segment == "weapon" and index + 1 < len(segments):
            weapon = _normalize_key(segments[index + 1])
            add(weapon)
            add(_WEAPON_KEY_ALIASES.get(weapon, ""))
        prefix = _ARMOR_PREFIXES.get(segment)
        if prefix is None:
            continue
        add(prefix + normalized_type)
        if index > 0 and not segments[index - 1].endswith(".equ"):
            add(prefix + _normalize_key(segments[index - 1]))
        if index + 1 < len(segments) and not segments[index + 1].endswith(".equ"):
            add(prefix + _normalize_key(segments[index + 1]))
    return tuple(sorted(keys))


def _parse_equipment_selections(
    text: str,
    *,
    section_name: str,
) -> dict[int, dict[str, tuple[int, int, int]]]:
    tokens = _typed_tokens(_one_section(text, section_name))
    out: dict[int, dict[str, tuple[int, int, int]]] = {}
    for index, token in enumerate(tokens):
        if token[0] != "text" or index < 2:
            continue
        rarity = _integer_value(tokens[index - 2])
        marker = _integer_value(tokens[index - 1])
        if rarity not in (2, 3) or marker != -1:
            continue
        group_ids: list[int] = []
        for following in tokens[index + 1 :]:
            value = _integer_value(following)
            if value is not None and 1000 <= value < 2000:
                group_ids.append(value)
                if len(group_ids) == 3:
                    break
        if len(group_ids) != 3:
            raise ValueError(
                "%s has no three groups rarity=%d key=%s"
                % (section_name, rarity, token[1])
            )
        key = _normalize_key(token[1])
        target = out.setdefault(rarity, {})
        groups = (group_ids[0], group_ids[1], group_ids[2])
        if not key:
            raise ValueError(
                "empty %s key rarity=%d" % (section_name, rarity)
            )
        # The current PVF intentionally repeats some rarity/key rows. Its
        # consumer keeps the last declaration, so preserve that order here.
        target[key] = groups
    if not out.get(2) or not out.get(3):
        raise ValueError(
            "%s requires rarity 2 and 3 entries" % section_name
        )
    return out


def parse_unseal_selections(
    text: str,
) -> dict[int, dict[str, tuple[int, int, int]]]:
    return _parse_equipment_selections(
        text,
        section_name="choose option group",
    )


def parse_modified_selections(
    text: str,
) -> dict[int, dict[str, tuple[int, int, int]]]:
    return _parse_equipment_selections(
        text,
        section_name="modified option selection",
    )


def parse_option_groups(text: str) -> dict[int, tuple[WeightedOptionID, ...]]:
    out: dict[int, tuple[WeightedOptionID, ...]] = {}
    for body in _sections(text, "option group"):
        values = _integer_tokens(body, context="option group")
        if len(values) < 3 or (len(values) - 1) % 2:
            raise ValueError("truncated random-option group")
        group_id = int(values[0])
        entries = tuple(
            WeightedOptionID(int(values[offset]), int(values[offset + 1]))
            for offset in range(1, len(values), 2)
        )
        if (
            group_id <= 0
            or group_id in out
            or any(
                entry.option_id <= 0
                or entry.option_id > 0xFF
                or entry.weight <= 0
                for entry in entries
            )
        ):
            raise ValueError("invalid random-option group=%d" % group_id)
        out[group_id] = entries
    if not out:
        raise ValueError("random-option grouping has no groups")
    return out


def parse_option_paths(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    base = posixpath.dirname(OPTION_LIST_PATH)
    for raw_id, raw_path in _LST_PAIR_RE.findall(str(text or "")):
        option_id = int(raw_id)
        relative = str(raw_path or "").replace("\\", "/").strip()
        path = posixpath.normpath(posixpath.join(base, relative))
        if option_id <= 0 or option_id > 0xFF or option_id in out:
            raise ValueError("invalid or duplicate random-option path id=%d" % option_id)
        out[option_id] = path
    if not out:
        raise ValueError("randomoption.lst has no option paths")
    return out


def parse_option_definition(
    text: str,
    *,
    expected_option_id: int,
    source_path: str,
) -> MagicSealOptionDefinition:
    dungeon_sections = _sections(text, "dungeon")
    if not dungeon_sections:
        raise ValueError("random-option dungeon block missing path=%s" % source_path)
    dungeon = dungeon_sections[0]
    option_match = re.search(r"\[option\]\s*(-?\d+)", dungeon, re.IGNORECASE)
    option_id = int(option_match.group(1)) if option_match is not None else 0
    if option_id <= 0 or option_id > 0xFF:
        raise ValueError(
            "random-option identity invalid list_id=%d option=%d path=%s"
            % (int(expected_option_id), option_id, source_path)
        )
    levels: list[MagicSealOptionLevel] = []
    expected_element = _ELEMENT_OPTION_NAMES.get(option_id)
    for match in re.finditer(
        r"\[level\]\s*(-?\d+)\s*(.*?)\[/level\]",
        dungeon,
        re.IGNORECASE | re.DOTALL,
    ):
        level = int(match.group(1))
        body = match.group(2)
        if level < 0:
            raise ValueError("negative random-option level path=%s" % source_path)
        if expected_element is not None:
            endpoints = [
                _normalize_key(value)
                for kind, value in _typed_tokens(body)
                if kind == "text"
            ]
            if endpoints != [expected_element, expected_element]:
                raise ValueError(
                    "element random-option endpoints invalid option=%d level=%d"
                    % (option_id, level)
                )
            value1, value2 = 1, 1
        else:
            numbers = [
                value
                for kind, value in _typed_tokens(body)
                if kind == "number"
            ]
            if len(numbers) < 2:
                raise ValueError(
                    "random-option values missing option=%d level=%d"
                    % (option_id, level)
                )
            value1 = _encode_option_scalar(numbers[-2])
            value2 = _encode_option_scalar(numbers[-1])
        levels.append(MagicSealOptionLevel(level, value1, value2))
    ordered = tuple(sorted(levels, key=lambda row: row.level))
    if not ordered or len({row.level for row in ordered}) != len(ordered):
        raise ValueError(
            "random-option levels missing or duplicated option=%d"
            % option_id
        )
    return MagicSealOptionDefinition(
        option_id=option_id,
        levels=ordered,
        source_path=str(source_path),
    )


def parse_modification_costs(text: str) -> tuple[MagicSealModificationCost, ...]:
    values = _integer_tokens(
        _one_section(text, "option modification"),
        context="option modification",
    )
    if not values or len(values) % 3:
        raise ValueError("random-option modification cost table is truncated")
    rows = tuple(
        MagicSealModificationCost(
            level=int(values[offset]),
            common_cost=int(values[offset + 1]),
            unique_cost=int(values[offset + 2]),
        )
        for offset in range(0, len(values), 3)
    )
    if any(
        row.level < 0 or row.common_cost < 0 or row.unique_cost < 0
        for row in rows
    ) or len({row.level for row in rows}) != len(rows):
        raise ValueError("invalid random-option modification cost table")
    return tuple(sorted(rows, key=lambda row: row.level))


def build_magic_seal_random_option_index_from_reader(
    reader: Any,
) -> MagicSealRandomOptionIndex:
    selection_text = reader.read_text(SELECTION_PATH)
    unseal_selections = parse_unseal_selections(selection_text)
    modified_selections = parse_modified_selections(selection_text)
    groups = parse_option_groups(reader.read_text(GROUPING_PATH))
    paths = parse_option_paths(reader.read_text(OPTION_LIST_PATH))
    costs = parse_modification_costs(reader.read_text(OVERALL_PATH))
    referenced_groups = {
        group_id
        for selection_table in (unseal_selections, modified_selections)
        for rarity_entries in selection_table.values()
        for group_ids in rarity_entries.values()
        for group_id in group_ids
    }
    missing_groups = sorted(referenced_groups - set(groups))
    if missing_groups:
        raise ValueError(
            "modified random-option groups missing ids=%s" % missing_groups
        )
    referenced_options = {
        entry.option_id
        for group_id in referenced_groups
        for entry in groups[group_id]
    }
    missing_paths = sorted(referenced_options - set(paths))
    if missing_paths:
        raise ValueError(
            "modified random-option paths missing ids=%s" % missing_paths
        )
    options = {
        option_id: parse_option_definition(
            reader.read_text(paths[option_id]),
            expected_option_id=option_id,
            source_path=paths[option_id],
        )
        for option_id in sorted(referenced_options)
    }
    return MagicSealRandomOptionIndex(
        unseal_selections=unseal_selections,
        modified_selections=modified_selections,
        groups={group_id: groups[group_id] for group_id in referenced_groups},
        options=options,
        modification_costs=costs,
    )


def install_magic_seal_random_option_index(
    index: MagicSealRandomOptionIndex,
) -> None:
    if not isinstance(index, MagicSealRandomOptionIndex) or not index.options:
        raise ValueError("invalid magic-seal random-option index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_magic_seal_random_option_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_magic_seal_random_option_index() -> MagicSealRandomOptionIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("magic-seal random-option index is not warmed")
    return index


def warmup_magic_seal_random_option_index() -> dict[str, Any]:
    reader = pvf_runtime.ensure_shared_reader()
    index = build_magic_seal_random_option_index_from_reader(reader)
    install_magic_seal_random_option_index(index)
    return {
        "ok": 1,
        "unseal_selection_count": sum(
            len(entries) for entries in index.unseal_selections.values()
        ),
        "selection_count": sum(
            len(entries) for entries in index.modified_selections.values()
        ),
        "group_count": len(index.groups),
        "option_count": len(index.options),
        "cost_row_count": len(index.modification_costs),
    }


__all__ = [
    "GROUPING_PATH",
    "MagicSealEquipmentRule",
    "MagicSealModificationCost",
    "MagicSealOptionDefinition",
    "MagicSealOptionLevel",
    "MagicSealRandomOptionCandidate",
    "MagicSealRandomOptionIndex",
    "MAX_RANDOM_OPTION_COUNT",
    "OPTION_LIST_PATH",
    "OVERALL_PATH",
    "SELECTION_PATH",
    "WeightedOptionID",
    "build_magic_seal_random_option_index_from_reader",
    "clear_magic_seal_random_option_index",
    "equipment_selection_keys",
    "get_magic_seal_random_option_index",
    "install_magic_seal_random_option_index",
    "parse_modification_costs",
    "parse_modified_selections",
    "parse_unseal_selections",
    "parse_option_definition",
    "parse_option_groups",
    "parse_option_paths",
    "warmup_magic_seal_random_option_index",
]
