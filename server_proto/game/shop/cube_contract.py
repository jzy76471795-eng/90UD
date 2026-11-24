"""Stage3 cube-contract selection state shared by purchase, login, and C1/0218."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from game.shop.premium_contract_index import PREMIUM_TYPE_CUBE


CUBE_CONTRACT_UPDATE_OPCODE = 0x0218
CUBE_CONTRACT_STATE_OPCODE = 0x0383
CUBE_CONTRACT_UPDATE_MODE = 0
CUBE_CONTRACT_STATE_MODE = 1
CUBE_CONTRACT_NO_SELECTION = 0xFF
CUBE_CONTRACT_SELECTIONS: frozenset[int] = frozenset(range(6))


@dataclass(frozen=True)
class CubeContractUpdateRequest:
    mode: int
    selection: int


@dataclass(frozen=True)
class CubeContractState:
    active: bool
    stored_selection: int
    wire_selection: int

    def to_body(self) -> bytes:
        return build_cube_contract_state_body(self.wire_selection)


def is_cube_contract_selection(value: int, *, allow_none: bool = True) -> bool:
    selection = int(value)
    return selection in CUBE_CONTRACT_SELECTIONS or (
        allow_none and selection == CUBE_CONTRACT_NO_SELECTION
    )


def normalize_cube_contract_selection(value: Any) -> int:
    try:
        selection = int(value)
    except (TypeError, ValueError):
        return CUBE_CONTRACT_NO_SELECTION
    if not is_cube_contract_selection(selection):
        return CUBE_CONTRACT_NO_SELECTION
    return selection


def parse_cube_contract_update(body: bytes) -> CubeContractUpdateRequest | None:
    """Parse the live Stage3 writer body: ``u8 mode=0, u8 selection``."""
    raw = bytes(body or b"")
    if len(raw) != 2 or raw[0] != CUBE_CONTRACT_UPDATE_MODE:
        return None
    selection = int(raw[1])
    if not is_cube_contract_selection(selection):
        return None
    return CubeContractUpdateRequest(
        mode=CUBE_CONTRACT_UPDATE_MODE,
        selection=selection,
    )


def build_cube_contract_state_body(selection: int) -> bytes:
    normalized = int(selection)
    if not is_cube_contract_selection(normalized):
        raise ValueError("invalid cube-contract selection=%d" % normalized)
    return bytes((CUBE_CONTRACT_STATE_MODE, normalized))


def _has_active_cube_contract(
    rows: Sequence[Mapping[str, Any]],
    *,
    now_unix: int,
) -> bool:
    now = max(0, int(now_unix))
    for row in rows:
        if (int(row.get("premium_type", 0) or 0) & 0xFF) != PREMIUM_TYPE_CUBE:
            continue
        remaining = int(row.get("remaining_seconds", 0) or 0)
        end_time = int(row.get("end_time", 0) or 0)
        if remaining > 0 or end_time > now:
            return True
    return False


def resolve_cube_contract_state(
    repo: Any,
    *,
    account_id: int,
    character_id: int,
    now_unix: int | None = None,
    active_premium_rows: Sequence[Mapping[str, Any]] | None = None,
) -> CubeContractState:
    """Load the saved character selection and mask it while type 92 is inactive."""
    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    rows = (
        list(active_premium_rows)
        if active_premium_rows is not None
        else repo.list_active_account_premiums(int(account_id), now_unix=now)
    )
    active = _has_active_cube_contract(rows, now_unix=now)
    stored = normalize_cube_contract_selection(
        repo.load_cube_contract_selection(int(character_id))
    )
    return CubeContractState(
        active=active,
        stored_selection=stored,
        wire_selection=stored if active else CUBE_CONTRACT_NO_SELECTION,
    )


__all__ = [
    "CUBE_CONTRACT_NO_SELECTION",
    "CUBE_CONTRACT_SELECTIONS",
    "CUBE_CONTRACT_STATE_MODE",
    "CUBE_CONTRACT_STATE_OPCODE",
    "CUBE_CONTRACT_UPDATE_MODE",
    "CUBE_CONTRACT_UPDATE_OPCODE",
    "CubeContractState",
    "CubeContractUpdateRequest",
    "build_cube_contract_state_body",
    "is_cube_contract_selection",
    "normalize_cube_contract_selection",
    "parse_cube_contract_update",
    "resolve_cube_contract_state",
]
