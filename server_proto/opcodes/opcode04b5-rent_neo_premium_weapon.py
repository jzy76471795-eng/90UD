# -*- coding: utf-8 -*-
"""C1/04B5 Neo Premium Plus rental-weapon selection."""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import build_item_list_mode0, build_item_list_update_000e
from game.chain_loader import load_chain
from game.shop.neo_premium_benefits import (
    NEO_PREMIUM_PLUS_TYPE,
    neo_premium_rental_item_ids,
    resolve_neo_premium_rental_weapon,
)
from game.shop.premium_contract_index import get_active_premium_effects


_equip_bar = load_chain("helper-equip_itemlist.py")


@dataclass(frozen=True)
class NeoPremiumRentalSelection:
    group_index: int
    item_index: int


@dataclass(frozen=True)
class NeoPremiumRentalRequest:
    fixed_one: int
    selections: tuple[NeoPremiumRentalSelection, ...]


def parse_neo_premium_rental_request(body: bytes) -> NeoPremiumRentalRequest | None:
    raw = bytes(body or b"")
    if len(raw) < 2 or raw[0] != 1:
        return None
    count = int(raw[1])
    if count <= 0 or len(raw) != 2 + count * 2:
        return None
    selections = tuple(
        NeoPremiumRentalSelection(
            group_index=int(raw[2 + index * 2]),
            item_index=int(raw[3 + index * 2]),
        )
        for index in range(count)
    )
    if len({(row.group_index, row.item_index) for row in selections}) != count:
        return None
    return NeoPremiumRentalRequest(fixed_one=1, selections=selections)


def build_neo_premium_rental_success(withdrawn: list[dict[str, int]]) -> bytes:
    if len(withdrawn) > 0xFF:
        raise ValueError("invalid rental withdrawal count")
    # The generic S1 dispatcher consumes the success byte before 04B5 reads count.
    body = bytearray([1, len(withdrawn) & 0xFF])
    for row in withdrawn:
        list_type = int(row["list_type"])
        slot = int(row["slot_index"])
        if list_type < 0 or list_type > 0xFF or slot < 0 or slot > 0xFFFF:
            raise ValueError("invalid rental withdrawal location")
        body.extend(struct.pack("<BH", list_type, slot))
    return bytes(body)


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        try:
            value = int(getattr(session, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


class RentNeoPremiumWeapon04B5Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    @staticmethod
    def _failure(note: str) -> tuple[list[bytes], str]:
        return [], "C1/04B5 NEO_PREMIUM_RENT FAIL %s" % note

    def handle_rent_neo_premium_weapon_04b5(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_neo_premium_rental_request(body)
        if request is None:
            return self._failure("malformed plain_len=%d" % len(bytes(body or b"")))
        cid = _selected_character_id(session)
        if cid is None:
            return self._failure("selected_character_missing")
        try:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        except (TypeError, ValueError):
            account_id = 1
        now = int(time.time())
        repo = self.server._repo()
        character = next(
            (
                row
                for row in repo.list_by_account(account_id, now_unix=now)
                if int(row.character_id) == cid
            ),
            None,
        )
        if character is None:
            return self._failure("character_account_mismatch")
        effects = get_active_premium_effects(
            repo,
            account_id,
            session=session,
            now_unix=now,
        )
        if NEO_PREMIUM_PLUS_TYPE not in effects.active_types:
            return self._failure("neo_premium_plus_inactive")
        effective_level = min(
            255,
            max(1, int(character.level))
            + max(0, int(effects.equipment_level_reduction)),
        )

        resolved = []
        try:
            for selection in request.selections:
                resolved.append(
                    resolve_neo_premium_rental_weapon(
                        job=int(character.job),
                        effective_level=effective_level,
                        group_index=selection.group_index,
                        item_index=selection.item_index,
                    )
                )
        except (LookupError, ValueError) as exc:
            return self._failure("selection_rejected:%s" % exc)

        metadata = tuple(resolve_item_metadata(row.item_id) for row in resolved)
        ok, settlement_note, detail = repo.grant_neo_premium_rental_weapons(
            account_id=account_id,
            character_id=cid,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            weapons=metadata,
            rental_item_ids=neo_premium_rental_item_ids(),
            now_unix=now,
        )
        if not ok:
            return self._failure(settlement_note)

        withdrawn = list(detail.get("withdrawn", ()) or ())
        equipped_withdrawn = list(detail.get("equipped_withdrawn", ()) or ())
        withdrawal_rows = withdrawn + equipped_withdrawn
        packets = [
            self._emit(
                1,
                0x04B5,
                build_neo_premium_rental_success(withdrawal_rows),
            )
        ]
        try:
            changed = {int(value) for value in detail.get("changed_slots", ())}
            if changed:
                rows = repo.list_inventory_items(cid, list_type=0, now_unix=now)
                items = [InventoryItemView(row) for row in rows]
                update_items = [
                    item for item in items if int(item.slot_index) in changed
                ]
                packets.append(
                    self._emit(
                        0,
                        0x000E,
                        build_item_list_update_000e(
                            update_items, list_type=0, now_unix=now
                        ),
                    )
                )
                packets.append(
                    self._emit(
                        0,
                        0x000D,
                        build_item_list_mode0(
                            items,
                            list_type=0,
                            list_param16=repo.get_container_list_param(cid, 0),
                        ),
                    )
                )
            affected_equipment_slots = tuple(
                detail.get("affected_equipment_slots", ()) or ()
            )
            if affected_equipment_slots:
                equip_body, _count, equip_note = (
                    _equip_bar.build_equipment_bar_update_000e_body(repo, cid)
                )
                packets.append(self._emit(0, 0x000E, equip_body))
                settlement_note += " equipment_refresh=%s" % equip_note
        except Exception as exc:
            settlement_note += " refresh_error=%s:%s" % (type(exc).__name__, exc)
        return packets, (
            "C1/04B5 NEO_PREMIUM_RENT OK selections=%s effective_level=%d %s"
            % (
                [(row.group_index, row.item_index) for row in request.selections],
                effective_level,
                settlement_note,
            )
        )


__all__ = [
    "NeoPremiumRentalRequest",
    "NeoPremiumRentalSelection",
    "RentNeoPremiumWeapon04B5Service",
    "build_neo_premium_rental_success",
    "parse_neo_premium_rental_request",
]
