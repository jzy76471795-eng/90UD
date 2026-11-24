# -*- coding: utf-8 -*-
"""C1/00EE equipment-design recipe settlement for frozen Stage3.

Stage3 writer ``sub_2944440`` emits exactly::

    u32 recipe_item_id
    u16 requested_count
    i16 card_slot_index       # -1 for product/equipment recipes

The registered response reader ``sub_1626640`` receives the common success
and error bytes from the dispatcher.  Its remaining success body is::

    u8 attempted_output_count
    attempted_output_count * (u32 item_id, u32 count)
    u32 success_count
    u32 failure_count
    u8 trailing_flag

Only PVF recipes declaring ``[maintain ability] normal`` with one equipment
input and one equipment output enter the transform path.  The source script's
valueless ``[maintain socket stat]`` marker may be absent from normalized
reader text, so it is recorded when visible but is not the runtime identity
gate.  The repository retargets the source row in place and settles every
other material in the same SQLite transaction.
"""
from __future__ import annotations

import struct
from typing import Any, Iterable

from game.character.character_db import InventoryGrantSpec
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail
from pvf.compound_item_index import resolve_compound_recipe


COMPOUND_EQUIPMENT_RECIPE_OPCODE = 0x00EE
ERROR_INVENTORY_FULL = 0x04
ERROR_LEVEL_TOO_LOW = 0x0E
ERROR_INVALID_STATE = 0x13
ERROR_MATERIAL_NOT_ENOUGH = 0x15


def parse_compound_equipment_recipe_body(
    plain: bytes,
) -> dict[str, int | str] | None:
    """Parse the frozen Stage3 eight-byte request."""
    data = bytes(plain or b"")
    if len(data) != 8:
        return None
    recipe_item_id, requested_count, card_slot_index = struct.unpack("<IHh", data)
    if recipe_item_id <= 0 or requested_count <= 0 or card_slot_index < -1:
        return None
    return {
        "recipe_item_id": int(recipe_item_id),
        "requested_count": int(requested_count),
        "card_slot_index": int(card_slot_index),
        "raw_hex": data.hex(" "),
    }


def build_compound_equipment_recipe_error(
    error_code: int = ERROR_INVALID_STATE,
) -> bytes:
    """Build the common failure discriminator and Stage3 error byte."""
    return bytes((0, int(error_code) & 0xFF))


def build_compound_equipment_recipe_success(
    attempted_outputs: Iterable[tuple[int, int]],
    *,
    success_count: int,
    failure_count: int,
    trailing_flag: int = 0,
) -> bytes:
    """Build the exact success vector consumed by ``sub_1626640``."""
    outputs = tuple((int(item_id), int(count)) for item_id, count in attempted_outputs)
    if not outputs or len(outputs) > 0xFF:
        raise ValueError("00EE attempted output count must be 1..255")
    body = bytearray((1, len(outputs)))
    for item_id, count in outputs:
        if item_id <= 0 or item_id > 0xFFFFFFFF or count <= 0 or count > 0xFFFFFFFF:
            raise ValueError("00EE attempted output row is outside u32")
        body += struct.pack("<II", item_id, count)
    for field_name, value in (
        ("success_count", int(success_count)),
        ("failure_count", int(failure_count)),
    ):
        if value < 0 or value > 0xFFFFFFFF:
            raise ValueError("00EE %s is outside u32" % field_name)
        body += struct.pack("<I", value)
    body.append(int(trailing_flag) & 0xFF)
    return bytes(body)


def _character_id(session: Any, server: Any) -> int | None:
    if session is not None:
        try:
            character_id = int(getattr(session, "selected_character_id", 0) or 0)
        except (TypeError, ValueError):
            character_id = 0
        if character_id > 0:
            return character_id
    try:
        detail = resolve_selected_character_detail(server, session)
        character_id = int(detail.get("character_id") or 0)
    except Exception:
        return None
    return character_id if character_id > 0 else None


def _error_for_transaction_note(note: str) -> int:
    reason = str(note or "")
    if reason.startswith("container_full"):
        return ERROR_INVENTORY_FULL
    if reason.startswith(("material_insufficient", "gold_insufficient")):
        return ERROR_MATERIAL_NOT_ENOUGH
    return ERROR_INVALID_STATE


class CompoundEquipmentRecipe00EEService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _failure(
        self,
        note: str,
        *,
        error_code: int = ERROR_INVALID_STATE,
    ) -> tuple[list[bytes], str]:
        body = build_compound_equipment_recipe_error(error_code)
        packet, mode = self._emit(1, COMPOUND_EQUIPMENT_RECIPE_OPCODE, body)
        return [packet], "%s; ACK_fail/%s error=%02x" % (note, mode, error_code)

    def _main_refresh(self, repo: Any, character_id: int) -> tuple[bytes, str]:
        rows, list_param16 = repo.load_main_inventory_snapshot(int(character_id))
        body = build_item_list_mode0(
            [InventoryItemView(row) for row in rows],
            list_type=0,
            list_param16=int(list_param16),
        )
        packet, mode = self._emit(0, 0x000D, body)
        return packet, "000D_main/%s entries=%d" % (mode, len(rows))

    def handle_compound_equipment_recipe_00ee(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_COMPOUND_EQUIPMENT_00EE", True):
            return [], "C1/00EE COMPOUND_EQUIPMENT_RECIPE gate=OFF"

        parsed = parse_compound_equipment_recipe_body(body)
        if parsed is None:
            return self._failure(
                "C1/00EE COMPOUND_EQUIPMENT_RECIPE parse_fail plain_len=%d"
                % len(body or b"")
            )
        recipe_item_id = int(parsed["recipe_item_id"])
        requested_count = int(parsed["requested_count"])
        card_slot_index = int(parsed["card_slot_index"])
        base = (
            "C1/00EE COMPOUND_EQUIPMENT_RECIPE recipe=%d count=%d card_slot=%d plain=%s"
            % (
                recipe_item_id,
                requested_count,
                card_slot_index,
                parsed["raw_hex"],
            )
        )
        if card_slot_index != -1:
            return self._failure(base + "; reject=card_recipe_not_supported")
        if requested_count != 1:
            return self._failure(base + "; reject=equipment_count_must_be_one")

        recipe = resolve_compound_recipe(recipe_item_id)
        if recipe is None:
            return self._failure(base + "; reject=recipe_missing")
        try:
            equipment_transform = recipe.resolve_preserved_equipment_transform()
            materials, outputs = recipe.scale(requested_count)
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(base + "; reject=recipe_invalid:%s" % exc)
        if equipment_transform is None:
            return self._failure(base + "; reject=not_preserved_equipment_recipe")

        character_id = _character_id(session, self.server)
        if character_id is None:
            return self._failure(base + "; reject=character_missing")

        repo = self.server._repo()
        ok, transaction_note, _detail = repo.compound_item_recipe(
            int(character_id),
            recipe_item_id=recipe_item_id,
            materials=materials,
            rewards=[
                InventoryGrantSpec(item_id=int(item_id), count=int(count))
                for item_id, count in outputs
            ],
            equipment_transform=equipment_transform,
        )
        if not ok:
            return self._failure(
                base + "; reject=%s" % transaction_note,
                error_code=_error_for_transaction_note(transaction_note),
            )

        ack = build_compound_equipment_recipe_success(
            outputs,
            success_count=requested_count,
            failure_count=0,
            trailing_flag=0,
        )
        packet, mode = self._emit(1, COMPOUND_EQUIPMENT_RECIPE_OPCODE, ack)
        packets = [packet]
        notes = [
            base,
            transaction_note,
            "ACK_ok/%s outputs=%d len=%d" % (mode, len(outputs), len(ack)),
        ]
        try:
            refresh_packet, refresh_note = self._main_refresh(repo, int(character_id))
            packets.append(refresh_packet)
            notes.append(refresh_note)
        except Exception as exc:
            notes.append("refresh_error=%s:%s" % (type(exc).__name__, exc))
        return packets, "; ".join(notes)


def handle_compound_equipment_recipe_00ee(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    """Thin route adapter used by ``opcodes.index``."""
    return CompoundEquipmentRecipe00EEService(
        server
    ).handle_compound_equipment_recipe_00ee(body, session=session)


__all__ = [
    "COMPOUND_EQUIPMENT_RECIPE_OPCODE",
    "CompoundEquipmentRecipe00EEService",
    "ERROR_INVALID_STATE",
    "ERROR_INVENTORY_FULL",
    "ERROR_LEVEL_TOO_LOW",
    "ERROR_MATERIAL_NOT_ENOUGH",
    "build_compound_equipment_recipe_error",
    "build_compound_equipment_recipe_success",
    "handle_compound_equipment_recipe_00ee",
    "parse_compound_equipment_recipe_body",
]
