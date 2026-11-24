"""Shared Stage3 wire and inventory helpers for random containers."""
from __future__ import annotations

import struct
from typing import Any, Iterable

from game.character.character_db import InventoryGrantSpec
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    ItemMetadata,
    resolve_item_metadata,
)
from game.character.payloads import build_item_list_update_000e
from game.character.random_container_index import RandomRewardCandidate
from game.shop.cerashop_index import resolve_purchase_expire_time


ERROR_INVALID_CONTAINER = 4
REFRESHABLE_INVENTORY_LIST_TYPES = (0, 1, 7, GUILD_MEDAL_INVENTORY_LIST_TYPE)


def build_random_container_failure(error_code: int = ERROR_INVALID_CONTAINER) -> bytes:
    return struct.pack("<BB", 0, int(error_code) & 0xFF)


def lottery_item_uses_extension_blobs(metadata: ItemMetadata) -> bool:
    """Match Stage3 ``sub_1AE3A00`` for the C1/001B item row.

    The native equipment enum uses values 0..10 for avatars and 11 for an
    ordinary weapon. With item-list mode 3, both auxiliary blobs are present
    for exactly those equipment types.
    """
    equipment_type = " ".join(
        str(getattr(metadata, "equipment_type", "") or "").strip().lower().split()
    )
    return bool(getattr(metadata, "is_avatar", False)) or equipment_type == "[weapon]"


def _append_zero_common_item_tail(body: bytearray) -> None:
    body.extend(struct.pack("<I", 0))
    body.extend(b"\x00" * 3)
    body.extend(struct.pack("<H", 0))
    body.extend(b"\x00" * 5)
    body.extend(struct.pack("<II", 0, 0))
    body.append(0)
    body.extend(struct.pack("<I", 0))


def build_lottery_item_success(
    *,
    source_slot: int,
    reward_slot: int,
    item_id: int,
    count: int,
    include_item_extensions: bool = False,
) -> bytes:
    """Build S1/001B for Stage3 reader VA 0x0162B980.

    The common dispatcher consumes the leading result byte. Ordinary rewards
    produce a 46-byte body. Item kinds selected by the client's extension
    predicate consume two additional u32-length blobs after the first fields.
    PVF gold results use the native Main-wallet identity ``slot=0, item_id=0``.
    """
    iid = int(item_id)
    quantity = int(count)
    if (
        source_slot < 0
        or source_slot > 0xFFFF
        or reward_slot < 0
        or reward_slot > 0xFFFF
        or iid < 0
        or iid > 0xFFFFFFFF
        or (iid == 0 and reward_slot != 0)
        or quantity <= 0
        or quantity > 0xFFFFFFFF
    ):
        raise ValueError("invalid S1/001B result values")
    body = bytearray(
        struct.pack(
            "<BHHIIHBBH",
            1,
            int(source_slot),
            int(reward_slot),
            iid,
            quantity,
            0,
            0,
            0,
            0,
        )
    )
    if include_item_extensions:
        body.extend(struct.pack("<II", 0, 0))
    _append_zero_common_item_tail(body)
    return bytes(body)


def build_random_box_success(
    *,
    box_slot: int,
    material_slot: int,
    rewards: Iterable[RandomRewardCandidate],
    display_slots: Iterable[int] | None = None,
) -> bytes:
    """Build S1/00D0 for Stage3 reader VA 0x01606DA0."""
    rows = tuple(rewards)
    if display_slots is None:
        slots = (0xFFFF,) * len(rows)
    else:
        slots = tuple(int(value) for value in display_slots)
    if (
        box_slot < 0
        or box_slot > 0xFFFF
        or material_slot < 0
        or material_slot > 0xFFFF
        or len(rows) > 0xFFFF
        or len(slots) != len(rows)
    ):
        raise ValueError("invalid S1/00D0 header values")
    body = bytearray(struct.pack("<BHHH", 1, int(box_slot), int(material_slot), len(rows)))
    for reward, display_slot in zip(rows, slots):
        item_id = int(reward.item_id)
        count = int(reward.count)
        if (
            item_id <= 0
            or item_id > 0xFFFFFFFF
            or count <= 0
            or count > 0xFFFFFFFF
            or display_slot < 0
            or display_slot > 0xFFFF
        ):
            raise ValueError(
                "invalid S1/00D0 reward item=%d count=%d slot=%d"
                % (item_id, count, display_slot)
            )
        # Stage3 stores this byte in ``n2``.  Values >= 2 force the result
        # window into its exceptional post-result state; ordinary reward rows
        # use the native zero value.
        body.extend(struct.pack("<HIIHBI", display_slot, item_id, count, 0, 0, 0))
        # Stage3 invokes sub_1570E00 immediately after this u32.  Its first
        # byte is the nested-entry count; zero means there are no entries.
        # The three following zeroes belong to the ordinary row fields.
        body.extend(b"\x00" * 4)
        body.extend(struct.pack("<H", 0))
        body.extend(b"\x00" * 5)
        body.extend(struct.pack("<II", 0, 0))
        body.append(0)
        body.extend(struct.pack("<I", 0))
    body.append(0)
    return bytes(body)


def selected_character_id(session: Any) -> int | None:
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


def account_id_for_character(repo: Any, character_id: int) -> int | None:
    try:
        value = repo.get_account_id_for_character(int(character_id))
        account_id = int(value or 0)
    except (TypeError, ValueError):
        return None
    return account_id if account_id > 0 else None


def build_random_reward_grants(
    rewards: Iterable[RandomRewardCandidate],
    *,
    now_unix: int,
) -> tuple[list[InventoryGrantSpec], dict[int, ItemMetadata]]:
    grants: list[InventoryGrantSpec] = []
    metadata_by_id: dict[int, ItemMetadata] = {}
    for reward in rewards:
        item_id = int(reward.item_id) & 0xFFFFFFFF
        count = int(reward.count)
        metadata = metadata_by_id.get(item_id)
        if metadata is None:
            metadata = resolve_item_metadata(item_id)
            metadata_by_id[item_id] = metadata
        if (
            item_id <= 0
            or count <= 0
            or count > 0xFFFFFFFF
            or metadata.item_kind not in {"equipment", "stackable"}
            or not str(metadata.note or "").startswith("ok")
        ):
            raise ValueError(
                "random reward metadata unavailable item=%d count=%d kind=%s note=%s"
                % (item_id, count, metadata.item_kind, metadata.note)
            )
        grants.append(
            InventoryGrantSpec(
                item_id=item_id,
                count=count,
                metadata=metadata,
                expire_time=resolve_purchase_expire_time(
                    metadata,
                    now_unix=now_unix,
                ),
            )
        )
    if not grants:
        raise ValueError("random reward list is empty")
    return grants, metadata_by_id


def resolve_random_box_display_slots(
    detail: dict[str, Any],
    rewards: Iterable[RandomRewardCandidate],
) -> tuple[tuple[int, ...], dict[int, tuple[int, ...]]]:
    """Map committed reward grants to their Stage3 S1/00D0 display slots.

    The S1/00D0 reader first resolves a non-``0xFFFF`` display slot from the
    already-populated item container.  ``open_cera_package`` preserves grant
    order and returns every concrete recipient slot, so this mapping keeps the
    special magic-box UI on that native lookup path instead of its nullable
    temporary-item fallback.
    """
    reward_rows = tuple(rewards)
    grant_rows = tuple(dict(value) for value in (detail.get("grants", ()) or ()))
    if len(grant_rows) != len(reward_rows):
        raise ValueError(
            "random-box grant/display count mismatch grants=%d rewards=%d"
            % (len(grant_rows), len(reward_rows))
        )

    display_slots: list[int] = []
    refresh_slots: dict[int, set[int]] = {}
    for index, (reward, grant) in enumerate(zip(reward_rows, grant_rows)):
        item_id = int(reward.item_id) & 0xFFFFFFFF
        count = int(reward.count)
        grant_item_id = int(grant.get("item_id", 0) or 0) & 0xFFFFFFFF
        grant_count = int(grant.get("count", 0) or 0)
        raw_list_type = grant.get("list_type", -1)
        list_type = int(-1 if raw_list_type is None else raw_list_type)
        slots = tuple(int(value) for value in (grant.get("slots", ()) or ()))
        is_system_activation = bool(grant.get("system_activation"))
        valid_inventory_grant = (
            list_type in REFRESHABLE_INVENTORY_LIST_TYPES
            and bool(slots)
        )
        valid_system_grant = is_system_activation and (
            (list_type < 0 and not slots) or valid_inventory_grant
        )
        if (
            grant_item_id != item_id
            or grant_count != count
            or not (valid_inventory_grant or valid_system_grant)
            or any(slot < 0 or slot > 0xFFFF for slot in slots)
        ):
            raise ValueError(
                "invalid random-box grant/display index=%d item=%d count=%d grant=%r"
                % (index, item_id, count, grant)
            )
        display_slots.append(0xFFFF if is_system_activation else slots[0])
        if slots:
            refresh_slots.setdefault(list_type, set()).update(slots)

    return tuple(display_slots), {
        list_type: tuple(sorted(slots))
        for list_type, slots in sorted(refresh_slots.items())
    }


def resolve_lottery_reward_slot(detail: dict[str, Any]) -> int:
    """Return the S1/001B display slot for its single committed reward."""
    grant_rows = tuple(dict(value) for value in (detail.get("grants", ()) or ()))
    if len(grant_rows) != 1:
        raise ValueError("lottery grant count mismatch grants=%d" % len(grant_rows))
    grant = grant_rows[0]
    slots = tuple(int(value) for value in (grant.get("slots", ()) or ()))
    is_system_activation = bool(grant.get("system_activation"))
    if is_system_activation:
        if any(slot < 0 or slot > 0xFFFF for slot in slots):
            raise ValueError("invalid lottery system grant slots=%r" % (slots,))
        return 0xFFFF
    if not slots or any(slot < 0 or slot > 0xFFFF for slot in slots):
        raise ValueError("invalid lottery inventory grant slots=%r" % (slots,))
    primary = int(detail.get("primary_reward_slot", -1))
    if primary != slots[0]:
        raise ValueError(
            "lottery primary reward slot mismatch primary=%d slots=%r"
            % (primary, slots)
        )
    return primary


def build_changed_inventory_refreshes(
    server: Any,
    repo: Any,
    *,
    character_id: int,
    detail: dict[str, Any],
    now_unix: int,
    only_slots_by_list_type: dict[int, Iterable[int]] | None = None,
) -> tuple[list[bytes], tuple[str, ...]]:
    packets: list[bytes] = []
    notes: list[str] = []
    changed_map = dict(detail.get("changed_slots", {}) or {})
    slot_filter: dict[int, set[int]] | None = None
    if only_slots_by_list_type is not None:
        slot_filter = {}
        for list_type, values in dict(only_slots_by_list_type).items():
            normalized_list_type = int(list_type)
            if normalized_list_type not in REFRESHABLE_INVENTORY_LIST_TYPES:
                continue
            slot_filter[normalized_list_type] = {
                int(value) & 0xFFFF for value in (values or ())
            }
    changed_list_types = sorted(
        {
            int(value)
            for value in list(detail.get("changed_list_types", ()) or ())
            if int(value) in REFRESHABLE_INVENTORY_LIST_TYPES
        }
    )
    for list_type in changed_list_types:
        changed_slots = sorted(
            {
                int(value) & 0xFFFF
                for value in list(changed_map.get(list_type, ()) or ())
            }
        )
        if slot_filter is not None:
            allowed_slots = slot_filter.get(list_type, set())
            changed_slots = [slot for slot in changed_slots if slot in allowed_slots]
        if not changed_slots:
            continue
        rows = repo.list_inventory_items(character_id, list_type=list_type)
        by_slot = {
            int(row.get("slot_index", 0) or 0) & 0xFFFF: row
            for row in rows
            if (int(row.get("slot_index", 0) or 0) & 0xFFFF) in changed_slots
        }
        update_items: list[InventoryItemView] = []
        for slot in changed_slots:
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
        body = build_item_list_update_000e(
            update_items,
            list_type=list_type,
            avatar=(list_type == 1),
            now_unix=now_unix,
        )
        packets.append(server._codec_response(0, 0x000E, body))
        notes.append("list=%d slots=%s" % (list_type, changed_slots))
    return packets, tuple(notes)


__all__ = [
    "ERROR_INVALID_CONTAINER",
    "account_id_for_character",
    "build_changed_inventory_refreshes",
    "build_lottery_item_success",
    "build_random_box_success",
    "build_random_container_failure",
    "build_random_reward_grants",
    "lottery_item_uses_extension_blobs",
    "resolve_random_box_display_slots",
    "selected_character_id",
]
