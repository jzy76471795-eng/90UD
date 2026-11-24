# -*- coding: utf-8 -*-
"""PVF ``etc/itemdropinfo_object.etc`` runtime index.

The object table has the same four primary channels as the monster table, but
its probability and grade windows are independent.  In the current translated
PVF it yields a small amount of gold and a very small equipment chance; type1
and type3 remain implemented so another PVF can enable material channels
without a server code change.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import extract_section, parse_floats, parse_ints
from pvf import pvf_runtime

OBJECT_DROP_PATH = "etc/itemdropinfo_object.etc"
DROP_DENOMINATOR = 10000
_CATEGORY_COUNT = 5
_DIFFICULTY_COUNT = 5


@dataclass(frozen=True)
class ObjectDropProbRow:
    level_min: int
    level_max: int
    gold_rate: int
    type1_rate: int
    type2_rate: int
    type3_rate: int
    extra_rate: int = 0


@dataclass(frozen=True)
class ObjectGradeWindow:
    down: int
    up: int


class ObjectDropConfig:
    def __init__(self) -> None:
        self.drop_probs: List[ObjectDropProbRow] = []
        self.grade_ranges: List[ObjectGradeWindow] = [
            ObjectGradeWindow(0, 1) for _ in range(200)
        ]
        self.rarity_thresholds: List[List[int]] = [[0] * 7 for _ in range(4)]
        self.difficulty_bonus: List[List[float]] = [
            [1.0] * _DIFFICULTY_COUNT for _ in range(_CATEGORY_COUNT)
        ]
        self.loaded = False
        self.source_path = OBJECT_DROP_PATH

    def load_from_text(self, text: str) -> None:
        self._parse_drop_prob(text)
        self._parse_grade_ranges(text)
        self._parse_rarity(text)
        self._parse_difficulty_bonus(text)
        self.loaded = True

    def _parse_drop_prob(self, text: str) -> None:
        count_values = parse_ints(extract_section(text, "drop prob count"))
        if len(count_values) != 1 or int(count_values[0]) <= 0:
            raise RuntimeError("object [drop prob count] invalid")
        row_count = int(count_values[0])
        vals = parse_ints(extract_section(text, "drop prob"))
        if len(vals) != row_count * 7:
            raise RuntimeError("object [drop prob] shape invalid")
        rows: List[ObjectDropProbRow] = []
        for offset in range(0, len(vals) - 6, 7):
            rows.append(
                ObjectDropProbRow(
                    level_min=int(vals[offset]),
                    level_max=int(vals[offset + 1]),
                    gold_rate=int(vals[offset + 2]),
                    type1_rate=int(vals[offset + 3]),
                    type2_rate=int(vals[offset + 4]),
                    type3_rate=int(vals[offset + 5]),
                    extra_rate=int(vals[offset + 6]),
                )
            )
        if not rows:
            raise RuntimeError("object [drop prob] empty")
        self.drop_probs = rows

    def _parse_grade_ranges(self, text: str) -> None:
        vals = parse_ints(extract_section(text, "item drop ref table"))
        if not vals or len(vals) % 3:
            raise RuntimeError("object [item drop ref table] shape invalid")
        ranges = [ObjectGradeWindow(0, 1) for _ in range(200)]
        for offset in range(0, len(vals) - 2, 3):
            level = int(vals[offset])
            if 1 <= level <= 200:
                ranges[level - 1] = ObjectGradeWindow(
                    int(vals[offset + 1]), int(vals[offset + 2])
                )
        self.grade_ranges = ranges

    def _parse_rarity(self, text: str) -> None:
        vals = parse_ints(extract_section(text, "basis of rarity dicision"))
        if len(vals) != 28:
            raise RuntimeError("object [basis of rarity dicision] shape invalid")
        self.rarity_thresholds = [
            [int(vals[row * 7 + col]) for col in range(7)]
            for row in range(4)
        ]

    def _parse_difficulty_bonus(self, text: str) -> None:
        vals = parse_floats(
            extract_section(text, "dungeon difficulty drop bonusrate")
        )
        if not vals:
            return
        if len(vals) != _CATEGORY_COUNT * _DIFFICULTY_COUNT:
            raise RuntimeError(
                "object [dungeon difficulty drop bonusrate] shape invalid"
            )
        self.difficulty_bonus = [
            [
                float(vals[category * _DIFFICULTY_COUNT + difficulty])
                for difficulty in range(_DIFFICULTY_COUNT)
            ]
            for category in range(_CATEGORY_COUNT)
        ]

    def find_drop_prob(self, object_level: int) -> ObjectDropProbRow:
        level = max(1, int(object_level))
        for row in self.drop_probs:
            if row.level_min <= level <= row.level_max:
                return row
        return ObjectDropProbRow(1, 200, 0, 0, 0, 0, 0)

    def difficulty_multiplier(self, category: int, difficulty: int) -> float:
        if category < 0 or category >= len(self.difficulty_bonus):
            return 1.0
        row = self.difficulty_bonus[category]
        index = max(0, min(int(difficulty), len(row) - 1))
        return max(0.0, float(row[index]))

    def get_all_drop_rates(
        self, object_level: int, difficulty: int
    ) -> Tuple[int, int, int, int]:
        row = self.find_drop_prob(object_level)
        difficulty_index = max(0, min(int(difficulty), _DIFFICULTY_COUNT - 1))
        base_rates = (
            row.gold_rate,
            row.type1_rate,
            row.type2_rate,
            row.type3_rate,
        )
        rates: list[int] = []
        for category, base_rate in enumerate(base_rates):
            bonus = self.difficulty_multiplier(category, difficulty_index)
            value = int(float(base_rate) * max(0.0, bonus))
            rates.append(max(0, min(value, DROP_DENOMINATOR)))
        return rates[0], rates[1], rates[2], rates[3]

    def roll_rarity(self, lcg: DnfLcg, row: int = 0) -> int:
        thresholds = self.rarity_thresholds[max(0, min(int(row), 3))]
        roll = int(lcg.next(1000000)) + 1
        for rarity, threshold in enumerate(thresholds):
            if roll <= int(threshold):
                return int(rarity)
        return 0

    def grade_window(self, object_level: int) -> ObjectGradeWindow:
        level = max(1, min(200, int(object_level)))
        return self.grade_ranges[level - 1]


_LOCK = threading.RLock()
_CONFIG: Optional[ObjectDropConfig] = None


def get_object_drop_config() -> ObjectDropConfig:
    global _CONFIG
    with _LOCK:
        if _CONFIG is None or not _CONFIG.loaded:
            raise RuntimeError("ObjectDropConfig not warmed; call warmup first")
        return _CONFIG


def install_object_drop_config(config: ObjectDropConfig) -> None:
    if config is None or not bool(config.loaded):
        raise ValueError("loaded ObjectDropConfig required")
    global _CONFIG
    with _LOCK:
        _CONFIG = config


def warmup_object_drop_config() -> dict:
    text = pvf_runtime.read_text(OBJECT_DROP_PATH)
    config = ObjectDropConfig()
    config.load_from_text(text)
    install_object_drop_config(config)
    return {
        "ok": 1,
        "path": OBJECT_DROP_PATH,
        "drop_prob_rows": len(config.drop_probs),
        "grade_rows": len(config.grade_ranges),
        "rarity_rows": len(config.rarity_thresholds),
        "difficulty_rows": len(config.difficulty_bonus),
    }


def clear_object_drop_config() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None
