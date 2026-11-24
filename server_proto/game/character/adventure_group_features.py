# -*- coding: utf-8 -*-
"""Adventure dispatch and point-shop projections for the frozen Stage3 client."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import struct
import sys
import time
from typing import Any, Mapping, Sequence

from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.character.payloads import build_item_list_mode0


MERCENARY_INFO_OPCODE = 0x01BA
MERCENARY_STATE_OPCODE = 0x0540
ADVENTURE_SHOP_POINT_NOTICE_OPCODE = 0x054B

DISPATCH_STATE_WAITING = 0
DISPATCH_STATE_RUNNING = 1
DISPATCH_STATE_REWARD = 2

AVATAR_SET_SLOTS = tuple(range(8))
BODY_EQUIPMENT_SLOT_MIN = 12
BODY_EQUIPMENT_SLOT_MAX = 25
NORMAL_AVATAR_POINT_TIER = 1
RARE_AVATAR_POINT_TIER = 2
NORMAL_AVATAR_SET_POINTS = 30
RARE_AVATAR_SET_POINTS = 60
RARE_AVATAR_MINIMUM_RARITY = 2
MERCENARY_NAME_CAPACITY = 256


@dataclass(frozen=True)
class MercenaryCharacterPoints:
    character_id: int
    name_bytes: bytes
    level: int
    equipment_points: int
    avatar_point_tier: int
    avatar_points: int

    @property
    def total_points(self) -> int:
        return self.equipment_points + self.avatar_points


def _row_value(row: Any, name: str, default: Any = 0) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _name_bytes(row: Any) -> bytes:
    raw = _row_value(row, "name_bytes", b"")
    if isinstance(raw, memoryview):
        value = raw.tobytes()
    elif isinstance(raw, (bytes, bytearray)):
        value = bytes(raw)
    else:
        value = b""
    if not value:
        value = str(_row_value(row, "name", "") or "").encode("utf-8")
    if not 0 < len(value) < MERCENARY_NAME_CAPACITY:
        raise ValueError("mercenary character name byte length=%d" % len(value))
    return value


def equipment_points_for_rarity(rarity: int) -> int:
    """Project one worn body's PVF rarity into the native point scale."""
    value = max(0, min(0xFF, int(rarity)))
    return (value + 1) * 10


def calculate_mercenary_character_points(
    row: Any,
    *,
    metadata_resolver: Any = resolve_item_metadata,
) -> MercenaryCharacterPoints:
    equipped = tuple(_row_value(row, "equipped_items", ()) or ())
    equipment_points = 0
    avatar_rarities: dict[int, int] = {}
    for entry in equipped:
        slot = int(_row_value(entry, "slot", -1))
        item_id = int(_row_value(entry, "item_id", 0)) & 0xFFFFFFFF
        if item_id <= 0:
            continue
        metadata: ItemMetadata = metadata_resolver(item_id)
        if slot in AVATAR_SET_SLOTS:
            if bool(metadata.is_avatar):
                avatar_rarities[slot] = max(0, int(metadata.rarity))
            continue
        if (
            BODY_EQUIPMENT_SLOT_MIN <= slot <= BODY_EQUIPMENT_SLOT_MAX
            and bool(metadata.is_equipment)
            and not bool(metadata.is_avatar)
        ):
            equipment_points += equipment_points_for_rarity(metadata.rarity)

    avatar_tier = 0
    avatar_points = 0
    if all(slot in avatar_rarities for slot in AVATAR_SET_SLOTS):
        if min(avatar_rarities.values()) >= RARE_AVATAR_MINIMUM_RARITY:
            avatar_tier = RARE_AVATAR_POINT_TIER
            avatar_points = RARE_AVATAR_SET_POINTS
        else:
            avatar_tier = NORMAL_AVATAR_POINT_TIER
            avatar_points = NORMAL_AVATAR_SET_POINTS

    return MercenaryCharacterPoints(
        character_id=max(0, int(_row_value(row, "character_id", 0))),
        name_bytes=_name_bytes(row),
        level=max(0, min(0xFF, int(_row_value(row, "level", 0)))),
        equipment_points=equipment_points,
        avatar_point_tier=avatar_tier,
        avatar_points=avatar_points,
    )


def load_mercenary_character_points(
    repo: Any,
    account_id: int,
) -> tuple[MercenaryCharacterPoints, ...]:
    rows = tuple(repo.list_adventure_dispatch_characters(int(account_id)) or ())
    records = tuple(calculate_mercenary_character_points(row) for row in rows)
    if len(records) > 0xFF:
        raise ValueError("mercenary character record count exceeds u8")
    return records


def build_mercenary_info_body_01ba(
    records: Sequence[MercenaryCharacterPoints],
) -> bytes:
    entries = tuple(records)
    if len(entries) > 0xFF:
        raise ValueError("mercenary record count exceeds u8")
    body = bytearray((1, len(entries)))
    for record in entries:
        character_id = int(record.character_id)
        total_points = int(record.total_points)
        tier = int(record.avatar_point_tier)
        name = bytes(record.name_bytes)
        if not 0 < character_id <= 0xFFFFFFFF:
            raise ValueError("mercenary character id must fit positive u32")
        if not 0 < len(name) < MERCENARY_NAME_CAPACITY:
            raise ValueError("mercenary name length is invalid")
        if not 0 <= tier <= 0xFF or not 0 <= total_points <= 0xFFFFFFFF:
            raise ValueError("mercenary point projection is outside wire range")
        body += struct.pack("<II", character_id, len(name))
        body += name
        # The first byte after DSTR is present in the verified reader but has
        # no observed consumer. Keep it neutral; the next fields are live.
        body += struct.pack("<BBI", 0, tier, total_points)
    return bytes(body)


def client_tick_seconds() -> int:
    """Legacy same-PC clock, only for explicitly requested local projections."""
    if sys.platform == "win32":
        get_tick_count = ctypes.windll.kernel32.GetTickCount
        get_tick_count.restype = ctypes.c_uint32
        return int(get_tick_count()) // 1000
    return int(time.monotonic()) & 0xFFFFFFFF


def project_dispatch_times_to_client_clock(
    start_unix: int,
    finish_unix: int,
    *,
    now_unix: int | None = None,
    tick_seconds: int | None = None,
) -> tuple[int, int]:
    """Project into the paired client's START-seeded Unix seconds clock.

    The installed raid-runtime-v2 clock reads START+5 and advances it with
    the client's own monotonic timer on every channel. Server OS uptime is
    unrelated to that clock. ``tick_seconds`` remains an explicit override
    for legacy local-only callers; normal public-server packets use Unix.
    """
    start = max(0, int(start_unix))
    finish = max(0, int(finish_unix))
    if finish <= start:
        return 0, 0

    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    tick = now if tick_seconds is None else int(tick_seconds)
    duration = finish - start
    elapsed = max(0, min(duration, now - start))
    remaining = max(0, min(duration, finish - now))

    # Both the native progress bar and auto-completion compare against the
    # same START-seeded clock. Preserve the existing elapsed/remaining clamps
    # and u32 wire layout, including explicit legacy clock projections.
    wire_start = (tick - elapsed) & 0xFFFFFFFF
    wire_finish = (tick + remaining) & 0xFFFFFFFF
    return wire_start, wire_finish


def build_mercenary_state_body_0540(
    state: Mapping[str, Any] | None,
    *,
    now_unix: int | None = None,
    tick_seconds: int | None = None,
) -> bytes:
    snapshot = dict(state or {})
    wire_start, wire_finish = project_dispatch_times_to_client_clock(
        int(snapshot.get("start_unix", 0)),
        int(snapshot.get("finish_unix", 0)),
        now_unix=now_unix,
        tick_seconds=tick_seconds,
    )
    values = (
        int(snapshot.get("dispatch_state", DISPATCH_STATE_WAITING)),
        int(snapshot.get("area_index", 0)),
        int(snapshot.get("committed_points", 0)),
        wire_start,
        wire_finish,
    )
    if not 0 <= values[0] <= 2 or not 0 <= values[1] <= 0xFF:
        raise ValueError("invalid mercenary dispatch state")
    if any(value < 0 or value > 0xFFFFFFFF for value in values[2:]):
        raise ValueError("mercenary dispatch state value must fit u32")
    return struct.pack("<BBIII", *values)


def build_adventure_shop_point_notice_054b(old_points: int, new_points: int) -> bytes:
    old_value = int(old_points)
    new_value = int(new_points)
    if not 0 <= old_value <= 0xFFFFFFFF or not 0 <= new_value <= 0xFFFFFFFF:
        raise ValueError("adventure shop point notice values must fit u32")
    return struct.pack("<II", old_value, new_value)


def build_main_inventory_refresh_packet(
    server: Any,
    repo: Any,
    character_id: int,
) -> tuple[bytes, str]:
    cid = int(character_id)
    rows, list_param16 = repo.build_itemlist_mode0_rows(cid, list_type=0)
    body = build_item_list_mode0(
        [InventoryItemView(row) for row in rows],
        list_type=0,
        list_param16=int(list_param16),
    )
    packet = server._codec_response(0, 0x000D, body)
    return packet, "S0/000D list0 entries=%d" % len(rows)


def resolve_account_id(server: Any, repo: Any, session: Any = None) -> int:
    account_id = int(getattr(server, "account_id", 1) or 1)
    character_id = int(getattr(session, "selected_character_id", 0) or 0)
    if character_id > 0:
        owner = repo.get_account_id_for_character(character_id)
        if owner is not None:
            account_id = int(owner)
    return account_id


def current_period_yyyymm(now_unix: int | None = None) -> int:
    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    return int(time.strftime("%Y%m", time.localtime(now)))


def selected_target_key(server: Any, session: Any = None) -> int:
    value = getattr(session, "adventure_group_target_key", None)
    if value is None:
        value = (getattr(session, "selected_character_id", 0) if session is not None
                 else getattr(server, "_adventure_group_target_key", 0))
    return int(value or 0) & 0xFFFF


def remember_target_key(server: Any, session: Any, target_key: int) -> None:
    key = int(target_key) & 0xFFFF
    setattr(server, "_adventure_group_target_key", key)
    if session is not None:
        setattr(session, "adventure_group_target_key", key)


__all__ = [
    "ADVENTURE_SHOP_POINT_NOTICE_OPCODE",
    "DISPATCH_STATE_REWARD",
    "DISPATCH_STATE_RUNNING",
    "DISPATCH_STATE_WAITING",
    "MERCENARY_INFO_OPCODE",
    "MERCENARY_STATE_OPCODE",
    "MercenaryCharacterPoints",
    "build_adventure_shop_point_notice_054b",
    "build_main_inventory_refresh_packet",
    "build_mercenary_info_body_01ba",
    "build_mercenary_state_body_0540",
    "calculate_mercenary_character_points",
    "client_tick_seconds",
    "current_period_yyyymm",
    "equipment_points_for_rarity",
    "load_mercenary_character_points",
    "project_dispatch_times_to_client_clock",
    "remember_target_key",
    "resolve_account_id",
    "selected_target_key",
]
