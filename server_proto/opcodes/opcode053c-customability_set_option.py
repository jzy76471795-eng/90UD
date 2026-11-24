"""Stage3 C1/S1 053C Aiken dynamic part-set assignment."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.customability_index import (
    CustomAbilityIndex,
    get_customability_index,
)
from game.character.customability_repository import (
    CustomAbilitySetTarget,
    apply_customability_set_assignment,
)
from game.character.customability_state import parse_customability_state
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_REQUEST = 1
ERROR_INSUFFICIENT_MATERIAL = 8
IMPLEMENTATION_ATTEMPT = 3
_ARMOR_TYPES = {"[coat]", "[pants]", "[shoulder]", "[shoes]", "[waist]"}
_set_helper = load_chain("helper-customability_set.py")


@dataclass(frozen=True)
class CustomAbilitySetOptionRequest:
    equipment_slots: tuple[int, ...]
    option_ids: tuple[int, ...]


def parse_customability_set_option_request(
    body: bytes,
) -> CustomAbilitySetOptionRequest | None:
    raw = bytes(body or b"")
    if not raw:
        return None
    option_count = int(raw[0])
    if not 1 <= option_count <= 4:
        return None
    equipment_count_offset = 1 + option_count
    if len(raw) <= equipment_count_offset:
        return None
    option_ids = tuple(int(value) for value in raw[1:equipment_count_offset])
    equipment_count = int(raw[equipment_count_offset])
    expected_size = equipment_count_offset + 1 + equipment_count * 2
    if (
        not 3 <= equipment_count <= 6
        or len(raw) != expected_size
        or any(value == 0 for value in option_ids)
        or len(set(option_ids)) != len(option_ids)
    ):
        return None
    equipment_slots = tuple(
        int(value)
        for value in struct.unpack_from(
            "<%dH" % equipment_count,
            raw,
            equipment_count_offset + 1,
        )
    )
    if len(set(equipment_slots)) != len(equipment_slots):
        return None
    return CustomAbilitySetOptionRequest(equipment_slots, option_ids)


def build_customability_set_option_success() -> bytes:
    return b"\x01"


def build_customability_set_option_error(
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


def _normalized_equipment_type(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


class CustomAbilitySetOption053CService:
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
        body = build_customability_set_option_error(error_code)
        packet, mode = self._emit(1, 0x053C, body)
        return [packet], "%s; ACK053C_fail/%s error=%d" % (
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
        request = parse_customability_set_option_request(body)
        if request is None:
            return self._error(
                "C1/053C CUSTOM_ABILITY_SET_OPTION parse_fail "
                "plain_len=%d plain=%s attempt=%d/3"
                % (
                    len(body or b""),
                    bytes(body or b"")[:32].hex(" "),
                    IMPLEMENTATION_ATTEMPT,
                )
            )
        base = (
            "C1/053C CUSTOM_ABILITY_SET_OPTION slots=%s options=%s attempt=%d/3"
            % (
                request.equipment_slots,
                request.option_ids,
                IMPLEMENTATION_ATTEMPT,
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(base + "; reject=no_character")
        try:
            index = self.index or get_customability_index()
        except RuntimeError as exc:
            return self._error(base + "; reject=index_unavailable:%s" % exc)
        config = index.part_set
        if config is None:
            return self._error(base + "; reject=part_set_index_unavailable")
        if not config.min_part_count <= len(request.equipment_slots) <= config.max_part_count:
            return self._error(base + "; reject=equipment_count")
        if len(request.option_ids) > config.max_option_count:
            return self._error(base + "; reject=option_count")

        groups = tuple(config.group_for_option(value) for value in request.option_ids)
        if any(group is None for group in groups):
            return self._error(base + "; reject=option_unknown")
        group_keys = tuple(group.key for group in groups if group is not None)
        if len(set(group_keys)) != len(group_keys):
            return self._error(base + "; reject=option_group_duplicate")
        for group in groups:
            assert group is not None
            if group.required_part_count > len(request.equipment_slots):
                return self._error(
                    base
                    + "; reject=option_part_count group=%s need=%d"
                    % (group.key, group.required_part_count)
                )

        repo = self.server._repo()
        targets: list[CustomAbilitySetTarget] = []
        equipment_slots: set[int] = set()
        armor_subtypes: set[int] = set()
        grades: list[int] = []
        for bag_slot in request.equipment_slots:
            row = repo.get_inventory_item_at(
                character_id,
                list_type=0,
                slot_index=bag_slot,
            )
            if row is None:
                return self._error(base + "; reject=target_slot_empty slot=%d" % bag_slot)
            item_id = int(row.get("item_template_id") or 0) & 0xFFFFFFFF
            if (
                item_id <= 0
                or str(row.get("item_kind") or "") != "equipment"
                or int(row.get("stack_count") or 0) != 1
            ):
                return self._error(base + "; reject=target_instance slot=%d" % bag_slot)
            metadata = resolve_item_metadata(item_id)
            if (
                not metadata.is_customability_equipment
                or int(metadata.custom_ability_count) != 2
                or int(metadata.custom_ability_part_set_index) != config.part_set_index
            ):
                return self._error(
                    base + "; reject=target_config slot=%d item=%d" % (bag_slot, item_id)
                )
            equipment_slot = metadata.equipment_slot()
            if equipment_slot is None or int(equipment_slot) in equipment_slots:
                return self._error(base + "; reject=wear_slot slot=%d" % bag_slot)
            equipment_slots.add(int(equipment_slot))
            equipment_type = _normalized_equipment_type(metadata.equipment_type)
            if equipment_type in _ARMOR_TYPES:
                subtype = int(metadata.equipment_sub_type)
                if subtype < 0:
                    return self._error(base + "; reject=armor_subtype_missing")
                armor_subtypes.add(subtype)
            state = parse_customability_state(row.get("extra_json"))
            if state is None:
                return self._error(base + "; reject=target_state slot=%d" % bag_slot)
            grades.append(int(state.grade))
            targets.append(CustomAbilitySetTarget(bag_slot, item_id, state))
        if len(armor_subtypes) > 1:
            return self._error(base + "; reject=armor_subtype_mismatch")

        set_grade = sum(grades) // len(grades)
        raw_points = config.total_points(request.option_ids)
        maximum_points = config.maximum_points(set_grade, len(targets))
        effective_points = config.effective_points(
            request.option_ids,
            maximum_points,
        )
        material_count = config.material_count_for_points(effective_points)
        if (
            raw_points is None
            or maximum_points is None
            or effective_points is None
            or effective_points > config.max_point
            or material_count is None
            or material_count <= 0
        ):
            return self._error(
                base
                + "; reject=option_points raw=%s effective=%s max=%s grade=%d"
                % (raw_points, effective_points, maximum_points, set_grade)
            )

        ok, transaction_note, settlement = apply_customability_set_assignment(
            repo,
            character_id=character_id,
            targets=targets,
            part_set_index=config.part_set_index,
            option_ids=request.option_ids,
            set_grade=set_grade,
            material_item_id=config.material_item_id,
            material_count=material_count,
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
                *settlement.changed_inventory_slots,
                *settlement.changed_material_slots,
            }
        )
        final_rows = {
            int(row.get("slot_index") or 0): row
            for row in repo.list_inventory_items(character_id, list_type=0)
        }
        views = [_refresh_view(final_rows.get(slot), slot=slot) for slot in changed_slots]
        refresh_body = build_item_list_update_000e(views, list_type=0)
        refresh_packet, refresh_mode = self._emit(0, 0x000E, refresh_body)

        packets = [refresh_packet]
        equip_refresh_note = "list3_unchanged"
        if settlement.changed_equipment_slots:
            equip_helper = load_chain("helper-equip_itemlist.py")
            equip_body, _equip_count, equip_refresh_note = (
                equip_helper.build_equipment_bar_update_000e_body(
                    repo,
                    character_id,
                )
            )
            equip_packet, _equip_mode = self._emit(0, 0x000E, equip_body)
            packets.append(equip_packet)

        state_body = _set_helper.build_customability_set_state_body(
            character_id,
            settlement.record,
        )
        state_packet, state_mode = self._emit(
            0,
            _set_helper.CUSTOMABILITY_SET_STATE_OPCODE,
            state_body,
        )
        ack_body = build_customability_set_option_success()
        ack_packet, ack_mode = self._emit(1, 0x053C, ack_body)
        packets.extend((state_packet, ack_packet))
        return packets, (
            "%s; OK record=%d config=%d required_parts=%d grade=%d points_raw=%d "
            "points_effective=%d/%d "
            "material=%dx%d remain=%d; 000E_list0/%s slots=%s; "
            "000E_list3 slots=%s %s; "
            "S0/%04X/%s body=%s; ACK053C_ok/%s body=%s; %s"
            % (
                base,
                settlement.record.set_record_id,
                settlement.record.part_set_index,
                settlement.record.required_part_count,
                settlement.record.set_grade,
                raw_points,
                effective_points,
                maximum_points,
                settlement.material_item_id,
                settlement.material_cost,
                settlement.material_remaining,
                refresh_mode,
                changed_slots,
                settlement.changed_equipment_slots,
                equip_refresh_note,
                _set_helper.CUSTOMABILITY_SET_STATE_OPCODE,
                state_mode,
                state_body.hex(" "),
                ack_mode,
                ack_body.hex(" "),
                transaction_note,
            )
        )


def handle_customability_set_option_053c(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CustomAbilitySetOption053CService(server).handle(body, session=session)


__all__ = [
    "CustomAbilitySetOption053CService",
    "CustomAbilitySetOptionRequest",
    "ERROR_INSUFFICIENT_MATERIAL",
    "ERROR_INVALID_REQUEST",
    "build_customability_set_option_error",
    "build_customability_set_option_success",
    "handle_customability_set_option_053c",
    "parse_customability_set_option_request",
]
