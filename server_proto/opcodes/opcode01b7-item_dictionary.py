# -*- coding: utf-8 -*-
"""C1/01B7 weapon forging for the frozen Stage3 client.

Stage3 ``sub_1B70860`` sends the request. The matching S1/01B7 callback
``sub_1A26C00`` consumes the result and clears the forge-window wait state.
S0/0181 belongs to the quick-party dictionary and must not be used here.
"""
from __future__ import annotations

import random
import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.character.upgrade_config import is_weapon_type
from game.character.upgrade_ticket import (
    UPGRADE_WEIGHT_SCALE,
    find_weapon_forging_ticket_rule,
)
from game.character.weapon_forging import (
    get_weapon_forging_config,
    parse_forging_level,
)
from game.quest.quest_session import resolve_selected_character_detail


_PREFIX_LEN = 9
_LOG_TAIL_LIMIT = 96
FORGING_OUTCOME_SUCCESS = 0
FORGING_OUTCOME_FAILURE = 1

ERROR_INVALID_TARGET = 4
ERROR_CANNOT_FORGE = 0x11
ERROR_INVALID_MATERIAL = 0x16
ERROR_MAX_LEVEL = 0x5F


def build_weapon_forging_success_01b7(
    *,
    material_slot: int,
    material_remaining: int,
    old_level: int,
    outcome: int,
    new_level: int,
    target_list_type: int,
    target_slot: int,
) -> bytes:
    """Build the exact success body consumed by Stage3 ``sub_1A26C00``."""
    return struct.pack(
        "<BHIBBBBH",
        1,
        int(material_slot) & 0xFFFF,
        max(0, int(material_remaining)) & 0xFFFFFFFF,
        int(old_level) & 0xFF,
        int(outcome) & 0xFF,
        int(new_level) & 0xFF,
        int(target_list_type) & 0xFF,
        int(target_slot) & 0xFFFF,
    )


def build_weapon_forging_error_01b7(error_code: int) -> bytes:
    """Build the Stage3 S1/01B7 failure body."""
    return bytes((0, int(error_code) & 0xFF))


def build_weapon_forging_notice_0056(
    *,
    success: bool,
    actor_key: int,
    item_id: int,
    reinforce_level: int,
    forging_level: int,
) -> bytes:
    """Build S0/0056 subtype 12, the Stage3 forging result text."""
    return struct.pack(
        "<BBHIBB",
        12,
        1 if success else 0,
        int(actor_key) & 0xFFFF,
        int(item_id) & 0xFFFFFFFF,
        int(reinforce_level) & 0xFF,
        int(forging_level) & 0xFF,
    )


def parse_item_dictionary_request(body: bytes) -> dict[str, Any]:
    """Parse the Stage3 fixed prefix, DSTR item name, and trailing flag."""
    raw = bytes(body or b"")
    parsed: dict[str, Any] = {
        "raw_len": len(raw),
        "raw_hex": raw[:_LOG_TAIL_LIMIT].hex(" ") + (" ..." if len(raw) > _LOG_TAIL_LIMIT else ""),
        "valid_prefix": False,
        "request_kind": None,
        "request_index": None,
        "selected_item_id": None,
        "selection_value": None,
        "item_name": "",
        "trailing_flag": None,
        "valid_dstr": False,
        "tail_hex": "",
        "tail_len": 0,
    }
    if len(raw) < _PREFIX_LEN:
        return parsed

    parsed.update(
        valid_prefix=True,
        request_kind=int(raw[0]),
        request_index=int.from_bytes(raw[1:3], "little"),
        selected_item_id=int.from_bytes(raw[3:7], "little"),
        selection_value=int.from_bytes(raw[7:9], "little"),
        tail_hex=raw[_PREFIX_LEN : _PREFIX_LEN + _LOG_TAIL_LIMIT].hex(" ")
        + (" ..." if len(raw) - _PREFIX_LEN > _LOG_TAIL_LIMIT else ""),
        tail_len=len(raw) - _PREFIX_LEN,
    )
    if len(raw) < _PREFIX_LEN + 5:
        return parsed
    name_length = int.from_bytes(raw[_PREFIX_LEN:_PREFIX_LEN + 4], "little")
    name_start = _PREFIX_LEN + 4
    name_end = name_start + name_length
    if name_length > 4096 or name_end + 1 != len(raw):
        return parsed
    parsed.update(
        item_name=raw[name_start:name_end].decode("utf-8", errors="replace"),
        trailing_flag=int(raw[name_end]),
        valid_dstr=True,
    )
    return parsed


def _selected_character_id(server: Any, session: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            try:
                value = int(getattr(session, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    try:
        value = int(
            resolve_selected_character_detail(server, session).get("character_id") or 0
        )
    except Exception:
        value = 0
    return value if value > 0 else None


class ItemDictionary01B7Service:
    """Validate and apply the Stage3 weapon forging request."""

    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng if rng is not None else random.SystemRandom()

    def _error(
        self, error_code: int, *, note: str
    ) -> tuple[list[bytes], str]:
        body = build_weapon_forging_error_01b7(error_code)
        packet = self.server._codec_response(1, 0x01B7, body)
        return [packet], "%s; S1/01B7 error=%d len=%d" % (
            note,
            int(error_code) & 0xFF,
            len(body),
        )

    def handle_item_dictionary_01b7(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        parsed = parse_item_dictionary_request(body)
        base = "C1/01B7 ITEM_DICTIONARY plain_len=%d" % parsed["raw_len"]
        if not parsed["valid_prefix"]:
            return self._error(
                ERROR_INVALID_TARGET,
                note="%s; reject=short_prefix raw=%s" % (base, parsed["raw_hex"]),
            )
        if not parsed["valid_dstr"]:
            return self._error(
                ERROR_INVALID_TARGET,
                note="%s; reject=invalid_dstr tail=%s" % (base, parsed["tail_hex"]),
            )
        target_slot = int(parsed["request_index"])
        target_item_id = int(parsed["selected_item_id"])
        material_slot = int(parsed["selection_value"])
        note = (
            "%s; forge kind=%d target_slot=%d item_id=%d material_slot=%d name=%r"
            % (
                base, int(parsed["request_kind"]), target_slot, target_item_id,
                material_slot, parsed["item_name"],
            )
        )
        if int(parsed["request_kind"]) != 0:
            return self._error(
                ERROR_CANNOT_FORGE,
                note=note + "; reject=non_main_inventory",
            )
        cid = _selected_character_id(self.server, session)
        if cid is None:
            return self._error(
                ERROR_INVALID_TARGET,
                note=note + "; reject=no_character",
            )
        repo = self.server._repo()
        target = repo.get_inventory_item_at(cid, list_type=0, slot_index=target_slot)
        if target is None or (
            int(target.get("item_template_id") or 0) & 0xFFFFFFFF
        ) != target_item_id or str(target.get("item_kind") or "") != "equipment":
            return self._error(
                ERROR_INVALID_TARGET,
                note=note + "; reject=target_mismatch",
            )
        metadata = resolve_item_metadata(target_item_id)
        if not metadata.is_equipment or not is_weapon_type(metadata.equipment_type):
            return self._error(
                ERROR_CANNOT_FORGE,
                note=note + "; reject=weapon_only type=%s" % metadata.equipment_type,
            )
        old_level = parse_forging_level(target.get("extra_json"))
        config = get_weapon_forging_config()
        if old_level >= config.max_level:
            return self._error(
                ERROR_MAX_LEVEL,
                note=note + "; reject=max_level=%d" % config.max_level,
            )
        material = repo.get_inventory_item_at(
            cid,
            list_type=0,
            slot_index=material_slot,
        )
        actual_material_item_id = 0
        if material is not None and str(material.get("item_kind") or "") == "stackable":
            actual_material_item_id = (
                int(material.get("item_template_id") or 0) & 0xFFFFFFFF
            )
        ticket_rule = find_weapon_forging_ticket_rule(actual_material_item_id)
        if ticket_rule is not None:
            try:
                character_context = repo.get_character_level_job(cid)
            except Exception:
                character_context = None
            if not isinstance(character_context, tuple) or len(character_context) != 2:
                return self._error(
                    ERROR_INVALID_TARGET,
                    note=note + "; reject=character_eligibility_missing",
                )
            character_level, character_job = (
                int(character_context[0]),
                int(character_context[1]) & 0xFF,
            )
            usable, usable_note = ticket_rule.is_usable(
                level=character_level,
                job=character_job,
                hardcore=bool(
                    session is not None
                    and (
                        getattr(session, "hardcore_mode", False)
                        or getattr(session, "is_hardcore_mode", False)
                    )
                ),
            )
            if not usable:
                return self._error(
                    ERROR_CANNOT_FORGE,
                    note=(
                        note
                        + "; reject=ticket_character_restriction item=%d reason=%s"
                        % (ticket_rule.item_id, usable_note)
                    ),
                )
            if not ticket_rule.allows_target(
                item_id=target_item_id,
                equipment_level=metadata.minimum_level,
            ):
                return self._error(
                    ERROR_CANNOT_FORGE,
                    note=(
                        note
                        + "; reject=ticket_target_restriction item=%d"
                        % ticket_rule.item_id
                    ),
                )
            success_level = ticket_rule.success_level(old_level)
            if success_level is None:
                return self._error(
                    ERROR_MAX_LEVEL,
                    note=(
                        note
                        + "; reject=ticket_level_limit item=%d old_level=%d"
                        % (ticket_rule.item_id, old_level)
                    ),
                )
            if success_level > config.max_level:
                return self._error(
                    ERROR_MAX_LEVEL,
                    note=(
                        note
                        + "; reject=ticket_level_outside_forging_config item=%d "
                        "target=%d max=%d"
                        % (ticket_rule.item_id, success_level, config.max_level)
                    ),
                )
            roll_scale = UPGRADE_WEIGHT_SCALE
            roll = int(self.rng.randrange(roll_scale))
            success_rate = int(ticket_rule.success_weight)
            success = ticket_rule.succeeds_for_roll(roll)
            material_item_id = int(ticket_rule.item_id)
            material_count = 1
            source = "ticket"
        else:
            success_level = old_level + 1
            tier = config.tier_for_target_level(success_level)
            if tier is None:
                return self._error(
                    ERROR_CANNOT_FORGE,
                    note=note + "; reject=missing_tier",
                )
            material_item_id, material_count = config.material_for(
                grade=int(metadata.grade or metadata.minimum_level),
                target_level=success_level,
            )
            roll_scale = 10000
            roll = int(self.rng.randrange(roll_scale))
            success_rate = int(tier.success_rate)
            success = roll < success_rate
            source = "material"
        ok, db_note, detail = repo.apply_weapon_forging(
            character_id=cid,
            target_slot=target_slot,
            expected_item_id=target_item_id,
            expected_old_forging_level=old_level,
            material_slot=material_slot,
            material_item_id=material_item_id,
            material_count=material_count,
            success=success,
            success_level=success_level,
        )
        if not ok:
            error_code = (
                ERROR_INVALID_MATERIAL
                if str(db_note or "").startswith("material_")
                else ERROR_INVALID_TARGET
            )
            return self._error(
                error_code,
                note=note + "; transaction_reject=%s" % db_note,
            )
        new_level = int(detail.get("new_forging_level") or old_level)
        material_remaining = int(detail.get("material_remaining") or 0)
        outcome = (
            FORGING_OUTCOME_SUCCESS if success else FORGING_OUTCOME_FAILURE
        )
        ack_body = build_weapon_forging_success_01b7(
            material_slot=material_slot,
            material_remaining=material_remaining,
            old_level=old_level,
            outcome=outcome,
            new_level=new_level,
            target_list_type=0,
            target_slot=target_slot,
        )
        notice_body = build_weapon_forging_notice_0056(
            success=success,
            actor_key=cid,
            item_id=target_item_id,
            reinforce_level=int(target.get("option_value") or 0) & 0x1F,
            forging_level=new_level,
        )
        packets = [
            self.server._codec_response(1, 0x01B7, ack_body),
            self.server._codec_response(0, 0x0056, notice_body),
        ]
        rows = repo.list_inventory_items(cid, list_type=0)
        rows_by_slot = {int(row["slot_index"]): row for row in rows}
        refresh = []
        for slot in sorted(set(int(slot) for slot in detail.get("changed_slots", ()))):
            row = rows_by_slot.get(slot) or {
                "slot_index": slot, "item_template_id": 0xFFFFFFFF,
                "stack_count": 0, "instance_value": 0, "item_kind": "unknown",
            }
            refresh.append(InventoryItemView(row))
        refresh_body = build_item_list_update_000e(refresh, list_type=0)
        packets.append(self.server._codec_response(0, 0x000E, refresh_body))
        return packets, (
            "%s; S1/01B7 outcome=%d len=%d S0/0056/12 len=%d; "
            "result=%s source=%s level=%d->%d roll=%d/%d success_rate=%d "
            "success=%d "
            "material=%dx%d remain=%d 000E_list0_slots=%s"
            % (
                note, outcome, len(ack_body), len(notice_body), db_note,
                source, old_level, new_level, roll, roll_scale, success_rate,
                int(success), material_item_id, material_count,
                material_remaining,
                sorted(set(int(slot) for slot in detail.get("changed_slots", ()))),
            )
        )


__all__ = [
    "ERROR_CANNOT_FORGE",
    "ERROR_INVALID_MATERIAL",
    "ERROR_INVALID_TARGET",
    "ERROR_MAX_LEVEL",
    "FORGING_OUTCOME_FAILURE",
    "FORGING_OUTCOME_SUCCESS",
    "ItemDictionary01B7Service",
    "build_weapon_forging_error_01b7",
    "build_weapon_forging_notice_0056",
    "build_weapon_forging_success_01b7",
    "parse_item_dictionary_request",
]
