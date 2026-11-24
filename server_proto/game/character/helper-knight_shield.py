# -*- coding: utf-8 -*-
"""骑士/守护者 盾牌栏（Knight Shield Deck）。

Stage3 live（2026-07-20 CCC 转职后；2026-07-22 拖槽/持久化钉死）：
  C1/0013 MOVE_ITEMSPACE
    equip 主:  src=(list=32 catalog, slot=0, iv=item_id) → dst=(list=31 equipped, slot=0)
               **仅** 写主盾 slot0 + slot24；**不** 自动把旧主塞进副槽。
               副槽只由拖放 / C1/0292 写入。
    equip 备:  catalog → 31:1..4（可选；UI 多拖到下方槽，常走 0292）
    unequip:   src=(list=31, slot=0|1..4, iv=0) → dst=(list=32 catalog, slot=?)
    技能切盾:  src=31:N → dst=31:0（deck 内 swap；主盾变更后刷 slot24 + 000E
               + **城镇** USERINFO0/1；**副本** 仅 USERINFO0，禁 U1）

  C1/0292 CHANGE_DECK_INFO（Stage3 IDA ``sub_15FE950`` @ 0x015FE950）
    拖动下方 1 主 + 4 副时客户端整 deck 上报：plain = 5×u32 LE。
    服务端写 ``character_knight_shield_deck``，回 S1/0292 ACK
    （u8 status=1 + u8 reserved=0 + 5×u32），主盾变更时刷 slot24/000E/U0。

  S0/0245 SEND_DECK_INFO（Stage3 IDA ``sub_1674530`` @ 0x01674530）
    plain = 5×u32 LE（无前缀）。进房 / 0013 盾变更后下发，UI 底部槽才持久可见。
    **禁止** 0x0244：Stage3 ``cmd_0244_clear_pending_close_ui42`` 是频道 settle。

客户端盾牌仓库两区：
  1) 盾牌列表（catalog ~21 外观）→ list32；右键 = 装主盾
  2) 预备槽 1 主 + 4 副 → list31 / 本地 deck；**拖放** 填副槽（C1/0292）
  主盾控制属性与外观（slot24）

对照（只借思路，不抄 wire）：
  - S4A12 list 为 33/34；**Stage3 live 真值为 31/32**。
  - S4 NOTI SEND_DECK=0x0245 / CMD CHANGE_DECK=0x0292；Stage3 同号已 IDA 钉死。
  - 主盾外观穿戴位 = `[support weapon]` **slot 24**（非 22）。
  - 副本普通换装默认 **不** 发 USERINFO0/1；**骑士主盾变更**例外放行 U0。

PVF 目录：etc/character/knight/shieldwindowdata.etc（[item index]+[grow type]）。
"""
from __future__ import annotations

import re
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# Stage3 live list_type（禁止改用 S4 的 33/34）
LIST_KNIGHT_SHIELD_EQUIPPED = 31
LIST_KNIGHT_SHIELD_CATALOG = 32

# Stage3 IDA 2026-07-22：
#   NOTI 0x0245 → sub_1674530：循环 5 次 sub_2C74B00 读 u32 → 写 deck 槽
#   CMD  0x0292 → sub_15FE950：ACK 路径读 status 后同样 5×u32
# 禁止 0x0244（cmd_0244_clear_pending_close_ui42 频道 settle）。
NOTI_SEND_DECK_INFO = 0x0245
CMD_CHANGE_DECK_INFO = 0x0292

# deck：0=主盾，1..4=备用
DECK_SLOT_COUNT = 5
DECK_MAIN_SLOT = 0

# 身体装备栏 [support weapon]（副手/盾）。Stage3 = 24（90qiyuan 同；S4 枚举 23 勿用）。
# 禁止写 slot22（与 [support] 冲突）。
# 2026-07-20 曾误判「写 slot24 必闪退」而默认剥离；次日钉死真因是称号
# S0/0166 多 entry 缺 blobLen（commit 2feb1af64）。7/20 21:05 live 已成功
# 下发 n=11 slots=…,24:113370003。默认由 standard_local_flow 打开
# USDOF_GAME_KNIGHT_SHIELD_EMIT_BODY_WIRE=1。
SUPPORT_WEAPON_EQUIP_SLOT = 24
# The confirmed shield wire/storage slot is 24.  Slots 22 and 23 are the
# auxiliary-equipment and magic-stone slots, respectively, and must never be
# treated as shield slots just because an old experiment used them.
SUPPORT_WEAPON_WIRE_SLOTS = (SUPPORT_WEAPON_EQUIP_SLOT,)
# Old local builds could persist a real shield in one of these candidates.
# Cleanup may inspect them by item type, but must not delete by slot alone.
LEGACY_SUPPORT_WEAPON_SLOTS = (22, 23, SUPPORT_WEAPON_EQUIP_SLOT)

GUARDIAN_JOB = 12  # characters.job == knight

_PVF_SHIELD_PATH = "etc/character/knight/shieldwindowdata.etc"

_catalog_lock = threading.Lock()
_catalog_by_grow: dict[int, tuple[int, ...]] | None = None
_catalog_all: frozenset[int] | None = None


@dataclass(frozen=True)
class KnightShieldDeck:
    """5 槽盾牌 deck；slot0 主盾。"""

    item_ids: tuple[int, int, int, int, int]

    @classmethod
    def empty(cls) -> "KnightShieldDeck":
        return cls((0, 0, 0, 0, 0))

    @classmethod
    def from_seq(cls, values: Sequence[int] | None) -> "KnightShieldDeck":
        arr = [0] * DECK_SLOT_COUNT
        for i, v in enumerate(list(values or ())[:DECK_SLOT_COUNT]):
            arr[i] = max(0, int(v) & 0xFFFFFFFF)
        return cls(tuple(arr))  # type: ignore[arg-type]

    @property
    def main_item_id(self) -> int:
        return int(self.item_ids[DECK_MAIN_SLOT])

    def with_main(self, item_id: int) -> "KnightShieldDeck":
        iid = max(0, int(item_id) & 0xFFFFFFFF)
        arr = list(self.item_ids)
        arr[DECK_MAIN_SLOT] = iid
        # 主盾若在备用槽出现则清空，避免重复
        if iid > 0:
            for i in range(1, DECK_SLOT_COUNT):
                if arr[i] == iid:
                    arr[i] = 0
        return KnightShieldDeck(tuple(arr))  # type: ignore[arg-type]

    def with_cleared_main(self) -> "KnightShieldDeck":
        arr = list(self.item_ids)
        arr[DECK_MAIN_SLOT] = 0
        return KnightShieldDeck(tuple(arr))  # type: ignore[arg-type]

    def swap_slots(self, src: int, dst: int) -> "KnightShieldDeck":
        arr = list(self.item_ids)
        s = int(src)
        d = int(dst)
        if not (0 <= s < DECK_SLOT_COUNT and 0 <= d < DECK_SLOT_COUNT):
            raise ValueError("deck slot out of range")
        arr[s], arr[d] = arr[d], arr[s]
        return KnightShieldDeck(tuple(arr))  # type: ignore[arg-type]


def is_shield_list_type(list_type: int) -> bool:
    lt = int(list_type) & 0xFF
    return lt in (LIST_KNIGHT_SHIELD_EQUIPPED, LIST_KNIGHT_SHIELD_CATALOG)


def is_eligible_character(*, job: int, grow_type: int = 0) -> bool:
    """守护者（job=12）。转职后 grow_type 一转 nibble>0 才有 UI，但 job 即可放行。"""
    return int(job) == GUARDIAN_JOB


def normalize_grow_nibble(grow_type: int) -> int:
    """一转 grow 取低 4 bit（与 S4A12 NormalizeGrowType 一致）。"""
    return int(grow_type) & 0x0F


def _load_catalog_from_pvf() -> tuple[dict[int, tuple[int, ...]], frozenset[int]]:
    text = ""
    try:
        from pvf import pvf_runtime

        reader = pvf_runtime.get_shared_reader()
        if reader is not None:
            text = reader.read_text(_PVF_SHIELD_PATH) or ""
    except Exception:
        text = ""
    if not text:
        try:
            from pvf.nkpi_reader import PvfReader
            from config import PVF_PATH

            reader = PvfReader.open(Path(PVF_PATH))
            try:
                text = reader.read_text(_PVF_SHIELD_PATH) or ""
            finally:
                try:
                    reader.close()
                except Exception:
                    pass
        except Exception:
            text = ""

    by_grow: dict[int, list[int]] = {}
    all_ids: set[int] = set()
    if text:
        for m in re.finditer(r"\[shield\](.*?)\[/shield\]", text, re.S | re.I):
            block = m.group(1)
            iid_m = re.search(r"\[item\s+index\]\s*(\d+)", block, re.I)
            gt_m = re.search(r"\[grow\s+type\]\s*(\d+)", block, re.I)
            if not iid_m:
                continue
            iid = int(iid_m.group(1))
            gt = int(gt_m.group(1)) if gt_m else 0
            if iid <= 0:
                continue
            all_ids.add(iid)
            by_grow.setdefault(gt, []).append(iid)
    frozen = {g: tuple(sorted(set(v))) for g, v in by_grow.items()}
    return frozen, frozenset(all_ids)


def get_catalog_maps() -> tuple[dict[int, tuple[int, ...]], frozenset[int]]:
    global _catalog_by_grow, _catalog_all
    with _catalog_lock:
        if _catalog_by_grow is None or _catalog_all is None:
            _catalog_by_grow, _catalog_all = _load_catalog_from_pvf()
        return _catalog_by_grow, _catalog_all


def is_catalog_shield(item_id: int, *, grow_type: int = 0) -> bool:
    """item 是否在盾牌目录。优先按 grow nibble 匹配；目录未加载时放行 11337xxxx。"""
    iid = int(item_id) & 0xFFFFFFFF
    if iid <= 0:
        return False
    by_grow, all_ids = get_catalog_maps()
    if all_ids:
        gt = normalize_grow_nibble(grow_type)
        if gt > 0 and gt in by_grow and iid in by_grow[gt]:
            return True
        # grow 未匹配时仍允许目录内任意盾（任务解锁后客户端会发）
        return iid in all_ids
    # PVF 未就绪：接受 113370000..113379999 段
    return 113370000 <= iid <= 113379999


def build_send_deck_info_body(deck: KnightShieldDeck | Sequence[int] | None) -> bytes:
    """S0/0245 body：固定 5×u32 LE，无前缀（Stage3 sub_1674530）。"""
    d = deck if isinstance(deck, KnightShieldDeck) else KnightShieldDeck.from_seq(deck)
    out = bytearray()
    for i in range(DECK_SLOT_COUNT):
        out += struct.pack("<I", int(d.item_ids[i]) & 0xFFFFFFFF)
    return bytes(out)


def build_change_deck_ack_body(deck: KnightShieldDeck | Sequence[int] | None) -> bytes:
    """S1/0292 ACK：u8 status=1 + u8 reserved=0 + 5×u32（对照 S4 / Stage3 reader）。"""
    d = deck if isinstance(deck, KnightShieldDeck) else KnightShieldDeck.from_seq(deck)
    out = bytearray()
    out.append(1)
    out.append(0)
    for i in range(DECK_SLOT_COUNT):
        out += struct.pack("<I", int(d.item_ids[i]) & 0xFFFFFFFF)
    return bytes(out)


def parse_change_deck_request(body: bytes) -> tuple[int, int, int, int, int] | None:
    """C1/0292 plain：5×u32 LE（至少 20B；多出的尾忽略）。"""
    data = bytes(body or b"")
    if len(data) < DECK_SLOT_COUNT * 4:
        return None
    vals = []
    for i in range(DECK_SLOT_COUNT):
        vals.append(struct.unpack_from("<I", data, i * 4)[0] & 0xFFFFFFFF)
    return (vals[0], vals[1], vals[2], vals[3], vals[4])


def should_send_deck_notification() -> bool:
    """是否允许发 S0/0245 deck NOTI。默认开；=0 可关。"""
    import os

    if int(NOTI_SEND_DECK_INFO) <= 0:
        return False
    raw = os.environ.get("USDOF_GAME_KNIGHT_SHIELD_SEND_DECK_NOTI")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}




def should_emit_support_weapon_on_body_wire() -> bool:
    """是否把副手写进 USERINFO/000E 身体装备线体。

    日常 profile（standard_local_flow）默认开：主盾同步 slot24 + 装备栏/纸娃娃。
    未设 env 时默认 False（单测/裸起服保守）。关：=0。
    进房闪退真因已钉为称号 0166 blobLen，不是 slot24 wire。
    """
    import os

    raw = os.environ.get("USDOF_GAME_KNIGHT_SHIELD_EMIT_BODY_WIRE")
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def is_support_weapon_body_slot(slot: int) -> bool:
    return int(slot) & 0xFF in SUPPORT_WEAPON_WIRE_SLOTS


def is_support_weapon_item(item_id: int) -> bool:
    """PVF [support weapon] 或骑士盾 id 段。"""
    iid = int(item_id) & 0xFFFFFFFF
    if iid <= 0:
        return False
    if 113370000 <= iid <= 113379999:
        return True
    try:
        from game.character.item_metadata import resolve_item_metadata

        meta = resolve_item_metadata(iid)
        et = str(getattr(meta, "equipment_type", "") or "").lower().strip()
        return et in {"[support weapon]", "support weapon"}
    except Exception:
        return False


def filter_body_equipment_entries(entries):
    """从身体装备列表去掉真实骑士盾（默认）。

    This helper is shared by every job's USERINFO1/list3 projection.  Slot
    numbers alone are not enough to identify a knight shield: slots 22/23 are
    extension equipment and slot 24 is also the Blade Dancer secondary weapon.
    """
    if should_emit_support_weapon_on_body_wire():
        return tuple(entries or ())
    out = []
    for entry in entries or ():
        if isinstance(entry, dict):
            item_id = int(entry.get("item_id", 0) or 0) & 0xFFFFFFFF
        else:
            item_id = int(getattr(entry, "item_id", 0) or 0) & 0xFFFFFFFF
        if is_support_weapon_item(item_id):
            continue
        out.append(entry)
    return tuple(out)


def describe_deck(deck: KnightShieldDeck | Sequence[int] | None) -> str:
    d = deck if isinstance(deck, KnightShieldDeck) else KnightShieldDeck.from_seq(deck)
    return "deck=[%s]" % ",".join(str(int(x)) for x in d.item_ids)


__all__ = [
    "LIST_KNIGHT_SHIELD_EQUIPPED",
    "LIST_KNIGHT_SHIELD_CATALOG",
    "NOTI_SEND_DECK_INFO",
    "CMD_CHANGE_DECK_INFO",
    "DECK_SLOT_COUNT",
    "DECK_MAIN_SLOT",
    "SUPPORT_WEAPON_EQUIP_SLOT",
    "SUPPORT_WEAPON_WIRE_SLOTS",
    "LEGACY_SUPPORT_WEAPON_SLOTS",
    "GUARDIAN_JOB",
    "should_emit_support_weapon_on_body_wire",
    "is_support_weapon_body_slot",
    "is_support_weapon_item",
    "filter_body_equipment_entries",
    "KnightShieldDeck",
    "is_shield_list_type",
    "is_eligible_character",
    "normalize_grow_nibble",
    "get_catalog_maps",
    "is_catalog_shield",
    "build_send_deck_info_body",
    "build_change_deck_ack_body",
    "parse_change_deck_request",
    "should_send_deck_notification",
    "describe_deck",
]
