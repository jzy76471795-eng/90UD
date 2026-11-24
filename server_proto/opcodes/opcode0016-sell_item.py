# -*- coding: utf-8 -*-
"""C1/0x0016 ENUM_CMDPACKET_SELL_ITEM — NPC 商店出售。

注意双表：
  - C->S flag1/0x0016 = SELL_ITEM（本文件）
  - S->C flag0/0x0016 = USER_POSITION（``opcode0016-user_position.py``）

Stage3 直播（2026-07-19 11:06 / 11:07）:
  - codec_index=8 XTEA，payload=0x0d
  - plain 前缀::

      00 09 00 01 00 ...   # list=0 slot=9 count=1
      00 09 00 01 00 00 00 02 00 00 00 15 00

  与 S4A12 ``TryParseDeleteOrSellRequest`` 一致：
      u8 list_type | i16 slot | i16 sell_count

S4A12 ACK（SellItemBuilder）:
  成功: u8 01 | i32 updatedGold | u8 list | i16 slot | i16 appliedCount
  失败: u8 00 | u8 error_code

本服：扣包 + 钱包加价 + S1/0016 ACK + S0/000D 刷新 Main（清图标）。
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_mode0
from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail

_payloads = load_chain("helper-dungeon_payloads.py")
clamp_wallet_gold_for_client = _payloads.clamp_wallet_gold_for_client

_cube_soul = load_chain("helper-cube_soul_slots.py")
is_cube_fragment = _cube_soul.is_cube_fragment
is_account_soul = _cube_soul.is_account_soul
is_cube_fragment_slot = _cube_soul.is_cube_fragment_slot
is_soul_material_slot = _cube_soul.is_soul_material_slot


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


def parse_sell_item_body(plain: bytes) -> dict[str, Any] | None:
    """解析 C1/0016。优先 S4/直播固定头，兼容仅 slot+count。"""
    data = bytes(plain or b"")
    if len(data) < 4:
        return None

    # 主路径：u8 list | i16 slot | i16 count（直播 13B 体前 5 字节）
    if len(data) >= 5 and data[0] <= 16:
        list_type = int(data[0]) & 0xFF
        slot = int(struct.unpack_from("<h", data, 1)[0])
        count = int(struct.unpack_from("<h", data, 3)[0])
        if 0 <= slot <= 0x300:
            return {
                "layout": "list_slot_count5",
                "list_type": list_type,
                "slot_index": slot,
                "sell_count": count if count > 0 else 0,
                "raw_len": len(data),
                "raw_hex": data[:16].hex(" "),
            }

    # 退化：i16 slot | i16 count（无 list，默认 Main）
    slot = int(struct.unpack_from("<h", data, 0)[0])
    count = int(struct.unpack_from("<h", data, 2)[0])
    if 0 < slot <= 0x300:
        return {
            "layout": "slot_count4",
            "list_type": 0,
            "slot_index": slot,
            "sell_count": count if count > 0 else 0,
            "raw_len": len(data),
            "raw_hex": data[:16].hex(" "),
        }
    return None


def build_sell_item_ack_ok(
    *,
    list_type: int,
    slot_index: int,
    applied_count: int,
    updated_gold: int,
) -> bytes:
    """S1/0016 成功体（S4A12 SellItemBuilder）。"""
    return (
        bytes([0x01])
        + struct.pack("<i", clamp_wallet_gold_for_client(updated_gold))
        + bytes([int(list_type) & 0xFF])
        + struct.pack("<h", int(slot_index))
        + struct.pack("<h", int(applied_count))
    )


def build_sell_item_ack_error(error_code: int = 0x11) -> bytes:
    return bytes([0x00, int(error_code) & 0xFF])


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


def _wallet_gold(repo: Any, cid: int) -> int:
    try:
        ok, _note, wallet = repo.get_account_wallet(int(cid))
        if ok:
            return max(0, int(wallet.get("gold") or 0))
    except Exception:
        pass
    try:
        _exp, _g_char, g_wallet, _job, _grow = repo.get_character_economy(int(cid))
        return max(0, int(g_wallet))
    except Exception:
        pass
    return 0


class SellItem0016Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle_sell_item_0016(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_SELL_ITEM_0016", True):
            return [], "C1/0016 SELL_ITEM gate=OFF"

        parsed = parse_sell_item_body(body)
        if parsed is None:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], (
                "C1/0016 SELL_ITEM parse_fail plain_len=%s; ACK_fail/%s"
                % (len(body or b""), mode)
            )

        list_type = int(parsed["list_type"]) & 0xFF
        slot = int(parsed["slot_index"])
        sell_count = int(parsed["sell_count"])
        base = (
            "C1/0016 SELL_ITEM layout=%s list=%s slot=%s count=%s plain=%s"
            % (
                parsed.get("layout"),
                list_type,
                slot,
                sell_count,
                parsed.get("raw_hex"),
            )
        )

        # 仅支持 Main / PersonalCargo 出售（S4 IsSupportedDeleteOrSellListType）
        if list_type not in (0, 2):
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], base + "; reject=list_type; ACK_fail/%s" % mode

        # 固定钱包槽禁止出售
        if list_type == 0 and slot in (0, 1):
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], base + "; reject=wallet_slot; ACK_fail/%s" % mode

        cid = _cid(session, self.server)
        if cid is None:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], base + "; skip=no_cid; ACK_fail/%s" % mode

        repo = self.server._repo()
        peek = repo.get_inventory_item_at(
            int(cid), list_type=list_type, slot_index=int(slot)
        )
        account_shared_material = False
        if list_type == 0 and (
            is_cube_fragment_slot(slot) or is_soul_material_slot(slot)
        ):
            # Cubes and Souls are account-ledger projections, not physical
            # character_items rows. Resolve fixed Main slots directly from the
            # account source so a legacy physical row cannot override it.
            peek = None
            aid = repo.get_account_id_for_character(int(cid))
            projected_rows = []
            if aid is not None:
                projected_rows = (
                    repo.list_cube_fragments(int(aid))
                    if is_cube_fragment_slot(slot)
                    else repo.list_account_souls(int(aid))
                )
            for row in projected_rows:
                if int(row.get("slot_index", -1)) != int(slot):
                    continue
                projected_item_id = int(row.get("item_template_id") or 0)
                if is_cube_fragment(projected_item_id) or is_account_soul(
                    projected_item_id
                ):
                    peek = row
                    account_shared_material = True
                break
        if peek is None:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], base + "; reject=slot_empty; ACK_fail/%s" % mode

        item_id = int(peek.get("item_template_id") or 0)
        kind = str(peek.get("item_kind") or "")
        stack = max(0, int(peek.get("stack_count") or 0))
        if item_id <= 0 or stack <= 0:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], base + "; reject=invalid_item; ACK_fail/%s" % mode

        equipment_lock_id = max(0, int(peek.get("equipment_lock_id") or 0)) & 0xFF
        if equipment_lock_id > 0:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [pkt], (
                base
                + "; reject=equipment_locked lock_id=%d; ACK_fail/%s"
                % (equipment_lock_id, mode)
            )

        meta = resolve_item_metadata(item_id)
        is_stack = bool(meta.is_stackable or kind == "stackable")
        # 非堆叠（装备）整格出售；堆叠按请求数量，0/超限 = 全卖
        if not is_stack or kind == "equipment":
            applied = stack if stack > 0 else 1
        elif sell_count <= 0 or sell_count >= stack:
            applied = stack
        else:
            applied = sell_count

        unit_gold = max(0, int(meta.sell_gold))
        gold_delta = int(unit_gold) * int(applied)

        if account_shared_material:
            ok, consume_note, detail = repo.consume_inventory_by_template(
                int(cid),
                int(item_id),
                int(applied),
                list_type=0,
            )
        else:
            ok, consume_note, detail = repo.consume_inventory_item(
                int(cid),
                list_type=list_type,
                slot_index=int(slot),
                count=int(applied),
                reject_locked=True,
            )
        removed = int(detail.get("removed") or 0)
        if not ok or removed <= 0:
            ack = build_sell_item_ack_error(0x11)
            pkt, mode = self._emit(1, 0x0016, ack)
            return [
                pkt
            ], base + "; consume_fail note=%s; ACK_fail/%s" % (consume_note, mode)

        # 金币入钱包 slot0
        updated_gold = _wallet_gold(repo, int(cid))
        if gold_delta > 0:
            _gok, _gnote, updated_gold = repo.grant_wallet_gold(int(cid), int(gold_delta))
        else:
            # 0 价也卖成功（S4 允许 zero-price consumable）
            updated_gold = _wallet_gold(repo, int(cid))

        packets: list[bytes] = []
        notes: list[str] = [base]
        notes.append(
            "OK item=%s removed=%s unit_gold=%s gold_delta=%s wallet=%s "
            "account_shared=%s consume=%s"
            % (
                item_id,
                removed,
                unit_gold,
                gold_delta,
                updated_gold,
                int(account_shared_material),
                consume_note,
            )
        )

        ack = build_sell_item_ack_ok(
            list_type=list_type,
            slot_index=int(slot),
            applied_count=int(removed),
            updated_gold=int(updated_gold),
        )
        pkt_ack, mode_ack = self._emit(1, 0x0016, ack)
        packets.append(pkt_ack)
        notes.append("ACK0016/%s plain=%s" % (mode_ack, ack.hex(" ")))

        # 刷新对应容器 ITEMLIST（默认开；与 0012/0013 对齐）
        if env_bool("USDOF_GAME_SELL_ITEM_SEND_000D", True):
            try:
                inv_rows = repo.list_inventory_items(int(cid), list_type=list_type)
                items = [InventoryItemView(row) for row in inv_rows]
                list_param16 = int(
                    repo.get_container_list_param(int(cid), list_type=list_type)
                )
                body_d = build_item_list_mode0(
                    items, list_type=list_type, list_param16=list_param16
                )
                pkt_d, mode_d = self._emit(0, 0x000D, body_d)
                packets.append(pkt_d)
                notes.append(
                    "000D/%s list=%s entries=%d" % (mode_d, list_type, len(items))
                )
            except Exception as exc:
                notes.append("000D_err=%r" % (exc,))

        return packets, "; ".join(notes)


__all__ = [
    "SellItem0016Service",
    "parse_sell_item_body",
    "build_sell_item_ack_ok",
    "build_sell_item_ack_error",
]
