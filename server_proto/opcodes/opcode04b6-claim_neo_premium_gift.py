# -*- coding: utf-8 -*-
"""C1/04B6 Neo Premium Plus daily-gift claim."""
from __future__ import annotations

import time
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0, build_item_list_update_000e
from game.chain_loader import load_chain
from game.shop.neo_premium_benefits import (
    NEO_PREMIUM_PLUS_TYPE,
    neo_premium_daily_period,
    neo_premium_daily_reward_items,
)


_equip_bar = load_chain("helper-equip_itemlist.py")


def parse_neo_premium_daily_gift_request(body: bytes) -> bool:
    return len(bytes(body or b"")) == 0


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


class ClaimNeoPremiumGift04B6Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    @staticmethod
    def _failure(note: str) -> tuple[list[bytes], str]:
        return [], "C1/04B6 NEO_PREMIUM_GIFT FAIL %s" % note

    def handle_claim_neo_premium_gift_04b6(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not parse_neo_premium_daily_gift_request(body):
            return self._failure("malformed plain_len=%d" % len(bytes(body or b"")))
        cid = _selected_character_id(session)
        if cid is None:
            return self._failure("selected_character_missing")
        try:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        except (TypeError, ValueError):
            account_id = 1
        now = int(time.time())
        period_start, next_reset = neo_premium_daily_period(now)
        try:
            daily_items = neo_premium_daily_reward_items()
        except Exception as exc:
            return self._failure(
                "daily_gift_rewards_invalid type=118 error=%s:%s"
                % (type(exc).__name__, exc)
            )
        if not daily_items:
            return self._failure("daily_gift_rewards_empty type=118")

        repo = self.server._repo()
        ok, settlement_note, detail = repo.claim_neo_premium_daily_gift(
            account_id=account_id,
            character_id=cid,
            premium_type=NEO_PREMIUM_PLUS_TYPE,
            period_start=period_start,
            next_reset=next_reset,
            daily_items=daily_items,
            now_unix=now,
        )
        if not ok:
            return self._failure(settlement_note)

        # The shared S1 dispatcher consumes the leading success byte before
        # invoking the 04B6 callback.  An empty body leaves the client reader
        # without that mandatory status and stalls the follow-up booster use.
        packets = [self._emit(1, 0x04B6, b"\x01")]
        helper_activation = load_chain("helper-system_reward_activation.py")
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
            packets.append(
                self._emit(
                    0,
                    0x0042,
                    helper_activation.build_premium_activation_notice(
                        premium_type, remaining
                    ),
                )
            )
        try:
            raw_changed = detail.get("changed_slots", {})
            if not isinstance(raw_changed, dict):
                raw_changed = {0: raw_changed}
            for list_type in (0, 1, 7, 36):
                changed = {
                    int(value)
                    for value in raw_changed.get(
                        list_type,
                        raw_changed.get(str(list_type), ()),
                    )
                }
                if not changed:
                    continue
                rows = repo.list_inventory_items(
                    cid,
                    list_type=list_type,
                    now_unix=now,
                )
                by_slot = {
                    int(row.get("slot_index", 0) or 0) & 0xFFFF: row
                    for row in rows
                    if (int(row.get("slot_index", 0) or 0) & 0xFFFF) in changed
                }
                update_items: list[InventoryItemView] = []
                for slot in sorted(changed):
                    row = by_slot.get(slot)
                    if row is None:
                        row = {
                            "slot_index": slot,
                            "item_template_id": -1,
                            "item_kind": "unknown",
                            "stack_count": 0,
                            "instance_value": 0,
                        }
                    update_items.append(InventoryItemView(row))
                packets.append(
                    self._emit(
                        0,
                        0x000E,
                        build_item_list_update_000e(
                            update_items,
                            list_type=list_type,
                            avatar=(list_type == 1),
                            now_unix=now,
                        ),
                    )
                )
                if list_type == 0:
                    items = [InventoryItemView(row) for row in rows]
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
            if detail.get("expired_equipment_slots"):
                equip_body, _count, _note = _equip_bar.build_equipment_bar_update_000e_body(
                    repo, cid
                )
                packets.append(self._emit(0, 0x000E, equip_body))
        except Exception as exc:
            settlement_note += " refresh_error=%s:%s" % (type(exc).__name__, exc)
        return packets, "C1/04B6 NEO_PREMIUM_GIFT OK %s" % settlement_note


__all__ = [
    "ClaimNeoPremiumGift04B6Service",
    "parse_neo_premium_daily_gift_request",
]
