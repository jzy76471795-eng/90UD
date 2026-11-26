"""C1/001B PVF legacy lottery-item open flow for frozen Stage3."""
from __future__ import annotations

import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from game.chain_loader import load_chain
from game.character.item_metadata import resolve_item_metadata
from game.character.random_container_index import LOTTERY_TYPES, resolve_lottery_container


_helper = load_chain("helper-random_container.py")
_activation = load_chain("helper-system_reward_activation.py")


@dataclass(frozen=True)
class UseLotteryItemRequest:
    source_slot: int


def parse_use_lottery_item_request(body: bytes) -> UseLotteryItemRequest | None:
    raw = bytes(body or b"")
    if len(raw) != 2:
        return None
    return UseLotteryItemRequest(source_slot=int(struct.unpack_from("<H", raw)[0]))


class UseLotteryItem001BService:
    def __init__(
        self,
        server: Any,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        self.server = server
        self.randbelow = randbelow

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        return self.server._codec_response(flag, opcode, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        packet = self._emit(1, 0x001B, _helper.build_random_container_failure())
        return [packet], "C1/001B USE_LOTTERY_ITEM FAIL attempt=1/3 %s" % note

    def handle_use_lottery_item_001b(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_use_lottery_item_request(body)
        if request is None:
            return self._failure("malformed_request plain_len=%d" % len(body or b""))
        character_id = _helper.selected_character_id(session)
        if character_id is None:
            return self._failure("selected_character_missing")
        repo = self.server._repo()
        account_id = _helper.account_id_for_character(repo, character_id)
        if account_id is None or account_id != int(self.server.account_id or 0):
            return self._failure("character_account_missing cid=%d" % character_id)
        source = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.source_slot,
        )
        if source is None:
            return self._failure("source_slot_empty=%d" % request.source_slot)
        source_item_id = int(source.get("item_template_id", 0) or 0) & 0xFFFFFFFF
        definition = resolve_lottery_container(source_item_id)
        if definition is None:
            return self._failure("lottery_definition_missing item=%d" % source_item_id)
        try:
            reward = definition.roll(randbelow=self.randbelow)
            now_unix = int(time.time())
            is_gold_reward = int(reward.item_id) == 0
            if is_gold_reward:
                grants = []
                include_item_extensions = False
            else:
                grants, reward_metadata = _helper.build_random_reward_grants(
                    (reward,),
                    now_unix=now_unix,
                )
                include_item_extensions = _helper.lottery_item_uses_extension_blobs(
                    reward_metadata[reward.item_id]
                )
            _helper.build_lottery_item_success(
                source_slot=request.source_slot,
                reward_slot=0,
                item_id=reward.item_id,
                count=reward.count,
                include_item_extensions=include_item_extensions,
            )
        except (KeyError, ValueError) as exc:
            return self._failure("reward_error item=%d error=%s" % (source_item_id, exc))

        source_metadata = resolve_item_metadata(source_item_id)
        ok, settlement_note, detail = repo.open_cera_package(
            account_id=account_id,
            character_id=character_id,
            source_slot=request.source_slot,
            expected_package_item_id=source_item_id,
            grants=grants,
            source_metadata=source_metadata,
            allowed_source_types=LOTTERY_TYPES,
            enforce_reward_job_restrictions=False,
            gold_reward=reward.count if is_gold_reward else 0,
            gold_cost=definition.gold_cost,
            activate_system_rewards=True,
            now_unix=now_unix,
        )
        if not ok:
            return self._failure(
                "item=%d slot=%d transaction=%s"
                % (source_item_id, request.source_slot, settlement_note)
            )
        # open_cera_package proves and returns this slot before applying any
        # writes. All other result fields were validated by the preflight
        # builder above, so no failure response is possible after commit.
        reward_slot = _helper.resolve_lottery_reward_slot(detail)
        result_body = _helper.build_lottery_item_success(
            source_slot=request.source_slot,
            reward_slot=reward_slot,
            item_id=reward.item_id,
            count=reward.count,
            include_item_extensions=include_item_extensions,
        )

        result_packet = self._emit(1, 0x001B, result_body)
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
        return [result_packet] + activation_packets + refresh_packets, (
            "C1/001B USE_LOTTERY_ITEM OK attempt=1/3 source=%d slot=%d "
            "reward=%dx%d reward_slot=%d currency=%s source_remaining=%d refresh=%s; %s"
            % (
                source_item_id,
                request.source_slot,
                reward.item_id,
                reward.count,
                reward_slot,
                "gold" if is_gold_reward else "item",
                int(detail.get("source_remaining", 0) or 0),
                list(refresh_notes) + list(activation_notes),
                settlement_note,
            )
        )


__all__ = [
    "UseLotteryItem001BService",
    "UseLotteryItemRequest",
    "parse_use_lottery_item_request",
]
