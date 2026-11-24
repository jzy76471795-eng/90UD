"""C->S 0x0014 SORT_ITEM（背包/金库「整理」）。

直播（game_server_20260719_130837.log）：
  13:14:51 / 13:15:03 各一次 C1/0014 codec6，plain 形如::

      u32_len + protobuf:
        field2 = list_type   (0=Main 背包, 2=PersonalCargo 金库)
        field3 = category
        field4 = condition

  无 handler 时客户端会卡 UI / 异常退出。

S->C ACK（当前客户端016097B0 / protobuf RETURN）::

      flag=1 opcode=0x0014 body = success:u8 + length:u32 + RETURN
      RETURN field1 = list_type, field2 = result(0)

成功顺序固定为 ``S1/0014 ACK -> S0/000D 目标容器全量刷新``。槽位
重排由 character 域 repository 在单个 SQLite 事务内完成。
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.inventory_equipment_lock_ui import (
    build_equipment_lock_restore_packets,
)
from game.character.inventory_sort_lock_ui import (
    build_inventory_sort_lock_restore_packets,
)
from game.character.inventory_sort_repository import sort_inventory_container
from game.character.payloads import (
    build_account_cargo_item_list,
    build_avatar_item_list_mode0,
    build_item_list_mode0,
)
from game.quest.quest_session import resolve_selected_character_detail


def _read_varint(buf: bytes, off: int) -> tuple[int, int]:
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


def _parse_protobuf_fields(buf: bytes) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
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
            i += int(ln)
        elif wire == 5:
            if i + 4 > n:
                break
            i += 4
        elif wire == 1:
            if i + 8 > n:
                break
            i += 8
        else:
            break
    return out


def parse_sort_item_body(body: bytes) -> dict[str, Any]:
    """解析 C1/0014。优先 Stage3 u32+protobuf，退化 S4A12 固定 2B。"""
    raw = bytes(body or b"")
    out: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw.hex(" ") if len(raw) <= 48 else raw[:48].hex(" ") + "...",
        "layout": "unknown",
        "list_type": 0,
        "category": 0,
        "condition": 0,
    }
    if not raw:
        return out

    # Stage3 直播：u32_len + protobuf(field2=list, field3=cat, field4=cond)
    if len(raw) >= 5:
        declared = struct.unpack_from("<I", raw, 0)[0]
        if 1 <= declared <= 64 and 4 + declared <= len(raw):
            fields = _parse_protobuf_fields(raw[4 : 4 + declared])
            if 2 in fields or 3 in fields or 4 in fields:
                out["layout"] = "stage3_u32_len+protobuf"
                if 2 in fields:
                    out["list_type"] = int(fields[2][0]) & 0xFF
                if 3 in fields:
                    out["category"] = int(fields[3][0]) & 0xFF
                if 4 in fields:
                    out["condition"] = int(fields[4][0]) & 0xFF
                return out

    # 裸 protobuf（无 u32 前缀）
    fields = _parse_protobuf_fields(raw)
    if 2 in fields or 3 in fields:
        out["layout"] = "protobuf_fields"
        if 2 in fields:
            out["list_type"] = int(fields[2][0]) & 0xFF
        if 3 in fields:
            out["category"] = int(fields[3][0]) & 0xFF
        if 4 in fields:
            out["condition"] = int(fields[4][0]) & 0xFF
        return out

    # S4A12：u8 listType + u8 category [+ u8 condition]
    if len(raw) >= 2:
        out["layout"] = "s4a12_u8_list+u8_cat"
        out["list_type"] = int(raw[0]) & 0xFF
        out["category"] = int(raw[1]) & 0xFF
        if len(raw) >= 3:
            out["condition"] = int(raw[2]) & 0xFF
        return out

    out["layout"] = "u8_list_only"
    out["list_type"] = int(raw[0]) & 0xFF
    return out


def build_sort_item_ack_receipt(*, list_type: int) -> bytes:
    """016097B0 consumes the protobuf RETURN before releasing its container.

    004D88F0 writes enum field1 from object+14 (container) and field2 from
    object+18 (result). The historical two-byte ACK leaves account cargo pending.
    """
    container = int(list_type) & 0xFF
    encoded = bytes([container]) if container < 128 else bytes([(container & 0x7f) | 0x80, container >> 7])
    payload = b'\x08' + encoded + b'\x10\x00'
    return b'\x01' + struct.pack('<I', len(payload)) + payload


class SortItem0014Service:
    """Persist one selected sort segment and refresh exactly one container."""

    def __init__(self, server: Any) -> None:
        self.server = server

    @staticmethod
    def _build_container_refresh(
        repo: Any,
        *,
        character_id: int,
        account_id: int,
        character_level: int,
        list_type: int,
    ) -> tuple[bytes, str]:
        cid = int(character_id)
        aid = int(account_id)
        lt = int(list_type) & 0xFF
        if lt == int(repo.INVENTORY_LIST_ACCOUNT_CARGO):
            state = repo.get_account_cargo_state(
                aid,
                ensure_open=True,
                character_level=int(character_level),
            )
            rows = repo.list_account_cargo_items(
                aid,
                character_level=int(character_level),
            )
            selection_key = int(state.get("selection_key") or 0)
            if selection_key <= 0:
                selection_key = int(repo.DEFAULT_ACCOUNT_CARGO_SELECTION_KEY)
            body = build_account_cargo_item_list(
                [InventoryItemView(row) for row in rows],
                selection_key=selection_key,
                cargo_gold=int(state.get("value32") or 0),
            )
            return body, "S0/000D list12 n=%d key=%d" % (
                len(rows),
                selection_key,
            )

        rows = repo.list_inventory_items(cid, list_type=lt)
        items = [InventoryItemView(row) for row in rows]
        list_param16 = int(repo.get_container_list_param(cid, list_type=lt))
        if lt == int(repo.INVENTORY_LIST_AVATAR):
            body = build_avatar_item_list_mode0(
                items,
                list_param16=list_param16,
            )
        else:
            body = build_item_list_mode0(
                items,
                list_type=lt,
                list_param16=list_param16,
            )
        return body, "S0/000D list%d n=%d param=%d" % (
            lt,
            len(rows),
            list_param16,
        )

    def handle_sort_item_0014(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_sort_item_body(body)
        list_type = int(parsed.get("list_type") or 0) & 0xFF
        category = int(parsed.get("category") or 0) & 0xFF
        condition = int(parsed.get("condition") or 0) & 0xFF
        layout = str(parsed.get("layout") or "unknown")

        detail = resolve_selected_character_detail(self.server, session)
        character_id = detail.get("character_id")
        level = max(1, int(detail.get("level") or 1))
        ack = build_sort_item_ack_receipt(list_type=list_type)
        ack_packet = self.server._codec_response(1, 0x0014, ack)
        base = (
            "C1/0014 SORT_ITEM cid=%s plain_len=%s layout=%s "
            "list_type=%s category=%s condition=%s"
            % (
                character_id,
                len(body or b""),
                layout,
                list_type,
                category,
                condition,
            )
        )
        if character_id is None or layout == "unknown":
            return [ack_packet], "%s; reject=%s; S1/0014 ack=%s" % (
                base,
                "no_character" if character_id is None else "unknown_layout",
                ack.hex(" "),
            )

        repo = self.server._repo()
        account_id = repo.get_account_id_for_character(int(character_id))
        if account_id is None:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        result = sort_inventory_container(
            repo,
            character_id=int(character_id),
            account_id=int(account_id),
            list_type=list_type,
            category=category,
        )
        if not result.success:
            return [ack_packet], (
                "%s; sort_fail=%s range=%s..%s items=%s elapsed_ms=%s; "
                "S1/0014 ack=%s"
                % (
                    base,
                    result.reason,
                    result.start_slot,
                    result.end_slot,
                    result.item_count,
                    result.elapsed_ms,
                    ack.hex(" "),
                )
            )

        try:
            refresh_body, refresh_note = self._build_container_refresh(
                repo,
                character_id=int(character_id),
                account_id=int(account_id),
                character_level=level,
                list_type=list_type,
            )
        except Exception as exc:
            return [ack_packet], (
                "%s; sort_ok=1 changed=%s moved=%s elapsed_ms=%s; "
                "refresh_fail=%s:%s; S1/0014 ack=%s"
                % (
                    base,
                    int(result.changed),
                    result.moved_count,
                    result.elapsed_ms,
                    type(exc).__name__,
                    exc,
                    ack.hex(" "),
                )
            )
        refresh_packet = self.server._codec_response(0, 0x000D, refresh_body)
        restore_packets: list[Any] = []
        restore_note = ""
        if list_type in (
            int(repo.INVENTORY_LIST_MAIN),
            int(repo.INVENTORY_LIST_AVATAR),
        ):
            try:
                restore_packets, restore_note = (
                    build_inventory_sort_lock_restore_packets(
                        self.server,
                        repo,
                        character_id=int(character_id),
                        list_type=list_type,
                    )
                )
            except Exception as exc:
                restore_note = "sort_lock_restore_fail=%s:%s" % (
                    type(exc).__name__,
                    exc,
                )
        if list_type in (
            int(repo.INVENTORY_LIST_MAIN),
            int(repo.INVENTORY_LIST_AVATAR),
        ):
            try:
                equipment_packets, equipment_note = (
                    build_equipment_lock_restore_packets(
                        self.server,
                        repo,
                        character_id=int(character_id),
                        list_types=(list_type,),
                    )
                )
                restore_packets.extend(equipment_packets)
                restore_note = "; ".join(
                    note for note in (restore_note, equipment_note) if note
                )
            except Exception as exc:
                equipment_note = "equipment_lock_restore_fail=%s:%s" % (
                    type(exc).__name__,
                    exc,
                )
                restore_note = "; ".join(
                    note for note in (restore_note, equipment_note) if note
                )
        note = (
            "%s; sort_ok=1 changed=%s reason=%s range=%s..%s items=%s "
            "moved=%s elapsed_ms=%s; S1/0014 ack=%s; %s wire=%s%s"
            % (
                base,
                int(result.changed),
                result.reason,
                result.start_slot,
                result.end_slot,
                result.item_count,
                result.moved_count,
                result.elapsed_ms,
                ack.hex(" "),
                refresh_note,
                len(refresh_packet),
                "; " + restore_note if restore_note else "",
            )
        )
        return [ack_packet, refresh_packet, *restore_packets], note
