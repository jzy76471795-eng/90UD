"""C1/0064 rename the currently equipped creature and consume its card."""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    SLOT_RANGE_PET_CONSUMABLE,
    resolve_item_metadata,
)
from game.character.payloads import (
    build_creature_item_list,
    build_pet_item_list_mode0,
)
from game.character.pet_rename_repository import (
    PET_LIST_TYPE,
    load_pet_rename_context,
    rename_equipped_creature,
)
from game.quest.quest_session import resolve_selected_character_detail


RENAME_CREATURE_OPCODE = 0x0064
ERROR_INVALID_REQUEST = 1
MAX_CREATURE_NAME_BYTES = 19


def parse_rename_creature_0064(body: bytes) -> dict[str, Any] | None:
    """Parse the observed ``i16 source_slot + u8 list7 + DSTR name`` body."""
    raw = bytes(body or b"")
    if len(raw) < 7:
        return None
    source_slot = int(struct.unpack_from("<h", raw, 0)[0])
    source_list_type = int(raw[2])
    name_length = int(struct.unpack_from("<I", raw, 3)[0])
    if (
        source_list_type != PET_LIST_TYPE
        or not SLOT_RANGE_PET_CONSUMABLE[0]
        <= source_slot
        <= SLOT_RANGE_PET_CONSUMABLE[1]
        or name_length > MAX_CREATURE_NAME_BYTES
        or len(raw) != 7 + name_length
    ):
        return None
    name_bytes = raw[7:]
    if any(value < 0x20 or value == 0x7F for value in name_bytes):
        return None
    return {
        "source_slot": source_slot,
        "source_list_type": source_list_type,
        "name_bytes": name_bytes,
    }


def build_rename_creature_result(
    *,
    success: bool,
    source_slot: int = 0,
    source_list_type: int = PET_LIST_TYPE,
    error_code: int = ERROR_INVALID_REQUEST,
) -> bytes:
    """Build the exact Stage3 S1/0064 common result and success tail."""
    if not success:
        return struct.pack("<BB", 0, int(error_code) & 0xFF)
    return struct.pack(
        "<BhB",
        1,
        int(source_slot),
        int(source_list_type) & 0xFF,
    )


def build_rename_creature_notification(actor_id: int, name_bytes: bytes) -> bytes:
    """Build Stage3 S0/0065: ``u16 actor_id + DSTR creature_name``."""
    raw_name = bytes(name_bytes or b"")
    if len(raw_name) > MAX_CREATURE_NAME_BYTES:
        raise ValueError("creature rename notification exceeds Stage3 reader")
    return struct.pack("<HI", int(actor_id) & 0xFFFF, len(raw_name)) + raw_name


def _is_creature_rename_card(metadata: Any) -> bool:
    stackable_type = " ".join(
        str(getattr(metadata, "stackable_type", "") or "").strip().lower().split()
    )
    return bool(getattr(metadata, "is_stackable", False)) and stackable_type == "[creature] 1"


def _failure(server: Any, note: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        RENAME_CREATURE_OPCODE,
        build_rename_creature_result(success=False),
    )
    return [packet], "%s; S1/0064 success=0 error=%d" % (
        note,
        ERROR_INVALID_REQUEST,
    )


def handle_rename_creature_0064(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_rename_creature_0064(body)
    if parsed is None:
        return _failure(
            server,
            "C1/0064 RENAME_CREATURE reject=malformed body_len=%d"
            % len(body or b""),
        )

    selected = resolve_selected_character_detail(server, session)
    character_id = int(selected.get("character_id") or 0)
    if character_id <= 0:
        return _failure(server, "C1/0064 RENAME_CREATURE reject=no_character")

    repo = server._repo()
    source_slot = int(parsed["source_slot"])
    source_list_type = int(parsed["source_list_type"])
    context = load_pet_rename_context(
        repo,
        character_id,
        source_list_type=source_list_type,
        source_slot=source_slot,
    )
    source_item_id = int(context.get("source_item_id") or 0) & 0xFFFFFFFF
    pet_item_id = int(context.get("pet_item_id") or 0) & 0xFFFFFFFF
    creature_key = int(context.get("creature_key") or 0) & 0xFFFFFFFF
    source_metadata = resolve_item_metadata(source_item_id)
    pet_metadata = resolve_item_metadata(pet_item_id)
    if not _is_creature_rename_card(source_metadata):
        return _failure(
            server,
            "C1/0064 RENAME_CREATURE reject=source_not_pvf_rename_card "
            "character=%d list=%d slot=%d item=%d type=%s"
            % (
                character_id,
                source_list_type,
                source_slot,
                source_item_id,
                str(getattr(source_metadata, "stackable_type", "") or "-")
                .replace("\r", " ")
                .replace("\n", " "),
            ),
        )
    if not bool(getattr(pet_metadata, "is_creature", False)) or creature_key <= 0:
        return _failure(
            server,
            "C1/0064 RENAME_CREATURE reject=pet_not_equipped "
            "character=%d item=%d key=%d"
            % (character_id, pet_item_id, creature_key),
        )

    ok, reason, result = rename_equipped_creature(
        repo,
        character_id,
        source_list_type=source_list_type,
        source_slot=source_slot,
        expected_source_item_id=source_item_id,
        expected_pet_item_id=pet_item_id,
        expected_creature_key=creature_key,
        name_bytes=bytes(parsed["name_bytes"]),
    )
    if not ok:
        return _failure(
            server,
            "C1/0064 RENAME_CREATURE reject=%s character=%d list=%d slot=%d"
            % (reason, character_id, source_list_type, source_slot),
        )

    notification = server._codec_response(
        0,
        0x0065,
        build_rename_creature_notification(
            character_id,
            bytes(parsed["name_bytes"]),
        ),
    )
    ack = server._codec_response(
        1,
        RENAME_CREATURE_OPCODE,
        build_rename_creature_result(
            success=True,
            source_slot=source_slot,
            source_list_type=source_list_type,
        ),
    )
    pet_rows = repo.list_inventory_items(character_id, list_type=PET_LIST_TYPE)
    inventory = server._codec_response(
        0,
        0x000D,
        build_pet_item_list_mode0([InventoryItemView(row) for row in pet_rows]),
    )
    creature_states = repo.list_creature_states(character_id)
    creature_state = server._codec_response(
        0,
        0x0069,
        build_creature_item_list(creature_states),
    )
    return [notification, ack, inventory, creature_state], (
        "C1/0064 RENAME_CREATURE success character=%d source=(%d,%d) "
        "item=%d remain=%d pet=%d key=%d name_bytes=%d; "
        "S0/0065 actor=%d; S1/0064 success=1; "
        "S0/000D list7 n=%d; S0/0069 n=%d"
        % (
            character_id,
            source_list_type,
            source_slot,
            source_item_id,
            int(result.get("source_remaining") or 0),
            pet_item_id,
            creature_key,
            len(bytes(parsed["name_bytes"])),
            character_id & 0xFFFF,
            len(pet_rows),
            len(creature_states),
        )
    )


__all__ = [
    "MAX_CREATURE_NAME_BYTES",
    "RENAME_CREATURE_OPCODE",
    "build_rename_creature_notification",
    "build_rename_creature_result",
    "handle_rename_creature_0064",
    "parse_rename_creature_0064",
]
