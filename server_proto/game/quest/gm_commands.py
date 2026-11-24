"""Game-thread handlers for GM quest listing and lifecycle operations."""
from __future__ import annotations

import re
import struct
from typing import Any

from control.contracts import ControlApiError
from game.content_blocking import activate_character_content_blocks
from game.quest.quest_common import build_accept_quest_ack, is_client_hidden_quest_id
from game.quest.daily_challenge_index import get_daily_challenge_candidate_ids
from game.quest.quest_index import (
    _normalize_tag,
    compute_quest_init_trigger,
    get_quest_index,
    quest_clear_required_quest_ids,
)
from game.quest.quest_rewards import resolve_accept_event_items
from game.quest.quest_session import (
    pack_completeable_trigger_acks,
    pack_quest_refresh_packets,
    resolve_selected_character_detail,
)


QUEST_SEARCH_MAX_LENGTH = 128
QUEST_PAGE_SIZE_DEFAULT = 20
QUEST_PAGE_SIZE_MAX = 20
QUEST_LIST_STATUSES = frozenset({"accepted", "finished", "unfinished", "all"})


def _compile_like_pattern(value: str) -> re.Pattern[str]:
    parts = (re.escape(part) for part in value.split("%"))
    return re.compile(r"\A" + ".*".join(parts) + r"\Z", re.DOTALL)


def _quest_matches_search(quest: dict[str, Any], search: str) -> bool:
    if not search:
        return True
    if re.fullmatch(r"[0-9]+", search) is not None:
        search_id = int(search)
        return search_id in {
            int(quest.get("quest_id", 0) or 0),
            int(quest.get("titlebook_quest_id", 0) or 0),
        }
    name = str(quest.get("name") or "").casefold()
    needle = search.casefold()
    if "%" in needle:
        return _compile_like_pattern(needle).fullmatch(name) is not None
    return needle in name


def _resolve_titlebook_achievement_quest_id(
    index: Any,
    quest_id: int,
    indexed_ids: set[int] | frozenset[int],
) -> int | None:
    """Return the actionable quest behind one titlebook clear-quest proxy.

    Ordinary quests resolve to themselves. A title clear-quest row is only
    actionable when it references exactly one indexed achievement quest;
    malformed or stale title rows resolve to ``None`` and stay out of GM
    lifecycle operations.
    """
    quest_id = int(quest_id) & 0xFFFF
    meta = index.get_meta(quest_id)
    if meta is None:
        return None
    if not (
        _normalize_tag(str(getattr(meta, "grade", "") or "")) == "title"
        and _normalize_tag(str(getattr(meta, "quest_type", "") or ""))
        in {"clear quest", "quest clear"}
    ):
        return quest_id

    required_ids = quest_clear_required_quest_ids(meta)
    if len(required_ids) != 1:
        return None
    achievement_id = int(required_ids[0]) & 0xFFFF
    if achievement_id not in indexed_ids:
        return None
    achievement_meta = index.get_meta(achievement_id)
    if achievement_meta is None or _normalize_tag(
        str(getattr(achievement_meta, "grade", "") or "")
    ) != "achievement":
        return None
    return achievement_id


def _build_titlebook_achievement_aliases(
    index: Any,
    indexed_ids: list[int] | set[int] | frozenset[int],
) -> tuple[dict[int, int], frozenset[int]]:
    """Build proxy aliases and isolate invalid title clear-quest rows."""
    registered_ids = frozenset(int(value) & 0xFFFF for value in indexed_ids)
    proxy_to_achievement: dict[int, int] = {}
    isolated_ids: set[int] = set()
    for quest_id in sorted(registered_ids):
        resolved_id = _resolve_titlebook_achievement_quest_id(
            index,
            quest_id,
            registered_ids,
        )
        if resolved_id is None:
            meta = index.get_meta(quest_id)
            if meta is not None and (
                _normalize_tag(str(getattr(meta, "grade", "") or "")) == "title"
                and _normalize_tag(str(getattr(meta, "quest_type", "") or ""))
                in {"clear quest", "quest clear"}
            ):
                isolated_ids.add(quest_id)
            continue
        if resolved_id != quest_id:
            proxy_to_achievement[quest_id] = resolved_id
    return proxy_to_achievement, frozenset(isolated_ids)


def _titlebook_proxy_by_achievement(
    proxy_to_achievement: dict[int, int],
) -> dict[int, int]:
    out: dict[int, int] = {}
    for proxy_id, achievement_id in sorted(proxy_to_achievement.items()):
        out.setdefault(int(achievement_id), int(proxy_id))
    return out


class GmQuestControlCommands:
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
    def _activate(repo: Any, character_id: int, session: Any) -> None:
        activate_character_content_blocks(repo, int(character_id), session=session)

    @staticmethod
    def _quest_result(
        quest_id: int,
        *,
        status: str,
        slot: int | None,
        trigger: int | None,
        finished_flag: int = 0,
        index: Any | None = None,
        titlebook_quest_id: int | None = None,
    ) -> dict[str, Any]:
        quest_index = index if index is not None else get_quest_index()
        meta = quest_index.get_meta(int(quest_id))
        return {
            "slot": None if slot is None else int(slot),
            "quest_id": int(quest_id),
            "titlebook_quest_id": (
                None if titlebook_quest_id is None else int(titlebook_quest_id)
            ),
            "trigger": None if trigger is None else int(trigger),
            "completeable": status == "accepted" and int(trigger or 0) == 0,
            "status": str(status),
            "finished_flag": int(finished_flag) & 0xFF,
            "name": str(getattr(meta, "name", "") or ""),
            "grade": str(getattr(meta, "grade", "") or ""),
            "quest_type": str(getattr(meta, "quest_type", "") or ""),
            "minimum_level": max(0, int(getattr(meta, "min_level", 0) or 0)),
            "source_path": str(getattr(meta, "path", "") or ""),
        }

    @staticmethod
    def _indexed_quest_ids(index: Any) -> list[int]:
        """Return the shared quest index IDs after the boot/lazy warmup gate."""
        ensure_all_metas_loaded = getattr(index, "ensure_all_metas_loaded", None)
        if callable(ensure_all_metas_loaded):
            ensure_all_metas_loaded()
        indexed_ids = getattr(index, "sorted_quest_ids", ())
        if not indexed_ids:
            indexed_ids = getattr(index, "quest_paths", {})
            indexed_ids = getattr(indexed_ids, "keys", lambda: ())()
        if not indexed_ids:
            indexed_ids = getattr(index, "metas", {})
            indexed_ids = getattr(indexed_ids, "keys", lambda: ())()
        daily_challenge_ids = get_daily_challenge_candidate_ids()
        return sorted(
            {
                int(quest_id) & 0xFFFF
                for quest_id in indexed_ids
                if (
                    0 < int(quest_id) <= 0xFFFF
                    and not is_client_hidden_quest_id(quest_id)
                    and (int(quest_id) & 0xFFFF) not in daily_challenge_ids
                )
            }
        )

    @classmethod
    def _ordinary_catalog(
        cls,
        index: Any,
    ) -> tuple[frozenset[int], frozenset[int], dict[int, int], frozenset[int]]:
        """Build the only quest set that GM ordinary-task operations may use.

        ``_indexed_quest_ids`` already applies the current character's
        content-block mask and reserves the PVF Daily Challenge candidates.
        TitleBook clear-quest rows are presentation aliases rather than
        independently actionable tasks, so keep their mapping separately and
        expose only the referenced achievement row in the ordinary catalog.
        """
        indexed_ids = frozenset(cls._indexed_quest_ids(index))
        proxy_to_achievement, isolated_title_ids = (
            _build_titlebook_achievement_aliases(index, indexed_ids)
        )
        hidden_title_ids = frozenset(proxy_to_achievement).union(isolated_title_ids)
        catalog_ids = frozenset(set(indexed_ids) - set(hidden_title_ids))
        return indexed_ids, catalog_ids, proxy_to_achievement, isolated_title_ids

    @staticmethod
    def _has_registration_snapshot(index: Any) -> bool:
        return any(
            hasattr(index, name) for name in ("quest_paths", "sorted_quest_ids", "metas")
        )

    @classmethod
    def _catalog_allows(
        cls,
        index: Any,
        catalog_ids: frozenset[int],
        quest_id: int,
    ) -> bool:
        """Keep lightweight protocol fixtures usable without weakening real indexes.

        A production ``QuestIndex`` always exposes at least ``quest_paths``.
        Tiny unit-test doubles from older callers may expose only ``get_meta``;
        those have no authoritative registration snapshot, so their persisted
        active/finished rows remain inspectable while unfinished rows still
        have no candidates to enumerate.
        """
        if not cls._has_registration_snapshot(index):
            return True
        return int(quest_id) in catalog_ids

    def list_accepted(self, params: dict[str, Any], session: Any):
        if params:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.list_accepted does not accept parameters",
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        index = get_quest_index()
        (
            _indexed_ids,
            catalog_ids,
            proxy_to_achievement,
            _isolated_title_ids,
        ) = self._ordinary_catalog(index)
        titlebook_by_achievement = _titlebook_proxy_by_achievement(
            proxy_to_achievement
        )
        quests = []
        for slot, quest_id, trigger in repo.list_active_quests(character_id):
            if not self._catalog_allows(index, catalog_ids, quest_id):
                continue
            meta = index.get_meta(quest_id)
            quests.append(
                {
                    "slot": int(slot),
                    "quest_id": int(quest_id),
                    "titlebook_quest_id": titlebook_by_achievement.get(
                        int(quest_id)
                    ),
                    "trigger": int(trigger),
                    "completeable": int(trigger) == 0,
                    "name": str(getattr(meta, "name", "") or ""),
                    "grade": str(getattr(meta, "grade", "") or ""),
                    "quest_type": str(getattr(meta, "quest_type", "") or ""),
                    "minimum_level": max(
                        0, int(getattr(meta, "min_level", 0) or 0)
                    ),
                    "source_path": str(getattr(meta, "path", "") or ""),
                }
            )
        return [], {
            "quests": quests,
            "total": len(quests),
        }, "GM accepted quests count=%d" % len(quests)

    def list_quests(self, params: dict[str, Any], session: Any):
        allowed = {"search", "status", "page", "page_size"}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ControlApiError(
                "INVALID_PARAMS",
                "invalid quest.list parameters",
                status=400,
            )
        search = str(params.get("search") or "").strip()
        if len(search) > QUEST_SEARCH_MAX_LENGTH:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.list search must be at most %d characters"
                % QUEST_SEARCH_MAX_LENGTH,
                status=400,
            )
        list_status = str(params.get("status") or "all").strip().lower()
        if list_status not in QUEST_LIST_STATUSES:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.list status must be accepted, finished, unfinished, or all",
                status=400,
            )
        pagination_requested = "page" in params or "page_size" in params
        try:
            page = int(params.get("page", 1))
            page_size = int(params.get("page_size", QUEST_PAGE_SIZE_DEFAULT))
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.list page and page_size must be integers",
                status=400,
            )
        if page < 1:
            raise ControlApiError(
                "INVALID_PARAMS", "quest.list page must be at least 1", status=400
            )
        if not 1 <= page_size <= QUEST_PAGE_SIZE_MAX:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.list page_size must be between 1 and %d"
                % QUEST_PAGE_SIZE_MAX,
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        index = get_quest_index()
        (
            indexed_ids,
            catalog_ids,
            proxy_to_achievement,
            _isolated_title_ids,
        ) = self._ordinary_catalog(index)
        titlebook_by_achievement = _titlebook_proxy_by_achievement(
            proxy_to_achievement
        )
        active_rows = list(repo.list_active_quests(character_id))
        active_ids = {int(quest_id) for _slot, quest_id, _trigger in active_rows}
        finished_rows = list(repo.list_finished_quests(character_id))
        accepted = [
            self._quest_result(
                int(quest_id),
                status="accepted",
                slot=int(slot),
                trigger=int(trigger),
                index=index,
                titlebook_quest_id=titlebook_by_achievement.get(int(quest_id)),
            )
            for slot, quest_id, trigger in active_rows
            if self._catalog_allows(index, catalog_ids, quest_id)
        ]
        accepted = [quest for quest in accepted if _quest_matches_search(quest, search)]
        finished = [
            self._quest_result(
                int(quest_id),
                status="finished",
                slot=None,
                trigger=None,
                finished_flag=int(flag),
                index=index,
                titlebook_quest_id=titlebook_by_achievement.get(int(quest_id)),
            )
            for quest_id, flag in finished_rows
            if int(quest_id) not in active_ids
            and self._catalog_allows(index, catalog_ids, quest_id)
        ]
        finished = [quest for quest in finished if _quest_matches_search(quest, search)]
        finished.sort(key=lambda quest: int(quest["quest_id"]))

        # The shared quest index is already fully warmed during server boot.  Build
        # the third partition from every indexed quest that is neither active nor
        # finished, so the GM list also exposes tasks with no character DB row yet.
        finished_ids = {int(quest_id) for quest_id, _flag in finished_rows}
        unfinished = [
            self._quest_result(
                int(quest_id),
                status="unfinished",
                slot=None,
                trigger=None,
                index=index,
                titlebook_quest_id=titlebook_by_achievement.get(int(quest_id)),
            )
            for quest_id in sorted(catalog_ids)
            if int(quest_id) > 0
            and int(quest_id) not in active_ids
            and int(quest_id) not in finished_ids
        ]
        unfinished = [
            quest for quest in unfinished if _quest_matches_search(quest, search)
        ]
        unfinished.sort(key=lambda quest: int(quest["quest_id"]))
        partitions = {
            "accepted": accepted,
            "finished": finished,
            "unfinished": unfinished,
        }
        if list_status == "all":
            matched_quests = accepted + finished + unfinished
        else:
            matched_quests = partitions[list_status]
        total = len(matched_quests)
        result = {
            "quests": matched_quests,
            "total": total,
            "accepted_total": len(accepted),
            "finished_total": len(finished),
            "unfinished_total": len(unfinished),
            "search": search,
            "status": list_status,
        }
        if pagination_requested:
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(page, page_count)
            offset = (page - 1) * page_size
            result.update(
                {
                    "quests": matched_quests[offset : offset + page_size],
                    "page": page,
                    "page_size": page_size,
                    "page_count": page_count,
                }
            )
        return [], result, (
            "GM quest list accepted=%d finished=%d unfinished=%d "
            "status=%s page=%s page_size=%s search=%r"
        ) % (
            len(accepted),
            len(finished),
            len(unfinished),
            list_status,
            page if pagination_requested else "all",
            page_size if pagination_requested else "all",
            search,
        )

    def accept_unfinished(self, params: dict[str, Any], session: Any):
        """Accept one row from the GM list's unfinished partition."""
        if not isinstance(params, dict) or set(params) != {"quest_id"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.accept_unfinished requires only quest_id",
                status=400,
            )
        try:
            requested_quest_id = int(params["quest_id"])
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS", "quest_id must be an integer", status=400
            )
        if not 1 <= requested_quest_id <= 0xFFFF:
            raise ControlApiError(
                "INVALID_PARAMS", "quest_id is out of range", status=400
            )

        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        active_rows = list(repo.list_active_quests(character_id))
        active_ids = {int(value) for _slot, value, _trigger in active_rows}
        finished_rows = list(repo.list_finished_quests(character_id))
        finished_ids = {int(value) for value, _flag in finished_rows}
        index = get_quest_index()
        (
            indexed_ids,
            catalog_ids,
            proxy_to_achievement,
            isolated_title_ids,
        ) = self._ordinary_catalog(index)
        quest_id = proxy_to_achievement.get(requested_quest_id, requested_quest_id)
        if (
            requested_quest_id not in indexed_ids
            or requested_quest_id in isolated_title_ids
            or not self._catalog_allows(index, catalog_ids, quest_id)
            or quest_id in active_ids
            or quest_id in finished_ids
        ):
            raise ControlApiError(
                "QUEST_NOT_UNFINISHED",
                "quest is not in the current unfinished list",
                status=409,
            )

        body = struct.pack("<HH", 0x001F, quest_id)
        packets, note = self.server.accept_quest_001f.handle_accept_quest_001f(
            body,
            session=session,
            force_accept=True,
        )
        active_row = next(
            (
                (int(slot), int(trigger))
                for slot, active_id, trigger in repo.list_active_quests(character_id)
                if int(active_id) == quest_id
            ),
            None,
        )
        if active_row is None:
            error_code = (
                "QUEST_SLOT_FULL"
                if "no_free_slot" in str(note)
                else "QUEST_ACCEPT_FAILED"
            )
            raise ControlApiError(error_code, note, status=409)

        meta = index.get_meta(quest_id)
        titlebook_quest_id = _titlebook_proxy_by_achievement(
            proxy_to_achievement
        ).get(quest_id)
        return packets, {
            "quest_id": quest_id,
            "titlebook_quest_id": titlebook_quest_id,
            "name": str(getattr(meta, "name", "") or ""),
            "slot": active_row[0],
            "trigger": active_row[1],
            "accepted": True,
        }, "GM quest accept unfinished %s" % note

    def complete_accepted(self, params: dict[str, Any], session: Any):
        allowed = {"quest_id", "reward_selection_index"}
        if not isinstance(params, dict) or not set(params).issubset(allowed):
            raise ControlApiError(
                "INVALID_PARAMS",
                "invalid quest.complete_accepted parameters",
                status=400,
            )
        if "quest_id" not in params:
            raise ControlApiError("INVALID_PARAMS", "quest_id is required", status=400)
        try:
            quest_id = int(params["quest_id"])
            selection = int(params.get("reward_selection_index", 0))
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest_id and reward_selection_index must be integers",
                status=400,
            )
        if not 1 <= quest_id <= 0xFFFF or not 0 <= selection < 0xFFFF:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest_id or reward_selection_index is out of range",
                status=400,
            )
        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        active_ids = {
            int(active_quest_id)
            for _slot, active_quest_id, _trigger in repo.list_active_quests(character_id)
        }
        if quest_id not in active_ids:
            raise ControlApiError(
                "QUEST_NOT_ACCEPTED",
                "quest is not in the current accepted list",
                status=409,
            )
        index = get_quest_index()
        _indexed_ids, catalog_ids, _aliases, _isolated_ids = self._ordinary_catalog(
            index
        )
        if not self._catalog_allows(index, catalog_ids, quest_id):
            raise ControlApiError(
                "QUEST_BLOCKED",
                "quest is not available in the current GM task catalog",
                status=409,
            )
        body = struct.pack("<HHH", quest_id, selection, 1)
        packets, note = self.server.finish_quest_0022.handle_finish_quest_0022(
            body,
            session=session,
            force_complete=True,
        )
        if not repo.is_quest_finished(character_id, quest_id):
            raise ControlApiError(
                "QUEST_COMPLETE_FAILED",
                "quest completion did not persist",
                status=409,
            )
        meta = index.get_meta(quest_id)
        remaining = len(repo.list_active_quests(character_id))
        return packets, {
            "quest_id": quest_id,
            "name": str(getattr(meta, "name", "") or ""),
            "reward_selection_index": selection,
            "finished": True,
            "accepted_remaining": remaining,
        }, "GM quest complete %s" % note

    def giveup_accepted(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or set(params) != {"quest_id"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.giveup_accepted requires only quest_id",
                status=400,
            )
        try:
            quest_id = int(params["quest_id"])
        except (TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS", "quest_id must be an integer", status=400
            )
        if not 1 <= quest_id <= 0xFFFF:
            raise ControlApiError(
                "INVALID_PARAMS", "quest_id is out of range", status=400
            )

        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        active_ids = {
            int(active_quest_id)
            for _slot, active_quest_id, _trigger in repo.list_active_quests(character_id)
        }
        if quest_id not in active_ids:
            raise ControlApiError(
                "QUEST_NOT_ACCEPTED",
                "quest is not in the current accepted list",
                status=409,
            )
        index = get_quest_index()
        _indexed_ids, catalog_ids, _aliases, _isolated_ids = self._ordinary_catalog(
            index
        )
        if not self._catalog_allows(index, catalog_ids, quest_id):
            raise ControlApiError(
                "QUEST_BLOCKED",
                "quest is not available in the current GM task catalog",
                status=409,
            )

        body = struct.pack("<HH", 0x0020, quest_id)
        packets, note = self.server.giveup_quest_0020.handle_giveup_quest_0020(
            body,
            session=session,
        )
        remaining_rows = list(repo.list_active_quests(character_id))
        if any(int(active_id) == quest_id for _slot, active_id, _trigger in remaining_rows):
            raise ControlApiError(
                "QUEST_GIVEUP_FAILED",
                "quest giveup did not persist",
                status=409,
            )
        meta = index.get_meta(quest_id)
        return packets, {
            "quest_id": quest_id,
            "name": str(getattr(meta, "name", "") or ""),
            "abandoned": True,
            "accepted_remaining": len(remaining_rows),
        }, "GM quest giveup %s" % note

    def reaccept_finished(self, params: dict[str, Any], session: Any):
        if not isinstance(params, dict) or set(params) != {"quest_id"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "quest.reaccept requires only quest_id",
                status=400,
            )
        try:
            requested_quest_id = int(params["quest_id"])
        except (TypeError, ValueError):
            raise ControlApiError("INVALID_PARAMS", "quest_id must be an integer", status=400)
        if not 1 <= requested_quest_id <= 0xFFFF:
            raise ControlApiError("INVALID_PARAMS", "quest_id is out of range", status=400)

        character_id = self._character_id(session)
        repo = self.server._repo()
        self._activate(repo, character_id, session)
        index = get_quest_index()
        (
            indexed_ids,
            catalog_ids,
            proxy_to_achievement,
            isolated_title_ids,
        ) = self._ordinary_catalog(index)
        quest_id = proxy_to_achievement.get(requested_quest_id, requested_quest_id)
        finished_rows = list(repo.list_finished_quests(character_id))
        if not any(int(value) == quest_id for value, _flag in finished_rows):
            raise ControlApiError(
                "QUEST_NOT_FINISHED",
                "quest is not in the current finished list",
                status=409,
            )
        if (
            (
                self._has_registration_snapshot(index)
                and requested_quest_id not in indexed_ids
            )
            or requested_quest_id in isolated_title_ids
            or not self._catalog_allows(index, catalog_ids, quest_id)
        ):
            raise ControlApiError(
                "QUEST_BLOCKED",
                "quest is not available in the current GM task catalog",
                status=409,
            )
        detail = resolve_selected_character_detail(self.server, session)
        finished_ids = [
            int(value)
            for value, _flag in finished_rows
            if int(value) != quest_id
        ]
        try:
            init_trigger = int(
                compute_quest_init_trigger(
                    quest_id,
                    finished_quest_ids=finished_ids,
                )
            ) & 0xFFFFFFFF
        except Exception:
            init_trigger = 0

        ok, note = repo.reaccept_finished_quest(
            character_id,
            quest_id,
            trigger_value=init_trigger,
        )
        if not ok:
            error_code = {
                "not_finished": "QUEST_NOT_FINISHED",
                "no_free_slot": "QUEST_SLOT_FULL",
            }.get(str(note).split(" ", 1)[0], "QUEST_REACCEPT_FAILED")
            raise ControlApiError(error_code, note, status=409)

        event_items = resolve_accept_event_items(
            quest_id,
            player_job=int(detail.get("job") if detail.get("job") is not None else -1),
            player_grow_type=int(
                detail.get("grow_type") if detail.get("grow_type") is not None else -1
            ),
        )
        granted = []
        mailed = []
        if event_items:
            granted, mailed, _item_note = repo.grant_quest_items(
                character_id,
                [(item.item_id, item.count) for item in event_items],
                quest_id=quest_id,
                reason="gm_quest_reaccept_depend_give",
            )

        ack_items = tuple((slot, item_id, count) for slot, item_id, count in granted)
        packets = [
            self.server._codec_response(
                1,
                0x001F,
                build_accept_quest_ack(
                    success=True,
                    quest_id=quest_id,
                    init_trigger=init_trigger,
                    event_items=ack_items,
                ),
            )
        ]
        refresh, refresh_note = pack_quest_refresh_packets(
            self.server,
            character_id=character_id,
            job=int(detail.get("job") or 0),
            level=int(detail.get("level") or 1),
            grow_type=int(detail.get("grow_type") or 0),
            session=session,
            include_0015=True,
            include_023f=True,
            include_0164=True,
            allow_empty_0164=True,
        )
        packets.extend(refresh)
        if init_trigger == 0:
            arm_packets, arm_note = pack_completeable_trigger_acks(
                self.server,
                [(quest_id, 0)],
            )
            packets.extend(arm_packets)
            refresh_note = "%s;%s" % (refresh_note, arm_note)

        meta = index.get_meta(quest_id)
        active_row = next(
            (
                (slot, trigger)
                for slot, active_id, trigger in repo.list_active_quests(character_id)
                if int(active_id) == quest_id
            ),
            (None, init_trigger),
        )
        return packets, {
            "quest_id": quest_id,
            "titlebook_quest_id": _titlebook_proxy_by_achievement(
                proxy_to_achievement
            ).get(quest_id),
            "name": str(getattr(meta, "name", "") or ""),
            "slot": active_row[0],
            "trigger": int(active_row[1]),
            "accepted": True,
            "granted_item_count": len(granted),
            "mailed_item_count": len(mailed),
        }, "GM quest reaccept %s; %s" % (note, refresh_note)


__all__ = ["GmQuestControlCommands"]
