"""90US weekly attendance request and native inventory update."""
from __future__ import annotations

import sqlite3
import struct

from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_update_000e
from game.quest.weekly_attendance import Action, AttendanceError, MILESTONE_ITEMS, settle


OPCODE = 0x0458
REQUEST = struct.Struct("<3sBB")
STATUS = struct.Struct("<3sBBBBHB")
VERSION = b"WA1"


def _status_body(status) -> bytes:
    signed = sum(1 << (week - 1) for week in status.signed_weeks)
    milestones = sum(1 << index for index, week in enumerate(MILESTONE_ITEMS)
                     if week in status.claimed_milestones)
    return b"\x01" + STATUS.pack(VERSION, status.week, status.clears,
                                status.makeup_credits, status.makeup_week, signed, milestones)


def handle(server, body: bytes, *, session=None):
    # Stage3's cipher delivers this five-byte command in one eight-byte block.
    if len(body) not in (REQUEST.size, 8) or any(body[REQUEST.size:]):
        return [], "C1/0458 WEEKLY_ATTENDANCE ignored=unknown_body"
    magic, command, week = REQUEST.unpack_from(body)
    if magic != VERSION:
        return [], "C1/0458 WEEKLY_ATTENDANCE ignored=unknown_version"
    if (not getattr(session, "enter_seria_settled", False)
            or getattr(session, "in_dungeon_run", False)
            or getattr(session, "in_dungeon_select", False)):
        return [], "C1/0458 WEEKLY_ATTENDANCE rejected=not_in_town"

    try:
        action = Action(command)
        has_expected_week = action in (Action.SIGN_WEEK, Action.MAKE_UP)
        if (has_expected_week and not 1 <= week <= 10) or (not has_expected_week and week):
            raise ValueError("invalid expected week")
        status, slots = settle(server, session, action, week)
    except AttendanceError as exc:
        return [server._codec_response(1, OPCODE, b"\x00\x02")], f"C1/0458 WEEKLY_ATTENDANCE {exc}"
    except (ValueError, sqlite3.Error) as exc:
        return [server._codec_response(1, OPCODE, b"\x00\x16")], f"C1/0458 WEEKLY_ATTENDANCE {exc}"

    packets = [server._codec_response(1, OPCODE, _status_body(status))]
    if slots:
        repo = server._repo()
        rows = [repo.get_inventory_item_at(session.selected_character_id, list_type=0, slot_index=slot)
                for slot in sorted(set(slots))]
        updates = [InventoryItemView(row) for row in rows if row is not None]
        if updates:
            packets.append(server._codec_response(
                0, 0x000E, build_item_list_update_000e(updates, list_type=0),
            ))
    return packets, f"C1/0458 WEEKLY_ATTENDANCE action={action.name} week={status.week} awarded={len(slots)}"
