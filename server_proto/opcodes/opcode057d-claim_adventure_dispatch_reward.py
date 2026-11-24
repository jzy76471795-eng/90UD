# -*- coding: utf-8 -*-
"""C1/057D grants a completed dispatch reward to one account character."""
from __future__ import annotations

import struct
from typing import Any, Sequence

from game.character.adventure_group_features import (
    MERCENARY_INFO_OPCODE,
    MERCENARY_STATE_OPCODE,
    build_main_inventory_refresh_packet,
    build_mercenary_info_body_01ba,
    build_mercenary_state_body_0540,
    load_mercenary_character_points,
    resolve_account_id,
)
from game.character.item_metadata import resolve_item_metadata
from pvf.adventure_group_feature_index import get_adventure_group_feature_index


CLAIM_ADVENTURE_DISPATCH_REWARD_OPCODE = 0x057D
ERROR_CODE = 1
REWARD_WINDOW_SLOTS = 3
EMPTY_REWARD_DISPLAY_ID = 10899001
EMPTY_REWARD_SLOT = struct.pack("<II", EMPTY_REWARD_DISPLAY_ID, 1)


def parse_claim_adventure_dispatch_reward_057d(body: bytes) -> int | None:
    raw = bytes(body or b"")
    if len(raw) < 4 or any(raw[4:]):
        return None
    character_id = struct.unpack_from("<I", raw, 0)[0]
    return character_id if character_id > 0 else None


def build_claim_adventure_dispatch_success_057d(
    character_id: int,
    rewards: Sequence[tuple[int, int]],
) -> bytes:
    entries = tuple(rewards)
    if not 0 < int(character_id) <= 0xFFFFFFFF or len(entries) > 0xFF:
        raise ValueError("invalid adventure dispatch reward response")
    # The old client reuses the window and only overwrites the slots it receives.
    # The client-only blank visual clears the icon and name in a reused slot.
    # The stock control suppresses the quantity label for a single unit.
    empty_slots = max(0, REWARD_WINDOW_SLOTS - len(entries))
    body = bytearray(struct.pack("<BIB", 1, int(character_id), len(entries) + empty_slots))
    for item_id, count in entries:
        if int(item_id) == EMPTY_REWARD_DISPLAY_ID or not 0 < int(item_id) <= 0xFFFFFFFF or not 0 < int(count) <= 0xFFFFFFFF:
            raise ValueError("invalid adventure dispatch reward row")
        body += struct.pack("<II", int(item_id), int(count))
    body.extend(EMPTY_REWARD_SLOT * empty_slots)
    return bytes(body)


def _failure(server: Any, reason: str) -> tuple[list[bytes], str]:
    _audit(server, "reject=%s" % reason)
    packet = server._codec_response(
        1,
        CLAIM_ADVENTURE_DISPATCH_REWARD_OPCODE,
        bytes((0, ERROR_CODE)),
    )
    return [packet], "C1/057D ADVENTURE_REWARD reject=%s" % reason


def _audit(server: Any, message: str) -> None:
    logger = getattr(server, "log", None)
    if callable(logger):
        try:
            logger("[AdventureReward] " + message)
        except Exception:
            pass


def handle_claim_adventure_dispatch_reward_057d(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    character_id = parse_claim_adventure_dispatch_reward_057d(body)
    if character_id is None:
        return _failure(server, "invalid_body")
    repo = server._repo()
    account_id = resolve_account_id(server, repo, session)
    state = repo.load_adventure_dispatch_state(account_id)
    _audit(server, "request account=%d character=%d state=%d area=%d points=%d" % (
        account_id, character_id, int(state.get("dispatch_state", 0)),
        int(state.get("area_index", -1)), int(state.get("committed_points", 0))))
    if int(state.get("dispatch_state", 0)) != 2:
        return _failure(server, "dispatch_not_complete")
    feature_index = get_adventure_group_feature_index()
    area = feature_index.dispatch_area(int(state.get("area_index", -1)))
    if area is None:
        return _failure(server, "dispatch_area_missing")
    try:
        rewards = feature_index.dispatch_rewards(area.area_index, int(state.get("committed_points", 0)),
            duration_seconds=int(state.get('finish_unix',0))-int(state.get('start_unix',0)))
        reward_display = build_claim_adventure_dispatch_success_057d(character_id, rewards)
    except ValueError:
        return _failure(server, "invalid_reward_configuration_or_points")
    rewards_with_metadata = tuple(
        (item_id, count, resolve_item_metadata(item_id))
        for item_id, count in rewards
    )
    ok, detail, result = repo.claim_adventure_dispatch_reward(
        account_id=account_id,
        character_id=character_id,
        expected_area_index=area.area_index,
        required_character_level=area.required_character_level,
        rewards=rewards_with_metadata,
    )
    if not ok:
        return _failure(server, detail)
    _audit(server, "success account=%d character=%d area=%d rewards=%s" % (
        account_id, character_id, area.area_index, list(rewards)))
    packets = [
        server._codec_response(
            1,
            CLAIM_ADVENTURE_DISPATCH_REWARD_OPCODE,
            reward_display,
        ),
        server._codec_response(
            0,
            MERCENARY_STATE_OPCODE,
            build_mercenary_state_body_0540(None),
        ),
        server._codec_response(
            1,
            MERCENARY_INFO_OPCODE,
            build_mercenary_info_body_01ba(
                load_mercenary_character_points(repo, account_id)
            ),
        ),
    ]
    notes = ["S1/057D", "S0/0540 state=0", "S1/01BA"]
    current_character_id = int(getattr(session, "selected_character_id", 0) or 0)
    shared_souls_changed = any(grant.get("account_soul") for grant in result.get("rewards", ()))
    if current_character_id == character_id or (
        shared_souls_changed and current_character_id > 0
        and repo.get_account_id_for_character(current_character_id) == account_id
    ):
        refresh, refresh_note = build_main_inventory_refresh_packet(
            server, repo, current_character_id
        )
        packets.append(refresh)
        notes.append(refresh_note)
    return packets, (
        "C1/057D ADVENTURE_REWARD success account=%d character=%d area=%d "
        "rewards=%s flow=%s detail=%s"
        % (account_id, character_id, area.area_index, list(rewards), notes, result)
    )


__all__ = [
    "CLAIM_ADVENTURE_DISPATCH_REWARD_OPCODE",
    "build_claim_adventure_dispatch_success_057d",
    "handle_claim_adventure_dispatch_reward_057d",
    "parse_claim_adventure_dispatch_reward_057d",
]
