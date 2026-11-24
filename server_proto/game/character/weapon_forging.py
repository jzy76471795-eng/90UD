"""PVF-backed weapon forging configuration and persisted forging state."""
from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from pvf import pvf_runtime


WEAPON_FORGING_PVF_PATH = "etc/upgrade_separate.etc"
FORGING_LEVEL_KEY = "forging_level"
_LOCK = threading.RLock()
_CONFIG: "WeaponForgingConfig | None" = None
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _section(text: str, name: str) -> str:
    match = re.search(
        r"\[%s\]\s*(.*?)(?=\s*\[/%s\]|\s*\[[^/][^\]]*\]|\Z)"
        % (re.escape(name), re.escape(name)),
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise ValueError("upgrade_separate.etc missing section %s" % name)
    return match.group(1)


def _numbers(text: str) -> list[float]:
    return [float(value) for value in _NUMBER_RE.findall(text or "")]


def _positive_int(value: float) -> int:
    return max(0, int(math.floor(float(value))))


def parse_anti_evil_upgrade(
    text: str, *, required_levels: int,
) -> Tuple[Tuple[int, ...], ...] | None:
    """Read seven rarity columns per level, as native 02E58050 does.

    An absent section stays unknown. A present malformed table is rejected
    rather than replaced with independent attack or an invented coefficient.
    """
    if type(required_levels) is not int or required_levels < 1:
        raise ValueError("invalid forging anti-evil level count")
    starts = list(re.finditer(r"(?mi)^\s*\[anti evil upgrade\]\s*$", text))
    ends = list(re.finditer(r"(?mi)^\s*\[/anti evil upgrade\]\s*$", text))
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0].end() >= ends[0].start():
        raise ValueError("forging anti-evil requires one complete table")
    tokens = text[starts[0].end():ends[0].start()].split()
    if (not tokens or len(tokens) % 7 or len(tokens) // 7 < required_levels
            or any(re.fullmatch(r"\d+", value) is None for value in tokens)):
        raise ValueError("invalid forging anti-evil table shape")
    values = tuple(int(value) for value in tokens)
    if any(value > 0x7fffffff for value in values):
        raise ValueError("forging anti-evil exceeds signed integer")
    return tuple(values[offset:offset + 7] for offset in range(0, len(values), 7))


@dataclass(frozen=True)
class WeaponForgingTier:
    level: int
    independent_attack_base: int
    success_rate: int
    material_multiplier: float


@dataclass(frozen=True)
class WeaponForgingMaterial:
    grade: int
    item_id: int
    base_count: int


@dataclass(frozen=True)
class WeaponForgingConfig:
    tiers: Tuple[WeaponForgingTier, ...]
    max_level: int
    level_weight: float
    material_by_grade: Dict[int, WeaponForgingMaterial]
    item_weights_by_rarity: Tuple[float, ...]
    stat_weights_by_rarity: Tuple[float, ...]
    weapon_weights: Tuple[float, ...]
    anti_evil_by_level: Tuple[Tuple[int, ...], ...] | None = None

    def anti_evil_bonus(self, *, rarity: int, forging_level: int) -> int:
        """Positive forging contribution, native +8D4; not additive to +8CC.

        02E303B0 selects the map row by level, then indexes its rarity column.
        02D8EED0 takes the maximum of this and the upgrade contribution.
        """
        if (type(rarity) is not int or not 0 <= rarity < 7
                or type(forging_level) is not int
                or not 1 <= forging_level <= self.max_level
                or self.anti_evil_by_level is None
                or forging_level > len(self.anti_evil_by_level)):
            raise ValueError("native forging anti-evil policy or level unavailable")
        return self.anti_evil_by_level[forging_level - 1][rarity]

    def tier_for_target_level(self, target_level: int) -> WeaponForgingTier | None:
        index = int(target_level) - 1
        return self.tiers[index] if 0 <= index < len(self.tiers) else None

    def material_for(self, *, grade: int, target_level: int) -> tuple[int, int]:
        tier = self.tier_for_target_level(target_level)
        if tier is None:
            raise ValueError("forging tier missing for level %d" % int(target_level))
        wanted_grade = max(0, int(grade))
        material = self.material_by_grade.get(wanted_grade)
        if material is None:
            available = [key for key in self.material_by_grade if key <= wanted_grade]
            material = self.material_by_grade.get(max(available)) if available else None
        if material is None:
            raise ValueError("forging material missing for grade %d" % wanted_grade)
        # The frozen PVF specifies material count as grade-row base count times
        # table column 4, truncated toward zero.
        count = int(math.floor(material.base_count * tier.material_multiplier))
        return int(material.item_id), max(1, count)

    def independent_attack_for(
        self,
        *,
        minimum_level: int,
        rarity: int,
        forging_level: int,
    ) -> int:
        if int(forging_level) <= 0:
            return 0
        tier = self.tier_for_target_level(forging_level)
        if tier is None:
            return 0
        rarity_index = int(rarity)
        rarity_weight = (
            float(self.stat_weights_by_rarity[rarity_index])
            if 0 <= rarity_index < len(self.stat_weights_by_rarity)
            else 1.0
        )
        level_weight = float(self.level_weight)
        if level_weight <= 0:
            raise ValueError("forging level weight must be positive")
        value = (
            (1.0 + max(0, int(minimum_level)) / level_weight)
            * float(tier.independent_attack_base)
            * rarity_weight
        )
        return max(0, int(math.floor(value)))


def parse_weapon_forging_config(text: str) -> WeaponForgingConfig:
    table_values = _numbers(_section(text, "table"))
    if len(table_values) % 4:
        raise ValueError("upgrade_separate.etc table width mismatch")
    tiers: list[WeaponForgingTier] = []
    for offset in range(0, len(table_values), 4):
        values = table_values[offset:offset + 4]
        tier = WeaponForgingTier(
            level=len(tiers) + 1,
            independent_attack_base=_positive_int(values[0]),
            success_rate=_positive_int(values[2]),
            material_multiplier=float(values[3]),
        )
        if not 0 <= tier.success_rate <= 10000:
            raise ValueError("forging success rate out of range level=%d" % tier.level)
        if tier.material_multiplier <= 0:
            raise ValueError("forging material multiplier invalid level=%d" % tier.level)
        tiers.append(tier)

    max_values = _numbers(_section(text, "separate upgrade max"))
    level_weights = _numbers(_section(text, "level"))
    if not max_values or not level_weights:
        raise ValueError("upgrade_separate.etc max/level section empty")
    max_level = _positive_int(max_values[0])
    if max_level <= 0 or max_level > len(tiers):
        raise ValueError("forging max level outside table")
    level_weight = float(level_weights[0])
    if level_weight <= 0:
        raise ValueError("forging level weight must be positive")

    material_values = _numbers(_section(text, "item weights by grade"))
    if len(material_values) % 3:
        raise ValueError("forging material table width mismatch")
    material_by_grade: Dict[int, WeaponForgingMaterial] = {}
    for offset in range(0, len(material_values), 3):
        grade = _positive_int(material_values[offset])
        item_id = _positive_int(material_values[offset + 1])
        base_count = _positive_int(material_values[offset + 2])
        if grade <= 0 or item_id <= 0 or base_count <= 0:
            raise ValueError("forging material row invalid")
        material_by_grade[grade] = WeaponForgingMaterial(grade, item_id, base_count)

    item_weights = tuple(_numbers(_section(text, "item weights by rarity")))
    stat_weights = tuple(_numbers(_section(text, "stat weights by rarity")))
    weapon_weights = tuple(_numbers(_section(text, "weapon weights")))
    if not material_by_grade or not stat_weights or not weapon_weights:
        raise ValueError("forging weights missing")
    return WeaponForgingConfig(
        tiers=tuple(tiers),
        max_level=max_level,
        level_weight=level_weight,
        material_by_grade=material_by_grade,
        item_weights_by_rarity=item_weights,
        stat_weights_by_rarity=stat_weights,
        weapon_weights=weapon_weights,
        anti_evil_by_level=parse_anti_evil_upgrade(text, required_levels=max_level),
    )


def get_weapon_forging_config() -> WeaponForgingConfig:
    global _CONFIG
    with _LOCK:
        if _CONFIG is None:
            _CONFIG = parse_weapon_forging_config(
                pvf_runtime.read_text(WEAPON_FORGING_PVF_PATH)
            )
        return _CONFIG


def clear_weapon_forging_config_cache() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None


def warmup_weapon_forging_config() -> dict[str, int]:
    config = get_weapon_forging_config()
    return {
        "max_level": int(config.max_level),
        "tier_count": len(config.tiers),
        "material_grade_count": len(config.material_by_grade),
    }


def parse_forging_level(raw_extra_json: Any) -> int:
    try:
        value = json.loads(str(raw_extra_json or "{}"))
    except (TypeError, ValueError):
        return 0
    if not isinstance(value, dict):
        return 0
    try:
        return max(0, min(0xFF, int(value.get(FORGING_LEVEL_KEY, 0) or 0)))
    except (TypeError, ValueError):
        return 0


def merge_forging_level(raw_extra_json: Any, forging_level: int) -> str:
    try:
        value = json.loads(str(raw_extra_json or "{}"))
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    level = max(0, min(0xFF, int(forging_level)))
    if level:
        value[FORGING_LEVEL_KEY] = level
    else:
        value.pop(FORGING_LEVEL_KEY, None)
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FORGING_LEVEL_KEY", "WEAPON_FORGING_PVF_PATH", "WeaponForgingConfig",
    "WeaponForgingMaterial", "WeaponForgingTier", "clear_weapon_forging_config_cache",
    "get_weapon_forging_config", "merge_forging_level", "parse_forging_level",
    "parse_anti_evil_upgrade", "parse_weapon_forging_config", "warmup_weapon_forging_config",
]
