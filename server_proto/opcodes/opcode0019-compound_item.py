# -*- coding: utf-8 -*-
"""C1/0x0019 ``ENUM_CMDPACKET_COMPOUND_ITEM`` recipe crafting.

Stage3 C1 request (live vectors 2026-08-03 and 2026-08-10)::

    u32 source_value            # recipe item ID or Main inventory slot
    u8  source_is_item_id       # 1=item ID, 0=inventory slot
    u16 requested_count
    u8  reserved_0              # 0
    u8  trailing_1              # 1

The source is a PVF ``[recipe]`` stackable definition, not a consumed bag
item. ``pvf.compound_item_index`` is warmed before listeners start and supplies
the authored ``[input item]`` / ``[output item]`` pairs. The repository then
performs the entire settlement in one transaction.

Stage3 S1/0019 reader ``sub_1624240`` consumes, after the common success byte:

    u8 consumed_count
    consumed_count * (u8 list_type, u16 slot, u32 consumed_quantity)
    u8 reward_count
    reward_count * (u8 list_type, 108B full item row)
"""
from __future__ import annotations

import struct
from typing import Any

from game.character.character_db import InventoryGrantSpec
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import (
    build_avatar_item_list_mode0,
    build_guild_medal_item_list_mode0,
    build_item_list_mode0,
    build_item_list_update_000e,
    build_pet_item_list_mode0,
)
from game.quest.quest_common import env_bool
from game.quest.quest_session import resolve_selected_character_detail
from pvf.compound_item_index import resolve_compound_recipe


# Stage3 ``sub_1624240`` has explicit error-text branches for these values.
# The response still uses its common leading ``u8 success`` discriminator.
ERROR_INVENTORY_FULL = 0x04
ERROR_GOLD_NOT_ENOUGH = 0x0E
ERROR_MATERIAL_NOT_ENOUGH = 0x13
ERROR_INVALID_RECIPE = 0xB3


def parse_compound_item_body(plain: bytes) -> dict[str, int | str] | None:
    """Parse only the frozen Stage3 9-byte compound request form."""
    data = bytes(plain or b"")
    if len(data) != 9:
        return None
    source_value, source_is_item_id, requested_count, reserved_0, trailing_1 = (
        struct.unpack("<IBHBB", data)
    )
    if requested_count <= 0:
        return None
    return {
        "layout": "source_u32_is_item_id_u8_count_u16_reserved_u8_tail_u8",
        "source_value": int(source_value),
        "source_is_item_id": int(source_is_item_id),
        "requested_count": int(requested_count),
        "reserved_0": int(reserved_0),
        "trailing_1": int(trailing_1),
        "raw_hex": data.hex(" "),
    }


def build_compound_item_ack_error(error_code: int = ERROR_INVALID_RECIPE) -> bytes:
    """Stage3 S1/0019 failure: dispatcher reads ``success=0``, then error."""
    return bytes((0, int(error_code) & 0xFF))


def _reward_entry_108(row: dict[str, Any]) -> bytes:
    """Reuse the authoritative list serializer and retain the reader's 108B row."""
    list_type = int(row.get("list_type", 0) or 0) & 0xFF
    body = build_item_list_update_000e(
        [InventoryItemView(row)],
        list_type=list_type,
        avatar=(list_type == 1),
    )
    entry = body[3 : 3 + 108]
    if len(entry) != 108:
        raise ValueError("compound reward entry must be 108B, got %d" % len(entry))
    return entry


def build_compound_item_ack_ok(
    *,
    consumed_updates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    reward_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> bytes:
    """Build the exact success payload consumed by Stage3 ``sub_1624240``."""
    consumed = list(consumed_updates or ())
    rewards = list(reward_rows or ())
    if len(consumed) > 0xFF or len(rewards) > 0xFF:
        raise ValueError("compound ACK row count exceeds u8")
    body = bytearray((1, len(consumed) & 0xFF))
    for row in consumed:
        consumed_count = max(0, int(row.get("consumed_count", 0) or 0))
        if consumed_count <= 0:
            raise ValueError("compound consumed quantity must be positive")
        body += struct.pack(
            "<BHI",
            int(row.get("list_type", 0) or 0) & 0xFF,
            int(row.get("slot_index", 0) or 0) & 0xFFFF,
            consumed_count & 0xFFFFFFFF,
        )
    body.append(len(rewards) & 0xFF)
    for row in rewards:
        list_type = int(row.get("list_type", 0) or 0) & 0xFF
        body.append(list_type)
        body += _reward_entry_108(row)
    return bytes(body)


def _character_id(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            value = getattr(session, attr, None)
            if value is None:
                continue
            try:
                character_id = int(value)
            except (TypeError, ValueError):
                continue
            if character_id > 0:
                return character_id
    try:
        detail = resolve_selected_character_detail(server, session)
        value = detail.get("character_id")
        character_id = int(value) if value is not None else 0
        return character_id if character_id > 0 else None
    except Exception:
        return None


def _error_for_transaction_note(note: str) -> int:
    text = str(note or "")
    if text.startswith("container_full"):
        return ERROR_INVENTORY_FULL
    if text.startswith("gold_insufficient"):
        return ERROR_GOLD_NOT_ENOUGH
    if text.startswith("material_insufficient"):
        return ERROR_MATERIAL_NOT_ENOUGH
    return ERROR_INVALID_RECIPE


class CompoundItem0019Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _failure(
        self,
        base: str,
        *,
        error_code: int = ERROR_INVALID_RECIPE,
    ) -> tuple[list[bytes], str]:
        ack = build_compound_item_ack_error(error_code)
        packet, mode = self._emit(1, 0x0019, ack)
        return [packet], "%s; ACK_fail/%s error=%02x" % (base, mode, error_code)

    def _refresh_changed_lists(
        self,
        *,
        repo: Any,
        character_id: int,
        changed_rows: dict[int, list[dict[str, Any]]],
        selected_character_slot: int = 0,
    ) -> tuple[list[bytes], list[str]]:
        packets: list[bytes] = []
        notes: list[str] = []
        for list_type in sorted(int(value) for value in changed_rows):
            rows = list(changed_rows.get(list_type) or ())
            if not rows:
                continue
            body_update = build_item_list_update_000e(
                [InventoryItemView(row) for row in rows],
                list_type=list_type,
                avatar=(list_type == 1),
            )
            packet, mode = self._emit(0, 0x000E, body_update)
            packets.append(packet)
            notes.append(
                "000E/%s list=%d rows=%d" % (mode, list_type, len(rows))
            )

            all_rows = repo.list_inventory_items(int(character_id), list_type=list_type)
            items = [InventoryItemView(row) for row in all_rows]
            if list_type == 1:
                list_param16 = int(
                    repo.get_container_list_param(int(character_id), list_type=1) or 0
                )
                body_full = build_avatar_item_list_mode0(
                    items,
                    list_param16=list_param16,
                )
            elif list_type == 7:
                body_full = build_pet_item_list_mode0(items)
            elif list_type == repo.INVENTORY_LIST_GUILD_MEDAL:
                body_full = build_guild_medal_item_list_mode0(
                    items,
                    selected_character_slot=selected_character_slot,
                )
            else:
                list_param16 = int(
                    repo.get_container_list_param(
                        int(character_id), list_type=list_type
                    )
                    or 0
                )
                body_full = build_item_list_mode0(
                    items,
                    list_type=list_type,
                    list_param16=list_param16,
                )
            packet, mode = self._emit(0, 0x000D, body_full)
            packets.append(packet)
            notes.append(
                "000D/%s list=%d entries=%d" % (mode, list_type, len(items))
            )
        return packets, notes

    def handle_compound_item_0019(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not env_bool("USDOF_GAME_HANDLE_COMPOUND_ITEM_0019", True):
            return [], "C1/0019 COMPOUND_ITEM gate=OFF"

        parsed = parse_compound_item_body(body)
        if parsed is None:
            return self._failure(
                "C1/0019 COMPOUND_ITEM parse_fail plain_len=%d" % len(body or b"")
            )
        source_value = int(parsed["source_value"])
        source_is_item_id = int(parsed["source_is_item_id"])
        requested_count = int(parsed["requested_count"])
        base = (
            "C1/0019 COMPOUND_ITEM source=%d count=%d source_is_item_id=%d "
            "reserved=%d tail=%d plain=%s"
            % (
                source_value,
                requested_count,
                source_is_item_id,
                int(parsed["reserved_0"]),
                int(parsed["trailing_1"]),
                parsed["raw_hex"],
            )
        )
        if (
            source_is_item_id not in (0, 1)
            or int(parsed["reserved_0"]) != 0
            or int(parsed["trailing_1"]) != 1
        ):
            return self._failure(base + "; reject=unsupported_request_variant")

        character_id = _character_id(session, self.server)
        if character_id is None:
            return self._failure(base + "; reject=character_missing")

        repo = self.server._repo()
        recipe_source_slot: int | None = None
        if source_is_item_id:
            recipe_id = source_value
        else:
            if source_value > 0xFFFF:
                return self._failure(base + "; reject=recipe_source_slot_invalid")
            recipe_source_slot = source_value
            source_row = repo.get_inventory_item_at(
                int(character_id),
                list_type=0,
                slot_index=recipe_source_slot,
            )
            if source_row is None:
                return self._failure(base + "; reject=recipe_source_slot_empty")
            recipe_id = int(source_row.get("item_template_id", 0) or 0)
            if recipe_id <= 0:
                return self._failure(base + "; reject=recipe_source_item_invalid")
        base += "; recipe=%d" % recipe_id

        recipe = resolve_compound_recipe(recipe_id)
        if recipe is None:
            return self._failure(base + "; reject=recipe_missing")
        try:
            materials, outputs = recipe.scale(requested_count)
            equipment_transform = recipe.resolve_preserved_equipment_transform()
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(base + "; reject=%s" % exc)
        if equipment_transform is not None and requested_count != 1:
            return self._failure(base + "; reject=equipment_count_must_be_one")
        grants = [
            InventoryGrantSpec(item_id=item_id, count=count)
            for item_id, count in outputs
        ]
        ok, transaction_note, detail = repo.compound_item_recipe(
            int(character_id),
            recipe_item_id=recipe_id,
            materials=materials,
            rewards=grants,
            equipment_transform=equipment_transform,
            recipe_source_slot=recipe_source_slot,
        )
        if not ok:
            return self._failure(
                base + "; reject=%s" % transaction_note,
                error_code=_error_for_transaction_note(transaction_note),
            )

        ack = build_compound_item_ack_ok(
            consumed_updates=list(detail.get("consumed_updates") or ()),
            reward_rows=list(detail.get("reward_rows") or ()),
        )
        packets: list[bytes] = []
        notes = [base, transaction_note]
        activation_packets: list[bytes] = []
        for premium_type, remaining in tuple(
            (int(row.get("premium_type", 0)), int(row.get("remaining_seconds", 0)))
            for row in (detail.get("premium_activations") or ())
            if int(row.get("premium_type", 0) or 0) > 0
            and int(row.get("remaining_seconds", 0) or 0) > 0
        ):
            if session is not None:
                try:
                    session.account_premium_effects = None
                except Exception:
                    pass
            helper = load_chain("helper-system_reward_activation.py")
            activation_packets.append(
                self._emit(
                    0,
                    0x0042,
                    helper.build_premium_activation_notice(
                        premium_type, remaining
                    ),
                )[0]
            )
            notes.append(
                "0042_premium type=%d remaining=%d"
                % (premium_type, remaining)
            )
        packet, mode = self._emit(1, 0x0019, ack)
        packets.append(packet)
        packets.extend(activation_packets)
        notes.append(
            "ACK_ok/%s consumed=%d rewards=%d len=%d"
            % (
                mode,
                len(detail.get("consumed_updates") or ()),
                len(detail.get("reward_rows") or ()),
                len(ack),
            )
        )
        try:
            refresh_packets, refresh_notes = self._refresh_changed_lists(
                repo=repo,
                character_id=int(character_id),
                changed_rows=dict(detail.get("changed_rows") or {}),
                selected_character_slot=int(
                    getattr(session, "selected_slot", 0) or 0
                ),
            )
            packets.extend(refresh_packets)
            notes.extend(refresh_notes)
        except Exception as exc:
            # The transaction is already durable. Keep the successful ACK so
            # the client leaves the crafting state; a following full Main list
            # arrives on the next ordinary inventory refresh if this rare
            # serialization failure is encountered.
            notes.append("refresh_error=%s:%s" % (type(exc).__name__, exc))
        return packets, "; ".join(notes)


def handle_compound_item_0019(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    """Thin route adapter used by ``opcodes.index``."""
    return CompoundItem0019Service(server).handle_compound_item_0019(
        body,
        session=session,
    )


__all__ = [
    "CompoundItem0019Service",
    "ERROR_GOLD_NOT_ENOUGH",
    "ERROR_INVALID_RECIPE",
    "ERROR_INVENTORY_FULL",
    "ERROR_MATERIAL_NOT_ENOUGH",
    "build_compound_item_ack_error",
    "build_compound_item_ack_ok",
    "handle_compound_item_0019",
    "parse_compound_item_body",
]
