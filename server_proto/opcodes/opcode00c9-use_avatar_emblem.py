# -*- coding: utf-8 -*-
"""C1/00C9 avatar-emblem insertion for frozen Stage3."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.avatar_emblem_repository import apply_equipped_avatar_emblems
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_SOCKET_MISSING = 0x04
ERROR_EMBLEM_MISSING = 0x11
AVATAR_LIST_TYPE = 1
EQUIPPED_LIST_TYPE = 3
MAX_EMBLEM_COUNT = 5

_socket_helper = load_chain("helper-avatar_socket.py")
_equip_bar_helper = load_chain("helper-equip_itemlist.py")


@dataclass(frozen=True)
class AvatarEmblemChoice:
    material_slot: int
    emblem_item_id: int
    socket_index: int


@dataclass(frozen=True)
class UseAvatarEmblemRequest:
    list_type: int
    target_slot: int
    target_item_id: int
    choices: tuple[AvatarEmblemChoice, ...]


def parse_use_avatar_emblem_request(body: bytes) -> UseAvatarEmblemRequest | None:
    """Parse the Stage3 C1/00C9 writer observed in 2026-07-31 live traffic."""
    raw = bytes(body or b"")
    if len(raw) < 8:
        return None
    list_type, target_slot, target_item_id, count = struct.unpack_from(
        "<BHIB",
        raw,
        0,
    )
    if (
        target_item_id <= 0
        or count <= 0
        or count > MAX_EMBLEM_COUNT
        or len(raw) != 8 + int(count) * 7
    ):
        return None
    choices: list[AvatarEmblemChoice] = []
    # One batch may consume several units from the same emblem stack.
    material_item_by_slot: dict[int, int] = {}
    offset = 8
    for _index in range(int(count)):
        material_slot, emblem_item_id, socket_index = struct.unpack_from(
            "<HIB",
            raw,
            offset,
        )
        if emblem_item_id <= 0 or socket_index >= MAX_EMBLEM_COUNT:
            return None
        previous_item_id = material_item_by_slot.get(int(material_slot))
        if (
            previous_item_id is not None
            and previous_item_id != int(emblem_item_id)
        ):
            return None
        material_item_by_slot[int(material_slot)] = int(emblem_item_id)
        choices.append(
            AvatarEmblemChoice(
                material_slot=int(material_slot),
                emblem_item_id=int(emblem_item_id),
                socket_index=int(socket_index),
            )
        )
        offset += 7
    if len({choice.socket_index for choice in choices}) != len(choices):
        return None
    return UseAvatarEmblemRequest(
        list_type=int(list_type),
        target_slot=int(target_slot),
        target_item_id=int(target_item_id),
        choices=tuple(choices),
    )


def build_use_avatar_emblem_success(
    remaining_by_slot: list[tuple[int, int]] | tuple[tuple[int, int], ...],
) -> bytes:
    """Build the exact success body consumed by Stage3 handler VA 0x0161A750."""
    rows = list(remaining_by_slot)
    if len(rows) > 0xFF:
        raise ValueError("avatar emblem ACK count exceeds u8")
    body = bytearray((1, len(rows)))
    for slot, remaining in rows:
        body.extend(
            struct.pack(
                "<HI",
                int(slot) & 0xFFFF,
                max(0, int(remaining)) & 0xFFFFFFFF,
            )
        )
    return bytes(body)


def build_use_avatar_emblem_error(error_code: int) -> bytes:
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


def _emblem_matches_socket(socket_type: int, target_types: tuple[int, ...]) -> bool:
    target_mask = 0
    for value in target_types:
        target_mask |= int(value) & 0xFFFF
    return bool((int(socket_type) & 0x00FF) & target_mask)


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


def _get_avatar_target(
    repo: Any,
    character_id: int,
    *,
    list_type: int,
    slot: int,
) -> dict[str, Any] | None:
    if int(list_type) == AVATAR_LIST_TYPE:
        return repo.get_inventory_item_at(
            int(character_id),
            list_type=AVATAR_LIST_TYPE,
            slot_index=int(slot),
        )
    if int(list_type) != EQUIPPED_LIST_TYPE:
        return None
    for row in repo.list_equipped_items(int(character_id)):
        if int(row.get("slot", -1)) != int(slot):
            continue
        return {
            "item_template_id": int(row.get("item_id", 0) or 0),
            "extra_json": row.get("extra_json"),
        }
    return None


class UseAvatarEmblem00C9Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x00C9,
            build_use_avatar_emblem_error(error_code),
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (
            note,
            mode,
            error_code,
        )

    def handle_use_avatar_emblem_00c9(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_use_avatar_emblem_request(body)
        if request is None:
            return self._error(
                ERROR_SOCKET_MISSING,
                "C1/00C9 USE_AVATAR_EMBLEM parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = (
            "C1/00C9 USE_AVATAR_EMBLEM list=%d target=%d item=%d choices=%s"
            % (
                request.list_type,
                request.target_slot,
                request.target_item_id,
                [
                    (choice.material_slot, choice.emblem_item_id, choice.socket_index)
                    for choice in request.choices
                ],
            )
        )
        if request.list_type not in {AVATAR_LIST_TYPE, EQUIPPED_LIST_TYPE}:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=list_type")
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=no_character")
        repo = self.server._repo()

        target = _get_avatar_target(
            repo,
            character_id,
            list_type=request.list_type,
            slot=request.target_slot,
        )
        if target is None:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=target_empty")
        if int(target.get("item_template_id") or 0) != request.target_item_id:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=target_mismatch")
        target_meta = resolve_item_metadata(request.target_item_id)
        if not target_meta.is_avatar:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=target_pvf")
        blobs = _socket_helper.resolve_avatar_blobs(
            request.target_item_id,
            target.get("extra_json"),
            metadata=target_meta,
        )
        if blobs is None:
            return self._error(ERROR_SOCKET_MISSING, base + "; reject=no_sockets")
        old_socket_blob, _aux_blob = blobs
        sockets = _socket_helper.decode_socket_blob(old_socket_blob)

        new_socket_blob = bytearray(old_socket_blob)
        for choice in request.choices:
            material = repo.get_inventory_item_at(
                character_id,
                list_type=0,
                slot_index=choice.material_slot,
            )
            if material is None:
                return self._error(
                    ERROR_EMBLEM_MISSING,
                    base + "; reject=emblem_empty slot=%d" % choice.material_slot,
                )
            if int(material.get("item_template_id") or 0) != choice.emblem_item_id:
                return self._error(
                    ERROR_EMBLEM_MISSING,
                    base + "; reject=emblem_mismatch slot=%d" % choice.material_slot,
                )
            emblem_meta = resolve_item_metadata(choice.emblem_item_id)
            target_types = tuple(emblem_meta.avatar_emblem_target_types or ())
            if (
                not emblem_meta.is_stackable
                or not str(emblem_meta.stackable_type).lower().startswith(
                    "[avatar emblem]"
                )
                or not target_types
            ):
                return self._error(
                    ERROR_EMBLEM_MISSING,
                    base + "; reject=emblem_pvf item=%d" % choice.emblem_item_id,
                )
            socket_type, _old_emblem_id = sockets[choice.socket_index]
            if socket_type == 0 or not _emblem_matches_socket(
                socket_type,
                target_types,
            ):
                return self._error(
                    ERROR_SOCKET_MISSING,
                    base
                    + "; reject=socket_type index=%d socket=0x%04X target=%s"
                    % (choice.socket_index, socket_type, target_types),
                )
            struct.pack_into(
                "<I",
                new_socket_blob,
                choice.socket_index * _socket_helper.AVATAR_SOCKET_ENTRY_SIZE + 2,
                choice.emblem_item_id,
            )

        transaction = (
            repo.apply_avatar_emblems
            if request.list_type == AVATAR_LIST_TYPE
            else lambda **kwargs: apply_equipped_avatar_emblems(repo, **kwargs)
        )
        ok, db_note, detail = transaction(
            character_id=character_id,
            target_slot=request.target_slot,
            expected_target_item_id=request.target_item_id,
            expected_target_extra_json=target.get("extra_json"),
            materials=tuple(
                (choice.material_slot, choice.emblem_item_id)
                for choice in request.choices
            ),
            new_socket_blob=bytes(new_socket_blob),
        )
        if not ok:
            error_code = (
                ERROR_EMBLEM_MISSING
                if db_note.startswith("material_")
                else ERROR_SOCKET_MISSING
            )
            return self._error(
                error_code,
                base + "; transaction_reject=" + db_note,
            )

        remaining = dict(detail.get("material_remaining") or {})
        changed_material_slots = tuple(
            dict.fromkeys(choice.material_slot for choice in request.choices)
        )
        ack_rows = [
            (material_slot, int(remaining.get(material_slot, 0)))
            for material_slot in changed_material_slots
        ]
        ack_body = build_use_avatar_emblem_success(ack_rows)
        packets: list[bytes] = []
        ack_packet, ack_mode = self._emit(1, 0x00C9, ack_body)
        packets.append(ack_packet)

        main_views = []
        for material_slot in changed_material_slots:
            row = repo.get_inventory_item_at(
                character_id,
                list_type=0,
                slot_index=material_slot,
            )
            main_views.append(_refresh_view(row, slot=material_slot))
        main_body = build_item_list_update_000e(main_views, list_type=0)
        main_packet, main_mode = self._emit(0, 0x000E, main_body)
        packets.append(main_packet)

        if request.list_type == AVATAR_LIST_TYPE:
            target_after = repo.get_inventory_item_at(
                character_id,
                list_type=AVATAR_LIST_TYPE,
                slot_index=request.target_slot,
            )
            avatar_body = build_item_list_update_000e(
                [_refresh_view(target_after, slot=request.target_slot)],
                list_type=AVATAR_LIST_TYPE,
            )
        else:
            avatar_body, _count, _note = (
                _equip_bar_helper.build_equipment_bar_update_000e_body(
                    repo,
                    character_id,
                )
            )
        avatar_packet, avatar_mode = self._emit(0, 0x000E, avatar_body)
        packets.append(avatar_packet)
        return packets, (
            "%s; OK remaining=%s; ACK/%s len=%d 000E_main/%s "
            "000E_target%d/%s; %s"
            % (
                base,
                ack_rows,
                ack_mode,
                len(ack_body),
                main_mode,
                request.list_type,
                avatar_mode,
                db_note,
            )
        )


__all__ = [
    "AvatarEmblemChoice",
    "UseAvatarEmblem00C9Service",
    "UseAvatarEmblemRequest",
    "build_use_avatar_emblem_error",
    "build_use_avatar_emblem_success",
    "parse_use_avatar_emblem_request",
]
