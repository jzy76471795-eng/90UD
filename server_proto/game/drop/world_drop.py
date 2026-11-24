# -*- coding: utf-8 -*-
"""``etc/(r)worlddrop.etc`` — 全局等级掉落（极低概率 1 件）。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import extract_tagged_block, parse_ints
from pvf import pvf_runtime

WORLD_PATHS = (
    "etc/(r)worlddrop.etc",
    "etc/(f)worlddrop.etc",
)
DROP_DENOM = 100000
BASE_DROP_RATE = 100
MAX_LEVEL = 199


@dataclass
class WorldLevelPool:
    total_weight: int = 0
    items: List[Tuple[int, int]] = field(default_factory=list)  # (id, cum_weight)


class WorldDropIndex:
    def __init__(self) -> None:
        self.levels: Dict[int, WorldLevelPool] = {}
        self.loaded = False
        self.path = ""
        self.level_count = 0
        self.item_count = 0
        self.rejected_item_count = 0

    def load_from_text(
        self,
        text: str,
        path: str = "",
        *,
        is_registered_item: Optional[Callable[[int], bool]] = None,
    ) -> None:
        candidate_is_registered = is_registered_item or (lambda _item_id: True)

        def is_registered(item_id: int) -> bool:
            try:
                return bool(candidate_is_registered(int(item_id)))
            except Exception:
                return False

        block = extract_tagged_block(text, "world drop")
        if not block:
            # 有的文件只有 [world drop] 无闭合
            from game.drop.pvf_section import extract_section

            block = extract_section(text, "world drop")
        vals = parse_ints(block)
        levels: Dict[int, WorldLevelPool] = {}
        idx = 0
        level_count = 0
        item_count = 0
        rejected_item_count = 0
        while idx + 1 < len(vals):
            level = int(vals[idx])
            idx += 1
            # pad
            idx += 1
            total = 0
            items: List[Tuple[int, int]] = []
            while idx < len(vals):
                item_id = int(vals[idx])
                idx += 1
                if item_id == -1:
                    break
                if idx >= len(vals):
                    break
                weight = int(vals[idx])
                idx += 1
                if item_id <= 0 or weight <= 0:
                    continue
                if not is_registered(item_id):
                    rejected_item_count += 1
                    continue
                total += weight
                items.append((item_id, total))
                item_count += 1
            if 1 <= level <= 200 and total > 0 and items:
                levels[level] = WorldLevelPool(total_weight=total, items=items)
                level_count += 1
        self.levels = levels
        self.level_count = level_count
        self.item_count = item_count
        self.rejected_item_count = rejected_item_count
        self.path = path
        self.loaded = True
        if level_count <= 0:
            raise RuntimeError("worlddrop has no weighted levels")

    def generate(
        self,
        monster_level: int,
        lcg: DnfLcg,
        allow_item: Callable[[int], bool],
    ) -> List[int]:
        lv = int(monster_level)
        if lv < 1 or lv > MAX_LEVEL:
            return []
        pool = self.levels.get(lv)
        if pool is None or pool.total_weight <= 0 or not pool.items:
            return []
        threshold = int(pool.total_weight) * BASE_DROP_RATE // 100
        if threshold <= 0 or threshold <= lcg.next(DROP_DENOM):
            return []
        roll = lcg.next(pool.total_weight)
        chosen = pool.items[-1][0]
        for iid, cum in pool.items:
            if roll < cum:
                chosen = iid
                break
        if allow_item(int(chosen)):
            return [int(chosen)]
        return []


_LOCK = threading.RLock()
_INDEX: Optional[WorldDropIndex] = None


def get_world_drop_index() -> WorldDropIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None or not _INDEX.loaded:
            raise RuntimeError("WorldDropIndex not warmed")
        return _INDEX


def warmup_world_drop() -> dict:
    global _INDEX
    from game.drop.equip_stackable_pools import is_registered_drop_item

    last_err: Exception | None = None
    for path in WORLD_PATHS:
        try:
            text = pvf_runtime.read_text(path)
            idx = WorldDropIndex()
            idx.load_from_text(
                text,
                path=path,
                is_registered_item=is_registered_drop_item,
            )
            with _LOCK:
                _INDEX = idx
            return {
                "ok": 1,
                "path": path,
                "levels": idx.level_count,
                "items": idx.item_count,
                "unregistered_items": idx.rejected_item_count,
            }
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError("worlddrop load failed: %s" % (last_err,))


def clear_world_drop() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
