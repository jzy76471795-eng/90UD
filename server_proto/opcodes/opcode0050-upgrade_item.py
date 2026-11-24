# -*- coding: utf-8 -*-
"""C1/0050 equipment, amplification, and guild-medal reinforcement."""
from __future__ import annotations

import random
import struct
from typing import Any

from game.character.character_db import CLIENT_GOLD_DISPLAY_CAP
from game.character.amplify_option import AMPLIFY_TYPES, parse_amplify_state
from game.character.guild_medal_upgrade import (
    GUILD_MEDAL_CONTRIBUTION_ITEM_ID,
    GuildMedalUpgradeError,
    guild_medal_four_stat_bonus,
    settle_guild_medal_upgrade,
)
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    resolve_item_metadata,
)
from game.character.payloads import build_item_list_update_000e
from game.character.upgrade_config import (
    UPGRADE_WEIGHT_SCALE, get_amplify_upgrade_config, get_upgrade_config,
    is_upgrade_target_type, is_weapon_type,
)
from game.character.upgrade_ticket import (
    MODE_AMPLIFY as TICKET_MODE_AMPLIFY,
    MODE_REINFORCE as TICKET_MODE_REINFORCE,
    find_portable_upgrade_rule,
    find_upgrade_protection_ticket_rule,
    find_upgrade_ticket_rule,
)
from game.character.random_enchant_ticket import find_random_enchant_ticket
from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_TARGET = 4
ERROR_INSUFFICIENT_GOLD = 10
ERROR_UNSUPPORTED = 13
ERROR_RESTRICTION = 19
ERROR_UNSUPPORTED_OPTIONAL_TICKET = 21
ERROR_INVALID_MATERIAL = 22
ERROR_WRONG_UPGRADE_MODE = 23
ERROR_MAX_LEVEL = 95

UPGRADE_METHOD_NPC = 0
UPGRADE_METHOD_TICKET = 1

_payloads = load_chain("helper-dungeon_payloads.py")
build_wallet_gold_update_000e = _payloads.build_wallet_gold_update_000e


def parse_upgrade_item_body(plain: bytes) -> dict[str, Any] | None:
    """Parse the verified Stage3 C1/0050 writer layout."""
    data = bytes(plain or b"")
    if len(data) < 20:
        return None
    mode, target_list_type, target_slot, target_item_id, material_slot, ticket_slot, name_length = (
        struct.unpack_from("<BBhIhhI", data, 0)
    )
    tail_offset = 16 + int(name_length)
    if name_length > 4096 or tail_offset + 4 != len(data):
        return None
    if target_slot < 0 or material_slot < 0 or target_item_id <= 0:
        return None
    name_raw = data[16:tail_offset]
    return {
        "mode": int(mode), "target_list_type": int(target_list_type),
        "target_slot": int(target_slot), "target_item_id": int(target_item_id),
        "material_slot": int(material_slot), "optional_ticket_slot": int(ticket_slot),
        "target_name": name_raw.decode("utf-8", errors="replace"),
        "target_name_raw": name_raw,
        "trailing_i32_unknown": int(struct.unpack_from("<i", data, tail_offset)[0]),
        "semantic_length": tail_offset + 4, "raw_length": len(data),
    }


def build_upgrade_item_error(error_code: int) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def build_upgrade_item_notice_0056(
    *, success: bool, actor_key: int, item_id: int, reinforce_level: int,
) -> bytes:
    """Build S0/0056 subtype 1, the Stage3 upgrade result notification."""
    return struct.pack(
        "<BBHIBB",
        1,
        1 if success else 0,
        int(actor_key) & 0xFFFF,
        int(item_id) & 0xFFFFFFFF,
        int(reinforce_level) & 0xFF,
        0,
    )


def build_upgrade_item_success(
    *, mode: int, material_slot: int, material_remaining: int,
    optional_ticket_slot: int, method: int, old_level: int, result_code: int,
    new_level: int, target_list_type: int, target_slot: int,
    destroy_rewards: list[dict[str, int]] | None = None,
    gold_slot: int = 0, updated_gold: int = 0,
) -> bytes:
    """Build the exact Stage3 sub_1629EC0 success body."""
    rewards = list(destroy_rewards or ())
    if len(rewards) > 0xFF:
        raise ValueError("0050 destroy reward count exceeds u8")
    body = bytearray((1, int(mode) & 0xFF))
    body += struct.pack(
        "<hIhBBBBBh", int(material_slot), max(0, int(material_remaining)) & 0xFFFFFFFF,
        int(optional_ticket_slot), int(method) & 0xFF, int(old_level) & 0xFF,
        int(result_code) & 0xFF, int(new_level) & 0xFF,
        int(target_list_type) & 0xFF, int(target_slot),
    )
    if int(result_code) == 3:
        body.append(len(rewards) & 0xFF)
        for reward in rewards:
            body += struct.pack(
                "<hIII", int(reward.get("slot_index", -1)),
                int(reward.get("item_template_id", 0)) & 0xFFFFFFFF,
                max(0, int(reward.get("count", 0))) & 0xFFFFFFFF,
                int(reward.get("entry_state_56", 0)) & 0xFFFFFFFF,
            )
    body += struct.pack(
        "<hI", int(gold_slot),
        min(CLIENT_GOLD_DISPLAY_CAP, max(0, int(updated_gold))) & 0xFFFFFFFF,
    )
    return bytes(body)


def _cid(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            try:
                cid = int(getattr(session, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if cid > 0:
                return cid
    try:
        cid = int(resolve_selected_character_detail(server, session).get("character_id") or 0)
        return cid if cid > 0 else None
    except Exception:
        return None


def _db_error_code(note: str) -> int:
    text = str(note or "")
    if text.startswith("gold_"):
        return ERROR_INSUFFICIENT_GOLD
    if text.startswith("material_"):
        return ERROR_INVALID_MATERIAL
    if text.startswith("protection_"):
        return ERROR_INVALID_MATERIAL
    if text.startswith("destroy_reward_"):
        return ERROR_UNSUPPORTED
    return ERROR_INVALID_TARGET


def _session_hardcore(session: Any) -> bool:
    return bool(
        session is not None
        and (
            getattr(session, "hardcore_mode", False)
            or getattr(session, "is_hardcore_mode", False)
        )
    )


def _character_eligibility_context(
    repo: Any,
    character_id: int,
) -> tuple[int, int] | None:
    try:
        context = repo.get_character_level_job(int(character_id))
    except Exception:
        return None
    if not isinstance(context, tuple) or len(context) != 2:
        return None
    try:
        level, job = int(context[0]), int(context[1])
    except (TypeError, ValueError):
        return None
    if level <= 0 or job < 0:
        return None
    return level, job & 0xFF


def _select_upgrade_protection_ticket(
    rows: list[dict[str, Any]],
    *,
    upgrade_mode: int,
    excluded_slots: tuple[int, ...] = (),
    character_level: int = 1,
    character_job: int = 0,
    hardcore: bool = False,
) -> tuple[dict[str, Any], Any] | None:
    excluded = {int(slot) for slot in excluded_slots}
    candidates: list[tuple[tuple[int, int], dict[str, Any], Any]] = []
    for row in rows:
        slot = int(row.get("slot_index") or 0)
        if slot in excluded:
            continue
        if (
            str(row.get("item_kind") or "") != "stackable"
            or int(row.get("stack_count") or 0) <= 0
        ):
            continue
        item_id = int(row.get("item_template_id") or 0) & 0xFFFFFFFF
        rule = find_upgrade_protection_ticket_rule(item_id)
        if rule is None or int(rule.mode) != int(upgrade_mode):
            continue
        usable, _usable_note = rule.is_usable(
            level=int(character_level),
            job=int(character_job),
            hardcore=bool(hardcore),
        )
        if not usable:
            continue
        priority = (0 if 3 <= slot <= 8 else 1, slot)
        candidates.append((priority, row, rule))
    if not candidates:
        return None
    _priority, row, rule = min(candidates, key=lambda entry: entry[0])
    return row, rule


class UpgradeItem0050Service:
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng if rng is not None else random.SystemRandom()

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(1, 0x0050, build_upgrade_item_error(error_code))
        return [packet], "%s; ACK_fail/%s error=%d" % (note, mode, error_code)

    @staticmethod
    def _inventory_views_for_slots(
        repo: Any,
        character_id: int,
        *,
        list_type: int,
        slots: set[int] | list[int] | tuple[int, ...],
    ) -> list[InventoryItemView]:
        rows = repo.list_inventory_items(int(character_id), list_type=int(list_type))
        by_slot = {int(row["slot_index"]): row for row in rows}
        views: list[InventoryItemView] = []
        for slot in sorted({int(value) for value in slots}):
            row = by_slot.get(slot) or {
                "slot_index": slot,
                "item_template_id": 0xFFFFFFFF,
                "stack_count": 0,
                "instance_value": 0,
                "item_kind": "unknown",
            }
            views.append(InventoryItemView(row))
        return views

    def _handle_guild_medal_upgrade(
        self,
        *,
        parsed: dict[str, Any],
        cid: int,
        repo: Any,
        target: dict[str, Any],
        metadata: Any,
        base: str,
    ) -> tuple[list[bytes], str]:
        material_slot = int(parsed["material_slot"])
        try:
            settlement = settle_guild_medal_upgrade(
                repo,
                rng=self.rng,
                character_id=cid,
                upgrade_mode=int(parsed["mode"]),
                target_slot=int(parsed["target_slot"]),
                target=target,
                metadata=metadata,
                material_slot=material_slot,
            )
        except GuildMedalUpgradeError as exc:
            error_code = {
                "mode": ERROR_WRONG_UPGRADE_MODE,
                "target": ERROR_INVALID_TARGET,
                "restriction": ERROR_RESTRICTION,
                "max_level": ERROR_MAX_LEVEL,
                "material": ERROR_INVALID_MATERIAL,
            }.get(exc.category, ERROR_INVALID_TARGET)
            if exc.category == "transaction":
                error_code = _db_error_code(exc.db_reason)
            return self._error(
                error_code,
                base + "; " + exc.note,
            )

        target_slot = settlement.target_slot
        target_item_id = settlement.target_item_id
        old_level = settlement.old_level
        new_level = settlement.new_level
        result_code = settlement.result_code
        roll = settlement.roll
        rule = settlement.rule
        detail = settlement.detail
        db_note = settlement.db_note

        ack = build_upgrade_item_success(
            mode=0,
            material_slot=material_slot,
            material_remaining=int(detail.get("material_remaining") or 0),
            optional_ticket_slot=int(parsed["optional_ticket_slot"]),
            method=UPGRADE_METHOD_NPC,
            old_level=old_level,
            result_code=result_code,
            new_level=new_level,
            target_list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            target_slot=target_slot,
            destroy_rewards=[],
            gold_slot=0,
            updated_gold=int(detail.get("updated_gold") or 0),
        )
        packets: list[bytes] = []
        ack_packet, ack_mode = self._emit(1, 0x0050, ack)
        packets.append(ack_packet)

        notice_trace = "S0/0056/1 skipped"
        if result_code == 0:
            notice = build_upgrade_item_notice_0056(
                success=True,
                actor_key=cid,
                item_id=target_item_id,
                reinforce_level=new_level,
            )
            notice_packet, notice_mode = self._emit(0, 0x0056, notice)
            packets.append(notice_packet)
            notice_trace = "S0/0056/1 len=%d/%s" % (len(notice), notice_mode)

        target_views = self._inventory_views_for_slots(
            repo,
            cid,
            list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            slots={target_slot},
        )
        target_packet, target_refresh_mode = self._emit(
            0,
            0x000E,
            build_item_list_update_000e(
                target_views,
                list_type=GUILD_MEDAL_INVENTORY_LIST_TYPE,
            ),
        )
        packets.append(target_packet)

        material_slots = {
            int(slot) for slot in detail.get("changed_material_slots", ())
        }
        material_views = self._inventory_views_for_slots(
            repo,
            cid,
            list_type=0,
            slots=material_slots,
        )
        if material_views:
            material_packet, material_refresh_mode = self._emit(
                0,
                0x000E,
                build_item_list_update_000e(material_views, list_type=0),
            )
            packets.append(material_packet)
        else:
            material_refresh_mode = "skip"

        wallet_packet, wallet_mode = self._emit(
            0,
            0x000E,
            build_wallet_gold_update_000e(int(detail.get("updated_gold") or 0)),
        )
        packets.append(wallet_packet)
        old_four_stat = guild_medal_four_stat_bonus(old_level)
        new_four_stat = guild_medal_four_stat_bonus(new_level)
        return packets, (
            "%s; OK source=guild_medal level=%d->%d outcome=%d roll=%d "
            "success_weight=%d contribution=%dx%d remain=%d four_stat=%d->%d "
            "destroyed=%d; ACK/%s len=%d %s target_000E/%s list=36 slot=%d "
            "material_000E/%s list=0 slots=%s 000E_gold/%s; %s"
            % (
                base,
                old_level,
                new_level,
                result_code,
                roll,
                rule.success_weight,
                GUILD_MEDAL_CONTRIBUTION_ITEM_ID,
                rule.contribution_cost,
                int(detail.get("material_remaining") or 0),
                old_four_stat,
                new_four_stat,
                int(detail.get("destroyed") or 0),
                ack_mode,
                len(ack),
                notice_trace,
                target_refresh_mode,
                target_slot,
                material_refresh_mode,
                sorted(material_slots),
                wallet_mode,
                db_note,
            )
        )

    def handle_upgrade_item_0050(
        self, body: bytes, *, session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_upgrade_item_body(body)
        if parsed is None:
            return self._error(
                ERROR_INVALID_TARGET,
                "C1/0050 UPGRADE_ITEM parse_fail plain_len=%d" % len(body or b""),
            )
        base = (
            "C1/0050 UPGRADE_ITEM mode=%d list=%d target=%d item=%d material=%d "
            "ticket=%d trailing_i32=%d name=%r"
            % (
                parsed["mode"], parsed["target_list_type"], parsed["target_slot"],
                parsed["target_item_id"],
                parsed["material_slot"], parsed["optional_ticket_slot"],
                parsed["trailing_i32_unknown"], parsed["target_name"],
            )
        )
        upgrade_mode = int(parsed["mode"])
        if upgrade_mode not in (0, 1):
            return self._error(ERROR_WRONG_UPGRADE_MODE, base + "; reject=mode")
        if int(parsed["optional_ticket_slot"]) >= 0:
            return self._error(
                ERROR_UNSUPPORTED_OPTIONAL_TICKET, base + "; reject=optional_ticket",
            )
        if int(parsed["trailing_i32_unknown"]) != -1:
            return self._error(ERROR_UNSUPPORTED, base + "; reject=special_method")
        target_list_type = int(parsed["target_list_type"])
        if target_list_type not in (0, GUILD_MEDAL_INVENTORY_LIST_TYPE):
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=target_list_type",
            )
        cid = _cid(session, self.server)
        if cid is None:
            return self._error(ERROR_INVALID_TARGET, base + "; reject=no_character")
        repo = self.server._repo()
        eligibility_context = _character_eligibility_context(repo, cid)
        if eligibility_context is None:
            return self._error(
                ERROR_INVALID_TARGET,
                base + "; reject=character_eligibility_missing",
            )
        character_level, character_job = eligibility_context
        hardcore = _session_hardcore(session)
        target = repo.get_inventory_item_at(
            cid,
            list_type=target_list_type,
            slot_index=int(parsed["target_slot"]),
        )
        if target is None:
            return self._error(ERROR_INVALID_TARGET, base + "; reject=target_empty")
        target_item_id = int(target.get("item_template_id") or 0) & 0xFFFFFFFF
        if (
            target_item_id != int(parsed["target_item_id"])
            or str(target.get("item_kind") or "") != "equipment"
        ):
            return self._error(ERROR_INVALID_TARGET, base + "; reject=target_mismatch")
        metadata = resolve_item_metadata(target_item_id)
        if target_list_type == GUILD_MEDAL_INVENTORY_LIST_TYPE:
            if int(target.get("seal_flag") or 0) != 0:
                return self._error(ERROR_RESTRICTION, base + "; reject=sealed")
            return self._handle_guild_medal_upgrade(
                parsed=parsed,
                cid=cid,
                repo=repo,
                target=target,
                metadata=metadata,
                base=base,
            )
        if not metadata.is_equipment or not is_upgrade_target_type(metadata.equipment_type):
            return self._error(
                ERROR_RESTRICTION,
                base + "; reject=equipment_type type=%s" % metadata.equipment_type,
            )
        amplify_type, amplify_value = parse_amplify_state(target.get("extra_json"))
        if upgrade_mode == 1 and (
            amplify_type not in AMPLIFY_TYPES or amplify_value <= 0
        ):
            return self._error(
                ERROR_WRONG_UPGRADE_MODE,
                base + "; reject=missing_amplify_option",
            )
        old_level = (max(0, int(target.get("option_value") or 0)) & 0xFF) & 0x1F

        material = repo.get_inventory_item_at(
            cid, list_type=0, slot_index=int(parsed["material_slot"]),
        )
        material_item_id = 0
        ticket_rule = None
        portable_rule = None
        random_ticket = None
        if material is not None and str(material.get("item_kind") or "") == "stackable":
            material_item_id = int(material.get("item_template_id") or 0) & 0xFFFFFFFF
            random_ticket = find_random_enchant_ticket(material_item_id)
            ticket_rule = find_upgrade_ticket_rule(material_item_id)
            portable_rule = find_portable_upgrade_rule(material_item_id)

        if random_ticket is None and int(target.get("seal_flag") or 0) != 0:
            return self._error(ERROR_RESTRICTION, base + "; reject=sealed")
        ticket_attempt = ticket_rule is not None or random_ticket is not None
        reward_item_id, reward_count, reward_metadata = 0, 0, None
        protection_slot, protection_item_id = -1, 0
        if random_ticket is not None:
            usable, usable_note = random_ticket.is_usable(level=character_level,job=character_job,hardcore=hardcore)
            if not usable:
                return self._error(ERROR_RESTRICTION,base+"; reject=random_ticket_character_restriction reason="+usable_note)
            if upgrade_mode != 0 or (amplify_type in AMPLIFY_TYPES and amplify_value > 0):
                return self._error(ERROR_WRONG_UPGRADE_MODE,base+"; reject=random_ticket_upgrade_mode")
            if not random_ticket.allows_target(metadata,seal_flag=int(target.get('seal_flag') or 0)):
                return self._error(ERROR_RESTRICTION,base+"; reject=random_ticket_target_restriction")
            if int(target.get('equipment_lock_id') or 0):
                return self._error(ERROR_RESTRICTION,base+"; reject=random_ticket_locked_target")
            roll = int(self.rng.randrange(UPGRADE_WEIGHT_SCALE))
            result_code, new_level = 0, random_ticket.level_for_roll(roll)
            material_count, gold_cost, success_weight = 1, 0, UPGRADE_WEIGHT_SCALE
            upgrade_source = 'random_enchant_ticket'
        elif ticket_rule is not None:
            usable, usable_note = ticket_rule.is_usable(
                level=character_level,
                job=character_job,
                hardcore=hardcore,
            )
            if not usable:
                return self._error(
                    ERROR_RESTRICTION,
                    base
                    + "; reject=ticket_character_restriction ticket_item=%d reason=%s"
                    % (ticket_rule.item_id, usable_note),
                )
            expected_ticket_mode = (
                TICKET_MODE_AMPLIFY if upgrade_mode == 1 else TICKET_MODE_REINFORCE
            )
            if ticket_rule.mode != expected_ticket_mode:
                return self._error(
                    ERROR_WRONG_UPGRADE_MODE,
                    base + "; reject=ticket_mode ticket_item=%d ticket_mode=%d"
                    % (ticket_rule.item_id, ticket_rule.mode),
                )
            if ticket_rule.mode == TICKET_MODE_REINFORCE and (
                amplify_type in AMPLIFY_TYPES and amplify_value > 0
            ):
                return self._error(
                    ERROR_WRONG_UPGRADE_MODE,
                    base + "; reject=reinforcement_ticket_on_amplified_equipment",
                )
            if not ticket_rule.allows_target(
                item_id=target_item_id,
                equipment_level=metadata.minimum_level,
            ):
                return self._error(
                    ERROR_RESTRICTION,
                    base + "; reject=ticket_target_restriction ticket_item=%d"
                    % ticket_rule.item_id,
                )
            ticket_success_level = ticket_rule.success_level(old_level)
            if ticket_success_level is None:
                return self._error(
                    ERROR_MAX_LEVEL,
                    base + "; reject=ticket_level_limit ticket_item=%d old_level=%d"
                    % (ticket_rule.item_id, old_level),
                )
            roll = int(self.rng.randrange(UPGRADE_WEIGHT_SCALE))
            if ticket_rule.succeeds_for_roll(roll):
                result_code, new_level = 0, int(ticket_success_level)
            else:
                result_code, new_level = 1, old_level
            material_item_id = ticket_rule.item_id
            material_count = 1
            gold_cost = 0
            success_weight = ticket_rule.success_weight
            upgrade_source = "ticket"
        else:
            if portable_rule is not None:
                usable, usable_note = portable_rule.is_usable(
                    level=character_level,
                    job=character_job,
                    hardcore=hardcore,
                )
                if not usable:
                    return self._error(
                        ERROR_RESTRICTION,
                        base
                        + "; reject=portable_character_restriction item=%d reason=%s"
                        % (portable_rule.item_id, usable_note),
                    )
                if upgrade_mode != portable_rule.mode:
                    return self._error(
                        ERROR_WRONG_UPGRADE_MODE,
                        base
                        + "; reject=portable_upgrade_mode item=%d item_mode=%d"
                        % (portable_rule.item_id, portable_rule.mode),
                    )
                if portable_rule.mode == TICKET_MODE_REINFORCE and (
                    amplify_type in AMPLIFY_TYPES and amplify_value > 0
                ):
                    return self._error(
                        ERROR_WRONG_UPGRADE_MODE,
                        base + "; reject=portable_reinforce_on_amplified_equipment",
                    )
                if not portable_rule.allows_target(
                    current_level=old_level,
                    equipment_level=metadata.minimum_level,
                    item_id=target_item_id,
                ):
                    return self._error(
                        ERROR_RESTRICTION,
                        base
                        + "; reject=portable_upgrade_restriction item=%d "
                        "current=%d equipment_level=%d"
                        % (
                            portable_rule.item_id,
                            old_level,
                            metadata.minimum_level,
                        ),
                    )
            config = (
                get_amplify_upgrade_config()
                if upgrade_mode == 1
                else get_upgrade_config()
            )
            if old_level >= config.max_level(metadata.rarity):
                return self._error(ERROR_MAX_LEVEL, base + "; reject=max_level")
            table_row = config.row_for_target_level(old_level + 1)
            if table_row is None:
                return self._error(ERROR_MAX_LEVEL, base + "; reject=table_end")
            if portable_rule is not None:
                gold_cost = 0
            else:
                gold_cost = config.gold_cost(
                    minimum_level=metadata.minimum_level, rarity=metadata.rarity,
                    equipment_type=metadata.equipment_type, current_level=old_level,
                )
                if gold_cost is None:
                    return self._error(ERROR_UNSUPPORTED, base + "; reject=gold_table")
            policy = table_row.policy(is_weapon=is_weapon_type(metadata.equipment_type))
            success_weight = (
                portable_rule.adjusted_success_weight(policy.success_weight)
                if portable_rule is not None
                else int(policy.success_weight)
            )
            roll = int(self.rng.randrange(UPGRADE_WEIGHT_SCALE))
            if roll < success_weight:
                result_code, new_level = 0, old_level + 1
            elif policy.penalty_type == 1:
                result_code, new_level = 1, old_level
            elif policy.penalty_type == 2:
                new_level = max(0, old_level - max(0, int(policy.penalty_value)))
                result_code = 2 if new_level < old_level else 1
            else:
                result_code, new_level = 3, 0
                selected_protection = _select_upgrade_protection_ticket(
                    repo.list_inventory_items(cid, list_type=0),
                    upgrade_mode=upgrade_mode,
                    excluded_slots=(
                        int(parsed["target_slot"]),
                        int(parsed["material_slot"]),
                    ),
                    character_level=character_level,
                    character_job=character_job,
                    hardcore=hardcore,
                )
                if selected_protection is not None:
                    protection_row, protection_rule = selected_protection
                    protected_level = int(protection_rule.failure_level)
                    if protected_level < old_level:
                        result_code, new_level = 2, protected_level
                        protection_slot = int(protection_row["slot_index"])
                        protection_item_id = int(protection_rule.item_id)
                if result_code == 3:
                    reward_item_id = int(config.destroy.bonus_item_id)
                    reward_count = config.destroy.reward_count(
                        current_level=old_level,
                        equipment_grade=int(metadata.grade or metadata.minimum_level),
                        rarity=metadata.rarity,
                    )
                    if reward_count > 0:
                        reward_metadata = resolve_item_metadata(reward_item_id)
            if portable_rule is not None:
                material_item_id = int(portable_rule.item_id)
                material_count = 1
                portable_name = (
                    "portable_amplify"
                    if portable_rule.mode == TICKET_MODE_AMPLIFY
                    else "portable_reinforce"
                )
                upgrade_source = (
                    portable_name + "_protected"
                    if protection_slot >= 0
                    else portable_name
                )
            else:
                material_item_id = int(table_row.material_item_id)
                material_count = int(table_row.material_count)
                upgrade_source = "npc_protected" if protection_slot >= 0 else "npc"
        apply_args = {
            "character_id": cid,
            "target_list_type": 0,
            "target_slot": int(parsed["target_slot"]),
            "expected_item_id": target_item_id,
            "expected_old_level": old_level,
            "material_slot": int(parsed["material_slot"]),
            "material_item_id": material_item_id,
            "material_count": material_count,
            "gold_cost": int(gold_cost),
            "result_code": result_code,
            "new_level": new_level,
            "ticket_attempt": ticket_attempt,
            "protection_slot": protection_slot,
            "protection_item_id": protection_item_id,
            "reward_item_id": reward_item_id,
            "reward_count": reward_count,
            "reward_metadata": reward_metadata,
        }
        if upgrade_mode == 1:
            ok, db_note, detail = repo.apply_amplified_equipment_upgrade(
                expected_amplify_type=amplify_type,
                expected_amplify_value=amplify_value,
                **apply_args,
            )
        else:
            if random_ticket is not None:
                apply_args['random_ticket_attempt'] = True
            ok, db_note, detail = repo.apply_normal_equipment_upgrade(**apply_args)
        if not ok:
            return self._error(
                _db_error_code(db_note), base + "; transaction_reject=%s" % db_note,
            )

        rewards = list(detail.get("reward_entries") or ())
        response_method = (
            UPGRADE_METHOD_TICKET if ticket_attempt else UPGRADE_METHOD_NPC
        )
        ack = build_upgrade_item_success(
            mode=upgrade_mode, material_slot=int(parsed["material_slot"]),
            material_remaining=int(detail.get("material_remaining") or 0),
            optional_ticket_slot=int(parsed["optional_ticket_slot"]),
            method=response_method,
            old_level=old_level, result_code=result_code, new_level=new_level,
            target_list_type=0, target_slot=int(parsed["target_slot"]),
            destroy_rewards=rewards, gold_slot=0,
            updated_gold=int(detail.get("updated_gold") or 0),
        )
        packets: list[bytes] = []
        packet_ack, ack_mode = self._emit(1, 0x0050, ack)
        packets.append(packet_ack)
        notice_trace = "S0/0056/1 skipped"
        if result_code == 0:
            notice = build_upgrade_item_notice_0056(
                success=True,
                actor_key=cid,
                item_id=target_item_id,
                reinforce_level=new_level,
            )
            notice_packet, notice_mode = self._emit(0, 0x0056, notice)
            packets.append(notice_packet)
            notice_trace = "S0/0056/1 len=%d/%s" % (len(notice), notice_mode)
        rows = repo.list_inventory_items(cid, list_type=0)
        by_slot = {int(row["slot_index"]): row for row in rows}
        refresh_slots = {
            int(slot) for slot in detail.get("changed_slots", ()) if int(slot) != 0
        }
        refresh_items: list[InventoryItemView] = []
        for slot in sorted(refresh_slots):
            row = by_slot.get(slot) or {
                "slot_index": slot, "item_template_id": 0xFFFFFFFF,
                "stack_count": 0, "instance_value": 0, "item_kind": "unknown",
            }
            refresh_items.append(InventoryItemView(row))
        if refresh_items:
            refresh_packet, refresh_mode = self._emit(
                0, 0x000E, build_item_list_update_000e(refresh_items, list_type=0),
            )
            packets.append(refresh_packet)
        else:
            refresh_mode = "skip"
        wallet_packet, wallet_mode = self._emit(
            0, 0x000E,
            build_wallet_gold_update_000e(int(detail.get("updated_gold") or 0)),
        )
        packets.append(wallet_packet)
        return packets, (
            "%s; OK source=%s method=%d level=%d->%d outcome=%d roll=%d "
            "success_weight=%d "
            "material=%dx%d remain=%d protection=%d@%d remain=%d "
            "gold_cost=%d gold=%d rewards=%d; "
            "ACK/%s len=%d %s 000E_items/%s slots=%s 000E_gold/%s; %s"
            % (
                base, upgrade_source, response_method, old_level, new_level,
                result_code, roll, success_weight, material_item_id, material_count,
                int(detail.get("material_remaining") or 0), protection_item_id,
                protection_slot, int(detail.get("protection_remaining") or 0), gold_cost,
                int(detail.get("updated_gold") or 0), len(rewards), ack_mode, len(ack),
                notice_trace, refresh_mode, sorted(refresh_slots), wallet_mode, db_note,
            )
        )


__all__ = [
    "UpgradeItem0050Service", "build_upgrade_item_error",
    "build_upgrade_item_notice_0056", "build_upgrade_item_success",
    "parse_upgrade_item_body",
]
