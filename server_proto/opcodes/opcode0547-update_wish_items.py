#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1/0547 saved wish equipment list used by Temporal Rift rewards."""
from __future__ import annotations

from typing import Any

from game.drop.wish_service import (
    MAX_WISH_ITEMS,
    build_wish_items_snapshot_packet,
    save_character_wish_items,
)
from game.quest.quest_session import resolve_selected_character_detail


def parse_wish_item_ids(body: bytes) -> tuple[tuple[int, ...], str]:
    raw = bytes(body or b"")
    if not raw:
        return (), "empty"
    count = int(raw[0])
    if count > MAX_WISH_ITEMS:
        return (), "count_out_of_range=%d" % count
    expected = 1 + count * 4
    if len(raw) != expected:
        return (), "length=%d expected=%d" % (len(raw), expected)
    item_ids = tuple(
        int.from_bytes(raw[offset : offset + 4], "little")
        for offset in range(1, expected, 4)
    )
    invalid = sum(1 for item_id in item_ids if item_id <= 0)
    duplicate = len(item_ids) - len(set(item_ids))
    note = "count=%d" % count
    if invalid or duplicate:
        note += " invalid=%d duplicate=%d" % (invalid, duplicate)
    return item_ids, note


class UpdateWishItems0547Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_update_wish_items_0547(
        self, body: bytes, *, session: Any = None
    ) -> tuple[list[bytes], str]:
        item_ids, parse_note = parse_wish_item_ids(body)
        base = "C1/0547 UPDATE_WISH_ITEMS plain_len=%d" % len(body or b"")
        if parse_note.startswith(("empty", "count_out_of_range", "length")):
            return [], "%s; parse_fail=%s" % (base, parse_note)
        try:
            detail = resolve_selected_character_detail(self.server, session)
            cid = int(detail.get("character_id") or 0)
        except Exception:
            cid = int(getattr(session, "selected_character_id", 0) or 0)
        if cid <= 0:
            return [], "%s; skip=no_cid; %s" % (base, parse_note)
        try:
            saved, save_action = save_character_wish_items(
                self.server._repo(),
                cid,
                item_ids,
            )
            snapshot_packet, snapshot_note = build_wish_items_snapshot_packet(
                self.server,
                saved,
            )
        except Exception as exc:
            return [], "%s; normalize_or_store_fail cid=%d err=%r" % (base, cid, exc)
        if session is not None:
            session.dungeon_wish_item_ids = tuple(saved)
        return [snapshot_packet], "%s; stored cid=%d %s action=%s items=%s; %s" % (
            base,
            cid,
            parse_note,
            save_action,
            ",".join(str(item_id) for item_id in saved) or "-",
            snapshot_note,
        )
