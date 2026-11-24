"""Runtime-PVF rules and persisted state for bead enchantments."""
from __future__ import annotations

import json
import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
ENCHANT_STATE_KEY = "enchant"
ENCHANT_CACHE_DOMAIN = "enchant_by_bead"
ENCHANT_CACHE_VERSION = 2

_LOCK = threading.RLock()
_BEAD_RULES: dict[int, "EnchantBeadRule"] | None = None
_LAZY_RESOLVED_IDS: set[int] = set()
_SYSTEM_RANDOM = random.SystemRandom()


@dataclass(frozen=True)
class EnchantCardRule:
    item_id: int
    allowed_equipment_types: tuple[str, ...]
    upgrade_counts: tuple[int, ...]
    source_path: str


@dataclass(frozen=True)
class EnchantCardChoice:
    card: EnchantCardRule
    weight: int


@dataclass(frozen=True)
class EnchantBeadRule:
    item_id: int
    card_item_id: int
    target_item_ids: tuple[int, ...]
    card: EnchantCardRule
    source_path: str
    card_choices: tuple[EnchantCardChoice, ...] = ()


@dataclass(frozen=True)
class EnchantResolution:
    bead_item_id: int
    card_item_id: int
    upgrade_count: int
    target_item_id: int
    target_equipment_type: str
    bead_source_path: str
    card_source_path: str


def _normalize_equipment_type(value: str) -> str:
    text = " ".join(str(value or "").strip().strip("`").lower().split())
    start = text.find("[")
    end = text.find("]", start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _field_body(text: str, name: str) -> str:
    match = re.search(
        r"\[%s\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)" % re.escape(name),
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _ticks(text: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in re.finditer(r"`([^`]*)`", text or ""))


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"-?\d+", text or ""))


def _first_int_field(text: str, name: str) -> int:
    values = _ints(_field_body(text, name))
    return int(values[0]) if values else 0


def _stackable_type(text: str) -> str:
    values = _ticks(_field_body(text, "stackable type"))
    return _normalize_equipment_type(values[0]) if values else ""


def _parse_lst_id_path_map(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw_id, raw_path in re.findall(
        r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`",
        text or "",
    ):
        item_id = int(raw_id)
        path = raw_path.replace("\\", "/").strip().lstrip("/")
        if item_id > 0 and path:
            out[item_id] = path
    return out


def _dedupe_ints(values: tuple[int, ...]) -> tuple[int, ...]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        normalized = int(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def _dedupe_types(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_equipment_type(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def _target_item_ids(text: str) -> tuple[int, ...]:
    return _dedupe_ints(
        tuple(
            value & 0xFFFFFFFF
            for name in ("target item id", "bead limited usable item")
            for value in _ints(_field_body(text, name))
            if value > 0
        )
    )


def _card_choice_pairs(text: str) -> tuple[tuple[int, int], ...]:
    card_item_id = _first_int_field(text, "monster card id")
    if card_item_id > 0:
        return ((card_item_id & 0xFFFFFFFF, 1),)

    weighted = _ints(_field_body(text, "monster card id drop prob"))
    if weighted:
        if len(weighted) % 2:
            return ()
        choices: list[tuple[int, int]] = []
        for index in range(0, len(weighted), 2):
            choice_item_id = int(weighted[index])
            weight = int(weighted[index + 1])
            if choice_item_id <= 0 or weight <= 0:
                return ()
            choices.append((choice_item_id & 0xFFFFFFFF, weight))
        return tuple(choices)

    if "[enchant table]" not in (text or "").lower():
        legacy_item_id = _first_int_field(text, "enchant index")
        if legacy_item_id > 0:
            return ((legacy_item_id & 0xFFFFFFFF, 1),)
    return ()


def parse_enchant_card_rule(
    *,
    item_id: int,
    source_path: str,
    text: str,
) -> EnchantCardRule:
    """Parse the target-type and upgrade-count restrictions from one card."""
    string_data = _ticks(_field_body(text, "string data"))
    allowed_types = _dedupe_types(tuple(string_data[1:])) if len(string_data) > 1 else ()

    table_match = re.search(
        r"\[enchant table\](.*?)\[/enchant table\]",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    table_text = table_match.group(1) if table_match is not None else ""
    upgrade_counts = _dedupe_ints(
        tuple(
            int(value)
            for value in re.findall(
                r"\[enchant index\]\s*(-?\d+)",
                table_text,
                flags=re.IGNORECASE,
            )
            if 0 <= int(value) <= 0xFF
        )
    )
    return EnchantCardRule(
        item_id=int(item_id) & 0xFFFFFFFF,
        allowed_equipment_types=allowed_types,
        upgrade_counts=upgrade_counts,
        source_path=str(source_path),
    )


def parse_enchant_bead_rule(
    *,
    item_id: int,
    source_path: str,
    text: str,
    card_source_path: str,
    card_text: str,
) -> EnchantBeadRule | None:
    """Parse one ``[enchant waste]`` definition and its referenced card."""
    if _stackable_type(text) != "[enchant waste]":
        return None
    card_choices = _card_choice_pairs(text)
    if len(card_choices) != 1:
        return None
    card_item_id = int(card_choices[0][0])
    target_item_ids = _target_item_ids(text)
    card = parse_enchant_card_rule(
        item_id=card_item_id,
        source_path=card_source_path,
        text=card_text,
    )
    return EnchantBeadRule(
        item_id=int(item_id) & 0xFFFFFFFF,
        card_item_id=int(card_item_id) & 0xFFFFFFFF,
        target_item_ids=target_item_ids,
        card=card,
        source_path=str(source_path),
        card_choices=(EnchantCardChoice(card=card, weight=card_choices[0][1]),),
    )


def _rule_to_cache_row(rule: EnchantBeadRule) -> list[Any]:
    choices = rule.card_choices or (EnchantCardChoice(rule.card, 1),)
    return [
        int(rule.item_id),
        [int(value) for value in rule.target_item_ids],
        str(rule.source_path),
        [
            [
                int(choice.card.item_id),
                [str(value) for value in choice.card.allowed_equipment_types],
                [int(value) for value in choice.card.upgrade_counts],
                str(choice.card.source_path),
                int(choice.weight),
            ]
            for choice in choices
        ],
    ]


def _rule_from_cache_row(row: Any) -> EnchantBeadRule:
    if not isinstance(row, list) or len(row) != 4:
        raise ValueError("invalid enchant bead cache row")
    item_id = int(row[0])
    target_item_ids = _dedupe_ints(tuple(int(value) for value in row[1]))
    source_path = str(row[2] or "")
    raw_choices = row[3]
    if (
        item_id <= 0
        or not source_path
        or any(value <= 0 for value in target_item_ids)
        or not isinstance(raw_choices, list)
        or not raw_choices
    ):
        raise ValueError("invalid enchant bead cache identity")
    choices: list[EnchantCardChoice] = []
    for raw_choice in raw_choices:
        if not isinstance(raw_choice, list) or len(raw_choice) != 5:
            raise ValueError("invalid enchant bead cache choice")
        card_item_id = int(raw_choice[0])
        allowed_types = _dedupe_types(tuple(str(value) for value in raw_choice[1]))
        upgrade_counts = _dedupe_ints(tuple(int(value) for value in raw_choice[2]))
        card_source_path = str(raw_choice[3] or "")
        weight = int(raw_choice[4])
        if (
            card_item_id <= 0
            or not card_source_path
            or weight <= 0
            or any(not 0 <= value <= 0xFF for value in upgrade_counts)
        ):
            raise ValueError("invalid enchant bead cache choice identity")
        choices.append(
            EnchantCardChoice(
                card=EnchantCardRule(
                    item_id=card_item_id & 0xFFFFFFFF,
                    allowed_equipment_types=allowed_types,
                    upgrade_counts=upgrade_counts,
                    source_path=card_source_path,
                ),
                weight=weight,
            )
        )
    card = choices[0].card
    return EnchantBeadRule(
        item_id=item_id & 0xFFFFFFFF,
        card_item_id=card.item_id,
        target_item_ids=target_item_ids,
        card=card,
        source_path=source_path,
        card_choices=tuple(choices),
    )


def _build_enchant_by_bead_index(
) -> tuple[dict[int, EnchantBeadRule], dict[str, int]]:
    paths = _parse_lst_id_path_map(pvf_runtime.read_text(STACKABLE_LIST_PATH))
    rules: dict[int, EnchantBeadRule] = {}
    cards: dict[int, EnchantCardRule] = {}
    candidate_count = 0
    invalid_count = 0
    for item_id, rel_path in paths.items():
        source_path = "stackable/" + rel_path
        bead_text = pvf_runtime.read_text(source_path)
        if _stackable_type(bead_text) != "[enchant waste]":
            continue
        candidate_count += 1
        raw_choices = _card_choice_pairs(bead_text)
        if not raw_choices or any(
            int(card_item_id) not in paths for card_item_id, _weight in raw_choices
        ):
            invalid_count += 1
            continue
        choices: list[EnchantCardChoice] = []
        for card_item_id, weight in raw_choices:
            card_rel_path = paths[int(card_item_id)]
            card_source_path = "stackable/" + card_rel_path
            card = cards.get(int(card_item_id))
            if card is None:
                card = parse_enchant_card_rule(
                    item_id=card_item_id,
                    source_path=card_source_path,
                    text=pvf_runtime.read_text(card_source_path),
                )
                cards[int(card_item_id)] = card
            choices.append(EnchantCardChoice(card=card, weight=int(weight)))
        card = choices[0].card
        target_item_ids = _target_item_ids(bead_text)
        rules[int(item_id)] = EnchantBeadRule(
            item_id=int(item_id) & 0xFFFFFFFF,
            card_item_id=int(card.item_id) & 0xFFFFFFFF,
            target_item_ids=target_item_ids,
            card=card,
            source_path=source_path,
            card_choices=tuple(choices),
        )
    if not rules:
        raise RuntimeError("runtime PVF contains no usable [enchant waste] definitions")
    return rules, {
        "scanned_count": len(paths),
        "candidate_count": candidate_count,
        "bead_count": len(rules),
        "card_count": len(cards),
        "invalid_bead_count": int(invalid_count),
    }


def _rules_from_cache_payload(
    payload: Any,
) -> tuple[dict[int, EnchantBeadRule], dict[str, int]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("rows"), list):
        raise ValueError("invalid enchant bead cache payload")
    rules: dict[int, EnchantBeadRule] = {}
    for row in payload["rows"]:
        rule = _rule_from_cache_row(row)
        if int(rule.item_id) in rules:
            raise ValueError("duplicate enchant bead cache identity")
        rules[int(rule.item_id)] = rule
    if not rules:
        raise ValueError("empty enchant bead cache payload")
    metrics = {
        "scanned_count": max(0, int(payload.get("scanned_count") or 0)),
        "candidate_count": max(0, int(payload.get("candidate_count") or 0)),
        "bead_count": len(rules),
        "card_count": len(
            {
                choice.card.item_id
                for rule in rules.values()
                for choice in (
                    rule.card_choices or (EnchantCardChoice(rule.card, 1),)
                )
            }
        ),
        "invalid_bead_count": max(
            0, int(payload.get("invalid_bead_count") or 0)
        ),
    }
    if metrics["candidate_count"] < metrics["bead_count"]:
        raise ValueError("invalid enchant bead cache counts")
    return rules, metrics


def warmup_enchant_by_bead_index(cache: Any = None) -> dict[str, Any]:
    """Build every PVF-defined bead/card rule, with an identity-bound cache."""
    rules: dict[int, EnchantBeadRule] | None = None
    metrics: dict[str, int] = {}
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(ENCHANT_CACHE_DOMAIN, ENCHANT_CACHE_VERSION)
        if payload is not None:
            try:
                rules, metrics = _rules_from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(ENCHANT_CACHE_DOMAIN)

    if rules is None:
        rules, metrics = _build_enchant_by_bead_index()
        if cache is not None:
            payload = dict(metrics)
            payload["rows"] = [
                _rule_to_cache_row(rule)
                for _, rule in sorted(rules.items())
            ]
            cache.write(
                ENCHANT_CACHE_DOMAIN,
                ENCHANT_CACHE_VERSION,
                payload,
                record_count=len(rules),
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(cache.last_status.get(ENCHANT_CACHE_DOMAIN, "miss"))
            )

    install_enchant_by_bead_index(rules)
    result: dict[str, Any] = dict(metrics)
    result["cache"] = cache_status
    return result


def clear_enchant_by_bead_index() -> None:
    global _BEAD_RULES
    with _LOCK:
        _BEAD_RULES = None
        _LAZY_RESOLVED_IDS.clear()


def install_enchant_by_bead_index(records: Mapping[int, EnchantBeadRule]) -> None:
    """Install an already parsed index, primarily for deterministic tests."""
    global _BEAD_RULES
    normalized: dict[int, EnchantBeadRule] = {}
    for raw_item_id, rule in records.items():
        item_id = int(raw_item_id) & 0xFFFFFFFF
        if item_id <= 0 or not isinstance(rule, EnchantBeadRule):
            raise ValueError("invalid enchant bead rule")
        if int(rule.item_id) != item_id or int(rule.card_item_id) <= 0:
            raise ValueError("enchant bead rule identity mismatch")
        normalized[item_id] = rule
    with _LOCK:
        _BEAD_RULES = normalized
        _LAZY_RESOLVED_IDS.clear()


def _load_single_enchant_bead_rule(item_id: int) -> EnchantBeadRule | None:
    bead_item_id = int(item_id) & 0xFFFFFFFF
    if bead_item_id <= 0:
        return None
    metadata = resolve_item_metadata(bead_item_id)
    if not bool(getattr(metadata, "is_stackable", False)):
        return None
    source_path = str(getattr(metadata, "source_path", "") or "")
    if not source_path:
        return None
    bead_text = pvf_runtime.read_text(source_path)
    if _stackable_type(bead_text) != "[enchant waste]":
        return None
    raw_choices = _card_choice_pairs(bead_text)
    if not raw_choices:
        return None
    choices: list[EnchantCardChoice] = []
    for card_item_id, weight in raw_choices:
        card_metadata = resolve_item_metadata(card_item_id)
        if not bool(getattr(card_metadata, "is_stackable", False)):
            return None
        card_source_path = str(getattr(card_metadata, "source_path", "") or "")
        if not card_source_path:
            return None
        card = parse_enchant_card_rule(
            item_id=card_item_id,
            source_path=card_source_path,
            text=pvf_runtime.read_text(card_source_path),
        )
        choices.append(EnchantCardChoice(card=card, weight=int(weight)))
    card = choices[0].card
    return EnchantBeadRule(
        item_id=bead_item_id,
        card_item_id=int(card.item_id) & 0xFFFFFFFF,
        target_item_ids=_target_item_ids(bead_text),
        card=card,
        source_path=source_path,
        card_choices=tuple(choices),
    )


def get_enchant_bead_rule(item_id: int) -> EnchantBeadRule | None:
    """Resolve one requested bead instead of walking every stackable at boot."""
    global _BEAD_RULES
    bead_item_id = int(item_id) & 0xFFFFFFFF
    with _LOCK:
        rules = _BEAD_RULES
        if rules is not None:
            cached = rules.get(bead_item_id)
            if cached is not None:
                return cached
        if bead_item_id in _LAZY_RESOLVED_IDS:
            return None
        _LAZY_RESOLVED_IDS.add(bead_item_id)
        try:
            rule = _load_single_enchant_bead_rule(bead_item_id)
        except (KeyError, ValueError):
            rule = None
        if rule is None:
            return None
        if _BEAD_RULES is None:
            _BEAD_RULES = {}
        _BEAD_RULES[bead_item_id] = rule
        return rule


def resolve_enchant_by_bead(
    bead_item_id: int,
    target_item_id: int,
    upgrade_count: int,
    *,
    target_metadata: Any = None,
    random_ticket: int | None = None,
) -> tuple[EnchantResolution | None, str]:
    """Validate a bead against one target template using the warmed PVF rules."""
    bead_id = int(bead_item_id) & 0xFFFFFFFF
    target_id = int(target_item_id) & 0xFFFFFFFF
    upgrade = int(upgrade_count)
    rule = get_enchant_bead_rule(bead_id)
    if rule is None:
        return None, "bead_rule_missing"
    if rule.target_item_ids and target_id not in rule.target_item_ids:
        return None, "target_item_not_allowed"

    target = (
        target_metadata
        if target_metadata is not None
        else resolve_item_metadata(target_id)
    )
    if not target.is_equipment:
        return None, "target_not_equipment"
    target_type = _normalize_equipment_type(target.equipment_type)
    if not target_type:
        return None, "target_equipment_type_missing"
    if not 0 <= upgrade <= 0xFF:
        return None, "upgrade_count_out_of_range"

    choices = rule.card_choices or (EnchantCardChoice(rule.card, 1),)
    eligible: list[EnchantCardChoice] = []
    type_matched = False
    upgrade_rejected = False
    upgrade_table_missing = False
    for choice in choices:
        allowed_types = tuple(choice.card.allowed_equipment_types)
        if allowed_types and target_type not in allowed_types:
            continue
        if target_type == "[creature]" and "[creature]" not in allowed_types:
            continue
        type_matched = True
        allowed_upgrades = tuple(choice.card.upgrade_counts)
        if allowed_upgrades:
            if upgrade not in allowed_upgrades:
                upgrade_rejected = True
                continue
        elif upgrade != 0:
            upgrade_table_missing = True
            continue
        if int(choice.weight) > 0:
            eligible.append(choice)
    if not eligible:
        if not type_matched:
            return None, "target_equipment_type_not_allowed"
        if upgrade_rejected:
            return None, "upgrade_count_not_allowed"
        if upgrade_table_missing:
            return None, "upgrade_count_requires_table"
        return None, "bead_card_choice_missing"

    total_weight = sum(int(choice.weight) for choice in eligible)
    ticket = (
        _SYSTEM_RANDOM.randrange(total_weight)
        if random_ticket is None
        else int(random_ticket) % total_weight
    )
    selected = eligible[-1]
    for choice in eligible:
        weight = int(choice.weight)
        if ticket < weight:
            selected = choice
            break
        ticket -= weight

    return EnchantResolution(
        bead_item_id=bead_id,
        card_item_id=int(selected.card.item_id) & 0xFFFFFFFF,
        upgrade_count=upgrade,
        target_item_id=target_id,
        target_equipment_type=target_type,
        bead_source_path=rule.source_path,
        card_source_path=selected.card.source_path,
    ), "ok"


def _json_object(raw_extra_json: Any) -> dict[str, Any]:
    if isinstance(raw_extra_json, Mapping):
        return dict(raw_extra_json)
    raw = raw_extra_json
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def _clamped_int(value: Any, maximum: int) -> int:
    try:
        return max(0, min(int(maximum), int(value or 0)))
    except (TypeError, ValueError):
        return 0


def parse_enchant_state(raw_extra_json: Any) -> tuple[int, int]:
    """Return ``(card_item_id, upgrade_count)`` from canonical or legacy JSON."""
    value = _json_object(raw_extra_json)
    nested = value.get(ENCHANT_STATE_KEY)
    if isinstance(nested, Mapping):
        card = nested.get(
            "card_item_id",
            nested.get("card_id", nested.get("enchant_index", 0)),
        )
        upgrade = nested.get(
            "upgrade_count",
            nested.get("enchant_upgrade_count", 0),
        )
    else:
        card = value.get(
            "enchant_card_id",
            value.get("enchant_index", value.get("card_item_id", 0)),
        )
        upgrade = value.get(
            "enchant_upgrade_count",
            value.get("upgrade_count", 0),
        )
    return _clamped_int(card, 0xFFFFFFFF), _clamped_int(upgrade, 0xFF)


def parse_enchant_upgrade_count(raw_extra_json: Any) -> int:
    return int(parse_enchant_state(raw_extra_json)[1])


def merge_enchant_state(
    raw_extra_json: Any,
    card_item_id: int,
    upgrade_count: int,
) -> str:
    """Replace only the enchant object while preserving other instance state."""
    value = _json_object(raw_extra_json)
    for legacy_key in (
        "enchant_card_id",
        "enchant_index",
        "enchant_upgrade_count",
        "card_item_id",
        "upgrade_count",
    ):
        value.pop(legacy_key, None)
    value[ENCHANT_STATE_KEY] = {
        "card_item_id": _clamped_int(card_item_id, 0xFFFFFFFF),
        "upgrade_count": _clamped_int(upgrade_count, 0xFF),
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ENCHANT_CACHE_DOMAIN",
    "ENCHANT_CACHE_VERSION",
    "ENCHANT_STATE_KEY",
    "EnchantBeadRule",
    "EnchantCardChoice",
    "EnchantCardRule",
    "EnchantResolution",
    "clear_enchant_by_bead_index",
    "get_enchant_bead_rule",
    "install_enchant_by_bead_index",
    "merge_enchant_state",
    "parse_enchant_bead_rule",
    "parse_enchant_card_rule",
    "parse_enchant_state",
    "parse_enchant_upgrade_count",
    "resolve_enchant_by_bead",
    "warmup_enchant_by_bead_index",
]
