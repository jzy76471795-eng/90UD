"""C1/0066 hatch one creature egg in Pet inventory list 7."""
from __future__ import annotations

import struct
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_creature_item_list, build_pet_item_list_mode0
from game.quest.quest_session import resolve_selected_character_detail

HATCH_CREATURE_OPCODE = 0x0066
PET_LIST_TYPE = 7


def parse_hatch_creature_0066(body: bytes) -> dict[str, int] | None:
    raw = bytes(body or b"")
    if len(raw) != 3 or raw[0] != PET_LIST_TYPE:
        return None
    slot = int(struct.unpack_from("<H", raw, 1)[0])
    if slot > 139:
        return None
    return {"list_type": PET_LIST_TYPE, "slot_index": slot}


def build_hatch_creature_ack(success: bool) -> bytes:
    return bytes((1 if success else 0,))


def handle_hatch_creature_0066(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_hatch_creature_0066(body)
    base = "C1/0066 HATCH_CREATURE plain_len=%d" % len(body or b"")
    if parsed is None:
        return [server._codec_response(1, HATCH_CREATURE_OPCODE, build_hatch_creature_ack(False))], base + "; reject=malformed"
    detail = resolve_selected_character_detail(server, session)
    cid = int(detail.get("character_id") or 0)
    if cid <= 0:
        return [server._codec_response(1, HATCH_CREATURE_OPCODE, build_hatch_creature_ack(False))], base + "; reject=no_character"
    repo = server._repo()
    source = repo.get_inventory_item_at(cid, list_type=PET_LIST_TYPE, slot_index=parsed["slot_index"])
    source_id = int(source.get("item_template_id") or 0) if source else 0
    ok, note, result = repo.hatch_pet_egg(
        cid,
        list_type=PET_LIST_TYPE,
        slot_index=parsed["slot_index"],
        expected_item_id=source_id,
    )
    if not ok:
        return [server._codec_response(1, HATCH_CREATURE_OPCODE, build_hatch_creature_ack(False))], base + "; reject=%s" % note
    pet_rows = repo.list_inventory_items(cid, list_type=PET_LIST_TYPE)
    creature_rows = repo.list_creature_states(cid)
    packets = [
        server._codec_response(1, HATCH_CREATURE_OPCODE, build_hatch_creature_ack(True)),
        server._codec_response(0, 0x000D, build_pet_item_list_mode0([InventoryItemView(row) for row in pet_rows])),
        server._codec_response(0, 0x0069, build_creature_item_list(creature_rows)),
    ]
    return packets, (
        "%s; OK slot=%d egg=%d hatched=%d handle=%d equipped_changed=%d; "
        "S1/0066 + S0/000D list7 + S0/0069"
        % (
            base,
            parsed["slot_index"],
            int(result.get("source_item_id") or 0),
            int(result.get("hatched_item_id") or 0),
            int(result.get("creature_key") or 0),
            int(result.get("equipped_changed") or 0),
        )
    )


__all__ = [
    "HATCH_CREATURE_OPCODE",
    "build_hatch_creature_ack",
    "handle_hatch_creature_0066",
    "parse_hatch_creature_0066",
]
