# -*- coding: utf-8 -*-
"""C1/0040 Cera-shop purchase with PVF catalog and atomic inventory grant."""
from __future__ import annotations
from game.shop.purchase_limits import execute_purchase,notice
from game.chain_loader import load_chain

build_item_list_clear_slot_000e = load_chain("helper-dungeon_payloads.py").build_item_list_clear_slot_000e

import struct
import time

from dataclasses import dataclass
from typing import Any

from game.character.inventory_item_view import InventoryItemView
from game.character.character_db import InventoryGrantSpec
from game.character.seria_luck_index import resolve_seria_purchase_bonus
from game.character.avatar_closet import expansion_unlocked_set_count
from game.character.item_metadata import resolve_item_metadata
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.character.payloads import (
    build_avatar_closet_set_noti,
    build_avatar_item_list_mode0,
    build_guild_medal_item_list_mode0,
    build_item_list_mode0,
    build_item_list_update_000e,
    build_pet_item_list_mode0,
)
from game.cera_notifications import CERA_BALANCE_OPCODE, build_cera_balance
from game.shop.cerashop_index import (
    MILEAGE_RARE_VOUCHER,
    get_mileage_shop_index,
    resolve_account_purchase_limit,
    resolve_cerashop_product,
    resolve_cerashop_bonus_items,
    resolve_purchase_expire_time,
)
from game.shop.cera_booster_wrapper import (
    build_cera_booster_wrapper_grant,
    resolve_deterministic_cera_booster_wrapper,
)
from game.shop.cube_contract import (
    CUBE_CONTRACT_STATE_OPCODE,
    resolve_cube_contract_state,
)
from game.shop.mileage import (
    MILEAGE_AWARD_OPCODE,
    MILEAGE_BALANCE_OPCODE,
    build_mileage_award,
    build_mileage_balance,
)
from game.shop.premium_contract_index import PREMIUM_TYPE_CUBE, resolve_premium_contract
from game.shop.avatar_exchange_ticket import ticket_only_catalog,purchase_with_avatar_ticket


@dataclass(frozen=True)
class CerashopPurchaseEntry:
    selection0: int
    selection1: int
    commodity_id: int
    trailing0: int
    trailing1: int


@dataclass(frozen=True)
class CerashopPurchaseRequest:
    unknown0: int
    unknown1: int
    fixed_one: int
    selection0: int
    selection1: int
    commodity_id: int
    trailing0: int
    trailing1: int
    entries: tuple[CerashopPurchaseEntry, ...] = ()
    ticket_slot: int | None = None
    ticket_kind: int = 0


def parse_cerashop_purchase_request(body: bytes) -> CerashopPurchaseRequest | None:
    """Parse the Stage3 C1/0040 checkout body.

    Native Stage3 has two observed writers:
      * single-product writer: mode 00 01 + one 8-byte product row
      * cart writer:          mode 00 NN + NN 8-byte product rows

    Mode 0 uses the Cera/gold catalog. Mode 1 uses the mileage catalog,
    including when a commodity ID is also present in the Cera catalog.

    Each product row is:
      u8 selection0, u8 selection1, u32 commodity_id, u16 trailing_zero

    The third byte is still not a grant multiplier; it is the number of
    product rows in the cart.  Each row buys one shop commodity, whose item
    quantity and price come from cerashop.etc.
    """
    raw = bytes(body or b"")
    if len(raw) < 11:
        return None
    # Preserve the native non-gift body. The paired extension appends the
    # strictly bounded AVT1 inventory-voucher footer to one avatar purchase.
    if raw[0] not in (0, 1, 2) or raw[1] != 0:
        return None
    offset=2
    ticket_slot=None;ticket_kind=0
    count = int(raw[offset]);offset+=1
    if count <= 0:
        return None
    expected_len = offset + count * 8
    if len(raw) != expected_len:
        footer=raw[expected_len:]
        if raw[0]==1 or count!=1 or len(footer)!=7 or footer[:4]!=b'AVT1':return None
        ticket_slot,ticket_kind=struct.unpack_from('<HB',footer,4)
        if ticket_kind not in (1,2,3):return None
    entries: list[CerashopPurchaseEntry] = []
    for index in range(count):
        row_offset=offset+index*8
        commodity_id = struct.unpack_from("<I", raw, row_offset + 2)[0]
        trailing0 = int(raw[row_offset + 6])
        trailing1 = int(raw[row_offset + 7])
        if commodity_id <= 0 or trailing0 != 0 or trailing1 != 0:
            return None
        entries.append(
            CerashopPurchaseEntry(
                selection0=int(raw[row_offset]),
                selection1=int(raw[row_offset + 1]),
                commodity_id=int(commodity_id),
                trailing0=trailing0,
                trailing1=trailing1,
            )
        )
    first = entries[0]
    return CerashopPurchaseRequest(
        unknown0=int(raw[0]),
        unknown1=int(raw[1]),
        fixed_one=count,
        selection0=first.selection0,
        selection1=first.selection1,
        commodity_id=first.commodity_id,
        trailing0=first.trailing0,
        trailing1=first.trailing1,
        entries=tuple(entries),
        ticket_slot=ticket_slot,ticket_kind=int(ticket_kind),
    )


def build_cerashop_purchase_success(commodity_id: int) -> bytes:
    """Build the complete Stage3 success branch consumed by the 0040 reader.

    The two state fields at +10/+14 intentionally remain equal so the client
    runs its normal shop-close refresh.  That branch always consumes two more
    u32 values after the extension list; -1 is the native "no extra item"
    sentinel and the following value is still read before the sentinel check.
    """
    return struct.pack(
        "<BBiIIIIHiI",
        1,
        0,
        -1,
        int(commodity_id) & 0xFFFFFFFF,
        0,
        0,
        0,
        0,
        -1,
        0,
    )


def build_cerashop_purchase_failure(error_code: int = 4) -> bytes:
    """Stage3 failure branch: u8 field plus five complete i32 fields."""
    return struct.pack(
        "<BBBiiiii",
        0,
        int(error_code) & 0xFF,
        0,
        -1,
        -1,
        0,
        0,
        0,
    )


def build_premium_activation_notice(
    premium_type: int,
    remaining_seconds: int,
) -> bytes:
    """Stage3 S0/0042 subop 2: type plus signed 64-bit remaining seconds."""
    ptype = int(premium_type)
    remaining = int(remaining_seconds)
    if ptype <= 0 or ptype > 0xFF:
        raise ValueError("invalid premium_type=%d" % ptype)
    if remaining <= 0 or remaining > 0x7FFFFFFFFFFFFFFF:
        raise ValueError("invalid premium remaining=%d" % remaining)
    return struct.pack("<HBq", 2, ptype, remaining)


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            return cid
    return None


class BuyCerashopItem0040Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        packet = self._emit(1, 0x0040, build_cerashop_purchase_failure())
        return [packet], "C1/0040 CERA_SHOP_BUY FAIL %s" % note

    def handle_buy_cerashop_item_0040(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_cerashop_purchase_request(body)
        if request is None:
            return self._failure("malformed plain_len=%d" % len(bytes(body or b"")))

        cid = _selected_character_id(session)
        if cid is None:
            return self._failure("selected_character_missing")
        try:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        except (TypeError, ValueError):
            account_id = 1

        now_unix = int(time.time())
        repo = self.server._repo()
        ack_packets: list[bytes] = []
        premium_packets: list[bytes] = []
        closet_packets: list[bytes] = []
        success_notes: list[str] = []
        failure_notes: list[str] = []
        list_slots: dict[int, set[int]] = {}
        exhausted_ticket_slots: set[int] = set()
        list_order: list[int] = []
        mileage_delta = 0
        mileage_balance = 0
        cera_balance = None
        mileage_shop = request.unknown0 == 1
        mileage_products = get_mileage_shop_index() if mileage_shop else None

        for entry in request.entries:
            product = (
                mileage_products.get(entry.commodity_id)
                if mileage_products is not None
                else resolve_cerashop_product(entry.commodity_id)
            )
            if product is None:
                failure_notes.append("commodity_missing=%d" % entry.commodity_id)
                continue
            metadata = resolve_item_metadata(product.item_id)
            required_rule=ticket_only_catalog().get(entry.commodity_id) if not mileage_shop else None
            # A material-priced avatar may never fall through to the cash branch, even
            # if a release mistakenly omits or changes the matching signed PVF rule.
            material = (product.avatar_material_id, product.avatar_material_count)
            if material != (0, 0) and (
                required_rule is None or required_rule != (product.item_id, *material)
                or product.raw_quantity != 4 or product.cera_price or product.gold_price
            ):
                failure_notes.append('avatar_ticket_material_mismatch')
                continue
            if required_rule and required_rule[0]!=product.item_id:
                failure_notes.append('avatar_ticket_catalog_mismatch');continue
            voucher_purchase=request.ticket_slot is not None or required_rule is not None
            if voucher_purchase and mileage_shop:
                failure_notes.append('avatar_ticket_mileage_rejected');continue
            # Checkout validates the PVF source item; opening validates the
            # package reward graph independently after the box is in list0.
            grant_count = int(product.quantity)
            cera_cost = int(product.cera_price)
            gold_cost = int(product.gold_price)
            mileage_cost = int(product.mileage_price)
            currency_kwargs = {"total_mileage_cost": mileage_cost} if mileage_shop else {}
            if grant_count <= 0 or grant_count > 0xFFFFFFFF:
                failure_notes.append(
                    "commodity=%d grant_count_overflow=%d"
                    % (entry.commodity_id, grant_count)
                )
                continue
            if cera_cost < 0 or cera_cost > 0x7FFFFFFFFFFFFFFF:
                failure_notes.append(
                    "commodity=%d cera_cost_overflow=%d"
                    % (entry.commodity_id, cera_cost)
                )
                continue

            expire_time = resolve_purchase_expire_time(metadata, now_unix=now_unix)
            purchase_limit = resolve_account_purchase_limit(product)
            premium = resolve_premium_contract(product.item_id)
            closet_target = expansion_unlocked_set_count(metadata)
            delivery_item_id=product.item_id
            delivery_expire_time=expire_time
            wrapper_note=''
            if voucher_purchase:
                ok,settlement_note,detail=execute_purchase(repo, account_id, cid, metadata, grant_count, lambda: purchase_with_avatar_ticket(repo,product=product,metadata=metadata,
                    ticket_slot=request.ticket_slot,required_ticket=required_rule[1] if required_rule else 0,
                    account_id=account_id,character_id=cid,commodity_id=entry.commodity_id,
                    purchase_limit=purchase_limit,item_id=product.item_id,grant_count=grant_count,
                    option_value=0,expire_time=0), now=now_unix)
                premium=None;closet_target=None
                cera_cost=gold_cost=mileage_cost=0
                delivery_expire_time=0
                wrapper_note=' avatar_ticket=%s slot=%s remaining=%s' % (detail.get('avatar_ticket_item'),detail.get('avatar_ticket_slot'),detail.get('avatar_ticket_remaining'))
            elif premium is not None:
                premium_duration = int(premium.duration_seconds) * grant_count
                ok, settlement_note, detail = execute_purchase(repo, account_id, cid, metadata, grant_count, lambda: repo.purchase_cerashop_premium(
                    account_id=account_id,
                    character_id=cid,
                    commodity_id=entry.commodity_id,
                    purchase_limit=purchase_limit,
                    item_id=product.item_id,
                    premium_type=premium.premium_type,
                    duration_seconds=premium_duration,
                    total_cera_cost=cera_cost,
                    total_gold_cost=gold_cost,
                    now_unix=now_unix,
                    **currency_kwargs,
                ), now=now_unix)
            elif closet_target is not None:
                ok, settlement_note, detail = execute_purchase(repo, account_id, cid, metadata, grant_count, lambda: repo.purchase_avatar_closet_expansion(
                    account_id=account_id,
                    character_id=cid,
                    commodity_id=entry.commodity_id,
                    purchase_limit=purchase_limit,
                    item_id=product.item_id,
                    unlocked_set_count=closet_target,
                    total_cera_cost=cera_cost,
                    total_gold_cost=gold_cost,
                    **currency_kwargs,
                ), now=now_unix)
            else:
                delivery_item_id = int(product.item_id) & 0xFFFFFFFF
                delivery_metadata = metadata
                delivery_count = grant_count
                delivery_expire_time = expire_time
                wrapper_note = ""
                try:
                    wrapper = resolve_deterministic_cera_booster_wrapper(
                        product.item_id,
                        source_metadata=metadata,
                    )
                except ValueError as exc:
                    failure_notes.append(
                        "commodity=%d item=%d cera_booster_wrapper=%s"
                        % (entry.commodity_id, product.item_id, exc)
                    )
                    continue
                if wrapper is not None:
                    delivery_expire_time = resolve_purchase_expire_time(
                        wrapper.reward_metadata,
                        now_unix=now_unix,
                    )
                    try:
                        delivery = build_cera_booster_wrapper_grant(
                            wrapper,
                            copies=grant_count,
                            expire_time=delivery_expire_time,
                        )
                    except ValueError as exc:
                        failure_notes.append(
                            "commodity=%d item=%d cera_booster_delivery=%s"
                            % (entry.commodity_id, product.item_id, exc)
                        )
                        continue
                    delivery_item_id = int(delivery.item_id) & 0xFFFFFFFF
                    delivery_metadata = delivery.metadata
                    delivery_count = int(delivery.count)
                    delivery_expire_time = int(delivery.expire_time)
                    wrapper_note = " cera_booster=%d->%dx%d" % (
                        int(wrapper.source_item_id),
                        delivery_item_id,
                        delivery_count,
                    )
                try:
                    bonuses = ()
                    if not mileage_shop:
                        legacy = resolve_seria_purchase_bonus(product)
                        configured = tuple(
                            (gift.item_id, gift.count) for gift in resolve_cerashop_bonus_items(product.commodity_id)
                        )
                        if legacy and configured and sorted(legacy) != sorted(configured):
                            raise ValueError("conflicting shop bonus definitions")
                        bonuses = configured or legacy
                    bonus_grants = []
                    for bonus_id, bonus_count in bonuses:
                        bonus_meta = resolve_item_metadata(bonus_id)
                        bonus_grants.append(InventoryGrantSpec(
                            item_id=bonus_id, count=bonus_count, metadata=bonus_meta,
                            expire_time=resolve_purchase_expire_time(bonus_meta, now_unix=now_unix),
                        ))
                except ValueError as exc:
                    failure_notes.append("commodity=%d bonus=%s" % (entry.commodity_id, exc))
                    continue
                purchase_method = repo.purchase_cerashop_item
                bonus_kwargs = {}
                if bonus_grants:
                    purchase_method = repo.purchase_cerashop_bundle
                    bonus_kwargs["bonus_grants"] = tuple(bonus_grants)
                quota_metadata = metadata
                if mileage_shop and (product.commodity_id, product.display_item_id, product.item_id) == MILEAGE_RARE_VOUCHER:
                    quota_metadata = resolve_item_metadata(product.display_item_id)
                ok, settlement_note, detail = execute_purchase(repo, account_id, cid, quota_metadata, grant_count, lambda: purchase_method(
                    account_id=account_id,
                    character_id=cid,
                    commodity_id=entry.commodity_id,
                    purchase_limit=purchase_limit,
                    item_id=delivery_item_id,
                    grant_count=delivery_count,
                    total_cera_cost=cera_cost,
                    total_gold_cost=gold_cost,
                    metadata=delivery_metadata,
                    # The two request selection bytes remain protocol fields until a
                    # Stage3 avatar-attribute consumer is closed; do not guess them.
                    option_value=0,
                    expire_time=delivery_expire_time,
                    **currency_kwargs,
                    **bonus_kwargs,
                ), now=now_unix)
                if ok and bonus_grants:
                    wrapper_note += " bonuses=" + ",".join("%dx%d" % (s.item_id, s.count) for s in bonus_grants)
            if not ok:
                if "purchase_limit" in settlement_note:premium_packets.append(notice(self.server))
                failure_notes.append(
                    "commodity=%d item=%d %s"
                    % (entry.commodity_id, product.item_id, settlement_note)
                )
                continue

            ack_packets.append(
                self._emit(
                    1,
                    0x0040,
                    build_cerashop_purchase_success(entry.commodity_id),
                )
            )
            if 'avatar_ticket_slot' in detail:
                ticket_slot = int(detail['avatar_ticket_slot'])
                list_slots.setdefault(0,set()).add(ticket_slot)
                if int(detail['avatar_ticket_remaining']) == 0:
                    exhausted_ticket_slots.add(ticket_slot)
                if 0 not in list_order:list_order.append(0)
            mileage_change = detail.get("mileage_change")
            if mileage_change is not None:
                before, mileage_balance = mileage_change
                mileage_delta += mileage_balance - before
            if cera_cost:
                cera_balance = detail["cera_after"]
            if gold_cost:
                list_slots.setdefault(0, set()).add(0)
                if 0 not in list_order:
                    list_order.append(0)
            if premium is not None:
                premium_extra_note = ""
                premium_packets.append(
                    self._emit(
                        0,
                        0x0042,
                        build_premium_activation_notice(
                            premium.premium_type,
                            int(detail.get("remaining_seconds", 0) or 0),
                        ),
                    )
                )
                if int(premium.premium_type) == PREMIUM_TYPE_CUBE:
                    try:
                        cube_state = resolve_cube_contract_state(
                            repo,
                            account_id=account_id,
                            character_id=cid,
                            now_unix=now_unix,
                        )
                        premium_packets.append(
                            self._emit(
                                0,
                                CUBE_CONTRACT_STATE_OPCODE,
                                cube_state.to_body(),
                            )
                        )
                        premium_extra_note = " cube_selection=%02x" % (
                            cube_state.wire_selection,
                        )
                    except Exception as exc:
                        premium_extra_note = " cube_state_error=%s:%s" % (
                            type(exc).__name__,
                            exc,
                        )
                if session is not None:
                    try:
                        session.account_premium_effects = None
                    except Exception:
                        pass
                success_notes.append(
                    "commodity=%d item=%d count=%d section=%s premium_type=%d "
                    "duration=%d remaining=%d cera=%s->%s purchase=%s/%s "
                    "selection=%02x/%02x%s"
                    % (
                        entry.commodity_id,
                        product.item_id,
                        grant_count,
                        product.section,
                        premium.premium_type,
                        premium_duration,
                        detail.get("remaining_seconds"),
                        detail.get("cera_before"),
                        detail.get("cera_after"),
                        detail.get("purchase_count_after"),
                        detail.get("purchase_limit"),
                        entry.selection0,
                        entry.selection1,
                        premium_extra_note,
                    )
                )
                continue
            if closet_target is not None:
                unlocked = int(
                    detail.get("unlocked_set_count", closet_target) or closet_target
                )
                current = int(detail.get("current_set_index", 0) or 0)
                closet_packets.append(
                    self._emit(
                        0,
                        0x043E,
                        build_avatar_closet_set_noti(unlocked, current),
                    )
                )
                success_notes.append(
                    "commodity=%d item=%d count=%d section=%s "
                    "avatar_closet=%s->%s current=%s cera=%s->%s "
                    "purchase=%s/%s selection=%02x/%02x"
                    % (
                        entry.commodity_id,
                        product.item_id,
                        grant_count,
                        product.section,
                        detail.get("unlocked_set_count_before"),
                        unlocked,
                        current,
                        detail.get("cera_before"),
                        detail.get("cera_after"),
                        detail.get("purchase_count_after"),
                        detail.get("purchase_limit"),
                        entry.selection0,
                        entry.selection1,
                    )
                )
                continue
            list_type = int(detail.get("list_type", 0) or 0)
            changed_slots = {
                int(slot) for slot in list(detail.get("changed_slots", ()) or ())
            }
            if list_type not in list_slots:
                list_slots[list_type] = set()
                list_order.append(list_type)
            list_slots[list_type].update(changed_slots)
            for bonus_list, bonus_slots in detail.get("changed_slots_by_list_type", {}).items():
                bonus_list = int(bonus_list)
                if bonus_list not in list_slots:
                    list_slots[bonus_list] = set()
                    list_order.append(bonus_list)
                list_slots[bonus_list].update(int(slot) for slot in bonus_slots)
            revive_wallet_note = ""
            if is_revive_coin_grant_item_id(
                int(detail.get("item_id", delivery_item_id) or delivery_item_id)
            ):
                revive_wallet_note = " revive_coin=%s->%s" % (
                    detail.get("revive_coin_before"),
                    detail.get("revive_coin_after"),
                )
            success_notes.append(
                "commodity=%d item=%d count=%d section=%s list=%d slots=%s "
                "cera=%s->%s purchase=%s/%s expire=%d selection=%02x/%02x%s"
                % (
                    entry.commodity_id,
                    product.item_id,
                    grant_count,
                    product.section,
                    list_type,
                    sorted(changed_slots),
                    detail.get("cera_before"),
                    detail.get("cera_after"),
                    detail.get("purchase_count_after"),
                    detail.get("purchase_limit"),
                    delivery_expire_time,
                    entry.selection0,
                    entry.selection1,
                    revive_wallet_note + wrapper_note,
                )
            )

        if not ack_packets:
            packets,note = self._failure(
                "all_failed n=%d %s" % (len(request.entries), "; ".join(failure_notes))
            )
            return packets + premium_packets, note

        if len(request.entries) == 1 and not failure_notes:
            note = "C1/0040 CERA_SHOP_BUY OK " + success_notes[0]
        else:
            note = (
                "C1/0040 CERA_SHOP_BUY OK batch_n=%d success=%d fail=%d ok=[%s]"
                % (
                    len(request.entries),
                    len(success_notes),
                    len(failure_notes),
                    "; ".join(success_notes),
                )
            )
            if failure_notes:
                note += " fail=[%s]" % "; ".join(failure_notes)

        # Settlement is already committed. Always return its success ACK even
        # if a later inventory refresh cannot be materialized; reporting a
        # purchase failure here would invite a retry and duplicate settlement.
        refresh_packets: list[bytes] = []
        refresh_mismatches: list[str] = []
        try:
            for list_type in list_order:
                changed_slots = list_slots[list_type]
                inventory_rows = repo.list_inventory_items(cid, list_type=list_type)
                all_items = [InventoryItemView(row) for row in inventory_rows]
                changed_items = [
                    item for item in all_items if int(item.slot_index) in changed_slots
                ]
                present = {int(item.slot_index) for item in changed_items}
                emptied = (exhausted_ticket_slots - present) if list_type == 0 else set()
                for empty_slot in sorted(emptied):
                    refresh_packets.append(self._emit(0, 0x000E, build_item_list_clear_slot_000e(empty_slot, list_type=0)))
                if present | emptied != changed_slots:
                    refresh_mismatches.append(
                        "list=%d rows=%s expected=%s"
                        % (
                            list_type,
                            [item.slot_index for item in changed_items],
                            sorted(changed_slots),
                        )
                    )
                list_param16 = (
                    0
                    if list_type == repo.INVENTORY_LIST_GUILD_MEDAL
                    else int(repo.get_container_list_param(cid, list_type))
                )
                update = build_item_list_update_000e(
                    changed_items,
                    list_type=list_type,
                    avatar=(list_type == 1),
                    now_unix=now_unix,
                )
                if list_type == 1:
                    full = build_avatar_item_list_mode0(
                        all_items,
                        list_param16=list_param16,
                        now_unix=now_unix,
                    )
                elif list_type == 7:
                    full = build_pet_item_list_mode0(all_items)
                elif list_type == repo.INVENTORY_LIST_GUILD_MEDAL:
                    full = build_guild_medal_item_list_mode0(
                        all_items,
                        selected_character_slot=int(
                            getattr(session, "selected_slot", 0) or 0
                        ),
                    )
                else:
                    full = build_item_list_mode0(
                        all_items,
                        list_type=list_type,
                        list_param16=list_param16,
                    )
                refresh_packets.extend(
                    [
                        self._emit(0, 0x000E, update),
                        self._emit(0, 0x000D, full),
                    ]
                )
        except Exception as exc:
            note += " refresh_error=%s:%s" % (type(exc).__name__, exc)
            refresh_packets.clear()

        if refresh_mismatches:
            note += " refresh_mismatch %s" % refresh_mismatches
        packets = ack_packets + premium_packets + closet_packets + refresh_packets
        if cera_balance is not None:
            packets.append(self._emit(0, CERA_BALANCE_OPCODE, build_cera_balance(cera_balance)))
        if mileage_delta > 0:
            packets.append(
                self._emit(
                    0, MILEAGE_AWARD_OPCODE,
                    build_mileage_award(mileage_balance - mileage_delta, mileage_balance),
                )
            )
        elif mileage_delta < 0:
            packets.append(self._emit(0, MILEAGE_BALANCE_OPCODE, build_mileage_balance(mileage_balance)))
        if mileage_delta:
            note += " mileage=%+d balance=%d" % (mileage_delta, mileage_balance)
        return packets, note


__all__ = [
    "BuyCerashopItem0040Service",
    "CerashopPurchaseEntry",
    "CerashopPurchaseRequest",
    "InventoryItemView",
    "build_cerashop_purchase_failure",
    "build_cerashop_purchase_success",
    "build_premium_activation_notice",
    "parse_cerashop_purchase_request",
]
