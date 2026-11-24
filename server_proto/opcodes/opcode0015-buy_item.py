# -*- coding: utf-8 -*-
"""C1/0x0015 ENUM_CMDPACKET_BUY_ITEM — NPC 商店购买。

注意双表：
  - C->S flag1/0x0015 = BUY_ITEM（本文件）
  - S->C flag0/0x0015 = ACCEPTABLE_QUEST_LIST（可接任务，其它链路）

Stage3 直播（2026-07-19 11:43 商店买 战吼水晶）::

  plain: b6 f0 99 00 01 00 00 00 52 00 00 00 2d 01 00 00
  → i32 item=10088630 | i32 count=1 | 尾部 shop/UI 字段忽略

  该道具 PVF 无 [price]，有 ``[need material] 10088621 1``（材料兑换）。

对照：
  - S4A12 ``Handle_ENUM_CMDPACKET_BUY_ITEM`` / ``BuyItemAckBuilder`` / ``TryBuyItem``
  - 90qiyuan ``DecodeBuyItemRequest``：i32 item + optional i32 count

语义：
  - 可买：扣金币/材料 → 入库 → S1/0015 成功 ACK（108B 道具条）+ 000E/000D
  - 不可买：S1/0015 失败 ACK（解锁商店 UI，不入库）
"""
from __future__ import annotations
from contextlib import nullcontext
from game.shop.purchase_limits import reservation,notice,rule_for

import struct
import re
from functools import lru_cache
from pvf import pvf_runtime
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.character_db import (
    is_virtual_inventory_grant,
    parse_virtual_premium_grant,
)
from game.character.item_metadata import GOLD_CUBE_FRAGMENT_ID, resolve_item_metadata
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.character.payloads import (
    build_avatar_item_list_mode0,
    build_guild_medal_item_list_mode0,
    build_item_list_mode0,
    build_pet_item_list_mode0,
)
from game.chain_loader import load_chain
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail

_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e
build_wallet_gold_update_000e = _payloads.build_wallet_gold_update_000e
clamp_wallet_gold_for_client = _payloads.clamp_wallet_gold_for_client

# Stage3 `sub_1623380` failure reader: 0x04 uses the full-inventory prompt,
# while 0x11 selects the dedicated material-insufficient prompt.
BUY_ITEM_ERROR_INVENTORY_FULL = 0x04
BUY_ITEM_ERROR_MATERIAL_NOT_ENOUGH = 0x11


@lru_cache(maxsize=2)
def raid_ticket_costs(path):
    text = pvf_runtime.read_text(path)
    section = re.search(r"\[need material\]([^\[]+)\[/need material\]", text)
    if section is None:
        raise ValueError("missing raid ticket price")
    values = [int(value) for value in section[1].split()]
    if not values or len(values) % 2 or min(values) <= 0:
        raise ValueError("invalid raid ticket price")
    return tuple(zip(values[::2], values[1::2]))


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


def parse_buy_item_body(plain: bytes) -> dict[str, Any] | None:
    """解析 C1/0015。语义：i32 item_id | i32 count（缺省 1）。"""
    data = bytes(plain or b"")
    if len(data) < 4:
        return None
    item_id = int(struct.unpack_from("<i", data, 0)[0])
    if item_id <= 0:
        return None
    count = 1
    if len(data) >= 8:
        count = int(struct.unpack_from("<i", data, 4)[0])
        if count <= 0:
            count = 1
    # 直播 16B 尾字段仅日志，不参与计价（防客户端改价）
    extra0 = int(struct.unpack_from("<i", data, 8)[0]) if len(data) >= 12 else 0
    extra1 = int(struct.unpack_from("<i", data, 12)[0]) if len(data) >= 16 else 0
    return {
        "layout": "item_count8",
        "item_template_id": item_id & 0xFFFFFFFF,
        "buy_count": int(count),
        "extra0": extra0,
        "extra1": extra1,
        "raw_len": len(data),
        "raw_hex": data[:16].hex(" "),
    }


def build_buy_item_ack_ok(
    *,
    updated_gold: int,
    slot_index: int,
    item_template_id: int,
    instance_value: int,
    durability: int = 0,
    seal_flag: int = 0,
    updated_sp: int = 0,
    updated_coin: int = 0,
    cost_items: list[tuple[int, int]] | None = None,
) -> bytes:
    """S1/0015 成功体 — Stage3 IDA ``sub_1623380``。

    读序（dispatcher 先读 u8 success，handler 再读剩余）::

      u8  success=1
      i32 gold
      i32 sp
      i32 reserved0
      i32 coin
      108B item entry   # 与 S0/000E 同形（``sub_2C74B80`` push 0x6C）
        u16 slot
        u32 item_template_id
        u32 count/instance
        u8  ext0
        u16 durability
        u8  seal
        其余 0
      u8  cost_count
      cost_count × (i32 material_id, i32 new_stack)

    旧 S4 短体（~47B，无完整 108B 条）会在读道具条时溢出 → 客户端 00D9。
    """
    body = bytearray()
    body.append(0x01)
    body += struct.pack("<i", clamp_wallet_gold_for_client(updated_gold))
    body += struct.pack("<i", int(updated_sp) & 0xFFFFFFFF)
    body += struct.pack("<i", 0)
    body += struct.pack("<i", int(updated_coin) & 0xFFFFFFFF)
    # 108B 道具条（Stage3 硬编码 0x6C）
    entry = bytearray(108)
    struct.pack_into(
        "<HII",
        entry,
        0,
        int(slot_index) & 0xFFFF,
        int(item_template_id) & 0xFFFFFFFF,
        int(instance_value) & 0xFFFFFFFF,
    )
    entry[10] = 0  # ext0
    struct.pack_into("<H", entry, 11, max(0, int(durability)) & 0xFFFF)
    entry[13] = max(0, int(seal_flag)) & 0xFF
    body += entry
    costs = list(cost_items or [])
    body.append(min(255, len(costs)) & 0xFF)
    for mid, mstack in costs[:255]:
        body += struct.pack("<i", int(mid) & 0xFFFFFFFF)
        body += struct.pack("<i", max(0, int(mstack)) & 0xFFFFFFFF)
    return bytes(body)


def build_buy_item_ack_error(error_code: int = BUY_ITEM_ERROR_INVENTORY_FULL) -> bytes:
    """S1/0015 失败体：u8 0 + u8 error（解锁商店 UI / 弹错误）。"""
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


class BuyItem0015Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle_buy_item_0015(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_BUY_ITEM_0015", True):
            return [], "C1/0015 BUY_ITEM gate=OFF"

        parsed = parse_buy_item_body(body)
        if parsed is None:
            ack = build_buy_item_ack_error(BUY_ITEM_ERROR_INVENTORY_FULL)
            pkt, mode = self._emit(1, 0x0015, ack)
            return [pkt], (
                "C1/0015 BUY_ITEM parse_fail plain_len=%s; ACK_fail/%s"
                % (len(body or b""), mode)
            )

        item_id = int(parsed["item_template_id"])
        buy_count = max(1, int(parsed["buy_count"]))
        base = (
            "C1/0015 BUY_ITEM layout=%s item=%s count=%s extra=%s/%s plain=%s"
            % (
                parsed.get("layout"),
                item_id,
                buy_count,
                parsed.get("extra0"),
                parsed.get("extra1"),
                parsed.get("raw_hex"),
            )
        )

        def _fail(
            reason: str,
            err: int = BUY_ITEM_ERROR_INVENTORY_FULL,
        ) -> tuple[list[bytes], str]:
            ack = build_buy_item_ack_error(err)
            pkt, mode = self._emit(1, 0x0015, ack)
            return [pkt], base + "; %s; ACK_fail/%s" % (reason, mode)

        cid = _cid(session, self.server)
        if cid is None:
            return _fail("skip=no_cid")

        meta = resolve_item_metadata(item_id)
        kind = str(meta.item_kind or "")
        # S4: special/unknown 拒买
        if kind in {"special", "unknown", ""}:
            return _fail("reject=kind=%s note=%s" % (kind, meta.note))

        unit_gold = max(0, int(meta.buy_gold))
        total_gold = unit_gold * buy_count
        mat_id = int(meta.need_material_id or 0) if meta.is_material_exchange else 0
        mat_unit = int(meta.need_material_count or 0) if meta.is_material_exchange else 0
        total_mat = mat_unit * buy_count

        repo = self.server._repo()
        have_gold = _wallet_gold(repo, int(cid))
        if have_gold < total_gold:
            return _fail(
                "reject=gold have=%s need=%s" % (have_gold, total_gold), err=0x04
            )

        # Both sides of a cube exchange use the account inventory ledger.
        scope = repo.inventory_transaction_scope() if item_id == GOLD_CUBE_FRAGMENT_ID and total_mat else nullcontext()
        effective_count = buy_count if meta.is_stackable or kind == 'stackable' else 1
        try:rule_for(meta)
        except (ValueError,KeyError) as exc:return _fail('reject=purchase_limit_rule '+str(exc),err=0x13)
        extra_costs = ()
        extra_updates = []
        if item_id in (10162064, 10162065):
            try:
                costs = raid_ticket_costs(meta.source_path)
                if costs[0] != (mat_id, mat_unit) or not rule_for(meta):
                    raise ValueError("ticket metadata mismatch")
                extra_costs = costs[1:]
            except (ValueError, KeyError) as exc:
                return _fail("reject=raid_ticket_terms " + str(exc), err=0x13)
        with reservation(repo,int(cid),meta,effective_count) as quota, scope:
            if not quota.allowed:
                packets,note=_fail('reject='+quota.reason,err=0x13)
                return [notice(self.server)]+packets,note
            cost_primary_slot = -1
            cost_remaining = 0
            if total_mat > 0 and mat_id > 0:
                counts = repo.count_inventory_items_by_template(
                    int(cid), list_type=0, item_ids=[mat_id]
                )
                have_mat = int(counts.get(mat_id, 0) or 0)
                if have_mat < total_mat:
                    return _fail(
                        "reject=material id=%s have=%s need=%s"
                        % (mat_id, have_mat, total_mat),
                        err=BUY_ITEM_ERROR_MATERIAL_NOT_ENOUGH,
                    )

            for material, unit in extra_costs:
                ok, note, detail = repo.consume_inventory_by_template(
                    int(cid), material, unit * buy_count, list_type=0
                )
                if not ok:
                    return _fail("reject=raid_material " + note, err=BUY_ITEM_ERROR_MATERIAL_NOT_ENOUGH)
                extra_updates.append((material, detail))

            # 1) 先扣材料（不足已在上面拦截）
            if total_mat > 0 and mat_id > 0:
                mok, mnote, mdetail = repo.consume_inventory_by_template(
                    int(cid), mat_id, total_mat, list_type=0
                )
                if not mok:
                    return _fail(
                        "consume_mat_fail %s" % mnote,
                        err=BUY_ITEM_ERROR_MATERIAL_NOT_ENOUGH,
                    )
                cost_primary_slot = int(mdetail.get("primary_slot") or -1)
                cost_remaining = int(mdetail.get("remaining_at_primary") or 0)
                base_mat_note = mnote
            else:
                base_mat_note = "no_material"

            # 2) 扣金币
            updated_gold = have_gold
            if total_gold > 0:
                gok, gnote, updated_gold = repo.spend_wallet_gold(int(cid), total_gold)
                if not gok:
                    # 尽力回滚材料
                    if total_mat > 0 and mat_id > 0:
                        repo.grant_inventory_item(int(cid), mat_id, total_mat)
                    return _fail("spend_gold_fail %s" % gnote)
            else:
                updated_gold = _wallet_gold(repo, int(cid))
                gnote = "gold0"

            # 3) 入库
            effective_count = buy_count if meta.is_stackable or kind == "stackable" else 1
            ok, grant_note, slot = repo.grant_inventory_item(
                int(cid), int(item_id), int(effective_count)
            )
            virtual_grant = bool(ok and is_virtual_inventory_grant(grant_note))
            if not ok or (int(slot) < 0 and not virtual_grant):
                # 回滚金币与材料
                if total_gold > 0:
                    repo.grant_wallet_gold(int(cid), total_gold)
                if total_mat > 0 and mat_id > 0:
                    repo.grant_inventory_item(int(cid), mat_id, total_mat)
                return _fail("grant_fail %s" % grant_note)

            quota.commit()
        delivery_list_type = int(meta.inventory_list_type())
        row = repo.get_inventory_item_at(
            int(cid),
            list_type=delivery_list_type,
            slot_index=int(slot),
        )
        if row is None:
            # Account cubes/Souls are virtual list0 rows and deliberately have
            # no character_items record.  Read their projected total so the
            # 000E update does not replace an existing account balance with
            # only this transaction's quantity.  The ACK item entry retains
            # its established "quantity purchased" value.
            row = next(
                (
                    item
                    for item in repo.list_inventory_items(
                        int(cid),
                        list_type=delivery_list_type,
                    )
                    if int(item.get("slot_index", -1)) == int(slot)
                    and int(item.get("item_template_id", 0)) == int(item_id)
                ),
                None,
            )
        stack_now = int((row or {}).get("stack_count") or effective_count)
        projected_item_id = int(
            (row or {}).get("item_template_id", item_id) or item_id
        ) & 0xFFFFFFFF
        dura = int((row or {}).get("durability") or meta.durability or 0)
        # ACK instance：堆叠回写入数量；装备用 TopQualitySeed（grant 已写）
        if str((row or {}).get("item_kind") or "") == "equipment":
            instance_value = int((row or {}).get("instance_value") or 0)
            # 供 C1/001A codec12 乱码 fallback：优先拆刚买进的装备槽
            if session is not None and delivery_list_type == 0 and int(slot) > 0:
                try:
                    setattr(session, "last_buy_item_slot", int(slot))
                    prefer = list(getattr(session, "last_buy_item_slots", None) or [])
                    if int(slot) in prefer:
                        prefer = [s for s in prefer if int(s) != int(slot)]
                    prefer.insert(0, int(slot))
                    setattr(session, "last_buy_item_slots", prefer[:12])
                except Exception:
                    pass
        else:
            instance_value = int(effective_count)

        cost_items: list[tuple[int, int]] = []
        if mat_id > 0 and total_mat > 0:
            # ACK 里是「该材料模板剩余总堆」语义；S4 用 CostItemNewStackCount（单槽）
            # 用 primary 槽剩余，与后续 000E 一致
            cost_items.append((mat_id, max(0, cost_remaining)))

        for material, detail in extra_updates:
            cost_items.append((material, int(detail["remaining_at_primary"])))

        if virtual_grant:
            if session is not None:
                try:
                    session.account_premium_effects = None
                except Exception:
                    pass
            # The transaction already activated the account reward.  Return a
            # successful ACK with an empty item row so the client never gets a
            # contract template that it could place in Main.
            virtual_slot = 1 if is_revive_coin_grant_item_id(item_id) else 0
            virtual_item_id = 1 if is_revive_coin_grant_item_id(item_id) else 0xFFFFFFFF
            virtual_count = 0
            if is_revive_coin_grant_item_id(item_id):
                wallet_ok, _wallet_note, wallet_state = repo.get_account_wallet(int(cid))
                if wallet_ok:
                    virtual_count = int(wallet_state.get("revive_coin", 0) or 0)
            ack = build_buy_item_ack_ok(
                updated_gold=int(updated_gold),
                slot_index=virtual_slot,
                item_template_id=virtual_item_id,
                instance_value=virtual_count,
                durability=0,
                cost_items=cost_items,
            )
            packets: list[bytes] = []
            pkt_ack, mode_ack = self._emit(1, 0x0015, ack)
            packets.append(pkt_ack)
            premium_notice = parse_virtual_premium_grant(grant_note)
            if premium_notice is not None:
                helper_activation = load_chain("helper-system_reward_activation.py")
                packets.append(
                    self._emit(
                        0,
                        0x0042,
                        helper_activation.build_premium_activation_notice(
                            *premium_notice
                        ),
                    )[0]
                )
            if total_gold > 0 and env_bool("USDOF_GAME_BUY_ITEM_SEND_WALLET_000E", True):
                pkt_w, _mode_w = self._emit(
                    0,
                    0x000E,
                    build_wallet_gold_update_000e(int(updated_gold)),
                )
                packets.append(pkt_w)
            if is_revive_coin_grant_item_id(item_id) and env_bool(
                "USDOF_GAME_BUY_ITEM_SEND_GRANT_000E", True
            ):
                body_coin = build_item_list_update_000e(
                    [(1, 1, int(virtual_count))], list_type=0
                )
                packets.append(self._emit(0, 0x000E, body_coin)[0])
            if (
                mat_id > 0
                and total_mat > 0
                and cost_primary_slot >= 0
                and env_bool("USDOF_GAME_BUY_ITEM_SEND_COST_000E", True)
            ):
                if cost_remaining > 0:
                    body_c = build_item_list_update_000e(
                        [(int(cost_primary_slot), int(mat_id), int(cost_remaining))],
                        list_type=0,
                    )
                else:
                    body_c = build_item_list_clear_slot_000e(
                        int(cost_primary_slot), list_type=0
                    )
                pkt_c, _mode_c = self._emit(0, 0x000E, body_c)
                packets.append(pkt_c)
            virtual_slot = 1 if is_revive_coin_grant_item_id(item_id) else -2
            return packets, (
                base
                + " OK slot=%d virtual_grant grant=%s"
                % (virtual_slot, grant_note)
            )

        ack = build_buy_item_ack_ok(
            updated_gold=int(updated_gold),
            slot_index=int(slot),
            item_template_id=projected_item_id,
            instance_value=int(instance_value),
            durability=int(dura),
            cost_items=cost_items,
        )

        packets: list[bytes] = []
        notes = [
            base,
            "OK slot=%s gold=%s cost_mat=%sx%s remain_slot=%s grant=%s mat=%s gold_note=%s"
            % (
                slot,
                updated_gold,
                mat_id,
                total_mat,
                cost_remaining,
                grant_note,
                base_mat_note,
                gnote,
            ),
        ]

        pkt_ack, mode_ack = self._emit(1, 0x0015, ack)
        packets.append(pkt_ack)
        notes.append("ACK_ok/%s len=%d" % (mode_ack, len(ack)))

        # 钱包 000E（扣金时）
        if total_gold > 0 and env_bool("USDOF_GAME_BUY_ITEM_SEND_WALLET_000E", True):
            body_w = build_wallet_gold_update_000e(int(updated_gold))
            pkt_w, mode_w = self._emit(0, 0x000E, body_w)
            packets.append(pkt_w)
            notes.append("000E_wallet/%s gold=%s" % (mode_w, updated_gold))

        # 材料槽 000E
        if (
            mat_id > 0
            and total_mat > 0
            and cost_primary_slot >= 0
            and env_bool("USDOF_GAME_BUY_ITEM_SEND_COST_000E", True)
        ):
            if cost_remaining > 0:
                body_c = build_item_list_update_000e(
                    [(int(cost_primary_slot), int(mat_id), int(cost_remaining))],
                    list_type=0,
                )
            else:
                body_c = build_item_list_clear_slot_000e(
                    int(cost_primary_slot), list_type=0
                )
            pkt_c, mode_c = self._emit(0, 0x000E, body_c)
            packets.append(pkt_c)
            notes.append(
                "000E_cost/%s slot=%s remain=%s" % (mode_c, cost_primary_slot, cost_remaining)
            )

        if env_bool("USDOF_GAME_BUY_ITEM_SEND_COST_000E", True):
            for material, detail in extra_updates:
                cost_slot = int(detail["primary_slot"])
                remaining = int(detail["remaining_at_primary"])
                if remaining:
                    change = build_item_list_update_000e([(cost_slot, material, remaining)], list_type=0)
                else:
                    change = build_item_list_clear_slot_000e(cost_slot, list_type=0)
                packets.append(self._emit(0, 0x000E, change)[0])

        # 购入格 000E
        if env_bool("USDOF_GAME_BUY_ITEM_SEND_GRANT_000E", True):
            body_g = build_item_list_update_000e(
                [(int(slot), projected_item_id, int(stack_now), int(dura))],
                list_type=delivery_list_type,
            )
            pkt_g, mode_g = self._emit(0, 0x000E, body_g)
            packets.append(pkt_g)
            notes.append("000E_grant/%s slot=%s stack=%s" % (mode_g, slot, stack_now))

        # Full authoritative container snapshot (Main remains the common path).
        if env_bool("USDOF_GAME_BUY_ITEM_SEND_000D", True):
            try:
                inv_rows = repo.list_inventory_items(
                    int(cid),
                    list_type=delivery_list_type,
                )
                items = [InventoryItemView(row) for row in inv_rows]
                if delivery_list_type == 1:
                    list_param16 = int(
                        repo.get_container_list_param(int(cid), list_type=1) or 0
                    )
                    body_d = build_avatar_item_list_mode0(
                        items,
                        list_param16=list_param16,
                    )
                elif delivery_list_type == 7:
                    body_d = build_pet_item_list_mode0(items)
                elif delivery_list_type == repo.INVENTORY_LIST_GUILD_MEDAL:
                    body_d = build_guild_medal_item_list_mode0(
                        items,
                        selected_character_slot=int(
                            getattr(session, "selected_slot", 0) or 0
                        ),
                    )
                else:
                    list_param16 = int(
                        repo.get_container_list_param(
                            int(cid),
                            list_type=delivery_list_type,
                        )
                        or 0
                    )
                    body_d = build_item_list_mode0(
                        items,
                        list_type=delivery_list_type,
                        list_param16=list_param16,
                    )
                pkt_d, mode_d = self._emit(0, 0x000D, body_d)
                packets.append(pkt_d)
                notes.append(
                    "000D/%s list=%s n=%s"
                    % (mode_d, delivery_list_type, len(items))
                )
            except Exception as exc:
                notes.append("000D_fail=%s" % exc)

        return packets, "%s; chain_file=opcodes/opcode0015-buy_item.py" % (
            "; ".join(notes)
        )
