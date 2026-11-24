"""Game-thread control commands for persisted quest and item blocking."""
from __future__ import annotations

from typing import Any, Iterable

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character.item_metadata import resolve_item_metadata
from game.content_blocking import activate_character_content_blocks
from game.quest.quest_index import get_quest_index
from game.quest.quest_session import (
    hydrate_quest_lists,
    pack_quest_refresh_packets,
    resolve_selected_character_detail,
)


_equip_bar = load_chain("helper-equip_itemlist.py")
_CONTENT_TYPES = ("quest", "item")
_MAX_BATCH_SIZE = 256


class ContentBlockControlCommands:
    """Keep content-rule validation and UI-facing projections on the Game thread."""

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
    def _id_list(
        params: dict[str, Any],
        name: str,
        *,
        maximum: int,
    ) -> list[int]:
        raw = params.get(name, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ControlApiError(
                "INVALID_PARAMS",
                "%s must be an array" % name,
                status=400,
            )
        if len(raw) > _MAX_BATCH_SIZE:
            raise ControlApiError(
                "INVALID_PARAMS",
                "%s must contain at most %d IDs" % (name, _MAX_BATCH_SIZE),
                status=400,
            )
        values: list[int] = []
        seen: set[int] = set()
        for value in raw:
            if isinstance(value, bool):
                raise ControlApiError(
                    "INVALID_PARAMS",
                    "%s must contain integer IDs" % name,
                    status=400,
                )
            try:
                content_id = int(value)
            except (TypeError, ValueError):
                raise ControlApiError(
                    "INVALID_PARAMS",
                    "%s must contain integer IDs" % name,
                    status=400,
                )
            if content_id <= 0 or content_id > int(maximum):
                raise ControlApiError(
                    "INVALID_PARAMS",
                    "%s contains an out-of-range ID" % name,
                    status=400,
                )
            if content_id not in seen:
                seen.add(content_id)
                values.append(content_id)
        return values

    def _activate(self, repo: Any, character_id: int, session: Any) -> tuple[frozenset[int], frozenset[int]]:
        return activate_character_content_blocks(repo, character_id, session=session)

    @staticmethod
    def _quest_row(quest_id: int) -> dict[str, Any]:
        index = get_quest_index()
        meta = index.get_meta(int(quest_id))
        return {
            "quest_id": int(quest_id),
            "name": str(getattr(meta, "name", "") or ""),
            "grade": str(getattr(meta, "grade", "") or ""),
            "quest_type": str(getattr(meta, "quest_type", "") or ""),
            "minimum_level": max(0, int(getattr(meta, "min_level", 0) or 0)),
            "source_path": str(getattr(meta, "path", "") or ""),
        }

    @staticmethod
    def _item_row(item_id: int) -> dict[str, Any]:
        metadata = resolve_item_metadata(int(item_id))
        return {
            "item_id": int(item_id),
            "name": str(getattr(metadata, "name", "") or ""),
            "item_kind": str(getattr(metadata, "item_kind", "") or ""),
            "rarity": max(0, int(getattr(metadata, "rarity", 0) or 0)),
            "level": max(0, int(getattr(metadata, "minimum_level", 0) or 0)),
            "source_path": str(getattr(metadata, "source_path", "") or ""),
        }

    def _validate_quest_ids(self, quest_ids: Iterable[int]) -> list[dict[str, Any]]:
        index = get_quest_index()
        known_paths = getattr(index, "quest_paths", {})
        rows = []
        for quest_id in quest_ids:
            row = self._quest_row(int(quest_id))
            if not row["name"] and int(quest_id) not in known_paths:
                raise ControlApiError(
                    "CONTENT_NOT_FOUND",
                    "quest ID %d is not present in the current PVF" % int(quest_id),
                    status=404,
                )
            rows.append(row)
        return rows

    def _validate_item_ids(self, item_ids: Iterable[int]) -> list[dict[str, Any]]:
        rows = []
        for item_id in item_ids:
            row = self._item_row(int(item_id))
            if not row["name"]:
                raise ControlApiError(
                    "CONTENT_NOT_FOUND",
                    "item ID %d is not present in the current PVF" % int(item_id),
                    status=404,
                )
            rows.append(row)
        return rows

    @staticmethod
    def _scope_payload_from_scope(
        scope: tuple[int, list[int]] | None,
        *,
        fallback_account_id: int = 0,
    ) -> dict[str, Any]:
        account_id = int(scope[0]) if scope is not None else int(fallback_account_id)
        character_ids = [int(value) for value in (scope[1] if scope is not None else ())]
        return {
            "scope": "account",
            "account_id": account_id,
            "account_character_ids": character_ids,
        }

    @classmethod
    def _scope_payload(cls, repo: Any, character_id: int) -> dict[str, Any]:
        scope_loader = getattr(repo, "get_character_content_block_scope", None)
        scope = scope_loader(int(character_id)) if callable(scope_loader) else None
        return cls._scope_payload_from_scope(scope)

    @classmethod
    def _account_scope_payload(cls, repo: Any, account_id: int) -> dict[str, Any]:
        scope_loader = getattr(repo, "get_account_content_block_scope", None)
        scope = scope_loader(int(account_id)) if callable(scope_loader) else None
        return cls._scope_payload_from_scope(
            scope,
            fallback_account_id=int(account_id),
        )

    def _current_account_id(self) -> int:
        account_id = int(getattr(self.server, "account_id", 0) or 0)
        if account_id <= 0:
            raise ControlApiError(
                "SESSION_NOT_READY",
                "current client account is not ready",
                status=409,
            )
        return account_id

    def _rules_payload(self, repo: Any, character_id: int, session: Any) -> dict[str, Any]:
        quest_ids, item_ids = self._activate(repo, character_id, session)
        return {
            **self._scope_payload(repo, character_id),
            "character_id": int(character_id),
            "blocked_quests": [self._quest_row(value) for value in sorted(quest_ids)],
            "blocked_items": [self._item_row(value) for value in sorted(item_ids)],
        }

    def _account_rules_payload(self, repo: Any, account_id: int) -> dict[str, Any]:
        rule_loader = getattr(repo, "list_account_content_block_ids", None)
        if callable(rule_loader):
            quest_ids, item_ids = rule_loader(int(account_id))
        else:
            quest_ids, item_ids = [], []
        return {
            **self._account_scope_payload(repo, account_id),
            "character_id": None,
            "blocked_quests": [self._quest_row(value) for value in sorted(quest_ids)],
            "blocked_items": [self._item_row(value) for value in sorted(item_ids)],
        }

    def list_rules(self, params: dict[str, Any], session: Any):
        if params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "content_block.list does not accept parameters",
                status=400,
            )
        repo = self.server._repo()
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id > 0:
            result = self._rules_payload(repo, character_id, session)
        else:
            result = self._account_rules_payload(repo, self._current_account_id())
        result["quest_total"] = len(result["blocked_quests"])
        result["item_total"] = len(result["blocked_items"])
        return [], result, "content blocks listed"

    def resolve_ids(self, params: dict[str, Any], session: Any):
        allowed = {"quest_ids", "item_ids"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ControlApiError(
                "INVALID_PARAMS",
                "invalid content_block.resolve parameters",
                status=400,
            )
        self._character_id(session)
        quest_ids = self._id_list(params, "quest_ids", maximum=0xFFFF)
        item_ids = self._id_list(params, "item_ids", maximum=0xFFFFFFFF)
        if not quest_ids and not item_ids:
            raise ControlApiError(
                "INVALID_PARAMS",
                "at least one quest or item ID is required",
                status=400,
            )
        return [], {
            "quests": self._validate_quest_ids(quest_ids),
            "items": self._validate_item_ids(item_ids),
        }, "content block IDs resolved"

    def list_candidates(self, params: dict[str, Any], session: Any):
        if params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "content_block.candidates does not accept parameters",
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        detail = resolve_selected_character_detail(self.server, session)
        hydrated = hydrate_quest_lists(
            self.server,
            character_id=character_id,
            job=int(detail.get("job") or 0),
            level=max(1, int(detail.get("level") or 1)),
            grow_type=int(detail.get("grow_type") or 0),
            session=session,
            ensure_unlock=False,
        )
        visible_quests = [
            self._quest_row(quest_id)
            for quest_id in hydrated.get("acceptable_ids", ())
        ]
        accepted_quests = [
            {
                **self._quest_row(quest_id),
                "slot": int(slot),
                "trigger": int(trigger),
            }
            for slot, quest_id, trigger in repo.list_active_quests(character_id)
        ]
        owned_items = list(
            self.server.gm_item_commands._inventory_rows(repo, character_id)
        )
        return [], {
            **self._scope_payload(repo, character_id),
            "character_id": character_id,
            "visible_quests": visible_quests,
            "accepted_quests": accepted_quests,
            "owned_items": owned_items,
        }, "content block candidates visible=%d accepted=%d owned=%d" % (
            len(visible_quests),
            len(accepted_quests),
            len(owned_items),
        )

    def apply_rules(self, params: dict[str, Any], session: Any):
        return self._change_rules(params, session, is_blocked=True)

    def remove_rules(self, params: dict[str, Any], session: Any):
        return self._change_rules(params, session, is_blocked=False)

    def _change_rules(self, params: dict[str, Any], session: Any, *, is_blocked: bool):
        allowed = {"quest_ids", "item_ids"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ControlApiError(
                "INVALID_PARAMS",
                "invalid content block parameters",
                status=400,
            )
        quest_ids = self._id_list(params, "quest_ids", maximum=0xFFFF)
        item_ids = self._id_list(params, "item_ids", maximum=0xFFFFFFFF)
        if not quest_ids and not item_ids:
            raise ControlApiError(
                "INVALID_PARAMS",
                "at least one quest or item ID is required",
                status=400,
            )
        quest_rows = self._validate_quest_ids(quest_ids)
        item_rows = self._validate_item_ids(item_ids)
        character_id = self._character_id(session)
        repo = self.server._repo()
        ok, note, detail = repo.update_character_content_block_rules(
            character_id,
            quest_ids=quest_ids,
            item_ids=item_ids,
            is_blocked=is_blocked,
        )
        if not ok:
            raise ControlApiError("CONTENT_BLOCK_UPDATE_FAILED", note, status=409)
        self._activate(repo, character_id, session)

        packets: list[bytes] = []
        packet_flow: list[str] = []
        if quest_ids:
            char_detail = resolve_selected_character_detail(self.server, session)
            quest_packets, quest_note = pack_quest_refresh_packets(
                self.server,
                character_id=character_id,
                job=int(char_detail.get("job") or 0),
                level=max(1, int(char_detail.get("level") or 1)),
                grow_type=int(char_detail.get("grow_type") or 0),
                session=session,
                include_0015=True,
                include_023f=True,
                include_0164=False,
            )
            packets.extend(quest_packets)
            packet_flow.append(quest_note)

        for list_type in detail.get("affected_list_types", ()):
            list_type = int(list_type)
            if list_type == 3:
                body, _count, equip_note = _equip_bar.build_equipment_bar_update_000e_body(
                    repo,
                    character_id,
                )
                packets.append(self.server._codec_response(0, 0x000E, body))
                packet_flow.append(equip_note)
                continue
            container_packets, container_flow = self.server.gm_item_commands._full_container_packets(
                repo,
                character_id,
                list_type,
            )
            packets.extend(container_packets)
            packet_flow.extend(container_flow)

        detail.update(
            {
                "blocked": bool(is_blocked),
                "quests": quest_rows,
                "items": item_rows,
                "packet_flow": packet_flow,
                "rules": self._rules_payload(repo, character_id, session),
            }
        )
        action = "applied" if is_blocked else "removed"
        return packets, detail, "content blocks %s %s" % (action, note)


__all__ = ["ContentBlockControlCommands"]
