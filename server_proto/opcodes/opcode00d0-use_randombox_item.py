"""C1/00D0 PVF multi-upgradable random-box flow for frozen Stage3."""
from __future__ import annotations

import secrets
import struct
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from game.chain_loader import load_chain
from game.character.item_metadata import resolve_item_metadata
from game.character.seria_luck_index import resolve_seria_luck_max
from game.character.random_container_index import (
    RANDOM_BOX_TYPE,
    merge_random_rewards,
    resolve_random_box,
)


_helper = load_chain("helper-random_container.py")
_activation = load_chain("helper-system_reward_activation.py")


@dataclass(frozen=True)
class UseRandomBoxRequest:
    box_slot: int
    material_slot: int


def parse_use_random_box_request(body: bytes) -> UseRandomBoxRequest | None:
    raw = bytes(body or b"")
    if len(raw) != 4:
        return None
    box_slot, material_slot = struct.unpack_from("<HH", raw)
    return UseRandomBoxRequest(
        box_slot=int(box_slot),
        material_slot=int(material_slot),
    )


class UseRandomBoxItem00D0Service:
    def __init__(
        self,
        server: Any,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.server = server
        self.randbelow = randbelow
        self.now = now

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        packet = self._emit(1, 0x00D0, _helper.build_random_container_failure())
        return [packet], "C1/00D0 USE_RANDOM_BOX FAIL attempt=3/3 %s" % note

    def handle_use_random_box_item_00d0(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_use_random_box_request(body)
        if request is None:
            return self._failure("malformed_request plain_len=%d" % len(body or b""))
        character_id = _helper.selected_character_id(session)
        if character_id is None:
            return self._failure("selected_character_missing")
        repo = self.server._repo()
        account_id = _helper.account_id_for_character(repo, character_id)
        if account_id is None:
            return self._failure("character_account_missing cid=%d" % character_id)
        source = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.box_slot,
        )
        if source is None:
            return self._failure("box_slot_empty=%d" % request.box_slot)
        source_item_id = int(source.get("item_template_id", 0) or 0) & 0xFFFFFFFF
        definition = resolve_random_box(source_item_id)
        if definition is None:
            return self._failure("random_box_definition_missing item=%d" % source_item_id)

        material = definition.sealing_removal_item
        if material is None and request.material_slot != 0xFFFF:
            return self._failure(
                "unexpected_material_slot=%d item=%d"
                % (request.material_slot, source_item_id)
            )
        if material is not None and request.material_slot == 0xFFFF:
            return self._failure(
                "material_slot_missing item=%d need=%dx%d"
                % (source_item_id, material.item_id, material.count)
            )

        try:
            rolled = definition.roll(now=self.now(), randbelow=self.randbelow)
            rewards = merge_random_rewards(rolled)
            now_unix = int(time.time())
            grants, _reward_metadata = _helper.build_random_reward_grants(
                rewards,
                now_unix=now_unix,
            )
        except ValueError as exc:
            return self._failure("reward_error item=%d error=%s" % (source_item_id, exc))

        source_metadata = resolve_item_metadata(source_item_id)
        ok, settlement_note, detail = repo.open_cera_package(
            account_id=account_id,
            character_id=character_id,
            source_slot=request.box_slot,
            expected_package_item_id=source_item_id,
            grants=grants,
            source_metadata=source_metadata,
            allowed_source_types=(RANDOM_BOX_TYPE,),
            material_slot=(request.material_slot if material is not None else None),
            expected_material_item_id=(material.item_id if material is not None else 0),
            material_count=(material.count if material is not None else 0),
            enforce_reward_job_restrictions=False,
            activate_system_rewards=True,
            now_unix=now_unix,
            seria_luck_max=resolve_seria_luck_max(source_item_id),
        )
        if not ok:
            return self._failure(
                "item=%d box_slot=%d material_slot=%d transaction=%s"
                % (
                    source_item_id,
                    request.box_slot,
                    request.material_slot,
                    settlement_note,
                )
            )

        multiplier = int(detail.get("reward_multiplier", 1))
        if multiplier != 1:
            rewards = tuple(replace(row, count=row.count * multiplier) for row in rewards)

        display_slots, reward_refresh_slots = _helper.resolve_random_box_display_slots(
            detail,
            rewards,
        )
        # Prime only newly granted reward objects first.  The special UI still
        # needs the pre-consumption source/key objects when it reads S1/00D0,
        # so their final refresh remains after the result packet.
        pre_refresh_slots = {
            list_type: set(slots)
            for list_type, slots in reward_refresh_slots.items()
        }
        main_pre_refresh_slots = pre_refresh_slots.get(0)
        if main_pre_refresh_slots is not None:
            main_pre_refresh_slots.discard(request.box_slot)
            if material is not None:
                main_pre_refresh_slots.discard(request.material_slot)
        pre_refresh_slots = {
            list_type: tuple(sorted(slots))
            for list_type, slots in pre_refresh_slots.items()
            if slots
        }
        try:
            pre_refresh_packets, pre_refresh_notes = _helper.build_changed_inventory_refreshes(
                self.server,
                repo,
                character_id=character_id,
                detail=detail,
                now_unix=now_unix,
                only_slots_by_list_type=pre_refresh_slots,
            )
        except Exception as exc:
            pre_refresh_packets = []
            pre_refresh_notes = ("pre_refresh_error=%s:%s" % (type(exc).__name__, exc),)

        result_body = _helper.build_random_box_success(
            box_slot=request.box_slot,
            material_slot=request.material_slot,
            rewards=rewards,
            display_slots=display_slots,
        )
        result_packet = self._emit(1, 0x00D0, result_body)
        activation_packets, activation_notes = _activation.build_system_reward_activation_packets(
            self.server,
            repo,
            detail=detail,
            account_id=account_id,
            character_id=character_id,
            session=session,
        )
        try:
            refresh_packets, refresh_notes = _helper.build_changed_inventory_refreshes(
                self.server,
                repo,
                character_id=character_id,
                detail=detail,
                now_unix=now_unix,
            )
        except Exception as exc:
            refresh_packets = []
            refresh_notes = ("refresh_error=%s:%s" % (type(exc).__name__, exc),)
        reward_text = ["%dx%d" % (row.item_id, row.count) for row in rewards]
        return pre_refresh_packets + [result_packet] + activation_packets + refresh_packets, (
            "C1/00D0 USE_RANDOM_BOX OK attempt=3/3 source=%d box_slot=%d "
            "material=%s rewards=%s source_remaining=%d material_remaining=%d "
            "groups=%d merged=%d pre_refresh=%s refresh=%s; %s"
            % (
                source_item_id,
                request.box_slot,
                (
                    "%dx%d@%d" % (material.item_id, material.count, request.material_slot)
                    if material is not None
                    else "none"
                ),
                reward_text,
                int(detail.get("source_remaining", 0) or 0),
                int(detail.get("material_remaining", 0) or 0),
                len(rolled),
                len(rewards),
                list(pre_refresh_notes),
                list(refresh_notes) + list(activation_notes),
                settlement_note,
            )
        )


__all__ = [
    "UseRandomBoxItem00D0Service",
    "UseRandomBoxRequest",
    "parse_use_random_box_request",
]
