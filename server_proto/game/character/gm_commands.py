"""Game-thread handlers for the GM item catalog and inventory management."""
from __future__ import annotations

import time
from typing import Any

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character.gm_item_catalog import (
    CATALOG_CATEGORIES,
    CATALOG_PAGE_SIZE_DEFAULT,
    CATALOG_PAGE_SIZE_MAX,
)
from game.character.gm_pvf_resources import GmPvfCacheFailed, GmPvfCacheNotReady
from game.character.gm_item_grant import (
    GM_GRANT_COUNT_MAX,
    GM_INVENTORY_SECTIONS,
    grant_gm_item,
    grant_gm_item_to_section,
)
from game.character.character_db import parse_virtual_premium_grant
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    SLOT_RANGE_GUILD_MEDAL,
    SLOT_RANGE_GUARDIAN_GEM,
    resolve_item_metadata,
)
from game.character.payloads import (
    build_account_cargo_item_list,
    build_avatar_item_list_mode0,
    build_creature_item_list,
    build_guild_medal_item_list_mode0,
    build_item_list_mode0,
    build_item_list_update_000e,
    build_pet_item_list_mode0,
)


_cube_soul = load_chain("helper-cube_soul_slots.py")
_SECTION_LABELS = {
    "all": "全部栏目",
    "quick": "快捷栏",
    "personal_cargo": "个人金库",
    "account_cargo": "账号金库",
    "equipment": "装备",
    "consumable": "消耗品",
    "material": "材料",
    "quest": "任务道具",
    "avatar": "装扮",
    "emblem": "徽章",
    "pet": "宠物",
    "pet_equipment": "宠物装备",
    "guild_medal": "公会勋章",
    "guardian_gem": "守护珠",
}
_SECTION_ORDER = {name: index for index, name in enumerate(GM_INVENTORY_SECTIONS)}


class GmItemControlCommands:
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
    def _attach_owned_counts(
        result: dict[str, Any],
        repo: Any,
        character_id: int,
    ) -> None:
        items = list(result.get("items") or [])
        item_ids = {int(item.get("item_id", 0) or 0) for item in items}
        counts = {item_id: 0 for item_id in item_ids if item_id > 0}
        if int(character_id) > 0 and counts:
            now_unix = int(time.time())
            for list_type in (0, 1, 2, 7, GUILD_MEDAL_INVENTORY_LIST_TYPE):
                rows = repo.list_inventory_items(
                    int(character_id),
                    list_type=list_type,
                    now_unix=now_unix,
                )
                for row in rows:
                    item_id = int(row.get("item_template_id", 0) or 0)
                    if item_id in counts:
                        counts[item_id] += max(
                            0,
                            int(row.get("stack_count", 0) or 0),
                        )
            account_id = repo.get_account_id_for_character(int(character_id))
            if account_id is not None:
                for row in repo.list_account_cargo_items(int(account_id)):
                    item_id = int(row.get("item_template_id", 0) or 0)
                    if item_id in counts:
                        counts[item_id] += max(
                            0,
                            int(row.get("stack_count", 0) or 0),
                        )
            for row in repo.list_equipped_items(
                int(character_id),
                now_unix=now_unix,
            ):
                item_id = int(row.get("item_id", 0) or 0)
                if item_id in counts:
                    counts[item_id] += 1
        for item in items:
            item_id = int(item.get("item_id", 0) or 0)
            item["owned_count"] = int(counts.get(item_id, 0))
        result["items"] = items

    def list_items(self, params: dict[str, Any], session: Any):
        allowed = {"search", "category", "page", "page_size"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ControlApiError("INVALID_PARAMS", "invalid pvf.list_items parameters", status=400)
        try:
            catalog = self.server.gm_pvf_resources.get_item_catalog()
            result = catalog.query(
                search=str(params.get("search") or ""),
                category=str(params.get("category") or "all"),
                page=int(params.get("page", 1)),
                page_size=int(params.get("page_size", CATALOG_PAGE_SIZE_DEFAULT)),
            )
        except GmPvfCacheNotReady as exc:
            raise ControlApiError(
                "GM_PVF_CACHE_PENDING",
                str(exc),
                status=503,
            )
        except GmPvfCacheFailed as exc:
            raise ControlApiError(
                "GM_PVF_CACHE_FAILED",
                str(exc),
                status=503,
            )
        except (TypeError, ValueError) as exc:
            raise ControlApiError("INVALID_PARAMS", str(exc), status=400)
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        self._attach_owned_counts(result, self.server._repo(), character_id)
        return [], result, "GM PVF item catalog page=%d total=%d" % (
            result["page"],
            result["total"],
        )

    def grant_item(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or not set(params).issubset({"item_id", "count"}):
            raise ControlApiError("INVALID_PARAMS", "invalid inventory.grant_item parameters", status=400)
        if "item_id" not in params:
            raise ControlApiError("INVALID_PARAMS", "item_id is required", status=400)
        try:
            item_id = int(params["item_id"])
            count = int(params.get("count", 1))
        except (TypeError, ValueError):
            raise ControlApiError("INVALID_PARAMS", "item_id and count must be integers", status=400)
        if item_id <= 0 or not 1 <= count <= GM_GRANT_COUNT_MAX:
            raise ControlApiError(
                "INVALID_PARAMS",
                "item_id must be positive and count must be between 1 and %d"
                % GM_GRANT_COUNT_MAX,
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        ok, note, result = grant_gm_item(
            repo,
            character_id=character_id,
            item_id=item_id,
            count=count,
        )
        if not ok:
            code = "INVENTORY_FULL" if note == "container_full" else "ITEM_GRANT_FAILED"
            raise ControlApiError(code, note, status=409)

        premium_notice = parse_virtual_premium_grant(note)
        if premium_notice is not None:
            helper = load_chain("helper-system_reward_activation.py")
            packet = self.server._codec_response(
                0,
                0x0042,
                helper.build_premium_activation_notice(*premium_notice),
            )
            result["packet_flow"] = ["S0/0042"]
            result["changed_slots"] = []
            return [packet], result, "GM item grant %s" % note

        list_type = int(result["list_type"])
        changed_slots = {int(value) for value in result["changed_slots"]}
        now_unix = int(time.time())
        rows = repo.list_inventory_items(character_id, list_type=list_type, now_unix=now_unix)
        all_items = [InventoryItemView(row) for row in rows]
        changed_items = [item for item in all_items if int(item.slot_index) in changed_slots]
        update = build_item_list_update_000e(
            changed_items,
            list_type=list_type,
            avatar=(list_type == 1),
            now_unix=now_unix,
        )
        list_param = (
            0
            if list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE
            else int(repo.get_container_list_param(character_id, list_type=list_type))
        )
        if list_type == 1:
            full = build_avatar_item_list_mode0(
                all_items,
                list_param16=list_param,
                now_unix=now_unix,
            )
        elif list_type == 7:
            full = build_pet_item_list_mode0(all_items)
        elif list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE:
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
                list_param16=list_param,
            )
        packets = [
            self.server._codec_response(0, 0x000E, update),
            self.server._codec_response(0, 0x000D, full),
        ]
        result["packet_flow"] = ["S0/000E", "S0/000D"]
        return packets, result, "GM item grant %s" % note

    @staticmethod
    def _section_for_slot(list_type: int, slot_index: int) -> str | None:
        list_type = int(list_type)
        slot = int(slot_index)
        if list_type == 1:
            return "avatar"
        if list_type == 2:
            return "personal_cargo"
        if list_type == 12:
            return "account_cargo"
        if list_type == 7:
            if 140 <= slot <= 188:
                return "pet_equipment"
            if 0 <= slot <= 139 or 189 <= slot <= 237:
                return "pet"
            return None
        if list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE:
            if SLOT_RANGE_GUILD_MEDAL[0] <= slot <= SLOT_RANGE_GUILD_MEDAL[1]:
                return "guild_medal"
            if SLOT_RANGE_GUARDIAN_GEM[0] <= slot <= SLOT_RANGE_GUARDIAN_GEM[1]:
                return "guardian_gem"
            return None
        if list_type != 0:
            return None
        if 3 <= slot <= 8:
            return "quick"
        if 9 <= slot <= 64:
            return "equipment"
        if 65 <= slot <= 120:
            return "consumable"
        if 121 <= slot <= 176 or 233 <= slot <= 288 or 354 <= slot <= 366:
            return "material"
        if 177 <= slot <= 232:
            return "quest"
        if 289 <= slot <= 353:
            return "emblem"
        return None

    def _inventory_rows(self, repo: Any, character_id: int) -> list[dict[str, Any]]:
        now_unix = int(time.time())
        account_id = repo.get_account_id_for_character(character_id)
        source_rows: list[tuple[int, dict[str, Any]]] = []
        for list_type in (0, 1, 2, 7, GUILD_MEDAL_INVENTORY_LIST_TYPE):
            source_rows.extend(
                (list_type, row)
                for row in repo.list_inventory_items(
                    character_id,
                    list_type=list_type,
                    now_unix=now_unix,
                )
            )
        if account_id is not None:
            source_rows.extend(
                (12, row)
                for row in repo.list_account_cargo_items(int(account_id))
            )

        items: list[dict[str, Any]] = []
        for list_type, row in source_rows:
            slot = int(row.get("slot_index", -1))
            section = self._section_for_slot(list_type, slot)
            item_id = int(row.get("item_template_id", 0) or 0)
            if section is None or item_id <= 0:
                continue
            metadata = resolve_item_metadata(item_id)
            count = max(0, int(row.get("stack_count", 0) or 0))
            if count <= 0 and str(row.get("item_kind") or "") == "equipment":
                count = 1
            items.append(
                {
                    "key": "%d:%d" % (int(list_type), slot),
                    "list_type": int(list_type),
                    "slot_index": slot,
                    "item_id": item_id,
                    "name": str(metadata.name or ""),
                    "count": count,
                    "item_kind": str(row.get("item_kind") or metadata.db_item_kind()),
                    "section": section,
                    "section_label": _SECTION_LABELS[section],
                    "rarity": max(0, int(metadata.rarity or 0)),
                    "expire_time": max(0, int(row.get("expire_time", 0) or 0)),
                }
            )
        items.sort(
            key=lambda item: (
                _SECTION_ORDER.get(str(item["section"]), 999),
                int(item["slot_index"]),
                int(item["item_id"]),
            )
        )
        return items

    def list_inventory(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or not set(params).issubset({"section"}):
            raise ControlApiError(
                "INVALID_PARAMS", "invalid inventory.list parameters", status=400
            )
        section = str(params.get("section") or "all").strip().lower()
        if section not in GM_INVENTORY_SECTIONS:
            raise ControlApiError("INVALID_PARAMS", "unknown inventory section", status=400)
        character_id = self._character_id(session)
        items = self._inventory_rows(self.server._repo(), character_id)
        counts = {
            name: sum(1 for item in items if item["section"] == name)
            for name in GM_INVENTORY_SECTIONS
            if name != "all"
        }
        counts["all"] = len(items)
        visible = items if section == "all" else [
            item for item in items if item["section"] == section
        ]
        result = {
            "character_id": character_id,
            "section": section,
            "section_label": _SECTION_LABELS[section],
            "items": visible,
            "total": len(visible),
            "section_counts": counts,
            "sections": [
                {
                    "name": name,
                    "label": _SECTION_LABELS[name],
                    "count": int(counts[name]),
                }
                for name in GM_INVENTORY_SECTIONS
            ],
        }
        return [], result, "GM inventory list section=%s total=%d" % (
            section,
            len(visible),
        )

    def _full_container_packets(
        self,
        repo: Any,
        character_id: int,
        list_type: int,
        *,
        selected_character_slot: int = 0,
    ) -> tuple[list[bytes], list[str]]:
        list_type = int(list_type)
        now_unix = int(time.time())
        if list_type == 12:
            account_id = repo.get_account_id_for_character(character_id)
            if account_id is None:
                raise ControlApiError("ITEM_REFRESH_FAILED", "account_missing", status=409)
            state = repo.get_account_cargo_state(int(account_id), ensure_open=True)
            rows = repo.list_account_cargo_items(int(account_id))
            body = build_account_cargo_item_list(
                [InventoryItemView(row) for row in rows],
                selection_key=int(state.get("selection_key", 0) or 0),
                cargo_gold=int(state.get("value32", 0) or 0),
            )
            return [self.server._codec_response(0, 0x000D, body)], ["S0/000D list12"]

        rows = repo.list_inventory_items(
            character_id,
            list_type=list_type,
            now_unix=now_unix,
        )
        items = [InventoryItemView(row) for row in rows]
        list_param = (
            0
            if list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE
            else int(repo.get_container_list_param(character_id, list_type=list_type))
        )
        if list_type == 1:
            body = build_avatar_item_list_mode0(
                items,
                list_param16=list_param,
                now_unix=now_unix,
            )
        elif list_type == 7:
            body = build_pet_item_list_mode0(items)
        elif list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE:
            body = build_guild_medal_item_list_mode0(
                items,
                selected_character_slot=selected_character_slot,
            )
        else:
            body = build_item_list_mode0(
                items,
                list_type=list_type,
                list_param16=list_param,
            )
        packets = [self.server._codec_response(0, 0x000D, body)]
        flow = [
            "S0/000D list36/wire39"
            if list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE
            else "S0/000D list%d" % list_type
        ]
        if list_type == 7:
            creature_body = build_creature_item_list(
                repo.list_creature_states(character_id)
            )
            packets.append(self.server._codec_response(0, 0x0069, creature_body))
            flow.append("S0/0069")
        return packets, flow

    def add_inventory_item(self, params: dict[str, Any], session: Any):
        allowed = {"item_id", "count", "section"}
        if not isinstance(params, dict) or set(params) - allowed or not allowed.issubset(params):
            raise ControlApiError(
                "INVALID_PARAMS", "invalid inventory.add_item parameters", status=400
            )
        try:
            item_id = int(params["item_id"])
            count = int(params["count"])
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS", "item_id and count must be integers", status=400
            )
        section = str(params.get("section") or "").strip().lower()
        if item_id <= 0 or not 1 <= count <= GM_GRANT_COUNT_MAX:
            raise ControlApiError(
                "INVALID_PARAMS", "item_id or count is out of range", status=400
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        ok, note, result = grant_gm_item_to_section(
            repo,
            character_id=character_id,
            item_id=item_id,
            count=count,
            section=section,
        )
        if not ok:
            code = "INVENTORY_FULL" if note == "container_full" else "ITEM_ADD_FAILED"
            raise ControlApiError(code, note, status=409)
        premium_notice = parse_virtual_premium_grant(note)
        if premium_notice is not None:
            helper = load_chain("helper-system_reward_activation.py")
            packet = self.server._codec_response(
                0,
                0x0042,
                helper.build_premium_activation_notice(*premium_notice),
            )
            result["packet_flow"] = ["S0/0042"]
            result["changed_slots"] = []
            return [packet], result, "GM inventory add %s" % note
        packets, packet_flow = self._full_container_packets(
            repo,
            character_id,
            int(result["list_type"]),
            selected_character_slot=int(
                getattr(session, "selected_slot", 0) or 0
            ),
        )
        result["packet_flow"] = packet_flow
        return packets, result, "GM inventory add %s" % note

    def delete_inventory_item(self, params: dict[str, Any], session: Any):
        required = {"item_id", "list_type", "slot_index", "section"}
        if (not isinstance(params, dict) or not required.issubset(params)
                or set(params) - required - {"count"}):
            raise ControlApiError(
                "INVALID_PARAMS", "invalid inventory.delete_item parameters", status=400
            )
        try:
            item_id = int(params["item_id"])
            list_type = int(params["list_type"])
            slot_index = int(params["slot_index"])
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS", "inventory row fields must be integers", status=400
            )
        if "count" in params and (type(params["count"]) is not int
                or not 1 <= params["count"] <= 0xFFFFFFFF):
            raise ControlApiError(
                "INVALID_PARAMS", "count must be a positive uint32 integer", status=400
            )
        section = str(params.get("section") or "").strip().lower()
        if (
            item_id <= 0
            or section not in GM_INVENTORY_SECTIONS
            or section == "all"
            or self._section_for_slot(list_type, slot_index) != section
        ):
            raise ControlApiError("INVALID_PARAMS", "inventory row mismatch", status=400)

        character_id = self._character_id(session)
        repo = self.server._repo()
        selected = next(
            (
                row
                for row in self._inventory_rows(repo, character_id)
                if int(row["list_type"]) == list_type
                and int(row["slot_index"]) == slot_index
                and int(row["item_id"]) == item_id
                and row["section"] == section
            ),
            None,
        )
        if selected is None:
            raise ControlApiError("ITEM_DELETE_FAILED", "inventory_row_missing", status=409)
        available_count = max(1, int(selected.get("count", 0) or 0))
        remove_count = params.get("count", available_count)
        if remove_count > available_count:
            raise ControlApiError("ITEM_DELETE_FAILED", "inventory_count_changed", status=409)
        if selected.get("item_kind") == "equipment" and remove_count != available_count:
            raise ControlApiError("INVALID_PARAMS", "equipment cannot be partially removed", status=400)
        account_id = repo.get_account_id_for_character(character_id)
        if list_type == 12:
            if account_id is None:
                ok, note, detail = False, "account_missing", {}
            else:
                ok, note, detail = repo.consume_account_cargo_item(
                    int(account_id),
                    slot_index=slot_index,
                    expected_item_id=item_id,
                    count=remove_count,
                )
        elif list_type == 0 and _cube_soul.is_cube_fragment(item_id):
            if account_id is None:
                ok, note, detail = False, "account_missing", {}
            else:
                ok, note, detail = repo.consume_cube_fragment(
                    int(account_id), item_id, remove_count
                )
        elif list_type == 0 and _cube_soul.is_account_soul(item_id):
            if account_id is None:
                ok, note, detail = False, "account_missing", {}
            else:
                ok, note, _slot, remaining = repo.consume_account_soul(
                    int(account_id), item_id, remove_count
                )
                detail = {
                    "item_template_id": item_id,
                    "removed": remove_count if ok else 0,
                    "remaining": remaining,
                    "list_type": list_type,
                    "slot_index": slot_index,
                }
        else:
            ok, note, detail = repo.consume_inventory_item(
                character_id,
                list_type=list_type,
                slot_index=slot_index,
                count=remove_count,
            )
        if not ok:
            raise ControlApiError("ITEM_DELETE_FAILED", note, status=409)
        packets, packet_flow = self._full_container_packets(
            repo,
            character_id,
            list_type,
            selected_character_slot=int(
                getattr(session, "selected_slot", 0) or 0
            ),
        )
        result = dict(detail)
        result.update(
            {
                "character_id": character_id,
                "item_id": item_id,
                "section": section,
                "packet_flow": packet_flow,
            }
        )
        return packets, result, "GM inventory delete %s" % note

    def clear_inventory_scope(self, params: dict[str, Any], session: Any):
        """Clear after the trusted local GM has saved a verified off-server snapshot."""
        if not isinstance(params, dict) or set(params) != {"scope", "local_backup"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "inventory.clear_scope requires a verified local backup reference",
                status=400,
            )
        scope = str(params.get("scope") or "").strip().lower()
        allowed = {
            "inventory",
            "quickbar",
            "equipped",
            "pet",
            "worn_pet",
            "medal",
            "personal_cargo",
            "account_cargo",
            "all",
        }
        if scope not in allowed:
            raise ControlApiError("INVALID_PARAMS", "unknown clear scope", status=400)
        backup = params["local_backup"]
        if (not isinstance(backup, dict) or set(backup) != {"id", "sha256"}
                or not isinstance(backup["id"], str) or not 1 <= len(backup["id"]) <= 128
                or not all(c.isascii() and (c.isalnum() or c == '-') for c in backup["id"])
                or not isinstance(backup["sha256"], str) or len(backup["sha256"]) != 64
                or any(c not in '0123456789abcdef' for c in backup["sha256"])):
            raise ControlApiError("INVALID_PARAMS", "invalid local backup reference", status=400)
        character_id = self._character_id(session)
        repo = self.server._repo()
        had_override = '_create_clear_snapshot' in repo.__dict__
        previous_override = repo.__dict__.get('_create_clear_snapshot')
        try:
            # GM control handlers execute serially on the selected Game context.
            # Preserve the native clear SQL/packet behavior while replacing only
            # its disk snapshot callback for the duration of this operation.
            repo._create_clear_snapshot = lambda *_args: 'local-backup:' + backup['id']
            result = repo.clear_gm_inventory_scope(character_id, scope)
            result.update({'backup_location':'local','local_backup':dict(backup)})
        except (TypeError, ValueError) as exc:
            raise ControlApiError("INVALID_PARAMS", str(exc), status=400) from exc
        except (OSError, RuntimeError) as exc:
            raise ControlApiError("SAVE_BACKUP_FAILED", str(exc), status=500) from exc
        finally:
            if had_override: repo._create_clear_snapshot = previous_override
            else: del repo._create_clear_snapshot

        packets: list[bytes] = []
        packet_flow: list[str] = []
        packet_refresh_errors: list[str] = []
        for list_type in result.get("changed_list_types") or ():
            try:
                container_packets, flow = self._full_container_packets(
                    repo,
                    character_id,
                    int(list_type),
                    selected_character_slot=int(
                        getattr(session, "selected_slot", 0) or 0
                    ),
                )
                packets.extend(container_packets)
                packet_flow.extend(flow)
            except Exception as exc:
                # The destructive transaction has already committed. Keep the
                # backup path/result visible even if a malformed remaining row
                # prevents an immediate client refresh.
                packet_refresh_errors.append(
                    "list%d:%s" % (int(list_type), type(exc).__name__)
                )

        if bool(result.get("changed_equipment")):
            record = None
            account_id = repo.get_account_id_for_character(character_id)
            if account_id is not None:
                record = next(
                    (
                        item
                        for item in repo.list_by_account(int(account_id))
                        if int(getattr(item, "character_id", 0) or 0) == character_id
                    ),
                    None,
                )
            if not bool(getattr(session, "in_dungeon_run", False)):
                try:
                    appearance = load_chain("helper-userinfo_equipment.py")
                    appearance_packets, appearance_note = appearance.build_userinfo0_appearance_packets(
                        self.server,
                        character_id=character_id,
                        record=record,
                        repo=repo,
                        session=session,
                    )
                    packets.extend(appearance_packets)
                    if appearance_packets:
                        packet_flow.append("USERINFO0/S0/0165/USERINFO1")
                    result["appearance_note"] = appearance_note
                except Exception as exc:
                    result["appearance_note"] = "USERINFO0_fail:%s" % type(exc).__name__
            try:
                equip_helper = load_chain("helper-equip_itemlist.py")
                equip_body, _count, equip_note = equip_helper.build_equipment_bar_update_000e_body(
                    repo,
                    character_id,
                )
                packets.append(self.server._codec_response(0, 0x000E, equip_body))
                packet_flow.append("S0/000E list3")
                result["equipment_note"] = equip_note
            except Exception as exc:
                result["equipment_note"] = "equip_bar_fail:%s" % type(exc).__name__

        result["packet_flow"] = packet_flow
        if packet_refresh_errors:
            result["packet_refresh_errors"] = packet_refresh_errors
        return packets, result, (
            "GM inventory clear scope=%s deleted=%s backup=%s"
            % (scope, result.get("deleted_count", 0), result.get("backup_path", ""))
        )


__all__ = [
    "CATALOG_CATEGORIES",
    "CATALOG_PAGE_SIZE_MAX",
    "GmItemControlCommands",
]
