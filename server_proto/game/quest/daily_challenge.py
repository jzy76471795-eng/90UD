"""Stage3 Daily Challenge protocol bodies and domain service."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.character_db import parse_virtual_premium_grants
from game.character.item_metadata import resolve_item_metadata
from game.character.payloads import (
    build_item_list_mode0,
    build_item_list_update_000e,
)
from game.quest.daily_challenge_index import (
    STAGE3_CLAIM_FLAG_COUNT,
    DailyChallengeIndex,
    DailyChallengeReward,
    get_daily_challenge_index,
)
from game.quest.daily_challenge_repository import (
    DailyChallengeRepository,
    DailyChallengeState,
)
from game.quest.quest_common import (
    build_finish_quest_ack,
    build_finish_reward_insert_entries,
    build_set_quest_trigger_ack,
)
from game.quest.quest_index import _normalize_tag, daily_quest_day_key
from game.quest.quest_residual import apply_set_trigger_delta


_LOGGER = logging.getLogger(__name__)
_CLAIMED_PROJECTION_WARNINGS: set[str] = set()


def _premium_activation_packets(
    server: Any,
    *,
    note: str,
    session: Any = None,
) -> list[bytes]:
    premium_rows = parse_virtual_premium_grants(note)
    if not premium_rows:
        return []
    helper = load_chain("helper-system_reward_activation.py")
    if session is not None:
        try:
            session.account_premium_effects = None
        except Exception:
            pass
    return [
        server._codec_response(
            0,
            0x0042,
            helper.build_premium_activation_notice(premium_type, remaining),
        )
        for premium_type, remaining in premium_rows
    ]


@dataclass(frozen=True)
class DailyChallengeGrantResult:
    granted: tuple[tuple[int, int, int], ...]
    mailed: tuple[tuple[int, int], ...]
    note: str


def build_daily_challenge_body_0287(
    state: DailyChallengeState,
    *,
    include_claimed: bool = True,
) -> bytes:
    """Build the frozen Stage3 S0/0287 initialization body.

    ``include_claimed=False`` is used when rebuilding the client-side Daily
    subscriber vector.  Group headers and claim flags remain present, while a
    claimed entry is omitted so Stage3 cannot subscribe it again.
    """
    if len(state.claim_flags) != STAGE3_CLAIM_FLAG_COUNT:
        raise ValueError(
            "Stage3 Daily Challenge requires %d claim flags"
            % STAGE3_CLAIM_FLAG_COUNT
        )
    body = bytearray()
    body += (int(state.generated_level) & 0xFFFFFFFF).to_bytes(4, "little")
    body += (len(state.groups) & 0xFFFFFFFF).to_bytes(4, "little")
    for group in state.groups:
        entries = tuple(
            entry
            for entry in group.entries
            if include_claimed or not entry.reward_claimed
        )
        body += (int(group.group_index) & 0xFFFFFFFF).to_bytes(4, "little")
        body += (len(entries) & 0xFFFFFFFF).to_bytes(4, "little")
        for entry in entries:
            body += (int(entry.quest_id) & 0xFFFFFFFF).to_bytes(4, "little")
            body += (int(entry.target_value) & 0xFFFFFFFF).to_bytes(4, "little")
            body += (int(entry.current_value) & 0xFFFFFFFF).to_bytes(4, "little")
    body += STAGE3_CLAIM_FLAG_COUNT.to_bytes(4, "little")
    body.extend(1 if int(flag) else 0 for flag in state.claim_flags)
    return bytes(body)


def build_daily_challenge_group_reward_ack(
    *,
    success: bool,
    group_index: int = 0,
    error_code: int = 0x16,
) -> bytes:
    if not success:
        return bytes((0, int(error_code) & 0xFF))
    return (
        b"\x01"
        + (int(group_index) & 0xFFFFFFFF).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
    )


def ensure_daily_challenge_state(
    character_repo: Any,
    *,
    character_id: int,
    level: int,
    index: DailyChallengeIndex | None = None,
    day_key: str | None = None,
) -> DailyChallengeState:
    catalog = index or get_daily_challenge_index()
    catalog.ensure_loaded()
    return DailyChallengeRepository(character_repo).ensure_state(
        catalog,
        character_id=int(character_id),
        day_key=str(day_key or daily_quest_day_key()),
        level=max(1, int(level)),
    )


def build_daily_challenge_init_0287(
    character_repo: Any,
    *,
    character_id: int,
    level: int,
) -> tuple[bytes, DailyChallengeState, str]:
    state = ensure_daily_challenge_state(
        character_repo,
        character_id=character_id,
        level=level,
    )
    body = build_daily_challenge_body_0287(state, include_claimed=False)
    entry_count = sum(
        1
        for group in state.groups
        for entry in group.entries
        if not entry.reward_claimed
    )
    claimed_omitted = sum(
        1
        for group in state.groups
        for entry in group.entries
        if entry.reward_claimed
    )
    note = (
        "S0/0287 daily_challenge_init day=%s level=%d groups=%d entries=%d "
        "claims=%s active_entries=%d claimed_omitted=%d pvf=%s plain_len=%d"
        % (
            state.day_key,
            state.generated_level,
            len(state.groups),
            entry_count,
            "".join(str(int(flag)) for flag in state.claim_flags),
            entry_count,
            claimed_omitted,
            state.pvf_sha256[:16],
            len(body),
        )
    )
    return body, state, note


def list_today_claimed_daily_challenge_ids(
    character_repo: Any,
    character_id: int,
) -> list[int]:
    if not callable(getattr(character_repo, "open", None)):
        return []
    try:
        index = get_daily_challenge_index()
        index.ensure_loaded()
        return DailyChallengeRepository(character_repo).list_claimed_entry_ids(
            character_id=int(character_id),
            day_key=daily_quest_day_key(),
            pvf_sha256=index.pvf_sha256,
        )
    except Exception as exc:
        # Lightweight repositories used by quest-only tools have no daily ledger.
        # Keep that compatibility fallback, but make a real PVF/DB failure
        # visible.  De-duplicate the warning so a bad projection cannot itself
        # create a log flood during repeated quest refreshes.
        warning_key = "%s:%s" % (type(exc).__name__, str(exc))
        if warning_key not in _CLAIMED_PROJECTION_WARNINGS:
            _CLAIMED_PROJECTION_WARNINGS.add(warning_key)
            _LOGGER.warning(
                "daily challenge claimed projection unavailable: %s",
                warning_key,
            )
        return []


def list_today_daily_challenge_ids_by_type(
    character_repo: Any,
    character_id: int,
    *,
    quest_type: str,
) -> list[int]:
    """Return today's ledger entries of one warmed PVF type.

    This is a read-only projection over the already-created daily ledger.  It
    deliberately does not create a new daily state from a quest hot path.
    """
    if not callable(getattr(character_repo, "open", None)):
        return []
    wanted = str(quest_type or "").strip().lower()
    if not wanted:
        return []
    try:
        index = get_daily_challenge_index()
        index.ensure_loaded()
        state = DailyChallengeRepository(character_repo).load_matching_state(
            character_id=int(character_id),
            day_key=daily_quest_day_key(),
            pvf_sha256=index.pvf_sha256,
        )
        if state is None:
            return []
        matching: list[int] = []
        for group in state.groups:
            for entry in group.entries:
                definition = index.quest(entry.quest_id)
                if definition is None or definition.quest_type != wanted:
                    continue
                matching.append(int(entry.quest_id) & 0xFFFF)
        return matching
    except Exception:
        # Lightweight quest tools and incomplete fixture repositories do not
        # participate in the runtime Daily Challenge projection.
        return []


def arm_daily_challenge_clear_grade_event(
    session: Any,
    *,
    character_repo: Any,
    character_id: int,
    source_quest_id: int,
    source_grade: str,
) -> str:
    """Bind one finished quest to the client's following clear-grade reports."""
    if session is None:
        return "daily_clear_grade_event=skip_no_session"
    challenge_ids = tuple(
        list_today_daily_challenge_ids_by_type(
            character_repo,
            int(character_id),
            quest_type="clear quest by grade",
        )
    )
    grade = _normalize_tag(str(source_grade or ""))
    if not challenge_ids:
        session.daily_challenge_clear_grade_event = None
        return "daily_clear_grade_event=none source=%d grade=%s" % (
            int(source_quest_id) & 0xFFFF,
            grade or "unknown",
        )
    claimed_ids = {
        int(value) & 0xFFFF
        for value in list_today_claimed_daily_challenge_ids(
            character_repo,
            int(character_id),
        )
    }
    claimed_challenge_ids = tuple(
        value for value in challenge_ids if int(value) in claimed_ids
    )
    session.daily_challenge_clear_grade_event = {
        "source_quest_id": int(source_quest_id) & 0xFFFF,
        "source_grade": grade,
        "challenge_ids": challenge_ids,
        "claimed_challenge_ids": claimed_challenge_ids,
        "handled_ids": set(),
    }
    return (
        "daily_clear_grade_event=armed source=%d grade=%s challenges=%s claimed=%s"
        % (
            int(source_quest_id) & 0xFFFF,
            grade or "unknown",
            ",".join(str(value) for value in challenge_ids),
            ",".join(str(value) for value in claimed_challenge_ids) or "none",
        )
    )


def _consume_daily_challenge_clear_grade_event(
    session: Any,
    *,
    quest_id: int,
) -> tuple[str, int, str]:
    qid = int(quest_id) & 0xFFFF
    event = (
        getattr(session, "daily_challenge_clear_grade_event", None)
        if session is not None
        else None
    )
    if not isinstance(event, dict):
        return "uncorrelated", 0, ""
    challenge_ids = {
        int(value) & 0xFFFF for value in (event.get("challenge_ids") or ())
    }
    if qid not in challenge_ids:
        return "uncorrelated", int(event.get("source_quest_id") or 0), str(
            event.get("source_grade") or ""
        )
    handled_ids = event.get("handled_ids")
    if not isinstance(handled_ids, set):
        handled_ids = set()
        event["handled_ids"] = handled_ids
    source_quest_id = int(event.get("source_quest_id") or 0) & 0xFFFF
    source_grade = str(event.get("source_grade") or "")
    if qid in handled_ids:
        return "replay", source_quest_id, source_grade
    handled_ids.add(qid)
    if source_grade == "achievement":
        return "blocked_achievement", source_quest_id, source_grade
    return "accepted", source_quest_id, source_grade


def _handle_claimed_daily_challenge_trigger(
    server: Any,
    *,
    character_id: int,
    quest_id: int,
    state: DailyChallengeState,
    session: Any,
) -> tuple[list[bytes], str]:
    """Consume a stale post-claim event without rebuilding the terminal ID."""
    qid = int(quest_id) & 0xFFFF
    return [], (
        "daily_challenge quest=%d status=claimed stale_event=drop day=%s"
        % (qid, state.day_key)
    )


def handle_daily_challenge_set_trigger(
    server: Any,
    *,
    character_repo: Any,
    character_id: int,
    level: int,
    quest_id: int,
    trigger_type: int | None,
    is_increment: int | None,
    flag: int | None,
    session: Any = None,
) -> tuple[list[bytes], str] | None:
    index = get_daily_challenge_index()
    if not index.is_candidate(quest_id):
        return None
    definition = index.quest(int(quest_id))
    state = ensure_daily_challenge_state(
        character_repo,
        character_id=character_id,
        level=level,
        index=index,
    )
    # A reward claim is persisted independently of ordinary quest triggers.
    # Resolve that state before clear-grade event correlation so every Daily
    # type, including ``[use skill]``, takes the same non-completion path.
    entry = state.entry_for_quest(int(quest_id))
    if entry is not None and entry.reward_claimed:
        return _handle_claimed_daily_challenge_trigger(
            server,
            character_id=int(character_id),
            quest_id=int(quest_id),
            state=state,
            session=session,
        )
    trigger = int(
        trigger_type
        if trigger_type is not None
        else (flag if flag is not None else 0)
    ) & 0xFF
    increment = bool(is_increment) if is_increment is not None else False
    clear_grade_event = None
    if definition is not None and definition.quest_type == "clear quest by grade":
        clear_grade_event = _consume_daily_challenge_clear_grade_event(
            session,
            quest_id=int(quest_id),
        )
        event_status, source_quest_id, source_grade = clear_grade_event
        if event_status != "accepted":
            return [], (
                "daily_challenge quest=%d clear_grade_event=%s source=%d "
                "source_grade=%s mutation=suppressed ack=suppressed day=%s"
                % (
                    int(quest_id) & 0xFFFF,
                    event_status,
                    source_quest_id,
                    source_grade or "unknown",
                    state.day_key,
                )
        )
    ledger = DailyChallengeRepository(character_repo)
    result = ledger.mutate_entry(
        character_id=int(character_id),
        day_key=state.day_key,
        pvf_sha256=state.pvf_sha256,
        quest_id=int(quest_id),
        mutation=lambda target, current: min(
            int(target),
            apply_set_trigger_delta(
                int(current),
                trigger_type=trigger,
                is_increment=increment,
            ),
        ),
    )
    if result.status == "claimed":
        claimed_state = result.state or state
        return _handle_claimed_daily_challenge_trigger(
            server,
            character_id=int(character_id),
            quest_id=int(quest_id),
            state=claimed_state,
            session=session,
        )
    if clear_grade_event is not None:
        _event_status, source_quest_id, source_grade = clear_grade_event
        if not result.found:
            return [], (
                "daily_challenge quest=%d clear_grade_event=accepted source=%d "
                "source_grade=%s settled_or_unavailable mutation=unchanged "
                "ack=suppressed day=%s"
                % (
                    int(quest_id) & 0xFFFF,
                    source_quest_id,
                    source_grade or "unknown",
                    state.day_key,
                )
            )
        return [], (
            "daily_challenge quest=%d clear_grade_event=accepted source=%d "
            "source_grade=%s type=0x%02x inc=%d remaining=%d->%d target=%d "
            "changed=%d ack=suppressed"
            % (
                int(quest_id) & 0xFFFF,
                source_quest_id,
                source_grade or "unknown",
                trigger,
                int(increment),
                result.previous_value,
                result.current_value,
                result.target_value,
                int(result.changed),
            )
        )
    if not result.found:
        # A stale subscriber from yesterday is not a new completion event.
        # Replace the subscriber vector using the established snapshot reader.
        packet = server._codec_response(
            0, 0x0287,
            build_daily_challenge_body_0287(result.state or state, include_claimed=False),
        )
        return [packet], (
            "daily_challenge quest=%d status=%s settled_or_unavailable "
            "refresh=S0/0287 completion=suppressed day=%s"
            % (int(quest_id) & 0xFFFF, result.status, state.day_key)
        )
    packet = server._codec_response(
        1,
        0x0021,
        build_set_quest_trigger_ack(
            success=True,
            quest_id=int(quest_id),
            trigger_value=result.current_value,
        ),
    )
    return [packet], (
        "daily_challenge quest=%d type=0x%02x inc=%d remaining=%d->%d "
        "target=%d changed=%d"
        % (
            int(quest_id) & 0xFFFF,
            trigger,
            int(increment),
            result.previous_value,
            result.current_value,
            result.target_value,
            int(result.changed),
        )
    )


def handle_daily_challenge_disjoint_item(
    server: Any,
    *,
    character_repo: Any,
    character_id: int,
    level: int,
    event_count: int = 1,
) -> tuple[list[bytes], str]:
    """Apply one or more successfully settled equipment-disjoint events."""
    count = max(1, int(event_count))
    index = get_daily_challenge_index()
    state = ensure_daily_challenge_state(
        character_repo,
        character_id=character_id,
        level=level,
        index=index,
    )
    ledger = DailyChallengeRepository(character_repo)
    packets: list[bytes] = []
    notes: list[str] = []
    matched = 0
    changed = 0

    for group in state.groups:
        for entry in group.entries:
            definition = index.quest(entry.quest_id)
            if definition is None or definition.quest_type != "disjoint item":
                continue
            matched += 1
            result = ledger.mutate_entry(
                character_id=int(character_id),
                day_key=state.day_key,
                pvf_sha256=state.pvf_sha256,
                quest_id=entry.quest_id,
                mutation=lambda _target, current: max(0, int(current) - count),
            )
            if not result.found:
                notes.append("quest=%d settled_or_unavailable" % entry.quest_id)
                continue
            notes.append(
                "quest=%d remaining=%d->%d"
                % (entry.quest_id, result.previous_value, result.current_value)
            )
            if not result.changed:
                continue
            changed += 1
            packets.append(
                server._codec_response(
                    1,
                    0x0021,
                    build_set_quest_trigger_ack(
                        success=True,
                        quest_id=entry.quest_id,
                        trigger_value=result.current_value,
                    ),
                )
            )

    return packets, (
        "daily_challenge disjoint_item matched=%d changed=%d events=%d%s"
        % (matched, changed, count, ("; " + ", ".join(notes)) if notes else "")
    )


def _grant_items(
    character_repo: Any,
    *,
    character_id: int,
    quest_id: int,
    rewards: Sequence[DailyChallengeReward],
    reason: str,
) -> DailyChallengeGrantResult:
    pairs = [
        (int(reward.item_id), int(reward.count))
        for reward in rewards
        if int(reward.item_id) > 0 and int(reward.count) > 0
    ]
    if not pairs:
        raise ValueError("daily challenge reward has no positive item rows")
    granted, mailed, note = character_repo.grant_quest_items(
        int(character_id),
        pairs,
        quest_id=int(quest_id) & 0xFFFF,
        reason=str(reason),
    )
    if not granted and not mailed and not parse_virtual_premium_grants(note):
        raise RuntimeError("daily challenge item grant returned no inventory or mail rows")
    return DailyChallengeGrantResult(
        granted=tuple(
            (int(slot), int(item_id), int(count))
            for slot, item_id, count in (granted or ())
        ),
        mailed=tuple(
            (int(item_id), int(count)) for item_id, count in (mailed or ())
        ),
        note=str(note or ""),
    )


def _granted_insert_entries(
    character_repo: Any,
    *,
    character_id: int,
    granted: Sequence[tuple[int, int, int]],
) -> list[tuple[int, int, int, int, int]]:
    rows_by_slot: dict[int, dict[str, Any]] = {}
    try:
        rows_by_slot = {
            int(row["slot_index"]): row
            for row in character_repo.list_inventory_items(
                int(character_id), list_type=0
            )
        }
    except Exception:
        rows_by_slot = {}
    out: list[tuple[int, int, int, int, bool]] = []
    for slot, item_id, count in granted:
        metadata = resolve_item_metadata(int(item_id))
        row = rows_by_slot.get(int(slot))
        durability = (
            int(row.get("durability", 0) or 0)
            if row is not None
            else int(getattr(metadata, "durability", 0) or 0)
        )
        out.append(
            (
                int(slot) & 0xFFFF,
                int(item_id) & 0xFFFFFFFF,
                int(count) & 0xFFFFFFFF,
                max(0, durability) & 0xFFFF,
                bool(metadata.is_equipment),
            )
        )
    return out


def _inventory_refresh_packets(
    server: Any,
    character_repo: Any,
    *,
    character_id: int,
    granted: Sequence[tuple[int, int, int]],
) -> list[bytes]:
    if not granted:
        return []
    rows = character_repo.list_inventory_items(int(character_id), list_type=0)
    views = [InventoryItemView(row) for row in rows]
    wanted_slots = {int(slot) for slot, _item_id, _count in granted}
    changed = [view for view in views if int(view.slot_index) in wanted_slots]
    packets: list[bytes] = []
    if changed:
        packets.append(
            server._codec_response(
                0,
                0x000E,
                build_item_list_update_000e(changed, list_type=0),
            )
        )
    list_param16 = int(
        character_repo.get_container_list_param(int(character_id), list_type=0)
    )
    packets.append(
        server._codec_response(
            0,
            0x000D,
            build_item_list_mode0(
                views,
                list_type=0,
                list_param16=list_param16,
            ),
        )
    )
    return packets


def _entry_finish_success_packet(
    server: Any,
    character_repo: Any,
    *,
    character_id: int,
    quest_id: int,
    granted: Sequence[tuple[int, int, int]] = (),
    completion_event_quantity: int = 0,
) -> bytes:
    granted_meta = _granted_insert_entries(
        character_repo,
        character_id=character_id,
        granted=granted,
    )
    inserts = build_finish_reward_insert_entries(granted_items=granted_meta)
    return server._codec_response(
        1,
        0x0022,
        build_finish_quest_ack(
            success=True,
            quest_id=int(quest_id),
            exp=0,
            # Corrections keep the default zero; a newly settled entry
            # explicitly sends one below.
            completion_event_quantity=max(0, int(completion_event_quantity)),
            chain_type=0,
            insert_entries=inserts,
        ),
    )


def _entry_claim_completion_snapshot(
    server: Any,
    *,
    character_id: int,
) -> tuple[list[bytes], str]:
    # Local import avoids the quest_session -> daily_challenge import cycle.
    from game.quest.quest_session import pack_authoritative_finished_quest_snapshot

    return pack_authoritative_finished_quest_snapshot(
        server,
        character_id=int(character_id),
        allow_empty=True,
        context="daily_claim",
    )


def _entry_claim_daily_snapshot(
    server: Any,
    *,
    state: DailyChallengeState | None,
) -> tuple[bytes, str] | None:
    if state is None:
        return None
    body = build_daily_challenge_body_0287(state, include_claimed=False)
    active_entries = sum(
        1
        for group in state.groups
        for entry in group.entries
        if not entry.reward_claimed
    )
    claimed_omitted = sum(
        1
        for group in state.groups
        for entry in group.entries
        if entry.reward_claimed
    )
    return (
        server._codec_response(0, 0x0287, body),
        "S0/0287 daily_claim_rebuild active_entries=%d claimed_omitted=%d"
        % (active_entries, claimed_omitted),
    )


def handle_daily_challenge_entry_reward(
    server: Any,
    *,
    character_repo: Any,
    character_id: int,
    level: int,
    quest_id: int,
    session: Any = None,
) -> tuple[list[bytes], str] | None:
    index = get_daily_challenge_index()
    definition = index.quest(quest_id)
    if definition is None:
        return None
    state = ensure_daily_challenge_state(
        character_repo,
        character_id=character_id,
        level=level,
        index=index,
    )
    ledger = DailyChallengeRepository(character_repo)
    reservation = ledger.reserve_entry_claim(
        character_id=int(character_id),
        day_key=state.day_key,
        pvf_sha256=state.pvf_sha256,
        quest_id=int(quest_id),
    )
    if reservation.status == "already_claimed":
        packets: list[bytes] = [
            server._codec_response(
                1, 0x0022, build_finish_quest_ack(success=False, error_code=0x16),
            )
        ]
        completion_packets, completion_note = _entry_claim_completion_snapshot(
            server,
            character_id=int(character_id),
        )
        packets.extend(completion_packets)
        daily_snapshot = _entry_claim_daily_snapshot(
            server,
            state=reservation.state,
        )
        daily_note = "daily_rebuild=skip"
        if daily_snapshot is not None:
            daily_packet, daily_note = daily_snapshot
            packets.append(daily_packet)
        return packets, "daily_challenge entry quest=%d already_claimed; %s; %s" % (
            int(quest_id) & 0xFFFF, daily_note, completion_note
        )
    if reservation.status != "reserved":
        return [
            server._codec_response(
                1,
                0x0022,
                build_finish_quest_ack(success=False, error_code=0x16),
            )
        ], "daily_challenge entry quest=%d reject=%s" % (
            int(quest_id) & 0xFFFF,
            reservation.status,
        )

    try:
        grant = _grant_items(
            character_repo,
            character_id=character_id,
            quest_id=quest_id,
            rewards=definition.reward_items,
            reason="daily_challenge_entry_reward",
        )
    except Exception as exc:
        ledger.release_entry_claim(
            character_id=int(character_id), quest_id=int(quest_id)
        )
        return [
            server._codec_response(
                1,
                0x0022,
                build_finish_quest_ack(success=False, error_code=0x16),
            )
        ], "daily_challenge entry quest=%d grant_failed=%s:%s" % (
            int(quest_id) & 0xFFFF,
            type(exc).__name__,
            exc,
        )

    packets = [
        _entry_finish_success_packet(
            server,
            character_repo,
            character_id=character_id,
            quest_id=quest_id,
            granted=grant.granted,
            completion_event_quantity=1,
        )
    ]
    packets.extend(
        _premium_activation_packets(server, note=grant.note, session=session)
    )
    completion_packets, completion_note = _entry_claim_completion_snapshot(
        server,
        character_id=int(character_id),
    )
    packets.extend(completion_packets)
    daily_snapshot = _entry_claim_daily_snapshot(
        server,
        state=reservation.state,
    )
    daily_note = "daily_rebuild=skip"
    if daily_snapshot is not None:
        daily_packet, daily_note = daily_snapshot
        packets.append(daily_packet)
    try:
        packets.extend(
            _inventory_refresh_packets(
                server,
                character_repo,
                character_id=character_id,
                granted=grant.granted,
            )
        )
    except Exception as exc:
        return packets, (
            "daily_challenge entry quest=%d claimed items=%d mailed=%d "
            "refresh_failed=%s:%s; %s; %s; %s"
            % (
                int(quest_id) & 0xFFFF,
                len(grant.granted),
                len(grant.mailed),
                type(exc).__name__,
                exc,
                grant.note,
                daily_note,
                completion_note,
            )
        )
    return packets, (
        "daily_challenge entry quest=%d claimed items=%d mailed=%d; %s; %s; %s"
        % (
            int(quest_id) & 0xFFFF,
            len(grant.granted),
            len(grant.mailed),
            grant.note,
            daily_note,
            completion_note,
        )
    )


def handle_daily_challenge_group_reward(
    server: Any,
    *,
    character_repo: Any,
    character_id: int,
    level: int,
    group_index: int,
    session: Any = None,
) -> tuple[list[bytes], str]:
    index = get_daily_challenge_index()
    state = ensure_daily_challenge_state(
        character_repo,
        character_id=character_id,
        level=level,
        index=index,
    )
    ledger = DailyChallengeRepository(character_repo)
    reservation = ledger.reserve_group_claim(
        index,
        character_id=int(character_id),
        day_key=state.day_key,
        pvf_sha256=state.pvf_sha256,
        group_index=int(group_index),
    )
    if reservation.status == "already_claimed":
        return [
            server._codec_response(
                1,
                0x02BC,
                build_daily_challenge_group_reward_ack(
                    success=False, error_code=0x16,
                ),
            ),
            server._codec_response(
                0, 0x0287, build_daily_challenge_body_0287(state, include_claimed=False),
            ),
        ], "daily_challenge group=%d already_claimed" % int(group_index)
    if reservation.status != "reserved" or reservation.reward is None:
        return [
            server._codec_response(
                1,
                0x02BC,
                build_daily_challenge_group_reward_ack(
                    success=False, error_code=0x16
                ),
            )
        ], "daily_challenge group=%d reject=%s completed=%d/%d" % (
            int(group_index),
            reservation.status,
            reservation.completed_count,
            reservation.required_count,
        )

    try:
        grant = _grant_items(
            character_repo,
            character_id=character_id,
            quest_id=0,
            rewards=(reservation.reward,),
            reason="daily_challenge_group_reward",
        )
    except Exception as exc:
        ledger.release_group_claim(
            character_id=int(character_id), group_index=int(group_index)
        )
        return [
            server._codec_response(
                1,
                0x02BC,
                build_daily_challenge_group_reward_ack(
                    success=False, error_code=0x16
                ),
            )
        ], "daily_challenge group=%d grant_failed=%s:%s" % (
            int(group_index),
            type(exc).__name__,
            exc,
        )

    packets = [
        server._codec_response(
            1,
            0x02BC,
            build_daily_challenge_group_reward_ack(
                success=True, group_index=int(group_index)
            ),
        )
    ]
    packets.extend(
        _premium_activation_packets(server, note=grant.note, session=session)
    )
    try:
        packets.extend(
            _inventory_refresh_packets(
                server,
                character_repo,
                character_id=character_id,
                granted=grant.granted,
            )
        )
    except Exception as exc:
        return packets, (
            "daily_challenge group=%d claimed completed=%d/%d items=%d mailed=%d "
            "refresh_failed=%s:%s; %s"
            % (
                int(group_index),
                reservation.completed_count,
                reservation.required_count,
                len(grant.granted),
                len(grant.mailed),
                type(exc).__name__,
                exc,
                grant.note,
            )
        )
    return packets, (
        "daily_challenge group=%d claimed completed=%d/%d items=%d mailed=%d; %s"
        % (
            int(group_index),
            reservation.completed_count,
            reservation.required_count,
            len(grant.granted),
            len(grant.mailed),
            grant.note,
        )
    )
