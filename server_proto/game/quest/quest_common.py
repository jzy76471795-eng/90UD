"""Quest protocol helpers (pure; no socket/DB side effects).

Module boundary:
- parse C2S quest bodies
- build S2C ACK / list bodies for quest opcodes
- filter acceptable ids against active/finished

Does NOT own routing, session mutation, or START_MAP orchestration.
"""
from __future__ import annotations

import os
import struct
from typing import Iterable, Sequence

from game.content_blocking import is_quest_blocked


def is_client_hidden_quest_id(quest_id: int) -> bool:
    return is_quest_blocked(int(quest_id))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except Exception:
        return int(default)


def parse_quest_id_from_body(plain: bytes, *, expected_opcode: int | None = None) -> dict:
    """Stage3-first quest id parse.

    Live layouts:
      ACCEPT  : 1f 00 f8 03 ...  -> opcode_echo + quest
      TRIGGER : 21 00 0c 00 ...  -> opcode_echo + quest [+ flag]
      FINISH  : 22 00 0c 00 ...  -> opcode_echo + quest (never treat opcode as quest)
      bare    : FA 07 ...        -> quest only
    """
    raw = bytes(plain or b"")
    out = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "layout": "unknown",
        "quest_id": None,
        "flag": None,
        # SET_TRIGGER: S4A12 body after strip = quest + triggerType + isIncrement
        "trigger_type": None,
        "is_increment": None,
        "u16_0": None,
        "u16_1": None,
    }
    if len(raw) < 2:
        return out
    u0 = int.from_bytes(raw[0:2], "little")
    out["u16_0"] = u0
    if len(raw) >= 4:
        u1 = int.from_bytes(raw[2:4], "little")
        out["u16_1"] = u1
        if expected_opcode is not None and u0 == (int(expected_opcode) & 0xFFFF) and 0 < u1 < 0xFFFF:
            out["layout"] = "u16_opcode_echo+u16_quest"
            out["quest_id"] = u1
        elif u0 in {0x001F, 0x0020, 0x0021, 0x0022} and 0 < u1 < 0xFFFF:
            out["layout"] = "u16_opcode_echo+u16_quest"
            out["quest_id"] = u1
        else:
            out["layout"] = "u16_quest(+pad)"
            out["quest_id"] = u0
    else:
        out["layout"] = "u16_quest"
        out["quest_id"] = u0
    # flag / triggerType / isIncrement tails
    if out["layout"] == "u16_opcode_echo+u16_quest":
        if len(raw) >= 5:
            out["flag"] = int(raw[4]) & 0xFF
            out["trigger_type"] = int(raw[4]) & 0xFF
        if len(raw) >= 6:
            out["is_increment"] = int(raw[5]) != 0
    elif out["layout"].startswith("u16_quest"):
        if len(raw) >= 3:
            out["flag"] = int(raw[2]) & 0xFF
            out["trigger_type"] = int(raw[2]) & 0xFF
        if len(raw) >= 4:
            out["is_increment"] = int(raw[3]) != 0
    return out


def build_accept_quest_ack(
    *,
    success: bool,
    quest_id: int = 0,
    init_trigger: int = 0,
    error_code: int = 0x12,
    event_items: Sequence[tuple[int, int, int]] = (),
) -> bytes:
    if not success:
        return bytes([0x00, int(error_code) & 0xFF])
    body = bytearray()
    body.append(0x01)
    body += (int(quest_id) & 0xFFFF).to_bytes(2, "little")
    body += (int(init_trigger) & 0xFFFFFFFF).to_bytes(4, "little")
    items = list(event_items or ())
    body.append(len(items) & 0xFF)
    for slot, item_id, count in items:
        body += (int(slot) & 0xFFFF).to_bytes(2, "little")
        body += (int(item_id) & 0xFFFFFFFF).to_bytes(4, "little")
        body += (int(count) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(body)


def build_giveup_quest_ack(*, success: bool, quest_id: int = 0, error_code: int = 0x13) -> bytes:
    """Giveup fail default 0x13 (S4A12 QuestAckFormatSelfTest: missing quest)."""
    if not success:
        return bytes([0x00, int(error_code) & 0xFF])
    return bytes([0x01]) + (int(quest_id) & 0xFFFF).to_bytes(2, "little")


def build_set_quest_trigger_ack(
    *,
    success: bool,
    quest_id: int = 0,
    trigger_value: int = 0,
    error_code: int = 0x14,
) -> bytes:
    if not success:
        return bytes([0x00, int(error_code) & 0xFF])
    body = bytearray()
    body.append(0x01)
    body += (int(quest_id) & 0xFFFF).to_bytes(2, "little")
    body += (int(trigger_value) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(body)


# S4A12 ItemQuality.TopQualitySeed — 装备 insert 的 count/seed 字段
EQUIP_TOP_QUALITY_SEED = 999999998


# Stage3 S1/0x0022 真 handler = sub_162C740（mapA via sub_1AB3EB0）。
# 每条 insert 行 19B（比 S4A12 的 18B 多 1 字节 notify；90qiyuan 20B 不可照搬）。
# 历史参数名 ``gold`` 对应 reader 的第二个 u32，但 Stage3 随后把它作为
# event-10 quantity 分发；实际金币奖励由 item_id=0 的 insert 行承载。
FINISH_INSERT_ENTRY_WIRE_SIZE = 19


def build_finish_reward_insert_entries(
    *,
    gold: int = 0,
    granted_items: Sequence[tuple[int, int, int, int, bool]] = (),
    notify_flag: int = 0,
) -> list[tuple[int, int, int, int, int, int, int, int]]:
    """Stage3 交任务奖励动画 insert 列表（IDA sub_162C740 chainType=0）。

    顺序约定：
      1. 金币：slot=0, item_id=0, count=gold（飞币动画）
      2. 每个入包道具：stackable 用 count；equipment 用 TopQualitySeed + durability

    返回 (slot, item_id, count_or_seed, upgrade, dura, reserved, flags, notify)*
    notify = sub_162C740 末尾 u8（v120）；默认 0。
    """
    nf = int(notify_flag) & 0xFF
    out: list[tuple[int, int, int, int, int, int, int, int]] = []
    g = max(0, int(gold or 0)) & 0xFFFFFFFF
    if g > 0:
        out.append((0, 0, g, 0, 0, 0, 0, nf))
    for slot, item_id, count, dura, is_equipment in list(granted_items or ()):
        iid = int(item_id) & 0xFFFFFFFF
        if iid <= 0:
            continue
        if bool(is_equipment):
            seed = EQUIP_TOP_QUALITY_SEED
            d = max(0, int(dura or 0)) & 0xFFFF
            out.append((int(slot) & 0xFFFF, iid, seed, 0, d, 0, 0, nf))
        else:
            cnt = max(0, int(count or 0)) & 0xFFFFFFFF
            if cnt <= 0:
                continue
            out.append((int(slot) & 0xFFFF, iid, cnt, 0, 0, 0, 0, nf))
    return out


def build_finish_quest_ack(
    *,
    success: bool,
    quest_id: int = 0,
    error_code: int = 0x16,
    exp: int = 0,
    # ``gold`` remains a compatibility keyword for older fixtures.  The
    # Stage3 wire field is the completion-event quantity; reward gold belongs
    # in the item_id=0 insert row built above.
    gold: int | None = None,
    completion_event_quantity: int | None = None,
    completion_type: int = 0,
    chain_type: int = 0,
    grow_number: int = 0,
    slot_expansion_actor_key: int | None = None,
    insert_entries: Sequence[tuple] = (),
    consumed_entries: Sequence[tuple[int, int, int]] = (),
    # chain 1/2/20 内联赠技：两棵树各自 list[(level, skill_id, flag)]
    # Stage3 默认可空（count=0）；全量技能靠 S0/0013 替换
    profession_grants_tree0: Sequence[tuple[int, int, int]] = (),
    profession_grants_tree1: Sequence[tuple[int, int, int]] = (),
) -> bytes:
    """Stage3 S1/0x0022 finish body（IDA sub_162C740，2026-07-20）。

    Wire after success:
      u16 quest | u8 completion | u32 exp | u32 completion_event_quantity
      | u8 consumedCount + consumed*(u8 type,u16 slot,u32 count)
      | u8 chainType
      | chainType==0: u8 insertCount + insert*19B
        insert: u16 slot | u32 item | u32 countOrSeed | u8 up | u16 dura
                | u32 reserved | u8 flags | u8 notify
      | chainType∈{1,2,20}:
        u8 growNumber
        for tree in 0..1:
          u8 grantCount + grant*(u8 level | u16 skill_id | u8 flag)
        客户端随后 sub_1D07810 重建 actor + sub_2788D20(0,13) 转职动画。
      | chainType==21: u32 current actor runtime object key

    ``gold`` 是兼容既有调用方的历史参数名，线上的正确语义是
    ``completion_event_quantity``。成功交付一次任务通常传 1；奖励金币仍放在 insert 行。
    insert_entries 驱动多奖励动画；应填齐（金币 item_id=0 + 各道具）。
    兼容 7 元组（自动补 notify=0）与 8 元组。
    """
    if not success:
        return bytes([0x00, int(error_code) & 0xFF])
    body = bytearray()
    body.append(0x01)
    body += (int(quest_id) & 0xFFFF).to_bytes(2, "little")
    body.append(int(completion_type) & 0xFF)
    body += (int(exp) & 0xFFFFFFFF).to_bytes(4, "little")
    if completion_event_quantity is None:
        # Keep the old keyword working for external fixtures while making all
        # in-tree callers use the protocol semantic name.
        completion_event_quantity = 0 if gold is None else int(gold)
    elif gold is not None and int(gold) != int(completion_event_quantity):
        raise ValueError("gold and completion_event_quantity disagree")
    body += (int(completion_event_quantity) & 0xFFFFFFFF).to_bytes(4, "little")
    consumed = list(consumed_entries or ())
    body.append(len(consumed) & 0xFF)
    for update_type, slot, count in consumed:
        body.append(int(update_type) & 0xFF)
        body += (int(slot) & 0xFFFF).to_bytes(2, "little")
        body += (int(count) & 0xFFFFFFFF).to_bytes(4, "little")
    ct = int(chain_type) & 0xFF
    body.append(ct)
    if ct == 0:
        inserts = list(insert_entries or ())
        body.append(len(inserts) & 0xFF)
        for entry in inserts:
            slot = int(entry[0]) & 0xFFFF
            item_id = int(entry[1]) & 0xFFFFFFFF
            count = int(entry[2]) & 0xFFFFFFFF
            up = int(entry[3]) & 0xFF if len(entry) > 3 else 0
            dura = int(entry[4]) & 0xFFFF if len(entry) > 4 else 0
            reserved = int(entry[5]) & 0xFFFFFFFF if len(entry) > 5 else 0
            flags = int(entry[6]) & 0xFF if len(entry) > 6 else 0
            notify = int(entry[7]) & 0xFF if len(entry) > 7 else 0
            body += slot.to_bytes(2, "little")
            body += item_id.to_bytes(4, "little")
            body += count.to_bytes(4, "little")
            body.append(up)
            body += dura.to_bytes(2, "little")
            body += reserved.to_bytes(4, "little")
            body.append(flags)
            body.append(notify)  # Stage3 第 19 字节；缺则流错位闪退
    elif ct in (1, 2, 20):
        # Stage3: growNumber + 两棵树赠技计数（可 0；全量靠 0013）
        body.append(int(grow_number) & 0xFF)
        for grants in (profession_grants_tree0, profession_grants_tree1):
            rows = list(grants or ())
            if len(rows) > 0xFF:
                rows = rows[:0xFF]
            body.append(len(rows) & 0xFF)
            for row in rows:
                level = int(row[0]) & 0xFF
                skill_id = int(row[1]) & 0xFFFF
                flag = int(row[2]) & 0xFF if len(row) > 2 else 0
                body.append(level)
                body += skill_id.to_bytes(2, "little")
                body.append(flag)
    elif ct == 21:
        # sub_162C740 looks this key up in the runtime actor map, then dispatches
        # equipment UI effect mode 4 for that actor.
        if slot_expansion_actor_key is None:
            raise ValueError("chainType 21 requires slot_expansion_actor_key")
        body += (int(slot_expansion_actor_key) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(body)


def build_accepted_quest_list_023f(pairs: Sequence[tuple[int, int]] = ()) -> bytes:
    """S0/023F ACCEPTED_QUEST (IDA sub_164EE20 @ Stage3).

    Wire: u32 count + (u16 quest_id | u32 trigger) * count
    Reader: sub_2C74B00 (u32) then loop sub_2C74A80(u16)+sub_2C74B00(u32).
    """
    rows = [
        (int(qid) & 0xFFFF, int(trig) & 0xFFFFFFFF)
        for qid, trig in (pairs or ())
        if not is_client_hidden_quest_id(qid)
    ]
    body = bytearray(struct.pack("<I", len(rows)))
    for qid, trig in rows:
        body += struct.pack("<HI", qid, trig)
    return bytes(body)


def build_quest_trigger_list_0123(pairs: Sequence[tuple[int, int]] = ()) -> bytes:
    """S0/0123 QUEST_TRIGGER_LIST (IDA sub_164DD20 @ Stage3).

    Wire: u16 count + (u16 quest_id | u32 trigger) * count
    注意：count 是 u16，与 023F 的 u32 count 不同（文档旧写法“同 023F”已否）。
    Reader: sub_2C74A80 (u16 count) then same pair loop as 023F.
    """
    rows = [
        (int(qid) & 0xFFFF, int(trig) & 0xFFFFFFFF)
        for qid, trig in (pairs or ())
        if not is_client_hidden_quest_id(qid)
    ]
    if len(rows) > 0xFFFF:
        rows = rows[:0xFFFF]
    body = bytearray(struct.pack("<H", len(rows)))
    for qid, trig in rows:
        body += struct.pack("<HI", qid, trig)
    return bytes(body)


def build_clear_quest_list_0164(finished_quest_ids: Sequence[int] = ()) -> bytes:
    """S0/0164 CLEAR_QUEST (IDA sub_164EA10 -> sub_2BF4BC0).

    Wire: u32 payload_len + raw bytes[payload_len]; index=quest_id, value=1 finished.
    Empty list -> payload_len=0 (live-safe; START_MAP 默认仅 non-empty 推送).
    """
    ids = [
        int(q) & 0xFFFF
        for q in (finished_quest_ids or ())
        if int(q) > 0 and not is_client_hidden_quest_id(q)
    ]
    if not ids:
        return struct.pack("<I", 0)
    payload_len = max(ids) + 1
    if payload_len > 30000:
        payload_len = 30000
        ids = [q for q in ids if q < payload_len]
    blob = bytearray(payload_len)
    for qid in ids:
        if 0 <= qid < payload_len:
            blob[qid] = 1
    return struct.pack("<I", payload_len) + bytes(blob)


def filter_acceptable_quest_ids(
    candidate_ids: Sequence[int],
    *,
    active_ids: Iterable[int] = (),
    finished_ids: Iterable[int] = (),
    registered_ids: Iterable[int] | None = None,
) -> list[int]:
    active = {int(x) & 0xFFFF for x in active_ids}
    finished = {int(x) & 0xFFFF for x in finished_ids}
    registered = (
        None
        if registered_ids is None
        else {int(x) & 0xFFFF for x in registered_ids if int(x) > 0}
    )
    out: list[int] = []
    seen: set[int] = set()
    for qid in candidate_ids:
        q = int(qid) & 0xFFFF
        if (
            q <= 0
            or is_client_hidden_quest_id(q)
            or (registered is not None and q not in registered)
            or q in seen
            or q in active
            or q in finished
        ):
            continue
        seen.add(q)
        out.append(q)
    return out


def active_pairs_from_rows(
    rows: Sequence[tuple[int, int, int]] | Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Normalize DB rows to (quest_id, trigger)."""
    pairs: list[tuple[int, int]] = []
    for row in rows or ():
        if len(row) >= 3:
            pairs.append((int(row[1]) & 0xFFFF, int(row[2]) & 0xFFFFFFFF))
        elif len(row) >= 2:
            pairs.append((int(row[0]) & 0xFFFF, int(row[1]) & 0xFFFFFFFF))
    return pairs

