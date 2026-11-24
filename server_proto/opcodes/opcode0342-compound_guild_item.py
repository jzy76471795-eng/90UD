"""C1/S1 0342 guardian-gem compound flow and shared wire service."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.character.guild_item_compound_repository import (
    apply_guild_item_compound,
)
from game.character.guild_item_compound_index import KIND_GUARDIAN_GEM
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import GUILD_MEDAL_INVENTORY_LIST_TYPE
from game.character.payloads import (
    build_guild_medal_full_list36,
    build_item_list_update_000e,
)
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_REQUEST = 0x04
REQUEST_SIZE = 37


@dataclass(frozen=True)
class CompoundGuildItemRequest:
    material_count: int
    client_category: int
    material_slots: tuple[int, ...]


def parse_compound_guild_item_request(
    body: bytes,
) -> CompoundGuildItemRequest | None:
    """Parse the frozen Stage3 37-byte C1/0342 writer layout."""
    raw = bytes(body or b"")
    if len(raw) != REQUEST_SIZE:
        return None
    material_count, client_category = struct.unpack_from("<ii", raw, 13)
    all_slots = struct.unpack_from("<4i", raw, 21)
    if material_count not in (2, 3, 4):
        return None
    selected = tuple(int(slot) for slot in all_slots[:material_count])
    if any(slot < 0 for slot in selected):
        return None
    return CompoundGuildItemRequest(
        material_count=int(material_count),
        client_category=int(client_category),
        material_slots=selected,
    )


def build_compound_guild_item_success(result_item_id: int) -> bytes:
    item_id = int(result_item_id) & 0xFFFFFFFF
    if item_id <= 0:
        raise ValueError("guild item compound result item id is invalid")
    return bytes((1,)) + struct.pack("<I", item_id)


def build_compound_guild_item_error(
    error_code: int = ERROR_INVALID_REQUEST,
) -> bytes:
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
            resolve_selected_character_detail(server, session).get(
                "character_id"
            )
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


class CompoundGuildItemService:
    def __init__(
        self,
        server: Any,
        *,
        opcode: int,
        expected_kind: str,
        operation_name: str,
        success_ack_after_refresh: bool = False,
        rng: Any = None,
    ) -> None:
        self.server = server
        self.opcode = int(opcode) & 0xFFFF
        self.expected_kind = str(expected_kind)
        self.operation_name = str(operation_name)
        self.success_ack_after_refresh = bool(success_ack_after_refresh)
        self.rng = rng

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            self.opcode,
            build_compound_guild_item_error(),
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (
            note,
            mode,
            ERROR_INVALID_REQUEST,
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_compound_guild_item_request(body)
        if request is None:
            return self._error(
                "C1/%04X %s parse_fail plain_len=%d"
                % (self.opcode, self.operation_name, len(body or b""))
            )
        base = (
            "C1/%04X %s count=%d category=%d slots=%s"
            % (
                self.opcode,
                self.operation_name,
                request.material_count,
                request.client_category,
                list(request.material_slots),
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(base + "; reject=no_character")

        repo = self.server._repo()
        ok, transaction_note, detail = apply_guild_item_compound(
            repo,
            character_id=character_id,
            material_slots=request.material_slots,
            expected_kind=self.expected_kind,
            rng=self.rng,
        )
        if not ok:
            return self._error(
                base + "; transaction_reject=" + transaction_note
            )

        result_item_id = int(detail.get("result_item_id") or 0)
        ack_body = build_compound_guild_item_success(result_item_id)

        packets: list[bytes] = []
        notes = [base, transaction_note]

        def append_success_ack() -> None:
            ack_packet, ack_mode = self._emit(1, self.opcode, ack_body)
            packets.append(ack_packet)
            notes.append(
                "ACK_ok/%s item=%d rarity=%d->%d"
                % (
                    ack_mode,
                    result_item_id,
                    int(detail.get("source_rarity", -1)),
                    int(detail.get("result_rarity", -1)),
                )
            )

        if not self.success_ack_after_refresh:
            append_success_ack()

        changed_slots = sorted(
            {
                int(slot)
                for slot in detail.get("changed_list36_slots") or ()
            }
        )
        try:
            views = []
            for slot in changed_slots:
                row = repo.get_inventory_item_at(
                    character_id,
                    list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
                    slot_index=slot,
                )
                views.append(_refresh_view(row, slot=slot))
            refresh_body = build_item_list_update_000e(
                views,
                list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            )
            refresh_packet, refresh_mode = self._emit(
                0,
                0x000E,
                refresh_body,
            )
            packets.append(refresh_packet)
            notes.append(
                "000E_list36/%s rows=%d" % (refresh_mode, len(views))
            )
        except Exception as exc:
            notes.append("refresh_error=%s:%s" % (type(exc).__name__, exc))

        try:
            all_rows = repo.list_inventory_items(
                character_id,
                list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            )
            full_body = build_guild_medal_full_list36(
                [InventoryItemView(row) for row in all_rows]
            )
            full_packet, full_mode = self._emit(0, 0x000D, full_body)
            packets.append(full_packet)
            notes.append(
                "000D_list36_plain_full/%s entries=%d"
                % (full_mode, len(all_rows))
            )
        except Exception as exc:
            notes.append(
                "full_refresh_error=%s:%s" % (type(exc).__name__, exc)
            )
        if self.success_ack_after_refresh:
            append_success_ack()
        return packets, "; ".join(notes)


class CompoundGuildItem0342Service(CompoundGuildItemService):
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        super().__init__(
            server,
            opcode=0x0342,
            expected_kind=KIND_GUARDIAN_GEM,
            operation_name="COMPOUND_GUARDIAN_GEM",
            success_ack_after_refresh=True,
            rng=rng,
        )


def handle_compound_guild_item_0342(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CompoundGuildItem0342Service(server).handle(body, session=session)


__all__ = [
    "CompoundGuildItem0342Service",
    "CompoundGuildItemService",
    "CompoundGuildItemRequest",
    "ERROR_INVALID_REQUEST",
    "REQUEST_SIZE",
    "build_compound_guild_item_error",
    "build_compound_guild_item_success",
    "handle_compound_guild_item_0342",
    "parse_compound_guild_item_request",
]
