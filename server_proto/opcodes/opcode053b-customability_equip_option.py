"""Stage3 C1/S1 053B Aiken individual-option adjustment."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.character.customability_index import (
    CustomAbilityIndex,
    get_customability_index,
)
from game.character.customability_repository import (
    apply_customability_option_change,
)
from game.character.customability_state import parse_customability_state
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_REQUEST = 1
ERROR_INSUFFICIENT_MATERIAL = 8
IMPLEMENTATION_ATTEMPT = 1
REQUEST_SIZE = 4


@dataclass(frozen=True)
class CustomAbilityEquipOptionRequest:
    equipment_slot: int
    option_line: int
    option_id: int


def parse_customability_equip_option_request(
    body: bytes,
) -> CustomAbilityEquipOptionRequest | None:
    """Parse the exact Stage3 writer: ``u16 slot + u8 line + u8 option``."""
    raw = bytes(body or b"")
    if len(raw) != REQUEST_SIZE:
        return None
    equipment_slot, option_line, option_id = struct.unpack("<HBB", raw)
    if option_line not in (0, 1) or option_id == 0:
        return None
    return CustomAbilityEquipOptionRequest(
        equipment_slot=int(equipment_slot),
        option_line=int(option_line),
        option_id=int(option_id),
    )


def build_customability_equip_option_success() -> bytes:
    return b"\x01"


def build_customability_equip_option_error(
    error_code: int = ERROR_INVALID_REQUEST,
) -> bytes:
    code = int(error_code)
    if not 1 <= code <= 11:
        raise ValueError("customability error code must be between 1 and 11")
    return bytes((0, code))


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
            resolve_selected_character_detail(server, session).get("character_id")
            or 0
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


class CustomAbilityEquipOption053BService:
    def __init__(
        self,
        server: Any,
        *,
        index: CustomAbilityIndex | None = None,
    ) -> None:
        self.server = server
        self.index = index

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(
        self,
        note: str,
        *,
        error_code: int = ERROR_INVALID_REQUEST,
    ) -> tuple[list[bytes], str]:
        body = build_customability_equip_option_error(error_code)
        packet, mode = self._emit(1, 0x053B, body)
        return [packet], "%s; ACK053B_fail/%s error=%d" % (
            note,
            mode,
            int(error_code),
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_customability_equip_option_request(body)
        if request is None:
            return self._error(
                "C1/053B CUSTOM_ABILITY_EQUIP_OPTION parse_fail "
                "plain_len=%d plain=%s attempt=%d/3"
                % (
                    len(body or b""),
                    bytes(body or b"")[:16].hex(" "),
                    IMPLEMENTATION_ATTEMPT,
                )
            )
        base = (
            "C1/053B CUSTOM_ABILITY_EQUIP_OPTION slot=%d line=%d option=%d "
            "attempt=%d/3"
            % (
                request.equipment_slot,
                request.option_line,
                request.option_id,
                IMPLEMENTATION_ATTEMPT,
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(base + "; reject=no_character")

        repo = self.server._repo()
        target = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.equipment_slot,
        )
        if target is None:
            return self._error(base + "; reject=target_slot_empty")
        target_item_id = int(target.get("item_template_id") or 0) & 0xFFFFFFFF
        if (
            target_item_id <= 0
            or str(target.get("item_kind") or "") != "equipment"
            or int(target.get("stack_count") or 0) != 1
        ):
            return self._error(base + "; reject=target_instance_invalid")

        metadata = resolve_item_metadata(target_item_id)
        if (
            not metadata.is_customability_equipment
            or int(metadata.custom_ability_count) != 2
        ):
            return self._error(
                base + "; reject=target_not_customability item=%d" % target_item_id
            )
        state = parse_customability_state(target.get("extra_json"))
        if state is None:
            return self._error(base + "; reject=target_state_missing")
        old_option = int(state.individual_options[request.option_line])
        if old_option == request.option_id:
            return self._error(base + "; reject=option_unchanged")

        try:
            index = self.index or get_customability_index()
        except RuntimeError as exc:
            return self._error(base + "; reject=index_unavailable:%s" % exc)
        if not index.options_share_group(old_option, request.option_id):
            return self._error(
                base
                + "; reject=option_group old=%d new=%d"
                % (old_option, request.option_id)
            )
        material_cost = index.material_cost(request.option_id, state.grade)
        if material_cost is None:
            return self._error(
                base
                + "; reject=material_rule option=%d grade=%d"
                % (request.option_id, state.grade)
            )

        ok, transaction_note, settlement = apply_customability_option_change(
            repo,
            character_id=character_id,
            target_slot=request.equipment_slot,
            expected_target_item_id=target_item_id,
            expected_state=state,
            option_line=request.option_line,
            new_option_id=request.option_id,
            material_cost=material_cost,
        )
        if not ok or settlement is None:
            error_code = (
                ERROR_INSUFFICIENT_MATERIAL
                if str(transaction_note).startswith("material_insufficient")
                else ERROR_INVALID_REQUEST
            )
            return self._error(
                base + "; transaction_reject=" + transaction_note,
                error_code=error_code,
            )

        changed_slots = sorted(
            {
                request.equipment_slot,
                *settlement.changed_material_slots,
            }
        )
        final_rows = {
            int(row.get("slot_index") or 0): row
            for row in repo.list_inventory_items(character_id, list_type=0)
        }
        views = [
            _refresh_view(final_rows.get(slot), slot=slot)
            for slot in changed_slots
        ]
        refresh_body = build_item_list_update_000e(views, list_type=0)
        refresh_packet, refresh_mode = self._emit(0, 0x000E, refresh_body)

        ack_body = build_customability_equip_option_success()
        ack_packet, ack_mode = self._emit(1, 0x053B, ack_body)
        return [refresh_packet, ack_packet], (
            "%s; OK item=%d option=%d->%d grade=%d material=%dx%d remain=%d; "
            "000E_list0/%s slots=%s plain_len=%d; "
            "ACK053B_ok/%s body=%s; %s"
            % (
                base,
                settlement.target_item_id,
                settlement.old_option_id,
                settlement.new_option_id,
                settlement.grade,
                settlement.material_item_id,
                settlement.material_cost,
                settlement.material_remaining,
                refresh_mode,
                changed_slots,
                len(refresh_body),
                ack_mode,
                ack_body.hex(" "),
                transaction_note,
            )
        )


def handle_customability_equip_option_053b(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CustomAbilityEquipOption053BService(server).handle(
        body,
        session=session,
    )


__all__ = [
    "CustomAbilityEquipOption053BService",
    "CustomAbilityEquipOptionRequest",
    "ERROR_INSUFFICIENT_MATERIAL",
    "ERROR_INVALID_REQUEST",
    "build_customability_equip_option_error",
    "build_customability_equip_option_success",
    "handle_customability_equip_option_053b",
    "parse_customability_equip_option_request",
]
