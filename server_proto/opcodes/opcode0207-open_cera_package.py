# -*- coding: utf-8 -*-
"""C1/0207 Stage3 Cera avatar-package open flow."""
from __future__ import annotations

import struct
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain
from game.character.character_db import InventoryGrantSpec
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.character.payloads import build_item_list_update_000e
from game.shop.cera_package_index import (
    CeraPackageDefinition,
    resolve_cera_package,
)
from game.shop.cerashop_index import resolve_purchase_expire_time


_CeraPackageRefreshFlow = load_chain("flow-cera_package_refresh.py")
_SystemRewardActivation = load_chain("helper-system_reward_activation.py")
arm_cera_package_refresh = _CeraPackageRefreshFlow.arm_cera_package_refresh
is_cera_package_reward_notice_confirmed = (
    _CeraPackageRefreshFlow.is_cera_package_reward_notice_confirmed
)
build_system_reward_activation_packets = (
    _SystemRewardActivation.build_system_reward_activation_packets
)


@dataclass(frozen=True)
class AvatarPackageChoice:
    item_id: int
    option_value: int


@dataclass(frozen=True)
class OpenCeraPackageRequest:
    source_slot: int
    choices: tuple[AvatarPackageChoice, ...]


def parse_open_cera_package_request(body: bytes) -> OpenCeraPackageRequest | None:
    """Parse the frozen Stage3 writer at VA 0x017AF4E0.

    Layout: ``u16 slot + u8 count + count * (u32 avatar_id + u8 option)``.
    """
    raw = bytes(body or b"")
    if len(raw) < 3:
        return None
    source_slot, choice_count = struct.unpack_from("<HB", raw, 0)
    if len(raw) != 3 + int(choice_count) * 5:
        return None
    choices: list[AvatarPackageChoice] = []
    offset = 3
    for _ in range(int(choice_count)):
        item_id, option_value = struct.unpack_from("<IB", raw, offset)
        if item_id <= 0:
            return None
        choices.append(
            AvatarPackageChoice(
                item_id=int(item_id),
                option_value=int(option_value),
            )
        )
        offset += 5
    return OpenCeraPackageRequest(
        source_slot=int(source_slot),
        choices=tuple(choices),
    )


def build_open_cera_package_success(source_slot: int) -> bytes:
    """Build the success branch consumed by Stage3 reader VA 0x017AECE0."""
    return struct.pack("<BH", 1, int(source_slot) & 0xFFFF)


def build_open_cera_package_failure(error_code: int = 4) -> bytes:
    """Build the same reader's result=0/error-code branch."""
    return struct.pack("<BB", 0, int(error_code) & 0xFF)


def build_cera_package_reward_notice(
    source_slot: int,
    grants: list[InventoryGrantSpec] | tuple[InventoryGrantSpec, ...],
) -> bytes:
    """Build S1/00A0 for the frozen Stage3 reader at VA 0x01627450.

    The Stage3 success branch consumes exactly one reserved dword before the
    reward count. Older reference clients use two dwords here and are not wire
    compatible with this reader.
    """
    rows = list(grants)
    if len(rows) > 0xFFFF:
        raise ValueError("Cera package reward notice exceeds u16 count")
    body = bytearray(
        struct.pack(
            "<BHIH",
            1,
            int(source_slot) & 0xFFFF,
            0,
            len(rows),
        )
    )
    for grant in rows:
        item_id = int(grant.item_id) & 0xFFFFFFFF
        count = int(grant.count)
        if item_id <= 0 or count <= 0 or count > 0xFFFFFFFF:
            raise ValueError(
                "invalid Cera package reward notice item=%d count=%d"
                % (item_id, count)
            )
        body.extend(struct.pack("<II", item_id, count & 0xFFFFFFFF))
    return bytes(body)


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid > 0:
            return cid
    return None


def _build_package_grants(
    definition: CeraPackageDefinition,
    choices: tuple[AvatarPackageChoice, ...],
    *,
    now_unix: int,
) -> tuple[list[InventoryGrantSpec] | None, str]:
    metadata_by_item: dict[int, ItemMetadata] = {}
    deliverable_rewards: list[tuple[Any, ItemMetadata]] = []
    expected_avatar_counts: Counter[int] = Counter()
    expected_avatar_units = 0
    filtered_reward_counts: Counter[int] = Counter()
    filtered_invalid = 0

    for reward in definition.rewards:
        item_id = int(reward.item_id) & 0xFFFFFFFF
        metadata = metadata_by_item.get(item_id)
        if metadata is None:
            metadata = resolve_item_metadata(item_id)
            metadata_by_item[item_id] = metadata
        metadata_ok = bool(
            metadata is not None
            and metadata.item_kind in {"equipment", "stackable"}
            and str(metadata.note or "").startswith("ok")
        )
        if not metadata_ok:
            filtered_invalid += 1
            filtered_reward_counts[item_id] += int(reward.count)
            continue
        deliverable_rewards.append((reward, metadata))
        if bool(metadata.is_avatar):
            expected_avatar_units += int(reward.count)
            if expected_avatar_units > 0xFF:
                return None, "avatar_choice_count_overflow=%d" % expected_avatar_units
            expected_avatar_counts[item_id] += int(reward.count)

    if not deliverable_rewards:
        return None, "package_rewards_all_invalid filtered=%d" % filtered_invalid

    actual_avatar_counts: Counter[int] = Counter()
    ignored_filtered_choices: Counter[int] = Counter()
    unexpected_choices: Counter[int] = Counter()
    for choice in choices:
        item_id = int(choice.item_id) & 0xFFFFFFFF
        if item_id in expected_avatar_counts:
            actual_avatar_counts[item_id] += 1
        elif item_id in filtered_reward_counts:
            ignored_filtered_choices[item_id] += 1
        else:
            unexpected_choices[item_id] += 1
    filtered_choice_overflow = {
        item_id: count
        for item_id, count in ignored_filtered_choices.items()
        if count > filtered_reward_counts[item_id]
    }
    if (
        actual_avatar_counts != expected_avatar_counts
        or unexpected_choices
        or filtered_choice_overflow
    ):
        return None, (
            "avatar_choice_mismatch expected=%s actual=%s unexpected=%s "
            "filtered_overflow=%s"
        ) % (
            sorted(expected_avatar_counts.items()),
            sorted(actual_avatar_counts.items()),
            sorted(unexpected_choices.items()),
            sorted(filtered_choice_overflow.items()),
        )

    option_queues: dict[int, deque[int]] = defaultdict(deque)
    for choice in choices:
        item_id = int(choice.item_id) & 0xFFFFFFFF
        if item_id in expected_avatar_counts:
            option_queues[item_id].append(int(choice.option_value) & 0xFF)

    grants: list[InventoryGrantSpec] = []
    for reward, metadata in deliverable_rewards:
        item_id = int(reward.item_id) & 0xFFFFFFFF
        expire_time = resolve_purchase_expire_time(metadata, now_unix=now_unix)
        if bool(metadata.is_avatar):
            for _ in range(int(reward.count)):
                grants.append(
                    InventoryGrantSpec(
                        item_id=item_id,
                        count=1,
                        metadata=metadata,
                        option_value=option_queues[item_id].popleft(),
                        expire_time=expire_time,
                    )
                )
        else:
            grants.append(
                InventoryGrantSpec(
                    item_id=item_id,
                    count=int(reward.count),
                    metadata=metadata,
                    option_value=0,
                    expire_time=expire_time,
                )
            )
    if filtered_invalid:
        return grants, "ok filtered_invalid=%d" % filtered_invalid
    return grants, "ok"


class OpenCeraPackage0207Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        packet = self._emit(1, 0x0207, build_open_cera_package_failure())
        return [packet], "C1/0207 OPEN_CERA_PACKAGE FAIL %s" % note

    def handle_open_cera_package_0207(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_open_cera_package_request(body)
        if request is None:
            return self._failure("malformed plain_len=%d" % len(bytes(body or b"")))

        cid = _selected_character_id(session)
        if cid is None:
            return self._failure("selected_character_missing")
        try:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        except (TypeError, ValueError):
            account_id = 1

        repo = self.server._repo()
        source = repo.get_inventory_item_at(
            cid,
            list_type=0,
            slot_index=request.source_slot,
        )
        if source is None:
            return self._failure("source_slot_empty=%d" % request.source_slot)
        package_item_id = int(source.get("item_template_id", 0) or 0) & 0xFFFFFFFF
        definition = resolve_cera_package(package_item_id)
        if definition is None:
            return self._failure("package_definition_missing item=%d" % package_item_id)

        now_unix = int(time.time())
        grants, grant_note = _build_package_grants(
            definition,
            request.choices,
            now_unix=now_unix,
        )
        if grants is None:
            return self._failure("item=%d %s" % (package_item_id, grant_note))
        try:
            reward_notice = build_cera_package_reward_notice(
                request.source_slot,
                grants,
            )
        except ValueError as exc:
            return self._failure("item=%d notice_error=%s" % (package_item_id, exc))

        package_metadata = resolve_item_metadata(package_item_id)
        ok, settlement_note, detail = repo.open_cera_package(
            account_id=account_id,
            character_id=cid,
            source_slot=request.source_slot,
            expected_package_item_id=package_item_id,
            grants=grants,
            source_metadata=package_metadata,
            now_unix=now_unix,
        )
        if not ok:
            return self._failure(
                "item=%d slot=%d %s"
                % (package_item_id, request.source_slot, settlement_note)
            )

        ack_packet = self._emit(
            1,
            0x0207,
            build_open_cera_package_success(request.source_slot),
        )
        reward_packet = self._emit(1, 0x00A0, reward_notice)
        note = (
            "C1/0207 OPEN_CERA_PACKAGE OK item=%d slot=%d choices=%d "
            "rewards=%d source_remaining=%d lists=%s"
            % (
                package_item_id,
                request.source_slot,
                len(request.choices),
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
                    character_id=cid,
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

        # The package transaction is already committed. Keep both the success
        # ACK and reward notification if a later delta cannot be encoded.
        refresh_packets: list[tuple[bytes, str]] = []
        try:
            changed_list_types = sorted(
                {
                    int(value)
                    for value in list(detail.get("changed_list_types", ()) or ())
                    if int(value) in (
                        0,
                        1,
                        7,
                        repo.INVENTORY_LIST_GUILD_MEDAL,
                    )
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
                rows = repo.list_inventory_items(cid, list_type=list_type)
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
                        "CERA_PACKAGE_REFRESH list=%d slots=%s"
                        % (list_type, sorted(changed_slots)),
                    )
                )
        except Exception as exc:
            note += " refresh_error=%s:%s" % (type(exc).__name__, exc)
            return [ack_packet, reward_packet] + activation_packets, note
        if is_cera_package_reward_notice_confirmed(session):
            note += " refresh_immediate_piv_seen_00a0=%d" % len(refresh_packets)
            return [ack_packet, reward_packet] + activation_packets + [
                packet for packet, _refresh_note in refresh_packets
            ], note

        armed_count = arm_cera_package_refresh(
            session,
            refresh_packets,
            character_id=cid,
        )
        note += " refresh_wait_piv_00a0=%d" % armed_count
        return [ack_packet, reward_packet] + activation_packets, note


__all__ = [
    "AvatarPackageChoice",
    "OpenCeraPackage0207Service",
    "OpenCeraPackageRequest",
    "build_cera_package_reward_notice",
    "build_open_cera_package_failure",
    "build_open_cera_package_success",
    "parse_open_cera_package_request",
]
