# -*- coding: utf-8 -*-
"""``etc/itemdropinfo_common.etc`` — [gold drop ref table]。

形态（用户确认 + 书签 15680）::

  col1 = 怪物等级
  col2 = 金币掉落**上限**（最高）
  col3 = 金币掉落**下限**（最少）

本模块只生成 ``uniform_int[min, max]`` 基础值。调用方再乘当前掉落来源
PVF 主表的金币难度倍率，避免怪物表和建筑表共用服务端常量。

是否出金仍由 monseter ``[drop prob].gold`` 决定，本表只定数量。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import extract_section, parse_ints
from pvf import pvf_runtime

COMMON_PATH = "etc/itemdropinfo_common.etc"


@dataclass(frozen=True)
class GoldReference:
    level: int
    gold_max: int
    gold_min: int

    @property
    def base(self) -> int:
        """兼容旧字段名：取上下限中点（仅调试用）。"""
        return (int(self.gold_min) + int(self.gold_max)) // 2


class GoldTable:
    def __init__(self) -> None:
        self.by_level: Dict[int, GoldReference] = {}
        self.loaded = False

    def load_from_text(self, text: str) -> None:
        section = extract_section(text, "gold drop ref table")
        vals = parse_ints(section)
        if len(vals) < 3 or len(vals) % 3 != 0:
            raise RuntimeError("common [gold drop ref table] shape invalid")
        by_level: Dict[int, GoldReference] = {}
        for i in range(0, len(vals), 3):
            level = int(vals[i])
            gold_max = int(vals[i + 1])  # 上限
            gold_min = int(vals[i + 2])  # 下限
            if level <= 0:
                raise RuntimeError(
                    "invalid gold row level=%s max=%s min=%s" % (level, gold_max, gold_min)
                )
            # 容错：若表里写反，按数值交换，保证 min<=max
            lo = min(gold_min, gold_max)
            hi = max(gold_min, gold_max)
            if hi < 0:
                raise RuntimeError(
                    "invalid gold row level=%s max=%s min=%s" % (level, gold_max, gold_min)
                )
            if hi == 0 and lo == 0:
                # 全 0 行无意义，跳过（不应出现）
                continue
            if lo < 0:
                lo = 0
            by_level[level] = GoldReference(level=level, gold_max=hi, gold_min=lo)
        if not by_level:
            raise RuntimeError("gold table empty")
        self.by_level = by_level
        self.loaded = True

    def get(self, monster_level: int) -> GoldReference:
        lv = max(1, int(monster_level))
        if lv in self.by_level:
            return self.by_level[lv]
        # 最近不大于 lv 的档；否则最小档
        best = None
        for k, v in self.by_level.items():
            if k <= lv and (best is None or k > best.level):
                best = v
        if best is not None:
            return best
        return min(self.by_level.values(), key=lambda r: r.level)

    def roll_amount(self, lcg: DnfLcg, monster_level: int, difficulty: int) -> int:
        """在 [gold_min, gold_max] 均匀取整；难度倍率由调用方读取 PVF。"""
        ref = self.get(monster_level)
        lo = int(ref.gold_min)
        hi = int(ref.gold_max)
        if hi < lo:
            lo, hi = hi, lo
        if hi < 1:
            return 1
        if lo < 1:
            lo = 1
        span = hi - lo + 1
        raw = lo + int(lcg.next(span))
        return max(1, int(raw))


_LOCK = threading.RLock()
_TABLE: Optional[GoldTable] = None


def get_gold_table() -> GoldTable:
    global _TABLE
    with _LOCK:
        if _TABLE is None or not _TABLE.loaded:
            raise RuntimeError("GoldTable not warmed")
        return _TABLE


def warmup_gold_table() -> dict:
    global _TABLE
    text = pvf_runtime.read_text(COMMON_PATH)
    table = GoldTable()
    table.load_from_text(text)
    with _LOCK:
        _TABLE = table
    return {
        "ok": 1,
        "path": COMMON_PATH,
        "levels": len(table.by_level),
        "shape": "level,gold_max,gold_min",
    }


def clear_gold_table() -> None:
    global _TABLE
    with _LOCK:
        _TABLE = None
