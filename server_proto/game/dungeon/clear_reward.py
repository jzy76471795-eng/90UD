# -*- coding: utf-8 -*-
"""PVF-backed ordinary dungeon clear-card rewards.

The frozen Stage3 client owns the upper/lower card choice locally.  C1/0047
therefore carries only the selected side and local player id, not a card
index.  This module produces one immutable-per-run reward set for that
protocol shape:

* free side: one PVF gold roll plus one optional configured equipment;
* paid side: one optional configured equipment and its PVF gold cost.

``etc/itemdropinfo_clearreward.etc`` is the authoritative source.  The
existing prewarmed gold and equipment pools provide the actual candidate
items; no runtime path reads ``data/pvf/unpack``.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from game.character.item_metadata import resolve_item_metadata
from game.drop.dnf_lcg import DnfLcg
from game.drop.equip_stackable_pools import get_equip_stackable_pools
from game.drop.gold_table import get_gold_table
from game.drop.pvf_section import extract_section, parse_floats, parse_ints
from pvf import pvf_runtime


CLEAR_REWARD_PATH = "etc/itemdropinfo_clearreward.etc"
CARD_ROLL_DENOMINATOR = 10000
RARITY_ROLL_DENOMINATOR = 1000000
ORDINARY_CARD_LAYOUT = "ordinary_4x4"
ORDINARY_PAID_CARD_REWARD_MULTIPLIER = 2


@dataclass(frozen=True)
class ClearRewardDropBand:
    level_min: int
    level_max: int
    probability: int
    auxiliary_value: int


@dataclass(frozen=True)
class ClearRewardGradeWindow:
    down: int
    up: int


@dataclass(frozen=True)
class ClearRewardPlan:
    """Frozen server-side rewards for one ordinary clear settlement."""

    free_gold: int = 0
    free_item_id: int = 0
    free_item_count: int = 0
    paid_gold: int = 0
    paid_item_id: int = 0
    paid_item_count: int = 0
    paid_cost: int = 0
    seed_before: int = 0
    seed_after: int = 0

    def as_session_dict(
        self, *, free_delivered: bool = False
    ) -> Dict[str, Union[int, bool, str]]:
        """Return only simple mutable values suitable for ``GameSessionState``."""
        return {
            # This plan is only for the ordinary upper-four/lower-four card
            # layout.  The multiplier is frozen with the PVF roll so other
            # dungeon result-card implementations do not inherit it.
            "schema": 2,
            "card_layout": ORDINARY_CARD_LAYOUT,
            "paid_reward_multiplier": ORDINARY_PAID_CARD_REWARD_MULTIPLIER,
            "free_gold": max(0, int(self.free_gold)),
            "free_item_id": max(0, int(self.free_item_id)),
            "free_item_count": max(0, int(self.free_item_count)),
            "paid_gold": max(0, int(self.paid_gold)),
            "paid_item_id": max(0, int(self.paid_item_id)),
            "paid_item_count": max(0, int(self.paid_item_count)),
            "paid_cost": max(0, int(self.paid_cost)),
            "free_gold_delivered": bool(free_delivered),
            "free_item_delivered": bool(free_delivered),
            "free_delivered": bool(free_delivered),
            "paid_cost_charged": False,
            "paid_item_delivered": False,
            "paid_delivered": False,
            "seed_before": int(self.seed_before) & 0xFFFFFFFF,
            "seed_after": int(self.seed_after) & 0xFFFFFFFF,
        }


class ClearRewardConfig:
    """Parsed subset of Stage3 ``itemdropinfo_clearreward.etc``.

    The current PVF also contains specialized dungeon/event sections.  They
    stay out of the ordinary one-player card flow until their client state
    and dungeon selectors are independently established.
    """

    def __init__(self) -> None:
        self.drop_profiles: Dict[str, Tuple[ClearRewardDropBand, ...]] = {}
        self.drop_profile_row_count = 0
        self.item_type_weights: Tuple[int, ...] = ()
        self.difficulty_item_bonuses: Tuple[int, ...] = ()
        self.rarity_thresholds: Tuple[int, ...] = ()
        self.difficulty_gold_rates: Tuple[float, ...] = ()
        self.map_count_rates: Dict[int, int] = {}
        self.paid_card_costs: Dict[int, int] = {}
        self.paid_card_create_rate: float = 0.0
        self.grade_windows: Dict[int, ClearRewardGradeWindow] = {}
        self.loaded = False

    def load_from_text(self, text: str) -> None:
        profile_count_values = parse_ints(
            extract_section(text, "drop prob count")
        )
        if len(profile_count_values) != 1 or int(profile_count_values[0]) <= 0:
            raise RuntimeError("clearreward [drop prob count] shape invalid")
        profile_row_count = int(profile_count_values[0])
        profiles = _parse_drop_profiles(
            extract_section(text, "drop prob"),
            profile_row_count,
        )
        if "default" not in profiles or "event" not in profiles:
            raise RuntimeError("clearreward [drop prob] lacks default/event profiles")

        item_weights = tuple(
            max(0, int(value))
            for value in parse_ints(extract_section(text, "drop item type prob"))
        )
        if not item_weights or sum(item_weights) <= 0:
            raise RuntimeError("clearreward [drop item type prob] empty")

        rarity_thresholds = tuple(
            max(0, int(value))
            for value in parse_ints(extract_section(text, "basis of rarity dicision"))
        )
        if not rarity_thresholds:
            raise RuntimeError("clearreward [basis of rarity dicision] empty")
        previous = -1
        for threshold in rarity_thresholds:
            if threshold < previous:
                raise RuntimeError("clearreward rarity thresholds are not cumulative")
            previous = threshold

        difficulty_bonus = tuple(
            max(0, int(value))
            for value in parse_ints(
                extract_section(text, "dungeon difficulty drop bonusrate")
            )
        )
        if not difficulty_bonus:
            raise RuntimeError("clearreward difficulty item bonus empty")

        gold_rates = tuple(
            max(0.0, float(value))
            for value in parse_floats(
                extract_section(text, "dungeon difficulty gold drop bonusrate")
            )
        )
        if not gold_rates:
            raise RuntimeError("clearreward difficulty gold rate empty")

        map_rates = _parse_pairs(
            parse_ints(extract_section(text, "reward item rate per map max count")),
            "reward item rate per map max count",
        )
        paid_costs = _parse_pairs(
            parse_ints(extract_section(text, "gold card cost table")),
            "gold card cost table",
        )
        if not paid_costs:
            raise RuntimeError("clearreward [gold card cost table] empty")
        create_rates = parse_floats(extract_section(text, "gold card create rate"))
        if not create_rates:
            raise RuntimeError("clearreward [gold card create rate] empty")
        grade_windows = _parse_grade_windows(
            parse_ints(extract_section(text, "item drop ref table"))
        )
        if not grade_windows:
            raise RuntimeError("clearreward [item drop ref table] empty")

        self.drop_profiles = profiles
        self.drop_profile_row_count = profile_row_count
        self.item_type_weights = item_weights
        self.difficulty_item_bonuses = difficulty_bonus
        self.rarity_thresholds = rarity_thresholds
        self.difficulty_gold_rates = gold_rates
        self.map_count_rates = map_rates
        self.paid_card_costs = paid_costs
        self.paid_card_create_rate = max(0.0, float(create_rates[0]))
        self.grade_windows = grade_windows
        self.loaded = True

    def drop_probability(self, profile: str, level: int) -> int:
        bands = self.drop_profiles.get(str(profile or "").strip().lower(), ())
        target = max(1, int(level))
        for band in bands:
            if int(band.level_min) <= target <= int(band.level_max):
                return max(0, int(band.probability))
        return 0

    def difficulty_item_bonus(self, difficulty: int) -> int:
        return _indexed_int(self.difficulty_item_bonuses, difficulty, 0)

    def difficulty_gold_rate(self, difficulty: int) -> float:
        return _indexed_float(self.difficulty_gold_rates, difficulty, 1.0)

    def exploration_item_rate(self, total_room_count: int, visited_room_count: int) -> float:
        total = max(1, int(total_room_count))
        visited = max(0, min(total, int(visited_room_count)))
        explored_ratio = float(visited) / float(total)
        configured = self.map_count_rates.get(total)
        if configured is None:
            return explored_ratio
        return max(0.0, float(configured) * explored_ratio / CARD_ROLL_DENOMINATOR)

    def paid_card_cost(self, level: int) -> int:
        return _closest_not_greater(self.paid_card_costs, level)

    def grade_window(self, level: int) -> ClearRewardGradeWindow:
        if not self.grade_windows:
            return ClearRewardGradeWindow(0, 0)
        target = max(1, int(level))
        if target in self.grade_windows:
            return self.grade_windows[target]
        if any(key <= target for key in self.grade_windows):
            best_key = max(key for key in self.grade_windows if key <= target)
            return self.grade_windows[best_key]
        return self.grade_windows[min(self.grade_windows)]

    def roll_item_type(self, lcg: DnfLcg) -> int:
        total = sum(max(0, int(value)) for value in self.item_type_weights)
        if total <= 0:
            return 0
        roll = int(lcg.next(total))
        cumulative = 0
        for index, weight in enumerate(self.item_type_weights, start=1):
            cumulative += max(0, int(weight))
            if roll < cumulative:
                return index
        return 0

    def roll_rarity(self, lcg: DnfLcg) -> int:
        roll = int(lcg.next(RARITY_ROLL_DENOMINATOR)) + 1
        for rarity, threshold in enumerate(self.rarity_thresholds):
            if roll <= max(0, int(threshold)):
                return int(rarity)
        return 0


def _parse_drop_profiles(
    section: str, row_count: int
) -> Dict[str, Tuple[ClearRewardDropBand, ...]]:
    """Parse the current PVF's labelled four-column clear-card rows.

    Stage3's ``itemdropinfo_clearreward.etc`` declares its row count in
    ``[drop prob count]``.  Each profile then has exactly that many rows of
    ``level_min level_max probability auxiliary``.  The fourth value is
    retained and validated, but it is not assigned a gameplay meaning until
    its Stage3 consumer path is independently closed.
    """
    expected_rows = max(0, int(row_count))
    if expected_rows <= 0:
        raise RuntimeError("clearreward [drop prob count] must be positive")

    result: Dict[str, Tuple[ClearRewardDropBand, ...]] = {}
    current_name: Optional[str] = None
    current_values: List[int] = []

    def flush() -> None:
        nonlocal current_values
        if not current_name:
            current_values = []
            return
        expected_values = expected_rows * 4
        if len(current_values) != expected_values:
            raise RuntimeError(
                "clearreward [drop prob] profile %s has %d values; expected %d"
                % (current_name, len(current_values), expected_values)
            )
        rows: List[ClearRewardDropBand] = []
        for index in range(0, len(current_values), 4):
            lo, hi, probability, auxiliary_value = current_values[index : index + 4]
            if lo <= 0 or hi < lo:
                raise RuntimeError(
                    "clearreward [drop prob] invalid range %s..%s" % (lo, hi)
                )
            rows.append(
                ClearRewardDropBand(
                    lo,
                    hi,
                    max(0, int(probability)),
                    int(auxiliary_value),
                )
            )
        if current_name in result:
            raise RuntimeError(
                "clearreward [drop prob] duplicate profile %s" % current_name
            )
        result[current_name] = tuple(rows)
        current_values = []

    for match in re.finditer(r"`([^`]*)`|(-?\d+)", str(section or "")):
        label = match.group(1)
        integer = match.group(2)
        if label is not None:
            flush()
            current_name = str(label).strip().lower()
        elif integer is not None:
            if current_name is None:
                raise RuntimeError("clearreward [drop prob] value before profile label")
            current_values.append(int(integer))
    flush()
    return result


def _parse_pairs(values: Sequence[int], label: str) -> Dict[int, int]:
    if len(values) % 2:
        raise RuntimeError("clearreward [%s] has odd value count" % label)
    result: Dict[int, int] = {}
    for index in range(0, len(values), 2):
        key = int(values[index])
        value = int(values[index + 1])
        if key <= 0:
            continue
        result[key] = max(0, value)
    return result


def _parse_grade_windows(values: Sequence[int]) -> Dict[int, ClearRewardGradeWindow]:
    if len(values) % 3:
        raise RuntimeError("clearreward [item drop ref table] shape invalid")
    result: Dict[int, ClearRewardGradeWindow] = {}
    for index in range(0, len(values), 3):
        level = int(values[index])
        if level <= 0:
            continue
        result[level] = ClearRewardGradeWindow(
            max(0, int(values[index + 1])),
            max(0, int(values[index + 2])),
        )
    return result


def _indexed_int(values: Sequence[int], index: int, fallback: int) -> int:
    target = int(index)
    if target < 0 or target >= len(values):
        return int(fallback)
    return int(values[target])


def _indexed_float(values: Sequence[float], index: int, fallback: float) -> float:
    target = int(index)
    if target < 0 or target >= len(values):
        return float(fallback)
    return float(values[target])


def _closest_not_greater(values: Mapping[int, int], level: int) -> int:
    if not values:
        return 0
    target = max(1, int(level))
    eligible = [key for key in values if int(key) <= target]
    if eligible:
        return max(0, int(values[max(eligible)]))
    return max(0, int(values[min(values)]))


def _is_main_inventory_equipment(item_id: int) -> bool:
    """Avoid an unverified list1/avatar result path in the ordinary card UI."""
    try:
        meta = resolve_item_metadata(int(item_id))
    except Exception:
        return False
    return bool(
        int(item_id) > 0
        and bool(getattr(meta, "is_equipment", False))
        and not bool(getattr(meta, "is_avatar", False))
        and not bool(getattr(meta, "is_pet_inventory_item", False))
    )


def _roll_configured_equipment(
    config: ClearRewardConfig,
    *,
    dungeon_level: int,
    lcg: DnfLcg,
) -> Tuple[int, int]:
    """Roll the PVF type/rarity/window and return one supported main item."""
    item_type = config.roll_item_type(lcg)
    rarity = config.roll_rarity(lcg)
    # Current Stage3 table assigns type2=ordinary equipment and type4=avatar.
    # The latter needs a separate card/list1 client presentation chain, so it
    # stays empty rather than being misfiled as a normal equipment reward.
    if item_type != 2:
        return 0, 0
    window = config.grade_window(dungeon_level)
    if window.up <= 0:
        return 0, 0
    pools = get_equip_stackable_pools()
    # Candidate pools can include out-of-band templates in customised PVFs.
    # Consume a bounded number of normal candidate rolls and only select items
    # that the existing metadata index will place in main inventory.
    for _ in range(8):
        item_id = int(
            pools.choose_equipment_in_window(
                lcg,
                max(1, int(dungeon_level)),
                int(rarity),
                down=int(window.down),
                up=int(window.up),
            )
            or 0
        )
        if item_id > 0 and _is_main_inventory_equipment(item_id):
            return item_id, 1
    return 0, 0


def _roll_item_card(
    config: ClearRewardConfig,
    *,
    profile: str,
    dungeon_level: int,
    difficulty: int,
    create_rate: float,
    lcg: DnfLcg,
) -> Tuple[int, int]:
    base_probability = config.drop_probability(profile, dungeon_level)
    threshold = (
        float(base_probability) * max(0.0, float(create_rate))
        + float(config.difficulty_item_bonus(difficulty))
    )
    threshold_i = max(0, min(CARD_ROLL_DENOMINATOR, int(math.floor(threshold))))
    if threshold_i <= 0 or int(lcg.next(CARD_ROLL_DENOMINATOR)) >= threshold_i:
        return 0, 0
    return _roll_configured_equipment(
        config,
        dungeon_level=dungeon_level,
        lcg=lcg,
    )


def _roll_free_gold(
    config: ClearRewardConfig,
    *,
    dungeon_level: int,
    difficulty: int,
    lcg: DnfLcg,
) -> int:
    """Use the PVF gold reference range and clearreward difficulty rate."""
    reference = get_gold_table().get(max(1, int(dungeon_level)))
    low = max(0, int(reference.gold_min))
    high = max(low, int(reference.gold_max))
    raw = low if high == low else low + int(lcg.next(high - low + 1))
    amount = int(math.floor(float(raw) * config.difficulty_gold_rate(difficulty)))
    return max(0, amount)


def build_clear_reward_plan(
    *,
    dungeon_level: int,
    difficulty: int,
    visited_room_count: int,
    total_room_count: int,
    seed: int,
) -> ClearRewardPlan:
    """Generate the ordinary solo free/paid reward set exactly once per run."""
    config = get_clear_reward_config()
    seed_before = int(seed) & 0xFFFFFFFF
    lcg = DnfLcg(seed_before or 1)
    level = max(1, min(200, int(dungeon_level)))
    diff = max(0, int(difficulty))

    free_gold = _roll_free_gold(
        config,
        dungeon_level=level,
        difficulty=diff,
        lcg=lcg,
    )
    free_item_id, free_item_count = _roll_item_card(
        config,
        profile="default",
        dungeon_level=level,
        difficulty=diff,
        create_rate=config.exploration_item_rate(
            total_room_count,
            visited_room_count,
        ),
        lcg=lcg,
    )
    paid_item_id, paid_item_count = _roll_item_card(
        config,
        profile="event",
        dungeon_level=level,
        difficulty=diff,
        create_rate=config.paid_card_create_rate,
        lcg=lcg,
    )
    paid_cost = (
        config.paid_card_cost(level) if paid_item_id > 0 and paid_item_count > 0 else 0
    )
    return ClearRewardPlan(
        free_gold=free_gold,
        free_item_id=free_item_id,
        free_item_count=free_item_count,
        paid_gold=0,
        paid_item_id=paid_item_id,
        paid_item_count=paid_item_count,
        paid_cost=paid_cost,
        seed_before=seed_before,
        seed_after=int(lcg.seed),
    )


_LOCK = threading.RLock()
_CONFIG: Optional[ClearRewardConfig] = None


def get_clear_reward_config() -> ClearRewardConfig:
    with _LOCK:
        if _CONFIG is None or not _CONFIG.loaded:
            raise RuntimeError("ClearRewardConfig not warmed; call boot warmup first")
        return _CONFIG


def warmup_clear_reward_index() -> Dict[str, Union[int, str]]:
    """Load ordinary clear-card metadata through the shared preloaded PVF."""
    global _CONFIG
    config = ClearRewardConfig()
    config.load_from_text(pvf_runtime.read_text(CLEAR_REWARD_PATH))
    with _LOCK:
        _CONFIG = config
    return {
        "ok": 1,
        "path": CLEAR_REWARD_PATH,
        "profiles": len(config.drop_profiles),
        "drop_profile_rows": int(config.drop_profile_row_count),
        "item_types": len(config.item_type_weights),
        "rarities": len(config.rarity_thresholds),
        "grade_windows": len(config.grade_windows),
        "paid_cost_levels": len(config.paid_card_costs),
    }


def clear_clear_reward_index() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None
