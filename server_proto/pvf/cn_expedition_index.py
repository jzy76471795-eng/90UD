"""Read the supplied 2018 expedition rules from the active server PVF."""

from dataclasses import dataclass
import math
import struct
from threading import Lock
from types import MappingProxyType
from typing import Mapping

from game.character.item_metadata import CHARACTER_JOB_LABELS
from pvf import pvf_runtime
from pvf.adventure_group_feature_index import _closed_sections, _field_values, _lines, _untick


EXPEDITION_PATH = "etc/adventurersystem/adventurerexpeditionsystem.etc"
ADVENTURER_2018_PATH = "etc/adventurersystem/adventurersystem2018.etc"
EXPEDITION_HOURS = (6, 12, 18, 24)
ALLOW_GROUP_C = "[group c allow]"
COLLECTION_JOB_IDS = {f"[{name}]": job for job, name in enumerate((*CHARACTER_JOB_LABELS, "gun blader"))}


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


@dataclass(frozen=True)
class ExpeditionArea:
    area_id: int
    name: str
    required_level: int
    attribute_groups: tuple[str, ...]
    uniqueness: tuple[tuple[str, str], ...]
    reward_coefficients: tuple[float, ...]
    reward_condition_blind: int


@dataclass(frozen=True)
class ExpeditionIndex:
    areas: Mapping[int, ExpeditionArea]
    attributes: Mapping[str, int]
    groups: Mapping[str, tuple[str, ...]]
    characters: Mapping[tuple[int, int, int], tuple[str, ...]]
    collection_characters: frozenset[tuple[int, int, int]]
    rotation_days: int
    extra_rate: float
    party_rates: tuple[float, ...]
    attribute_rates: tuple[float, ...]
    glory_cap: int

    def matches(self, actual: tuple[str, ...], required: str) -> bool:
        if required == ALLOW_GROUP_C:
            return any(attribute in actual for attribute in self.groups["[group c]"])
        return required in actual

    def glory_reward(self, area_id: int, hours: int, party_size: int, matched_attributes: int) -> int:
        """Use the calculation displayed by the supplied CN expedition confirmation."""
        coefficient = self.areas[area_id].reward_coefficients[EXPEDITION_HOURS.index(hours)]
        party_reward = _float32(self.party_rates[party_size] * coefficient)
        matched_reward = _float32(self.attribute_rates[matched_attributes] * party_reward)
        rounded_reward = int(_float32(matched_reward + 0.5))
        return int(_float32(rounded_reward * self.extra_rate))


def _section(lines: list[str], tag: str) -> list[str]:
    (section,) = _closed_sections(lines, tag)
    return section


def _value(lines: list[str], tag: str) -> str:
    (value,) = _field_values(lines, tag)
    return _untick(value)


def _values(lines: list[str]) -> tuple[str, ...]:
    return tuple(_untick(value).casefold() for value in lines)


def _rates(lines: list[str], count: int) -> tuple[float, ...]:
    rates = tuple(map(float, lines))
    if len(rates) != count or any(rate <= 0 or not math.isfinite(rate) for rate in rates):
        raise ValueError("invalid CN expedition reward rates")
    return rates


def _collection_characters(adventure: list[str]) -> frozenset[tuple[int, int, int]]:
    groups = _closed_sections(_section(adventure, "expert list"), "group")
    characters = set()
    for group_id, group in enumerate(groups):
        slots = [i for i, item in enumerate(group) if item == "[info]"]
        if not group or int(group[0]) != group_id or not 1 <= len(slots) <= 5:
            raise ValueError("invalid CN collection group")
        for pos in slots:
            job_name, stage, branch = _values(group[pos + 1:pos + 4])
            job = COLLECTION_JOB_IDS.get(job_name)
            branch_id = int(branch)
            if job is None or stage not in ("second awakening", "second realization") or not 1 <= branch_id <= 5:
                raise ValueError("invalid CN collection character")
            key = (job, branch_id, 2)
            if key in characters:
                raise ValueError("duplicate CN collection character")
            characters.add(key)
    if len(groups) != 15:
        raise ValueError("invalid CN collection group count")
    return frozenset(characters)


def parse_cn_expedition_index(expedition: str, adventure: str) -> ExpeditionIndex:
    lines = _lines(expedition)
    attributes = {}
    attribute_rows = _section(lines, "attribute data")
    for start in range(0, len(attribute_rows), 4):
        attr_id, attr, _group, _frame = attribute_rows[start:start + 4]
        name = _untick(attr).casefold()
        if name in attributes or int(attr_id) <= 0:
            raise ValueError("invalid CN expedition attribute")
        attributes[name] = int(attr_id)
    if len(set(attributes.values())) != len(attributes) or ALLOW_GROUP_C not in attributes:
        raise ValueError("CN expedition attribute IDs are not unique")

    groups = {}
    for group_name, *members in _closed_sections(_section(lines, "attribute group"), "group"):
        group_name = _untick(group_name).casefold()
        members = _values(members)
        if group_name in groups or not members or len(set(members)) != len(members) or not set(members) <= attributes.keys():
            raise ValueError("invalid CN expedition attribute group")
        groups[group_name] = members
    if "[group c]" not in groups:
        raise ValueError("CN expedition rules missing group C")

    characters = {}
    rows = _section(lines, "character attribute")
    for start in range(0, len(rows), 6):
        job, branch, awakening, *raw = rows[start:start + 6]
        key = (int(job), int(branch), int(awakening))
        names = _values(raw)
        if key in characters or len(set(names)) != 3 or not set(names) <= attributes.keys():
            raise ValueError("invalid CN expedition character attribute mapping")
        characters[key] = names

    areas = {}
    for area in _closed_sections(_section(lines, "area info"), "area"):
        area_id = int(_value(area, "index"))
        level = int(_value(area, "required level"))
        groups_for_area = _values(_section(area, "designated attribute"))
        reward = _section(area, "reward info")
        hours = tuple(map(int, reward[::2]))
        rules = () if "[uniqueness]" not in area else _values(_section(area, "uniqueness"))
        uniqueness = tuple(zip(rules[::2], rules[1::2], strict=True))
        if (
            area_id in areas or level <= 0 or hours != EXPEDITION_HOURS
            or not 1 <= len(groups_for_area) <= 4
            or not set(groups_for_area) <= groups.keys() | {ALLOW_GROUP_C}
            or any(group not in groups_for_area or scope not in {"global", "local"} for group, scope in uniqueness)
        ):
            raise ValueError("invalid CN expedition area")
        areas[area_id] = ExpeditionArea(
            area_id, _value(area, "name"), level, groups_for_area, uniqueness,
            _rates(reward[1::2], len(EXPEDITION_HOURS)),
            int(_value(area, "reward condition blind")),
        )
    if tuple(areas) != (1, 2, 3, 4):
        raise ValueError("CN expedition requires original areas 1–4")

    rotation_days = int(_value(lines, "area attribute rotation period"))
    bonus = _section(lines, "reward bonus info")
    (extra,) = _rates(_field_values(bonus, "extra rate"), 1)
    adventure_lines = _lines(adventure)
    collection_characters = _collection_characters(adventure_lines)
    if not characters.keys() <= collection_characters:
        raise ValueError("CN expedition attributes refer to an unlisted collection character")
    shops = _closed_sections(_section(adventure_lines, "adventurer shop list"), "shop")
    (glory,) = (shop for shop in shops if _value(shop, "index") == "1")
    glory_cap = int(_value(glory, "max shop point"))
    if rotation_days <= 0 or glory_cap <= 0 or "[reset point]" not in glory:
        raise ValueError("invalid CN expedition rotation or glory parameters")

    return ExpeditionIndex(
        MappingProxyType(areas), MappingProxyType(attributes), MappingProxyType(groups),
        MappingProxyType(characters), collection_characters, rotation_days, extra,
        _rates(_field_values(bonus, "party member rate"), 5),
        _rates(_field_values(bonus, "attribute achieve rate"), 5),
        glory_cap,
    )


_lock = Lock()
_index_source = None
_index = None


def get_cn_expedition_index() -> ExpeditionIndex:
    global _index_source, _index
    reader = pvf_runtime.get_or_open_shared_reader()
    with _lock:
        if reader is not _index_source:
            index = parse_cn_expedition_index(reader.read_text(EXPEDITION_PATH), reader.read_text(ADVENTURER_2018_PATH))
            _index_source, _index = reader, index
        return _index
