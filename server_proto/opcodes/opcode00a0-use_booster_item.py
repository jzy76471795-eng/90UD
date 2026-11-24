# -*- coding: utf-8 -*-
"""C1/00A0 Stage3 direct and selection booster open flow."""
from __future__ import annotations

import struct
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from game.chain_loader import load_chain
from game.character.character_db import InventoryGrantSpec
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    ItemMetadata,
    resolve_item_metadata,
)
from game.character.payloads import (
    build_item_list_mode0,
    build_item_list_update_000e,
)
from game.shop.booster_item_index import (
    DIRECT_BOOSTER_TYPE_PREFIXES,
    BoosterItemDefinition,
    resolve_booster_item,
    resolve_booster_selection_item,
)
from game.shop.booster_selection_index import (
    BoosterSelectionDefinition,
    BoosterSelectionReward,
    SELECTION_BOOSTER_TYPE_PREFIX,
    is_deliverable_selection_reward,
)
from game.shop.cerashop_index import resolve_purchase_expire_time


build_booster_reward_notice = load_chain(
    "opcode0207-open_cera_package.py"
).build_cera_package_reward_notice
build_system_reward_activation_packets = load_chain(
    "helper-system_reward_activation.py"
).build_system_reward_activation_packets


@dataclass(frozen=True)
class UseBoosterItemRequest:
    source_slot: int


@dataclass(frozen=True)
class BoosterAvatarChoice:
    item_id: int
    option_value: int


@dataclass(frozen=True)
class UseBoosterSelectionRequest:
    source_slot: int
    category_a: int
    category_b: int
    selected_item_ids: tuple[int, ...]
    avatar_choices: tuple[BoosterAvatarChoice, ...]


def parse_use_booster_item_request(body: bytes) -> UseBoosterItemRequest | None:
    """Parse the direct ``[booster]`` Stage3 writer: ``u16 source_slot``."""
    raw = bytes(body or b"")
    # Codec 6 can retain padding from the first eight-byte cipher block.
    if len(raw) not in (2, 8) or any(raw[2:]):
        return None
    return UseBoosterItemRequest(source_slot=int(struct.unpack_from("<H", raw)[0]))


def build_use_booster_item_failure(error_code: int = 4) -> bytes:
    """Build the result=0 branch consumed by Stage3 reader VA 0x01627450."""
    return struct.pack("<BB", 0, int(error_code) & 0xFF)


def _match_selection_rewards(
    definition: BoosterSelectionDefinition,
    request: UseBoosterSelectionRequest,
) -> tuple[list[tuple[BoosterSelectionReward, int]] | None, str]:
    category = definition.resolve_category(
        request.category_a,
        request.category_b,
    )
    if category is None:
        return None, "category_missing=%d/%d" % (
            request.category_a,
            request.category_b,
        )

    reward_pool = list(category.rewards)
    def take_reward(
        pool: list[BoosterSelectionReward],
        item_id: int,
    ) -> BoosterSelectionReward | None:
        target = int(item_id) & 0xFFFFFFFF
        for index, reward in enumerate(pool):
            if (int(reward.item_id) & 0xFFFFFFFF) == target:
                return pool.pop(index)
        return None

    selected: list[tuple[BoosterSelectionReward, int]] = []
    if definition.selection_count > 0:
        actual_count = len(request.selected_item_ids)
        if actual_count != int(definition.selection_count):
            return None, "selection_count=%d expect=%d" % (
                actual_count,
                definition.selection_count,
            )
        avatar_choices = list(request.avatar_choices)
        for item_id in request.selected_item_ids:
            reward = take_reward(reward_pool, item_id)
            if reward is None:
                return None, "selected_choice_not_in_category item=%d" % item_id
            if not reward.wire_avatar:
                selected.append((reward, 0))
                continue
            matched_index = next(
                (
                    index
                    for index, choice in enumerate(avatar_choices)
                    if int(choice.item_id) == int(reward.item_id)
                ),
                None,
            )
            if matched_index is None:
                default_option = int(reward.avatar_default_option)
                if 0 <= default_option <= 0xFF:
                    selected.append((reward, default_option))
                    continue
                return None, "selected_avatar_descriptor_missing item=%d" % item_id
            choice = avatar_choices.pop(matched_index)
            selected.append((reward, int(choice.option_value) & 0xFF))
        if avatar_choices:
            return None, "selected_avatar_descriptor_unmatched=%d" % len(
                avatar_choices
            )
        return selected, "ok"

    expected_item_ids = tuple(
        int(reward.item_id) & 0xFFFFFFFF for reward in category.rewards
    )
    if request.selected_item_ids and sorted(request.selected_item_ids) != sorted(
        expected_item_ids
    ):
        return None, "automatic_selected_items_mismatch"

    choices = list(request.avatar_choices)
    for reward in category.rewards:
        if not reward.wire_avatar:
            selected.append((reward, 0))
            continue
        matched_index = next(
            (
                index
                for index, choice in enumerate(choices)
                if int(choice.item_id) == int(reward.item_id)
            ),
            None,
        )
        if matched_index is None:
            default_option = int(reward.avatar_default_option)
            if 0 <= default_option <= 0xFF:
                selected.append((reward, default_option))
                continue
            return None, "automatic_avatar_missing item=%d" % reward.item_id
        choice = choices.pop(matched_index)
        selected.append((reward, int(choice.option_value) & 0xFF))
    if choices:
        return None, "automatic_avatar_unmatched=%d" % len(choices)
    return selected, "ok"


def parse_use_booster_selection_request(
    body: bytes,
    definition: BoosterSelectionDefinition,
) -> tuple[UseBoosterSelectionRequest | None, str]:
    """Parse the Stage3 writer at ``sub_19A8880/sub_19AABC0``.

    Wire layout is ``u16 slot + u8 category_a + u8 category_b``, followed by
    selected ``u32 item_id`` values, then ``u8 avatar_count`` and
    ``avatar_count * {u32 item_id, u8 option}``.  Explicit-selection packages
    carry ``selection_count`` ids. Automatic packages may omit the id prefix
    or repeat the complete PVF category reward list before the descriptors.
    """
    raw = bytes(body or b"")
    if len(raw) < 5:
        return None, "selection_request_too_short=%d" % len(raw)
    source_slot = int(struct.unpack_from("<H", raw, 0)[0])
    category_a = int(raw[2])
    category_b = int(raw[3])
    category = definition.resolve_category(category_a, category_b)
    if category is None:
        return None, "category_missing=%d/%d" % (category_a, category_b)

    from game.shop.booster_selection_index import artifact_avatar_preview_id
    preview_items = {}
    for reward in category.rewards:
        if reward.wire_avatar:
            preview = artifact_avatar_preview_id(definition.source_item_id, reward.item_id)
            if preview is not None:
                preview_items[preview] = reward.item_id

    configured_count = max(0, int(definition.selection_count))
    selected_count_candidates = (
        (configured_count,)
        if configured_count > 0
        else tuple(dict.fromkeys((0, len(category.rewards))))
    )
    candidates: list[UseBoosterSelectionRequest] = []
    candidate_errors: list[str] = []
    for selected_count in selected_count_candidates:
        avatar_count_offset = 4 + selected_count * 4
        if avatar_count_offset >= len(raw):
            continue
        avatar_count = int(raw[avatar_count_offset])
        expected_length = avatar_count_offset + 1 + avatar_count * 5
        if expected_length != len(raw):
            continue

        selected_ids = tuple(
            int(struct.unpack_from("<I", raw, 4 + index * 4)[0])
            for index in range(selected_count)
        )
        choices: list[BoosterAvatarChoice] = []
        selected_ids = tuple(preview_items.get(item, item) for item in selected_ids)
        cursor = avatar_count_offset + 1
        for _index in range(avatar_count):
            item_id = int(struct.unpack_from("<I", raw, cursor)[0])
            option = int(raw[cursor + 4])
            choices.append(
                BoosterAvatarChoice(
                    item_id=preview_items.get(item_id, item_id),
                    option_value=option,
                )
            )
            cursor += 5
        request = UseBoosterSelectionRequest(
            source_slot=source_slot,
            category_a=category_a,
            category_b=category_b,
            selected_item_ids=selected_ids,
            avatar_choices=tuple(choices),
        )
        matched, match_note = _match_selection_rewards(definition, request)
        if matched is None:
            candidate_errors.append(match_note)
            continue
        candidates.append(request)

    if len(candidates) == 1:
        return candidates[0], "ok"
    if len(candidates) > 1:
        return None, "selection_request_ambiguous=%d" % len(candidates)
    if candidate_errors:
        return None, "selection_choices_invalid:%s" % candidate_errors[0]
    return None, "selection_request_shape plain_len=%d" % len(raw)


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return None


def build_direct_booster_grants(
    definition: BoosterItemDefinition,
    *,
    now_unix: int,
    randbelow: Callable[[int], int] | None = None,
) -> tuple[list[InventoryGrantSpec] | None, str]:
    try:
        rolled = (
            definition.roll()
            if randbelow is None
            else definition.roll(randbelow=randbelow)
        )
    except Exception as exc:
        return None, "reward_roll_error=%s:%s" % (type(exc).__name__, exc)
    if not rolled:
        return None, "reward_roll_empty"

    aggregate: "OrderedDict[tuple[int, int, int], tuple[int, ItemMetadata]]" = (
        OrderedDict()
    )
    for candidate in rolled:
        item_id = int(candidate.item_id) & 0xFFFFFFFF
        metadata = resolve_item_metadata(item_id)
        if metadata.item_kind not in {"equipment", "stackable"} or not str(
            metadata.note or ""
        ).startswith("ok"):
            return None, (
                "reward_metadata_unavailable item=%d kind=%s note=%s"
                % (item_id, metadata.item_kind, metadata.note)
            )
        option = int(candidate.option_value) & 0xFF if metadata.is_avatar else 0
        expire = resolve_purchase_expire_time(metadata, now_unix=now_unix)
        key = (item_id, option, expire)
        previous_count, _previous_metadata = aggregate.get(key, (0, metadata))
        final_count = int(previous_count) + int(candidate.count)
        if final_count <= 0 or final_count > 0xFFFFFFFF:
            return None, "reward_count_overflow item=%d count=%d" % (
                item_id,
                final_count,
            )
        aggregate[key] = (final_count, metadata)

    grants = [
        InventoryGrantSpec(
            item_id=item_id,
            count=count,
            metadata=metadata,
            option_value=option,
            expire_time=expire,
        )
        for (item_id, option, expire), (count, metadata) in aggregate.items()
    ]
    return grants, "ok"


def build_selection_booster_grants(
    definition: BoosterSelectionDefinition,
    request: UseBoosterSelectionRequest,
    *,
    now_unix: int,
) -> tuple[list[InventoryGrantSpec] | None, str]:
    selected, selection_note = _match_selection_rewards(definition, request)
    if selected is None:
        return None, selection_note
    if not selected:
        return None, "selection_rewards_empty"

    aggregate: "OrderedDict[tuple[int, int, int], tuple[int, ItemMetadata]]" = (
        OrderedDict()
    )
    filtered_invalid = 0
    for reward, selected_option in selected:
        if not is_deliverable_selection_reward(reward):
            filtered_invalid += 1
            continue
        item_id = int(reward.item_id) & 0xFFFFFFFF
        metadata = resolve_item_metadata(item_id)
        metadata_ok = bool(
            metadata is not None
            and metadata.item_kind in {"equipment", "stackable"}
            and str(metadata.note or "").startswith("ok")
        )
        if not metadata_ok:
            child_name = " ".join(
                str(reward.child_name or "").strip().lower().split()
            )
            if child_name == "avatar":
                metadata = ItemMetadata(
                    item_id=item_id,
                    item_kind="equipment",
                    stack_limit=1,
                    equipment_type="[avatar]",
                    is_avatar=True,
                    note="ok:selection_reward_section:avatar",
                )
            elif child_name == "equipment":
                metadata = ItemMetadata(
                    item_id=item_id,
                    item_kind="equipment",
                    stack_limit=1,
                    note="ok:selection_reward_section:equipment",
                )
            elif child_name in {"stackable", "etc"}:
                metadata = ItemMetadata(
                    item_id=item_id,
                    item_kind="stackable",
                    stackable_type="[%s]" % child_name,
                    stack_limit=1000,
                    note="ok:selection_reward_section:%s" % child_name,
                )
            else:
                return None, (
                    "reward_metadata_unavailable item=%d child=%s kind=%s note=%s"
                    % (
                        item_id,
                        child_name,
                        getattr(metadata, "item_kind", "missing"),
                        getattr(metadata, "note", "missing"),
                    )
                )
        # Non-avatar child tags group candidates but do not reliably encode the
        # registered reward type. The authoritative metadata decides its
        # container; only avatar rows require the dedicated wire descriptor.
        if reward.wire_avatar and not metadata.is_avatar:
            filtered_invalid += 1
            continue
        # The weapon box offers one fixed attribute per native reward category.
        # Old clients still display the generic avatar confirm, whose disabled
        # attribute field writes 0xFF. The previously chosen category is authoritative.
        if (definition.source_item_id == 491000014 and definition.category_dimensions == (4,)
                and item_id == 2026072401 and reward.wire_avatar
                and reward.avatar_option_mode == 0):
            category_option = int(reward.avatar_default_option)
            if (request.category_b != 0 or category_option != request.category_a
                    or category_option not in range(4)
                    or selected_option not in (category_option, 0xFF)):
                return None, "weapon_clone_category_option_mismatch"
            selected_option = category_option
        elif (item_id in (2026072401, 2026072901) and reward.wire_avatar
                and reward.avatar_option_mode == 0 and reward.avatar_default_option == 0):
            if selected_option not in (0, 1, 2, 3, 0xFF):
                return None, "invalid_clone_avatar_attribute_option"
            selected_option = 0
        if item_id in (2026072401, 2026072901) and reward.wire_avatar and selected_option not in range(4):
            return None, "invalid_clone_avatar_attribute_option"
        option = (
            int(selected_option) & 0xFF
            if reward.wire_avatar and metadata.is_avatar
            else 0
        )
        expire = resolve_purchase_expire_time(metadata, now_unix=now_unix)
        key = (item_id, option, expire)
        previous_count, _previous_metadata = aggregate.get(key, (0, metadata))
        final_count = int(previous_count) + int(reward.count)
        if final_count <= 0 or final_count > 0xFFFFFFFF:
            filtered_invalid += 1
            continue
        aggregate[key] = (final_count, metadata)

    if not aggregate:
        return None, "selection_rewards_all_invalid filtered=%d" % filtered_invalid
    grants = [
        InventoryGrantSpec(
            item_id=item_id,
            count=count,
            metadata=metadata,
            option_value=option,
            expire_time=expire,
        )
        for (item_id, option, expire), (count, metadata) in aggregate.items()
    ]
    if filtered_invalid:
        return grants, "ok filtered_invalid=%d" % filtered_invalid
    return grants, "ok"


class UseBoosterItem00A0Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        packet = self._emit(1, 0x00A0, build_use_booster_item_failure())
        return [packet], "C1/00A0 USE_BOOSTER_ITEM FAIL %s" % note

    def handle_use_booster_item_00a0(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        raw_body = bytes(body or b"")
        if len(raw_body) < 2:
            return self._failure("source_slot_missing plain_len=%d" % len(raw_body))
        source_slot = int(struct.unpack_from("<H", raw_body, 0)[0])

        character_id = _selected_character_id(session)
        if character_id is None:
            return self._failure("selected_character_missing")
        try:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        except (TypeError, ValueError):
            account_id = 1

        repo = self.server._repo()
        source = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=source_slot,
        )
        if source is None:
            return self._failure("source_slot_empty=%d" % source_slot)
        source_item_id = int(source.get("item_template_id", 0) or 0) & 0xFFFFFFFF
        direct_definition = resolve_booster_item(source_item_id)
        selection_definition = (
            None
            if direct_definition is not None
            else resolve_booster_selection_item(source_item_id)
        )
        if direct_definition is None and selection_definition is None:
            return self._failure(
                "booster_definition_missing item=%d" % source_item_id
            )

        now_unix = int(time.time())
        request: UseBoosterItemRequest | UseBoosterSelectionRequest
        booster_kind: str
        allowed_source_types: tuple[str, ...]
        if direct_definition is not None:
            direct_request = parse_use_booster_item_request(raw_body)
            if direct_request is None:
                return self._failure(
                    "malformed_direct_request item=%d plain_len=%d"
                    % (source_item_id, len(raw_body))
                )
            request = direct_request
            grants, grant_note = build_direct_booster_grants(
                direct_definition,
                now_unix=now_unix,
            )
            booster_kind = "direct"
            allowed_source_types = DIRECT_BOOSTER_TYPE_PREFIXES
        else:
            assert selection_definition is not None
            selection_request, parse_note = parse_use_booster_selection_request(
                raw_body,
                selection_definition,
            )
            if selection_request is None:
                return self._failure(
                    "malformed_selection_request item=%d %s"
                    % (source_item_id, parse_note)
                )
            request = selection_request
            grants, grant_note = build_selection_booster_grants(
                selection_definition,
                selection_request,
                now_unix=now_unix,
            )
            booster_kind = "selection"
            allowed_source_types = (SELECTION_BOOSTER_TYPE_PREFIX,)
        if grants is None:
            return self._failure("item=%d %s" % (source_item_id, grant_note))
        try:
            reward_notice = build_booster_reward_notice(
                request.source_slot,
                grants,
            )
        except ValueError as exc:
            return self._failure(
                "item=%d notice_error=%s" % (source_item_id, exc)
            )

        source_metadata = resolve_item_metadata(source_item_id)
        ok, settlement_note, detail = repo.open_cera_package(
            account_id=account_id,
            character_id=character_id,
            source_slot=request.source_slot,
            expected_package_item_id=source_item_id,
            grants=grants,
            source_metadata=source_metadata,
            allowed_source_types=allowed_source_types,
            now_unix=now_unix,
        )
        if not ok:
            return self._failure(
                "item=%d slot=%d %s"
                % (source_item_id, request.source_slot, settlement_note)
            )

        reward_packet = self._emit(1, 0x00A0, reward_notice)
        note = (
            "C1/00A0 USE_BOOSTER_ITEM OK kind=%s item=%d slot=%d rewards=%d "
            "source_remaining=%d lists=%s"
            % (
                booster_kind,
                source_item_id,
                request.source_slot,
                len(grants),
                int(detail.get("source_remaining", 0) or 0),
                list(detail.get("changed_list_types", ()) or ()),
            )
        )
        if grant_note != "ok":
            note += " %s" % grant_note

        try:
            activation_packets, activation_notes = (
                build_system_reward_activation_packets(
                    self.server,
                    repo,
                    detail=detail,
                    account_id=account_id,
                    character_id=character_id,
                    session=session,
                )
            )
            if activation_notes:
                note += " auto_activation=%s" % list(activation_notes)
        except Exception as exc:
            activation_packets = []
            note += " auto_activation_notice_error=%s:%s" % (
                type(exc).__name__,
                exc,
            )

        refresh_packets: list[tuple[bytes, str]] = []
        try:
            changed_list_types = sorted(
                {
                    int(value)
                    for value in list(detail.get("changed_list_types", ()) or ())
                    if int(value) in (0, 1, 7, GUILD_MEDAL_INVENTORY_LIST_TYPE)
                }
            )
            for list_type in changed_list_types:
                changed_slots = {
                    int(value) & 0xFFFF
                    for value in list(
                        dict(detail.get("changed_slots", {}) or {}).get(
                            list_type,
                            (),
                        )
                        or ()
                    )
                }
                rows = repo.list_inventory_items(
                    character_id,
                    list_type=list_type,
                )
                by_slot = {
                    int(row.get("slot_index", 0) or 0) & 0xFFFF: row
                    for row in rows
                    if (int(row.get("slot_index", 0) or 0) & 0xFFFF)
                    in changed_slots
                }
                update_items: list[InventoryItemView] = []
                missing_slots: list[int] = []
                for changed_slot in sorted(changed_slots):
                    row = by_slot.get(changed_slot)
                    if row is None:
                        if (
                            list_type == 0
                            and changed_slot == request.source_slot
                            and int(detail.get("source_remaining", 0) or 0) <= 0
                        ):
                            row = {
                                "slot_index": changed_slot,
                                "item_template_id": -1,
                                "stack_count": 0,
                                "instance_value": 0,
                            }
                        else:
                            missing_slots.append(changed_slot)
                            continue
                    update_items.append(InventoryItemView(row))
                if missing_slots:
                    note += " refresh_mismatch list=%d slots=%s" % (
                        list_type,
                        missing_slots,
                    )
                if not update_items:
                    continue
                update = build_item_list_update_000e(
                    update_items,
                    list_type=list_type,
                    avatar=(list_type == 1),
                    now_unix=now_unix,
                )
                refresh_packets.append(
                    (
                        self._emit(0, 0x000E, update),
                        "BOOSTER_ITEM_REFRESH list=%d slots=%s"
                        % (list_type, sorted(changed_slots)),
                    )
                )
        except Exception as exc:
            note += " refresh_error=%s:%s" % (type(exc).__name__, exc)
            return [reward_packet] + activation_packets, note

        immediate_packets = [packet for packet, _refresh_note in refresh_packets]
        note += " refresh_immediate=%d" % len(immediate_packets)

        # Stage3 acknowledges each 000E delta but does not consistently rebuild
        # the town consumable grid after a booster replaces its own source slot.
        # Publish the committed Main snapshot last so the consumed package and
        # every Main reward converge before the next inventory action.
        try:
            main_rows = repo.list_inventory_items(character_id, list_type=0)
            main_items = [InventoryItemView(row) for row in main_rows]
            list_param16 = int(
                repo.get_container_list_param(character_id, list_type=0) or 0
            )
            main_body = build_item_list_mode0(
                main_items,
                list_type=0,
                list_param16=list_param16,
            )
            main_packet = self._emit(0, 0x000D, main_body)
        except Exception as exc:
            note += " main_full_refresh_error=%s:%s" % (
                type(exc).__name__,
                exc,
            )
            return [reward_packet] + activation_packets + immediate_packets, note

        note += " main_full_refresh=1 entries=%d" % len(main_items)
        return (
            [reward_packet]
            + activation_packets
            + immediate_packets
            + [main_packet],
            note,
        )


__all__ = [
    "UseBoosterItem00A0Service",
    "UseBoosterItemRequest",
    "UseBoosterSelectionRequest",
    "BoosterAvatarChoice",
    "build_direct_booster_grants",
    "build_selection_booster_grants",
    "build_use_booster_item_failure",
    "parse_use_booster_item_request",
    "parse_use_booster_selection_request",
]
