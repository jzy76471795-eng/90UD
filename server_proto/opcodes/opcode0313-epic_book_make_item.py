# -*- coding: utf-8 -*-
"""C1/0313 Epic Book craft request for the frozen Stage3 client.

Stage3 evidence, 2026-07-26:
  * ``sub_BEDB90`` writes one little-endian ``u32`` equipment id to C1/0313
    and records it as the pending Epic Book target.
  * The command-response dispatcher ``sub_1AB3810`` consumes the first result
    byte before invoking the registered reader.  ``sub_1628730`` consumes no
    additional fields; result=1 displays the created equipment, while
    result=0 clears the pending target.

The authoritative resource operation remains in ``game.character.epic_book``.
This module only validates the request and emits the verified client state
transitions plus normal inventory/account-material refreshes.
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.epic_book import (
    EPIC_PIECE_BALANCE_SYNC_OPCODE,
    build_epic_piece_balance_sync_047e,
    craft_epic_book_item,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0


EPIC_BOOK_MAKE_ITEM_OPCODE = 0x0313
_TARGET_ITEM = struct.Struct("<I")


def parse_epic_book_make_item_request(body: bytes) -> int | None:
    """Return the exact Stage3 C1/0313 target equipment id."""
    raw = bytes(body or b"")
    if len(raw) != _TARGET_ITEM.size:
        return None
    target = int(_TARGET_ITEM.unpack(raw)[0])
    return target if target > 0 else None


def build_epic_book_make_item_result_0313(*, success: bool) -> tuple[int, bytes]:
    """Build the complete Stage3 0313 command-response body.

    ``sub_1AB3810`` consumes the common result byte before ``sub_1628730``.
    Failed command responses for opcodes other than 0x0012/0x0014 also consume
    one common error-code byte; 4 is the established material/action failure.
    """
    return 1, (b"\x01" if success else b"\x00\x04")


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return None


def _build_main_inventory_refresh(repo: Any, character_id: int) -> bytes:
    rows = repo.list_inventory_items(int(character_id), list_type=0)
    return build_item_list_mode0(
        [InventoryItemView(row) for row in rows],
        list_type=0,
        list_param16=int(
            repo.get_container_list_param(int(character_id), list_type=0)
        ),
    )


class EpicBookMakeItem0313Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(int(flag), int(opcode), bytes(body))

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        flag, result_body = build_epic_book_make_item_result_0313(success=False)
        return [self._emit(flag, EPIC_BOOK_MAKE_ITEM_OPCODE, result_body)], (
            "C1/0313 EPIC_BOOK_MAKE_ITEM FAIL %s; "
            "S1/0313 result=0 error=4 pending_cleared" % note
        )

    def handle_epic_book_make_item_0313(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        target_item_id = parse_epic_book_make_item_request(body)
        if target_item_id is None:
            return self._failure("malformed plain_len=%d" % len(bytes(body or b"")))

        character_id = _selected_character_id(session)
        if character_id is None:
            return self._failure("selected_character_missing target=%d" % target_item_id)

        repo = self.server._repo()
        ok, craft_note, detail = craft_epic_book_item(
            repo, int(character_id), int(target_item_id)
        )
        if not ok:
            return self._failure(
                "target=%d character=%d %s"
                % (target_item_id, character_id, craft_note)
            )

        # The DB transaction is complete before the client learns about it.
        # Emit the Stage3 success branch first so its result UI consumes the
        # pending target, then converge bag, Soul projections, and fragments.
        flag, result_body = build_epic_book_make_item_result_0313(success=True)
        packets = [self._emit(flag, EPIC_BOOK_MAKE_ITEM_OPCODE, result_body)]
        note = (
            "C1/0313 EPIC_BOOK_MAKE_ITEM OK target=%d character=%d slot=%d %s; "
            "S1/0313 result=1"
            % (
                target_item_id,
                character_id,
                int(detail.get("slot_index", -1)),
                craft_note,
            )
        )

        try:
            inventory_body = _build_main_inventory_refresh(repo, int(character_id))
            packets.append(self._emit(0, 0x000D, inventory_body))

            piece_id = int(detail["piece_id"])
            piece_after = max(0, int(detail["piece_after"]))
            # Incremental 047E entries are absolute.  Include zero explicitly
            # after consumption so the completed card moves to 0/1000 at once.
            piece_body = build_epic_piece_balance_sync_047e(
                {piece_id: piece_after}, reset_client=False
            )
            packets.append(self._emit(0, EPIC_PIECE_BALANCE_SYNC_OPCODE, piece_body))
            note += "; S0/000D list0; S0/047E piece=%d balance=%d" % (
                piece_id,
                piece_after,
            )
        except Exception as exc:
            # The craft has committed.  Keep the Stage3 result packet instead
            # of presenting a retryable failure that could duplicate a craft.
            note += "; refresh_error=%s:%s" % (type(exc).__name__, exc)
        return packets, note


__all__ = [
    "EPIC_BOOK_MAKE_ITEM_OPCODE",
    "EpicBookMakeItem0313Service",
    "build_epic_book_make_item_result_0313",
    "parse_epic_book_make_item_request",
]
