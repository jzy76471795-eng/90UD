# -*- coding: utf-8 -*-
"""怪物脚本 ``[item]`` 专属独立掉落池。

有效池每次击杀必定按 weight 选择一件；weight 只做选型，不是出率。
"""
from __future__ import annotations

import re
import threading
from typing import Callable, Dict, List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import parse_lst_entries
from pvf import pvf_runtime

_ITEM_BLOCK_RE = re.compile(r"\[item\]\s*([\s\S]*?)\s*\[/item\]", re.I)


class MobItemPoolIndex:
    def __init__(self) -> None:
        self.monster_paths: Dict[int, str] = {}
        self.pool_cache: Dict[int, List[Tuple[int, int]]] = {}
        self.loaded = False
        self.monster_lst_count = 0
        self.rejected_item_count = 0
        self._is_registered_item: Callable[[int], bool] = lambda _item_id: True

    def load_monster_lst(
        self,
        *,
        is_registered_item: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self._is_registered_item = is_registered_item or (lambda _item_id: True)
        text = pvf_runtime.read_text("monster/monster.lst")
        paths: Dict[int, str] = {}
        for code, rel in parse_lst_entries(text):
            paths[int(code)] = rel.replace("\\", "/").lstrip("/")
        self.monster_paths = paths
        self.monster_lst_count = len(paths)
        self.loaded = True

    def get_pool(self, monster_code: int) -> List[Tuple[int, int]]:
        code = int(monster_code)
        if code in self.pool_cache:
            return list(self.pool_cache[code])
        rel = self.monster_paths.get(code)
        if not rel:
            self.pool_cache[code] = []
            return []
        full = rel if rel.lower().startswith("monster/") else "monster/" + rel
        try:
            text = pvf_runtime.read_text(full)
        except Exception:
            self.pool_cache[code] = []
            return []
        m = _ITEM_BLOCK_RE.search(text)
        if not m:
            self.pool_cache[code] = []
            return []
        toks = m.group(1).replace("\r", " ").replace("\n", " ").replace("\t", " ").split()
        pool: List[Tuple[int, int]] = []
        for i in range(0, len(toks) - 1, 2):
            try:
                iid = int(toks[i], 10)
                w = int(toks[i + 1], 10)
            except ValueError:
                continue
            if iid <= 0 or w <= 0:
                continue
            try:
                registered = bool(self._is_registered_item(int(iid)))
            except Exception:
                registered = False
            if not registered:
                self.rejected_item_count += 1
                continue
            pool.append((iid, w))
        self.pool_cache[code] = pool
        return list(pool)

    def generate(
        self,
        monster_code: int,
        lcg: DnfLcg,
        allow_item: Callable[[int], bool],
    ) -> List[int]:
        pool = []
        for item_id, weight in self.get_pool(monster_code):
            try:
                allowed = bool(allow_item(int(item_id)))
            except Exception:
                allowed = False
            if allowed:
                pool.append((int(item_id), int(weight)))
        if not pool:
            return []
        total = sum(int(w) for _i, w in pool)
        if total <= 0:
            return []
        roll = lcg.next(total)
        cum = 0
        chosen = pool[-1][0]
        for iid, w in pool:
            cum += int(w)
            if roll < cum:
                chosen = iid
                break
        return [int(chosen)]


_LOCK = threading.RLock()
_INDEX: Optional[MobItemPoolIndex] = None


def get_mob_item_pool_index() -> MobItemPoolIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None or not _INDEX.loaded:
            raise RuntimeError("MobItemPoolIndex not warmed")
        return _INDEX


def warmup_mob_item_pool() -> dict:
    global _INDEX
    from game.drop.equip_stackable_pools import is_registered_drop_item

    idx = MobItemPoolIndex()
    idx.load_monster_lst(is_registered_item=is_registered_drop_item)
    with _LOCK:
        _INDEX = idx
    return {
        "ok": 1,
        "monster_lst": idx.monster_lst_count,
        "unregistered_items": idx.rejected_item_count,
    }


def clear_mob_item_pool() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
