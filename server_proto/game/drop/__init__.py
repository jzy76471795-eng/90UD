# -*- coding: utf-8 -*-
"""地下城怪物材料/道具掉落（非翻牌、非组队）。

对外入口：
- ``warmup_drop_index`` / ``drop_warmup.warmup_drop_index``
- ``drop_service.generate_and_register_monster_drops``
- ``drop_service.generate_and_register_passive_object_drops``
"""
from __future__ import annotations

from game.drop.drop_service import (
    generate_and_register_monster_drops,
    generate_and_register_passive_object_drops,
)
from game.drop.drop_warmup import warmup_drop_index

__all__ = [
    "generate_and_register_monster_drops",
    "generate_and_register_passive_object_drops",
    "warmup_drop_index",
]
