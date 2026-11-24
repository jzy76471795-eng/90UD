# -*- coding: utf-8 -*-
"""C1/0583 buys one PVF-declared adventure point-shop product."""
from __future__ import annotations

import struct
import time
from typing import Any

from game.character.adventure_group import load_adventure_group_overview
from game.character import cn2018_shop
from game.character.adventure_group_features import (
    build_main_inventory_refresh_packet,
    current_period_yyyymm,
    resolve_account_id,
    selected_target_key,
)
from game.character.item_metadata import resolve_item_metadata
from pvf.adventure_group_feature_index import get_adventure_group_feature_index


BUY_ADVENTURE_SHOP_ITEM_OPCODE = 0x0583
ERROR_CODE = 1


def _handle_cn_shop(server: Any, request_id: int, session: Any):
    account = int(getattr(session, "account_id", 0) or 0)
    character = int(getattr(session, "selected_character_id", 0) or 0)
    if not account or not character or not request_id or not getattr(session, "login_session_id", None):
        return _failure(server, "cn_shop_session_unavailable")
    repo = server._repo()
    if repo.get_account_id_for_character(character) != account:
        return _failure(server, "cn_shop_character_account_mismatch")
    item = 0
    try:
        shop = cn2018_shop.open_shop(repo)
        item = cn2018_shop.product_for_request(shop.catalog, request_id)
        shop.buy(account, character, item, 1, request_id, int(time.time()))
        success, reason = 1, "ok"
    except cn2018_shop.ShopRejected as exc:
        success, reason = 2, str(exc)
    except (KeyError, FileNotFoundError, ValueError):
        return _failure(server, "cn_shop_config_unavailable")
    packets, note, _ = server.contents_play_info_0578.build_full_state_refresh_0578(
        target_key=selected_target_key(server, session),
        session=session,
        reason="cn-shop-purchase",
        cn_shop_receipt=(request_id, success),
    )
    if success == 1:
        refresh, refresh_note = build_main_inventory_refresh_packet(server, repo, character)
        packets.append(refresh)
        note += "; " + refresh_note
    return packets, f"C1/0583 CN2018_SHOP item={item} quantity=1 result={reason}; {note}"


def parse_buy_adventure_shop_item_0583(body: bytes) -> int | None:
    raw = bytes(body or b"")
    if len(raw) < 4 or any(raw[4:]):
        return None
    content_id = struct.unpack_from("<I", raw, 0)[0]
    return content_id if content_id > 0 else None


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    packet = server._codec_response(
        1,
        BUY_ADVENTURE_SHOP_ITEM_OPCODE,
        bytes((0, ERROR_CODE)),
    )
    return [packet], "C1/0583 ADVENTURE_SHOP_BUY reject=%s" % reason


def handle_buy_adventure_shop_item_0583(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    content_id = parse_buy_adventure_shop_item_0583(body)
    if content_id is None:
        return _failure(server, "invalid_body")
    if cn2018_shop.enabled() and cn2018_shop.is_purchase_request(content_id):
        return _handle_cn_shop(server, content_id, session)
    character_id = int(getattr(session, "selected_character_id", 0) or 0)
    if character_id <= 0:
        return _failure(server, "character_not_selected")
    feature_index = get_adventure_group_feature_index()
    product = feature_index.shop_product(content_id)
    if product is None:
        return _failure(server, "unknown_content=%d" % content_id)
    repo = server._repo()
    account_id = resolve_account_id(server, repo, session)
    overview = load_adventure_group_overview(repo, account_id)
    period = current_period_yyyymm()
    repo.load_adventure_shop_state(
        account_id,
        total_character_exp=overview.progression.total_character_exp,
        maximum_points=feature_index.maximum_shop_points,
        exp_per_award=feature_index.shop_exp_per_award,
        points_per_award=feature_index.shop_points_per_award,
        period_yyyymm=period,
    )
    metadata = resolve_item_metadata(content_id)
    ok, detail, result = repo.purchase_adventure_shop_item(
        account_id=account_id,
        character_id=character_id,
        content_id=content_id,
        price=product.price,
        purchase_limit=product.purchase_limit,
        required_adventure_level=product.required_adventure_level,
        current_adventure_level=overview.progression.adventure_level,
        period_yyyymm=period,
        metadata=metadata,
    )
    if not ok:
        return _failure(server, detail)

    packets = [server._codec_response(1, BUY_ADVENTURE_SHOP_ITEM_OPCODE, b"\x01")]
    target_key = selected_target_key(server, session)
    state_packets, state_note, _shop_state = (
        server.contents_play_info_0578.build_full_state_refresh_0578(
            target_key=target_key,
            session=session,
            reason="shop-purchase",
        )
    )
    packets.extend(state_packets)
    refresh, refresh_note = build_main_inventory_refresh_packet(
        server, repo, character_id
    )
    packets.append(refresh)
    return packets, (
        "C1/0583 ADVENTURE_SHOP_BUY success account=%d character=%d "
        "content=%d price=%d points=%d->%d count=%d/%d target_key=%d; %s; %s"
        % (
            account_id,
            character_id,
            content_id,
            product.price,
            int(result["shop_points_before"]),
            int(result["shop_points_after"]),
            int(result["purchase_count_after"]),
            product.purchase_limit,
            target_key,
            state_note,
            refresh_note,
        )
    )


__all__ = [
    "BUY_ADVENTURE_SHOP_ITEM_OPCODE",
    "handle_buy_adventure_shop_item_0583",
    "parse_buy_adventure_shop_item_0583",
]
