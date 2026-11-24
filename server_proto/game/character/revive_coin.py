"""Canonical item identities for the account-owned revive-coin wallet."""
from __future__ import annotations


REVIVE_COIN_WALLET_ITEM_ID = 1
REVIVE_COIN_REWARD_ITEM_ID = 42
REVIVE_COIN_GRANT_ITEM_IDS = frozenset(
    (REVIVE_COIN_WALLET_ITEM_ID, REVIVE_COIN_REWARD_ITEM_ID)
)


def is_revive_coin_grant_item_id(item_id: int) -> bool:
    """Return whether an acquired item must credit the revive-coin wallet."""
    return (int(item_id) & 0xFFFFFFFF) in REVIVE_COIN_GRANT_ITEM_IDS


def project_revive_coin_grant_item_id(item_id: int) -> int:
    """Map a revive-coin reward to the Stage3 fixed wallet projection ID."""
    normalized = int(item_id) & 0xFFFFFFFF
    if is_revive_coin_grant_item_id(normalized):
        return REVIVE_COIN_WALLET_ITEM_ID
    return normalized


__all__ = [
    "REVIVE_COIN_GRANT_ITEM_IDS",
    "REVIVE_COIN_REWARD_ITEM_ID",
    "REVIVE_COIN_WALLET_ITEM_ID",
    "is_revive_coin_grant_item_id",
    "project_revive_coin_grant_item_id",
]
