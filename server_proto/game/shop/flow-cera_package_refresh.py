# -*- coding: utf-8 -*-
"""PIV-gated inventory refresh for Cera package rewards."""
from __future__ import annotations

from typing import Any, Iterable


_PENDING_ATTR = "cera_package_refresh_pending"
_CHARACTER_ATTR = "cera_package_refresh_character_id"
_REWARD_NOTICE_CONFIRMED_ATTR = "cera_package_reward_notice_piv_confirmed"
_REWARD_NOTICE_PIV = (1, 0x00A0)


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


def clear_cera_package_refresh(session: Any) -> None:
    if session is None:
        return
    setattr(session, _PENDING_ATTR, [])
    setattr(session, _CHARACTER_ATTR, None)


def is_cera_package_reward_notice_confirmed(session: Any) -> bool:
    """Return whether this connection already accepted the 00A0 reader."""
    if session is None:
        return False
    return bool(getattr(session, _REWARD_NOTICE_CONFIRMED_ATTR, False))


def arm_cera_package_refresh(
    session: Any,
    packets: Iterable[tuple[bytes, str]],
    *,
    character_id: int,
) -> int:
    """Hold encoded list deltas until Stage3 accepts the reward notice."""
    if session is None:
        return 0

    normalized = [
        (bytes(packet), str(note))
        for packet, note in packets
        if bytes(packet)
    ]
    if not normalized:
        return 0

    armed_character_id = int(character_id)
    pending_character_id = getattr(session, _CHARACTER_ATTR, None)
    pending = list(getattr(session, _PENDING_ATTR, None) or ())
    if pending and pending_character_id != armed_character_id:
        pending = []

    pending.extend(normalized)
    setattr(session, _PENDING_ATTR, pending)
    setattr(session, _CHARACTER_ATTR, armed_character_id)
    return len(normalized)


def release_cera_package_refresh_on_piv(
    session: Any,
    echoed_flag: int,
    echoed_opcode: int,
) -> list[tuple[bytes, str]]:
    """Release all held deltas once Stage3 accepts the reward notice."""
    if session is None:
        return []
    echoed_key = (
        int(echoed_flag) & 0xFF,
        int(echoed_opcode) & 0xFFFF,
    )
    if echoed_key != _REWARD_NOTICE_PIV:
        return []

    # Stage3 reports this reader acceptance once per connection. Remember an
    # idle confirmation too, because a small booster can hit 00A0 before 0207.
    setattr(session, _REWARD_NOTICE_CONFIRMED_ATTR, True)

    pending = list(getattr(session, _PENDING_ATTR, None) or ())
    pending_character_id = getattr(session, _CHARACTER_ATTR, None)
    current_character_id = _selected_character_id(session)

    # Consume before returning so duplicate PIV packets cannot replay deltas.
    clear_cera_package_refresh(session)
    if not pending:
        return []
    if (
        pending_character_id is None
        or current_character_id is None
        or int(pending_character_id) != int(current_character_id)
    ):
        return []
    return [(bytes(packet), str(note)) for packet, note in pending]


__all__ = [
    "arm_cera_package_refresh",
    "clear_cera_package_refresh",
    "is_cera_package_reward_notice_confirmed",
    "release_cera_package_refresh_on_piv",
]
