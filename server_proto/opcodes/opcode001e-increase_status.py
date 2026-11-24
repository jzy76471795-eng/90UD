# -*- coding: utf-8 -*-
"""C1/0x001E INCREASE_STATUS for Main consumables.

Stage3 live evidence from 2026-08-04 shows that using a book from Main
quickslots sends exactly ``i16 slot_index``.  The S1 reader at ``0x0161B580``
consumes a 12-byte success body::

    u8  success
    u16 target_actor_key
    u8  status_type       # 0 = SP, 18 = TP
    u32 amount_delta
    u16 compatible_tail_0
    u16 compatible_tail_1

The request contains only an i16 Main slot.  The slot is a locator: the item
template in that slot selects SP, TP, or experience behavior.  The following
S0/000E synchronizes the consumed stack and experience items additionally use
the existing S0/0025 progression notification.
"""
from __future__ import annotations

from contextlib import nullcontext
import struct
from typing import Any, Dict, List, Optional, Tuple

from game.character import experience_consumable as _experience_consumables
from game.chain_loader import load_chain


_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e
build_exp_notification_0025 = _payloads.build_exp_notification_0025

_skill_point_books = load_chain("helper-skill_point_books.py")
skill_point_book_effect = _skill_point_books.skill_point_book_effect
consume_skill_point_book_at = _skill_point_books.consume_skill_point_book_at

_character_exp = load_chain("helper-character_exp.py")


MAIN_LIST_TYPE = 0
STATUS_TYPE_SP = 0
STATUS_TYPE_TP = 18
STATUS_TYPE_EXPERIENCE = 0xFF
ERROR_UNKNOWN = 0x01
ERROR_MISSING_SOURCE_ITEM = 0x11


def parse_increase_status_body(plain: bytes) -> Optional[Dict[str, int]]:
    """Parse the exact Stage3 request body: one signed Main slot index."""
    data = bytes(plain or b"")
    if len(data) != 2:
        return None
    slot = int(struct.unpack("<h", data)[0])
    if slot < 0:
        return None
    return {"slot_index": slot, "raw_len": len(data)}


def build_increase_status_ack_ok(
    *,
    target_actor_key: int,
    status_type: int,
    amount_delta: int,
) -> bytes:
    """Build the exact S1/001E success body consumed by ``0x0161B580``."""
    actor_key = int(target_actor_key)
    status = int(status_type)
    amount = int(amount_delta)
    if not 0 < actor_key <= 0xFFFF:
        raise ValueError("target_actor_key must fit a nonzero u16")
    if status in (STATUS_TYPE_SP, STATUS_TYPE_TP):
        if not 0 < amount <= 0xFFFFFFFF:
            raise ValueError("amount_delta must fit a positive u32")
    elif status == STATUS_TYPE_EXPERIENCE:
        if amount != 0:
            raise ValueError("experience status must carry a zero delta")
    else:
        raise ValueError("unsupported INCREASE_STATUS status type")
    return struct.pack("<BHBIHH", 1, actor_key, status, amount, 0, 0)


def build_increase_status_ack_error(error_code: int = ERROR_UNKNOWN) -> bytes:
    """Build the S1/001E failure body: result byte followed by reader error."""
    return bytes((0, int(error_code) & 0xFF))


def _selected_character_id(session: Any) -> Optional[int]:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        if value is None:
            continue
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return None


def _failure_error(note: str) -> int:
    if note in {"slot_empty", "item_mismatch", "invalid_item"}:
        return ERROR_MISSING_SOURCE_ITEM
    return ERROR_UNKNOWN


class IncreaseStatus001EService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _stage(self, name: str) -> Any:
        metrics = getattr(self.server, "hotpath_metrics", None)
        return metrics.stage(name) if metrics is not None else nullcontext()

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        with self._stage("codec"):
            return self.server._codec_response(flag, opcode, body)

    def _fail(
        self,
        note: str,
        *,
        error_code: int = ERROR_UNKNOWN,
    ) -> Tuple[List[bytes], str]:
        body = build_increase_status_ack_error(error_code)
        packet = self._emit(1, 0x001E, body)
        return [packet], (
            "C1/001E INCREASE_STATUS reject=%s error=0x%02x; "
            "S1/001E failure len=%d"
            % (note, int(error_code) & 0xFF, len(body))
        )

    @staticmethod
    def _session_job(session: Any) -> int | None:
        for attr in ("character_job", "job", "job_id"):
            value = getattr(session, attr, None) if session is not None else None
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _session_hardcore(session: Any) -> bool:
        if session is None:
            return False
        return bool(
            getattr(session, "hardcore_mode", False)
            or getattr(session, "is_hardcore_mode", False)
        )

    def _build_experience_snapshot(
        self,
        *,
        repo: Any,
        character_id: int,
        session: Any,
        level: int,
        exp: int,
        expertise_exp: int,
    ) -> bytes:
        ledger = repo.get_character_skill_points(int(character_id)) or {}
        remain_sp = int(ledger.get("remaining_sp") or getattr(session, "skill_remain_sp", 0) or 0)
        remain_tp = int(ledger.get("remaining_tp") or getattr(session, "skill_remain_tp", 0) or 0)
        return build_exp_notification_0025(
            int(level),
            int(exp),
            expertise_exp=int(expertise_exp),
            remain_sp=remain_sp,
            remain_tp=remain_tp,
            page0_sp=remain_sp,
            page1_sp=remain_sp,
            page0_tp=remain_tp,
            page1_tp=0,
            gain_exp=0,
        )

    def _experience_refresh_packets(
        self,
        *,
        repo: Any,
        character_id: int,
        session: Any,
        result: dict[str, Any],
    ) -> tuple[list[bytes], str]:
        new_level = int(result.get("new_level") or 1)
        new_exp = int(result.get("exp") or 0)
        expertise_exp = int(result.get("expertise_exp") or 0)
        levels_gained = int(result.get("levels_gained") or 0)
        if levels_gained > 0:
            gm = getattr(self.server, "gm_profession_commands", None)
            pack_refresh = getattr(gm, "pack_level_refresh", None)
            if callable(pack_refresh):
                try:
                    refreshed = pack_refresh(
                        session=session,
                        character_id=int(character_id),
                        level=new_level,
                        exp=new_exp,
                        expertise_exp=expertise_exp,
                    )
                    if isinstance(refreshed, tuple) and len(refreshed) >= 1:
                        packets = list(refreshed[0] or ())
                        return packets, "level_refresh=pack_level_refresh packets=%d" % len(packets)
                except Exception as exc:
                    refresh_note = "level_refresh=fallback:%s" % type(exc).__name__
                else:
                    refresh_note = "level_refresh=pack_level_refresh_empty"
            else:
                refresh_note = "level_refresh=no_service"
        else:
            refresh_note = "level_refresh=same_level"
        body = self._build_experience_snapshot(
            repo=repo,
            character_id=int(character_id),
            session=session,
            level=new_level,
            exp=new_exp,
            expertise_exp=expertise_exp,
        )
        return [self._emit(0, 0x0025, body)], refresh_note + " S0/0025"

    def handle(self, plain: bytes, *, session: Any) -> Tuple[List[bytes], str]:
        parsed = parse_increase_status_body(plain)
        if parsed is None:
            return self._fail("malformed_body")

        slot = int(parsed["slot_index"])
        if slot in (0, 1):
            return self._fail("wallet_slot", error_code=ERROR_MISSING_SOURCE_ITEM)

        character_id = _selected_character_id(session)
        if character_id is None:
            return self._fail("no_selected_character")
        actor_key = int(character_id) & 0xFFFF
        if actor_key == 0:
            return self._fail("invalid_actor_key")

        repo = self.server._repo()
        with self._stage("db_lookup"):
            item = repo.get_inventory_item_at(
                int(character_id),
                list_type=MAIN_LIST_TYPE,
                slot_index=slot,
            )
        if item is None:
            return self._fail(
                "slot_empty",
                error_code=ERROR_MISSING_SOURCE_ITEM,
            )

        item_id = int(item.get("item_template_id") or 0) & 0xFFFFFFFF
        effect = skill_point_book_effect(item_id)
        if effect is not None:
            add_sp, add_tp = int(effect[0]), int(effect[1])
            if (add_sp > 0) == (add_tp > 0):
                return self._fail("invalid_book_effect_%d" % item_id)
            with self._stage("db_consume"):
                ok, consume_note, detail = consume_skill_point_book_at(
                    repo,
                    character_id=int(character_id),
                    list_type=MAIN_LIST_TYPE,
                    slot_index=slot,
                    expected_item_id=item_id,
                    hardcore=self._session_hardcore(session),
                )
            if not ok or int(detail.get("removed") or 0) != 1:
                return self._fail(
                    consume_note,
                    error_code=_failure_error(consume_note),
                )

            status_type = STATUS_TYPE_SP if add_sp > 0 else STATUS_TYPE_TP
            amount = add_sp if add_sp > 0 else add_tp
            remaining = max(0, int(detail.get("remaining") or 0))
            ack_body = build_increase_status_ack_ok(
                target_actor_key=actor_key,
                status_type=status_type,
                amount_delta=amount,
            )
            if remaining > 0:
                inventory_body = build_item_list_update_000e(
                    [(slot, item_id, remaining)],
                    list_type=MAIN_LIST_TYPE,
                )
                inventory_note = "update remain=%d" % remaining
            else:
                inventory_body = build_item_list_clear_slot_000e(
                    slot,
                    list_type=MAIN_LIST_TYPE,
                )
                inventory_note = "clear remain=0 item_id=-1"

            packets = [
                self._emit(1, 0x001E, ack_body),
                self._emit(0, 0x000E, inventory_body),
            ]
            if session is not None:
                try:
                    session.skill_remain_sp = int(detail.get("remaining_sp") or 0)
                    session.skill_remain_tp = int(detail.get("remaining_tp") or 0)
                except Exception:
                    pass

            note = (
                "C1/001E INCREASE_STATUS slot=%d item=%d; "
                "skill_point_book_consumed add_sp=%d add_tp=%d "
                "remain_sp=%d remain_tp=%d; "
                "S1/001E success actor=%d type=%d amount=%d -> "
                "S0/000E %s"
                % (
                    slot,
                    item_id,
                    add_sp,
                    add_tp,
                    int(detail.get("remaining_sp") or 0),
                    int(detail.get("remaining_tp") or 0),
                    actor_key,
                    status_type,
                    amount,
                    inventory_note,
                )
            )
            return packets, note

        experience_rule = _experience_consumables.resolve_experience_consumable(item_id)
        if experience_rule is None:
            return self._fail("unsupported_item_%d" % item_id)

        level, current_exp, expertise_exp = repo.get_character_progression(int(character_id))
        usable, usable_note = experience_rule.is_usable(
            level=int(level),
            job=self._session_job(session),
            hardcore=self._session_hardcore(session),
        )
        if not usable:
            return self._fail("%s_item_%d" % (usable_note, item_id))
        gain, target_level = _experience_consumables.calculate_experience_gain(
            experience_rule,
            level=int(level),
            current_exp=int(current_exp),
            thresholds=_character_exp.load_exp_thresholds(),
        )
        if gain <= 0 and (target_level is None or int(target_level) <= int(level)):
            return self._fail("zero_experience_gain_%d" % item_id)
        with self._stage("db_consume"):
            progression = _character_exp.grant_exp_to_character(
                repo,
                int(character_id),
                int(gain),
                session=session,
                expected_level=int(level),
                expected_exp=int(current_exp),
                consume_stackable={
                    "list_type": MAIN_LIST_TYPE,
                    "slot_index": slot,
                    "item_id": item_id,
                    "count": 1,
                },
            )
        if not progression.get("ok") or int(progression.get("source_removed") or 0) != 1:
            return self._fail(
                str(progression.get("note") or "experience_transaction_failed"),
                error_code=ERROR_MISSING_SOURCE_ITEM,
            )

        remaining = max(0, int(progression.get("source_remaining") or 0))
        ack_body = build_increase_status_ack_ok(
            target_actor_key=actor_key,
            status_type=STATUS_TYPE_EXPERIENCE,
            amount_delta=0,
        )
        if remaining > 0:
            inventory_body = build_item_list_update_000e(
                [(slot, item_id, remaining)],
                list_type=MAIN_LIST_TYPE,
            )
            inventory_note = "update remain=%d" % remaining
        else:
            inventory_body = build_item_list_clear_slot_000e(
                slot,
                list_type=MAIN_LIST_TYPE,
            )
            inventory_note = "clear remain=0 item_id=-1"

        packets = [self._emit(1, 0x001E, ack_body)]
        packets.append(self._emit(0, 0x000E, inventory_body))
        refresh_packets, refresh_note = self._experience_refresh_packets(
            repo=repo,
            character_id=int(character_id),
            session=session,
            result=progression,
        )
        packets.extend(refresh_packets)
        note = (
            "C1/001E INCREASE_STATUS slot=%d item=%d; "
            "experience_consumable kind=%s gain=%d level=%d->%d exp=%d->%d "
            "remain=%d; S1/001E status=0xFF -> S0/000E %s -> %s"
            % (
                slot,
                item_id,
                experience_rule.kind,
                gain,
                int(progression.get("old_level") or level),
                int(progression.get("new_level") or level),
                int(progression.get("old_exp") or current_exp),
                int(progression.get("exp") or current_exp),
                remaining,
                inventory_note,
                refresh_note,
            )
        )
        return packets, note


def handle_increase_status_001e(
    server: Any,
    plain: bytes,
    *,
    session: Any,
) -> Tuple[List[bytes], str]:
    return IncreaseStatus001EService(server).handle(plain, session=session)


__all__ = [
    "ERROR_MISSING_SOURCE_ITEM",
    "ERROR_UNKNOWN",
    "IncreaseStatus001EService",
    "MAIN_LIST_TYPE",
    "STATUS_TYPE_EXPERIENCE",
    "STATUS_TYPE_SP",
    "STATUS_TYPE_TP",
    "build_increase_status_ack_error",
    "build_increase_status_ack_ok",
    "handle_increase_status_001e",
    "parse_increase_status_body",
]
