# -*- coding: utf-8 -*-
"""C1/0x0012 ENUM_CMDPACKET_DELETE_ITEM — 背包删除/丢弃（弹窗确认）。

PacketEnum:
  ENUM_ALT_0012 = 18  # 删除 物品
  （主表 CMD 18 位空缺；Stage3 直播 C1/0x0012）

Stage3 直播证据（2026-07-19 08:14 / 08:19）:
  - codec_index=4 AES seed0
  - plain（reserved+body 解密）::

      u32 body_len                 # 后续有效字节数（不含尾 0 填充）
      protobuf-like:
        field2 varint              # 观测 = 0（保留）
        field3 length-delimited:   # 删除条目
          field1 varint            # 观测 = 1/2（确认/动作，非 list_type）
          field2 varint            # slot_index
          field3 varint            # item_template_id
          field4 varint            # stack_count

    例：slot=122 item=10149508 count=2 对齐 DB list0 背包。

  - 与 C1/0x002F DROP_ITEM 分流：
    * 0012 = 背包 UI 丢弃弹窗（可无坐标）
    * 002F = 场景坐标丢弃（含 pos_x/pos_y）

丢弃策略（2026-07-26）:
  - 主动丢弃统一扣包销毁；副本内不再创建可拾取地面物。

回包（Stage3 IDA 2026-07-19 09:28 定案）:
  - 消耗品弹窗走本 opcode；装备场景丢弃走 002F
  - C2S 仍是 ``u32 + protobuf DELETE_REQUEST``（直播已对齐）
  - S2C 注册表 ``sub_1630830`` 对 opcode=18 **写两次**：
      1) ``sub_161D890`` protobuf RETURN（先写）
      2) ``sub_160F1E0`` 仅 UI prompt（后写，**覆盖**）
    活跃 handler = ``sub_160F1E0(a1, a2, n36)``：
      - 成功 a2!=0：只弹 string 19368，**不读 body**
      - 失败 a2==0：按 n36=34/36 弹错
    因此 S1/0012 body 只能是 class1 结果字节，多出来的固定/PB 体
    无人消费 → 立刻 C1/00D9 OVERFLOW（09:11/09:23 直播均中招）
  - 90qiyuan/s4a12 的 ``01|list|01|slot|count|pad`` 适配其 EXE 的消费 handler，
    **不能**照搬到 Stage3（本客户端最后注册的是 prompt-only）
  - **现行默认**：
      1) S1/0012 = ``01`` 成功 / ``00 <err>`` 失败
      2) 成功后 S0/000D 全量 Main 刷新（对齐 002F/0013 已验证清 UI）；
         唯一例外是副本内账号小晶体扣减，使用 S0/000E 单格同步，避免
         重建本地 Main/快捷栏对象
      3) 0028/000E 默认关
"""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail

_payloads = load_chain("helper-dungeon_payloads.py")
build_drop_item_slot_update_0028 = _payloads.build_drop_item_slot_update_0028
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e

_tower_inventory = load_chain("helper-death_tower_inventory.py")

_cube_soul = load_chain("helper-cube_soul_slots.py")
is_cube_fragment = _cube_soul.is_cube_fragment
cube_fragment_slot = _cube_soul.cube_fragment_slot
is_account_soul = _cube_soul.is_account_soul
account_soul_slot = _cube_soul.account_soul_slot


def _write_varint(value: int) -> bytes:
    """写 protobuf 风格 varint（无符号）。"""
    v = int(value) & 0xFFFFFFFF
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _write_pb_int32(field: int, value: int) -> bytes:
    """protobuf wire0：tag=(field<<3)|0 + varint。"""
    return _write_varint((int(field) << 3) | 0) + _write_varint(int(value))


def _write_pb_message(field: int, payload: bytes) -> bytes:
    """protobuf wire2：tag=(field<<3)|2 + len + bytes。"""
    blob = bytes(payload or b"")
    return _write_varint((int(field) << 3) | 2) + _write_varint(len(blob)) + blob


def _read_varint(buf: bytes, off: int) -> tuple[int, int]:
    """读 protobuf 风格 varint，返回 (value, new_offset)。"""
    result = 0
    shift = 0
    i = int(off)
    n = len(buf)
    while i < n:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, i
        shift += 7
        if shift > 63:
            break
    return result, i


def _parse_protobuf_fields(buf: bytes) -> dict[int, list[Any]]:
    """极简 protobuf 字段扫描：wire0=varint，wire2=length-delimited bytes。"""
    out: dict[int, list[Any]] = {}
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] == 0 and all(x == 0 for x in buf[i:]):
            break
        key, i2 = _read_varint(buf, i)
        if i2 <= i:
            break
        i = i2
        field = key >> 3
        wire = key & 7
        if field <= 0:
            break
        if wire == 0:
            val, i = _read_varint(buf, i)
            out.setdefault(field, []).append(int(val))
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            chunk = buf[i : i + int(ln)]
            i += int(ln)
            out.setdefault(field, []).append(bytes(chunk))
        elif wire == 5:
            if i + 4 > n:
                break
            out.setdefault(field, []).append(struct.unpack_from("<I", buf, i)[0])
            i += 4
        elif wire == 1:
            if i + 8 > n:
                break
            out.setdefault(field, []).append(buf[i : i + 8])
            i += 8
        else:
            break
    return out


def parse_delete_item_body(plain: bytes) -> dict[str, Any]:
    """解析 C1/0012。优先直播 protobuf 帧，退化固定布局。"""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" ") if len(raw) <= 48 else raw[:48].hex(" ") + "...",
        "layout": "unknown",
        "list_type": 0,
        "slot_index": None,
        "item_template_id": 0,
        "stack_count": 1,
        "action": 0,
    }
    if len(raw) < 4:
        return out

    body_len = struct.unpack_from("<I", raw, 0)[0]
    if 0 < body_len <= len(raw) - 4:
        body = raw[4 : 4 + body_len]
    else:
        body = raw[4:] if len(raw) > 4 else raw

    # 主路径：u32 len + protobuf
    fields = _parse_protobuf_fields(body)
    # 外层 field3 = 嵌套删除条目
    nested_blob = None
    for v in fields.get(3, []):
        if isinstance(v, (bytes, bytearray)) and v:
            nested_blob = bytes(v)
            break
    if nested_blob is None and fields:
        # 有时无外层包装，body 本身即条目
        nested_blob = body
        nested_fields = fields
    else:
        nested_fields = _parse_protobuf_fields(nested_blob) if nested_blob else {}

    if nested_fields:
        action = int((nested_fields.get(1) or [0])[0] or 0)
        slot = int((nested_fields.get(2) or [0])[0] or 0)
        item_id = int((nested_fields.get(3) or [0])[0] or 0)
        count = int((nested_fields.get(4) or [1])[0] or 1)
        # 直播：field1=1 是确认位，list 默认 Main(0)。
        # 若 field1 像 list_type（0..16）且 slot 很大，仍优先当 action。
        list_type = 0
        if 0 <= action <= 16 and slot == 0 and item_id == 0:
            list_type = action
        if slot > 0 or item_id > 0:
            out.update(
                {
                    "layout": "pb_u32len_entry",
                    "list_type": int(list_type) & 0xFF,
                    "slot_index": int(slot) & 0xFFFF,
                    "item_template_id": int(item_id) & 0xFFFFFFFF,
                    "stack_count": max(1, int(count)),
                    "action": int(action),
                }
            )
            return out

    # 退化：u8 list + u16 slot + u32 item + u32 count
    if len(raw) >= 11:
        list_type = raw[0]
        slot = struct.unpack_from("<H", raw, 1)[0]
        item_id = struct.unpack_from("<I", raw, 3)[0]
        count = struct.unpack_from("<I", raw, 7)[0]
        if 0 <= list_type <= 16 and 0 <= slot <= 0x300 and count > 0:
            out.update(
                {
                    "layout": "list_slot_item_count",
                    "list_type": int(list_type),
                    "slot_index": int(slot),
                    "item_template_id": int(item_id) & 0xFFFFFFFF,
                    "stack_count": int(count) & 0xFFFFFFFF,
                }
            )
            return out

    # 再退化：u16 slot + u32 count
    if len(raw) >= 6:
        slot = struct.unpack_from("<H", raw, 0)[0]
        count = struct.unpack_from("<I", raw, 2)[0]
        if 0 < slot <= 0x300 and count > 0:
            out.update(
                {
                    "layout": "slot_count6",
                    "slot_index": int(slot),
                    "stack_count": int(count) & 0xFFFFFFFF,
                }
            )
    return out


def build_delete_item_ack_0012(
    *,
    ok: bool,
    list_type: int = 0,
    slot_index: int = 0,
    applied_count: int = 0,
    item_template_id: int = 0,
    error_code: int = 4,
) -> bytes:
    """S1/0x0012 删除 ACK — Stage3 活跃 handler ``sub_160F1E0`` 形态。

    活跃路径（``sub_1630830`` 后写覆盖 protobuf ``sub_161D890``）::

        成功: u8 0x01          # a2=true；handler 只弹 toast，不读后续
        失败: u8 0x00 + u8 err # a2=false；n36=err（34/36 有专属文案）

    槽位/数量/道具 id **不要**写进本 ACK：写了无人消费会 00D9。
    背包 UI 清图标靠后续 S0/000D（与 002F DROP 同策略）。

    参数 list_type/slot/count/item 保留签名兼容，当前不编码进 body。
    """
    del list_type, slot_index, applied_count, item_template_id  # 不进 wire
    if ok:
        return bytes([0x01])
    return bytes([0x00, int(error_code) & 0xFF])


def build_delete_item_completion_0012() -> bytes:
    """S0/0x0012 库存动作完成通知：空 ``u16 entry_count``。

    Stage3 ``S2C_handler_opcode18_sub_16597B0`` 先读该计数；无条目时仍会
    清空 ``dword_4189AEC`` 的库存等待态。账号小晶体在副本中是虚拟 Main
    槽，S1 ACK 与 S0/000E 只更新提示和格子，不能完成这一动作队列。
    """
    return struct.pack("<H", 0)


def _cid(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            val = getattr(session, attr, None)
            if val is not None:
                try:
                    cid = int(val)
                    if cid > 0:
                        return cid
                except Exception:
                    pass
    try:
        detail = resolve_selected_character_detail(server, session)
        cid = detail.get("character_id")
        if cid is not None and int(cid) > 0:
            return int(cid)
    except Exception:
        pass
    return None


def _in_dungeon_run(session: Any) -> bool:
    """仅认显式 in_dungeon_run。禁止用残留 cur_dungeon_id 误判城镇为地下城。"""
    if session is None:
        return False
    return bool(getattr(session, "in_dungeon_run", False))


class SimpleInventoryItem:
    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
    )

    def __init__(
        self,
        slot_index: int,
        item_template_id: int,
        stack_count: int,
        durability: int = 0,
        *,
        instance_value: int = 0,
        item_kind: str = "",
        seal_flag: int = 0,
    ) -> None:
        self.slot_index = int(slot_index)
        self.item_template_id = int(item_template_id)
        self.stack_count = int(stack_count)
        self.durability = max(0, int(durability or 0)) & 0xFFFF
        self.instance_value = max(0, int(instance_value or 0)) & 0xFFFFFFFF
        self.item_kind = str(item_kind or "")
        self.seal_flag = max(0, int(seal_flag or 0)) & 0xFF


class DeleteItem0012Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle_delete_item_0012(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_DELETE_ITEM_0012", True):
            return [], "C1/0012 DELETE_ITEM gate=OFF"

        parsed = parse_delete_item_body(body)
        base = (
            "C1/0012 DELETE_ITEM layout=%s list=%s slot=%s item=%s count=%s action=%s plain_len=%s"
            % (
                parsed.get("layout"),
                parsed.get("list_type"),
                parsed.get("slot_index"),
                parsed.get("item_template_id"),
                parsed.get("stack_count"),
                parsed.get("action"),
                parsed.get("raw_len"),
            )
        )
        slot = parsed.get("slot_index")
        packets: list[bytes] = []
        notes: list[str] = []

        if slot is None:
            ack = build_delete_item_ack_0012(
                ok=False, list_type=0, slot_index=0, applied_count=0
            )
            pkt, mode = self._emit(1, 0x0012, ack, "codec")
            return [pkt], base + "; skip=no_slot; ACK_fail/%s pb_len=%d" % (
                mode,
                len(ack),
            )

        cid = _cid(session, self.server)
        if cid is None:
            ack = build_delete_item_ack_0012(
                ok=False,
                list_type=int(parsed.get("list_type") or 0),
                slot_index=int(slot),
                applied_count=0,
                item_template_id=int(parsed.get("item_template_id") or 0),
            )
            pkt, mode = self._emit(1, 0x0012, ack, "codec")
            return [pkt], base + "; skip=no_cid; ACK_fail/%s" % mode

        list_type = int(parsed.get("list_type") or 0) & 0xFF
        count = max(1, int(parsed.get("stack_count") or 1))
        expect_tid = int(parsed.get("item_template_id") or 0) & 0xFFFFFFFF
        if _tower_inventory.request_targets_death_tower_material(
            session,
            slot_index=int(slot),
            expected_item_id=expect_tid,
        ):
            ok, consume_note, detail = (
                _tower_inventory.consume_death_tower_material(
                    session,
                    slot_index=int(slot),
                    expected_item_id=expect_tid,
                    count=count,
                )
            )
            actual_item = int(detail.get("item_template_id", expect_tid) or 0)
            removed = int(detail.get("removed", 0) or 0)
            remaining = int(detail.get("remaining", 0) or 0)
            ack = build_delete_item_ack_0012(
                ok=bool(ok and removed > 0),
                list_type=_tower_inventory.DEATH_TOWER_LIST_TYPE,
                slot_index=int(slot),
                applied_count=removed,
                item_template_id=actual_item,
            )
            pkt_ack, mode_ack = self._emit(1, 0x0012, ack, "codec")
            packets.append(pkt_ack)
            if not ok or removed <= 0:
                return packets, (
                    "%s; reject=death_tower_material:%s; ACK_fail/%s"
                    % (base, consume_note, mode_ack)
                )

            update_entries = _tower_inventory.build_death_tower_update_entries(
                session,
                detail["changed_slots"],
            )
            body_e = build_item_list_update_000e(
                update_entries,
                list_type=_tower_inventory.DEATH_TOWER_LIST_TYPE,
            )
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            body_done = build_delete_item_completion_0012()
            pkt_done, mode_done = self._emit(0, 0x0012, body_done, "codec")
            packets.append(pkt_done)
            return packets, (
                "%s; OK death_tower_material item=%d removed=%d remain=%d; "
                "ACK0012/%s; 000E/%s list=0x%02X; "
                "0012_COMPLETE/%s entry_count=0; %s; "
                "chain_file=opcodes/opcode0012-delete_item.py"
                % (
                    base,
                    actual_item,
                    removed,
                    remaining,
                    mode_ack,
                    mode_e,
                    _tower_inventory.DEATH_TOWER_LIST_TYPE,
                    mode_done,
                    consume_note,
                )
            )

        repo = self.server._repo()
        is_account_cube = bool(expect_tid > 0 and is_cube_fragment(expect_tid))
        is_account_soul_item = bool(expect_tid > 0 and is_account_soul(expect_tid))
        account_slot = (
            cube_fragment_slot(expect_tid)
            if is_account_cube
            else account_soul_slot(expect_tid) if is_account_soul_item else -1
        )
        peek: dict[str, Any] | None = None
        peek_tid = 0
        peek_dura = 0

        if is_account_cube or is_account_soul_item:
            # 354..365 are virtual Main slots backed by account ledgers. A
            # character_items lookup must not turn this valid client consume
            # request into slot_empty.
            if list_type != 0 or int(slot) != int(account_slot):
                ack = build_delete_item_ack_0012(
                    ok=False,
                    list_type=list_type,
                    slot_index=int(slot),
                    applied_count=0,
                    item_template_id=expect_tid,
                )
                pkt, mode = self._emit(1, 0x0012, ack, "codec")
                return [pkt], (
                    "%s; reject=%s_slot_mismatch expect_slot=%s; ACK_fail/%s"
                    % (
                        base,
                        "cube" if is_account_cube else "soul",
                        account_slot,
                        mode,
                    )
                )
            aid = repo.get_account_id_for_character(int(cid))
            if aid is None:
                ok, note, detail = False, "account_material_no_account", {
                    "item_template_id": expect_tid,
                    "removed": 0,
                    "remaining": 0,
                    "durability": 0,
                    "seal_flag": 0,
                }
            elif is_account_cube:
                ok, note, detail = repo.consume_cube_fragment(
                    int(aid), expect_tid, count
                )
            else:
                ok, note, soul_slot, remaining = repo.consume_account_soul(
                    int(aid), expect_tid, count
                )
                detail = {
                    "item_template_id": expect_tid,
                    "removed": count if ok else 0,
                    "remaining": remaining,
                    "durability": 0,
                    "item_kind": "stackable",
                    "seal_flag": 0,
                    "list_type": 0,
                    "slot_index": soul_slot,
                }
        else:
            peek = repo.get_inventory_item_at(
                int(cid), list_type=list_type, slot_index=int(slot)
            )
            # 客户端 list 可能省略；若 Main 空则不盲扫其它栏，避免误删时装
            if peek is None and list_type == 0:
                pass
            peek_tid = int((peek or {}).get("item_template_id") or 0)
            peek_dura = int((peek or {}).get("durability") or 0)

            # 客户端带了 item_id 时做一致性校验（防错槽）
            if expect_tid > 0 and peek_tid > 0 and expect_tid != peek_tid:
                ack = build_delete_item_ack_0012(
                    ok=False,
                    list_type=list_type,
                    slot_index=int(slot),
                    applied_count=0,
                    item_template_id=expect_tid,
                )
                pkt, mode = self._emit(1, 0x0012, ack, "codec")
                return [pkt], (
                    "%s; reject=item_mismatch expect=%s bag=%s; ACK_fail/%s"
                    % (base, expect_tid, peek_tid, mode)
                )

            ok, note, detail = repo.consume_inventory_item(
                int(cid),
                list_type=list_type,
                slot_index=int(slot),
                count=count,
                reject_locked=True,
            )
        remain = int(detail.get("remaining") or 0)
        item_id = int(detail.get("item_template_id") or peek_tid or expect_tid or 0)
        dura = int(detail.get("durability") or peek_dura or 0)
        removed = int(detail.get("removed") or 0)

        # ACK 始终先回，避免弹窗挂死；体只能是 class1 结果字节（见 build 注释）
        ack = build_delete_item_ack_0012(
            ok=bool(ok and removed > 0),
            list_type=list_type,
            slot_index=int(slot),
            applied_count=removed if ok else 0,
            item_template_id=item_id,
        )
        pkt_ack, mode_ack = self._emit(1, 0x0012, ack, "codec")
        packets.append(pkt_ack)
        notes.append(
            "ACK0012/%s ok=%s removed=%s plain=%s"
            % (mode_ack, ok, removed, ack.hex(" "))
        )

        destroy_reason = "n/a"
        if is_account_cube and ok and removed > 0:
            destroy_reason = "consume_account_cube slot=%s" % account_slot
        elif is_account_soul_item and ok and removed > 0:
            destroy_reason = "consume_account_soul slot=%s" % account_slot
        elif ok and item_id > 0 and removed > 0:
            destroy_reason = "destroy manual_discard"

        # 活跃 S1/0012 handler 不改背包 UI；成功后必须 flag0 刷新（对齐 002F）。
        # In a run, settle the main inventory action with a slot update and
        # the native completion notification instead of rebuilding the bag.
        main_discard_in_run = bool(list_type == 0 and ok and removed > 0 and _in_dungeon_run(session))
        if main_discard_in_run:
            if remain > 0 and item_id > 0:
                body_e = build_item_list_update_000e(
                    [(int(slot) & 0xFFFF, item_id, remain, dura)], list_type=0
                )
            else:
                body_e = build_item_list_clear_slot_000e(int(slot), list_type=0)
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            notes.append(
                "000E/%s discard_in_run list=0 slot=%s remain=%s"
                % (
                    mode_e,
                    int(slot),
                    remain,
                )
            )
            # Stage3 的 S0/0012 reader 即使 entry_count=0 也会收束库存
            # 动作队列。缺这条完成通知时，扣晶体后立刻离图会把忙状态带到
            # 下一副本，随后普通消耗品在客户端门禁处拦下，C1/002C 不出线。
            body_done = build_delete_item_completion_0012()
            pkt_done, mode_done = self._emit(0, 0x0012, body_done, "codec")
            packets.append(pkt_done)
            notes.append(
                "0012_COMPLETE/%s entry_count=0 clear_inventory_wait" % mode_done
            )

        if env_bool("USDOF_GAME_DELETE_ITEM_SEND_0028", False):
            if remain > 0 and item_id > 0:
                body28 = build_drop_item_slot_update_0028(
                    int(slot),
                    item_template_id=item_id,
                    stack_count=remain,
                    endurance=dura,
                )
            else:
                body28 = build_drop_item_slot_update_0028(
                    int(slot),
                    item_template_id=0,
                    stack_count=0,
                )
            pkt28, mode28 = self._emit(0, 0x0028, body28, "codec")
            packets.append(pkt28)
            notes.append("0028/%s/len=%d remain=%s" % (mode28, len(body28), remain))

        if (
            not main_discard_in_run
            and env_bool("USDOF_GAME_DELETE_ITEM_SEND_000E", False)
        ):
            if remain > 0 and item_id > 0:
                rows = [(int(slot) & 0xFFFF, item_id, remain, dura)]
            else:
                rows = [(int(slot) & 0xFFFF, 0, 0, 0)]
            body_e = build_item_list_update_000e(rows, list_type=list_type)
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            notes.append("000E/%s list=%s" % (mode_e, list_type))

        # 默认开：S0/000D 全量 Main 刷新清图标（002F/0013 已验证）
        if (
            ok
            and removed > 0
            and list_type == 0
            and not main_discard_in_run
            and env_bool("USDOF_GAME_DELETE_ITEM_SEND_000D", True)
        ):
            try:
                inv_rows = repo.list_inventory_items(int(cid), list_type=0)
                items = [InventoryItemView(row) for row in inv_rows]
                list_param16 = int(repo.get_container_list_param(int(cid), list_type=0))
                body_d = build_item_list_mode0(
                    items, list_type=0, list_param16=list_param16
                )
                pkt_d, mode_d = self._emit(0, 0x000D, body_d, "codec")
                packets.append(pkt_d)
                notes.append("000D/%s entries=%d" % (mode_d, len(items)))
            except Exception as exc:
                notes.append("000D_err=%r" % (exc,))

        if ok and item_id > 0:
            notes.append(destroy_reason)
        else:
            notes.append("destroy_skip %s note=%s" % (destroy_reason, note))

        status = "OK" if ok and removed > 0 else "FAIL"
        return packets, (
            "%s; %s mode=%s; %s; %s; consume=%s; chain_file=opcodes/opcode0012-delete_item.py"
            % (
                base,
                status,
                "destroy",
                destroy_reason if ok and item_id > 0 else "-",
                ", ".join(notes),
                note,
            )
        )
