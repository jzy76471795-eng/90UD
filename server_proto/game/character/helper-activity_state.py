"""Character activity-state helpers shared by login and reward flows."""
from __future__ import annotations

import struct
from typing import Any


FATIGUE_BURNING_ACTIVITY_KEY = "fatigue_burning"
FATIGUE_BURNING_STATE_OPCODE = 0x01EB
FATIGUE_BURNING_COMMAND_OPCODE = 0x0233
FATIGUE_BURNING_EXP_PERCENT = 100


def build_fatigue_burning_state_body(enabled: bool, *, value: int = 0) -> bytes:
    """Build the Stage3 ``S0/01EB`` body: ``u16 value + u8 state``."""
    return struct.pack("<HB", int(value) & 0xFFFF, 1 if enabled else 0)


def cache_fatigue_burning_state(
    session: Any,
    *,
    character_id: int,
    enabled: bool,
) -> None:
    if session is None:
        return
    session.fatigue_burning_enabled = bool(enabled)
    session.fatigue_burning_hydrated = True
    session.fatigue_burning_character_id = int(character_id)


def hydrate_fatigue_burning_state(
    repo: Any,
    character_id: int,
    *,
    session: Any = None,
) -> bool:
    """Load the persisted switch and refresh the connection-local cache."""
    cid = int(character_id or 0)
    enabled = bool(
        cid > 0
        and repo.get_character_activity_state(
            cid,
            FATIGUE_BURNING_ACTIVITY_KEY,
            default=False,
        )
    )
    cache_fatigue_burning_state(
        session,
        character_id=cid,
        enabled=enabled,
    )
    return enabled


def fatigue_burning_enabled(
    repo: Any,
    character_id: int,
    *,
    session: Any = None,
) -> bool:
    """Return the cached switch, hydrating it on first use or character change."""
    cid = int(character_id or 0)
    if session is not None:
        try:
            if (
                bool(getattr(session, "fatigue_burning_hydrated", False))
                and int(getattr(session, "fatigue_burning_character_id", 0) or 0)
                == cid
            ):
                return bool(getattr(session, "fatigue_burning_enabled", False))
        except (TypeError, ValueError):
            pass
    return hydrate_fatigue_burning_state(repo, cid, session=session)


def compute_fatigue_burning_exp_bonus(
    repo: Any,
    character_id: int,
    base_exp: int,
    *,
    session: Any = None,
) -> int:
    """Apply the activity's 100% bonus to one authoritative base EXP value."""
    base = max(0, int(base_exp or 0))
    if base <= 0:
        return 0
    if not fatigue_burning_enabled(repo, character_id, session=session):
        return 0
    return min(0xFFFFFFFF, base * FATIGUE_BURNING_EXP_PERCENT // 100)


__all__ = [
    "FATIGUE_BURNING_ACTIVITY_KEY",
    "FATIGUE_BURNING_COMMAND_OPCODE",
    "FATIGUE_BURNING_EXP_PERCENT",
    "FATIGUE_BURNING_STATE_OPCODE",
    "build_fatigue_burning_state_body",
    "cache_fatigue_burning_state",
    "compute_fatigue_burning_exp_bonus",
    "fatigue_burning_enabled",
    "hydrate_fatigue_burning_state",
]
