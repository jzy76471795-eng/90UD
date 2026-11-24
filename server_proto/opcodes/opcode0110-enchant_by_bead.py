# -*- coding: utf-8 -*-
"""C1/0110 bead enchantment for the frozen Stage3 client."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.enchant_by_bead import (
    parse_enchant_upgrade_count,
    resolve_enchant_by_bead,
)
from game.character.enchant_repository import apply_enchant_by_bead
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import (
    build_item_list_mode0,
    build_pet_item_list_mode0,
)
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_BEAD = 0x11
ERROR_INVALID_TARGET = 0x13
ERROR_UNSUPPORTED = 0x17

MAIN_LIST_TYPE = 0
EQUIPMENT_LIST_TYPE = 3
PET_LIST_TYPE = 7
SUPPORTED_TARGET_LIST_TYPES = frozenset(
    {MAIN_LIST_TYPE, EQUIPMENT_LIST_TYPE, PET_LIST_TYPE}
)

_equip_bar = load_chain("helper-equip_itemlist.py")


@dataclass(frozen=True)
class EnchantByBeadRequest:
    bead_list_type: int
    bead_slot: int
    target_list_type: int
    target_slot: int


def parse_enchant_by_bead_request(body: bytes) -> EnchantByBeadRequest | None:
    """Parse the exact Stage3 writer layout: u8/u16/u8/u16."""
    raw = bytes(body or b"")
    if len(raw) != 6:
        return None
    bead_list_type, bead_slot, target_list_type, target_slot = struct.unpack(
        "<BHBH",
        raw,
    )
    return EnchantByBeadRequest(
        bead_list_type=int(bead_list_type),
        bead_slot=int(bead_slot),
        target_list_type=int(target_list_type),
        target_slot=int(target_slot),
    )


def build_enchant_by_bead_success(
    *,
    target_list_type: int,
    target_slot: int,
) -> bytes:
    """Build the exact body consumed by Stage3 success reader VA 0x0160AF80."""
    return struct.pack(
        "<BBH",
        1,
        int(target_list_type) & 0xFF,
        int(target_slot) & 0xFFFF,
    )


def build_enchant_by_bead_error(error_code: int) -> bytes:
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
            resolve_selected_character_detail(server, session).get("character_id")
            or 0
        )
    except Exception:
        return None
    return character_id if character_id > 0 else None


def _target_row(
    repo: Any,
    character_id: int,
    *,
    list_type: int,
    slot: int,
) -> dict[str, Any] | None:
    if int(list_type) in {MAIN_LIST_TYPE, PET_LIST_TYPE}:
        return repo.get_inventory_item_at(
            int(character_id),
            list_type=int(list_type),
            slot_index=int(slot),
        )
    if int(list_type) != EQUIPMENT_LIST_TYPE:
        return None
    for row in repo.list_equipped_items(int(character_id)):
        if int(row.get("slot", -1)) != int(slot):
            continue
        return {
            "slot_index": int(row.get("slot", 0) or 0),
            "item_template_id": int(row.get("item_id", 0) or 0),
            "item_kind": "equipment",
            "stack_count": 1,
            "extra_json": row.get("extra_json"),
        }
    return None


def _transaction_error_code(note: str) -> int:
    reason = str(note or "")
    if reason.startswith("bead_"):
        return ERROR_INVALID_BEAD
    if reason.startswith("target_list_"):
        return ERROR_UNSUPPORTED
    return ERROR_INVALID_TARGET


class EnchantByBead0110Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x0110,
            build_enchant_by_bead_error(error_code),
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (
            note,
            mode,
            error_code,
        )

    def _main_refresh(
        self,
        repo: Any,
        character_id: int,
    ) -> tuple[bytes, str]:
        rows, list_param16 = repo.load_main_inventory_snapshot(int(character_id))
        body = build_item_list_mode0(
            [InventoryItemView(row) for row in rows],
            list_type=MAIN_LIST_TYPE,
            list_param16=int(list_param16),
        )
        packet, mode = self._emit(0, 0x000D, body)
        return packet, "000D_main/%s entries=%d" % (mode, len(rows))

    def _target_refresh(
        self,
        repo: Any,
        character_id: int,
        target_list_type: int,
    ) -> tuple[bytes, str]:
        if int(target_list_type) == PET_LIST_TYPE:
            rows = repo.list_inventory_items(
                int(character_id),
                list_type=PET_LIST_TYPE,
            )
            body = build_pet_item_list_mode0(
                [InventoryItemView(row) for row in rows]
            )
            packet, mode = self._emit(0, 0x000D, body)
            return packet, "000D_pet/%s entries=%d" % (mode, len(rows))
        if int(target_list_type) == EQUIPMENT_LIST_TYPE:
            body, count, _note = _equip_bar.build_equipment_bar_update_000e_body(
                repo,
                int(character_id),
            )
            packet, mode = self._emit(0, 0x000E, body)
            return packet, "000E_equipment/%s entries=%d" % (mode, count)
        raise ValueError("target refresh requested for unsupported list type")

    def handle_enchant_by_bead_0110(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_enchant_by_bead_request(body)
        if request is None:
            return self._error(
                ERROR_INVALID_BEAD,
                "C1/0110 ENCHANT_BY_BEAD parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = (
            "C1/0110 ENCHANT_BY_BEAD bead=(%d,%d) target=(%d,%d)"
            % (
                request.bead_list_type,
                request.bead_slot,
                request.target_list_type,
                request.target_slot,
            )
        )
        if request.bead_list_type != MAIN_LIST_TYPE:
            return self._error(
                ERROR_UNSUPPORTED,
                base + "; reject=bead_list_type",
            )
        if request.target_list_type not in SUPPORTED_TARGET_LIST_TYPES:
            return self._error(
                ERROR_UNSUPPORTED,
                base + "; reject=target_list_type",
            )
        if request.bead_slot == 0 or (
            request.target_list_type == request.bead_list_type
            and request.target_slot == request.bead_slot
        ):
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=slot_identity",
            )

        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=no_character",
            )
        repo = self.server._repo()
        bead = repo.get_inventory_item_at(
            character_id,
            list_type=MAIN_LIST_TYPE,
            slot_index=request.bead_slot,
        )
        if bead is None or int(bead.get("stack_count") or 0) <= 0:
            return self._error(
                ERROR_INVALID_BEAD,
                base + "; reject=bead_empty",
            )
        bead_item_id = int(bead.get("item_template_id") or 0) & 0xFFFFFFFF
        bead_metadata = resolve_item_metadata(bead_item_id)
        if (
            not bead_metadata.is_stackable
            or not str(bead_metadata.stackable_type or "")
            .strip()
            .lower()
            .startswith("[enchant waste]")
        ):
            return self._error(
                ERROR_INVALID_BEAD,
                base + "; reject=bead_pvf item=%d" % bead_item_id,
            )

        target = _target_row(
            repo,
            character_id,
            list_type=request.target_list_type,
            slot=request.target_slot,
        )
        if target is None:
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=target_empty",
            )
        target_item_id = int(target.get("item_template_id") or 0) & 0xFFFFFFFF
        target_metadata = resolve_item_metadata(target_item_id)
        if not target_metadata.is_equipment or target_metadata.is_avatar:
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=target_pvf item=%d" % target_item_id,
            )
        if (
            request.target_list_type == PET_LIST_TYPE
            and not target_metadata.is_creature
        ):
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=pet_target_not_creature",
            )

        upgrade_count = parse_enchant_upgrade_count(bead.get("extra_json"))
        try:
            resolution, reject_reason = resolve_enchant_by_bead(
                bead_item_id,
                target_item_id,
                upgrade_count,
                target_metadata=target_metadata,
            )
        except RuntimeError as exc:
            return self._error(
                ERROR_UNSUPPORTED,
                base + "; reject=index_unavailable:%s" % exc,
            )
        if resolution is None:
            error_code = (
                ERROR_INVALID_BEAD
                if reject_reason.startswith("bead_")
                else ERROR_UNSUPPORTED
                if reject_reason.startswith("upgrade_")
                else ERROR_INVALID_TARGET
            )
            return self._error(
                error_code,
                base + "; reject=" + reject_reason,
            )

        ok, db_note, detail = apply_enchant_by_bead(
            repo,
            character_id=character_id,
            bead_list_type=request.bead_list_type,
            bead_slot=request.bead_slot,
            expected_bead_item_id=bead_item_id,
            expected_bead_extra_json=bead.get("extra_json"),
            target_list_type=request.target_list_type,
            target_slot=request.target_slot,
            expected_target_item_id=target_item_id,
            expected_target_extra_json=target.get("extra_json"),
            card_item_id=resolution.card_item_id,
            upgrade_count=resolution.upgrade_count,
        )
        if not ok:
            return self._error(
                _transaction_error_code(db_note),
                base + "; transaction_reject=" + db_note,
            )

        packets: list[bytes] = []
        refresh_notes: list[str] = []
        if request.target_list_type != MAIN_LIST_TYPE:
            target_packet, target_note = self._target_refresh(
                repo,
                character_id,
                request.target_list_type,
            )
            packets.append(target_packet)
            refresh_notes.append(target_note)
        main_packet, main_note = self._main_refresh(repo, character_id)
        packets.append(main_packet)
        refresh_notes.append(main_note)

        ack_body = build_enchant_by_bead_success(
            target_list_type=request.target_list_type,
            target_slot=request.target_slot,
        )
        ack_packet, ack_mode = self._emit(1, 0x0110, ack_body)
        packets.append(ack_packet)
        return packets, (
            "%s; OK target_item=%d card=%d:%d->%d:%d bead_item=%d remain=%d; "
            "%s ACK/%s len=%d; %s"
            % (
                base,
                target_item_id,
                int(detail.get("old_card_item_id") or 0),
                int(detail.get("old_upgrade_count") or 0),
                resolution.card_item_id,
                resolution.upgrade_count,
                bead_item_id,
                int(detail.get("bead_remaining") or 0),
                " ".join(refresh_notes),
                ack_mode,
                len(ack_body),
                db_note,
            )
        )


__all__ = [
    "ERROR_INVALID_BEAD",
    "ERROR_INVALID_TARGET",
    "ERROR_UNSUPPORTED",
    "EnchantByBead0110Service",
    "EnchantByBeadRequest",
    "build_enchant_by_bead_error",
    "build_enchant_by_bead_success",
    "parse_enchant_by_bead_request",
]
