# -*- coding: utf-8 -*-
"""PVF ``itemdropinfo_monster_hell.etc`` reader for Hell Party equipment.

The normal PVF drop table uses ``[grade]``. Actor-tier Epic rewards instead
use the crafted equipment's real ``[minimum level]``. Both indexes are
prewarmed so the DIE path never re-scans ``equipment/*.equ``.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import extract_section, parse_ints, parse_lst_entries
from pvf import pvf_runtime

HELL_DROP_PATH = "etc/itemdropinfo_monster_hell.etc"
HELL_DROP_CHANCE_DENOMINATOR = 1001
HELL_EQUIPMENT_LST_PATH = "equipment/equipment.lst"
HELL_COMMON_RARITY = 0
HELL_UNCOMMON_RARITY = 1
HELL_RARE_RARITY = 2
HELL_UNIQUE_RARITY = 3
HELL_EPIC_RARITY = 4
HELL_CHRONICLE_RARITY = 5
HELL_LEGENDARY_RARITY = 6
HELL_RARITY_ROLL_DENOMINATOR = 1000000
_INT_FIELD_RE = r"(?m)^\s*(-?\d+)\s*$"
HELL_EPIC_MIN_LEVEL = 50
HELL_EPIC_MAX_LEVEL = 90


def hell_epic_grade_levels(monster_level: int) -> Tuple[int, ...]:
    """Return the Hell Epic equipment-level tiers selected by the killed actor.

    The supported range is 50-90. The level bands are inclusive at their
    upper end: 81-85 selects 80/85, 86-90 selects 85/90, and actors above
    level 90 select only level 90.
    """
    level = max(HELL_EPIC_MIN_LEVEL, int(monster_level))
    if level > HELL_EPIC_MAX_LEVEL:
        return (HELL_EPIC_MAX_LEVEL,)
    upper = max(HELL_EPIC_MIN_LEVEL, ((level + 4) // 5) * 5)
    lower = max(HELL_EPIC_MIN_LEVEL, upper - 5)
    if lower >= upper:
        return (upper,)
    return (lower, upper)


@dataclass(frozen=True)
class HellDropProbRow:
    level_min: int
    level_max: int
    difficulty_rates: Tuple[int, int, int, int, int]


@dataclass(frozen=True)
class HellGradeWindow:
    down: int
    up: int


@dataclass(frozen=True)
class HellEquipmentEntry:
    item_id: int
    grade: int
    rarity: int
    weight: int
    source_path: str = ""
    minimum_level: int = 0


class HellDropConfig:
    """Immutable-at-runtime view of Hell Party PVF data."""

    def __init__(self) -> None:
        self.drop_probs: List[HellDropProbRow] = []
        self.rarity_thresholds: List[List[int]] = []
        self.grade_ranges: List[HellGradeWindow] = [
            HellGradeWindow(0, 1) for _ in range(200)
        ]
        self.equipment_by_grade_rarity: Dict[Tuple[int, int], List[HellEquipmentEntry]] = {}
        self.equipment_by_level_rarity: Dict[Tuple[int, int], List[HellEquipmentEntry]] = {}
        # Derived from the same equipment.lst/.equ rows.  Wish validation
        # uses this identity index instead of opening PVF files on demand.
        self.equipment_by_item_id: Dict[int, HellEquipmentEntry] = {}
        self.equipment_count = 0
        self.registration_rejected_count = 0
        self.loaded = False
        self.source_path = HELL_DROP_PATH

    def load_from_text(self, text: str) -> None:
        self._parse_drop_probs(text)
        self._parse_rarity_thresholds(text)
        self._parse_grade_ranges(text)
        self._load_equipment_index()
        self.loaded = True

    def to_cache_payload(self) -> dict:
        """Return the complete immutable drop index in JSON-safe form."""
        return {
            "source_path": self.source_path,
            "drop_probs": [
                [row.level_min, row.level_max, *row.difficulty_rates]
                for row in self.drop_probs
            ],
            "rarity_thresholds": [list(row) for row in self.rarity_thresholds],
            "grade_ranges": [[row.down, row.up] for row in self.grade_ranges],
            "equipment": [
                [
                    entry.item_id,
                    entry.grade,
                    entry.rarity,
                    entry.weight,
                    entry.source_path,
                    entry.minimum_level,
                ]
                for key in sorted(self.equipment_by_grade_rarity)
                for entry in self.equipment_by_grade_rarity[key]
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: dict) -> "HellDropConfig":
        """Restore and validate a payload without consulting PVF text."""
        config = cls()
        rows = payload.get("drop_probs")
        thresholds = payload.get("rarity_thresholds")
        ranges = payload.get("grade_ranges")
        equipment = payload.get("equipment")
        if not isinstance(rows, list) or not isinstance(thresholds, list):
            raise ValueError("hell cache table type")
        if not isinstance(ranges, list) or len(ranges) != 200 or not isinstance(equipment, list):
            raise ValueError("hell cache shape")
        config.drop_probs = [
            HellDropProbRow(int(row[0]), int(row[1]), tuple(int(value) for value in row[2:7]))
            for row in rows
            if isinstance(row, list) and len(row) == 7
        ]
        config.rarity_thresholds = [[int(value) for value in row] for row in thresholds]
        config.grade_ranges = [HellGradeWindow(int(row[0]), int(row[1])) for row in ranges]
        for row in equipment:
            if not isinstance(row, list) or len(row) != 6:
                raise ValueError("hell equipment row")
            entry = HellEquipmentEntry(
                item_id=int(row[0]), grade=int(row[1]), rarity=int(row[2]),
                weight=int(row[3]), source_path=str(row[4]), minimum_level=int(row[5]),
            )
            if not (
                entry.item_id > 0
                and 1 <= entry.grade <= 200
                and 0 <= entry.rarity <= 7
                and entry.weight > 0
                and 0 <= entry.minimum_level <= 200
            ):
                raise ValueError("hell equipment value")
            config._add_equipment_entry(entry)
        if not config.drop_probs or not config.rarity_thresholds or config.equipment_count <= 0:
            raise ValueError("hell cache empty")
        if any(len(row.difficulty_rates) != 5 for row in config.drop_probs):
            raise ValueError("hell drop rate shape")
        if any(len(row) != 7 for row in config.rarity_thresholds):
            raise ValueError("hell rarity shape")
        config.source_path = str(payload.get("source_path") or HELL_DROP_PATH)
        config.loaded = True
        return config

    def _parse_drop_probs(self, text: str) -> None:
        count_values = parse_ints(extract_section(text, "drop prob count"))
        if len(count_values) != 1 or int(count_values[0]) <= 0:
            raise RuntimeError("monster_hell [drop prob count] invalid")
        row_count = int(count_values[0])
        vals = parse_ints(extract_section(text, "dungeon difficulty drop prob"))
        if len(vals) != row_count * 7:
            raise RuntimeError("monster_hell [dungeon difficulty drop prob] shape invalid")
        rows: List[HellDropProbRow] = []
        for index in range(0, len(vals) - 6, 7):
            rows.append(
                HellDropProbRow(
                    level_min=int(vals[index]),
                    level_max=int(vals[index + 1]),
                    difficulty_rates=tuple(
                        int(vals[index + 2 + col]) for col in range(5)
                    ),
                )
            )
        if not rows:
            raise RuntimeError("monster_hell [dungeon difficulty drop prob] empty")
        self.drop_probs = rows

    def _parse_rarity_thresholds(self, text: str) -> None:
        vals = parse_ints(extract_section(text, "basis of rarity dicision"))
        if not vals:
            raise RuntimeError("monster_hell [basis of rarity dicision] empty")
        row_count = max(0, int(vals[0]))
        if row_count <= 0 or len(vals) != 1 + row_count * 7:
            raise RuntimeError("monster_hell rarity table shape invalid")
        self.rarity_thresholds = [
            [int(vals[1 + row * 7 + col]) for col in range(7)]
            for row in range(row_count)
        ]

    def _parse_grade_ranges(self, text: str) -> None:
        vals = parse_ints(extract_section(text, "item drop ref table"))
        if not vals or len(vals) % 3:
            raise RuntimeError("monster_hell [item drop ref table] shape invalid")
        ranges = [HellGradeWindow(0, 1) for _ in range(200)]
        for index in range(0, len(vals) - 2, 3):
            level = int(vals[index])
            if 1 <= level <= len(ranges):
                ranges[level - 1] = HellGradeWindow(
                    max(0, int(vals[index + 1])),
                    max(1, int(vals[index + 2])),
                )
        if not any(row.down or row.up != 1 for row in ranges):
            raise RuntimeError("monster_hell [item drop ref table] empty")
        self.grade_ranges = ranges

    def _load_equipment_index(self) -> None:
        self.equipment_by_grade_rarity = {}
        self.equipment_by_level_rarity = {}
        self.equipment_by_item_id = {}
        self.equipment_count = 0
        lst_text = pvf_runtime.read_text(HELL_EQUIPMENT_LST_PATH)
        for item_id, rel in parse_lst_entries(lst_text):
            path = rel.replace('\\', '/').lstrip('/')
            full = path if path.lower().startswith('equipment/') else 'equipment/' + path
            try:
                equ = pvf_runtime.read_text(full)
            except Exception:
                continue
            grade = _extract_int_field(equ, 'grade')
            rarity = _extract_int_field(equ, 'rarity')
            weight = _extract_int_field(equ, 'creation rate')
            minimum_level = _extract_int_field(equ, 'minimum level')
            if grade is None or rarity is None:
                continue
            grade_i = int(grade)
            rarity_i = int(rarity)
            weight_i = int(weight or 0)
            if grade_i <= 0 or grade_i > 200:
                continue
            if rarity_i < 0 or rarity_i > 7:
                continue
            if weight_i <= 0:
                continue
            self._add_equipment_entry(
                HellEquipmentEntry(
                    item_id=int(item_id),
                    grade=grade_i,
                    rarity=rarity_i,
                    weight=weight_i,
                    source_path=full,
                    minimum_level=max(0, int(minimum_level or 0)),
                )
            )
        if self.equipment_count <= 0:
            raise RuntimeError('monster_hell equipment index empty')

    def _add_equipment_entry(self, entry: HellEquipmentEntry) -> None:
        self.equipment_by_grade_rarity.setdefault(
            (int(entry.grade), int(entry.rarity)), []
        ).append(entry)
        if int(entry.minimum_level) > 0:
            self.equipment_by_level_rarity.setdefault(
                (int(entry.minimum_level), int(entry.rarity)), []
            ).append(entry)
        item_id = int(entry.item_id)
        existing = self.equipment_by_item_id.get(item_id)
        # equipment.lst is normally unique; if a stale/fixture source repeats
        # an ID, prefer its Epic row so wish validation remains conservative.
        if existing is None or (
            int(entry.rarity) == HELL_EPIC_RARITY
            and int(existing.rarity) != HELL_EPIC_RARITY
        ):
            self.equipment_by_item_id[item_id] = entry
        self.equipment_count += 1

    def _rebuild_item_index(self) -> None:
        """Rebuild the derived ID index for focused fixtures and old caches."""
        index: Dict[int, HellEquipmentEntry] = {}
        for entries in self.equipment_by_grade_rarity.values():
            for entry in entries:
                index.setdefault(int(entry.item_id), entry)
        self.equipment_by_item_id = index

    def equipment_for_item_id(self, item_id: int) -> Optional[HellEquipmentEntry]:
        """Return one prewarmed equipment entry for an authoritative item ID."""
        iid = int(item_id or 0)
        if iid <= 0:
            return None
        if iid not in self.equipment_by_item_id:
            self._rebuild_item_index()
        return self.equipment_by_item_id.get(iid)

    def is_epic_item(self, item_id: int) -> bool:
        entry = self.equipment_for_item_id(item_id)
        return entry is not None and int(entry.rarity) == HELL_EPIC_RARITY

    def epic_item_ids(self) -> Tuple[int, ...]:
        """Return the sorted, PVF-backed Epic equipment identity pool."""
        if not self.equipment_by_item_id:
            self._rebuild_item_index()
        return tuple(
            sorted(
                int(item_id)
                for item_id, entry in self.equipment_by_item_id.items()
                if int(entry.rarity) == HELL_EPIC_RARITY
            )
        )

    def retain_registered_equipment(
        self,
        is_registered_item: Callable[[int], bool],
    ) -> int:
        """Remove stale-cache entries that are absent from equipment.lst.

        Fresh indexes are already built by iterating equipment.lst.  The extra
        pass keeps a restored persistent cache under the same identity rule
        before any Hell probability roll can select a candidate.
        """
        entries = [
            entry
            for group in self.equipment_by_grade_rarity.values()
            for entry in group
        ]
        self.equipment_by_grade_rarity = {}
        self.equipment_by_level_rarity = {}
        self.equipment_by_item_id = {}
        self.equipment_count = 0
        rejected = 0
        for entry in entries:
            try:
                registered = bool(is_registered_item(int(entry.item_id)))
            except Exception:
                registered = False
            if not registered:
                rejected += 1
                continue
            self._add_equipment_entry(entry)
        self.registration_rejected_count = rejected
        if self.equipment_count <= 0:
            raise RuntimeError("monster_hell has no equipment.lst-registered candidates")
        return rejected

    def find_drop_prob(self, item_basis_level: int) -> HellDropProbRow:
        level = max(1, int(item_basis_level))
        for row in self.drop_probs:
            if row.level_min <= level <= row.level_max:
                return row
        return HellDropProbRow(level, level, (0, 0, 0, 0, 0))

    def drop_rate(self, item_basis_level: int, dungeon_difficulty: int) -> int:
        row = self.find_drop_prob(item_basis_level)
        index = max(0, min(int(dungeon_difficulty), len(row.difficulty_rates) - 1))
        return max(0, int(row.difficulty_rates[index]))

    def rarity_weights(self, hell_party_mode: int) -> Tuple[int, ...]:
        """Return the exact cumulative PVF rarity distribution for one mode."""
        if not self.rarity_thresholds:
            return (0, 0, 0, 0, 0, 0, 0)
        row_index = max(
            0,
            min(int(hell_party_mode or 1) - 1, len(self.rarity_thresholds) - 1),
        )
        thresholds = self.rarity_thresholds[row_index]
        weights = [0] * 7
        cursor = 0
        for rarity, threshold in enumerate(thresholds[: len(weights)]):
            upper = max(0, min(HELL_RARITY_ROLL_DENOMINATOR, int(threshold)))
            upper = max(cursor, upper)
            weights[rarity] = upper - cursor
            cursor = upper
        return tuple(int(weight) for weight in weights)

    def roll_rarity(self, lcg: DnfLcg, hell_party_mode: int) -> int:
        weights = self.rarity_weights(hell_party_mode)
        roll = int(lcg.next(HELL_RARITY_ROLL_DENOMINATOR))
        cursor = 0
        for rarity, weight in enumerate(weights):
            cursor += max(0, int(weight))
            if roll < cursor:
                return int(rarity)
        return -1

    def grade_window(self, item_basis_level: int) -> HellGradeWindow:
        level = max(1, min(200, int(item_basis_level)))
        return self.grade_ranges[level - 1]

    def choose_equipment(
        self,
        lcg: DnfLcg,
        *,
        item_basis_level: int,
        rarity: Optional[int],
        preferred_item_ids: Sequence[int] = (),
        preferred_weight: int = 1,
        include_preferred_outside_grade: bool = False,
        excluded_item_ids: Sequence[int] = (),
        grade_levels: Optional[Sequence[int]] = None,
        equipment_levels: Optional[Sequence[int]] = None,
    ) -> Optional[HellEquipmentEntry]:
        target_rarity = None if rarity is None else int(rarity)
        excluded = {
            int(item_id)
            for item_id in excluded_item_ids
            if int(item_id) > 0
        }
        candidates: List[HellEquipmentEntry] = []
        if equipment_levels is not None:
            levels = sorted(
                {
                    int(level)
                    for level in equipment_levels
                    if 1 <= int(level) <= 200
                }
            )
            for level in levels:
                if target_rarity is None:
                    for candidate_rarity in range(8):
                        candidates.extend(
                            self.equipment_by_level_rarity.get(
                                (int(level), candidate_rarity), []
                            )
                        )
                else:
                    candidates.extend(
                        self.equipment_by_level_rarity.get(
                            (int(level), target_rarity), []
                        )
                    )
        else:
            if grade_levels is None:
                window = self.grade_window(item_basis_level)
                lower = max(1, int(item_basis_level) - int(window.down))
                upper = min(200, int(item_basis_level) + int(window.up) - 1)
                grades = range(lower, upper + 1)
            else:
                grades = sorted(
                    {
                        int(grade)
                        for grade in grade_levels
                        if 1 <= int(grade) <= 200
                    }
                )
            for grade in grades:
                if target_rarity is None:
                    for candidate_rarity in range(8):
                        candidates.extend(
                            self.equipment_by_grade_rarity.get(
                                (int(grade), candidate_rarity), []
                            )
                        )
                else:
                    candidates.extend(
                        self.equipment_by_grade_rarity.get(
                            (int(grade), target_rarity), []
                        )
                    )
        preferred = {int(item_id) for item_id in preferred_item_ids if int(item_id) > 0}
        if (
            equipment_levels is None
            and grade_levels is None
            and bool(include_preferred_outside_grade)
            and target_rarity == 4
            and preferred
        ):
            known_ids = {int(entry.item_id) for entry in candidates}
            for (grade, candidate_rarity), entries in self.equipment_by_grade_rarity.items():
                if int(candidate_rarity) != target_rarity or int(grade) == 0:
                    continue
                for entry in entries:
                    if (
                        int(entry.item_id) in preferred
                        and int(entry.item_id) not in excluded
                        and int(entry.item_id) not in known_ids
                    ):
                        candidates.append(entry)
                        known_ids.add(int(entry.item_id))
        if excluded:
            candidates = [
                entry for entry in candidates if int(entry.item_id) not in excluded
            ]
        if not candidates:
            return None
        multiplier = max(1, int(preferred_weight))

        def _weight(entry: HellEquipmentEntry) -> int:
            value = max(0, int(entry.weight))
            if int(entry.item_id) in preferred:
                value *= multiplier
            return value

        total = sum(_weight(entry) for entry in candidates)
        if total <= 0:
            return None
        roll = int(lcg.next(total))
        cursor = 0
        for entry in candidates:
            cursor += _weight(entry)
            if roll < cursor:
                return entry
        return candidates[-1]


def _extract_int_field(text: str, key: str) -> Optional[int]:
    body = extract_section(text, key)
    if not body:
        return None
    match = re.search(_INT_FIELD_RE, body)
    if match:
        return int(match.group(1))
    match = re.search(r'(-?\d+)', body)
    if match:
        return int(match.group(1))
    return None


_LOCK = threading.RLock()
_CONFIG: Optional[HellDropConfig] = None


def get_hell_drop_config() -> HellDropConfig:
    with _LOCK:
        if _CONFIG is None or not _CONFIG.loaded:
            raise RuntimeError('HellDropConfig not warmed; call warmup first')
        return _CONFIG


def _warmup_report(config: HellDropConfig) -> dict:
    return {
        'ok': 1,
        'path': config.source_path,
        'drop_prob_rows': len(config.drop_probs),
        'rarity_rows': len(config.rarity_thresholds),
        'grade_rows': len(config.grade_ranges),
        'equipment_count': int(config.equipment_count),
    }


def warmup_hell_drop_config(*, force: bool = False) -> dict:
    global _CONFIG
    with _LOCK:
        existing = _CONFIG
    if not force and existing is not None and existing.loaded:
        return _warmup_report(existing)
    config = HellDropConfig()
    config.load_from_text(pvf_runtime.read_text(HELL_DROP_PATH))
    with _LOCK:
        _CONFIG = config
    return _warmup_report(config)


def install_hell_drop_config(config: HellDropConfig) -> None:
    """Install a validated cache-restored configuration for boot warmup."""
    if not isinstance(config, HellDropConfig) or not config.loaded:
        raise ValueError("cannot install unloaded HellDropConfig")
    global _CONFIG
    with _LOCK:
        _CONFIG = config


def clear_hell_drop_config() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None
