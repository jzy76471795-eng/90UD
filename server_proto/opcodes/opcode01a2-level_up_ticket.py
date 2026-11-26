"""C1/01A2 character level-up ticket for frozen Stage3."""
from __future__ import annotations

from secrets import randbelow
import struct
from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail


_ticket_rules = load_chain("helper-level_up_ticket.py")
_exp = load_chain("helper-character_exp.py")
_skill_points = load_chain("helper-skill_points.py")
_payloads = load_chain("helper-dungeon_payloads.py")
_side_quests = load_chain("helper-level_up_ticket_side_quests.py")
_level_titles = load_chain("helper-level_milestone_titles.py")

resolve_level_up_ticket_rule = _ticket_rules.resolve_level_up_ticket_rule
load_exp_thresholds = _exp.load_exp_thresholds
max_character_level = _exp.max_character_level
grant_exp_to_character = _exp.grant_exp_to_character
resolve_skill_point_protocol_state = (
    _skill_points.resolve_skill_point_protocol_state
)
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e
auto_complete_level_up_ticket_side_quests = (
    _side_quests.auto_complete_level_up_ticket_side_quests
)


MAIN_LIST_TYPE = 0
ERROR_UNKNOWN = 0x01
ERROR_MISSING_SOURCE_ITEM = 0x11
ERROR_INVALID_ITEM = 0x16
ERROR_LEVEL_LIMIT = 0x5F


def roll_level_up_ticket_increment(levelup_range: int) -> int:
    """Draw the PVF-authored inclusive ``1..levelup_range`` level increase."""
    limit = int(levelup_range)
    if not 1 <= limit <= 255:
        raise ValueError("level-up ticket range is invalid")
    offset = int(randbelow(limit))
    if not 0 <= offset < limit:
        raise ValueError("level-up ticket random source returned out of range")
    return offset + 1


def parse_level_up_ticket_body(plain: bytes) -> dict[str, int] | None:
    """Parse the Stage3 ticket fields and the native codec's zero padding."""
    data = bytes(plain or b"")
    if len(data) not in (3, 16) or any(data[3:]):
        return None
    slot_index, list_type = struct.unpack_from("<hB", data)
    if slot_index < 0:
        return None
    return {
        "slot_index": int(slot_index),
        "list_type": int(list_type),
        "raw_len": len(data),
    }


def build_level_up_ticket_ack_ok(
    *,
    slot_index: int,
    list_type: int,
    exp_delta: int,
    sp_delta: int = 0,
    tp_delta: int = 0,
) -> bytes:
    """Build the 12-byte S1/01A2 success body consumed at ``0x01616D60``."""
    slot = int(slot_index)
    exp = int(exp_delta)
    sp = int(sp_delta)
    tp = int(tp_delta)
    if not 0 <= slot <= 0x7FFF:
        raise ValueError("level-up ticket slot must fit nonnegative i16")
    if not 0 <= exp <= 0xFFFFFFFF:
        raise ValueError("level-up ticket EXP delta must fit u32")
    if not 0 <= sp <= 0xFFFF or not 0 <= tp <= 0xFFFF:
        raise ValueError("level-up ticket SP/TP delta must fit u16")
    return bytes((1,)) + struct.pack(
        "<hBIHH",
        slot,
        int(list_type) & 0xFF,
        exp,
        sp,
        tp,
    )


def build_level_up_ticket_ack_error(error_code: int = ERROR_UNKNOWN) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def _error_for_progression_note(note: str) -> int:
    if "source_item" in str(note or ""):
        return ERROR_MISSING_SOURCE_ITEM
    if "progression_conflict" in str(note or ""):
        return ERROR_LEVEL_LIMIT
    return ERROR_UNKNOWN


class LevelUpTicket01A2Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _fail(
        self,
        note: str,
        *,
        error_code: int = ERROR_UNKNOWN,
    ) -> tuple[list[bytes], str]:
        body = build_level_up_ticket_ack_error(error_code)
        return [self.server._codec_response(1, 0x01A2, body)], (
            "C1/01A2 LEVEL_UP_TICKET reject=%s error=0x%02x; S1/01A2 failure"
            % (note, int(error_code) & 0xFF)
        )

    def handle(
        self,
        plain: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_level_up_ticket_body(plain)
        if parsed is None:
            return self._fail("malformed_body")
        slot = int(parsed["slot_index"])
        list_type = int(parsed["list_type"])
        if list_type != MAIN_LIST_TYPE or slot in (0, 1):
            return self._fail("invalid_inventory_position")

        detail = resolve_selected_character_detail(self.server, session)
        character_id = int(detail.get("character_id") or 0)
        if character_id <= 0:
            return self._fail("no_selected_character")
        job = int(detail.get("job") or 0)
        grow_type = int(detail.get("grow_type") or 0)
        repo = self.server._repo()
        item = repo.get_inventory_item_at(
            character_id,
            list_type=list_type,
            slot_index=slot,
        )
        if item is None:
            return self._fail(
                "slot_empty",
                error_code=ERROR_MISSING_SOURCE_ITEM,
            )
        item_id = int(item.get("item_template_id") or 0) & 0xFFFFFFFF
        try:
            rule = resolve_level_up_ticket_rule(item_id)
        except Exception as exc:
            return self._fail(
                "pvf_rule_%s" % type(exc).__name__,
                error_code=ERROR_INVALID_ITEM,
            )
        if rule is None:
            return self._fail(
                "item_%d_not_level_up_ticket" % item_id,
                error_code=ERROR_INVALID_ITEM,
            )

        current_level, current_exp, _expertise_exp = repo.get_character_progression(
            character_id
        )
        character_cap = int(max_character_level())
        if not rule.allows_current_level(
            current_level,
            character_cap=character_cap,
        ):
            return self._fail(
                "level_limit current=%d item_max=%d cap=%d"
                % (current_level, rule.maximum_level, character_cap),
                error_code=ERROR_LEVEL_LIMIT,
            )
        try:
            rolled_levels = roll_level_up_ticket_increment(rule.levelup_range)
            target_level = rule.target_level(
                current_level,
                character_cap=character_cap,
                level_increment=rolled_levels,
            )
        except Exception as exc:
            return self._fail(
                "level_range_%s" % type(exc).__name__,
                error_code=ERROR_INVALID_ITEM,
            )
        if target_level is None or int(target_level) <= int(current_level):
            return self._fail(
                "level_limit current=%d item_max=%d cap=%d"
                % (current_level, rule.maximum_level, character_cap),
                error_code=ERROR_LEVEL_LIMIT,
            )

        thresholds = load_exp_thresholds()
        threshold_index = int(target_level) - 2
        if threshold_index < 0 or threshold_index >= len(thresholds):
            return self._fail("missing_exp_threshold", error_code=ERROR_LEVEL_LIMIT)
        target_exp = int(thresholds[threshold_index])
        exp_delta = target_exp - int(current_exp)
        if exp_delta <= 0:
            return self._fail(
                "nonpositive_exp_gap current=%d target=%d"
                % (current_exp, target_exp),
                error_code=ERROR_LEVEL_LIMIT,
            )

        before_points = resolve_skill_point_protocol_state(
            level=current_level,
            character_id=character_id,
            repo=repo,
            session=session,
            job=job,
        )
        progression = grant_exp_to_character(
            repo,
            character_id,
            exp_delta,
            session=session,
            expected_level=current_level,
            expected_exp=current_exp,
            consume_stackable={
                "list_type": list_type,
                "slot_index": slot,
                "item_id": item_id,
                "count": 1,
            },
        )
        if not progression.get("ok"):
            progression_note = str(progression.get("note") or "progression_failed")
            return self._fail(
                progression_note,
                error_code=_error_for_progression_note(progression_note),
            )
        new_level = int(progression.get("new_level") or current_level)
        committed_invariant_errors: list[str] = []
        if new_level != int(target_level):
            committed_invariant_errors.append("level=%d" % new_level)
        if int(progression.get("ordinary_exp_added") or 0) != exp_delta:
            committed_invariant_errors.append(
                "exp_added=%d"
                % int(progression.get("ordinary_exp_added") or 0)
            )
        if int(progression.get("source_removed") or 0) != 1:
            committed_invariant_errors.append(
                "source_removed=%d"
                % int(progression.get("source_removed") or 0)
            )
        committed_invariant_note = (
            "committed_invariant=" + ",".join(committed_invariant_errors)
            if committed_invariant_errors
            else "committed_invariant=ok"
        )

        after_points = resolve_skill_point_protocol_state(
            level=new_level,
            character_id=character_id,
            repo=repo,
            session=session,
            job=job,
        )
        sp_delta = max(0, int(after_points.page0_sp) - int(before_points.page0_sp))
        tp_delta = max(0, int(after_points.page0_tp) - int(before_points.page0_tp))
        if session is not None:
            try:
                session.skill_remain_sp = int(after_points.page0_sp)
                session.skill_remain_tp = int(after_points.page0_tp)
            except Exception:
                pass

        side_note = "side_quests=0"
        side_result = None
        try:
            side_result = auto_complete_level_up_ticket_side_quests(
                self.server,
                character_id=character_id,
                job=job,
                level=new_level,
                grow_type=grow_type,
                session=session,
            )
            side_note = "side_quests=%d failed=%d" % (
                len(side_result.completed_quest_ids),
                len(side_result.failed_quest_ids),
            )
        except Exception as exc:
            side_note = "side_quests_error=%s" % type(exc).__name__

        remaining = max(0, int(progression.get("source_remaining") or 0))
        if remaining > 0:
            inventory_body = build_item_list_update_000e(
                [(slot, item_id, remaining)],
                list_type=list_type,
            )
            inventory_note = "update remain=%d" % remaining
        else:
            inventory_body = build_item_list_clear_slot_000e(
                slot,
                list_type=list_type,
            )
            inventory_note = "clear remain=0"

        # Keep the native ticket ACK delta-free. The complete GM level-refresh
        # flow below owns the authoritative client snapshot.
        client_ack_exp_delta = 0
        ack_body = build_level_up_ticket_ack_ok(
            slot_index=slot,
            list_type=list_type,
            exp_delta=client_ack_exp_delta,
            sp_delta=sp_delta,
            tp_delta=tp_delta,
        )
        refresh_packets, _refresh_result, refresh_note = (
            self.server.gm_profession_commands.pack_level_refresh(
                session=session,
                character_id=character_id,
                level=new_level,
                exp=int(progression.get("exp") or 0),
                expertise_exp=int(progression.get("expertise_exp") or 0),
                include_0164=bool(
                    side_result is not None
                    and side_result.completed_quest_ids
                ),
            )
        )
        packets = [
            self.server._codec_response(1, 0x01A2, ack_body),
            *refresh_packets,
            self.server._codec_response(0, 0x000E, inventory_body),
        ]

        title_note = "level_title=0"
        try:
            title_grants = []
            if _level_titles.has_crossed_level_title_threshold(
                current_level,
                new_level,
            ):
                title_grants, claim_note = _level_titles.claim_level_title_rewards(
                    repo,
                    character_id=character_id,
                    previous_level=current_level,
                    current_level=new_level,
                )
                title_packets, packet_note = (
                    _level_titles.pack_level_title_grant_packets(
                        self.server,
                        grants=title_grants,
                    )
                )
                packets.extend(title_packets)
                title_note = "%s;%s" % (claim_note, packet_note)
        except Exception as exc:
            title_note = "level_title_error=%s" % type(exc).__name__

        return packets, (
            "C1/01A2 LEVEL_UP_TICKET slot=%d list=%d item=%d source=%s "
            "pvf_range=%d rolled_levels=%d applied_levels=%d level=%d->%d exp=%d+%d "
            "client_ack_exp=%d snapshot_delta=%d sp=%d tp=%d; "
            "S1/01A2 success -> GM_LEVEL_REFRESH level=%d exp=%d, "
            "S0/000E %s; %s; %s; %s; %s"
            % (
                slot,
                list_type,
                item_id,
                rule.source_path or "-",
                rule.levelup_range,
                rolled_levels,
                int(target_level) - int(current_level),
                current_level,
                new_level,
                current_exp,
                exp_delta,
                client_ack_exp_delta,
                exp_delta - client_ack_exp_delta,
                sp_delta,
                tp_delta,
                new_level,
                int(progression.get("exp") or 0),
                inventory_note,
                side_note,
                refresh_note,
                title_note,
                committed_invariant_note,
            )
        )


def handle_level_up_ticket_01a2(
    server: Any,
    plain: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return LevelUpTicket01A2Service(server).handle(plain, session=session)


__all__ = [
    "ERROR_INVALID_ITEM",
    "ERROR_LEVEL_LIMIT",
    "ERROR_MISSING_SOURCE_ITEM",
    "ERROR_UNKNOWN",
    "LevelUpTicket01A2Service",
    "build_level_up_ticket_ack_error",
    "build_level_up_ticket_ack_ok",
    "handle_level_up_ticket_01a2",
    "parse_level_up_ticket_body",
    "roll_level_up_ticket_increment",
]
