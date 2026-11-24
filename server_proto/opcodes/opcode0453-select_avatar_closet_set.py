"""C1/0453 handles the UI Change button and applies one closet outfit."""
from __future__ import annotations

import struct
from collections.abc import Iterable
from typing import Any

from game.character.avatar_closet_repository import (
    AvatarClosetMove,
    select_avatar_closet_outfit,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_avatar_item_list_mode0
from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail


SELECT_AVATAR_CLOSET_SET_OPCODE = 0x0453
AVATAR_CLOSET_MOVE_NOTI_OPCODE = 0x043F


def parse_select_avatar_closet_set_request(body: bytes) -> int | None:
    raw = bytes(body or b"")
    return int(raw[0]) if len(raw) == 1 else None


def build_select_avatar_closet_set_result(success: bool) -> bytes:
    return b"\x01" if bool(success) else b"\x00"


def _selected_character_id(session: Any) -> int | None:
    try:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return character_id if character_id > 0 else None


def build_avatar_closet_move_noti(moves: Iterable[AvatarClosetMove]) -> bytes:
    rows = tuple(moves)
    body = bytearray(struct.pack("<I", len(rows)))
    for move in rows:
        source_list = int(move.source_list)
        destination_list = int(move.destination_list)
        source_slot = int(move.source_slot)
        destination_slot = int(move.destination_slot)
        if not 0 <= source_list <= 0xFF or not 0 <= destination_list <= 0xFF:
            raise ValueError("Avatar Closet move list type is outside u8")
        if not 0 <= source_slot <= 0xFFFFFFFF:
            raise ValueError("Avatar Closet source slot is outside u32")
        if not 0 <= destination_slot <= 0xFFFFFFFF:
            raise ValueError("Avatar Closet destination slot is outside u32")
        body.extend(
            struct.pack(
                "<BIBI",
                source_list,
                source_slot,
                destination_list,
                destination_slot,
            )
        )
    return bytes(body)


def _build_avatar_container_snapshot(
    server: Any,
    repo: Any,
    character_id: int,
    list_type: int,
) -> tuple[bytes, int]:
    target_list = int(list_type)
    rows = repo.list_inventory_items(int(character_id), list_type=target_list)
    items = [InventoryItemView(row) for row in rows]
    list_param16 = (
        int(repo.get_container_list_param(int(character_id), list_type=target_list))
        if target_list == int(repo.INVENTORY_LIST_AVATAR)
        else 0
    )
    body = build_avatar_item_list_mode0(
        items,
        list_type=target_list,
        list_param16=list_param16,
    )
    return server._codec_response(0, 0x000D, body), len(items)


def _build_avatar_closet_live_refresh_packets(
    server: Any,
    repo: Any,
    character_id: int,
    session: Any,
) -> tuple[list[bytes], str]:
    packets: list[bytes] = []
    notes: list[str] = []
    for list_type in (
        int(repo.INVENTORY_LIST_AVATAR),
        int(repo.INVENTORY_LIST_AVATAR_CLOSET),
    ):
        packet, count = _build_avatar_container_snapshot(
            server,
            repo,
            int(character_id),
            list_type,
        )
        packets.append(packet)
        notes.append("S0/000D list%d n=%d" % (list_type, count))

    detail = resolve_selected_character_detail(server, session)
    appearance_helper = load_chain("helper-userinfo_equipment.py")
    appearance_packets, appearance_note = (
        appearance_helper.build_userinfo0_appearance_packets(
            server,
            character_id=int(character_id),
            record=detail.get("record"),
            repo=repo,
            session=session,
        )
    )
    packets.extend(appearance_packets)
    notes.append(appearance_note)

    equip_helper = load_chain("helper-equip_itemlist.py")
    equip_body, equip_count, equip_note = (
        equip_helper.build_equipment_bar_update_000e_body(
            repo,
            int(character_id),
        )
    )
    packets.append(server._codec_response(0, 0x000E, equip_body))
    notes.append("S0/000E list3 n=%d %s" % (equip_count, equip_note))
    return packets, "; ".join(note for note in notes if note)


def handle_select_avatar_closet_set_0453(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    target = parse_select_avatar_closet_set_request(body)
    if target is None:
        packet = server._codec_response(
            1,
            SELECT_AVATAR_CLOSET_SET_OPCODE,
            build_select_avatar_closet_set_result(False),
        )
        return [packet], "C1/0453 AVATAR_CLOSET_CHANGE malformed body_len=%d" % len(
            body or b""
        )
    character_id = _selected_character_id(session)
    if character_id is None:
        packet = server._codec_response(
            1,
            SELECT_AVATAR_CLOSET_SET_OPCODE,
            build_select_avatar_closet_set_result(False),
        )
        return [packet], "C1/0453 AVATAR_CLOSET_CHANGE no_selected_character"

    repo = server._repo()
    ok, note, detail = select_avatar_closet_outfit(
        repo,
        character_id,
        target,
    )
    result_packet = server._codec_response(
        1,
        SELECT_AVATAR_CLOSET_SET_OPCODE,
        build_select_avatar_closet_set_result(ok),
    )
    moves = tuple(detail.get("moves", ()) or ())
    packets = [result_packet]
    refresh_note = ""
    if ok and moves:
        packets.append(
            server._codec_response(
                0,
                AVATAR_CLOSET_MOVE_NOTI_OPCODE,
                build_avatar_closet_move_noti(moves),
            )
        )
        refresh_packets, refresh_note = _build_avatar_closet_live_refresh_packets(
            server,
            repo,
            character_id,
            session,
        )
        packets.extend(refresh_packets)
    return packets, (
        "C1/0453 AVATAR_CLOSET_CHANGE cid=%d target=%d success=%d "
        "moves=%d display_preserved=%s returned_to_list1=%s "
        "released_projections=%s detail=%s%s%s"
        % (
            character_id,
            target,
            int(ok),
            len(moves),
            detail.get("current_set_index"),
            detail.get("returned_inventory_slots", []),
            detail.get("released_closet_projection_slots", []),
            note,
            "; S1/0453 -> S0/043F -> authoritative snapshots -> USERINFO -> S0/000E"
            if ok and moves
            else "",
            "; " + refresh_note if refresh_note else "",
        )
    )


__all__ = [
    "AVATAR_CLOSET_MOVE_NOTI_OPCODE",
    "SELECT_AVATAR_CLOSET_SET_OPCODE",
    "build_avatar_closet_move_noti",
    "build_select_avatar_closet_set_result",
    "handle_select_avatar_closet_set_0453",
    "parse_select_avatar_closet_set_request",
]
