# -*- coding: utf-8 -*-
"""与 s4a12 / 90qiyuan 同构的房间掉落 LCG。

seed = seed*1103515245+12345 三轮混合；Next(max) = Next() % max。
"""
from __future__ import annotations


class DnfLcg:
    __slots__ = ("_seed",)

    def __init__(self, seed: int = 1) -> None:
        self._seed = int(seed) & 0xFFFFFFFF

    @property
    def seed(self) -> int:
        return int(self._seed) & 0xFFFFFFFF

    def next_u32(self) -> int:
        v2 = (self._seed * 1103515245 + 12345) & 0xFFFFFFFF
        v3 = (v2 * 1103515245 + 12345) & 0xFFFFFFFF
        v4 = (v3 * 1103515245 + 12345) & 0xFFFFFFFF
        self._seed = v4
        hi2 = (v2 >> 16) & 0x7FF
        hi3 = (v3 >> 16) & 0x3FF
        hi4 = (v4 >> 16) & 0x3FF
        return (((hi2 << 10) ^ hi3) << 10 | hi4) & 0xFFFFFFFF

    def next(self, max_value: int = 0) -> int:
        value = self.next_u32()
        if int(max_value) <= 0:
            return int(value)
        return int(value % int(max_value))


def roll_weighted_occurrences(
    lcg: DnfLcg,
    weight: int,
    *,
    denominator: int,
) -> int:
    """Return guaranteed rolls plus one residual weighted roll.

    PVF fixed-drop weights are not limited to one denominator.  For example,
    ``25000`` with a ``10000`` denominator means two guaranteed occurrences
    plus one more at 50 percent.  A zero residual deliberately consumes no
    random value, because no probabilistic decision remains.
    """
    base = max(1, int(denominator))
    total = max(0, int(weight))
    occurrences = total // base
    remainder = total % base
    if remainder > 0 and int(lcg.next(base)) < remainder:
        occurrences += 1
    return int(occurrences)
