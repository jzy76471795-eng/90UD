# -*- coding: utf-8 -*-
"""通用装备/材料池。

装备池优先 ``etc/itemdictionary/(r)itemdictionary.etc``（USDOF 列布局），
失败/过稀时合并 ``equipment/equipment.lst`` + ``.equ`` 的
``[grade]/[rarity]/[creation rate]``（对齐 s4a12 EquipmentDropPoolProvider）。

材料池：``stackable/stackable.lst`` + stk 字段。

硬规则（bookmark ``creation rate``）：
- creation/gen rate <= 0 → **永不入随机池**
- 仅 lst 中存在的 id 可被选中 / allow
"""
from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional, Tuple

from game.drop.dnf_lcg import DnfLcg
from game.drop.monster_drop_config import get_monster_drop_config
from game.drop.pvf_section import parse_lst_entries
from pvf import pvf_runtime

_ITEMDICT_CANDIDATES = (
    "etc/itemdictionary/(r)itemdictionary.etc",
    "etc/itemdictionary/(f)itemdictionary.etc",
)

_STACK_EXCLUDE_PREFIXES = (
    "cash/",
    "quest/",
    "recipe/",
    "temp/",
    "event/",
    "emblem/",
    "monstercard/",
    "monster card/",
)

_GRADE_RE = re.compile(r"\[grade\]\s*(\d+)", re.I)
_RARITY_RE = re.compile(r"\[rarity\]\s*(\d+)", re.I)
_CREATION_RE = re.compile(r"\[creation rate\]\s*(\d+)", re.I)


def _pool_key(grade: int, rarity: int) -> int:
    return int(grade) * 10 + int(rarity)


class EquipStackablePools:
    def __init__(self) -> None:
        self.equip_pool: Dict[int, List[Tuple[int, int]]] = {}  # key -> (id, weight)
        self.stackable_pool: Dict[int, List[Tuple[int, int]]] = {}
        self.item_ids_equip: set[int] = set()
        self.item_ids_stackable: set[int] = set()
        self.loaded = False
        self.itemdict_path = ""
        self.equip_count = 0
        self.equip_from_dict = 0
        self.equip_from_equ = 0
        self.stackable_count = 0
        self.equip_non_lst_rejected = 0

    def is_registered_item(self, item_id: int) -> bool:
        """Return whether an item is registered by an authoritative item lst."""
        iid = int(item_id)
        if iid <= 0:
            return False
        return iid in self.item_ids_equip or iid in self.item_ids_stackable

    def is_known_item(self, item_id: int) -> bool:
        """Compatibility alias for the strict equipment/stackable lst gate."""
        return self.is_registered_item(item_id)

    def load(self) -> None:
        self._load_equipment_lst_ids()
        self._load_equipment_dictionary()
        # 字典过稀或为空 → 用 .equ creation rate 补全（s4a12 EquipmentDropPoolProvider）
        if self.equip_count < 100:
            self._load_equipment_from_equ_scripts()
        self._load_stackable_pool()
        self.loaded = True
        if self.equip_count <= 0 and self.stackable_count <= 0:
            raise RuntimeError("equip+stackable drop pools both empty")

    def _load_equipment_lst_ids(self) -> None:
        """equipment.lst 全量 id 登记（allow 校验用，不论是否入随机池）。"""
        try:
            text = pvf_runtime.read_text("equipment/equipment.lst")
        except Exception:
            return
        for iid, _rel in parse_lst_entries(text):
            if int(iid) > 0:
                self.item_ids_equip.add(int(iid))

    def _add_equip(self, item_id: int, grade: int, rarity: int, weight: int) -> bool:
        if item_id <= 0 or weight <= 0:
            return False
        # ItemDictionary supplies drop fields only.  It must not create an
        # equipment identity that is absent from equipment/equipment.lst.
        if int(item_id) not in self.item_ids_equip:
            self.equip_non_lst_rejected += 1
            return False
        if grade <= 0 or grade > 200:
            return False
        if rarity < 0 or rarity > 5:
            return False
        key = _pool_key(grade, rarity)
        self.equip_pool.setdefault(key, []).append((int(item_id), int(weight)))
        self.equip_count += 1
        return True

    def _load_equipment_dictionary(self) -> None:
        """USDOF ItemDictionary 行：id rarity grade category genRate ... `name`

        与旧 s4a12 MonsterDropConfig（category@4 gen@5）不同：本包 category@3 gen@4。
        """
        text = ""
        path_used = ""
        for path in _ITEMDICT_CANDIDATES:
            try:
                text = pvf_runtime.read_text(path)
                path_used = path
                break
            except Exception:
                continue
        if not text:
            return
        self.itemdict_path = path_used

        # 按 `name` 切行
        chunks = re.split(r"`[^`]*`", text)
        added = 0
        for chunk in chunks:
            nums: List[int] = []
            for tok in chunk.replace("\r", " ").replace("\n", " ").replace("\t", " ").split():
                try:
                    nums.append(int(tok, 10))
                except ValueError:
                    continue
            if len(nums) < 5:
                continue
            item_id = int(nums[0])
            rarity = int(nums[1])
            grade = int(nums[2])
            equip_category = int(nums[3])
            gen_rate = int(nums[4])
            if gen_rate <= 0:
                continue
            main_cat = equip_category // 1000
            if main_cat < 10 or main_cat > 12:
                continue
            if self._add_equip(item_id, grade, rarity, gen_rate):
                added += 1
        self.equip_from_dict = added

    def _load_equipment_from_equ_scripts(self) -> None:
        """s4a12 EquipmentDropPoolProvider：扫 equipment.lst 读 .equ 的 creation rate。"""
        try:
            lst_text = pvf_runtime.read_text("equipment/equipment.lst")
        except Exception:
            return
        entries = parse_lst_entries(lst_text)
        added = 0
        for item_id, rel in entries:
            path = rel.replace("\\", "/").lstrip("/")
            full = path if path.lower().startswith("equipment/") else "equipment/" + path
            try:
                equ = pvf_runtime.read_text(full)
            except Exception:
                continue
            cm = _CREATION_RE.search(equ)
            creation = int(cm.group(1)) if cm else 0
            if creation <= 0:
                continue
            gm = _GRADE_RE.search(equ)
            if not gm:
                continue
            grade = int(gm.group(1))
            rm = _RARITY_RE.search(equ)
            rarity = int(rm.group(1)) if rm else 0
            if self._add_equip(int(item_id), grade, rarity, creation):
                added += 1
        self.equip_from_equ = added

    def _load_stackable_pool(self) -> None:
        lst_text = pvf_runtime.read_text("stackable/stackable.lst")
        entries = parse_lst_entries(lst_text)
        pool: Dict[int, List[Tuple[int, int]]] = {}
        known: set[int] = set()
        # 先登记全部 lst id（allow 用）；入池仍要 creation rate>0
        for item_id, rel in entries:
            known.add(int(item_id))
        added = 0
        for item_id, rel in entries:
            if item_id <= 2:
                continue
            path = rel.replace("\\", "/").lstrip("/")
            low = path.lower()
            if any(low.startswith(p) for p in _STACK_EXCLUDE_PREFIXES):
                continue
            full = path if low.startswith("stackable/") else "stackable/" + path
            try:
                stk = pvf_runtime.read_text(full)
            except Exception:
                continue
            cm = _CREATION_RE.search(stk)
            creation = int(cm.group(1)) if cm else 0
            if creation <= 0:
                continue
            gm = _GRADE_RE.search(stk)
            if not gm:
                continue
            grade = int(gm.group(1))
            if grade <= 0 or grade > 200:
                continue
            rm = _RARITY_RE.search(stk)
            rarity = int(rm.group(1)) if rm else 0
            if rarity < 0 or rarity > 6:
                continue
            key = _pool_key(grade, rarity)
            pool.setdefault(key, []).append((int(item_id), int(creation)))
            added += 1
        self.stackable_pool = pool
        self.item_ids_stackable |= known
        self.stackable_count = added

    def _collect_stackables(
        self, monster_level: int, down: int, up: int, rarity: int
    ) -> List[Tuple[int, int]]:
        result: List[Tuple[int, int]] = []
        if down < 0 or up <= 0:
            return result
        for offset in range(-int(down), int(up)):
            grade = int(monster_level) + int(offset)
            if grade < 1 or grade > 200:
                continue
            result.extend(self.stackable_pool.get(_pool_key(grade, rarity), []))
        return result

    def _collect_equips(
        self, monster_level: int, down: int, up: int, rarity: int
    ) -> List[Tuple[int, int]]:
        result: List[Tuple[int, int]] = []
        if down < 0 or up <= 0:
            return result
        for offset in range(-int(down), int(up)):
            grade = int(monster_level) + int(offset)
            if grade < 1 or grade > 200:
                continue
            result.extend(self.equip_pool.get(_pool_key(grade, rarity), []))
        return result

    def choose_stackable(self, lcg: DnfLcg, monster_level: int, rarity: int) -> int:
        cfg = get_monster_drop_config()
        win = cfg.grade_window(monster_level)
        return self.choose_stackable_in_window(
            lcg,
            monster_level,
            rarity,
            down=win.down,
            up=win.up,
        )

    def choose_stackable_in_window(
        self,
        lcg: DnfLcg,
        item_level: int,
        rarity: int,
        *,
        down: int,
        up: int,
    ) -> int:
        cands = self._collect_stackables(item_level, down, up, rarity)
        if not cands:
            return 0
        total = sum(int(weight) for _item_id, weight in cands if int(weight) > 0)
        if total <= 0:
            return 0
        roll = int(lcg.next(total))
        cursor = 0
        for item_id, weight in cands:
            if int(weight) <= 0:
                continue
            cursor += int(weight)
            if roll < cursor:
                return int(item_id)
        return int(cands[-1][0])

    def choose_equipment(self, lcg: DnfLcg, monster_level: int, rarity: int) -> int:
        cfg = get_monster_drop_config()
        win = cfg.grade_window(monster_level)
        return self.choose_equipment_in_window(
            lcg,
            monster_level,
            rarity,
            down=win.down,
            up=win.up,
        )

    def choose_equipment_in_window(
        self,
        lcg: DnfLcg,
        item_level: int,
        rarity: int,
        *,
        down: int,
        up: int,
    ) -> int:
        cands = self._collect_equips(item_level, down, up, rarity)
        if not cands:
            return 0
        total = 0
        for _id, w in cands:
            if w > 0:
                total += int(w)
        if total <= 0:
            return 0
        roll = lcg.next(total)
        cum = 0
        for iid, w in cands:
            if w <= 0:
                continue
            cum += int(w)
            if roll < cum:
                return int(iid)
        return int(cands[-1][0])


_LOCK = threading.RLock()
_POOLS: Optional[EquipStackablePools] = None


def _warmup_report(pools: EquipStackablePools) -> dict:
    return {
        "ok": 1,
        "itemdict_path": pools.itemdict_path,
        "equip_count": pools.equip_count,
        "equip_from_dict": pools.equip_from_dict,
        "equip_from_equ": pools.equip_from_equ,
        "stackable_count": pools.stackable_count,
        "equip_buckets": len(pools.equip_pool),
        "stackable_buckets": len(pools.stackable_pool),
        "equip_lst_ids": len(pools.item_ids_equip),
        "stackable_lst_ids": len(pools.item_ids_stackable),
        "equip_non_lst_rejected": pools.equip_non_lst_rejected,
    }


def get_equip_stackable_pools() -> EquipStackablePools:
    global _POOLS
    with _LOCK:
        if _POOLS is None or not _POOLS.loaded:
            raise RuntimeError("EquipStackablePools not warmed")
        return _POOLS


def is_registered_drop_item(item_id: int) -> bool:
    """Strict production gate for every non-gold dungeon ground-drop ID."""
    return get_equip_stackable_pools().is_registered_item(item_id)


def warmup_equip_stackable_pools() -> dict:
    global _POOLS
    pools = EquipStackablePools()
    pools.load()
    with _LOCK:
        _POOLS = pools
    return _warmup_report(pools)


def install_equip_stackable_pools(pools: EquipStackablePools) -> dict:
    if not isinstance(pools, EquipStackablePools) or not pools.loaded:
        raise ValueError("loaded EquipStackablePools required")
    global _POOLS
    with _LOCK:
        _POOLS = pools
    return _warmup_report(pools)


def clear_equip_stackable_pools() -> None:
    global _POOLS
    with _LOCK:
        _POOLS = None
