# -*- coding: utf-8 -*-
"""C1/00CD PVF-backed red-letter option items for frozen Stage3."""
from __future__ import annotations

import random
import struct
from typing import Any

from game.character.amplify_option import (
    AMPLIFY_ACTION_INVEST,
    AMPLIFY_ACTION_PURE_GOLD,
    AMPLIFY_ACTION_REINVEST,
    AMPLIFY_TYPES,
    AMPLIFY_TYPE_UNIDENTIFIED,
    get_amplify_option_config,
    parse_amplify_state,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_UNSUPPORTED = 8
ERROR_INVALID_REQUEST = 17
ERROR_ALREADY_UPGRADED = 18
ERROR_ALREADY_HAS_OPTION = 20
ERROR_NO_OPTION = 21
ERROR_SAME_OPTION = 23

_SUPPORTED_ACTIONS = frozenset(
    {AMPLIFY_ACTION_INVEST, AMPLIFY_ACTION_REINVEST, AMPLIFY_ACTION_PURE_GOLD}
)


def parse_invest_amplify_option_body(plain: bytes) -> dict[str, Any] | None:
    """Parse the verified Stage3 C1/00CD action-dependent writer layout."""
    data = bytes(plain or b"")
    fixed_size = struct.calcsize("<BhIhIB")
    if len(data) < fixed_size:
        return None
    (
        action,
        target_slot,
        target_item_id,
        material_slot,
        material_item_id,
        selected_option,
    ) = struct.unpack_from("<BhIhIB", data, 0)
    if (
        int(action) not in _SUPPORTED_ACTIONS
        or target_slot < 0
        or material_slot < 0
        or target_item_id <= 0
        or material_item_id <= 0
    ):
        return None
    name_raw = b""
    if int(action) == AMPLIFY_ACTION_PURE_GOLD:
        if len(data) < fixed_size + 4:
            return None
        name_length = struct.unpack_from("<I", data, fixed_size)[0]
        end = fixed_size + 4 + int(name_length)
        if name_length > 4096 or end != len(data):
            return None
        name_raw = data[fixed_size + 4:end]
    elif len(data) != fixed_size:
        return None
    return {
        "action": int(action),
        "target_slot": int(target_slot),
        "target_item_id": int(target_item_id) & 0xFFFFFFFF,
        "material_slot": int(material_slot),
        "material_item_id": int(material_item_id) & 0xFFFFFFFF,
        "selected_option": int(selected_option),
        "target_name": name_raw.decode("utf-8", errors="replace"),
        "target_name_raw": name_raw,
        "semantic_length": len(data),
        "raw_length": len(data),
    }


def build_invest_amplify_option_error(error_code: int) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def build_invest_amplify_option_success(
    *,
    action: int,
    material_slot: int,
    material_remaining: int,
    target_slot: int,
    amplify_type: int,
    amplify_value: int,
    amplify_level: int,
) -> bytes:
    """Build the exact Stage3 sub_162B030 success body."""
    body = bytearray((1, int(action) & 0xFF))
    body += struct.pack(
        "<hIhBH",
        int(material_slot),
        max(0, int(material_remaining)) & 0xFFFFFFFF,
        int(target_slot),
        int(amplify_type) & 0xFF,
        int(amplify_value) & 0xFFFF,
    )
    if int(action) == AMPLIFY_ACTION_PURE_GOLD:
        body.append(int(amplify_level) & 0xFF)
    return bytes(body)


def _character_id(session: Any, server: Any) -> int | None:
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
        return character_id if character_id > 0 else None
    except Exception:
        return None


class InvestAmplifyOption00CDService:
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng if rng is not None else random.SystemRandom()

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1, 0x00CD, build_invest_amplify_option_error(error_code)
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (note, mode, error_code)

    def handle_invest_amplify_option_00cd(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_invest_amplify_option_body(body)
        if parsed is None:
            return self._error(
                ERROR_INVALID_REQUEST,
                "C1/00CD INVEST_AMPLIFY_OPTION parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = (
            "C1/00CD INVEST_AMPLIFY_OPTION action=%d target=%d item=%d "
            "material=%d item=%d selected=%d name=%r"
            % (
                parsed["action"], parsed["target_slot"], parsed["target_item_id"],
                parsed["material_slot"], parsed["material_item_id"],
                parsed["selected_option"], parsed["target_name"],
            )
        )
        action = int(parsed["action"])
        if action not in _SUPPORTED_ACTIONS:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=action")
        if int(parsed["selected_option"]) not in AMPLIFY_TYPES:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=selected_option")

        character_id = _character_id(session, self.server)
        if character_id is None:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=no_character")
        repo = self.server._repo()
        target = repo.get_inventory_item_at(
            character_id, list_type=0, slot_index=int(parsed["target_slot"])
        )
        if target is None:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=target_empty")
        target_item_id = int(target.get("item_template_id") or 0) & 0xFFFFFFFF
        if (
            target_item_id != int(parsed["target_item_id"])
            or str(target.get("item_kind") or "") != "equipment"
        ):
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=target_mismatch")
        if int(target.get("seal_flag") or 0) != 0:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=sealed")

        config = get_amplify_option_config()
        rule = config.material_rule(
            action, int(parsed["material_item_id"])
        )
        if rule is None:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=material_config")
        selected_type = rule.selected_type(int(parsed["selected_option"]))
        if selected_type == 0:
            return self._error(ERROR_INVALID_REQUEST, base + "; reject=material_option")

        metadata = resolve_item_metadata(target_item_id)
        if (
            not metadata.is_equipment
            or int(metadata.minimum_level) < int(config.equip_level_const)
            or int(metadata.rarity) < 2
        ):
            return self._error(
                ERROR_UNSUPPORTED,
                base + "; reject=target_pvf level=%d rarity=%d"
                % (int(metadata.minimum_level), int(metadata.rarity)),
            )
        old_amp_type, old_amp_value = parse_amplify_state(target.get("extra_json"))
        if old_amp_type & AMPLIFY_TYPE_UNIDENTIFIED:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=unidentified")
        old_identified_type = old_amp_type & 0x7F
        old_option_value = max(0, int(target.get("option_value") or 0)) & 0xFF
        old_amplify_level = old_option_value & 0x1F
        if action == AMPLIFY_ACTION_INVEST:
            if old_identified_type != 0 or old_amp_value != 0:
                return self._error(
                    ERROR_ALREADY_HAS_OPTION, base + "; reject=already_has_option"
                )
            if old_amplify_level != 0:
                return self._error(
                    ERROR_ALREADY_UPGRADED, base + "; reject=already_upgraded"
                )
        elif action == AMPLIFY_ACTION_REINVEST:
            if old_identified_type not in AMPLIFY_TYPES or old_amp_value <= 0:
                return self._error(ERROR_NO_OPTION, base + "; reject=no_option")
            if old_amplify_level != 0:
                return self._error(
                    ERROR_ALREADY_UPGRADED, base + "; reject=already_upgraded"
                )
        elif old_identified_type not in AMPLIFY_TYPES and old_identified_type != 0:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=invalid_option")
        if old_identified_type == selected_type:
            return self._error(ERROR_SAME_OPTION, base + "; reject=same_option")

        amplify_value = config.initial_attribute_value(metadata.rarity, selected_type)
        if amplify_value <= 0:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=initial_value")
        amplify_level = old_amplify_level
        roll = 0
        roll_total = 0
        if action == AMPLIFY_ACTION_PURE_GOLD:
            try:
                amplify_level, roll, roll_total = config.roll_pure_gold_level(
                    int(parsed["material_item_id"]), self.rng
                )
            except (TypeError, ValueError) as exc:
                return self._error(
                    ERROR_UNSUPPORTED,
                    base + "; reject=random_level:%s" % exc,
                )

        ok, db_note, detail = repo.apply_amplify_option_item(
            action=action,
            character_id=character_id,
            target_slot=int(parsed["target_slot"]),
            expected_item_id=target_item_id,
            expected_option_value=old_option_value,
            expected_amplify_type=old_amp_type,
            expected_amplify_value=old_amp_value,
            material_slot=int(parsed["material_slot"]),
            material_item_id=int(parsed["material_item_id"]),
            material_count=int(rule.material_count),
            amplify_type=selected_type,
            amplify_value=amplify_value,
            amplify_level=amplify_level,
        )
        if not ok:
            return self._error(
                ERROR_INVALID_REQUEST,
                base + "; transaction_reject=%s" % db_note,
            )

        ack = build_invest_amplify_option_success(
            action=action,
            material_slot=int(parsed["material_slot"]),
            material_remaining=int(detail.get("material_remaining") or 0),
            target_slot=int(parsed["target_slot"]),
            amplify_type=selected_type,
            amplify_value=amplify_value,
            amplify_level=amplify_level,
        )
        packets: list[bytes] = []
        ack_packet, ack_mode = self._emit(1, 0x00CD, ack)
        packets.append(ack_packet)

        rows = repo.list_inventory_items(character_id, list_type=0)
        by_slot = {int(row["slot_index"]): row for row in rows}
        refresh_items = []
        refresh_slots = sorted(
            {int(slot) for slot in detail.get("changed_slots", ())}
        )
        for slot in refresh_slots:
            row = by_slot.get(slot) or {
                "slot_index": slot,
                "item_template_id": 0xFFFFFFFF,
                "stack_count": 0,
                "instance_value": 0,
                "item_kind": "unknown",
            }
            refresh_items.append(InventoryItemView(row))
        if refresh_items:
            refresh_packet, refresh_mode = self._emit(
                0,
                0x000E,
                build_item_list_update_000e(refresh_items, list_type=0),
            )
            packets.append(refresh_packet)
        else:
            refresh_mode = "skip"
        return packets, (
            "%s; OK amp=%d:%d level=%d roll=%d/%d material=%dx%d remain=%d; "
            "ACK/%s len=%d 000E_items/%s slots=%s; %s"
            % (
                base, selected_type, amplify_value, amplify_level, roll, roll_total,
                int(parsed["material_item_id"]), int(rule.material_count),
                int(detail.get("material_remaining") or 0), ack_mode, len(ack),
                refresh_mode, refresh_slots, db_note,
            )
        )


__all__ = [
    "InvestAmplifyOption00CDService",
    "build_invest_amplify_option_error",
    "build_invest_amplify_option_success",
    "parse_invest_amplify_option_body",
]
