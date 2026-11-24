"""称号簿 / 成就完成 协议体（S0/0166 · S0/0168）。

基线：``server_proto/docs/baseline_titlebook_20260719.md``（tip de78ad321）。

Stage3 客户端真源（IDA 2026-07-19 / 闪退复现 2026-07-21）:
- S0/0166 handler ``sub_166A690``（注册表 push 0x166 → 0x0166A690）
  读序: u8 infoType | u16 ownerId16 | i32 category | i32 entryCount
        entry×N:
          固定 22B: u16 slot | i32 itemId | i32 value | u8 attr | u16 dura
                    | u8 seal | i32 ench | u8 enchUp | u8 ampType | u16 ampVal
          变长尾:   u32 blobLen | blob[blobLen]
                    （``sub_2BF4BC0`` = 读 u32 再 memcpy/memset 到本地 6B 槽；
                     缺省必须发 ``blobLen=0``，否则下一条 slot/itemId 被当成长度 →
                     大 memset 写穿栈；live 闪退 EIP 0x031DA888 / 调用栈 0166 handler）
  行为: 清分类再 put。进房 delayed 默认带条目；019C/019D 默认不发。
- S0/0168 handler ``sub_166A9F0``
  读序: i32 questId | i32 category | i32 bookIndex | i32 titleItemId | u16 tail
  行为: put + **固定播**解锁/获取动画（包体无静默位）。
  用途: 交任务获取动画；进房默认不发。
"""
from __future__ import annotations

import struct
from typing import Iterable, Mapping, Sequence, Union

# 固定字段 22B + 变长尾最小 4B（u32 blobLen=0）
TITLEBOOK_0166_ENTRY_FIXED = 22
TITLEBOOK_0166_ENTRY_MIN = TITLEBOOK_0166_ENTRY_FIXED + 4  # 26


def build_titlebook_list_0166(
    category: int,
    entries: Sequence[Mapping[str, Union[int, bytes]]] | None = None,
    *,
    info_type: int = 0,
    owner_id16: int = 0,
) -> bytes:
    rows = list(entries or ())
    body = bytearray()
    body.append(int(info_type) & 0xFF)
    body += struct.pack("<H", int(owner_id16) & 0xFFFF)
    body += struct.pack("<i", int(category))
    body += struct.pack("<i", len(rows))
    for ent in rows:
        slot = int(ent.get("slot_index", ent.get("book_index", 0)) or 0) & 0xFFFF
        item_id = int(ent.get("item_id", 0) or 0)
        value = int(ent.get("value", item_id) or 0)
        attr = int(ent.get("attr", 0) or 0) & 0xFF
        dura = int(ent.get("durability", 0) or 0) & 0xFFFF
        seal = int(ent.get("seal_flag", 0) or 0) & 0xFF
        ench = int(ent.get("enchant_index", 0) or 0)
        ench_up = int(ent.get("enchant_upgrade_count", 0) or 0) & 0xFF
        amp_t = int(ent.get("amplify_type", 0) or 0) & 0xFF
        amp_v = int(ent.get("amplify_value", 0) or 0) & 0xFFFF
        body += struct.pack(
            "<HiiBHBIBBH",
            slot,
            item_id,
            value,
            attr,
            dura,
            seal,
            ench,
            ench_up,
            amp_t,
            amp_v,
        )
        # 变长尾：u32 blobLen + blob。默认 0（称号簿普通条目无附加串）。
        # 2026-07-21 闪退：缺此 4B 时 2+ 条目的下一条 itemId 低字节被读成 n2，
        # sub_31C4AE0(memset) 在 0x031DA888 写 [ecx+0x60] AV。
        raw_blob = ent.get("blob", b"")
        if isinstance(raw_blob, (bytes, bytearray)):
            blob = bytes(raw_blob)
        elif raw_blob in (0, None, ""):
            blob = b""
        else:
            blob = b""
        if len(blob) > 0x10000:
            blob = blob[:0x10000]
        body += struct.pack("<I", len(blob))
        if blob:
            body += blob
    return bytes(body)


def build_titlebook_empty_categories_0166(
    category_count: int = 5,
) -> list[bytes]:
    """进房初始化：5 个空分类（无条目时客户端也能开称号簿）。"""
    n = max(0, int(category_count))
    return [build_titlebook_list_0166(cat, ()) for cat in range(n)]


def build_achievement_complete_0168(
    quest_id: int,
    category: int,
    book_index: int,
    title_item_id: int,
) -> bytes:
    qid = int(quest_id)
    cat = int(category)
    idx = int(book_index)
    iid = int(title_item_id)
    return struct.pack(
        "<iiiiH",
        qid,
        cat,
        idx,
        iid,
        max(0, idx) & 0xFFFF,
    )


def entries_from_repo_rows(rows: Iterable[Mapping[str, int]]) -> list[dict[str, int]]:
    out: list[dict[str, int]] = []
    for r in rows:
        iid = int(r.get("item_id", 0) or 0)
        if iid <= 0:
            continue
        out.append(
            {
                "slot_index": int(r.get("book_index", r.get("slot_index", 0)) or 0),
                "item_id": iid,
                "value": int(r.get("value", iid) or iid),
                "attr": int(r.get("attr", 0) or 0),
                "durability": int(r.get("durability", 0) or 0),
                "seal_flag": int(r.get("seal_flag", 0) or 0),
                "enchant_index": int(r.get("enchant_index", 0) or 0),
                "enchant_upgrade_count": int(r.get("enchant_upgrade_count", 0) or 0),
                "amplify_type": int(r.get("amplify_type", 0) or 0),
                "amplify_value": int(r.get("amplify_value", 0) or 0),
            }
        )
    return out
