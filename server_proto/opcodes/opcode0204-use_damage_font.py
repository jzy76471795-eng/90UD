"""C1/0204 dispatches PVF-backed stackable actions used by Stage3."""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain
from game.client_toast_bridge import (
    ClientToastBridgeError,
    TOAST_MODE_TOAST,
    send_client_toast,
)
from game.character.damage_font import (
    build_damage_font_list_packet,
    build_main_inventory_refresh_packet,
)
from game.character.damage_font_index import get_damage_font_index
from game.character.item_metadata import resolve_item_metadata
from game.quest.quest_session import resolve_selected_character_detail


USE_DAMAGE_FONT_OPCODE = 0x0204
DAMAGE_FONT_ACTION_TYPE = 0x82
MAIN_LIST_TYPE = 0
EXTERNAL_CLEAR_TOAST = (
    "正在清空所有主线/外传/系统/成就/个性/时空碎片任务，请耐心等待。"
)

_quest_tickets = load_chain("helper-quest_completion_ticket.py")
_titlebook = load_chain("helper-titlebook_grant.py")

# Stage3 live vectors captured on 2026-08-10. Achievement clear remains
# item/PVF-authoritative until its transport action is observed separately.
_LIVE_ACTION_BY_QUEST_TICKET = {
    _quest_tickets.QUEST_CLEAR_ACTION: 0x47,
    _quest_tickets.FIRST_AWAKEN_CLEAR_ACTION: 0x5A,
    _quest_tickets.SECOND_AWAKEN_CLEAR_ACTION: 0x5C,
    _quest_tickets.ANY_QUEST_CLEAR_ACTION: 0x62,
}


def parse_use_damage_font_0204(body: bytes) -> dict[str, int] | None:
    """Parse ``i16 slot, u8 list, u32 instance/action/arg1/arg2`` exactly."""
    raw = bytes(body or b"")
    if len(raw) != 19:
        return None
    slot, list_type, instance, action_type, argument1, argument2 = struct.unpack(
        "<hBIIII",
        raw,
    )
    if slot < 0:
        return None
    return {
        "slot_index": int(slot),
        "list_type": int(list_type),
        "instance": int(instance),
        "action_type": int(action_type),
        "argument1": int(argument1),
        "argument2": int(argument2),
    }


def _titlebook_identity(
    repo: Any,
    character_id: int,
) -> frozenset[tuple[int, int, int]]:
    return frozenset(
        (
            int(row.get("category", 0) or 0),
            int(row.get("book_index", 0) or 0),
            int(row.get("item_id", 0) or 0),
        )
        for row in repo.list_titlebook_items(int(character_id))
        if int(row.get("item_id", 0) or 0) > 0
    )


def handle_use_damage_font_0204(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    parsed = parse_use_damage_font_0204(body)
    if parsed is None:
        return [], "C1/0204 USE_STACKABLE_ACTION ignored malformed_body"
    # 01A858C0 -> 01A85540: [story book] uses action 0x67 and the
    # PVF quest ID as argument1. It cannot pass the zero-argument gate below.
    if parsed["action_type"] == 0x67:
        from game.quest.story_book import use_story_book
        return use_story_book(server, parsed, session=session)
    if (
        parsed["list_type"] != MAIN_LIST_TYPE
        or parsed["instance"] != 0
        or parsed["argument1"] != 0
        or parsed["argument2"] != 0
    ):
        return [], (
            "C1/0204 USE_STACKABLE_ACTION ignored unsupported_layout "
            "list=%d instance=%d action=0x%x arg1=%d arg2=%d"
            % (
                parsed["list_type"],
                parsed["instance"],
                parsed["action_type"],
                parsed["argument1"],
                parsed["argument2"],
            )
        )

    detail = resolve_selected_character_detail(server, session)
    character_id = int(detail.get("character_id") or 0)
    if character_id <= 0:
        return [], "C1/0204 USE_STACKABLE_ACTION ignored no_selected_character"
    repo = server._repo()
    slot = int(parsed["slot_index"])
    item = repo.get_inventory_item_at(
        character_id,
        list_type=MAIN_LIST_TYPE,
        slot_index=slot,
    )
    if item is None:
        return [], "C1/0204 USE_STACKABLE_ACTION reject=slot_empty slot=%d" % slot
    item_id = int(item.get("item_template_id") or 0) & 0xFFFFFFFF
    metadata = resolve_item_metadata(item_id)
    # Preserve the deployed fatigue branch while synchronizing its missing
    # plaintext source (native dispatch verified before this change).
    if metadata.action_type == "[add fatigue]":
        from game.character import fatigue_consumable
        from game.character.fatigue_flow import native_body
        if parsed["action_type"] != fatigue_consumable.ACTION:
            return [], "C1/0204 FATIGUE_USE reject=action_mismatch"
        account_id = repo.get_account_id_for_character(character_id)
        if account_id is None:
            return [], "C1/0204 FATIGUE_USE reject=owner_missing"
        ok, note, balance = fatigue_consumable.consume_at(
            repo, account_id=int(account_id), character_id=character_id,
            slot=slot, item_id=item_id, metadata=metadata,
            in_dungeon=bool(getattr(session, "in_dungeon_run", False)),
        )
        if not ok:
            return [], "C1/0204 FATIGUE_USE reject=%s item=%d" % (note, item_id)
        inventory_packet, inventory_note = build_main_inventory_refresh_packet(
            server, repo, character_id,
        )
        return [inventory_packet, server._codec_response(0, 0x0024, native_body(balance))], (
            "C1/0204 FATIGUE_USE success item=%d remaining=%d source=%s; %s"
            % (item_id, balance.remaining, metadata.source_path, inventory_note)
        )
    task_rule = _quest_tickets.QuestCompletionTicketRule.from_action(
        metadata.action_type,
        metadata.action_values,
    )
    if task_rule is not None:
        if bool(getattr(session, "in_dungeon_run", False)):
            return [], (
                "C1/0204 QUEST_COMPLETION_TICKET reject=in_dungeon "
                "slot=%d item=%d pvf_action=%s ticket=preserved"
                % (slot, item_id, task_rule.action_type)
            )
        expected_action = _LIVE_ACTION_BY_QUEST_TICKET.get(task_rule.action_type)
        if (
            expected_action is not None
            and int(parsed["action_type"]) != int(expected_action)
        ):
            return [], (
                "C1/0204 QUEST_COMPLETION_TICKET reject=action_mismatch "
                "slot=%d item=%d pvf_action=%s request=0x%x expected=0x%x"
                % (
                    slot,
                    item_id,
                    task_rule.action_type,
                    int(parsed["action_type"]),
                    int(expected_action),
                )
            )

        toast_note = "toast=not_required"
        if task_rule.is_external_clear:
            try:
                send_client_toast(
                    EXTERNAL_CLEAR_TOAST,
                    mode=TOAST_MODE_TOAST,
                )
                toast_note = "toast=published"
            except (ClientToastBridgeError, OSError) as exc:
                toast_note = "toast=error_%s" % type(exc).__name__

        titlebook_before = _titlebook_identity(repo, character_id)
        result = _quest_tickets.auto_complete_quest_completion_ticket(
            server,
            character_id=character_id,
            job=int(detail.get("job") or 0),
            level=max(1, int(detail.get("level") or 1)),
            grow_type=int(detail.get("grow_type") or 0),
            rule=task_rule,
            session=session,
        )
        completed_count = len(result.completed_quest_ids)
        failed_count = len(result.failed_quest_ids)
        skipped_count = len(result.skipped_quest_ids)
        if completed_count <= 0:
            return [], (
                "C1/0204 QUEST_COMPLETION_TICKET no_effect character=%d "
                "slot=%d item=%d pvf_action=%s values=%s failed=%d skipped=%d "
                "source=%s ticket=preserved %s"
                % (
                    character_id,
                    slot,
                    item_id,
                    task_rule.action_type,
                    ",".join(str(value) for value in task_rule.action_values) or "-",
                    failed_count,
                    skipped_count,
                    metadata.source_path or "-",
                    toast_note,
                )
            )

        consumed = False
        consume_note = "batch_failed_ticket_preserved"
        consume_detail: dict[str, Any] = {}
        if result.ok:
            consumed, consume_note, consume_detail = repo.consume_stackable_at(
                character_id,
                list_type=MAIN_LIST_TYPE,
                slot_index=slot,
                expected_item_id=item_id,
                count=1,
            )

        level, exp, expertise_exp = repo.get_character_progression(character_id)
        refresh_packets, _refresh_result, refresh_note = (
            server.gm_profession_commands.pack_level_refresh(
                session=session,
                character_id=character_id,
                level=level,
                exp=exp,
                expertise_exp=expertise_exp,
                include_0164=True,
            )
        )
        inventory_packet, inventory_note = build_main_inventory_refresh_packet(
            server,
            repo,
            character_id,
        )
        titlebook_after = _titlebook_identity(repo, character_id)
        title_packets: list[bytes] = []
        title_note = "titlebook_batch unchanged"
        if titlebook_after != titlebook_before:
            title_packets, title_note = (
                _titlebook.pack_titlebook_batch_snapshot_packets(
                    server,
                    character_id=character_id,
                )
            )
        packets = [*refresh_packets, *title_packets, inventory_packet]
        return packets, (
            "C1/0204 QUEST_COMPLETION_TICKET character=%d slot=%d item=%d "
            "pvf_action=%s values=%s completed=%d failed=%d skipped=%d "
            "consumed=%d remain=%d consume=%s source=%s packets=%d %s; %s; %s; %s"
            % (
                character_id,
                slot,
                item_id,
                task_rule.action_type,
                ",".join(str(value) for value in task_rule.action_values) or "-",
                completed_count,
                failed_count,
                skipped_count,
                int(bool(consumed)),
                int(consume_detail.get("remaining") or 0),
                consume_note,
                metadata.source_path or "-",
                len(packets),
                toast_note,
                refresh_note,
                title_note,
                inventory_note,
            )
        )

    rule = get_damage_font_index().rule_for(item_id)
    if rule is None or int(parsed["action_type"]) != DAMAGE_FONT_ACTION_TYPE:
        return [], (
            "C1/0204 USE_STACKABLE_ACTION ignored unsupported_action "
            "slot=%d item=%d request=0x%x pvf_action=%s values=%s"
            % (
                slot,
                item_id,
                int(parsed["action_type"]),
                metadata.action_type or "-",
                ",".join(str(value) for value in metadata.action_values) or "-",
            )
        )

    ok, note, result = repo.activate_damage_font_at(
        character_id,
        list_type=MAIN_LIST_TYPE,
        slot_index=slot,
        expected_item_id=item_id,
        font_id=rule.font_id,
        expiration_mode=rule.expiration_mode,
        expiration_value=rule.expiration_value,
    )
    if not ok:
        return [], (
            "C1/0204 DAMAGE_FONT_USE reject=%s character=%d slot=%d item=%d "
            "font=%d source=%s"
            % (
                note,
                character_id,
                slot,
                item_id,
                rule.font_id,
                rule.source_path or "-",
            )
        )

    inventory_packet, inventory_note = build_main_inventory_refresh_packet(
        server,
        repo,
        character_id,
    )
    font_packet, font_note = build_damage_font_list_packet(
        server,
        repo,
        character_id,
    )
    return [inventory_packet, font_packet], (
        "C1/0204 DAMAGE_FONT_USE success character=%d slot=%d item=%d "
        "font=%d expiration=%s:%d expire_at=%d remain=%d source=%s; %s; %s"
        % (
            character_id,
            slot,
            item_id,
            rule.font_id,
            rule.expiration_mode,
            rule.expiration_value,
            int(result.get("expire_at") or 0),
            int(result.get("remaining") or 0),
            rule.source_path or "-",
            inventory_note,
            font_note,
        )
    )


__all__ = [
    "DAMAGE_FONT_ACTION_TYPE",
    "USE_DAMAGE_FONT_OPCODE",
    "handle_use_damage_font_0204",
    "parse_use_damage_font_0204",
]
