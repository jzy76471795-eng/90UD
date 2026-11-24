# -*- coding: utf-8 -*-
"""C1/S1 0100 avatar-emblem compound flow for frozen Stage3."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Sequence

from game.character.emblem_compound_repository import (
    EmblemCompoundMaterial,
    apply_emblem_compound,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVENTORY_FULL = 0x04
ERROR_MATERIAL_INVALID = 0x11
ERROR_INVALID_REQUEST = 0x13
MIN_EMBLEM_COUNT = 2
MAX_EMBLEM_COUNT = 5


@dataclass(frozen=True)
class CompoundEmblemRequest:
    materials: tuple[EmblemCompoundMaterial, ...]


def parse_compound_emblem_request(body: bytes) -> CompoundEmblemRequest | None:
    """Parse ``u8 count + count * {u32 emblem_item_id, u16 list0_slot}``."""
    raw = bytes(body or b"")
    if not raw:
        return None
    count = int(raw[0])
    if (
        count < MIN_EMBLEM_COUNT
        or count > MAX_EMBLEM_COUNT
        or len(raw) != 1 + count * 6
    ):
        return None
    materials: list[EmblemCompoundMaterial] = []
    item_by_slot: dict[int, int] = {}
    offset = 1
    for _index in range(count):
        item_id, material_slot = struct.unpack_from("<IH", raw, offset)
        if item_id <= 0:
            return None
        previous_item_id = item_by_slot.get(int(material_slot))
        if previous_item_id is not None and previous_item_id != int(item_id):
            return None
        item_by_slot[int(material_slot)] = int(item_id)
        materials.append(
            EmblemCompoundMaterial(
                material_slot=int(material_slot),
                emblem_item_id=int(item_id),
            )
        )
        offset += 6
    return CompoundEmblemRequest(materials=tuple(materials))


def build_compound_emblem_success(
    result_rows: Sequence[dict[str, Any]],
) -> bytes:
    """Build ``success=1, u8 count, count * {u32 item_id, u32 count}``."""
    rows = list(result_rows or ())
    if not rows or len(rows) > 0xFF:
        raise ValueError("emblem compound result row count is invalid")
    body = bytearray((1, len(rows)))
    seen_ids: set[int] = set()
    for row in rows:
        item_id = int(row.get("item_id", 0) or 0) & 0xFFFFFFFF
        granted_count = int(row.get("granted_count", 0) or 0)
        if (
            item_id <= 0
            or item_id in seen_ids
            or granted_count <= 0
            or granted_count > 0xFFFFFFFF
        ):
            raise ValueError("invalid emblem compound result row=%r" % (row,))
        seen_ids.add(item_id)
        body.extend(struct.pack("<II", item_id, granted_count))
    return bytes(body)


def build_compound_emblem_error(error_code: int) -> bytes:
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


def _error_for_note(note: str) -> int:
    text = str(note or "")
    if text == "inventory_full":
        return ERROR_INVENTORY_FULL
    if (
        text.startswith("material_")
        or text.startswith("mixed_material_grade")
    ):
        return ERROR_MATERIAL_INVALID
    return ERROR_INVALID_REQUEST


class CompoundEmblem0100Service:
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x0100,
            build_compound_emblem_error(error_code),
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (
            note,
            mode,
            error_code,
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_compound_emblem_request(body)
        if request is None:
            return self._error(
                ERROR_INVALID_REQUEST,
                "C1/0100 COMPOUND_EMBLEM parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = "C1/0100 COMPOUND_EMBLEM materials=%s" % (
            [
                (material.emblem_item_id, material.material_slot)
                for material in request.materials
            ],
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(
                ERROR_INVALID_REQUEST,
                base + "; reject=no_character",
            )

        repo = self.server._repo()
        ok, transaction_note, detail = apply_emblem_compound(
            repo,
            character_id=character_id,
            materials=request.materials,
            rng=self.rng,
        )
        if not ok:
            return self._error(
                _error_for_note(transaction_note),
                base + "; transaction_reject=" + transaction_note,
            )

        result_rows = list(detail.get("result_rows") or ())
        try:
            ack_body = build_compound_emblem_success(result_rows)
        except ValueError as exc:
            return self._error(
                ERROR_INVALID_REQUEST,
                "%s; committed_result_encode_error=%s:%s"
                % (base, type(exc).__name__, exc),
            )

        packets: list[bytes] = []
        notes = [base, transaction_note]
        changed_slots = sorted(
            {int(slot) for slot in detail.get("changed_main_slots") or ()}
        )

        # The ACK removes the client's selected material objects.  Apply the
        # final slot projection afterwards so a reward may reuse either slot.
        ack_packet, ack_mode = self._emit(1, 0x0100, ack_body)
        packets.append(ack_packet)
        notes.append(
            "ACK_ok/%s results=%s len=%d"
            % (
                ack_mode,
                [
                    (row["item_id"], row["granted_count"])
                    for row in result_rows
                ],
                len(ack_body),
            )
        )
        try:
            views = []
            for slot in changed_slots:
                row = repo.get_inventory_item_at(
                    character_id,
                    list_type=0,
                    slot_index=slot,
                )
                views.append(_refresh_view(row, slot=slot))
            refresh_body = build_item_list_update_000e(views, list_type=0)
            refresh_packet, refresh_mode = self._emit(0, 0x000E, refresh_body)
            packets.append(refresh_packet)
            notes.append(
                "000E_main/%s rows=%d" % (refresh_mode, len(views))
            )
        except Exception as exc:
            notes.append("refresh_error=%s:%s" % (type(exc).__name__, exc))
        return packets, "; ".join(notes)


def handle_compound_emblem_0100(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CompoundEmblem0100Service(server).handle(body, session=session)


__all__ = [
    "CompoundEmblem0100Service",
    "CompoundEmblemRequest",
    "build_compound_emblem_error",
    "build_compound_emblem_success",
    "handle_compound_emblem_0100",
    "parse_compound_emblem_request",
]
