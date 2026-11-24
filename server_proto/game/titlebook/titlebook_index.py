"""称号簿静态索引：etc/titlebook.etc（共享预加载 PVF）。

对照 S4A12 TitleBookStaticDataProvider：
- 5 分类 general/specific/pvp/despair/event
- 槽位：index / slotType / questId / allowed title item ids
- 奖励称号按 **item_id** 或 **quest_id** 定位槽位
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Iterable

from pvf import pvf_runtime


CATEGORY_NAMES: tuple[str, ...] = ("general", "specific", "pvp", "despair", "event")
# 与 S4A12 DefaultCapacities 对齐（titlebook.etc 内 capacity 可能略不同，以 etc 为准可扩）
DEFAULT_CAPACITIES: tuple[int, ...] = (90, 180, 50, 100, 100)

_LOCK = threading.RLock()
_WARM: "TitleBookIndex | None" = None


@dataclass(frozen=True)
class TitleBookSlot:
    category: int
    index: int
    slot_type: int = -1
    quest_id: int = -1
    allowed_item_ids: tuple[int, ...] = ()

    @property
    def is_open(self) -> bool:
        return int(self.slot_type) >= 0

    def allows_item(self, item_id: int) -> bool:
        if not self.is_open:
            return False
        iid = int(item_id)
        if not self.allowed_item_ids:
            return int(self.quest_id) == -1
        return iid in self.allowed_item_ids or -1 in self.allowed_item_ids


@dataclass
class TitleBookIndex:
    capacities: list[int] = field(default_factory=lambda: list(DEFAULT_CAPACITIES))
    slots: dict[tuple[int, int], TitleBookSlot] = field(default_factory=dict)
    by_item_id: dict[int, TitleBookSlot] = field(default_factory=dict)
    by_quest_id: dict[int, TitleBookSlot] = field(default_factory=dict)
    # achievement quest_id -> title item_id（adjust list 可选）
    adjust_quest_to_item: dict[int, int] = field(default_factory=dict)
    note: str = ""

    def capacity(self, category: int) -> int:
        cat = int(category)
        if 0 <= cat < len(self.capacities):
            return int(self.capacities[cat])
        return 0

    def get_slot(self, category: int, index: int) -> TitleBookSlot:
        key = (int(category), int(index))
        slot = self.slots.get(key)
        if slot is not None:
            return slot
        return TitleBookSlot(category=int(category), index=int(index))

    def find_by_item_id(self, item_id: int) -> TitleBookSlot | None:
        return self.by_item_id.get(int(item_id))

    def find_by_quest_id(self, quest_id: int) -> TitleBookSlot | None:
        return self.by_quest_id.get(int(quest_id) & 0xFFFF)

    def resolve_title_slot(
        self,
        *,
        item_id: int = 0,
        quest_id: int = 0,
    ) -> TitleBookSlot | None:
        """优先 item_id，其次 quest_id，再 adjust list。"""
        iid = int(item_id or 0)
        qid = int(quest_id or 0) & 0xFFFF
        if iid > 0:
            hit = self.find_by_item_id(iid)
            if hit is not None:
                return hit
        if qid > 0:
            hit = self.find_by_quest_id(qid)
            if hit is not None:
                return hit
            mapped = int(self.adjust_quest_to_item.get(qid, 0) or 0)
            if mapped > 0:
                return self.find_by_item_id(mapped)
        return None


def _strip_tick(s: str) -> str:
    return str(s or "").replace("`", "").strip()


def _strip_comment(line: str) -> str:
    raw = str(line or "")
    if "//" in raw:
        raw = raw.split("//", 1)[0]
    return _strip_tick(raw)


def _parse_collection_section(tokens: list[str], index: TitleBookIndex) -> None:
    if not tokens:
        return
    cat_name = tokens[0].lower()
    if cat_name not in CATEGORY_NAMES:
        return
    cat = CATEGORY_NAMES.index(cat_name)
    pos = 1
    # 可选 capacity
    if pos < len(tokens):
        try:
            cap = int(tokens[pos], 0)
            if cap > 0:
                while len(index.capacities) <= cat:
                    index.capacities.append(0)
                index.capacities[cat] = max(int(index.capacities[cat] or 0), cap)
            pos += 1
        except Exception:
            pass

    while pos < len(tokens):
        try:
            book_index = int(tokens[pos], 0)
            pos += 1
        except Exception:
            break
        if pos >= len(tokens):
            break
        try:
            slot_type = int(tokens[pos], 0)
            pos += 1
        except Exception:
            break
        quest_id = -1
        items: list[int] = []
        if slot_type >= 0:
            if pos >= len(tokens):
                break
            try:
                quest_id = int(tokens[pos], 0)
                pos += 1
            except Exception:
                break
            if pos >= len(tokens):
                break
            try:
                item_count = int(tokens[pos], 0)
                pos += 1
            except Exception:
                break
            for _ in range(max(0, item_count)):
                if pos >= len(tokens):
                    break
                try:
                    items.append(int(tokens[pos], 0))
                    pos += 1
                except Exception:
                    break
        slot = TitleBookSlot(
            category=cat,
            index=book_index,
            slot_type=slot_type,
            quest_id=quest_id,
            allowed_item_ids=tuple(items),
        )
        index.slots[(cat, book_index)] = slot
        if slot.is_open and quest_id > 0:
            index.by_quest_id[int(quest_id) & 0xFFFF] = slot
        if slot.is_open:
            for iid in items:
                if int(iid) > 0:
                    index.by_item_id[int(iid)] = slot


def _parse_adjust_list(text: str, index: TitleBookIndex) -> None:
    """可选 [adjust list]：三元组 quest_id / item_id / titlebook_quest_id。"""
    m = re.search(
        r"\[adjust list\](.*?)\[/adjust list\]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        # 无闭合时尝试到文件尾
        m2 = re.search(r"\[adjust list\](.*)$", text, flags=re.IGNORECASE | re.DOTALL)
        if not m2:
            return
        body = m2.group(1)
    else:
        body = m.group(1)
    nums: list[int] = []
    for tok in re.findall(r"-?\d+", body):
        try:
            nums.append(int(tok, 0))
        except Exception:
            continue
    # 常见形态：qid, item, titlebook_qid 循环
    for i in range(0, len(nums) - 2, 3):
        qid, iid, _tb_qid = nums[i], nums[i + 1], nums[i + 2]
        if qid > 0 and iid > 0:
            index.adjust_quest_to_item[int(qid) & 0xFFFF] = int(iid)


def parse_titlebook_etc(text: str) -> TitleBookIndex:
    index = TitleBookIndex(capacities=list(DEFAULT_CAPACITIES))
    if not text:
        index.note = "empty"
        return index

    in_section = False
    buf: list[str] = []
    for raw_line in str(text).splitlines():
        line = _strip_comment(raw_line)
        if not line:
            continue
        low = line.lower()
        if low == "[title collection info]":
            if in_section and buf:
                _parse_collection_section(buf, index)
            in_section = True
            buf = []
            continue
        if low == "[/title collection info]":
            if in_section and buf:
                _parse_collection_section(buf, index)
            in_section = False
            buf = []
            continue
        if not in_section:
            continue
        if line.startswith("[/"):
            continue
        buf.extend(line.split())
    if in_section and buf:
        _parse_collection_section(buf, index)

    _parse_adjust_list(text, index)
    open_n = sum(1 for s in index.slots.values() if s.is_open)
    index.note = "ok open=%s slots=%s items=%s quests=%s adjust=%s" % (
        open_n,
        len(index.slots),
        len(index.by_item_id),
        len(index.by_quest_id),
        len(index.adjust_quest_to_item),
    )
    return index


def load_titlebook_index_from_shared_pvf() -> TitleBookIndex:
    reader = pvf_runtime.ensure_shared_reader()
    if reader is None:
        return TitleBookIndex(note="no_shared_reader")
    try:
        text = reader.read_text("etc/titlebook.etc")
    except Exception as exc:
        return TitleBookIndex(note=f"read_fail:{exc}")
    return parse_titlebook_etc(text or "")


def get_titlebook_index() -> TitleBookIndex:
    global _WARM
    with _LOCK:
        if _WARM is not None:
            return _WARM
    idx = load_titlebook_index_from_shared_pvf()
    with _LOCK:
        _WARM = idx
        return idx


def warmup_titlebook_index() -> dict[str, int | str]:
    idx = load_titlebook_index_from_shared_pvf()
    with _LOCK:
        global _WARM
        _WARM = idx
    return {
        "ok": 1 if idx.note.startswith("ok") else 0,
        "note": idx.note,
        "open_slots": sum(1 for s in idx.slots.values() if s.is_open),
        "item_map": len(idx.by_item_id),
        "quest_map": len(idx.by_quest_id),
        "adjust": len(idx.adjust_quest_to_item),
        "capacities": ",".join(str(c) for c in idx.capacities),
    }


def resolve_title_items_for_quest(
    quest_id: int,
    item_ids: Iterable[int],
) -> list[tuple[TitleBookSlot, int]]:
    """(slot, item_id) 列表；无槽位映射的 item 跳过。"""
    idx = get_titlebook_index()
    out: list[tuple[TitleBookSlot, int]] = []
    seen: set[tuple[int, int]] = set()
    qid = int(quest_id) & 0xFFFF
    for raw_id in item_ids:
        iid = int(raw_id or 0)
        if iid <= 0:
            continue
        slot = idx.resolve_title_slot(item_id=iid, quest_id=qid)
        if slot is None or not slot.is_open:
            continue
        key = (slot.category, slot.index)
        if key in seen:
            continue
        seen.add(key)
        out.append((slot, iid))
    if not out and qid > 0:
        slot = idx.resolve_title_slot(quest_id=qid)
        if slot is not None and slot.is_open:
            iid = 0
            if slot.allowed_item_ids:
                iid = int(slot.allowed_item_ids[0])
            mapped = int(idx.adjust_quest_to_item.get(qid, 0) or 0)
            if mapped > 0:
                iid = mapped
            if iid > 0:
                out.append((slot, iid))
    return out
