# -*- coding: utf-8 -*-
"""``etc/itemdropinfo_monseter.etc`` — 主表出率 / 稀有度 / grade 窗 / 怪类型加成。

对齐 90qiyuan MonsterRules + s4a12 MonsterDropConfig。
组队 party bonus **不实现**（单机恒 1.0）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import extract_section, parse_floats, parse_ints
from pvf import pvf_runtime

MONSETER_PATH = "etc/itemdropinfo_monseter.etc"
DROP_DENOMINATOR = 10000

_CATEGORY_COUNT = 5
_DIFFICULTY_COUNT = 5


@dataclass(frozen=True)
class DropProbRow:
    level_min: int
    level_max: int
    gold_rate: int
    type1_rate: int
    type2_rate: int
    type3_rate: int
    extra_rate: int = 0  # 第 5 列存档；主路径暂不 roll


@dataclass(frozen=True)
class GradeWindow:
    down: int
    up: int


class MonsterDropConfig:
    def __init__(self) -> None:
        self.drop_probs: List[DropProbRow] = []
        self.grade_ranges: List[GradeWindow] = [GradeWindow(0, 1) for _ in range(200)]
        self.rarity_thresholds: List[List[int]] = [[0] * 7 for _ in range(4)]
        self.monster_type_bonus: List[List[float]] = [[1.0] * 4 for _ in range(5)]
        self.difficulty_bonus: List[List[float]] = [
            [1.0] * _DIFFICULTY_COUNT for _ in range(_CATEGORY_COUNT)
        ]
        self.loaded = False
        self.source_path = MONSETER_PATH

    def load_from_text(self, text: str) -> None:
        self._parse_drop_prob(text)
        self._parse_grade_ranges(text)
        self._parse_rarity(text)
        self._parse_monster_type_bonus(text)
        self._parse_difficulty_bonus(text)
        self.loaded = True

    def _parse_drop_prob(self, text: str) -> None:
        count_values = parse_ints(extract_section(text, "drop prob count"))
        if len(count_values) != 1 or int(count_values[0]) <= 0:
            raise RuntimeError("monseter [drop prob count] invalid")
        row_count = int(count_values[0])
        section = extract_section(text, "drop prob")
        vals = parse_ints(section)
        if len(vals) != row_count * 7:
            raise RuntimeError("monseter [drop prob] shape invalid")
        rows: List[DropProbRow] = []
        for i in range(0, len(vals) - 6, 7):
            rows.append(
                DropProbRow(
                    level_min=int(vals[i]),
                    level_max=int(vals[i + 1]),
                    gold_rate=int(vals[i + 2]),
                    type1_rate=int(vals[i + 3]),
                    type2_rate=int(vals[i + 4]),
                    type3_rate=int(vals[i + 5]),
                    extra_rate=int(vals[i + 6]),
                )
            )
        if not rows:
            raise RuntimeError("monseter [drop prob] empty")
        self.drop_probs = rows

    def _parse_grade_ranges(self, text: str) -> None:
        section = extract_section(text, "item drop ref table")
        vals = parse_ints(section)
        if not vals or len(vals) % 3:
            raise RuntimeError("monseter [item drop ref table] shape invalid")
        ranges = [GradeWindow(0, 1) for _ in range(200)]
        for i in range(0, len(vals) - 2, 3):
            level = int(vals[i])
            if 1 <= level <= 200:
                ranges[level - 1] = GradeWindow(int(vals[i + 1]), int(vals[i + 2]))
        self.grade_ranges = ranges

    def _parse_rarity(self, text: str) -> None:
        section = extract_section(text, "basis of rarity dicision")
        vals = parse_ints(section)
        if len(vals) != 28:
            raise RuntimeError("monseter [basis of rarity dicision] shape invalid")
        table: List[List[int]] = []
        for row in range(4):
            table.append([int(vals[row * 7 + col]) for col in range(7)])
        self.rarity_thresholds = table

    def _parse_monster_type_bonus(self, text: str) -> None:
        section = extract_section(text, "monster type drop bonusrate")
        vals = parse_floats(section)
        if len(vals) != 20:
            raise RuntimeError("monseter [monster type drop bonusrate] shape invalid")
        mat: List[List[float]] = []
        for cat in range(5):
            mat.append([float(vals[cat * 4 + mt]) for mt in range(4)])
        self.monster_type_bonus = mat

    def _parse_difficulty_bonus(self, text: str) -> None:
        """Read the PVF 5 x 5 category/difficulty multiplier matrix.

        The fifth category is retained for the PVF layout but is not currently
        emitted by the four-channel monster drop generator.
        """
        section = extract_section(text, "dungeon difficulty drop bonusrate")
        if not section.strip():
            return
        vals = parse_floats(section)
        if len(vals) != _CATEGORY_COUNT * _DIFFICULTY_COUNT:
            raise RuntimeError(
                "monseter [dungeon difficulty drop bonusrate] shape invalid"
            )
        self.difficulty_bonus = [
            [
                max(0.0, float(vals[category * _DIFFICULTY_COUNT + difficulty]))
                for difficulty in range(_DIFFICULTY_COUNT)
            ]
            for category in range(_CATEGORY_COUNT)
        ]

    def find_drop_prob(self, monster_level: int) -> DropProbRow:
        lv = max(1, int(monster_level))
        for row in self.drop_probs:
            if row.level_min <= lv <= row.level_max:
                return row
        return DropProbRow(lv, lv, 0, 0, 0, 0, 0)

    def type_bonus(self, category: int, monster_type: int) -> float:
        if category < 0 or category >= len(self.monster_type_bonus):
            return 1.0
        row = self.monster_type_bonus[category]
        mt = max(0, min(int(monster_type), len(row) - 1))
        val = float(row[mt])
        if val <= 0:
            return 0.0
        return val

    def difficulty_multiplier(self, category: int, difficulty: int) -> float:
        if category < 0 or category >= len(self.difficulty_bonus):
            return 1.0
        row = self.difficulty_bonus[category]
        index = max(0, min(int(difficulty), len(row) - 1))
        return max(0.0, float(row[index]))

    def get_all_drop_rates(
        self, monster_level: int, monster_type: int, difficulty: int
    ) -> Tuple[int, int, int, int]:
        """返回四类掉率，已乘 PVF 怪物类型和逐类别难度倍率，封顶 10000。"""
        prob = self.find_drop_prob(monster_level)
        diff_i = max(0, min(int(difficulty), _DIFFICULTY_COUNT - 1))

        def _rate(base: int, category: int) -> int:
            if base <= 0:
                return 0
            tb = self.type_bonus(category, monster_type)
            if tb <= 0:
                return 0
            difficulty_bonus = self.difficulty_multiplier(category, diff_i)
            value = int(float(base) * tb * difficulty_bonus)
            if value <= 0:
                return 0
            return min(value, DROP_DENOMINATOR)

        return (
            _rate(prob.gold_rate, 0),
            _rate(prob.type1_rate, 1),
            _rate(prob.type2_rate, 2),
            _rate(prob.type3_rate, 3),
        )

    def roll_rarity(self, lcg: DnfLcg, row: int = 0) -> int:
        """累计阈值；roll∈[1,1e6] 落入首个 roll<=阈值 的档。默认用 row0。"""
        thresholds = self.rarity_thresholds[max(0, min(int(row), 3))]
        roll = int(lcg.next(1000000)) + 1
        for rarity, thr in enumerate(thresholds):
            if roll <= int(thr):
                return int(rarity)
        return 0

    def grade_window(self, monster_level: int) -> GradeWindow:
        lv = max(1, min(200, int(monster_level)))
        return self.grade_ranges[lv - 1]


_LOCK = threading.RLock()
_CONFIG: Optional[MonsterDropConfig] = None


def get_monster_drop_config() -> MonsterDropConfig:
    global _CONFIG
    with _LOCK:
        if _CONFIG is None or not _CONFIG.loaded:
            raise RuntimeError("MonsterDropConfig not warmed; call warmup first")
        return _CONFIG


def warmup_monster_drop_config() -> dict:
    global _CONFIG
    text = pvf_runtime.read_text(MONSETER_PATH)
    cfg = MonsterDropConfig()
    cfg.load_from_text(text)
    with _LOCK:
        _CONFIG = cfg
    return {
        "ok": 1,
        "path": MONSETER_PATH,
        "drop_prob_rows": len(cfg.drop_probs),
        "grade_rows": 200,
        "rarity_rows": 4,
    }


def clear_monster_drop_config() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None
