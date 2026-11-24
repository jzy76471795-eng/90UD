# -*- coding: utf-8 -*-
"""掉落领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class DropInfo:
    """场景掉落一件（金币 template_id=0）。

    Stage3 IDA ``sub_1647080``：
      - ``object_key``：DIE 行前缀 u32 / GET_ITEM 回声键
      - ``scene_slot``：108B 道具条内 +0 槽位字段
    单机默认两者相同（从 1 递增）。
    """

    scene_slot: int
    template_id: int
    stack_count: int = 1
    endurance: int = 0
    upgrade_level: int = 0
    # monster/object gold and item channels, fixed pools, or tower policy.
    source: str = ""
    object_key: int = 0  # 0 → 使用 scene_slot
    # Optional quest owner for drops whose progress must advance on pickup.
    quest_id: int = 0

    @property
    def is_gold(self) -> bool:
        return int(self.template_id) == 0

    @property
    def wire_object_key(self) -> int:
        key = int(self.object_key or 0)
        if key > 0:
            return key & 0xFFFFFFFF
        return int(self.scene_slot) & 0xFFFF


@dataclass
class MonsterDropRequest:
    monster_level: int
    monster_type: int
    monster_code: int
    difficulty: int
    dungeon_basis_level: int
    dungeon_id: int = -1
    owner_actor_id: int = 0
    independent_drop_rate_bonus_percent: int = 0


@dataclass
class MonsterDropResult:
    gold_amount: int = 0
    drops: List[DropInfo] = field(default_factory=list)
    seed_before: int = 0
    seed_after: int = 0
    note: str = ""


@dataclass
class PreloadedDropBundle:
    """One already-rolled drop result reserved for a visible dungeon actor."""

    result: MonsterDropResult
    scope: str = "actor"
    consumed: bool = False
