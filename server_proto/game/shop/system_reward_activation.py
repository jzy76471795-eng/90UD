"""Classify PVF rewards that apply account or container state immediately."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from game.character.item_metadata import ItemMetadata
from game.character.revive_coin import is_revive_coin_grant_item_id
from game.shop.premium_contract_index import resolve_cached_premium_contract


SYSTEM_REWARD_REVIVE_COIN = "revive_coin"
SYSTEM_REWARD_PREMIUM = "premium_contract"
SYSTEM_REWARD_MAIN_EXPANSION = "main_inventory_expansion"
SYSTEM_REWARD_AVATAR_EXPANSION = "avatar_inventory_expansion"
SYSTEM_REWARD_PERSONAL_SAFE = "personal_safe_expansion"
SYSTEM_REWARD_ACCOUNT_SAFE = "account_safe_expansion"

_CONTAINER_TYPE_PREFIXES = (
    "[booster]",
    "[cera booster]",
    "[booster random]",
    "[booster selection]",
    "[usable cera package]",
    "[upgradable legacy]",
    "[legacy]",
    "[multi upgradable legacy]",
)

ACTION_UPGRADE_INVENTORY = "[upgrade inventory]"
ACTION_AVATAR_INVENTORY_EXPANSION = "[avatar inventory expansion]"
ACTION_UPGRADE_CARGO = "[upgrade cargo]"
ACTION_UPGRADE_ACCOUNT_CARGO = "[upgrade account cargo]"

_MAIN_EXPANSION_NAMES = frozenset(
    {
        "inventory expansion ticket",
        "inventory expansion kit",
        "物品栏扩展券",
    }
)
_AVATAR_EXPANSION_NAMES = frozenset(
    {
        "avatar inventory expansion ticket",
        "avatar inventory expansion pass",
        "装扮栏扩展券",
        "时装栏扩展券",
    }
)
_PERSONAL_SAFE_NAMES = frozenset(
    {
        "safe upgrade kit",
        "storage expansion coupon",
        "银质金库升级工具",
        "金库升级工具",
        "金库扩展券",
    }
)
_ACCOUNT_SAFE_NAMES = frozenset(
    {
        "account safe upgrade kit",
        "account vault upgrade kit",
        "账号金库升级工具",
        "账号金库扩展券",
    }
)


@dataclass(frozen=True)
class SystemRewardActivation:
    kind: str
    delta_per_item: int = 0
    premium_type: int = 0
    duration_seconds_per_item: int = 0


def _normalize_text(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = " ".join(text[1:-1].strip().lower().split())
    return text


def classify_system_reward(
    metadata: ItemMetadata,
) -> Optional[SystemRewardActivation]:
    """Return the immediate server action for one warmed PVF item definition.

    Item 42 is the fixed Life Token identity. Premium aliases come from
    ``premiumlist_new.etc``. Capacity identities prefer the current item
    script's structured ``[action type]`` and parameters. Exact localized
    names remain only for legacy scripts that omit an action.
    """
    if metadata is None or not bool(getattr(metadata, "is_stackable", False)):
        return None
    item_id = int(getattr(metadata, "item_id", 0) or 0) & 0xFFFFFFFF
    if item_id <= 0:
        return None

    # ID 42 is the current PVF Life Token reward. ID 1 is only the fixed
    # Stage3 wallet projection retained for established purchase paths.
    if is_revive_coin_grant_item_id(item_id):
        return SystemRewardActivation(kind=SYSTEM_REWARD_REVIVE_COIN)

    premium = resolve_cached_premium_contract(item_id)
    if premium is not None:
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_PREMIUM,
            premium_type=int(premium.premium_type) & 0xFF,
            duration_seconds_per_item=int(premium.duration_seconds),
        )

    action_type = _normalize_text(getattr(metadata, "action_type", ""))
    action_values = tuple(
        int(value)
        for value in (getattr(metadata, "action_values", ()) or ())
    )
    if action_type == ACTION_UPGRADE_INVENTORY:
        if len(action_values) > 1 or any(value <= 0 for value in action_values):
            return None
        multiplier = max(1, action_values[0]) if action_values else 1
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_MAIN_EXPANSION,
            delta_per_item=8 * multiplier,
        )
    if action_type == ACTION_AVATAR_INVENTORY_EXPANSION:
        if len(action_values) != 1 or action_values[0] <= 0:
            return None
        multiplier = max(1, action_values[0]) if action_values else 1
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_AVATAR_EXPANSION,
            delta_per_item=7 * multiplier,
        )
    if action_type == ACTION_UPGRADE_CARGO:
        if action_values:
            return None
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_PERSONAL_SAFE,
            delta_per_item=40,
        )
    if action_type == ACTION_UPGRADE_ACCOUNT_CARGO:
        if action_values:
            return None
        return SystemRewardActivation(kind=SYSTEM_REWARD_ACCOUNT_SAFE)

    name = _normalize_text(getattr(metadata, "name", ""))
    if name in _MAIN_EXPANSION_NAMES:
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_MAIN_EXPANSION,
            delta_per_item=8,
        )
    if name in _AVATAR_EXPANSION_NAMES:
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_AVATAR_EXPANSION,
            delta_per_item=7,
        )
    if name in _ACCOUNT_SAFE_NAMES:
        return SystemRewardActivation(kind=SYSTEM_REWARD_ACCOUNT_SAFE)
    if name in _PERSONAL_SAFE_NAMES or name.endswith(" safe upgrade kit"):
        return SystemRewardActivation(
            kind=SYSTEM_REWARD_PERSONAL_SAFE,
            delta_per_item=40,
        )

    stackable_type = _normalize_text(getattr(metadata, "stackable_type", ""))
    if any(stackable_type.startswith(prefix) for prefix in _CONTAINER_TYPE_PREFIXES):
        return None

    return None


__all__ = [
    "ACTION_AVATAR_INVENTORY_EXPANSION",
    "ACTION_UPGRADE_ACCOUNT_CARGO",
    "ACTION_UPGRADE_CARGO",
    "ACTION_UPGRADE_INVENTORY",
    "SYSTEM_REWARD_ACCOUNT_SAFE",
    "SYSTEM_REWARD_AVATAR_EXPANSION",
    "SYSTEM_REWARD_MAIN_EXPANSION",
    "SYSTEM_REWARD_PERSONAL_SAFE",
    "SYSTEM_REWARD_PREMIUM",
    "SYSTEM_REWARD_REVIVE_COIN",
    "SystemRewardActivation",
    "classify_system_reward",
]
