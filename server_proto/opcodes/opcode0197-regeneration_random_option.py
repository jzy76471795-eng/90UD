"""Stage3 C1/S1 0197 equipment regeneration (equipment synthesis)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.equipment_regeneration_index import (
    EquipmentRegenerationIndex,
    get_equipment_regeneration_index,
)
from game.character.equipment_regeneration_repository import (
    apply_equipment_regeneration,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import (
    build_item_list_mode0,
    build_item_list_update_000e,
)
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INSUFFICIENT_GOLD = 0x0A
ERROR_INVALID_REQUEST = 0x13
ERROR_INVALID_MATERIAL = 0x16
REQUEST_SIZE = 6

_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_wallet_gold_update_000e = _dungeon_payloads.build_wallet_gold_update_000e


@dataclass(frozen=True)
class RegenerationRandomOptionRequest:
    target_slot: int
    reserved: int
    equipment_category: int


def parse_regeneration_random_option_request(
    body: bytes,
) -> RegenerationRandomOptionRequest | None:
    """Parse the exact Stage3 writer: ``u16 slot + u16 zero + u16 category``."""
    raw = bytes(body or b"")
    if len(raw) != REQUEST_SIZE:
        return None
    target_slot, reserved, category = struct.unpack("<HHH", raw)
    if target_slot <= 0 or target_slot > 400:
        return None
    if reserved != 0 or category < 1 or category > 5:
        return None
    return RegenerationRandomOptionRequest(
        target_slot=int(target_slot),
        reserved=int(reserved),
        equipment_category=int(category),
    )


def build_regeneration_random_option_success(target_slot: int) -> bytes:
    slot = int(target_slot)
    if slot <= 0 or slot > 0xFFFF:
        raise ValueError("regeneration target slot is invalid")
    return bytes((1,)) + struct.pack("<H", slot)


def build_regeneration_random_option_error(
    error_code: int = ERROR_INVALID_REQUEST,
) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


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


def _transaction_error_code(note: str) -> int:
    text = str(note or "")
    if text.startswith("material_"):
        return ERROR_INVALID_MATERIAL
    if text.startswith("gold_"):
        return ERROR_INSUFFICIENT_GOLD
    return ERROR_INVALID_REQUEST


class RegenerationRandomOption0197Service:
    def __init__(
        self,
        server: Any,
        *,
        index: EquipmentRegenerationIndex | None = None,
        rng: Any = None,
    ) -> None:
        self.server = server
        self.index = index
        self.rng = rng

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(
        self,
        note: str,
        *,
        error_code: int = ERROR_INVALID_REQUEST,
    ) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x0197,
            build_regeneration_random_option_error(error_code),
        )
        return [packet], "%s; ACK0197_fail/%s error=%d" % (
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
        request = parse_regeneration_random_option_request(body)
        if request is None:
            return self._error(
                "C1/0197 REGENERATION_RANDOM_OPTION parse_fail plain_len=%d plain=%s"
                % (len(body or b""), bytes(body or b"")[:16].hex(" "))
            )
        base = (
            "C1/0197 REGENERATION_RANDOM_OPTION slot=%d reserved=%d category=%d"
            % (
                request.target_slot,
                request.reserved,
                request.equipment_category,
            )
        )

        detail = resolve_selected_character_detail(self.server, session)
        character_id = int(detail.get("character_id") or 0)
        character_job = int(detail.get("job") or 0)
        if character_id <= 0:
            return self._error(base + "; reject=no_character")

        repo = self.server._repo()
        source = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.target_slot,
        )
        if source is None:
            return self._error(base + "; reject=target_slot_empty")
        source_item_id = int(source.get("item_template_id") or 0) & 0xFFFFFFFF
        if (
            source_item_id <= 0
            or str(source.get("item_kind") or "") != "equipment"
            or int(source.get("stack_count") or 0) != 1
        ):
            return self._error(base + "; reject=target_instance_invalid")

        try:
            index = self.index or get_equipment_regeneration_index()
        except RuntimeError as exc:
            return self._error(base + "; reject=index_unavailable:%s" % exc)
        source_entry = index.entry_for(source_item_id)
        if source_entry is None:
            return self._error(
                base + "; reject=source_not_regenerable item=%d" % source_item_id
            )
        if source_entry.category != request.equipment_category:
            return self._error(
                base
                + "; reject=category_mismatch source=%d request=%d"
                % (source_entry.category, request.equipment_category)
            )

        source_metadata = resolve_item_metadata(source_item_id)
        if (
            not source_metadata.is_equipment
            or int(source_metadata.rarity) != source_entry.rarity
            or int(source_metadata.minimum_level) != source_entry.minimum_level
        ):
            return self._error(
                base + "; reject=source_metadata_mismatch item=%d" % source_item_id
            )

        try:
            result_entry = index.roll(
                category=request.equipment_category,
                rarity=source_entry.rarity,
                minimum_level=source_entry.minimum_level,
                character_job=character_job,
                rng=self.rng,
            )
            material_costs = index.config.material_costs(
                source_entry.rarity,
                source_entry.minimum_level,
            )
            gold_cost = index.config.gold_cost(
                source_entry.rarity,
                source_entry.minimum_level,
                character_job,
            )
        except (RuntimeError, ValueError) as exc:
            return self._error(base + "; reject=rule_resolution:%s" % exc)

        result_metadata = resolve_item_metadata(result_entry.item_id)
        if (
            not result_metadata.is_equipment
            or int(result_metadata.rarity) != source_entry.rarity
            or int(result_metadata.minimum_level) != source_entry.minimum_level
        ):
            return self._error(
                base
                + "; reject=result_metadata_mismatch item=%d"
                % result_entry.item_id
            )

        ok, transaction_note, settlement = apply_equipment_regeneration(
            repo,
            character_id=character_id,
            target_slot=request.target_slot,
            expected_source_item_id=source_item_id,
            result_metadata=result_metadata,
            material_costs=material_costs,
            gold_cost=gold_cost,
        )
        if not ok or settlement is None:
            return self._error(
                base + "; transaction_reject=" + transaction_note,
                error_code=_transaction_error_code(transaction_note),
            )

        packets: list[bytes] = []
        notes = [
            base,
            transaction_note,
            "pool=%d weight=%d result=%d"
            % (
                len(
                    index.candidates(
                        category=request.equipment_category,
                        rarity=source_entry.rarity,
                        minimum_level=source_entry.minimum_level,
                        character_job=character_job,
                    )
                ),
                result_entry.probability_weight,
                result_entry.item_id,
            ),
        ]

        changed_slots = sorted(
            {
                request.target_slot,
                *settlement.changed_material_slots,
            }
        )
        final_inventory_rows = repo.list_inventory_items(
            character_id,
            list_type=0,
        )
        final_rows = {
            int(row.get("slot_index") or 0): row
            for row in final_inventory_rows
        }
        views = []
        for slot in changed_slots:
            views.append(_refresh_view(final_rows.get(int(slot)), slot=slot))
        refresh_body = build_item_list_update_000e(views, list_type=0)
        refresh_packet, refresh_mode = self._emit(0, 0x000E, refresh_body)
        packets.append(refresh_packet)
        notes.append(
            "000E_list0/%s slots=%s plain_len=%d"
            % (refresh_mode, changed_slots, len(refresh_body))
        )

        if settlement.gold_cost > 0:
            wallet_body = build_wallet_gold_update_000e(settlement.updated_gold)
            wallet_packet, wallet_mode = self._emit(0, 0x000E, wallet_body)
            packets.append(wallet_packet)
            notes.append(
                "000E_wallet/%s gold=%d"
                % (wallet_mode, settlement.updated_gold)
            )

        # An occupied-slot 000E leaves Stage3's move-item identity cache on the
        # source template. Rebuild list0 before releasing the 0197 reader.
        full_views = [InventoryItemView(row) for row in final_inventory_rows]
        list_param16 = int(
            repo.get_container_list_param(character_id, list_type=0) or 0
        )
        full_body = build_item_list_mode0(
            full_views,
            list_type=0,
            list_param16=list_param16,
        )
        full_packet, full_mode = self._emit(0, 0x000D, full_body)
        packets.append(full_packet)
        notes.append(
            "000D_list0/%s entries=%d param=%d plain_len=%d"
            % (full_mode, len(full_views), list_param16, len(full_body))
        )

        ack_body = build_regeneration_random_option_success(request.target_slot)
        ack_packet, ack_mode = self._emit(1, 0x0197, ack_body)
        packets.append(ack_packet)
        notes.append(
            "ACK0197_ok/%s body=%s" % (ack_mode, ack_body.hex(" "))
        )
        return packets, "; ".join(notes)


def handle_regeneration_random_option_0197(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return RegenerationRandomOption0197Service(server).handle(
        body,
        session=session,
    )


__all__ = [
    "ERROR_INSUFFICIENT_GOLD",
    "ERROR_INVALID_MATERIAL",
    "ERROR_INVALID_REQUEST",
    "RegenerationRandomOption0197Service",
    "RegenerationRandomOptionRequest",
    "build_regeneration_random_option_error",
    "build_regeneration_random_option_success",
    "handle_regeneration_random_option_0197",
    "parse_regeneration_random_option_request",
]
