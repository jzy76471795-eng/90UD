"""Durable Avatar Closet list33 storage, selection, and outfit exchange."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from game.character.character_db import (
    CharacterRepository,
    _userinfo_equipment_raw_entry,
)
from game.character.item_metadata import ItemMetadata, resolve_item_metadata


AVATAR_CLOSET_SET_WIDTH = 12
AVATAR_CLOSET_MAX_SETS = 4
AVATAR_CLOSET_WEAR_SLOTS = tuple(range(AVATAR_CLOSET_SET_WIDTH))
AVATAR_CLOSET_LIST_TYPE = 33
AVATAR_CLOSET_SOURCE_UID_EXTRA_KEY = "usdof_avatar_closet_source_uid"


@dataclass(frozen=True)
class AvatarClosetMove:
    source_list: int
    source_slot: int
    destination_list: int
    destination_slot: int


def avatar_closet_slot(set_index: int, wear_slot: int) -> int:
    target = int(set_index)
    slot = int(wear_slot)
    if not 0 <= target < AVATAR_CLOSET_MAX_SETS:
        raise ValueError("Avatar Closet set index is outside 0..3")
    if slot not in AVATAR_CLOSET_WEAR_SLOTS:
        raise ValueError("Avatar Closet wear slot is outside 0..11")
    return target * AVATAR_CLOSET_SET_WIDTH + slot


def _is_ordinary_avatar(metadata: ItemMetadata, wear_slot: int) -> bool:
    return bool(
        metadata.is_avatar
        and not metadata.is_clone_avatar
        and int(wear_slot) in metadata.compatible_equipment_slots()
    )


def _is_avatar_for_wear_slot(metadata: ItemMetadata, wear_slot: int) -> bool:
    return bool(
        metadata.is_avatar
        and int(wear_slot) in metadata.compatible_equipment_slots()
    )


def _closet_slot_accepts(metadata: ItemMetadata, slot: int) -> bool:
    return _is_ordinary_avatar(metadata, int(slot) % AVATAR_CLOSET_SET_WIDTH)


def _state_tx(
    conn: sqlite3.Connection,
    character_id: int,
) -> Optional[tuple[int, int]]:
    cid = int(character_id)
    conn.execute(
        "INSERT OR IGNORE INTO character_avatar_closet_state "
        "(character_id, unlocked_set_count, current_set_index) "
        "SELECT character_id, 2, 0 FROM characters "
        "WHERE character_id = ? AND delete_flag = 0",
        (cid,),
    )
    row = conn.execute(
        "SELECT unlocked_set_count, current_set_index "
        "FROM character_avatar_closet_state WHERE character_id = ?",
        (cid,),
    ).fetchone()
    if row is None:
        return None
    unlocked = max(1, min(AVATAR_CLOSET_MAX_SETS, int(row[0] or 2)))
    current = max(0, min(unlocked - 1, int(row[1] or 0)))
    return unlocked, current


def _validate_target(unlocked: int, target_set_index: int) -> bool:
    target = int(target_set_index)
    return 0 <= target < int(unlocked) and target < AVATAR_CLOSET_MAX_SETS


def move_avatar_closet_item(
    repo: CharacterRepository,
    character_id: int,
    *,
    source_list_type: int,
    source_slot: int,
    source_instance_value: int,
    move_count: int,
    destination_list_type: int,
    destination_slot: int,
) -> tuple[bool, str, dict[str, int]]:
    """Move real avatar instances between list1 and the four list33 sets."""
    repo._ensure_schema()
    cid = int(character_id)
    src_lt = int(source_list_type) & 0xFF
    dst_lt = int(destination_list_type) & 0xFF
    src_slot = int(source_slot)
    dst_slot = int(destination_slot)
    want_id = int(source_instance_value) & 0xFFFFFFFF
    count = max(0, int(move_count))
    result = {
        "source_list_type": src_lt,
        "source_slot": src_slot & 0xFFFF,
        "destination_list_type": dst_lt,
        "destination_slot": dst_slot & 0xFFFF,
        "move_value32": count if count > 0 else want_id,
        "item_id": 0,
        "mutated": 0,
        "avatar_closet_changed": 1,
    }
    allowed = {int(repo.INVENTORY_LIST_AVATAR), AVATAR_CLOSET_LIST_TYPE}
    if src_lt not in allowed or dst_lt not in allowed:
        return False, "avatar_closet_cross_container_forbidden", result
    if AVATAR_CLOSET_LIST_TYPE not in (src_lt, dst_lt):
        return False, "avatar_closet_list33_required", result

    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = _state_tx(conn, cid)
            if state is None:
                return False, "character_missing", result
            unlocked, _current = state
            closet_end = int(unlocked) * AVATAR_CLOSET_SET_WIDTH - 1

            avatar_state = conn.execute(
                "SELECT list_param16 FROM character_container_state "
                "WHERE character_id = ? AND list_type = ?",
                (cid, int(repo.INVENTORY_LIST_AVATAR)),
            ).fetchone()
            avatar_expansion = (
                max(0, min(int(avatar_state[0] or 0), repo.MAX_AVATAR_LIST_PARAM16))
                if avatar_state is not None
                else repo.DEFAULT_AVATAR_LIST_PARAM16
            )
            avatar_end = repo.BASE_AVATAR_SLOT_COUNT + avatar_expansion - 1

            def _validate_slot(
                list_type: int,
                slot: int,
                label: str,
            ) -> Optional[str]:
                if list_type == AVATAR_CLOSET_LIST_TYPE:
                    if not 0 <= slot <= closet_end:
                        return "%s_closet_slot_locked slot=%d end=%d" % (
                            label,
                            slot,
                            closet_end,
                        )
                elif not 0 <= slot <= avatar_end:
                    return "%s_avatar_slot_out_of_range slot=%d end=%d" % (
                        label,
                        slot,
                        avatar_end,
                    )
                return None

            invalid = _validate_slot(src_lt, src_slot, "source")
            if invalid is None:
                invalid = _validate_slot(dst_lt, dst_slot, "destination")
            if invalid is not None:
                return False, invalid, result

            def _at(list_type: int, slot: int) -> Optional[sqlite3.Row]:
                return conn.execute(
                    "SELECT item_uid, item_template_id, stack_count "
                    "FROM character_items WHERE character_id = ? "
                    "AND owner_scope = 'character' AND owner_id = ? "
                    "AND list_type = ? AND slot_index = ? LIMIT 1",
                    (cid, cid, int(list_type), int(slot)),
                ).fetchone()

            def _can_place(row: sqlite3.Row, list_type: int, slot: int) -> bool:
                metadata = resolve_item_metadata(int(row["item_template_id"]))
                if not metadata.is_avatar:
                    return False
                if list_type == AVATAR_CLOSET_LIST_TYPE:
                    return _closet_slot_accepts(metadata, slot)
                return True

            source = _at(src_lt, src_slot)
            destination = _at(dst_lt, dst_slot)
            if src_lt == dst_lt and src_slot == dst_slot:
                if source is None:
                    return False, "avatar_closet_source_empty", result
                source_item_id = int(source["item_template_id"]) & 0xFFFFFFFF
                if want_id > 0 and source_item_id != want_id:
                    return False, (
                        "avatar_closet_source_mismatch want=%d found=%d"
                        % (want_id, source_item_id)
                    ), result
                result.update(
                    item_id=source_item_id,
                    move_value32=max(1, int(source["stack_count"] or 1)),
                    unlocked_set_count=int(unlocked),
                )
                return True, "avatar_closet_same_slot slot=%d" % src_slot, result
            reverse_empty_source = source is None
            if source is None:
                if destination is None:
                    return False, "avatar_closet_source_empty", result
                moving = destination
                moving_lt, moving_slot = dst_lt, dst_slot
                target_lt, target_slot = src_lt, src_slot
            else:
                moving = source
                moving_lt, moving_slot = src_lt, src_slot
                target_lt, target_slot = dst_lt, dst_slot
                source_item_id = int(source["item_template_id"]) & 0xFFFFFFFF
                if want_id > 0 and source_item_id != want_id:
                    return False, (
                        "avatar_closet_source_mismatch want=%d found=%d"
                        % (want_id, source_item_id)
                    ), result

            if not _can_place(moving, target_lt, target_slot):
                return False, (
                    "avatar_closet_item_slot_mismatch item=%d list=%d slot=%d"
                    % (
                        int(moving["item_template_id"]),
                        target_lt,
                        target_slot,
                    )
                ), result
            if destination is not None and source is not None:
                if not _can_place(destination, src_lt, src_slot):
                    return False, (
                        "avatar_closet_swap_slot_mismatch item=%d list=%d slot=%d"
                        % (
                            int(destination["item_template_id"]),
                            src_lt,
                            src_slot,
                        )
                    ), result

            moving_uid = int(moving["item_uid"])
            moving_item_id = int(moving["item_template_id"]) & 0xFFFFFFFF
            if destination is None or reverse_empty_source:
                conn.execute(
                    "UPDATE character_items SET list_type = ?, slot_index = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                    (target_lt, target_slot, moving_uid),
                )
                action = "fill_empty" if reverse_empty_source else "move"
            else:
                temporary_slot = 65000
                conn.execute(
                    "UPDATE character_items SET slot_index = ? WHERE item_uid = ?",
                    (temporary_slot, int(source["item_uid"])),
                )
                conn.execute(
                    "UPDATE character_items SET list_type = ?, slot_index = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                    (src_lt, src_slot, int(destination["item_uid"])),
                )
                conn.execute(
                    "UPDATE character_items SET list_type = ?, slot_index = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                    (dst_lt, dst_slot, int(source["item_uid"])),
                )
                action = "swap"

            result.update(
                item_id=moving_item_id,
                mutated=1,
                move_value32=max(1, int(moving["stack_count"] or 1)),
                unlocked_set_count=int(unlocked),
            )
            return True, (
                "avatar_closet_%s item=%d (%d,%d)->(%d,%d) unlocked=%d "
                "attempt=1/3"
                % (
                    action,
                    moving_item_id,
                    moving_lt,
                    moving_slot,
                    target_lt,
                    target_slot,
                    unlocked,
                )
            ), result
    except sqlite3.IntegrityError as exc:
        return False, "avatar_closet_integrity:%s" % exc, result
    except Exception as exc:
        return False, "avatar_closet_move_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), result


def change_avatar_closet_display_set(
    repo: CharacterRepository,
    character_id: int,
    target_set_index: int,
) -> tuple[bool, str, dict[str, Any]]:
    """Persist the set shown by the closet UI and Seria-room mannequin."""
    repo._ensure_schema()
    cid = int(character_id)
    target = int(target_set_index)
    detail: dict[str, Any] = {
        "character_id": cid,
        "target_set_index": target,
    }
    if cid <= 0 or not 0 <= target < AVATAR_CLOSET_MAX_SETS:
        return False, "invalid_character_or_set", detail

    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = _state_tx(conn, cid)
            if state is None:
                return False, "character_missing", detail
            unlocked, current = state
            detail.update(
                unlocked_set_count=unlocked,
                current_set_index_before=current,
            )
            if not _validate_target(unlocked, target):
                return False, "set_locked target=%d unlocked=%d" % (
                    target,
                    unlocked,
                ), detail
            conn.execute(
                "UPDATE character_avatar_closet_state SET current_set_index = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE character_id = ?",
                (target, cid),
            )
            detail["current_set_index"] = target
            return True, "changed display_set=%d previous=%d" % (
                target,
                current,
            ), detail
    except Exception as exc:
        return False, "avatar_closet_display_change_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), detail


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _extra_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items()}


def _avatar_closet_source_uid(extra_json: Any) -> int:
    value = _extra_json_object(extra_json).get(AVATAR_CLOSET_SOURCE_UID_EXTRA_KEY, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _serialize_extra_json_without_closet_source(extra_json: Any) -> str:
    payload = _extra_json_object(extra_json)
    payload.pop(AVATAR_CLOSET_SOURCE_UID_EXTRA_KEY, None)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _serialize_extra_json_with_closet_source(extra_json: Any, source_uid: int) -> str:
    payload = _extra_json_object(extra_json)
    payload[AVATAR_CLOSET_SOURCE_UID_EXTRA_KEY] = max(1, int(source_uid))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _insert_worn_avatar_in_inventory_tx(
    conn: sqlite3.Connection,
    repo: CharacterRepository,
    cid: int,
    inventory_slot: int,
    worn: Mapping[str, Any],
    metadata: ItemMetadata,
) -> None:
    conn.execute(
        "INSERT INTO character_items "
        "(owner_scope, owner_id, character_id, list_type, slot_index, "
        "item_template_id, item_kind, stack_count, instance_value, durability, "
        "seal_flag, option_value, expire_time, marker_16, equipment_lock_id, "
        "extra_json) "
        "VALUES ('character', ?, ?, ?, ?, ?, 'avatar', 1, 1, ?, 0, ?, ?, ?, ?, ?)",
        (
            cid,
            cid,
            int(repo.INVENTORY_LIST_AVATAR),
            int(inventory_slot),
            int(worn["item_id"]) & 0xFFFFFFFF,
            max(0, int(metadata.durability or 0)) & 0xFFFF,
            int(worn["option_value"] or 0) & 0xFF,
            int(worn["expire_time"] or 0) & 0xFFFFFFFF,
            int(worn["marker_16"] or 0) & 0xFFFF,
            int(worn["equipment_lock_id"] or 0) & 0xFF,
            _serialize_extra_json_without_closet_source(worn["extra_json"]),
        ),
    )


def _insert_closet_avatar_as_worn_tx(
    conn: sqlite3.Connection,
    cid: int,
    wear_slot: int,
    source: Mapping[str, Any],
) -> None:
    item_id = int(source["item_template_id"]) & 0xFFFFFFFF
    expire_time = int(source["expire_time"] or 0) & 0xFFFFFFFF
    raw_entry = _userinfo_equipment_raw_entry(wear_slot, item_id, expire_time)
    conn.execute(
        "INSERT INTO character_equipped_entries "
        "(character_id, slot, item_id, expire_time, equipment_lock_id, raw_entry, "
        "title_source, title_category, title_book_index, extra_json, option_value, "
        "marker_16, clear_avatar_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, -1, -1, ?, ?, ?, 0)",
        (
            cid,
            int(wear_slot),
            item_id,
            expire_time,
            int(source["equipment_lock_id"] or 0) & 0xFF,
            sqlite3.Binary(raw_entry),
            _serialize_extra_json_with_closet_source(
                source["extra_json"],
                int(source["item_uid"]),
            ),
            int(source["option_value"] or 0) & 0xFF,
            int(source["marker_16"] or 0) & 0xFFFF,
        ),
    )


def select_avatar_closet_outfit(
    repo: CharacterRepository,
    character_id: int,
    target_set_index: int,
) -> tuple[bool, str, dict[str, Any]]:
    """Project one persistent list33 outfit without changing the display set."""
    repo._ensure_schema()
    cid = int(character_id)
    target = int(target_set_index)
    detail: dict[str, Any] = {
        "character_id": cid,
        "target_set_index": target,
        "moves": [],
        "returned_inventory_slots": [],
        "released_closet_projection_slots": [],
        "unchanged_closet_projection_slots": [],
    }
    if cid <= 0 or not 0 <= target < AVATAR_CLOSET_MAX_SETS:
        return False, "invalid_character_or_set", detail

    try:
        with repo.open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = _state_tx(conn, cid)
            if state is None:
                return False, "character_missing", detail
            unlocked, current_set = state
            detail.update(
                unlocked_set_count=unlocked,
                current_set_index_before=current_set,
                current_set_index=current_set,
            )
            if not _validate_target(unlocked, target):
                return False, "set_locked target=%d unlocked=%d" % (
                    target,
                    unlocked,
                ), detail

            closet_start = avatar_closet_slot(target, 0)
            closet_end = avatar_closet_slot(target, AVATAR_CLOSET_SET_WIDTH - 1)
            closet_rows = conn.execute(
                "SELECT item_uid, slot_index, item_template_id, expire_time, "
                "equipment_lock_id, extra_json, option_value, marker_16 "
                "FROM character_items WHERE character_id = ? "
                "AND owner_scope = 'character' AND owner_id = ? AND list_type = ? "
                "AND slot_index BETWEEN ? AND ? ORDER BY slot_index",
                (
                    cid,
                    cid,
                    AVATAR_CLOSET_LIST_TYPE,
                    closet_start,
                    closet_end,
                ),
            ).fetchall()
            closet: dict[int, dict[str, Any]] = {}
            for row in closet_rows:
                source = _row_dict(row)
                wear_slot = int(source["slot_index"]) - closet_start
                metadata = resolve_item_metadata(int(source["item_template_id"]))
                if not _is_ordinary_avatar(metadata, wear_slot):
                    return False, "closet_item_slot_mismatch slot=%d item=%d" % (
                        int(source["slot_index"]),
                        int(source["item_template_id"]),
                    ), detail
                closet[wear_slot] = source

            equipped_rows = conn.execute(
                "SELECT slot, item_id, expire_time, equipment_lock_id, extra_json, "
                "option_value, marker_16, clear_avatar_id "
                "FROM character_equipped_entries WHERE character_id = ? "
                "AND slot BETWEEN 0 AND 11 ORDER BY slot",
                (cid,),
            ).fetchall()
            equipped: dict[int, dict[str, Any]] = {}
            for row in equipped_rows:
                worn = _row_dict(row)
                wear_slot = int(worn["slot"])
                metadata = resolve_item_metadata(int(worn["item_id"]))
                if not metadata.is_avatar:
                    continue
                if not _is_avatar_for_wear_slot(metadata, wear_slot):
                    return False, "worn_item_slot_mismatch slot=%d item=%d" % (
                        wear_slot,
                        int(worn["item_id"]),
                    ), detail
                equipped[wear_slot] = worn

            avatar_state = conn.execute(
                "SELECT list_param16 FROM character_container_state "
                "WHERE character_id = ? AND list_type = ?",
                (cid, int(repo.INVENTORY_LIST_AVATAR)),
            ).fetchone()
            avatar_expansion = (
                max(
                    0,
                    min(
                        int(avatar_state[0] or 0),
                        int(repo.MAX_AVATAR_LIST_PARAM16),
                    ),
                )
                if avatar_state is not None
                else int(repo.DEFAULT_AVATAR_LIST_PARAM16)
            )
            avatar_end = int(repo.BASE_AVATAR_SLOT_COUNT) + avatar_expansion - 1
            occupied_inventory_slots = {
                int(row[0])
                for row in conn.execute(
                    "SELECT slot_index FROM character_items "
                    "WHERE character_id = ? AND owner_scope = 'character' "
                    "AND owner_id = ? AND list_type = ? "
                    "AND slot_index BETWEEN 0 AND ?",
                    (
                        cid,
                        cid,
                        int(repo.INVENTORY_LIST_AVATAR),
                        avatar_end,
                    ),
                ).fetchall()
            }
            free_inventory_slots = [
                slot
                for slot in range(avatar_end + 1)
                if slot not in occupied_inventory_slots
            ]
            worn_return_count = sum(
                1
                for wear_slot in AVATAR_CLOSET_WEAR_SLOTS
                if closet.get(wear_slot) is not None
                and equipped.get(wear_slot) is not None
                and _avatar_closet_source_uid(
                    equipped[wear_slot].get("extra_json")
                )
                == 0
            )
            if len(free_inventory_slots) < worn_return_count:
                detail.update(
                    required_inventory_slots=worn_return_count,
                    free_inventory_slots=len(free_inventory_slots),
                )
                return False, (
                    "avatar_inventory_full required=%d free=%d"
                    % (worn_return_count, len(free_inventory_slots))
                ), detail

            moves: list[AvatarClosetMove] = []
            returned_inventory_slots: list[tuple[int, int]] = []
            released_closet_projection_slots: list[tuple[int, int]] = []
            unchanged_closet_projection_slots: list[tuple[int, int]] = []
            next_inventory_slot = iter(free_inventory_slots)
            for wear_slot in AVATAR_CLOSET_WEAR_SLOTS:
                source = closet.get(wear_slot)
                worn = equipped.get(wear_slot)
                # The Stage3 043F reader resolves source containers before it
                # dispatches on the destination. list3 is not a resolvable source
                # container. The list33 -> list3 native exchange is followed by
                # authoritative list1/list33 snapshots, so the persistent closet
                # source remains the outfit plan instead of being consumed.
                if source is None:
                    continue

                fixed_slot = avatar_closet_slot(target, wear_slot)
                source_uid = int(source["item_uid"])
                worn_source_uid = (
                    _avatar_closet_source_uid(worn.get("extra_json"))
                    if worn is not None
                    else 0
                )
                # The marker is written only by this repository. Keep treating the
                # worn row as a projection after its source leaves list33, otherwise
                # a later Change would duplicate a moved or disassembled source.
                worn_is_closet_projection = worn_source_uid > 0
                if worn_is_closet_projection and worn_source_uid == source_uid:
                    unchanged_closet_projection_slots.append((wear_slot, source_uid))
                    continue

                if worn is not None:
                    if worn_is_closet_projection:
                        released_closet_projection_slots.append(
                            (wear_slot, worn_source_uid)
                        )
                    else:
                        inventory_slot = next(next_inventory_slot)
                        worn_meta = resolve_item_metadata(int(worn["item_id"]))
                        _insert_worn_avatar_in_inventory_tx(
                            conn,
                            repo,
                            cid,
                            inventory_slot,
                            worn,
                            worn_meta,
                        )
                        returned_inventory_slots.append((wear_slot, inventory_slot))
                    conn.execute(
                        "DELETE FROM character_equipped_entries "
                        "WHERE character_id = ? AND slot = ?",
                        (cid, wear_slot),
                    )
                _insert_closet_avatar_as_worn_tx(conn, cid, wear_slot, source)
                moves.append(
                    AvatarClosetMove(
                        source_list=AVATAR_CLOSET_LIST_TYPE,
                        source_slot=fixed_slot,
                        destination_list=3,
                        destination_slot=wear_slot,
                    )
                )

            detail["moves"] = moves
            detail["returned_inventory_slots"] = returned_inventory_slots
            detail["released_closet_projection_slots"] = (
                released_closet_projection_slots
            )
            detail["unchanged_closet_projection_slots"] = (
                unchanged_closet_projection_slots
            )
            return True, (
                "selected outfit_set=%d display_set_preserved=%d moves=%d "
                "returned_to_list1=%s released_projections=%s unchanged_projections=%s"
            ) % (
                target,
                current_set,
                len(moves),
                returned_inventory_slots,
                released_closet_projection_slots,
                unchanged_closet_projection_slots,
            ), detail
    except Exception as exc:
        return False, "avatar_closet_outfit_select_error:%s:%s" % (
            type(exc).__name__,
            exc,
        ), detail


__all__ = [
    "AVATAR_CLOSET_LIST_TYPE",
    "AVATAR_CLOSET_MAX_SETS",
    "AVATAR_CLOSET_SET_WIDTH",
    "AVATAR_CLOSET_SOURCE_UID_EXTRA_KEY",
    "AVATAR_CLOSET_WEAR_SLOTS",
    "AvatarClosetMove",
    "avatar_closet_slot",
    "change_avatar_closet_display_set",
    "move_avatar_closet_item",
    "select_avatar_closet_outfit",
]
