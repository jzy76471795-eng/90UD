# -*- coding: utf-8 -*-
"""PVF-backed configuration for Stage3 adventure dispatch and point shop."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import threading

from pvf import pvf_runtime


ADVENTURER_SYSTEM_PATH = "etc/adventurersystem/adventurersystem.etc"
MERCENARY_UNION_PATH = "etc/mercenaryunion.etc"


@dataclass(frozen=True)
class AdventureShopProduct:
    content_id: int
    required_adventure_level: int
    price: int
    purchase_limit: int
    reset_period: int


@dataclass(frozen=True)
class AdventureDispatchArea:
    area_index: int
    name: str
    required_adventure_level: int
    required_character_level: int
    minimum_points: int
    maximum_points: int
    duration_seconds: int
    base_reward_item_id: int
    base_reward_count: int
    special_reward_item_id: int
    special_reward_count: int
    add_value_item_ids: tuple[int, ...] = ()

    @property
    def rewards(self) -> tuple[tuple[int, int], ...]:
        """PVF item/value pairs; the values are divisors, not item quantities."""
        return (
            (self.base_reward_item_id, self.base_reward_count),
            (self.special_reward_item_id, self.special_reward_count),
        )


@dataclass(frozen=True)
class AdventureGroupFeatureIndex:
    shop_products: tuple[AdventureShopProduct, ...]
    maximum_shop_points: int
    shop_exp_per_award: int
    shop_points_per_award: int
    dispatch_areas: tuple[AdventureDispatchArea, ...]
    dispatch_daily_expectation: int
    dispatch_reward_shares: tuple[float, ...]

    def dispatch_rewards(self, area_index: int, committed_points: int, *, duration_seconds: int | None = None) -> tuple[tuple[int, int], ...]:
        """Match frozen-client 02F5A5B0/02F5A680, including float32 rounding."""
        area = self.dispatch_area(area_index)
        points = int(committed_points)
        if area is None or points <= 0:
            raise ValueError("invalid dispatch reward input")
        duration = area.duration_seconds if duration_seconds is None else int(duration_seconds)
        days, remainder = divmod(duration, 86400)
        if days <= 0 or remainder or (
            duration != area.duration_seconds and days not in (1, 3, 7, 10, 15, 20, 30)
        ):
            raise ValueError("invalid dispatch reward duration")
        multiplier = min(1.3, 1.0 - math.log10(area.minimum_points / points))
        expectation = int(days * self.dispatch_daily_expectation * multiplier)
        f32 = lambda value: struct.unpack("<f", struct.pack("<f", value))[0]
        rewards = tuple(
            (item_id, int(f32(f32(expectation) * f32(share))) // unit_value)
            for (item_id, unit_value), share in zip(area.rewards, self.dispatch_reward_shares)
        )
        if rewards[0][1] <= 0 or any(count < 0 or count > 0xFFFFFFFF for _, count in rewards):
            raise ValueError("invalid calculated dispatch reward quantity")
        return tuple((item_id, count) for item_id, count in rewards if count)

    def shop_product(self, content_id: int) -> AdventureShopProduct | None:
        target = int(content_id)
        return next(
            (entry for entry in self.shop_products if entry.content_id == target),
            None,
        )

    def dispatch_area(self, area_index: int) -> AdventureDispatchArea | None:
        target = int(area_index)
        return next(
            (entry for entry in self.dispatch_areas if entry.area_index == target),
            None,
        )

    def snapshot(self) -> dict[str, int | str]:
        return {
            "adventurer_path": ADVENTURER_SYSTEM_PATH,
            "mercenary_path": MERCENARY_UNION_PATH,
            "shop_product_count": len(self.shop_products),
            "maximum_shop_points": self.maximum_shop_points,
            "shop_exp_per_award": self.shop_exp_per_award,
            "shop_points_per_award": self.shop_points_per_award,
            "dispatch_area_count": len(self.dispatch_areas),
            "dispatch_add_value_count": sum(
                len(area.add_value_item_ids) for area in self.dispatch_areas
            ),
        }


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _is_tag(value: str) -> bool:
    return value.startswith("[") and value.endswith("]")


def _untick(value: str) -> str:
    text = str(value).strip()
    return text[1:-1] if len(text) >= 2 and text[0] == "`" and text[-1] == "`" else text


def _int(value: str, context: str) -> int:
    try:
        return int(str(value).strip(), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an integer: %r" % (context, value)) from exc


def _closed_sections(lines: list[str], tag: str) -> list[list[str]]:
    start_tag = "[%s]" % tag
    end_tag = "[/%s]" % tag
    sections: list[list[str]] = []
    position = 0
    while position < len(lines):
        if lines[position].lower() != start_tag.lower():
            position += 1
            continue
        try:
            end = next(
                index
                for index in range(position + 1, len(lines))
                if lines[index].lower() == end_tag.lower()
            )
        except StopIteration as exc:
            raise ValueError("%s has no closing tag" % start_tag) from exc
        sections.append(lines[position + 1 : end])
        position = end + 1
    return sections


def _scalar_values(lines: list[str], tag: str) -> list[str]:
    needle = "[%s]" % tag
    positions = [
        index for index, value in enumerate(lines) if value.lower() == needle.lower()
    ]
    if len(positions) != 1:
        raise ValueError("%s occurrence count=%d" % (needle, len(positions)))
    start = positions[0] + 1
    end = start
    while end < len(lines) and not _is_tag(lines[end]):
        end += 1
    values = lines[start:end]
    if not values:
        raise ValueError("%s has no values" % needle)
    return values


def _field_values(block: list[str], tag: str) -> list[str]:
    needle = "[%s]" % tag
    positions = [
        index for index, value in enumerate(block) if value.lower() == needle.lower()
    ]
    if len(positions) != 1:
        raise ValueError("dispatch %s occurrence count=%d" % (needle, len(positions)))
    start = positions[0] + 1
    end = start
    while end < len(block) and not _is_tag(block[end]):
        end += 1
    return block[start:end]


def _exact_int_values(block: list[str], tag: str, count: int) -> tuple[int, ...]:
    values = _field_values(block, tag)
    if len(values) != int(count):
        raise ValueError(
            "dispatch [%s] value count=%d expected=%d" % (tag, len(values), count)
        )
    return tuple(_int(value, "[%s]" % tag) for value in values)


def _parse_shop(text: str) -> tuple[tuple[AdventureShopProduct, ...], int, int, int]:
    lines = _lines(text)
    sections = _closed_sections(lines, "adventurer shop purchase info")
    if len(sections) != 1:
        raise ValueError(
            "[adventurer shop purchase info] section count=%d" % len(sections)
        )
    values = sections[0]
    if not values or len(values) % 5:
        raise ValueError("adventurer shop purchase rows are not five integers wide")
    products: list[AdventureShopProduct] = []
    seen: set[int] = set()
    for offset in range(0, len(values), 5):
        row = tuple(
            _int(value, "adventurer shop row") for value in values[offset : offset + 5]
        )
        product = AdventureShopProduct(*row)
        if product.content_id <= 0 or product.content_id > 0xFFFFFFFF:
            raise ValueError("invalid adventure shop content id=%d" % product.content_id)
        if product.content_id in seen:
            raise ValueError("duplicate adventure shop content id=%d" % product.content_id)
        if (
            product.required_adventure_level <= 0
            or product.price <= 0
            or product.purchase_limit <= 0
            or product.reset_period <= 0
            or product.reset_period > 0xFF
        ):
            raise ValueError("invalid adventure shop row=%r" % (row,))
        seen.add(product.content_id)
        products.append(product)

    maximum_values = _scalar_values(lines, "max adventurer shop point")
    if len(maximum_values) != 1:
        raise ValueError("[max adventurer shop point] must contain one integer")
    maximum_points = _int(maximum_values[0], "max adventurer shop point")
    award_values = _scalar_values(lines, "exp to get shop point")
    if len(award_values) != 2:
        raise ValueError("[exp to get shop point] must contain exp and point values")
    exp_per_award, points_per_award = (
        _int(value, "exp to get shop point") for value in award_values
    )
    if maximum_points <= 0 or maximum_points > 0xFFFFFFFF:
        raise ValueError("invalid maximum adventure shop points=%d" % maximum_points)
    if exp_per_award <= 0 or points_per_award <= 0:
        raise ValueError("invalid adventure shop exp award=%r" % (award_values,))
    return tuple(products), maximum_points, exp_per_award, points_per_award


def _parse_add_values(lines: list[str]) -> dict[int, tuple[int, ...]]:
    sections = _closed_sections(lines, "add value")
    if len(sections) != 1:
        raise ValueError("[add value] section count=%d" % len(sections))
    values = sections[0]
    result: dict[int, tuple[int, ...]] = {}
    position = 0
    while position < len(values):
        if position + 2 > len(values):
            raise ValueError("truncated mercenary [add value] header")
        area_index = _int(values[position], "add value area")
        item_count = _int(values[position + 1], "add value item count")
        position += 2
        if item_count < 0 or position + item_count > len(values):
            raise ValueError("invalid mercenary [add value] count=%d" % item_count)
        item_ids = tuple(
            _int(value, "add value item id")
            for value in values[position : position + item_count]
        )
        position += item_count
        if area_index in result or any(item_id <= 0 for item_id in item_ids):
            raise ValueError("invalid mercenary [add value] area=%d" % area_index)
        result[area_index] = item_ids
    return result


def _parse_dispatch(text: str) -> tuple[AdventureDispatchArea, ...]:
    lines = _lines(text)
    sections = _closed_sections(lines, "dungeon area info")
    if not sections:
        raise ValueError("mercenary dispatch has no [dungeon area info]")
    add_values = _parse_add_values(lines)
    areas: list[AdventureDispatchArea] = []
    for expected_index, block in enumerate(sections):
        if not block or _is_tag(block[0]):
            raise ValueError("dispatch area %d has no leading index" % expected_index)
        area_index = _int(block[0], "dispatch area index")
        name_values = _field_values(block, "name")
        if len(name_values) != 1 or not _untick(name_values[0]):
            raise ValueError("dispatch area %d has invalid name" % area_index)
        required_adventure_level = _exact_int_values(
            block, "adventure union level", 1
        )[0]
        required_character_level = _exact_int_values(block, "mercenary level", 1)[0]
        minimum_points, maximum_points = _exact_int_values(
            block, "mercenary union point", 2
        )
        duration_seconds = _exact_int_values(block, "compete period sec", 1)[0]
        base_reward_item_id, base_reward_count = _exact_int_values(
            block, "base reward", 2
        )
        special_reward_item_id, special_reward_count = _exact_int_values(
            block, "special reward", 2
        )
        if area_index != expected_index:
            raise ValueError(
                "dispatch area index=%d expected=%d" % (area_index, expected_index)
            )
        if (
            required_adventure_level <= 0
            or not 1 <= required_character_level <= 0xFF
            or minimum_points <= 0
            or maximum_points < minimum_points
            or duration_seconds <= 0
            or base_reward_item_id <= 0
            or base_reward_count <= 0
            or special_reward_item_id <= 0
            or special_reward_count <= 0
        ):
            raise ValueError("invalid mercenary dispatch area=%d" % area_index)
        areas.append(
            AdventureDispatchArea(
                area_index=area_index,
                name=_untick(name_values[0]),
                required_adventure_level=required_adventure_level,
                required_character_level=required_character_level,
                minimum_points=minimum_points,
                maximum_points=maximum_points,
                duration_seconds=duration_seconds,
                base_reward_item_id=base_reward_item_id,
                base_reward_count=base_reward_count,
                special_reward_item_id=special_reward_item_id,
                special_reward_count=special_reward_count,
                add_value_item_ids=add_values.get(area_index, ()),
            )
        )
    unknown_add_areas = sorted(set(add_values) - {area.area_index for area in areas})
    if unknown_add_areas:
        raise ValueError("mercenary [add value] has unknown areas=%r" % unknown_add_areas)
    return tuple(areas)


def parse_adventure_group_feature_index(
    adventurer_text: str,
    mercenary_text: str,
) -> AdventureGroupFeatureIndex:
    products, maximum_points, exp_per_award, points_per_award = _parse_shop(
        adventurer_text
    )
    lines = _lines(mercenary_text)
    expectation_values = _scalar_values(lines, "default expectation")
    if len(expectation_values) != 1:
        raise ValueError("[default expectation] must contain one integer")
    daily_expectation = _int(expectation_values[0], "default expectation")
    shares = tuple(float(value) for value in _scalar_values(lines, "reward share"))
    if daily_expectation <= 0 or len(shares) != 3 or any(not math.isfinite(value) or not 0 < value <= 1 for value in shares):
        raise ValueError("invalid mercenary reward expectation or shares")
    return AdventureGroupFeatureIndex(
        shop_products=products,
        maximum_shop_points=maximum_points,
        shop_exp_per_award=exp_per_award,
        shop_points_per_award=points_per_award,
        dispatch_areas=_parse_dispatch(mercenary_text),
        dispatch_daily_expectation=daily_expectation,
        dispatch_reward_shares=shares,
    )


_LOCK = threading.Lock()
_INDEX: AdventureGroupFeatureIndex | None = None


def get_adventure_group_feature_index() -> AdventureGroupFeatureIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = parse_adventure_group_feature_index(
                pvf_runtime.read_text(ADVENTURER_SYSTEM_PATH),
                pvf_runtime.read_text(MERCENARY_UNION_PATH),
            )
        return _INDEX


def clear_adventure_group_feature_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def warmup_adventure_group_feature_index() -> dict[str, int | str]:
    return dict(get_adventure_group_feature_index().snapshot())


__all__ = [
    "ADVENTURER_SYSTEM_PATH",
    "MERCENARY_UNION_PATH",
    "AdventureDispatchArea",
    "AdventureGroupFeatureIndex",
    "AdventureShopProduct",
    "clear_adventure_group_feature_index_cache",
    "get_adventure_group_feature_index",
    "parse_adventure_group_feature_index",
    "warmup_adventure_group_feature_index",
]
