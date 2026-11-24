"""Stage3 C1/S1 01B6 magic-seal option change (attempt 1/3)."""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.magic_seal_random_option_index import (
    MAX_RANDOM_OPTION_COUNT,
    MagicSealRandomOptionIndex,
    get_magic_seal_random_option_index,
)
from game.character.magic_seal_random_option_repository import (
    apply_magic_seal_option_change,
)
from game.character.payloads import build_item_list_update_000e
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INSUFFICIENT_GOLD = 0x0A
ERROR_INVALID_REQUEST = 0x13
REQUEST_SIZE = 5

_random_option = load_chain("helper-random_option.py")
item_random_option_flag = _random_option.item_random_option_flag
inventory_row_random_options = _random_option.inventory_row_random_options
format_options_brief = _random_option.format_options_brief

_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_wallet_gold_update_000e = _dungeon_payloads.build_wallet_gold_update_000e


@dataclass(frozen=True)
class ChangeRandomOptionRequest:
    target_slot: int
    reserved: int
    option_index: int


def parse_change_random_option_request(
    body: bytes,
) -> ChangeRandomOptionRequest | None:
    """Parse the exact Stage3 writer: ``u16 slot + u16 zero + u8 index``."""
    raw = bytes(body or b"")
    if len(raw) != REQUEST_SIZE:
        return None
    target_slot, reserved, option_index = struct.unpack("<HHB", raw)
    if (
        target_slot <= 0
        or target_slot > 400
        or reserved != 0
        or option_index >= MAX_RANDOM_OPTION_COUNT
    ):
        return None
    return ChangeRandomOptionRequest(
        target_slot=int(target_slot),
        reserved=int(reserved),
        option_index=int(option_index),
    )


def build_change_random_option_success() -> bytes:
    return b"\x01"


def build_change_random_option_error(
    error_code: int = ERROR_INVALID_REQUEST,
) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def _is_unsealed(row: dict[str, Any]) -> bool:
    try:
        extra = json.loads(str(row.get("extra_json") or "{}"))
    except (TypeError, ValueError):
        return False
    return bool(isinstance(extra, dict) and extra.get("unsealed"))


def _transaction_error_code(note: str) -> int:
    if str(note or "").startswith("gold_"):
        return ERROR_INSUFFICIENT_GOLD
    return ERROR_INVALID_REQUEST


class ChangeRandomOption01B6Service:
    def __init__(
        self,
        server: Any,
        *,
        index: MagicSealRandomOptionIndex | None = None,
        rng: Any = None,
    ) -> None:
        self.server = server
        self.index = index
        self.rng = rng

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(
        self,
        note: str,
        *,
        error_code: int = ERROR_INVALID_REQUEST,
    ) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x01B6,
            build_change_random_option_error(error_code),
        )
        return [packet], "%s; ACK01B6_fail/%s error=%d attempt=1/3" % (
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
        request = parse_change_random_option_request(body)
        if request is None:
            return self._error(
                "C1/01B6 CHANGE_RANDOM_OPTION parse_fail plain_len=%d plain=%s"
                % (len(body or b""), bytes(body or b"")[:16].hex(" "))
            )
        base = (
            "C1/01B6 CHANGE_RANDOM_OPTION slot=%d reserved=%d index=%d"
            % (
                request.target_slot,
                request.reserved,
                request.option_index,
            )
        )

        detail = resolve_selected_character_detail(self.server, session)
        character_id = int(detail.get("character_id") or 0)
        if character_id <= 0:
            return self._error(base + "; reject=no_character")

        repo = self.server._repo()
        source = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.target_slot,
        )
        if source is None:
            return self._error(base + "; reject=target_slot_empty")
        item_id = int(source.get("item_template_id") or 0) & 0xFFFFFFFF
        item_uid = int(source.get("item_uid") or 0)
        if (
            item_id <= 0
            or item_uid <= 0
            or str(source.get("item_kind") or "") != "equipment"
            or int(source.get("stack_count") or 0) != 1
        ):
            return self._error(base + "; reject=target_instance_invalid")
        if not _is_unsealed(source):
            return self._error(base + "; reject=target_not_unsealed")

        options = inventory_row_random_options(source)
        if request.option_index >= len(options):
            return self._error(
                base
                + "; reject=option_index_out_of_range count=%d" % len(options)
            )
        metadata = resolve_item_metadata(item_id)
        if not metadata.is_equipment or item_random_option_flag(item_id) <= 0:
            return self._error(
                base + "; reject=target_not_magic_seal item=%d" % item_id
            )

        try:
            index = self.index or get_magic_seal_random_option_index()
            rule = index.resolve_equipment(metadata)
            if rule is None:
                return self._error(
                    base
                    + "; reject=equipment_pool_missing item=%d rarity=%d path=%s"
                    % (
                        item_id,
                        int(getattr(metadata, "rarity", 0) or 0),
                        str(getattr(metadata, "source_path", "") or "-"),
                    )
                )
            excluded_types = {
                int(entry.get("type", 0) or 0) & 0xFF
                for option_index, entry in enumerate(options)
                if option_index != request.option_index
            }
            candidate = index.roll_modified(
                metadata,
                option_index=request.option_index,
                excluded_types=excluded_types,
                rng=self.rng,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return self._error(base + "; reject=rule_resolution:%s" % exc)

        ok, transaction_note, settlement = apply_magic_seal_option_change(
            repo,
            character_id=character_id,
            target_slot=request.target_slot,
            expected_item_uid=item_uid,
            expected_item_id=item_id,
            option_index=request.option_index,
            expected_options=options,
            updated_option=candidate.as_entry(),
            gold_cost=rule.gold_cost,
        )
        if not ok or settlement is None:
            return self._error(
                base + "; transaction_reject=" + transaction_note,
                error_code=_transaction_error_code(transaction_note),
            )

        refreshed = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.target_slot,
        )
        if refreshed is None:
            return self._error(base + "; reject=target_missing_after_commit")

        packets: list[bytes] = []
        notes = [
            base,
            transaction_note,
            "pool_key=%s group=%d cost=%d before=%s after=%s"
            % (
                rule.equipment_key,
                rule.group_ids[request.option_index],
                rule.gold_cost,
                format_options_brief(options),
                format_options_brief(inventory_row_random_options(refreshed)),
            ),
        ]

        refresh_body = build_item_list_update_000e(
            [InventoryItemView(refreshed)],
            list_type=0,
        )
        refresh_packet, refresh_mode = self._emit(0, 0x000E, refresh_body)
        packets.append(refresh_packet)
        notes.append(
            "000E_list0/%s slot=%d plain_len=%d"
            % (refresh_mode, request.target_slot, len(refresh_body))
        )

        if settlement.gold_cost > 0:
            wallet_body = build_wallet_gold_update_000e(settlement.updated_gold)
            wallet_packet, wallet_mode = self._emit(0, 0x000E, wallet_body)
            packets.append(wallet_packet)
            notes.append(
                "000E_wallet/%s gold=%d"
                % (wallet_mode, settlement.updated_gold)
            )

        ack_body = build_change_random_option_success()
        ack_packet, ack_mode = self._emit(1, 0x01B6, ack_body)
        packets.append(ack_packet)
        notes.append(
            "ACK01B6_ok/%s body=%s attempt=1/3"
            % (ack_mode, ack_body.hex(" "))
        )
        return packets, "; ".join(notes)


def handle_change_random_option_01b6(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return ChangeRandomOption01B6Service(server).handle(
        body,
        session=session,
    )


__all__ = [
    "ChangeRandomOption01B6Service",
    "ChangeRandomOptionRequest",
    "ERROR_INSUFFICIENT_GOLD",
    "ERROR_INVALID_REQUEST",
    "build_change_random_option_error",
    "build_change_random_option_success",
    "handle_change_random_option_01b6",
    "parse_change_random_option_request",
]
