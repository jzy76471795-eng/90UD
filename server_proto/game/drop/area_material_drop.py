# -*- coding: utf-8 -*-
"""Region material drops shared by monsters and destructible objects."""
from __future__ import annotations

from dataclasses import dataclass

from game.drop.dnf_lcg import DnfLcg


AREA_MATERIAL_DROP_DENOMINATOR = 10000
AREA_MATERIAL_DROP_RATE = 2500


@dataclass(frozen=True)
class AreaMaterialRule:
    first_dungeon_id: int
    last_dungeon_id: int
    item_id: int

    def contains(self, dungeon_id: int) -> bool:
        value = int(dungeon_id)
        return int(self.first_dungeon_id) <= value <= int(self.last_dungeon_id)


AREA_MATERIAL_RULES = (
    AreaMaterialRule(144, 149, 10088620),
    AreaMaterialRule(150, 155, 10088624),
    AreaMaterialRule(156, 160, 10088622),
    AreaMaterialRule(161, 165, 10088626),
    AreaMaterialRule(166, 172, 10088628),
)


def resolve_area_material_rule(dungeon_id: int) -> AreaMaterialRule | None:
    did = int(dungeon_id)
    for rule in AREA_MATERIAL_RULES:
        if rule.contains(did):
            return rule
    return None


def area_material_item_id(dungeon_id: int) -> int:
    rule = resolve_area_material_rule(dungeon_id)
    return int(rule.item_id) if rule is not None else 0


def roll_area_material_item(
    dungeon_id: int,
    lcg: DnfLcg,
    *,
    drop_rate: int = AREA_MATERIAL_DROP_RATE,
) -> int:
    """Return the region material item id on an independent successful roll."""
    item_id = area_material_item_id(dungeon_id)
    if item_id <= 0:
        return 0
    rate = max(0, min(AREA_MATERIAL_DROP_DENOMINATOR, int(drop_rate)))
    if rate <= 0:
        return 0
    if int(lcg.next(AREA_MATERIAL_DROP_DENOMINATOR)) >= rate:
        return 0
    return item_id
