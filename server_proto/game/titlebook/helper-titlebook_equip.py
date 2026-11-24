"""称号簿穿/卸：C->S 0x019C PUT / 0x019D GET。

基线：``server_proto/docs/baseline_titlebook_20260719.md``（tip de78ad321）。

对照 S4A12 InventoryHandler.Handle_TITLE_BOOK + TitleBookMutationService：
- C body: i32 itemSpace | i32 slot | i32 itemId | i32 category | i32 bookIndex
- 019C PUT + itemSpace=Main(0)：背包称号 → PVF 通配自定义槽
- 019D GET + itemSpace=Main(0)：PVF 通配自定义槽 → 背包空槽
- 019D GET + itemSpace=Equipment(3)：簿 → 装备栏 slot（称号位 13）
- 019C PUT + itemSpace=Equipment(3)：装备栏 → 簿
- Success ACK(flag1 same opcode): u8 1 | i32 space | i32 slot | i32 cat | i32 idx
- Fail ACK: u8 0 | u8 err | i32 space | i32 cat
- 装备变更后默认：S0/000E list3 + S0/0002 USERINFO0(+U1 exp)
  **默认不发 0166**（S4A12 也不发；UI 打开时 0166 清表重建会黑屏闪退）

Live Stage3:
  plain=03 00 00 00 0d 00 00 00 e4 67 00 00 00 00 00 00 00 00 00 00
  → space=3 slot=13 item=26596 cat=0 idx=0
  plain=00 00 00 00 0a 00 00 00 7a 6e d2 1d 04 00 00 00 0a 00 00 00
  → space=0 slot=10 item=500330106 cat=4 idx=10（自定义通配槽）
  自定义换装：客户端找不到旧称号原生簿槽时，先以 0013 卸到 Main 并结束本次右键；后续
  019D 只确认装备新称号。旧称号的 Main 幽灵项必须由玩家真实 019C 请求收敛，不附加伪造 ACK。
"""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain
from game.character.item_metadata import resolve_item_metadata
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail
from game.titlebook.titlebook_index import get_titlebook_index
from game.titlebook.titlebook_repository import TitleBookCustomRepository

_payloads = load_chain("helper-titlebook_payloads.py")
build_titlebook_list_0166 = _payloads.build_titlebook_list_0166
entries_from_repo_rows = _payloads.entries_from_repo_rows

INVENTORY_LIST_EQUIPMENT = 3
INVENTORY_LIST_MAIN = 0
TITLE_EQUIP_SLOT_DEFAULT = 13
ERROR_GENERIC = 0x0A
ERROR_STATE = 0x02


def parse_title_book_body(body: bytes) -> dict[str, int] | None:
    data = bytes(body or b"")
    if len(data) < 20:
        return None
    item_space, slot, item_id, category, book_index = struct.unpack_from("<iiiii", data, 0)
    return {
        "item_space": int(item_space),
        "slot": int(slot),
        "item_id": int(item_id),
        "category": int(category),
        "book_index": int(book_index),
        "raw_len": len(data),
    }


def build_title_book_ack_ok(
    *,
    item_space: int,
    slot: int,
    category: int,
    book_index: int,
) -> bytes:
    return (
        bytes([0x01])
        + struct.pack("<i", int(item_space))
        + struct.pack("<i", int(slot))
        + struct.pack("<i", int(category))
        + struct.pack("<i", int(book_index))
    )


def build_title_book_ack_fail(
    *,
    item_space: int,
    category: int,
    error_code: int = ERROR_GENERIC,
) -> bytes:
    return (
        bytes([0x00, int(error_code) & 0xFF])
        + struct.pack("<i", int(item_space))
        + struct.pack("<i", int(category))
    )


def _is_valid_category_index(category: int, book_index: int) -> bool:
    idx = get_titlebook_index()
    cap = idx.capacity(int(category))
    return int(category) >= 0 and cap > 0 and 0 <= int(book_index) < cap


def _resolve_book_slot_for_item(
    item_id: int,
    requested_category: int,
    requested_book_index: int,
) -> tuple[int, int] | None:
    """优先 titlebook.etc item 映射；否则接受客户端合法 cat/idx。"""
    tb = get_titlebook_index()
    hit = tb.find_by_item_id(int(item_id))
    if hit is not None:
        return int(hit.category), int(hit.index)
    if _is_valid_category_index(requested_category, requested_book_index):
        return int(requested_category), int(requested_book_index)
    return None


def _is_allowed_custom_title_slot(
    item_id: int,
    category: int,
    book_index: int,
) -> tuple[bool, str]:
    """Validate a Main title against the warmed PVF custom-slot contract."""
    if not _is_valid_category_index(category, book_index):
        return False, "invalid_category_index"
    definition = get_titlebook_index().get_slot(int(category), int(book_index))
    if (
        not definition.is_open
        or int(definition.quest_id) != -1
        or not definition.allows_item(int(item_id))
    ):
        return False, "target_not_custom_title_slot"
    metadata = resolve_item_metadata(int(item_id))
    if not bool(getattr(metadata, "is_equipment", False)):
        return False, "source_not_equipment"
    equipment_slot = getattr(metadata, "equipment_slot", None)
    wear_slot = equipment_slot() if callable(equipment_slot) else None
    if wear_slot != TITLE_EQUIP_SLOT_DEFAULT:
        return False, "source_not_title"
    return True, "custom_title_slot"


def _is_custom_title_slot_definition(
    item_id: int,
    category: int,
    book_index: int,
) -> bool:
    if not _is_valid_category_index(category, book_index):
        return False
    definition = get_titlebook_index().get_slot(int(category), int(book_index))
    return bool(
        definition.is_open
        and int(definition.quest_id) == -1
        and definition.allows_item(int(item_id))
    )


def _pack_equip_and_appearance(
    server: Any,
    *,
    character_id: int,
    record: Any,
    category: int,
) -> tuple[list[bytes], str]:
    """装备变更后同步。

    默认（对齐 S4A12 Handle_TITLE_BOOK + Stage3 经验修复）：
      - S0/000E list3（装备栏槽对象）
      - S0/0002 USERINFO0 + USERINFO1（外观 + exp 防脏）
      - **不发** S0/0166：019C/019D ACK 已带 cat/idx，客户端本地改簿；
        在称号簿 UI 打开时 0166 清分类再 put 会与本地态打架 → 黑屏闪退
        （live 2026-07-19 替换 26596↔26598 双 refresh 路径）。
    调试可开：``USDOF_GAME_TITLEBOOK_SEND_0166_ON_EQUIP=1``。
    """
    packets: list[bytes] = []
    notes: list[str] = []
    repo = server._repo()

    if env_bool("USDOF_GAME_TITLEBOOK_SEND_EQUIP_000E", True):
        try:
            equip_helper = load_chain("helper-equip_itemlist.py")
            equip_body, n, enote = equip_helper.build_equipment_bar_update_000e_body(
                repo, int(character_id)
            )
            packets.append(server._codec_response(0, 0x000E, equip_body))
            notes.append("000E n=%s %s" % (n, enote))
        except Exception as exc:
            notes.append("000E_fail:%s" % type(exc).__name__)

    if env_bool("USDOF_GAME_TITLEBOOK_SEND_USERINFO0", True) and record is not None:
        try:
            # 统一走 helper：U0 外观 + 默认 U1(DB exp) 防经验条脏值
            equip_helper = load_chain("helper-userinfo_equipment.py")
            u0_packets, u0_note = equip_helper.build_userinfo0_appearance_packets(
                server,
                character_id=int(character_id),
                record=record,
                repo=repo,
            )
            packets.extend(u0_packets)
            notes.append(u0_note)
        except Exception as exc:
            notes.append("USERINFO0_fail:%s" % type(exc).__name__)

    # 默认关：替换称号时 019C+019D 各发一次 0166 曾导致黑屏闪退
    if env_bool("USDOF_GAME_TITLEBOOK_SEND_0166_ON_EQUIP", False):
        try:
            rows = repo.list_titlebook_items(int(character_id), category=int(category))
            body = build_titlebook_list_0166(
                int(category), entries_from_repo_rows(rows)
            )
            packets.append(server._codec_response(0, 0x0166, body))
            notes.append("0166 cat=%s n=%s" % (int(category), len(rows)))
        except Exception as exc:
            notes.append("0166_fail:%s" % type(exc).__name__)
    else:
        notes.append("0166=off")

    return packets, ";".join(notes) if notes else "no_refresh"


class TitleBookEquipService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_title_book(
        self,
        body: bytes,
        *,
        opcode: int,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        """opcode=0x019C put / 0x019D get。"""
        op = int(opcode) & 0xFFFF
        is_put = op == 0x019C
        tag = "PUT" if is_put else "GET"
        handle_on = env_bool("USDOF_GAME_HANDLE_TITLE_BOOK", True)
        parsed = parse_title_book_body(body)
        detail = resolve_selected_character_detail(self.server, session)
        cid = detail.get("character_id")
        record = detail.get("record")
        base = (
            "C1/%04X TITLE_BOOK_%s cid=%s plain_len=%s"
            % (op, tag, cid, len(body or b""))
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if parsed is None:
            packets = [
                self.server._codec_response(
                    1,
                    op,
                    build_title_book_ack_fail(
                        item_space=0, category=0, error_code=ERROR_GENERIC
                    ),
                )
            ]
            return packets, base + "; reject=short_body"
        if cid is None:
            packets = [
                self.server._codec_response(
                    1,
                    op,
                    build_title_book_ack_fail(
                        item_space=parsed["item_space"],
                        category=parsed["category"],
                        error_code=ERROR_GENERIC,
                    ),
                )
            ]
            return packets, base + "; reject=no_character"

        space = int(parsed["item_space"])
        slot = int(parsed["slot"])
        item_id = int(parsed["item_id"])
        category = int(parsed["category"])
        book_index = int(parsed["book_index"])
        detail_s = (
            " space=%s slot=%s item=%s cat=%s idx=%s"
            % (space, slot, item_id, category, book_index)
        )

        if space not in (INVENTORY_LIST_MAIN, INVENTORY_LIST_EQUIPMENT):
            packets = [
                self.server._codec_response(
                    1,
                    op,
                    build_title_book_ack_fail(
                        item_space=space,
                        category=category,
                        error_code=ERROR_STATE,
                    ),
                )
            ]
            return packets, base + detail_s + "; reject=unsupported_item_space"

        repo = self.server._repo()
        ok = False
        note = ""
        result: dict[str, int] = {}
        err = ERROR_STATE

        if space == INVENTORY_LIST_MAIN:
            allowed, allowed_note = _is_allowed_custom_title_slot(
                item_id,
                category,
                book_index,
            )
            if not allowed:
                ok, note = False, allowed_note
            else:
                custom_repo = TitleBookCustomRepository(repo)
                if is_put:
                    ok, note, result = custom_repo.put_from_main(
                        int(cid),
                        source_slot=slot,
                        item_id=item_id,
                        category=category,
                        book_index=book_index,
                    )
                else:
                    ok, note, result = custom_repo.get_to_main(
                        int(cid),
                        target_slot=slot,
                        item_id=item_id,
                        category=category,
                        book_index=book_index,
                    )
        elif is_put:
            # 卸下：仅称号簿内称号回簿；非簿称号（礼包）应走 0013 回背包
            resolved = _resolve_book_slot_for_item(item_id, category, book_index)
            if resolved is None:
                ok, note = False, "no_book_slot_not_titlebook_title"
            else:
                category, book_index = resolved
                ok, note, result = repo.unequip_title_to_titlebook(
                    int(cid),
                    equip_slot=slot,
                    item_id=item_id,
                    category=category,
                    book_index=book_index,
                )
        else:
            # GET：簿 → 装备
            if not _is_valid_category_index(category, book_index):
                resolved = _resolve_book_slot_for_item(item_id, category, book_index)
                if resolved is not None:
                    category, book_index = resolved
            # 客户端 slot 应为 13；若给 0 则按 PVF title 位纠正
            wear = int(slot) & 0xFFFF
            if wear <= 0 or wear > 0xFF:
                wear = TITLE_EQUIP_SLOT_DEFAULT
            # Stage3 live 发 slot=13；自定义槽必须移动完整实例，固定槽沿用既有投影事务。
            if _is_custom_title_slot_definition(item_id, category, book_index):
                allowed, allowed_note = _is_allowed_custom_title_slot(
                    item_id,
                    category,
                    book_index,
                )
                if not allowed:
                    ok, note = False, allowed_note
                else:
                    custom_repo = TitleBookCustomRepository(repo)
                    ok, note, result = custom_repo.equip_from_custom(
                        int(cid),
                        equip_slot=wear,
                        item_id=item_id,
                        category=category,
                        book_index=book_index,
                    )
            else:
                ok, note, result = repo.equip_title_from_titlebook(
                    int(cid),
                    equip_slot=wear,
                    item_id=item_id,
                    category=category,
                    book_index=book_index,
                )
            slot = wear

        if not ok:
            packets = [
                self.server._codec_response(
                    1,
                    op,
                    build_title_book_ack_fail(
                        item_space=space,
                        category=category,
                        error_code=err,
                    ),
                )
            ]
            return packets, base + detail_s + "; FAIL %s" % note

        out_cat = int(result.get("category", category))
        out_idx = int(result.get("book_index", book_index))
        packets = [
            self.server._codec_response(
                1,
                op,
                build_title_book_ack_ok(
                    item_space=space,
                    slot=int(result.get("slot", slot)),
                    category=out_cat,
                    book_index=out_idx,
                ),
            )
        ]
        refresh_note = ""
        if int(result.get("equipment_changed", 0)) or (
            space == INVENTORY_LIST_EQUIPMENT
            and env_bool("USDOF_GAME_TITLEBOOK_ALWAYS_REFRESH_ON_OK", True)
        ):
            extra, refresh_note = _pack_equip_and_appearance(
                self.server,
                character_id=int(cid),
                record=record,
                category=out_cat,
            )
            packets.extend(extra)

        return packets, (
            "%s%s; OK %s item=%s refresh=[%s]"
            % (base, detail_s, note, item_id, refresh_note)
        )
