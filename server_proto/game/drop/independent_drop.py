# -*- coding: utf-8 -*-
"""``etc/independent_drop.etc`` — 怪专属独立掉落。

每条 17 列 + 可选 [list]；启动时按 monster_code 缓存在内存。

- type=0：count[diff] 次独立判定（prob/1e6）。
- type=1：每次先结算 ``prob // 1e6`` 倍保底数量，再以余数作一次
  百万分率附加判定；例如 4_000_000 × count=1 固定掉 4 件。

flag2 依赖 independentdrop.lst（本包空 → 无条目）。

硬规则：抽中的 item_id 必须通过 allow_item 回调（配置内 + lst 可解析）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.pvf_section import parse_lst_entries
from pvf import pvf_runtime

INDEPENDENT_PATH = "etc/independent_drop.etc"
INDEPENDENT_LST = "etc/independentdrop.lst"
PROB_DENOM = 1_000_000


@dataclass
class IndependentEntry:
    entry_type: int
    item_id: int
    probs: List[int]  # 5
    counts: List[int]  # 5
    level_min: int
    level_max: int
    difficulty: int  # -1 = any
    list_items: List[Tuple[int, int]] = field(default_factory=list)  # (id, weight)
    total_weight: int = 0


class IndependentDropIndex:
    def __init__(self) -> None:
        self.by_monster: Dict[int, List[IndependentEntry]] = {}
        self.entry_count = 0
        self.monster_count = 0
        self.type0_entry_count = 0
        self.type1_entry_count = 0
        self.rejected_item_count = 0
        self.loaded = False

    def load_from_text(
        self,
        text: str,
        script_pools: Optional[Dict[int, List[Tuple[int, int]]]] = None,
        *,
        is_registered_item: Optional[Callable[[int], bool]] = None,
    ) -> None:
        script_pools = script_pools or {}
        candidate_is_registered = is_registered_item or (lambda _item_id: True)

        def is_registered(item_id: int) -> bool:
            try:
                return bool(candidate_is_registered(int(item_id)))
            except Exception:
                return False

        # 从 [independent drop] 起按 token 流解析
        low = text.lower()
        tag = "[independent drop]"
        start = low.find(tag)
        if start < 0:
            raise RuntimeError("independent_drop missing [independent drop]")
        body = text[start + len(tag) :]
        tokens = body.replace("\r", "\n").replace("\t", " ").split()
        # 去掉 // 注释 token 行已在 split 前粗处理：过滤以 // 开头
        cleaned: List[str] = []
        for t in tokens:
            if t.startswith("//"):
                continue
            cleaned.append(t)
        tokens = cleaned
        i = 0
        by_mon: Dict[int, List[IndependentEntry]] = {}
        entry_count = 0
        type0_entry_count = 0
        type1_entry_count = 0
        rejected_item_count = 0

        def next_int() -> int:
            nonlocal i
            if i >= len(tokens):
                return 0
            t = tokens[i]
            i += 1
            try:
                return int(t, 10)
            except ValueError:
                return 0

        while i + 16 < len(tokens):
            peek = tokens[i]
            if peek.startswith("[") and peek.lower() not in ("[list]",):
                # 下一 section
                if peek.lower() != "[list]":
                    break
            type_v = next_int()
            monster_code = next_int()
            item_id = next_int()
            probs = [next_int() for _ in range(5)]
            counts = [next_int() for _ in range(5)]
            level_min = next_int()
            level_max = next_int()
            diff = next_int()
            list_flag = next_int()

            list_items: List[Tuple[int, int]] = []
            total_weight = 0

            if list_flag == 1:
                # skip to [list]
                guard = 8
                while i < len(tokens) and tokens[i].lower() != "[list]" and guard > 0:
                    i += 1
                    guard -= 1
                if i < len(tokens) and tokens[i].lower() == "[list]":
                    i += 1
                while i < len(tokens):
                    if tokens[i].lower() == "[/list]":
                        i += 1
                        break
                    iid = next_int()
                    if i >= len(tokens) or tokens[i].lower() == "[/list]":
                        if i < len(tokens) and tokens[i].lower() == "[/list]":
                            i += 1
                        break
                    w = next_int()
                    if iid > 0 and w > 0 and is_registered(iid):
                        total_weight += w
                        list_items.append((iid, w))
                    elif iid > 0 and w > 0:
                        rejected_item_count += 1
            elif list_flag == 2:
                guard = 8
                while i < len(tokens) and tokens[i].lower() != "[list]" and guard > 0:
                    i += 1
                    guard -= 1
                if i < len(tokens) and tokens[i].lower() == "[list]":
                    i += 1
                indices: List[int] = []
                while i < len(tokens):
                    if tokens[i].lower() == "[/list]":
                        i += 1
                        break
                    indices.append(next_int())
                merged: List[Tuple[int, int]] = []
                for idx in indices:
                    merged.extend(script_pools.get(int(idx), []))
                for iid, w in merged:
                    if iid > 0 and w > 0 and is_registered(iid):
                        total_weight += w
                        list_items.append((iid, w))
                    elif iid > 0 and w > 0:
                        rejected_item_count += 1

            if type_v not in (0, 1):
                continue

            configured_item_id = int(item_id)
            if configured_item_id > 0 and not is_registered(configured_item_id):
                configured_item_id = 0
                rejected_item_count += 1
            # All random paths must have a lst-registered candidate before
            # their probability/count rolls can reach the DIE pipeline.
            if configured_item_id <= 0 and not list_items:
                continue

            entry = IndependentEntry(
                entry_type=int(type_v),
                item_id=configured_item_id,
                probs=probs,
                counts=counts,
                level_min=int(level_min),
                level_max=int(level_max),
                difficulty=int(diff),
                list_items=list_items,
                total_weight=int(total_weight),
            )
            by_mon.setdefault(int(monster_code), []).append(entry)
            entry_count += 1
            if type_v == 0:
                type0_entry_count += 1
            else:
                type1_entry_count += 1

        self.by_monster = by_mon
        self.entry_count = entry_count
        self.monster_count = len(by_mon)
        self.type0_entry_count = type0_entry_count
        self.type1_entry_count = type1_entry_count
        self.rejected_item_count = rejected_item_count
        self.loaded = True

    def generate(
        self,
        *,
        monster_code: int,
        difficulty: int,
        dungeon_basis_level: int,
        lcg: DnfLcg,
        allow_item: Callable[[int], bool],
        drop_rate_bonus_percent: int = 0,
    ) -> List[int]:
        """返回本次击杀独立掉落的 item_id 列表（可重复）。"""
        entries = self.by_monster.get(int(monster_code))
        if not entries:
            return []
        diff_idx = max(0, min(int(difficulty), 4))
        result: List[int] = []
        for entry in entries:
            if entry.level_min > 0 and entry.level_max > 0:
                if (
                    int(dungeon_basis_level) < entry.level_min
                    or int(dungeon_basis_level) > entry.level_max
                ):
                    continue
            if entry.difficulty >= 0 and entry.difficulty != int(difficulty):
                continue
            prob = int(entry.probs[diff_idx])
            count = int(entry.counts[diff_idx])
            if prob <= 0 or count <= 0:
                continue
            bonus_percent = max(0, int(drop_rate_bonus_percent))
            if bonus_percent > 0:
                prob = (prob * (100 + bonus_percent)) // 100
                if entry.entry_type == 0:
                    prob = min(PROB_DENOM, prob)

            drop_count = 0
            if entry.entry_type == 0 and (entry.item_id != 0 or entry.total_weight > 0):
                for _ in range(count):
                    if prob > lcg.next(PROB_DENOM):
                        drop_count += 1
            elif entry.entry_type == 0:
                drop_count = count
            elif entry.entry_type == 1:
                whole_multiplier, residual_prob = divmod(prob, PROB_DENOM)
                drop_count = int(whole_multiplier) * count
                if residual_prob:
                    for _ in range(count):
                        if residual_prob > lcg.next(PROB_DENOM):
                            drop_count += 1
            if drop_count <= 0:
                continue

            if entry.total_weight > 0 and entry.list_items:
                for _ in range(drop_count):
                    roll = lcg.next(entry.total_weight)
                    cum = 0
                    chosen = entry.list_items[-1][0]
                    for iid, w in entry.list_items:
                        cum += int(w)
                        if roll < cum:
                            chosen = iid
                            break
                    if allow_item(int(chosen)):
                        result.append(int(chosen))
            elif entry.item_id > 0:
                if allow_item(int(entry.item_id)):
                    for _ in range(drop_count):
                        result.append(int(entry.item_id))
        return result


def _load_script_pools() -> Dict[int, List[Tuple[int, int]]]:
    pools: Dict[int, List[Tuple[int, int]]] = {}
    try:
        lst_text = pvf_runtime.read_text(INDEPENDENT_LST)
    except Exception:
        return pools
    for pool_id, rel in parse_lst_entries(lst_text):
        path = rel.replace("\\", "/").lstrip("/")
        full = path if path.lower().startswith("etc/") else "etc/" + path
        try:
            script = pvf_runtime.read_text(full)
        except Exception:
            continue
        # [list] id w ...
        low = script.lower()
        s = low.find("[list]")
        if s < 0:
            continue
        s += len("[list]")
        e = low.find("[/list]", s)
        block = script[s : e if e >= 0 else len(script)]
        toks = block.replace("\r", " ").replace("\n", " ").replace("\t", " ").split()
        items: List[Tuple[int, int]] = []
        j = 0
        while j + 1 < len(toks):
            try:
                iid = int(toks[j], 10)
                w = int(toks[j + 1], 10)
            except ValueError:
                j += 1
                continue
            if iid > 0 and w > 0:
                items.append((iid, w))
            j += 2
        if items:
            pools[int(pool_id)] = items
    return pools


_LOCK = threading.RLock()
_INDEX: Optional[IndependentDropIndex] = None


def get_independent_drop_index() -> IndependentDropIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None or not _INDEX.loaded:
            raise RuntimeError("IndependentDropIndex not warmed")
        return _INDEX


def warmup_independent_drop() -> dict:
    global _INDEX
    from game.drop.equip_stackable_pools import is_registered_drop_item

    text = pvf_runtime.read_text(INDEPENDENT_PATH)
    script_pools = _load_script_pools()
    idx = IndependentDropIndex()
    idx.load_from_text(
        text,
        script_pools=script_pools,
        is_registered_item=is_registered_drop_item,
    )
    with _LOCK:
        _INDEX = idx
    return {
        "ok": 1,
        "path": INDEPENDENT_PATH,
        "entries": idx.entry_count,
        "monsters": idx.monster_count,
        "type0_entries": idx.type0_entry_count,
        "type1_entries": idx.type1_entry_count,
        "script_pools": len(script_pools),
        "unregistered_items": idx.rejected_item_count,
    }


def clear_independent_drop() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
