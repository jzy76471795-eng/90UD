"""PVF-backed account premium contract catalog.

Only contract types whose Stage3 consumer and requested behavior are closed are
eligible for immediate Cera-shop activation.  Item aliases and lifetimes still
come exclusively from ``etc/premiumlist_new.etc``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from pvf import pvf_runtime


PREMIUM_LIST_PATH = "etc/premiumlist_new.etc"
PREMIUM_TYPE_TACTICIAN = 22
PREMIUM_TYPE_CONQUEROR = 27
PREMIUM_TYPE_GROWTH = 79
PREMIUM_TYPE_CUBE = 92
PREMIUM_TYPE_NEO_BASIC = 117
PREMIUM_TYPE_NEO_PLUS = 118
PREMIUM_DETERMINISTIC_WRAPPER_ITEM_IDS: tuple[int, ...] = (50002921, 50002922)
SUPPORTED_ACCOUNT_PREMIUM_TYPES: frozenset[int] = frozenset(
    {
        PREMIUM_TYPE_TACTICIAN,
        PREMIUM_TYPE_CONQUEROR,
        PREMIUM_TYPE_GROWTH,
        PREMIUM_TYPE_CUBE,
        PREMIUM_TYPE_NEO_BASIC,
        PREMIUM_TYPE_NEO_PLUS,
    }
)


@dataclass(frozen=True)
class PremiumContractEffects:
    over_equip_levels: tuple[tuple[int, int], ...] = ()
    over_skill_level: int = 0
    bonus_exp_percent: int = 0
    quest_item_drop_rate_percent: int = 0
    independent_drop_rate_percent: tuple[int, ...] = ()
    exp_percent: int = 0
    fatigue: int = 0
    daily_items: tuple[tuple[int, int], ...] = ()

    @property
    def maximum_equipment_level_reduction(self) -> int:
        return max((int(value) for _kind, value in self.over_equip_levels), default=0)


@dataclass(frozen=True)
class PremiumContractItem:
    item_id: int
    premium_type: int
    duration_seconds: int
    term_value: int
    term_unit: str
    name: str = ""
    inventory_activation_mode: str = "created_time"


@dataclass(frozen=True)
class PremiumContractType:
    premium_type: int
    name: str
    effects: PremiumContractEffects
    item_ids: tuple[int, ...]


@dataclass(frozen=True)
class PremiumContractIndex:
    items: Mapping[int, PremiumContractItem]
    types: Mapping[int, PremiumContractType]
    source_path: str = PREMIUM_LIST_PATH

    def resolve(self, item_id: int) -> PremiumContractItem | None:
        return self.items.get(int(item_id) & 0xFFFFFFFF)

    def resolve_type(self, premium_type: int) -> PremiumContractType | None:
        return self.types.get(int(premium_type) & 0xFF)

    def activation_map(self) -> dict[int, tuple[int, int, str]]:
        return {
            int(item_id): (
                int(item.premium_type),
                int(item.duration_seconds),
                str(item.inventory_activation_mode),
            )
            for item_id, item in self.items.items()
        }


@dataclass(frozen=True)
class ActivePremiumEffects:
    active_types: tuple[int, ...] = ()
    valid_until: int = 0
    equipment_level_reduction: int = 0
    skill_level_reduction: int = 0
    bonus_exp_percent: int = 0
    growth_contract_exp_percent: int = 0
    neo_premium_exp_percent: int = 0
    quest_item_drop_rate_percent: int = 0
    independent_drop_rate_percent: tuple[int, ...] = ()
    daily_items: tuple[tuple[int, int], ...] = ()

    def _split_bonus_exp(self, base_exp: int) -> tuple[int, int]:
        """Split the already-rounded combined Premium amount deterministically.

        Stage3 displays Growth and Neo as separate rows, while the progression
        economy historically applies one combined percentage.  Rounding the
        combined value first and assigning the remainder to Neo keeps the
        displayed components exactly equal to the amount persisted in DB.
        """
        base = max(0, int(base_exp))
        combined = self.compute_bonus_exp(base)
        growth_percent = max(0, int(self.growth_contract_exp_percent))
        neo_percent = max(0, int(self.neo_premium_exp_percent))
        percent_total = growth_percent + neo_percent
        if combined <= 0 or percent_total <= 0:
            return 0, combined
        growth = (combined * growth_percent) // percent_total
        return growth, combined - growth

    def compute_bonus_exp(self, base_exp: int) -> int:
        base = max(0, int(base_exp))
        percent = max(0, int(self.bonus_exp_percent))
        if base <= 0 or percent <= 0:
            return 0
        return min(0xFFFFFFFF, (base * percent) // 100)

    def compute_growth_contract_exp(self, base_exp: int) -> int:
        return self._split_bonus_exp(base_exp)[0]

    def compute_neo_premium_exp(self, base_exp: int) -> int:
        return self._split_bonus_exp(base_exp)[1]

    def compute_quest_item_drop_rate(self, base_rate: int) -> int:
        rate = max(0, min(100, int(base_rate)))
        percent = max(0, int(self.quest_item_drop_rate_percent))
        return min(100, (rate * (100 + percent)) // 100)

    def independent_drop_bonus_for_difficulty(self, difficulty: int) -> int:
        rates = tuple(
            max(0, int(value)) for value in self.independent_drop_rate_percent
        )
        if not rates:
            return 0
        index = max(0, min(int(difficulty), len(rates) - 1))
        return rates[index]


_LOCK = threading.RLock()
_INDEX: PremiumContractIndex | None = None


def _clean_token(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].strip()
    return text


def _parse_int(value: Any, *, field: str, premium_type: int) -> int:
    try:
        return int(_clean_token(value), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "premium invalid %s type=%d value=%r" % (field, premium_type, value)
        ) from exc


def _tag_value(block: Sequence[str], tag: str, premium_type: int) -> int:
    try:
        index = block.index(tag)
    except ValueError:
        return 0
    if index + 1 >= len(block):
        raise ValueError("premium missing value type=%d tag=%s" % (premium_type, tag))
    return max(0, _parse_int(block[index + 1], field=tag, premium_type=premium_type))


def _tag_values(
    block: Sequence[str], tag: str, premium_type: int
) -> tuple[int, ...]:
    try:
        index = block.index(tag) + 1
    except ValueError:
        return ()
    values: list[int] = []
    while index < len(block) and not str(block[index]).startswith("["):
        values.append(
            max(0, _parse_int(block[index], field=tag, premium_type=premium_type))
        )
        index += 1
    return tuple(values)


def _parse_item_pairs(
    block: Sequence[str], tag: str, premium_type: int
) -> tuple[tuple[int, int], ...]:
    values = _tag_values(block, tag, premium_type)
    if len(values) % 2 != 0:
        raise ValueError(
            "premium item pairs shape type=%d tag=%s values=%d"
            % (premium_type, tag, len(values))
        )
    return tuple(
        (int(values[index]), int(values[index + 1]))
        for index in range(0, len(values), 2)
        if int(values[index]) > 0 and int(values[index + 1]) > 0
    )


def _parse_over_equip_levels(
    block: Sequence[str], premium_type: int
) -> tuple[tuple[int, int], ...]:
    try:
        index = block.index("[over equipinfo]") + 1
    except ValueError:
        return ()
    values: list[int] = []
    while index < len(block) and not str(block[index]).startswith("["):
        values.append(
            _parse_int(block[index], field="over equipinfo", premium_type=premium_type)
        )
        index += 1
    if not values:
        return ()
    count = int(values[0])
    pairs = values[1:]
    if count <= 0 or len(pairs) != count * 2:
        raise ValueError(
            "premium over equipinfo shape type=%d count=%d values=%d"
            % (premium_type, count, len(pairs))
        )
    return tuple(
        (max(0, int(pairs[index])), max(0, int(pairs[index + 1])))
        for index in range(0, len(pairs), 2)
    )


def _parse_type_block(
    premium_type: int,
    block: Sequence[str],
) -> tuple[PremiumContractType, list[PremiumContractItem]]:
    name = ""
    try:
        name_index = block.index("[name]")
        if name_index + 1 < len(block):
            name = _clean_token(block[name_index + 1])
    except ValueError:
        pass

    effects = PremiumContractEffects(
        over_equip_levels=_parse_over_equip_levels(block, premium_type),
        over_skill_level=_tag_value(block, "[over skill]", premium_type),
        bonus_exp_percent=_tag_value(block, "[bonus exp]", premium_type),
        quest_item_drop_rate_percent=_tag_value(
            block, "[quest item drop rate]", premium_type
        ),
        independent_drop_rate_percent=_tag_values(
            block, "[independent drop rate]", premium_type
        ),
        exp_percent=_tag_value(block, "[exp]", premium_type),
        fatigue=_tag_value(block, "[fatigue]", premium_type),
        daily_items=_parse_item_pairs(block, "[items]", premium_type),
    )

    items: list[PremiumContractItem] = []
    cursor = 0
    while cursor < len(block):
        if block[cursor] != "[item]":
            cursor += 1
            continue
        try:
            close = block.index("[/item]", cursor + 1)
        except ValueError as exc:
            raise ValueError("premium unclosed item type=%d" % premium_type) from exc
        item_block = list(block[cursor + 1 : close])
        if not item_block:
            raise ValueError("premium empty item type=%d" % premium_type)
        item_id = _parse_int(item_block[0], field="item_id", premium_type=premium_type)
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError(
                "premium invalid item_id type=%d item=%d" % (premium_type, item_id)
            )
        try:
            term_index = item_block.index("[term]")
        except ValueError as exc:
            raise ValueError(
                "premium item missing term type=%d item=%d" % (premium_type, item_id)
            ) from exc
        if term_index + 1 >= len(item_block):
            raise ValueError(
                "premium item empty term type=%d item=%d" % (premium_type, item_id)
            )
        term = _parse_int(
            item_block[term_index + 1], field="term", premium_type=premium_type
        )
        minute_unit = "[is term unit minute]" in item_block
        duration = term * (60 if minute_unit else 24 * 60 * 60)
        if term <= 0 or duration <= 0 or duration > 0x7FFFFFFFFFFFFFFF:
            raise ValueError(
                "premium invalid duration type=%d item=%d term=%d"
                % (premium_type, item_id, term)
            )
        items.append(
            PremiumContractItem(
                item_id=item_id,
                premium_type=premium_type,
                duration_seconds=duration,
                term_value=term,
                term_unit="minute" if minute_unit else "day",
                name=name,
            )
        )
        cursor = close + 1

    definition = PremiumContractType(
        premium_type=premium_type,
        name=name,
        effects=effects,
        item_ids=tuple(item.item_id for item in items),
    )
    return definition, items


def parse_premium_contract_tokens(
    tokens: Sequence[str],
    *,
    source_path: str = PREMIUM_LIST_PATH,
) -> PremiumContractIndex:
    normalized = [_clean_token(token) for token in tokens]
    type_offsets = [
        index for index, token in enumerate(normalized) if token == "[type]"
    ]
    items: dict[int, PremiumContractItem] = {}
    types: dict[int, PremiumContractType] = {}
    for offset_index, offset in enumerate(type_offsets):
        if offset + 1 >= len(normalized):
            raise ValueError("premium trailing [type]")
        premium_type = _parse_int(
            normalized[offset + 1], field="premium_type", premium_type=0
        )
        if premium_type not in SUPPORTED_ACCOUNT_PREMIUM_TYPES:
            continue
        end = (
            type_offsets[offset_index + 1]
            if offset_index + 1 < len(type_offsets)
            else len(normalized)
        )
        definition, type_items = _parse_type_block(
            premium_type, normalized[offset + 2 : end]
        )
        if premium_type in types:
            raise ValueError("premium duplicate type=%d" % premium_type)
        if not type_items:
            raise ValueError("premium type has no item aliases type=%d" % premium_type)
        types[premium_type] = definition
        for item in type_items:
            if item.item_id in items:
                raise ValueError("premium duplicate item=%d" % item.item_id)
            items[item.item_id] = item

    missing = sorted(SUPPORTED_ACCOUNT_PREMIUM_TYPES.difference(types))
    if missing:
        raise ValueError("premium required types missing=%s" % missing)
    return PremiumContractIndex(items=items, types=types, source_path=source_path)


def parse_premium_contract_text(
    text: str, *, source_path: str = PREMIUM_LIST_PATH
) -> PremiumContractIndex:
    return parse_premium_contract_tokens(
        str(text or "").splitlines(), source_path=source_path
    )


def _resolve_deterministic_wrapper_alias(
    index: PremiumContractIndex,
    source_item_id: int,
) -> PremiumContractItem:
    from game.character.item_metadata import resolve_item_metadata
    from game.shop.booster_item_index import (
        is_direct_booster_metadata,
        parse_booster_item_text,
    )

    source_id = int(source_item_id) & 0xFFFFFFFF
    current_id = source_id
    count_multiplier = 1
    seen: set[int] = set()
    source_name = ""
    for _depth in range(8):
        terminal = index.resolve(current_id)
        if terminal is not None:
            duration = int(terminal.duration_seconds) * count_multiplier
            if duration <= 0 or duration > 0x7FFFFFFFFFFFFFFF:
                raise ValueError(
                    "premium wrapper duration overflow source=%d" % source_id
                )
            return PremiumContractItem(
                item_id=source_id,
                premium_type=int(terminal.premium_type),
                duration_seconds=duration,
                term_value=int(terminal.term_value) * count_multiplier,
                term_unit=str(terminal.term_unit),
                name=source_name or str(terminal.name),
                inventory_activation_mode="on_consume",
            )
        if current_id in seen:
            raise ValueError("premium wrapper cycle source=%d" % source_id)
        seen.add(current_id)

        metadata = resolve_item_metadata(current_id)
        if not is_direct_booster_metadata(metadata):
            raise ValueError(
                "premium wrapper is not direct booster source=%d item=%d type=%s"
                % (source_id, current_id, metadata.stackable_type)
            )
        if not source_name:
            source_name = str(metadata.name or "")
        definition = parse_booster_item_text(
            current_id,
            pvf_runtime.read_text(str(metadata.source_path)),
            stackable_type=str(metadata.stackable_type),
            source_path=str(metadata.source_path),
        )
        if len(definition.groups) != 1:
            raise ValueError(
                "premium wrapper group count source=%d item=%d groups=%d"
                % (source_id, current_id, len(definition.groups))
            )
        group = definition.groups[0]
        if int(group.draw_count) != 1 or len(group.candidates) != 1:
            raise ValueError(
                "premium wrapper is not deterministic source=%d item=%d"
                % (source_id, current_id)
            )
        candidate = group.candidates[0]
        if int(candidate.weight) <= 0 or int(candidate.count) <= 0:
            raise ValueError(
                "premium wrapper reward invalid source=%d item=%d"
                % (source_id, current_id)
            )
        count_multiplier *= int(candidate.count)
        if count_multiplier > 0x7FFFFFFFFFFFFFFF:
            raise ValueError(
                "premium wrapper count overflow source=%d" % source_id
            )
        current_id = int(candidate.item_id) & 0xFFFFFFFF
    raise ValueError("premium wrapper chain too deep source=%d" % source_id)


def _with_deterministic_wrapper_aliases(
    index: PremiumContractIndex,
) -> PremiumContractIndex:
    items = dict(index.items)
    types = dict(index.types)
    for source_item_id in PREMIUM_DETERMINISTIC_WRAPPER_ITEM_IDS:
        if int(source_item_id) in items:
            raise ValueError("premium wrapper duplicates direct alias=%d" % source_item_id)
        alias = _resolve_deterministic_wrapper_alias(index, source_item_id)
        if int(alias.premium_type) != PREMIUM_TYPE_NEO_PLUS:
            raise ValueError(
                "premium wrapper resolved outside Neo Plus source=%d type=%d"
                % (source_item_id, alias.premium_type)
            )
        items[int(source_item_id)] = alias
        definition = types.get(int(alias.premium_type))
        if definition is None:
            raise ValueError(
                "premium wrapper target type missing source=%d type=%d"
                % (source_item_id, alias.premium_type)
            )
        types[int(alias.premium_type)] = replace(
            definition,
            item_ids=definition.item_ids + (int(source_item_id),),
        )
    return PremiumContractIndex(
        items=items,
        types=types,
        source_path=index.source_path,
    )


def get_premium_contract_index() -> PremiumContractIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = _with_deterministic_wrapper_aliases(
                parse_premium_contract_text(
                    pvf_runtime.read_text(PREMIUM_LIST_PATH),
                    source_path=PREMIUM_LIST_PATH,
                )
            )
        return _INDEX


def resolve_premium_contract(item_id: int) -> PremiumContractItem | None:
    return get_premium_contract_index().resolve(item_id)


def resolve_cached_premium_contract(item_id: int) -> PremiumContractItem | None:
    """Resolve from the boot-warmed catalog without initiating a PVF read."""
    with _LOCK:
        index = _INDEX
        if index is None:
            return None
        return index.resolve(item_id)


def clear_premium_contract_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def install_premium_contract_index(index: PremiumContractIndex) -> None:
    if not isinstance(index, PremiumContractIndex) or not index.items:
        raise ValueError("cannot install empty premium contract index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def combine_active_premium_effects(
    active_rows: Sequence[Mapping[str, Any]],
    *,
    index: PremiumContractIndex | None = None,
) -> ActivePremiumEffects:
    if not active_rows:
        return ActivePremiumEffects()
    catalog = index if index is not None else get_premium_contract_index()
    active_types: list[int] = []
    valid_until = 0
    equip_reduction = 0
    skill_reduction = 0
    bonus_exp = 0
    neo_exp = 0
    quest_drop_bonus = 0
    independent_drop_bonus: list[int] = []
    daily_item_counts: dict[int, int] = {}
    for row in active_rows:
        premium_type = int(row.get("premium_type", 0) or 0) & 0xFF
        end_time = max(0, int(row.get("end_time", 0) or 0))
        definition = catalog.resolve_type(premium_type)
        if definition is None or end_time <= 0:
            continue
        active_types.append(premium_type)
        valid_until = end_time if valid_until <= 0 else min(valid_until, end_time)
        effects = definition.effects
        equip_reduction = max(
            equip_reduction, effects.maximum_equipment_level_reduction
        )
        skill_reduction = max(skill_reduction, int(effects.over_skill_level))
        bonus_exp = max(bonus_exp, int(effects.bonus_exp_percent))
        neo_exp = max(neo_exp, int(effects.exp_percent))
        quest_drop_bonus = max(
            quest_drop_bonus, int(effects.quest_item_drop_rate_percent)
        )
        for difficulty, value in enumerate(effects.independent_drop_rate_percent):
            while len(independent_drop_bonus) <= difficulty:
                independent_drop_bonus.append(0)
            independent_drop_bonus[difficulty] = max(
                independent_drop_bonus[difficulty], int(value)
            )
        for item_id, count in effects.daily_items:
            daily_item_counts[int(item_id)] = max(
                daily_item_counts.get(int(item_id), 0), int(count)
            )
    combined_percent = min(10000, bonus_exp + neo_exp)
    return ActivePremiumEffects(
        active_types=tuple(sorted(set(active_types))),
        valid_until=valid_until,
        equipment_level_reduction=equip_reduction,
        skill_level_reduction=skill_reduction,
        bonus_exp_percent=combined_percent,
        growth_contract_exp_percent=min(10000, bonus_exp),
        neo_premium_exp_percent=min(10000, neo_exp),
        quest_item_drop_rate_percent=min(10000, quest_drop_bonus),
        independent_drop_rate_percent=tuple(
            min(10000, max(0, value)) for value in independent_drop_bonus
        ),
        daily_items=tuple(sorted(daily_item_counts.items())),
    )


def get_active_premium_effects(
    repo: Any,
    account_id: int,
    *,
    session: Any = None,
    now_unix: int | None = None,
) -> ActivePremiumEffects:
    import time

    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    if session is not None:
        cached = getattr(session, "account_premium_effects", None)
        if isinstance(cached, ActivePremiumEffects) and int(cached.valid_until) > now:
            return cached
    rows = repo.list_active_account_premiums(int(account_id), now_unix=now)
    effects = combine_active_premium_effects(rows)
    if session is not None:
        try:
            session.account_premium_effects = effects
        except Exception:
            pass
    return effects


def warmup_premium_contract_index() -> dict[str, int | str]:
    index = get_premium_contract_index()
    minute_aliases = sum(
        1 for item in index.items.values() if item.term_unit == "minute"
    )
    wrapper_aliases = sum(
        1
        for item in index.items.values()
        if item.inventory_activation_mode == "on_consume"
    )
    return {
        "ok": 1,
        "source_path": index.source_path,
        "type_count": len(index.types),
        "item_alias_count": len(index.items),
        "minute_alias_count": minute_aliases,
        "wrapper_alias_count": wrapper_aliases,
    }


__all__ = [
    "ActivePremiumEffects",
    "PREMIUM_LIST_PATH",
    "PREMIUM_DETERMINISTIC_WRAPPER_ITEM_IDS",
    "PREMIUM_TYPE_CONQUEROR",
    "PREMIUM_TYPE_CUBE",
    "PREMIUM_TYPE_GROWTH",
    "PREMIUM_TYPE_NEO_BASIC",
    "PREMIUM_TYPE_NEO_PLUS",
    "PREMIUM_TYPE_TACTICIAN",
    "PremiumContractEffects",
    "PremiumContractIndex",
    "PremiumContractItem",
    "PremiumContractType",
    "SUPPORTED_ACCOUNT_PREMIUM_TYPES",
    "clear_premium_contract_index_cache",
    "combine_active_premium_effects",
    "get_active_premium_effects",
    "get_premium_contract_index",
    "install_premium_contract_index",
    "parse_premium_contract_text",
    "parse_premium_contract_tokens",
    "resolve_premium_contract",
    "resolve_cached_premium_contract",
    "warmup_premium_contract_index",
]
