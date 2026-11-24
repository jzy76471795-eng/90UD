# -*- coding: utf-8 -*-
"""C1/035E Stage3 aura-skin slot expansion request.

The frozen client writer at ``0x01A31A50`` emits one ``u32`` (observed as
``0xFFFFFFFF``).  The S1 callback at ``0x01621B40`` consumes the common
``u8 success`` and, on failure, one ``u8 error``.  Slot ownership is carried
by USERINFO1 ``ex_equip_slot_stat & 0x08``; the request value is not an
inventory slot index.
"""
from __future__ import annotations

import struct
from typing import Any, Optional

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_mode0


OPEN_AURA_SKIN_SLOT_OPCODE = 0x035E
ERROR_CANNOT_USE = 1
ERROR_INVALID_REQUEST = 3
OPEN_AURA_SKIN_SLOT_ACTION = "[open aura skin slot]"


def parse_open_aura_skin_slot_request(body: bytes) -> Optional[int]:
    """Return the exact Stage3 ``u32`` request value."""
    raw = bytes(body or b"")
    if len(raw) != 4:
        return None
    return int(struct.unpack_from("<I", raw, 0)[0])


def build_open_aura_skin_slot_result(
    *,
    success: bool,
    error_code: int = 0,
) -> bytes:
    """Build the common success/error arguments consumed by 0x01621B40."""
    if success:
        return b"\x01"
    return struct.pack("<BB", 0, int(error_code) & 0xFF)


def _selected_character_id(server: Any, session: Any = None) -> Optional[int]:
    raw_cid = getattr(session, "selected_character_id", None) if session else None
    try:
        cid = int(raw_cid)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        return cid
    try:
        characters = server._repo().list_by_account(int(server.account_id))
    except Exception:
        return None
    return int(characters[0].character_id) if characters else None


class OpenAuraSkinSlot035EService:
    def __init__(self, server: Any, *, metadata_resolver: Any = None) -> None:
        self.server = server
        # Resolve at construction time so tests and embedded runtimes can
        # replace the shared PVF resolver before the route is dispatched.
        self.metadata_resolver = metadata_resolver or resolve_item_metadata

    def _response(self, body: bytes) -> bytes:
        return self.server._codec_response(1, OPEN_AURA_SKIN_SLOT_OPCODE, body)

    def _failure(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet = self._response(
            build_open_aura_skin_slot_result(
                success=False,
                error_code=error_code,
            )
        )
        return [packet], "%s; S1/035E success=0 error=%d" % (note, error_code)

    def _main_inventory_refresh(
        self,
        repo: Any,
        character_id: int,
    ) -> tuple[bytes, int]:
        rows, list_param16 = repo.load_main_inventory_snapshot(int(character_id))
        body = build_item_list_mode0(
            [InventoryItemView(row) for row in rows],
            list_type=0,
            list_param16=int(list_param16),
        )
        return self.server._codec_response(0, 0x000D, body), len(rows)

    @staticmethod
    def _normalize_action_type(value: object) -> str:
        text = " ".join(str(value or "").strip().lower().split())
        if text.startswith("`") and text.endswith("`") and len(text) >= 2:
            text = " ".join(text[1:-1].strip().lower().split())
        return text

    def _resolve_ticket_item_ids(
        self,
        repo: Any,
        character_id: int,
    ) -> tuple[int, ...]:
        """Resolve usable aura-slot tickets from current PVF item actions.

        The client request carries only a sentinel, so the item identity must
        come from the item template itself.  Looking at the character's live
        Main rows keeps this path independent of historical template IDs while
        still allowing the repository to perform the final atomic check.
        """
        try:
            rows = repo.list_inventory_items(int(character_id), list_type=0)
        except Exception:
            return ()
        candidates: list[tuple[int, int]] = []
        for row in rows or ():
            try:
                item_id = int(row.get("item_template_id") or 0) & 0xFFFFFFFF
                slot = int(row.get("slot_index") or 0)
                count = int(row.get("stack_count") or 0)
                kind = str(row.get("item_kind") or "").strip().lower()
            except (AttributeError, TypeError, ValueError):
                continue
            if item_id <= 0 or slot <= 0 or count <= 0 or kind != "stackable":
                continue
            try:
                metadata = self.metadata_resolver(item_id)
            except Exception:
                continue
            action = self._normalize_action_type(
                getattr(metadata, "action_type", "")
            )
            if action == OPEN_AURA_SKIN_SLOT_ACTION:
                candidates.append((slot, item_id))
        return tuple(
            item_id
            for _slot, item_id in sorted(set(candidates), key=lambda pair: pair[0])
        )

    def handle_open_aura_skin_slot_035e(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request_value = parse_open_aura_skin_slot_request(body)
        if request_value is None:
            return self._failure(
                ERROR_INVALID_REQUEST,
                "OPEN_AURA_SKIN_SLOT/035E malformed body_len=%d" % len(body or b""),
            )

        character_id = _selected_character_id(self.server, session)
        if character_id is None:
            return self._failure(
                ERROR_INVALID_REQUEST,
                "OPEN_AURA_SKIN_SLOT/035E no_character request=0x%08X"
                % request_value,
            )

        repo = self.server._repo()
        ticket_item_ids = self._resolve_ticket_item_ids(repo, int(character_id))
        ok, detail = repo.open_aura_skin_slot(
            int(character_id),
            ticket_item_ids=ticket_item_ids,
        )
        if not ok:
            error_code = (
                ERROR_CANNOT_USE
                if detail.get("reason") == "ticket_missing"
                else ERROR_INVALID_REQUEST
            )
            return self._failure(
                error_code,
                "OPEN_AURA_SKIN_SLOT/035E cid=%d request=0x%08X fail=%s"
                % (character_id, request_value, detail.get("reason")),
            )

        packets = [
            self._response(build_open_aura_skin_slot_result(success=True))
        ]
        inventory_packet, inventory_count = self._main_inventory_refresh(
            repo,
            int(character_id),
        )
        packets.append(inventory_packet)
        return packets, (
            "OPEN_AURA_SKIN_SLOT/035E cid=%d request=0x%08X result=%s "
            "consumed=%d ticket=%d slot=%d remaining=%d state=0x%02X->0x%02X; "
            "S1/035E success=1; S0/000D list0 n=%d"
            % (
                character_id,
                request_value,
                detail.get("reason"),
                int(detail.get("consumed") or 0),
                int(detail.get("ticket_item_id") or 0),
                int(detail.get("ticket_slot") or -1),
                int(detail.get("ticket_remaining") or 0),
                int(detail.get("state_before") or 0) & 0xFF,
                int(detail.get("state_after") or 0) & 0xFF,
                inventory_count,
            )
        )


__all__ = [
    "OPEN_AURA_SKIN_SLOT_OPCODE",
    "OPEN_AURA_SKIN_SLOT_ACTION",
    "OpenAuraSkinSlot035EService",
    "build_open_aura_skin_slot_result",
    "parse_open_aura_skin_slot_request",
]
