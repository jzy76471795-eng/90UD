# -*- coding: utf-8 -*-
"""C1/0x002F ENUM_CMDPACKET_DROP_ITEM — 丢弃背包道具。

PacketEnum:
  CMD  DROP_ITEM = 47 (0x2F)
  NOTI DROP_ITEM = 40 (0x28)

Stage3 证据（2026-07-19）:
  - 直播包 codec5 解密 plain::

      u16 pos_x
      u16 pos_y
      u8  list_type
      u16 slot_index
      u32 stack_count
      （pad 至 codec 对齐）

    例：``5d 01 f9 00 00 0e 00 01 ...`` → x=349 y=249 list=0 slot=14 count=1
    与当时背包 list0 slot14=装备 29904 对齐。

  - S0/0x0028 读者 ``sub_1634FD0``（``sub_1AB3F40(40, ...)``）::

      u8 count
      count × (u8 index + u32 size + size bytes)

丢弃策略（2026-07-26）:
  - 主动丢弃统一扣包销毁；副本内不再尝试创建地面恢复物。

回包（2026-07-19 复测）:
  - 直播：扣包 OK 但客户端背包 UI 仍显示 → 仅 0028/000E 不足以清格
  - 对照 0013 MOVE：必须补 **S0/000D 全量 Main 刷新**（已验证路径）
  - 默认：S0/0028（DROP_ITEM NOTI）+ **S0/000D**
  - 主动丢弃不发送 S0/0026，避免将活跃角色伪装为死亡实体。
  - 000E 空条默认关
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


class SimpleInventoryItem:
    """000D 条目最小结构（与 0013/0012 同形）。"""

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


def _parse_drop_item_body(plain: bytes) -> dict[str, Any]:
    """解析 C1/002F。优先 11B：u16 x, u16 y, u8 list, u16 slot, u32 count。"""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" ") if len(raw) <= 32 else raw[:32].hex(" ") + "...",
        "layout": "unknown",
        "pos_x": 0,
        "pos_y": 0,
        "list_type": 0,
        "slot_index": None,
        "stack_count": 1,
    }
    views = [raw]
    if len(raw) >= 5:
        views.append(raw[3:])  # 偶发 3B codec 前缀

    for view in views:
        # 主布局 11B+
        if len(view) >= 11:
            pos_x, pos_y = struct.unpack_from("<HH", view, 0)
            list_type = view[4]
            slot = struct.unpack_from("<H", view, 5)[0]
            count = struct.unpack_from("<I", view, 7)[0]
            # 槽位合理范围 + count>0
            if 0 <= int(slot) <= 0x300 and int(count) > 0 and int(list_type) <= 16:
                out.update(
                    {
                        "layout": "xy_list_slot_count11",
                        "pos_x": int(pos_x) & 0xFFFF,
                        "pos_y": int(pos_y) & 0xFFFF,
                        "list_type": int(list_type) & 0xFF,
                        "slot_index": int(slot),
                        "stack_count": int(count) & 0xFFFFFFFF,
                    }
                )
                return out
        # 退化：u8 list + u16 slot + u32 count
        if len(view) >= 7:
            list_type = view[0]
            slot = struct.unpack_from("<H", view, 1)[0]
            count = struct.unpack_from("<I", view, 3)[0]
            if 0 <= int(slot) <= 0x300 and int(count) > 0 and int(list_type) <= 16:
                out.update(
                    {
                        "layout": "list_slot_count7",
                        "list_type": int(list_type) & 0xFF,
                        "slot_index": int(slot),
                        "stack_count": int(count) & 0xFFFFFFFF,
                    }
                )
                return out
        # 再退化：u16 slot + u32 count
        if len(view) >= 6:
            slot = struct.unpack_from("<H", view, 0)[0]
            count = struct.unpack_from("<I", view, 2)[0]
            if 0 < int(slot) <= 0x300 and int(count) > 0:
                out.update(
                    {
                        "layout": "slot_count6",
                        "list_type": 0,
                        "slot_index": int(slot),
                        "stack_count": int(count) & 0xFFFFFFFF,
                    }
                )
                return out
    return out


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


class DropItem002FService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle_drop_item_002f(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_DROP_ITEM_002F", True):
            return [], "C1/002F DROP_ITEM gate=OFF"

        parsed = _parse_drop_item_body(body)
        base = (
            "C1/002F DROP_ITEM layout=%s list=%s slot=%s count=%s pos=(%s,%s) plain_len=%s"
            % (
                parsed.get("layout"),
                parsed.get("list_type"),
                parsed.get("slot_index"),
                parsed.get("stack_count"),
                parsed.get("pos_x"),
                parsed.get("pos_y"),
                parsed.get("raw_len"),
            )
        )
        slot = parsed.get("slot_index")
        if slot is None:
            # 仍回空 0028，避免客户端永久等待
            body28 = bytes([0])
            pkt, mode = self._emit(0, 0x0028, body28, "codec")
            return [pkt], base + "; skip=no_slot; 0028_empty/%s" % mode

        cid = _cid(session, self.server)
        if cid is None:
            body28 = bytes([0])
            pkt, mode = self._emit(0, 0x0028, body28, "codec")
            return [pkt], base + "; skip=no_cid; 0028_empty/%s" % mode

        list_type = int(parsed.get("list_type") or 0) & 0xFF
        count = max(1, int(parsed.get("stack_count") or 1))
        pos_x = int(parsed.get("pos_x") or 0) & 0xFFFF
        pos_y = int(parsed.get("pos_y") or 0) & 0xFFFF
        repo = self.server._repo()

        # 先读取当前物品条目，再执行扣除。
        peek = repo.get_inventory_item_at(
            int(cid), list_type=list_type, slot_index=int(slot)
        )
        peek_tid = int((peek or {}).get("item_template_id") or 0)
        peek_dura = int((peek or {}).get("durability") or 0)

        ok, note, detail = repo.consume_inventory_item(
            int(cid),
            list_type=list_type,
            slot_index=int(slot),
            count=count,
            reject_locked=True,
        )

        packets: list[bytes] = []
        notes: list[str] = []

        remain = int(detail.get("remaining") or 0)
        item_id = int(detail.get("item_template_id") or peek_tid or 0)
        dura = int(detail.get("durability") or peek_dura or 0)
        removed = int(detail.get("removed") or 0)

        destroy_reason = "n/a"
        if ok and item_id > 0 and removed > 0:
            destroy_reason = "destroy manual_discard"

        if remain > 0 and item_id > 0:
            body28 = build_drop_item_slot_update_0028(
                int(slot),
                item_template_id=item_id,
                stack_count=remain,
                endurance=dura,
            )
        else:
            # 清空：108B 空条（slot 保留，item_id=0 count=0）
            body28 = build_drop_item_slot_update_0028(
                int(slot),
                item_template_id=0,
                stack_count=0,
            )
        pkt28, mode28 = self._emit(0, 0x0028, body28, "codec")
        packets.append(pkt28)
        notes.append(
            "0028/%s/len=%d slot=%s remain=%s ok=%s %s"
            % (mode28, len(body28), slot, remain, ok, note)
        )

        main_discard_in_run = bool(
            list_type == 0 and ok and removed > 0 and getattr(session, "in_dungeon_run", False)
        )
        # In a run, refresh only the affected slot and release the shared wait.
        if main_discard_in_run or env_bool("USDOF_GAME_DROP_ITEM_SEND_000E", False):
            if remain > 0 and item_id > 0:
                body_e = build_item_list_update_000e(
                    [(int(slot) & 0xFFFF, item_id, remain, dura)], list_type=list_type,
                )
            else:
                body_e = build_item_list_clear_slot_000e(int(slot), list_type=list_type)
            pkt_e, mode_e = self._emit(0, 0x000E, body_e, "codec")
            packets.append(pkt_e)
            notes.append("000E/%s list=%s slot=%s" % (mode_e, list_type, slot))
        if main_discard_in_run:
            delete_item = load_chain("opcode0012-delete_item.py")
            pkt_done, mode_done = self._emit(0, 0x0012, delete_item.build_delete_item_completion_0012(), "codec")
            packets.append(pkt_done)
            notes.append("0012_COMPLETE/%s clear_inventory_wait" % mode_done)

        # S0/000D 全量 Main 刷新 — 0013 已验证；0028 单独无法清背包 UI
        if (
            ok
            and list_type == 0
            and not main_discard_in_run
            and env_bool("USDOF_GAME_DROP_ITEM_SEND_000D", True)
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
            notes.append("destroy_skip %s" % destroy_reason)

        status = "OK" if ok else "FAIL"
        return packets, "%s; %s mode=%s; %s; %s; chain_file=opcodes/opcode002f-drop_item.py" % (
            base,
            status,
            "destroy",
            destroy_reason if ok and item_id > 0 else "-",
            ", ".join(notes),
        )
