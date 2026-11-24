"""Official guild-medal reinforcement rules for the Stage3 guild machine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from game.character.item_metadata import (
    GUILD_MEDAL_INVENTORY_LIST_TYPE,
    SLOT_RANGE_GUILD_MEDAL,
)
from game.character.upgrade_config import UPGRADE_WEIGHT_SCALE


GUILD_MEDAL_CONTRIBUTION_ITEM_ID = 10154439
# The Stage3 guild machine checks one physical stack before writing C1/0050.
# Zero is the repository convention for a mergeable stack without a numeric cap.
GUILD_MEDAL_CONTRIBUTION_STACK_LIMIT = 0
GUILD_MEDAL_MAX_UPGRADE_LEVEL = 12


@dataclass(frozen=True)
class GuildMedalUpgradeRule:
    target_level: int
    contribution_cost: int
    four_stat_bonus: int
    success_rate_percent: int

    @property
    def success_weight(self) -> int:
        return int(self.success_rate_percent) * 1000

    def succeeds_for_roll(self, roll: int) -> bool:
        value = int(roll)
        if not 0 <= value < UPGRADE_WEIGHT_SCALE:
            raise ValueError("guild medal upgrade roll outside weight scale")
        return value < self.success_weight


@dataclass(frozen=True)
class GuildMedalFailureResult:
    result_code: int
    new_level: int

    @property
    def destroyed(self) -> bool:
        return int(self.result_code) == 3


@dataclass(frozen=True)
class GuildMedalUpgradeSettlement:
    target_item_id: int
    target_slot: int
    old_level: int
    new_level: int
    result_code: int
    roll: int
    rule: GuildMedalUpgradeRule
    detail: dict[str, Any]
    db_note: str


class GuildMedalUpgradeError(Exception):
    def __init__(self, category: str, note: str, *, db_reason: str = "") -> None:
        super().__init__(str(note))
        self.category = str(category)
        self.note = str(note)
        self.db_reason = str(db_reason)


# Target level, Guild Contribution Certificate cost, total four-stat bonus,
# and success percentage. The table was supplied from the official rules on
# 2026-08-04 and is intentionally separate from etc/upgrade.etc.
GUILD_MEDAL_UPGRADE_RULES = (
    GuildMedalUpgradeRule(1, 18, 4, 100),
    GuildMedalUpgradeRule(2, 36, 5, 85),
    GuildMedalUpgradeRule(3, 55, 6, 72),
    GuildMedalUpgradeRule(4, 73, 7, 61),
    GuildMedalUpgradeRule(5, 92, 9, 52),
    GuildMedalUpgradeRule(6, 110, 11, 44),
    GuildMedalUpgradeRule(7, 128, 13, 38),
    GuildMedalUpgradeRule(8, 147, 15, 26),
    GuildMedalUpgradeRule(9, 167, 16, 19),
    GuildMedalUpgradeRule(10, 188, 17, 12),
    GuildMedalUpgradeRule(11, 261, 27, 5),
    GuildMedalUpgradeRule(12, 367, 54, 1),
)

_RULE_BY_TARGET_LEVEL = {
    int(rule.target_level): rule for rule in GUILD_MEDAL_UPGRADE_RULES
}


def _validate_rules() -> None:
    expected_levels = tuple(range(1, GUILD_MEDAL_MAX_UPGRADE_LEVEL + 1))
    actual_levels = tuple(int(rule.target_level) for rule in GUILD_MEDAL_UPGRADE_RULES)
    if actual_levels != expected_levels:
        raise ValueError("guild medal upgrade target levels must be contiguous 1..12")
    for rule in GUILD_MEDAL_UPGRADE_RULES:
        if int(rule.contribution_cost) <= 0 or int(rule.four_stat_bonus) <= 0:
            raise ValueError("guild medal upgrade costs and bonuses must be positive")
        if not 1 <= int(rule.success_rate_percent) <= 100:
            raise ValueError("guild medal upgrade success rate must be 1..100")


_validate_rules()


def guild_medal_rule_for_target_level(
    target_level: int,
) -> GuildMedalUpgradeRule | None:
    return _RULE_BY_TARGET_LEVEL.get(int(target_level))


def guild_medal_four_stat_bonus(upgrade_level: int) -> int:
    level = int(upgrade_level)
    if level <= 0:
        return 0
    rule = guild_medal_rule_for_target_level(level)
    if rule is None:
        raise ValueError("guild medal upgrade level outside 0..12")
    return int(rule.four_stat_bonus)


def guild_medal_failure_result(current_level: int) -> GuildMedalFailureResult:
    """Return the normal reinforcement-style penalty for one failed attempt."""
    level = int(current_level)
    if not 0 <= level < GUILD_MEDAL_MAX_UPGRADE_LEVEL:
        raise ValueError("guild medal current level outside 0..11")
    if level >= 10:
        return GuildMedalFailureResult(3, 0)
    if level >= 9:
        return GuildMedalFailureResult(2, 0)
    if level >= 7:
        return GuildMedalFailureResult(2, level - 2)
    if level >= 4:
        return GuildMedalFailureResult(2, level - 1)
    return GuildMedalFailureResult(1, level)


def settle_guild_medal_upgrade(
    repo: Any,
    *,
    rng: Any,
    character_id: int,
    upgrade_mode: int,
    target_slot: int,
    target: dict[str, Any],
    metadata: Any,
    material_slot: int,
) -> GuildMedalUpgradeSettlement:
    """Validate and settle one guild-machine attempt through the repository."""
    cid = int(character_id)
    slot = int(target_slot)
    if int(upgrade_mode) != 0:
        raise GuildMedalUpgradeError("mode", "reject=guild_medal_mode")
    if not SLOT_RANGE_GUILD_MEDAL[0] <= slot <= SLOT_RANGE_GUILD_MEDAL[1]:
        raise GuildMedalUpgradeError("target", "reject=guild_medal_slot")
    if not bool(metadata.is_equipment and metadata.is_guild_medal):
        raise GuildMedalUpgradeError(
            "restriction",
            "reject=guild_medal_pvf type=%s" % metadata.equipment_type,
        )

    old_level = (max(0, int(target.get("option_value") or 0)) & 0xFF) & 0x1F
    rule = guild_medal_rule_for_target_level(old_level + 1)
    if rule is None:
        raise GuildMedalUpgradeError(
            "max_level",
            "reject=guild_medal_max_level old_level=%d" % old_level,
        )

    material_anchor = int(material_slot)
    material = repo.get_inventory_item_at(
        cid,
        list_type=0,
        slot_index=material_anchor,
    )
    if (
        material is None
        or str(material.get("item_kind") or "") != "stackable"
        or (int(material.get("item_template_id") or 0) & 0xFFFFFFFF)
        != GUILD_MEDAL_CONTRIBUTION_ITEM_ID
        or int(material.get("stack_count") or 0) <= 0
    ):
        raise GuildMedalUpgradeError("material", "reject=guild_medal_material")
    material_available = sum(
        max(0, int(row.get("stack_count") or 0))
        for row in repo.list_inventory_items(cid, list_type=0)
        if (
            str(row.get("item_kind") or "") == "stackable"
            and (int(row.get("item_template_id") or 0) & 0xFFFFFFFF)
            == GUILD_MEDAL_CONTRIBUTION_ITEM_ID
        )
    )
    if material_available < int(rule.contribution_cost):
        raise GuildMedalUpgradeError(
            "material",
            "reject=guild_medal_material_insufficient have=%d need=%d"
            % (material_available, rule.contribution_cost),
        )

    roll = int(rng.randrange(UPGRADE_WEIGHT_SCALE))
    if rule.succeeds_for_roll(roll):
        result_code, new_level = 0, old_level + 1
    else:
        failure = guild_medal_failure_result(old_level)
        result_code = int(failure.result_code)
        new_level = int(failure.new_level)

    target_item_id = int(target.get("item_template_id") or 0) & 0xFFFFFFFF
    ok, db_note, detail = repo.apply_guild_medal_upgrade(
        character_id=cid,
        target_slot=slot,
        expected_item_id=target_item_id,
        expected_old_level=old_level,
        material_slot=material_anchor,
        material_item_id=GUILD_MEDAL_CONTRIBUTION_ITEM_ID,
        material_count=int(rule.contribution_cost),
        result_code=result_code,
        new_level=new_level,
    )
    if not ok:
        raise GuildMedalUpgradeError(
            "transaction",
            "guild_medal_transaction_reject=%s" % db_note,
            db_reason=db_note,
        )
    return GuildMedalUpgradeSettlement(
        target_item_id=target_item_id,
        target_slot=slot,
        old_level=old_level,
        new_level=new_level,
        result_code=result_code,
        roll=roll,
        rule=rule,
        detail=dict(detail),
        db_note=str(db_note),
    )


__all__ = [
    "GUILD_MEDAL_CONTRIBUTION_ITEM_ID",
    "GUILD_MEDAL_CONTRIBUTION_STACK_LIMIT",
    "GUILD_MEDAL_MAX_UPGRADE_LEVEL",
    "GUILD_MEDAL_UPGRADE_RULES",
    "GuildMedalFailureResult",
    "GuildMedalUpgradeError",
    "GuildMedalUpgradeRule",
    "GuildMedalUpgradeSettlement",
    "guild_medal_failure_result",
    "guild_medal_four_stat_bonus",
    "guild_medal_rule_for_target_level",
    "settle_guild_medal_upgrade",
]
