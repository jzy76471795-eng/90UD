# -*- coding: utf-8 -*-
"""PVF-backed Stage3 adventurer-system progression and stat attributes.

The frozen client consumes one account-wide level from USERINFO1/S0/00C1 and
resolves the configured effects locally.  The authoritative table for that
level is ``etc/adventurersystem/adventurersystem.etc``.  The older
``etc/linksystem/charactermanage.etc`` table belongs to the superseded
management-point system and is intentionally not used here.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Iterable, Tuple

from pvf import pvf_runtime


ADVENTURER_SYSTEM_PATH = "etc/adventurersystem/adventurersystem.etc"
CN_TRAITS_PATH = "etc/adventurersystem/adventurersystem2018.etc"
HONOR_LEVEL_PATH = "etc/honorlevel.etc"
_MAX_LEVEL = "max adventure level"
_EXP_RATE = "adventure exp rate"
_EXP_TABLE = "adventure exp table"
_HONOR_MAX_EXP = "maxexp on maxlevel"
_EXPERTISE_EXP_TABLE = "honor expert exp table"
_SCRIPT_TOKEN_RE = re.compile(r"`([^`]*)`|([-+]?\d+(?:\.\d+)?)")
_STAT_NAMES = ("str", "con", "int", "wis")
_BENEFIT_FIELD_BY_KEY = {
    ("grow", "clear result exp up"): "clear_result_exp_percent",
    ("item", "set chronicle"): "chronicle_drop_percent",
    ("item", "ancient material count"): "ancient_material_bonus_count",
    ("item", "antonium drop"): "antonium_extra_one_percent",
    ("item", "epic piece drop"): "epic_piece_bonus_count",
    ("item", "root of light drop"): "light_source_extra_one_percent",
    ("etc", "add anton raid enter count"): "anton_raid_extra_entries",
    ("etc", "add tower of despair enter count"): "tower_despair_extra_entries",
    ("etc", "add tower of grief enter count"): "tower_grief_extra_entries",
    ("etc", "add week dungeon enter count"): "weekly_dungeon_extra_entries",
}
_ATTRIBUTE_TAGS = {
    "stat",
    "grow",
    "item",
    "etc",
    "stat view",
    "grow view",
    "item view",
    "etc view",
}


@dataclass(frozen=True)
class AdventureGroupAttributes:
    strength: int = 0
    vitality: int = 0
    intelligence: int = 0
    spirit: int = 0


@dataclass(frozen=True)
class AdventureGroupBenefits:
    strength: int = 0
    vitality: int = 0
    intelligence: int = 0
    spirit: int = 0
    clear_result_exp_percent: int = 0
    chronicle_drop_percent: int = 0
    ancient_material_bonus_count: int = 0
    antonium_extra_one_percent: int = 0
    epic_piece_bonus_count: int = 0
    light_source_extra_one_percent: int = 0
    anton_raid_extra_entries: int = 0
    tower_despair_extra_entries: int = 0
    tower_grief_extra_entries: int = 0
    weekly_dungeon_extra_entries: int = 0
    weekly_dungeon_extra_characters: int = 0
    guild_war_shop_item_id: int = 0
    guild_war_shop_extra_purchases: int = 0

    @property
    def attributes(self) -> AdventureGroupAttributes:
        return AdventureGroupAttributes(
            strength=self.strength,
            vitality=self.vitality,
            intelligence=self.intelligence,
            spirit=self.spirit,
        )


_BENEFIT_FIELD_NAMES = tuple(AdventureGroupBenefits.__dataclass_fields__)


@dataclass(frozen=True)
class AdventureGroupSummary:
    total_character_exp: int
    adventure_exp: int
    adventure_level: int
    attributes: AdventureGroupAttributes

    @property
    def manage_level(self) -> int:
        """Compatibility alias for the Stage3 wire field's historical name."""
        return self.adventure_level

    def describe(self) -> str:
        return (
            "adventure_group character_exp=%d exp=%d level=%d "
            "str=%d vit=%d int=%d spr=%d"
            % (
                self.total_character_exp,
                self.adventure_exp,
                self.adventure_level,
                self.attributes.strength,
                self.attributes.vitality,
                self.attributes.intelligence,
                self.attributes.spirit,
            )
        )


@dataclass(frozen=True)
class AdventureGroupIndex:
    max_adventure_level: int
    adventure_exp_rate: float
    exp_thresholds: Tuple[Tuple[int, int], ...]
    attributes_by_level: Tuple[Tuple[int, AdventureGroupAttributes], ...]
    benefits_by_level: Tuple[Tuple[int, AdventureGroupBenefits], ...]
    honor_max_exp_on_max_level: int = 0
    expertise_exp_requirements: Tuple[int, ...] = ()

    def level_for_exp(self, adventure_exp: int) -> int:
        value = int(adventure_exp)
        if value < 0:
            raise ValueError("adventure-group exp must be non-negative: %r" % adventure_exp)
        level = 1
        for row_level, threshold in self.exp_thresholds:
            if row_level > self.max_adventure_level or value < threshold:
                break
            level = row_level
        return min(level, self.max_adventure_level)

    def attributes_for_level(self, adventure_level: int) -> AdventureGroupAttributes:
        return self.benefits_for_level(adventure_level).attributes

    def benefits_for_level(self, adventure_level: int) -> AdventureGroupBenefits:
        level = max(1, min(int(adventure_level), self.max_adventure_level))
        for row_level, benefits in self.benefits_by_level:
            if row_level == level:
                return benefits
            if row_level > level:
                break
        return AdventureGroupBenefits()

    def expertise_level_for_exp(self, expertise_exp: int) -> int:
        """Resolve completed Expertise levels from PVF per-level requirements."""
        level, _current_level_exp = self.expertise_progress_for_exp(expertise_exp)
        return level

    def expertise_progress_for_exp(self, expertise_exp: int) -> Tuple[int, int]:
        """Return ``(completed_level, current_level_exp)`` for the Stage3 HUD.

        ``characters.expertise_exp`` is cumulative, while the client stores the
        EXP already earned inside the next Expertise level.  Once every PVF row
        is complete, the level stays at the final row and the step EXP is zero.
        """
        remaining = max(0, int(expertise_exp))
        level = 0
        for next_level, required_exp in enumerate(
            self.expertise_exp_requirements,
            start=1,
        ):
            if remaining < required_exp:
                return level, remaining
            remaining -= required_exp
            level = next_level
        return level, 0

    @staticmethod
    def minimum_character_exp(level: int, thresholds: Tuple[int, ...]) -> int:
        character_level = int(level)
        if character_level <= 1:
            return 0
        if not thresholds:
            raise ValueError("character exp threshold table is empty")
        index = min(character_level - 2, len(thresholds) - 1)
        return max(0, int(thresholds[index]))

    def calculate_progress(
        self,
        progress_rows: Iterable[Tuple[int, int]],
        character_exp_thresholds: Iterable[int],
    ) -> AdventureGroupSummary:
        thresholds = tuple(int(value) for value in character_exp_thresholds)
        total_character_exp = 0
        for level, stored_exp in progress_rows:
            minimum_exp = self.minimum_character_exp(int(level), thresholds)
            total_character_exp += max(minimum_exp, max(0, int(stored_exp)))
        adventure_exp = int(total_character_exp * self.adventure_exp_rate)
        adventure_level = self.level_for_exp(adventure_exp)
        return AdventureGroupSummary(
            total_character_exp=total_character_exp,
            adventure_exp=adventure_exp,
            adventure_level=adventure_level,
            attributes=self.attributes_for_level(adventure_level),
        )

    def snapshot(self) -> dict[str, int | float]:
        stat_levels = sum(
            1
            for _level, attributes in self.attributes_by_level
            if attributes != AdventureGroupAttributes()
        )
        return {
            "max_adventure_level": self.max_adventure_level,
            "adventure_exp_rate": self.adventure_exp_rate,
            "exp_threshold_rows": len(self.exp_thresholds),
            "attribute_levels": len(self.attributes_by_level),
            "benefit_levels": len(self.benefits_by_level),
            "stat_attribute_levels": stat_levels,
            "honor_max_exp_on_max_level": self.honor_max_exp_on_max_level,
            "expertise_exp_rows": len(self.expertise_exp_requirements),
            "expertise_max_level": len(self.expertise_exp_requirements),
        }


def _section(
    text: str,
    name: str,
    *,
    require_close: bool,
    path: str = ADVENTURER_SYSTEM_PATH,
) -> str:
    escaped = re.escape(name)
    opens = list(re.finditer(r"\[%s\]" % escaped, text, flags=re.IGNORECASE))
    if len(opens) != 1:
        raise ValueError(
            "%s section [%s] count=%d" % (path, name, len(opens))
        )
    start = opens[0].end()
    close = re.search(r"\[/%s\]" % escaped, text[start:], flags=re.IGNORECASE)
    if close is not None:
        return text[start : start + close.start()]
    if require_close:
        raise ValueError("%s section [%s] has no close" % (path, name))
    next_tag = re.search(r"\[[^\]\r\n]+\]", text[start:])
    end = start + next_tag.start() if next_tag is not None else len(text)
    return text[start:end]


def _script_tokens(text: str) -> Tuple[str, ...]:
    values = []
    for match in _SCRIPT_TOKEN_RE.finditer(text or ""):
        quoted, numeric = match.groups()
        values.append(quoted if quoted is not None else numeric)
    return tuple(values)


def _single_number(
    text: str,
    name: str,
    cast,
    *,
    path: str = ADVENTURER_SYSTEM_PATH,
):
    values = _script_tokens(
        _section(text, name, require_close=False, path=path)
    )
    if len(values) != 1:
        raise ValueError("[%s] must contain one value, got %r" % (name, values))
    try:
        return cast(values[0])
    except (TypeError, ValueError):
        raise ValueError("[%s] value is invalid: %r" % (name, values[0]))


def _parse_honor_expertise(text: str) -> Tuple[int, Tuple[int, ...]]:
    source = str(text or "")
    if not source.strip():
        raise ValueError("%s is empty" % HONOR_LEVEL_PATH)
    max_exp = _single_number(
        source,
        _HONOR_MAX_EXP,
        int,
        path=HONOR_LEVEL_PATH,
    )
    if not 0 <= max_exp <= 0xFFFFFFFF:
        raise ValueError("[%s] must fit u32: %r" % (_HONOR_MAX_EXP, max_exp))

    values = _script_tokens(
        _section(
            source,
            _EXPERTISE_EXP_TABLE,
            require_close=True,
            path=HONOR_LEVEL_PATH,
        )
    )
    if not values or len(values) % 2:
        raise ValueError("[%s] must contain level/exp pairs" % _EXPERTISE_EXP_TABLE)
    requirements = []
    previous_exp = 0
    for offset in range(0, len(values), 2):
        try:
            level = int(values[offset], 10)
            required_exp = int(values[offset + 1], 10)
        except ValueError:
            raise ValueError(
                "[%s] pair %d is not integer"
                % (_EXPERTISE_EXP_TABLE, offset // 2)
            )
        expected_level = offset // 2 + 1
        if (
            level != expected_level
            or required_exp <= 0
            or required_exp < previous_exp
            or required_exp > 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(
                "[%s] invalid pair level=%d exp=%d expected_level=%d"
                % (_EXPERTISE_EXP_TABLE, level, required_exp, expected_level)
            )
        requirements.append(required_exp)
        previous_exp = required_exp
    return max_exp, tuple(requirements)


def _parse_exp_thresholds(text: str) -> Tuple[Tuple[int, int], ...]:
    values = _script_tokens(_section(text, _EXP_TABLE, require_close=True))
    if not values or len(values) % 2:
        raise ValueError("[%s] must contain level/threshold pairs" % _EXP_TABLE)
    rows = []
    previous_level = 0
    previous_threshold = -1
    for offset in range(0, len(values), 2):
        try:
            level = int(values[offset], 10)
            threshold = int(values[offset + 1], 10)
        except ValueError:
            raise ValueError("[%s] pair %d is not integer" % (_EXP_TABLE, offset // 2))
        if level != previous_level + 1 or threshold < 0 or threshold <= previous_threshold:
            if not (level == 1 and threshold == 0 and previous_level == 0):
                raise ValueError(
                    "[%s] invalid pair level=%d threshold=%d after %d/%d"
                    % (_EXP_TABLE, level, threshold, previous_level, previous_threshold)
                )
        rows.append((level, threshold))
        previous_level = level
        previous_threshold = threshold
    if rows[0] != (1, 0):
        raise ValueError("[%s] must start with level 1 threshold 0" % _EXP_TABLE)
    return tuple(rows)


def _tag_bodies(text: str, tag: str) -> Tuple[str, ...]:
    escaped = re.escape(tag)
    pattern = re.compile(
        r"\[%s\](.*?)(?=\[[^\]\r\n]+\]|\Z)" % escaped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return tuple(match.group(1) for match in pattern.finditer(text or ""))


def _parse_benefits(
    text: str,
    max_adventure_level: int,
    *,
    cn_traits: bool = False,
) -> Tuple[Tuple[int, AdventureGroupBenefits], ...]:
    blocks = tuple(
        match.group(1)
        for match in re.finditer(
            r"\[attribute\](.*?)\[/attribute\]",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if len(blocks) != max_adventure_level:
        raise ValueError(
            "[attribute] count=%d does not match max level=%d"
            % (len(blocks), max_adventure_level)
        )
    rows = []
    previous = AdventureGroupBenefits()
    for expected_level, block in enumerate(blocks, start=1):
        prefix = block.split("[", 1)[0]
        prefix_values = _script_tokens(prefix)
        if len(prefix_values) != 1 or int(prefix_values[0], 10) != expected_level:
            raise ValueError(
                "[attribute] row=%d has invalid level prefix=%r"
                % (expected_level, prefix_values)
            )
        tag_source = re.sub(r"`[^`]*`", "", block)
        tags = {
            match.group(1).strip().lower()
            for match in re.finditer(r"\[([^\]/][^\]]*)\]", tag_source)
        }
        unknown_tags = tags - _ATTRIBUTE_TAGS
        if unknown_tags:
            raise ValueError(
                "[attribute] level=%d unknown tags=%r"
                % (expected_level, sorted(unknown_tags))
            )

        values_by_field = {}
        stats = {}
        for stat_body in _tag_bodies(block, "stat"):
            tokens = _script_tokens(stat_body)
            if len(tokens) != 5:
                raise ValueError(
                    "[attribute] level=%d [stat] tokens=%r" % (expected_level, tokens)
                )
            try:
                required_level = int(tokens[0], 10)
                value = int(tokens[2], 10)
                int(tokens[4], 10)
            except ValueError:
                raise ValueError(
                    "[attribute] level=%d [stat] numeric field invalid" % expected_level
                )
            stat_name = tokens[1].strip().lower()
            if (
                stat_name not in _STAT_NAMES
                or stat_name in stats
                or not 1 <= required_level <= expected_level
                or value < 0
            ):
                raise ValueError(
                    "[attribute] level=%d invalid stat=%r required=%d value=%d"
                    % (expected_level, stat_name, required_level, value)
                )
            stats[stat_name] = value
        values_by_field.update(
            {
                "strength": stats.get("str", 0),
                "vitality": stats.get("con", 0),
                "intelligence": stats.get("int", 0),
                "spirit": stats.get("wis", 0),
            }
        )

        for category in ("grow", "item", "etc"):
            observed_keys = set()
            for body in _tag_bodies(block, category):
                tokens = _script_tokens(body)
                key = tokens[1].strip().lower() if len(tokens) > 1 else ""
                guild_limit = cn_traits and (category, key) == ("item", "shop purchase count")
                if len(tokens) != (6 if guild_limit else 5):
                    raise ValueError(
                        "[attribute] level=%d [%s] tokens=%r"
                        % (expected_level, category, tokens)
                    )
                field_name = _BENEFIT_FIELD_BY_KEY.get((category, key))
                if guild_limit:
                    field_name = "guild_war_shop_extra_purchases"
                elif cn_traits and (category, key) == ("etc", "add day of dungeon enter count"):
                    field_name = "weekly_dungeon_extra_characters"
                try:
                    source_level = int(tokens[0], 10)
                    numeric_value = float(tokens[2])
                    display_mode = int(tokens[4], 10)
                    if guild_limit:
                        item_id = int(tokens[5], 10)
                        if item_id <= 0 or display_mode != 1:
                            raise ValueError("invalid guild shop item")
                        values_by_field["guild_war_shop_item_id"] = item_id
                except ValueError:
                    raise ValueError(
                        "[attribute] level=%d [%s] numeric field invalid"
                        % (expected_level, category)
                    )
                if (
                    field_name is None
                    or key in observed_keys
                    or not 1 <= source_level <= expected_level
                    or numeric_value < 0
                    or not numeric_value.is_integer()
                    or not 0 <= display_mode <= 0xFF
                ):
                    raise ValueError(
                        "[attribute] level=%d invalid %s key=%r source=%d value=%r mode=%d"
                        % (
                            expected_level,
                            category,
                            key,
                            source_level,
                            numeric_value,
                            display_mode,
                        )
                    )
                observed_keys.add(key)
                values_by_field[field_name] = int(numeric_value)
        if cn_traits:
            # CN 2018 lists the two towers together under the despair key.
            values_by_field["tower_grief_extra_entries"] = values_by_field.get(
                "tower_despair_extra_entries", 0
            )
        if expected_level == 1:
            if stats:
                raise ValueError("[attribute] level=1 must be the neutral row")
        elif set(stats) != set(_STAT_NAMES):
            raise ValueError(
                "[attribute] level=%d stat names=%r" % (expected_level, sorted(stats))
            )
        benefits = AdventureGroupBenefits(**values_by_field)
        if expected_level == 1 and benefits != AdventureGroupBenefits():
            raise ValueError("[attribute] level=1 must be the neutral benefit row")
        decreased = [
            field_name
            for field_name in _BENEFIT_FIELD_NAMES
            if getattr(benefits, field_name) < getattr(previous, field_name)
        ]
        if decreased:
            raise ValueError(
                "[attribute] level=%d is not a cumulative snapshot: %r decreased"
                % (expected_level, decreased)
            )
        rows.append((expected_level, benefits))
        previous = benefits
    return tuple(rows)


def parse_adventure_group_index(
    text: str,
    honor_text: str | None = None,
    *,
    cn_traits_text: str | None = None,
) -> AdventureGroupIndex:
    source = str(text or "")
    if not source.strip():
        raise ValueError("%s is empty" % ADVENTURER_SYSTEM_PATH)
    max_level = _single_number(source, _MAX_LEVEL, int)
    exp_rate = _single_number(source, _EXP_RATE, float)
    if not 1 <= max_level <= 0xFF:
        raise ValueError("[%s] must fit u8: %r" % (_MAX_LEVEL, max_level))
    if not 0.0 < exp_rate <= 1.0:
        raise ValueError("[%s] must be within (0, 1]: %r" % (_EXP_RATE, exp_rate))
    exp_thresholds = _parse_exp_thresholds(source)
    if len(exp_thresholds) < max_level:
        raise ValueError(
            "[%s] rows=%d below max level=%d"
            % (_EXP_TABLE, len(exp_thresholds), max_level)
        )
    honor_max_exp = 0
    expertise_exp_requirements: Tuple[int, ...] = ()
    if honor_text is not None:
        honor_max_exp, expertise_exp_requirements = _parse_honor_expertise(
            honor_text
        )
    if cn_traits_text is None:
        benefits_by_level = _parse_benefits(source, max_level)
    else:
        benefits_by_level = _parse_benefits(
            _section(cn_traits_text, "attribute list", require_close=True),
            max_level,
            cn_traits=True,
        )
    return AdventureGroupIndex(
        max_adventure_level=max_level,
        adventure_exp_rate=exp_rate,
        exp_thresholds=exp_thresholds,
        attributes_by_level=tuple(
            (level, benefits.attributes)
            for level, benefits in benefits_by_level
        ),
        benefits_by_level=benefits_by_level,
        honor_max_exp_on_max_level=honor_max_exp,
        expertise_exp_requirements=expertise_exp_requirements,
    )


_LOCK = threading.Lock()
_INDEX: AdventureGroupIndex | None = None


def cn_adventure_traits_enabled() -> bool:
    return os.environ.get("USDOF_CN2018_SHOP") == "1"


def get_adventure_group_index() -> AdventureGroupIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = parse_adventure_group_index(
                pvf_runtime.read_text(ADVENTURER_SYSTEM_PATH),
                pvf_runtime.read_text(HONOR_LEVEL_PATH),
                cn_traits_text=pvf_runtime.read_text(CN_TRAITS_PATH)
                if cn_adventure_traits_enabled() else None,
            )
        return _INDEX


def resolve_expertise_progress(expertise_exp: int) -> Tuple[int, int]:
    """Resolve cumulative character Expertise EXP for Stage3 wire consumers."""
    value = max(0, int(expertise_exp))
    if value == 0:
        return 0, 0
    return get_adventure_group_index().expertise_progress_for_exp(value)


def clear_adventure_group_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def warmup_adventure_group_index() -> dict[str, int | float | str]:
    index = get_adventure_group_index()
    result: dict[str, int | float | str] = {
        "path": ADVENTURER_SYSTEM_PATH,
        "honor_path": HONOR_LEVEL_PATH,
        "traits_path": CN_TRAITS_PATH if cn_adventure_traits_enabled() else ADVENTURER_SYSTEM_PATH,
    }
    result.update(index.snapshot())
    return result


__all__ = [
    "ADVENTURER_SYSTEM_PATH",
    "HONOR_LEVEL_PATH",
    "AdventureGroupAttributes",
    "AdventureGroupBenefits",
    "AdventureGroupIndex",
    "AdventureGroupSummary",
    "clear_adventure_group_index_cache",
    "get_adventure_group_index",
    "parse_adventure_group_index",
    "resolve_expertise_progress",
    "warmup_adventure_group_index",
]
