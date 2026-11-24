"""C1/S1 033C guardian-gem insertion for the frozen Stage3 client."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import GUILD_MEDAL_INVENTORY_LIST_TYPE
from game.character.payloads import build_item_list_update_000e
from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_REQUEST = 0x04
ERROR_GUARDIAN_GEM_MISSING = 0x12

_equip_bar = load_chain("helper-equip_itemlist.py")


@dataclass(frozen=True)
class UseGuardianGemRequest:
    equipped_medal_id: int
    material_slot: int
    guardian_gem_id: int
    socket_index: int


def parse_use_guardian_gem_request(body: bytes) -> UseGuardianGemRequest | None:
    """Parse Stage3's exact 11-byte ``u32/u16/u32/u8`` request body."""
    raw = bytes(body or b"")
    if len(raw) != 11:
        return None
    medal_id, material_slot, gem_id, socket_index = struct.unpack("<IHIB", raw)
    if medal_id <= 0 or gem_id <= 0 or socket_index >= 4:
        return None
    return UseGuardianGemRequest(
        equipped_medal_id=int(medal_id),
        material_slot=int(material_slot),
        guardian_gem_id=int(gem_id),
        socket_index=int(socket_index),
    )


def build_use_guardian_gem_success() -> bytes:
    """Stage3 S1/033C success body has no fields after the status byte."""
    return b"\x01"


def build_use_guardian_gem_error(error_code: int) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def _selected_character_id(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            try:
                character_id = int(getattr(session, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if character_id > 0:
                return character_id
    try:
        character_id = int(
            resolve_selected_character_detail(server, session).get("character_id") or 0
        )
    except Exception:
        return None
    return character_id if character_id > 0 else None


def _refresh_view(row: dict[str, Any] | None, *, slot: int) -> InventoryItemView:
    if row is not None:
        return InventoryItemView(row)
    return InventoryItemView(
        {
            "slot_index": int(slot),
            "item_template_id": 0xFFFFFFFF,
            "stack_count": 0,
            "instance_value": 0,
            "item_kind": "unknown",
        }
    )


class UseGuardianGem033CService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        return [
            self._emit(
                1,
                0x033C,
                build_use_guardian_gem_error(error_code),
            )
        ], "%s; ACK_fail error=%d" % (note, int(error_code))

    def handle_use_guardian_gem_033c(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_use_guardian_gem_request(body)
        if request is None:
            return self._error(
                ERROR_INVALID_REQUEST,
                "C1/033C USE_GUARDIAN_GEM parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = (
            "C1/033C USE_GUARDIAN_GEM medal=%d source=%d gem=%d socket=%d"
            % (
                request.equipped_medal_id,
                request.material_slot,
                request.guardian_gem_id,
                request.socket_index,
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=no_character")

        repo = self.server._repo()
        ok, db_note, detail = repo.apply_guardian_gem(
            character_id=character_id,
            equipped_medal_id=request.equipped_medal_id,
            material_slot=request.material_slot,
            guardian_gem_id=request.guardian_gem_id,
            socket_index=request.socket_index,
        )
        if not ok:
            error_code = (
                ERROR_GUARDIAN_GEM_MISSING
                if db_note.startswith("material_")
                else ERROR_INVALID_REQUEST
            )
            return self._error(error_code, base + "; transaction_reject=" + db_note)

        packets = [self._emit(1, 0x033C, build_use_guardian_gem_success())]
        material_after = repo.get_inventory_item_at(
            character_id,
            list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            slot_index=request.material_slot,
        )
        material_body = build_item_list_update_000e(
            [_refresh_view(material_after, slot=request.material_slot)],
            list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
        )
        packets.append(self._emit(0, 0x000E, material_body))

        equipment_body, equipment_count, equipment_note = (
            _equip_bar.build_equipment_bar_update_000e_body(repo, character_id)
        )
        packets.append(self._emit(0, 0x000E, equipment_body))
        return packets, (
            "%s; OK old=%d remaining=%d; ACK + 000E_list36 + "
            "000E_list3 n=%d; %s; %s"
            % (
                base,
                int(detail.get("previous_guardian_gem_id", 0) or 0),
                int(detail.get("material_remaining", 0) or 0),
                int(equipment_count),
                db_note,
                equipment_note,
            )
        )


def handle_use_guardian_gem_033c(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return UseGuardianGem033CService(server).handle_use_guardian_gem_033c(
        body,
        session=session,
    )


__all__ = [
    "ERROR_INVALID_REQUEST",
    "ERROR_GUARDIAN_GEM_MISSING",
    "UseGuardianGemRequest",
    "UseGuardianGem033CService",
    "parse_use_guardian_gem_request",
    "build_use_guardian_gem_success",
    "build_use_guardian_gem_error",
    "handle_use_guardian_gem_033c",
]
