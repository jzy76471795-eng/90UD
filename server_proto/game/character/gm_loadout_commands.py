"""Control-plane commands for character-scoped one-click loadouts."""
from __future__ import annotations

import time
from typing import Any, Mapping

from control.contracts import ControlApiError
from game.client_toast_bridge import (
    ClientToastBridgeError,
    TOAST_MODE_COMFIRM,
    TOAST_MODE_TOAST,
    send_client_toast,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.native_loadout_bridge import (
    NativeLoadoutAction,
    active_native_loadout_state,
    native_loadout_cooldown_remaining,
    native_loadout_enabled,
    queue_native_loadout_plan,
)
from game.character.payloads import (
    build_avatar_item_list_mode0,
    build_creature_item_list,
    build_item_list_mode0,
    build_pet_item_list_mode0,
)
from game.shop.premium_contract_index import get_active_premium_effects


LOADOUT_COUNT = 10
LOADOUT_SLOT_SPECS: dict[str, dict[str, Any]] = {
    # Avatar inventory is list1. The illusion-aura slot 11 is intentionally
    # absent; the normal attribute aura is slot 9.
    "avatar_weapon": {"group": "avatar", "slot": 10, "source_list_types": (1,)},
    "avatar_aura": {"group": "avatar", "slot": 9, "source_list_types": (1,)},
    "avatar_hat": {"group": "avatar", "slot": 0, "source_list_types": (1,)},
    "avatar_hair": {"group": "avatar", "slot": 1, "source_list_types": (1,)},
    "avatar_face": {"group": "avatar", "slot": 2, "source_list_types": (1,)},
    "avatar_neck": {"group": "avatar", "slot": 6, "source_list_types": (1,)},
    "avatar_top": {"group": "avatar", "slot": 3, "source_list_types": (1,)},
    "avatar_bottom": {"group": "avatar", "slot": 4, "source_list_types": (1,)},
    "avatar_waist": {"group": "avatar", "slot": 7, "source_list_types": (1,)},
    "avatar_shoes": {"group": "avatar", "slot": 5, "source_list_types": (1,)},
    "avatar_skin": {"group": "avatar", "slot": 8, "source_list_types": (1,)},
    # Body equipment can be sourced from Main or PersonalCargo.
    "weapon": {"group": "equipment", "slot": 12, "source_list_types": (0, 2)},
    "title": {"group": "equipment", "slot": 13, "source_list_types": (0, 2)},
    "top": {"group": "equipment", "slot": 14, "source_list_types": (0, 2)},
    "head_shoulder": {"group": "equipment", "slot": 15, "source_list_types": (0, 2)},
    "bottom": {"group": "equipment", "slot": 16, "source_list_types": (0, 2)},
    "shoes": {"group": "equipment", "slot": 17, "source_list_types": (0, 2)},
    "belt": {"group": "equipment", "slot": 18, "source_list_types": (0, 2)},
    "necklace": {"group": "equipment", "slot": 19, "source_list_types": (0, 2)},
    "bracelet": {"group": "equipment", "slot": 20, "source_list_types": (0, 2)},
    "ring": {"group": "equipment", "slot": 21, "source_list_types": (0, 2)},
    "support": {"group": "equipment", "slot": 22, "source_list_types": (0, 2)},
    "magic_stone": {"group": "equipment", "slot": 23, "source_list_types": (0, 2)},
    "earring": {"group": "equipment", "slot": 25, "source_list_types": (0, 2)},
    # Pet and three pet artifacts live in the dedicated pet inventory list7.
    "pet": {"group": "pet", "slot": 26, "source_list_types": (7,)},
    "pet_red": {"group": "pet", "slot": 27, "source_list_types": (7,)},
    "pet_blue": {"group": "pet", "slot": 28, "source_list_types": (7,)},
    "pet_green": {"group": "pet", "slot": 29, "source_list_types": (7,)},
}
LOADOUT_SLOT_KEYS = tuple(LOADOUT_SLOT_SPECS)
# Temporary server-side application scope. Keep the complete key set above so
# saved GM plans and their UI fields remain intact; restore all categories by
# assigning LOADOUT_SLOT_KEYS here after avatar/pet switching is revisited.
LOADOUT_APPLICATION_SLOT_KEYS = tuple(
    key
    for key in LOADOUT_SLOT_KEYS
    if LOADOUT_SLOT_SPECS[key]["group"] == "equipment"
)


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _int_or_zero(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _item_id(value: object) -> int:
    return _int_or_zero(value) & 0xFFFFFFFF


def _empty_item() -> dict[str, Any]:
    return {"display_name": "", "item_id": None}


def _normalize_item(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _empty_item()
    iid = _item_id(value.get("item_id"))
    if iid <= 0:
        return _empty_item()
    item: dict[str, Any] = {
        "display_name": _clip(value.get("display_name", value.get("name", "")), 80),
        "item_id": iid,
    }
    for key in (
        "item_uid",
        "source_item_uid",
        "list_type",
        "source_list_type",
        "slot_index",
        "source_slot",
        "equipped_slot",
    ):
        if key not in value or value.get(key) in (None, ""):
            continue
        numeric = _int_or_zero(value.get(key))
        if key in {"item_uid", "source_item_uid"} and numeric <= 0:
            continue
        item[key] = numeric
    source = _clip(value.get("source"), 24)
    if source:
        item["source"] = source
    if not item["display_name"]:
        item["display_name"] = _clip(resolve_item_metadata(iid).name, 80)
    return item


def _normalize_items(value: object) -> dict[str, dict[str, Any]]:
    raw = value if isinstance(value, Mapping) else {}
    return {key: _normalize_item(raw.get(key)) for key in LOADOUT_SLOT_KEYS}


def _normalize_loadout(index: int, value: object) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "index": int(index),
        "name": _clip(raw.get("name"), 32) or "方案 %d" % (int(index) + 1),
        "items": _normalize_items(raw.get("items")),
    }


def _normalize_loadouts(records: object) -> list[dict[str, Any]]:
    values = records if isinstance(records, list) else []
    return [
        _normalize_loadout(
            index,
            values[index] if index < len(values) else {},
        )
        for index in range(LOADOUT_COUNT)
    ]


class GmLoadoutControlCommands:
    def __init__(self, server: Any) -> None:
        self.server = server

    @staticmethod
    def _character_id(session: Any) -> int:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            raise ControlApiError(
                "SESSION_NOT_READY",
                "current client has no selected character",
                status=409,
            )
        return character_id

    @staticmethod
    def _record_for_character(repo: Any, character_id: int) -> Any:
        """Return the authoritative character row for a control command."""
        account_id = repo.get_account_id_for_character(int(character_id))
        if account_id is None:
            return None
        for record in repo.list_by_account(int(account_id)):
            if int(getattr(record, "character_id", 0) or 0) == int(character_id):
                return record
        return None

    @staticmethod
    def _notify_loadout_started(loadout: Mapping[str, Any]) -> None:
        """Show the native progress toast without affecting the move result."""
        try:
            send_client_toast(
                "正在一键换装，换装方案%s" % _clip(loadout.get("name"), 32),
                mode=TOAST_MODE_TOAST,
            )
        except (ClientToastBridgeError, OSError):
            # The loadout path remains usable during a one-off toast A/B run.
            pass

    @staticmethod
    def _notify_loadout_in_progress() -> None:
        """Tell the client that its existing native plan remains authoritative."""
        try:
            send_client_toast(
                "换装进行中，请勿重复操作",
                mode=TOAST_MODE_COMFIRM,
            )
        except (ClientToastBridgeError, OSError):
            pass

    @staticmethod
    def _notify_loadout_cooldown() -> None:
        """Tell the player that a completed dungeon plan is still settling."""
        try:
            send_client_toast("装备冷却中", mode=TOAST_MODE_COMFIRM)
        except (ClientToastBridgeError, OSError):
            pass

    def list_loadouts(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "loadout.list does not accept parameters",
                status=400,
            )
        character_id = self._character_id(session)
        records = _normalize_loadouts(
            self.server._repo().list_character_loadouts(character_id)
        )
        return [], {
            "character_id": character_id,
            "loadouts": records,
        }, "loadout list character=%s" % character_id

    def save_loadout(self, params: dict[str, Any], session: Any):
        required = {"index", "name", "items"}
        if (
            not isinstance(params, dict)
            or set(params) != required
            or not isinstance(params.get("items"), Mapping)
        ):
            raise ControlApiError(
                "INVALID_PARAMS",
                "loadout.save requires index, name, and items",
                status=400,
            )
        index = _int_or_zero(params.get("index"))
        name = _clip(params.get("name"), 32)
        if not 0 <= index < LOADOUT_COUNT or not name:
            raise ControlApiError(
                "INVALID_PARAMS",
                "loadout index or name is invalid",
                status=400,
            )
        character_id = self._character_id(session)
        items = _normalize_items(params.get("items"))
        records = self.server._repo().save_character_loadout(
            character_id,
            loadout_index=index,
            name=name,
            items=items,
        )
        return [], {
            "character_id": character_id,
            "saved_index": index,
            "loadouts": _normalize_loadouts(records),
        }, "loadout save character=%s index=%s" % (character_id, index)

    def read_current(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "loadout.read_current does not accept parameters",
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        equipped = {
            int(row.get("slot", -1)): row
            for row in repo.list_equipped_items(character_id)
        }
        items: dict[str, dict[str, Any]] = {}
        for key, spec in LOADOUT_SLOT_SPECS.items():
            slot = int(spec["slot"])
            row = equipped.get(slot)
            if row is None or _item_id(row.get("item_id")) <= 0:
                items[key] = _empty_item()
                continue
            iid = _item_id(row.get("item_id"))
            items[key] = {
                "display_name": _clip(resolve_item_metadata(iid).name, 80),
                "item_id": iid,
                "source": "equipped",
                "equipped_slot": slot,
            }
        return [], {
            "character_id": character_id,
            "items": items,
            "read_at": int(time.time()),
        }, "loadout read current character=%s" % character_id

    def _container_packets(
        self,
        repo: Any,
        character_id: int,
        list_type: int,
    ) -> tuple[list[bytes], str]:
        now_unix = int(time.time())
        lt = int(list_type)
        rows = repo.list_inventory_items(
            int(character_id),
            list_type=lt,
            now_unix=now_unix,
        )
        items = [InventoryItemView(row) for row in rows]
        if lt == repo.INVENTORY_LIST_AVATAR:
            body = build_avatar_item_list_mode0(
                items,
                list_param16=int(
                    repo.get_container_list_param(
                        int(character_id), list_type=repo.INVENTORY_LIST_AVATAR
                    )
                ),
                now_unix=now_unix,
            )
            return [self.server._codec_response(0, 0x000D, body)], "S0/000D list1"
        if lt == repo.INVENTORY_LIST_PET:
            body = build_pet_item_list_mode0(items)
            packets = [self.server._codec_response(0, 0x000D, body)]
            packets.append(
                self.server._codec_response(
                    0,
                    0x0069,
                    build_creature_item_list(repo.list_creature_states(int(character_id))),
                )
            )
            return packets, "S0/000D list7,S0/0069"
        body = build_item_list_mode0(
            items,
            list_type=lt,
            list_param16=int(
                repo.get_container_list_param(int(character_id), list_type=lt)
            ),
        )
        return [self.server._codec_response(0, 0x000D, body)], "S0/000D list%d" % lt

    @staticmethod
    def _source_hint(item: Mapping[str, Any]) -> tuple[int, int, int, bool]:
        """Return the saved physical item identity, if the plan supplied one."""
        hinted_uid = _int_or_zero(
            item.get("item_uid", item.get("source_item_uid", 0))
        )
        hinted_list_type = _int_or_zero(
            item.get("list_type", item.get("source_list_type", -1))
        )
        hinted_slot = _int_or_zero(
            item.get("slot_index", item.get("source_slot", -1))
        )
        return (
            hinted_uid,
            hinted_list_type,
            hinted_slot,
            hinted_uid > 0 or (hinted_list_type >= 0 and hinted_slot >= 0),
        )

    def _find_source_item(
        self,
        repo: Any,
        character_id: int,
        item: Mapping[str, Any],
        spec: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Find the live source row using the same identity priority as saved plans."""
        item_id = _item_id(item.get("item_id"))
        if item_id <= 0:
            return None
        hinted_uid, hinted_list_type, hinted_slot, has_hint = self._source_hint(item)
        candidates: list[dict[str, Any]] = []
        for list_type in tuple(spec.get("source_list_types") or ()):
            lt = int(list_type)
            for row in repo.list_inventory_items(int(character_id), list_type=lt):
                if _item_id(row.get("item_template_id")) != item_id:
                    continue
                source = repo.get_inventory_item_at(
                    int(character_id),
                    list_type=lt,
                    slot_index=int(row.get("slot_index", -1)),
                )
                if source is not None:
                    candidates.append(source)

        if hinted_uid > 0:
            return next(
                (
                    row
                    for row in candidates
                    if int(row.get("item_uid", 0) or 0) == hinted_uid
                ),
                None,
            )
        if has_hint:
            return next(
                (
                    row
                    for row in candidates
                    if int(row.get("list_type", -1)) == hinted_list_type
                    and int(row.get("slot_index", -1)) == hinted_slot
                ),
                None,
            )
        return candidates[0] if candidates else None

    @staticmethod
    def _find_main_staging_slot(
        repo: Any,
        character_id: int,
        *,
        reserved_slots: set[int] | None = None,
    ) -> int | None:
        """Find a normal bag slot accepted by the source item for a cargo hop.

        Prefer an empty physical equipment cell.  When the bag is full, the
        native cargo-to-bag move can swap through an occupied equipment cell;
        the final cargo return restores that staging item to this same cell.
        """
        rows_by_slot = {
            int(row.get("slot_index", -1)): row
            for row in repo.list_inventory_items(int(character_id), list_type=0)
        }
        reserved = {int(slot) for slot in (reserved_slots or set())}
        # This helper only serves list2 body equipment, whose native Main bag
        # band is the fixed physical interval 9..64.
        slots = range(9, 65)
        for slot in slots:
            if slot not in rows_by_slot:
                return slot
        for slot in slots:
            if slot in reserved:
                continue
            row = rows_by_slot.get(slot)
            if row is None or str(row.get("item_kind", "") or "") != "equipment":
                continue
            if repo.get_inventory_item_at(
                int(character_id),
                list_type=repo.INVENTORY_LIST_MAIN,
                slot_index=slot,
            ) is not None:
                return slot
        return None

    def _queue_dungeon_native_loadout(
        self,
        *,
        repo: Any,
        character_id: int,
        loadout: Mapping[str, Any],
        session: Any,
    ) -> tuple[list[bytes], dict[str, Any], str]:
        """Publish one client-native move plan without mutating dungeon rows.

        A dungeon shortcut must not pre-commit a batch in SQLite and then try
        to make the client catch up.  The DLL invokes the same native move
        entry point as a manual drag; C1/0013 remains the only mutation path.
        """
        active = active_native_loadout_state(session)
        if active is not None:
            self._notify_loadout_in_progress()
            raise ControlApiError(
                "LOADOUT_IN_PROGRESS",
                "a dungeon loadout is still waiting for the client confirmation",
                status=409,
            )
        if native_loadout_cooldown_remaining(session) > 0.0:
            self._notify_loadout_cooldown()
            raise ControlApiError(
                "LOADOUT_COOLDOWN",
                "a completed dungeon loadout is still cooling down",
                status=409,
            )
        if not native_loadout_enabled():
            raise ControlApiError(
                "NATIVE_LOADOUT_DISABLED",
                "dungeon loadout requires USDOF_NATIVE_LOADOUT=1",
                status=409,
            )

        skipped_empty_slots: list[str] = []
        unchanged_slots: list[str] = []
        missing_slots: list[str] = []
        failed_slots: dict[str, str] = {}
        resolved: list[tuple[str, Mapping[str, Any], Mapping[str, Any], int, Any]] = []
        used_source_uids: set[int] = set()

        # Resolve every source against the current database snapshot first.
        # This lets cargo staging avoid any main-bag source another planned
        # action still needs later in the ordered sequence.
        for key in LOADOUT_APPLICATION_SLOT_KEYS:
            spec = LOADOUT_SLOT_SPECS[key]
            item = loadout["items"][key]
            target_item_id = _item_id(item.get("item_id"))
            wear_slot = int(spec["slot"])
            if target_item_id <= 0:
                skipped_empty_slots.append(key)
                continue

            hinted_uid, _hinted_list_type, _hinted_slot, has_hint = self._source_hint(
                item
            )
            worn = repo.get_equipped_at_slot(character_id, wear_slot)
            worn_item_id = _item_id(worn.get("item_id")) if worn is not None else 0
            if worn_item_id == target_item_id and not has_hint:
                unchanged_slots.append(key)
                continue

            source = self._find_source_item(repo, character_id, item, spec)
            if source is None:
                missing_slots.append(key)
                detail = "saved_source_missing"
                if hinted_uid > 0:
                    detail += " uid=%s" % hinted_uid
                failed_slots[key] = detail
                continue
            source_uid = _int_or_zero(source.get("item_uid"))
            if source_uid > 0 and source_uid in used_source_uids:
                failed_slots[key] = "duplicate_saved_source"
                continue
            if source_uid > 0:
                used_source_uids.add(source_uid)
            resolved.append((key, spec, source, target_item_id, worn))

        reserved_main_slots = {
            int(source.get("slot_index", -1))
            for _key, _spec, source, _item_id_value, _worn in resolved
            if int(source.get("list_type", -1)) == repo.INVENTORY_LIST_MAIN
        }
        actions: list[NativeLoadoutAction] = []
        planned: dict[str, int] = {}
        predicted_list_types: set[int] = set()
        predicted_equipment_slots: set[int] = set()

        for key, spec, source, target_item_id, worn in resolved:
            source_list_type = int(source.get("list_type", -1))
            source_slot = int(source.get("slot_index", -1))
            wear_slot = int(spec["slot"])
            if source_slot < 0:
                failed_slots[key] = "saved_source_slot_invalid"
                continue

            if source_list_type == repo.INVENTORY_LIST_PERSONAL_CARGO:
                staging_slot = self._find_main_staging_slot(
                    repo,
                    character_id,
                    reserved_slots=reserved_main_slots,
                )
                if staging_slot is None:
                    missing_slots.append(key)
                    failed_slots[key] = "main_bag_full_for_personal_cargo"
                    continue
                staging_before = repo.get_inventory_item_at(
                    character_id,
                    list_type=repo.INVENTORY_LIST_MAIN,
                    slot_index=staging_slot,
                )
                # Stage3 only accepts a cargo body-equipment source through a
                # Main bag cell.  Restore whichever item was displaced by that
                # hop before the next planned slot starts.
                actions.append(
                    NativeLoadoutAction(
                        source_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                        source_slot=source_slot,
                        destination_list_type=repo.INVENTORY_LIST_MAIN,
                        destination_slot=staging_slot,
                        item_id=target_item_id,
                    )
                )
                actions.append(
                    NativeLoadoutAction(
                        source_list_type=repo.INVENTORY_LIST_MAIN,
                        source_slot=staging_slot,
                        destination_list_type=3,
                        destination_slot=wear_slot,
                        item_id=target_item_id,
                    )
                )
                worn_item_id = _item_id(worn.get("item_id")) if worn is not None else 0
                if worn_item_id > 0:
                    actions.append(
                        NativeLoadoutAction(
                            source_list_type=repo.INVENTORY_LIST_MAIN,
                            source_slot=staging_slot,
                            destination_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                            destination_slot=source_slot,
                            item_id=worn_item_id,
                        )
                    )
                elif staging_before is not None:
                    staging_item_id = _item_id(staging_before.get("item_template_id"))
                    if staging_item_id <= 0:
                        failed_slots[key] = "staging_item_invalid"
                        del actions[-2:]
                        continue
                    actions.append(
                        NativeLoadoutAction(
                            source_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                            source_slot=source_slot,
                            destination_list_type=repo.INVENTORY_LIST_MAIN,
                            destination_slot=staging_slot,
                            item_id=staging_item_id,
                        )
                    )
                predicted_list_types.update(
                    {
                        repo.INVENTORY_LIST_MAIN,
                        repo.INVENTORY_LIST_PERSONAL_CARGO,
                    }
                )
            elif source_list_type in (
                repo.INVENTORY_LIST_MAIN,
                repo.INVENTORY_LIST_AVATAR,
                repo.INVENTORY_LIST_PET,
            ):
                actions.append(
                    NativeLoadoutAction(
                        source_list_type=source_list_type,
                        source_slot=source_slot,
                        destination_list_type=3,
                        destination_slot=wear_slot,
                        item_id=target_item_id,
                    )
                )
                predicted_list_types.add(source_list_type)
            else:
                failed_slots[key] = "unsupported_source_list=%s" % source_list_type
                continue

            planned[key] = target_item_id
            predicted_equipment_slots.add(wear_slot)

        packet_flow: list[str] = []
        plan_id = None
        if actions:
            try:
                plan = queue_native_loadout_plan(
                    session,
                    character_id=character_id,
                    actions=actions,
                )
            except (OSError, ValueError) as exc:
                raise ControlApiError(
                    "NATIVE_LOADOUT_PLAN_ERROR",
                    "could not publish the dungeon native-loadout plan",
                    status=500,
                ) from exc
            plan_id = int(plan.plan_id)
            self._notify_loadout_started(loadout)
            packet_flow.append(
                "client-native C1/0013 -> S1/0013 -> S0/000D/S0/000E per action; server-confirmed sidechannel releases the next action"
            )

        result = {
            "character_id": character_id,
            "loadout_index": int(loadout["index"]),
            "loadout_name": loadout["name"],
            # Nothing is committed until the client-originated C1/0013 path
            # returns successfully; `queued` describes the immediate result.
            "changed": False,
            "queued": bool(actions),
            "execution_mode": "native_dungeon" if actions else "native_dungeon_noop",
            "native_plan_id": plan_id,
            "pending_action_count": len(actions),
            "changed_list_types": sorted(predicted_list_types),
            "changed_equipment_slots": sorted(predicted_equipment_slots),
            "missing_slots": missing_slots,
            "skipped_empty_slots": skipped_empty_slots,
            "unchanged_slots": unchanged_slots,
            "failed_slots": failed_slots,
            "applied": {},
            "planned": planned,
            "packet_flow": packet_flow,
            "appearance_note": "USERINFO0_skip=native_dungeon_move_chain",
            "equipment_note": "native C1/0013 handler owns each S0/000E list3 refresh",
        }
        note = "loadout native_dungeon queued_actions=%s planned=%s missing=%s failed=%s empty=%s" % (
            len(actions),
            len(planned),
            len(missing_slots),
            len(failed_slots),
            len(skipped_empty_slots),
        )
        return [], result, "loadout apply character=%s index=%s %s" % (
            character_id,
            int(loadout["index"]),
            note,
        )

    def _move_one_item(
        self,
        *,
        repo: Any,
        character_id: int,
        job: int,
        level: int,
        equipment_level_reduction: int,
        source_list_type: int,
        source_slot: int,
        source_item_id: int,
        destination_list_type: int,
        destination_slot: int,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Commit one native inventory move without emitting client packets."""
        ok, note, move = repo.try_move_itemspace(
            int(character_id),
            source_list_type=int(source_list_type),
            source_slot=int(source_slot),
            source_instance_value=int(source_item_id),
            move_count=1,
            destination_list_type=int(destination_list_type),
            destination_slot=int(destination_slot),
            job=int(job),
            level=int(level),
            equipment_level_reduction=int(equipment_level_reduction),
        )
        return bool(ok), str(note), dict(move)

    def _append_batch_refresh_packets(
        self,
        *,
        repo: Any,
        character_id: int,
        record: Any,
        session: Any,
        changed_list_types: set[int],
        changed_equipment_slots: set[int],
        packets: list[bytes],
        packet_flow: list[str],
    ) -> tuple[str, str]:
        """Refresh each changed container once after all native moves commit.

        A GM shortcut has no client-originated C1/0013 request to pair with an
        S1/0013 acknowledgement. Sending an acknowledgement for every move
        makes the client consume only part of a large loadout burst. The
        established server-initiated path is one final container projection and
        one equipment-bar projection after the complete ordered mutation set.
        In town, a full appearance projection sits between those packets so the
        client rebuilds the local character model from the committed rows.
        """
        for list_type in (0, 1, 2, 7):
            if list_type not in changed_list_types:
                continue
            container_packets, container_note = self._container_packets(
                repo,
                int(character_id),
                list_type,
            )
            packets.extend(container_packets)
            packet_flow.append(container_note)

        appearance_note = "appearance=unchanged"
        if not changed_equipment_slots:
            return appearance_note, "equip_bar=unchanged"

        if bool(getattr(session, "in_dungeon_run", False)):
            appearance_note = "USERINFO0_skip=in_dungeon_run"
        else:
            try:
                appearance_helper = __import__(
                    "game.chain_loader",
                    fromlist=["load_chain"],
                ).load_chain("helper-userinfo_equipment.py")
                appearance_packets, appearance_note = (
                    appearance_helper.build_userinfo0_appearance_packets(
                        self.server,
                        character_id=int(character_id),
                        record=record,
                        repo=repo,
                        session=session,
                    )
                )
                packets.extend(appearance_packets)
                if appearance_packets:
                    packet_flow.append("USERINFO0/S0/0165/USERINFO1")
            except Exception as exc:
                appearance_note = "USERINFO0_fail:%s" % type(exc).__name__

        try:
            equip_helper = __import__(
                "game.chain_loader",
                fromlist=["load_chain"],
            ).load_chain("helper-equip_itemlist.py")
            equip_body, _count, equip_note = (
                equip_helper.build_equipment_bar_update_000e_body(
                    repo,
                    int(character_id),
                )
            )
            packets.append(self.server._codec_response(0, 0x000E, equip_body))
            packet_flow.append("S0/000E list3")
            return appearance_note, str(equip_note)
        except Exception as exc:
            return appearance_note, "equip_bar_fail:%s" % type(exc).__name__

    def apply_loadout(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or set(params) != {"index"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "loadout.apply requires only index",
                status=400,
            )
        index = _int_or_zero(params.get("index"))
        if not 0 <= index < LOADOUT_COUNT:
            raise ControlApiError("INVALID_PARAMS", "loadout index is invalid", status=400)
        character_id = self._character_id(session)
        repo = self.server._repo()
        records = _normalize_loadouts(repo.list_character_loadouts(character_id))
        loadout = records[index]
        record = self._record_for_character(repo, character_id)
        if record is None:
            raise ControlApiError(
                "SESSION_NOT_READY",
                "current character state is unavailable",
                status=409,
            )
        if bool(getattr(session, "in_dungeon_run", False)):
            return self._queue_dungeon_native_loadout(
                repo=repo,
                character_id=character_id,
                loadout=loadout,
                session=session,
            )
        account_id = repo.get_account_id_for_character(character_id)
        premium_effects = get_active_premium_effects(
            repo,
            int(account_id or 0),
            session=session,
        )
        equipment_level_reduction = max(
            0,
            int(getattr(premium_effects, "equipment_level_reduction", 0) or 0),
        )
        packets: list[bytes] = []
        packet_flow: list[str] = []
        changed_list_types: set[int] = set()
        changed_equipment_slots: set[int] = set()
        missing_slots: list[str] = []
        skipped_empty_slots: list[str] = []
        unchanged_slots: list[str] = []
        failed_slots: dict[str, str] = {}
        applied: dict[str, int] = {}
        operation_notes: list[str] = []

        # Intentionally process one slot at a time through the same repository
        # path as C1/0013. Client state is projected only after every ordered
        # mutation completes, because a GM shortcut has no matching C1/0013
        # context for a burst of individual acknowledgements.
        for key in LOADOUT_APPLICATION_SLOT_KEYS:
            spec = LOADOUT_SLOT_SPECS[key]
            item = loadout["items"][key]
            target_item_id = _item_id(item.get("item_id"))
            wear_slot = int(spec["slot"])
            if target_item_id <= 0:
                # A blank plan cell means "leave the character as-is".
                skipped_empty_slots.append(key)
                continue

            hinted_uid, _hinted_list_type, _hinted_slot, has_hint = self._source_hint(
                item
            )
            worn = repo.get_equipped_at_slot(character_id, wear_slot)
            worn_item_id = _item_id(worn.get("item_id")) if worn is not None else 0
            if worn_item_id == target_item_id and not has_hint:
                unchanged_slots.append(key)
                continue

            source = self._find_source_item(repo, character_id, item, spec)
            if source is None:
                missing_slots.append(key)
                detail = "saved_source_missing"
                if hinted_uid > 0:
                    detail += " uid=%s" % hinted_uid
                failed_slots[key] = detail
                continue

            source_list_type = int(source["list_type"])
            source_slot = int(source["slot_index"])
            original_cargo_slot = source_slot
            working_source_slot = source_slot
            if source_list_type == repo.INVENTORY_LIST_PERSONAL_CARGO:
                temporary_slot = self._find_main_staging_slot(
                    repo,
                    character_id,
                )
                if temporary_slot is None:
                    missing_slots.append(key)
                    failed_slots[key] = "main_bag_full_for_personal_cargo"
                    continue
                ok, note, move = self._move_one_item(
                    repo=repo,
                    character_id=character_id,
                    job=int(record.job),
                    level=int(record.level),
                    equipment_level_reduction=equipment_level_reduction,
                    source_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                    source_slot=source_slot,
                    source_item_id=target_item_id,
                    destination_list_type=repo.INVENTORY_LIST_MAIN,
                    destination_slot=temporary_slot,
                )
                operation_notes.append("%s:%s" % (key, note))
                if not ok:
                    failed_slots[key] = note
                    continue
                changed_list_types.update(
                    {
                        int(move["source_list_type"]),
                        int(move["destination_list_type"]),
                    }
                )
                source_list_type = repo.INVENTORY_LIST_MAIN
                working_source_slot = temporary_slot

            ok, note, move = self._move_one_item(
                repo=repo,
                character_id=character_id,
                job=int(record.job),
                level=int(record.level),
                equipment_level_reduction=equipment_level_reduction,
                source_list_type=source_list_type,
                source_slot=working_source_slot,
                source_item_id=target_item_id,
                destination_list_type=3,
                destination_slot=wear_slot,
            )
            operation_notes.append("%s:%s" % (key, note))
            if not ok:
                failed_slots[key] = note
                # A failed cargo-origin equip leaves the target in its
                # temporary bag slot; put it back before continuing.
                if int(source.get("list_type", -1)) == repo.INVENTORY_LIST_PERSONAL_CARGO:
                    rollback_item = repo.get_inventory_item_at(
                        character_id,
                        list_type=repo.INVENTORY_LIST_MAIN,
                        slot_index=working_source_slot,
                    )
                    if rollback_item is not None:
                        rollback_ok, rollback_note, rollback_move = self._move_one_item(
                            repo=repo,
                            character_id=character_id,
                            job=int(record.job),
                            level=int(record.level),
                            equipment_level_reduction=equipment_level_reduction,
                            source_list_type=repo.INVENTORY_LIST_MAIN,
                            source_slot=working_source_slot,
                            source_item_id=_item_id(
                                rollback_item.get("item_template_id")
                            ),
                            destination_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                            destination_slot=original_cargo_slot,
                        )
                        operation_notes.append("%s rollback:%s" % (key, rollback_note))
                        if rollback_ok:
                            changed_list_types.update(
                                {
                                    int(rollback_move["source_list_type"]),
                                    int(rollback_move["destination_list_type"]),
                                }
                            )
                continue

            changed_list_types.update(
                {
                    int(move["source_list_type"]),
                    int(move["destination_list_type"]),
                }
            )
            changed_equipment_slots.add(wear_slot)
            applied[key] = target_item_id

            # Main -> equipment swaps put the displaced item back into the
            # temporary slot.  Return it to the original personal-cargo cell
            # so repeated shortcut swaps retain their saved source identity.
            if int(source.get("list_type", -1)) == repo.INVENTORY_LIST_PERSONAL_CARGO:
                returned_item = repo.get_inventory_item_at(
                    character_id,
                    list_type=repo.INVENTORY_LIST_MAIN,
                    slot_index=working_source_slot,
                )
                if returned_item is not None:
                    return_ok, return_note, return_move = self._move_one_item(
                        repo=repo,
                        character_id=character_id,
                        job=int(record.job),
                        level=int(record.level),
                        equipment_level_reduction=equipment_level_reduction,
                        source_list_type=repo.INVENTORY_LIST_MAIN,
                        source_slot=working_source_slot,
                        source_item_id=_item_id(returned_item.get("item_template_id")),
                        destination_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                        destination_slot=original_cargo_slot,
                    )
                    operation_notes.append("%s return:%s" % (key, return_note))
                    if return_ok:
                        changed_list_types.update(
                            {
                                int(return_move["source_list_type"]),
                                int(return_move["destination_list_type"]),
                            }
                        )
                    else:
                        failed_slots[key] = "return_to_cargo:%s" % return_note
                else:
                    # The wear slot was empty.  If cargo-to-main used an
                    # occupied staging cell, restore that displaced main item
                    # from the cargo source instead of leaving it behind.
                    staged_item = repo.get_inventory_item_at(
                        character_id,
                        list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                        slot_index=original_cargo_slot,
                    )
                    if staged_item is not None:
                        restore_ok, restore_note, restore_move = self._move_one_item(
                            repo=repo,
                            character_id=character_id,
                            job=int(record.job),
                            level=int(record.level),
                            equipment_level_reduction=equipment_level_reduction,
                            source_list_type=repo.INVENTORY_LIST_PERSONAL_CARGO,
                            source_slot=original_cargo_slot,
                            source_item_id=_item_id(
                                staged_item.get("item_template_id")
                            ),
                            destination_list_type=repo.INVENTORY_LIST_MAIN,
                            destination_slot=working_source_slot,
                        )
                        operation_notes.append("%s restore:%s" % (key, restore_note))
                        if restore_ok:
                            changed_list_types.update(
                                {
                                    int(restore_move["source_list_type"]),
                                    int(restore_move["destination_list_type"]),
                                }
                            )
                        else:
                            failed_slots[key] = "restore_staging:%s" % restore_note

        appearance_note, equipment_note = self._append_batch_refresh_packets(
            repo=repo,
            character_id=character_id,
            record=record,
            session=session,
            changed_list_types=changed_list_types,
            changed_equipment_slots=changed_equipment_slots,
            packets=packets,
            packet_flow=packet_flow,
        )
        result = {
            "character_id": character_id,
            "loadout_index": index,
            "loadout_name": loadout["name"],
            "changed": bool(applied),
            "changed_list_types": sorted(
                list_type
                for list_type in changed_list_types
                if list_type in (0, 1, 2, 7)
            ),
            "changed_equipment_slots": sorted(changed_equipment_slots),
            "missing_slots": missing_slots,
            "skipped_empty_slots": skipped_empty_slots,
            "unchanged_slots": unchanged_slots,
            "failed_slots": failed_slots,
            "applied": applied,
            "packet_flow": packet_flow,
            "appearance_note": appearance_note,
            "equipment_note": equipment_note,
        }
        if applied:
            self._notify_loadout_started(loadout)
        note = "loadout_incremental applied=%s missing=%s failed=%s empty=%s" % (
            len(applied),
            len(missing_slots),
            len(failed_slots),
            len(skipped_empty_slots),
        )
        if operation_notes:
            note += " operations=" + ";".join(operation_notes)
        return packets, result, "loadout apply character=%s index=%s %s packets=%s" % (
            character_id,
            index,
            note,
            len(packets),
        )


__all__ = [
    "GmLoadoutControlCommands",
    "LOADOUT_APPLICATION_SLOT_KEYS",
    "LOADOUT_COUNT",
    "LOADOUT_SLOT_KEYS",
    "LOADOUT_SLOT_SPECS",
]
