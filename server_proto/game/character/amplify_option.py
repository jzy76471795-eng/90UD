"""Runtime-PVF rules and persisted state for equipment amplification options."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime


AMPLIFY_ITEM_PVF_PATH = "etc/amplifyitem.etc"
AMPLIFY_UPGRADE_PVF_PATH = "etc/amplifyupgrade.etc"
AMPLIFY_LEVEL_MAX = 31
AMPLIFY_TABLE_ROW_WIDTH = 24

AMPLIFY_TYPE_VITALITY = 1
AMPLIFY_TYPE_SPIRIT = 2
AMPLIFY_TYPE_STRENGTH = 3
AMPLIFY_TYPE_INTELLIGENCE = 4
AMPLIFY_TYPE_UNIDENTIFIED = 0x80
AMPLIFY_TYPES = frozenset({1, 2, 3, 4})

AMPLIFY_ACTION_INVEST = 0
AMPLIFY_ACTION_REINVEST = 1
AMPLIFY_ACTION_PURE_GOLD = 2

_RARITY_NAMES = (
    "common", "uncommon", "rare", "unique", "epic", "chronicle", "legendary",
)
_OPTION_TYPE_BY_NAME = {
    "[physical defense]": AMPLIFY_TYPE_VITALITY,
    "[magical defense]": AMPLIFY_TYPE_SPIRIT,
    "[physical attack]": AMPLIFY_TYPE_STRENGTH,
    "[magical attack]": AMPLIFY_TYPE_INTELLIGENCE,
}
_ACTION_SECTION = {
    AMPLIFY_ACTION_INVEST: "invest option",
    AMPLIFY_ACTION_REINVEST: "reinvest option",
    AMPLIFY_ACTION_PURE_GOLD: "random invest upgrade option",
}
_TOKEN_RE = re.compile(r"`[^`]*`|-?\d+(?:\.\d+)?", re.IGNORECASE)
_SECTION_RE_TEMPLATE = r"\[{name}\]\s*(.*?)\s*\[/{name}\]"
_LOCK = threading.RLock()
_CONFIG: "AmplifyOptionConfig | None" = None


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().strip("`").strip().lower().split())


def _section(text: str, name: str) -> str:
    match = re.search(
        _SECTION_RE_TEMPLATE.format(name=re.escape(name)),
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _field(text: str, name: str) -> str:
    match = re.search(
        r"\[%s\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)" % re.escape(name),
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _tokens(text: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(text or "")]


def _numbers(text: str) -> list[float]:
    return [float(token) for token in _tokens(text) if not token.startswith("`")]


def _ints(text: str) -> list[int]:
    return [int(value) for value in _numbers(text)]


def _named_floats(text: str) -> Dict[str, float]:
    tokens = _tokens(text)
    out: Dict[str, float] = {}
    for index in range(0, len(tokens) - 1, 2):
        if tokens[index].startswith("`") and not tokens[index + 1].startswith("`"):
            out[_normalize_name(tokens[index])] = float(tokens[index + 1])
    return out


def _amplify_table_numbers(text: str) -> list[float]:
    for match in re.finditer(
        r"\[table\]\s*(.*?)\s*\[/table\]", text or "",
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = match.group(1)
        table_type = re.search(
            r"\[table\s+type\]\s*`([^`]*)`", body, flags=re.IGNORECASE,
        )
        if table_type is not None and _normalize_name(table_type.group(1)) == "amplify":
            return _numbers(body[table_type.end():])
    raise ValueError("amplifyupgrade.etc amplify table missing")


@dataclass(frozen=True)
class AmplifyWeightedLevel:
    level: int
    weight: int


@dataclass(frozen=True)
class AmplifyMaterialRule:
    action: int
    item_id: int
    option_code: int
    material_count: int
    random_levels: Tuple[AmplifyWeightedLevel, ...] = ()

    def selected_type(self, requested_option: int) -> int:
        if self.option_code == 5:
            selected = int(requested_option)
        else:
            selected = int(self.option_code)
        return selected if selected in AMPLIFY_TYPES else 0


@dataclass(frozen=True)
class AmplifyOptionConfig:
    material_rules: Mapping[Tuple[int, int], AmplifyMaterialRule]
    option_base_values: Mapping[int, int]
    rarity_weights: Mapping[str, float]
    equip_level_const: int
    amplify_table_rows: int
    amplify_material_ids: Tuple[int, ...]
    amplification_constants: Tuple[int, ...]

    def material_rule(self, action: int, item_id: int) -> AmplifyMaterialRule | None:
        return self.material_rules.get((int(action), int(item_id) & 0xFFFFFFFF))

    def initial_attribute_value(self, rarity: int, amplify_type: int) -> int:
        option = int(amplify_type)
        if option not in AMPLIFY_TYPES:
            return 0
        rarity_index = int(rarity)
        rarity_name = (
            _RARITY_NAMES[rarity_index]
            if 0 <= rarity_index < len(_RARITY_NAMES) else ""
        )
        base_value = int(self.option_base_values.get(option, 0))
        weight = float(self.rarity_weights.get(rarity_name, 0.0))
        value = int(base_value * weight)
        return value if 0 < value <= 0xFFFF else 0

    def roll_pure_gold_level(self, item_id: int, rng: Any) -> tuple[int, int, int]:
        rule = self.material_rule(AMPLIFY_ACTION_PURE_GOLD, item_id)
        if rule is None:
            raise ValueError("Pure Gold material is not configured")
        # Some same-name books only invest the selected red-letter attribute.
        # An absent PVF random-value section means level 0, never a shared
        # fallback distribution from another book.
        if not rule.random_levels:
            return 0, 0, 0
        total = sum(max(0, int(entry.weight)) for entry in rule.random_levels)
        if total <= 0:
            raise ValueError("Pure Gold amplification random weight is zero")
        roll = int(rng.randrange(total))
        cursor = 0
        for entry in rule.random_levels:
            cursor += max(0, int(entry.weight))
            if roll < cursor:
                return int(entry.level), roll, total
        raise AssertionError("Pure Gold amplification roll exceeded cumulative weight")


def _parse_option_mapping(text: str) -> Dict[str, int]:
    tokens = _tokens(_section(text, "option mapping table"))
    mapping: Dict[str, int] = {}
    for index in range(0, len(tokens) - 1, 2):
        if tokens[index].startswith("`") and not tokens[index + 1].startswith("`"):
            mapping[_normalize_name(tokens[index])] = int(float(tokens[index + 1]))
    return mapping


def _parse_option_base_values(text: str) -> Dict[int, int]:
    tokens = _tokens(_section(text, "option data"))
    values: Dict[int, int] = {}
    for index in range(0, len(tokens) - 2, 3):
        if not tokens[index].startswith("`"):
            continue
        option_type = _OPTION_TYPE_BY_NAME.get(_normalize_name(tokens[index]), 0)
        if option_type:
            values[option_type] = int(float(tokens[index + 2]))
    return values


def _parse_material_rules(text: str) -> Dict[Tuple[int, int], AmplifyMaterialRule]:
    option_mapping = _parse_option_mapping(text)
    rules: Dict[Tuple[int, int], AmplifyMaterialRule] = {}
    for action, section_name in _ACTION_SECTION.items():
        tokens = _tokens(_section(text, section_name))
        if len(tokens) % 3:
            raise ValueError("%s must contain option/item/count triples" % section_name)
        for index in range(0, len(tokens), 3):
            option_name = _normalize_name(tokens[index])
            option_code = int(option_mapping.get(option_name, -1))
            item_id = int(float(tokens[index + 1]))
            material_count = int(float(tokens[index + 2]))
            if option_code not in (1, 2, 3, 4, 5):
                raise ValueError("invalid amplify option %r" % option_name)
            if item_id <= 0 or material_count <= 0:
                raise ValueError("invalid amplify material in %s" % section_name)
            key = (int(action), item_id)
            rules[key] = AmplifyMaterialRule(
                action=int(action), item_id=item_id, option_code=option_code,
                material_count=material_count,
            )
    return rules


def parse_amplification_random_values(text: str) -> Tuple[AmplifyWeightedLevel, ...]:
    values = _ints(_section(text, "amplification random value"))
    if not values:
        return ()
    if len(values) % 2:
        raise ValueError("amplification random value must contain level/weight pairs")
    out = []
    for index in range(0, len(values), 2):
        level, weight = int(values[index]), int(values[index + 1])
        if not 0 <= level <= AMPLIFY_LEVEL_MAX or weight <= 0:
            raise ValueError(
                "invalid amplification random pair level=%d weight=%d" % (level, weight)
            )
        out.append(AmplifyWeightedLevel(level=level, weight=weight))
    return tuple(out)


def parse_amplify_option_config(
    amplify_item_text: str,
    amplify_upgrade_text: str,
    *,
    material_text_by_id: Mapping[int, str] | None = None,
) -> AmplifyOptionConfig:
    rules = _parse_material_rules(amplify_item_text)
    material_texts = dict(material_text_by_id or {})
    for key, rule in list(rules.items()):
        if rule.action != AMPLIFY_ACTION_PURE_GOLD:
            continue
        random_levels = parse_amplification_random_values(
            material_texts.get(rule.item_id, "")
        )
        rules[key] = AmplifyMaterialRule(
            action=rule.action,
            item_id=rule.item_id,
            option_code=rule.option_code,
            material_count=rule.material_count,
            random_levels=random_levels,
        )

    table_values = _amplify_table_numbers(amplify_upgrade_text)
    if not table_values or len(table_values) % AMPLIFY_TABLE_ROW_WIDTH:
        raise ValueError(
            "amplify table width mismatch values=%d width=%d"
            % (len(table_values), AMPLIFY_TABLE_ROW_WIDTH)
        )
    table_rows = len(table_values) // AMPLIFY_TABLE_ROW_WIDTH
    if table_rows < AMPLIFY_LEVEL_MAX:
        raise ValueError("amplify table has fewer than 31 rows")
    material_ids = tuple(
        int(table_values[offset + 14])
        for offset in range(0, len(table_values), AMPLIFY_TABLE_ROW_WIDTH)
    )
    constants = tuple(_ints(_section(amplify_upgrade_text, "amplification const")))
    config = AmplifyOptionConfig(
        material_rules=rules,
        option_base_values=_parse_option_base_values(amplify_item_text),
        rarity_weights=_named_floats(_section(amplify_item_text, "rarity weight")),
        equip_level_const=next(iter(_ints(_field(amplify_item_text, "equip level const"))), 0),
        amplify_table_rows=table_rows,
        amplify_material_ids=material_ids,
        amplification_constants=constants,
    )
    if set(config.option_base_values) != set(AMPLIFY_TYPES):
        raise ValueError("amplifyitem.etc option data is incomplete")
    if config.equip_level_const <= 0:
        raise ValueError("amplifyitem.etc equip level const is missing")
    return config


def _load_runtime_config() -> AmplifyOptionConfig:
    amplify_item_text = pvf_runtime.read_text(AMPLIFY_ITEM_PVF_PATH)
    rules = _parse_material_rules(amplify_item_text)
    material_texts: Dict[int, str] = {}
    for rule in rules.values():
        if rule.action != AMPLIFY_ACTION_PURE_GOLD:
            continue
        metadata = resolve_item_metadata(rule.item_id)
        if not metadata.source_path:
            raise ValueError("Pure Gold material path missing item=%d" % rule.item_id)
        material_texts[rule.item_id] = pvf_runtime.read_text(metadata.source_path)
    return parse_amplify_option_config(
        amplify_item_text,
        pvf_runtime.read_text(AMPLIFY_UPGRADE_PVF_PATH),
        material_text_by_id=material_texts,
    )


def get_amplify_option_config() -> AmplifyOptionConfig:
    global _CONFIG
    with _LOCK:
        if _CONFIG is None:
            _CONFIG = _load_runtime_config()
        return _CONFIG


def clear_amplify_option_config_cache() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None


def warmup_amplify_option_config() -> dict[str, int | str]:
    from game.character.upgrade_config import get_amplify_upgrade_config

    config = get_amplify_option_config()
    upgrade_config = get_amplify_upgrade_config()
    pure_gold = [
        rule for rule in config.material_rules.values()
        if rule.action == AMPLIFY_ACTION_PURE_GOLD
    ]
    missing = [rule.item_id for rule in pure_gold if not rule.random_levels]
    return {
        "path": AMPLIFY_ITEM_PVF_PATH,
        "upgrade_path": AMPLIFY_UPGRADE_PVF_PATH,
        "material_count": len(config.material_rules),
        "pure_gold_material_count": len(pure_gold),
        "pure_gold_random_material_count": len(pure_gold) - len(missing),
        "pure_gold_missing_random_count": len(missing),
        "table_rows": config.amplify_table_rows,
        "upgrade_cost_rows": len(upgrade_config.costs),
        "destroy_bonus_item": upgrade_config.destroy.bonus_item_id,
        "amplification_const_count": len(config.amplification_constants),
    }


def _extra_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray, memoryview)):
        text = bytes(raw).decode("utf-8", errors="ignore").strip()
    else:
        text = str(raw).strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def parse_amplify_state(raw: Any) -> tuple[int, int]:
    data = _extra_json_dict(raw)
    try:
        amplify_type = int(data.get("amplify_type", 0) or 0) & 0xFF
    except (TypeError, ValueError):
        amplify_type = 0
    try:
        amplify_value = int(data.get("amplify_value", 0) or 0) & 0xFFFF
    except (TypeError, ValueError):
        amplify_value = 0
    return amplify_type, amplify_value


def merge_amplify_state(raw: Any, amplify_type: int, amplify_value: int) -> str:
    data = _extra_json_dict(raw)
    data["amplify_type"] = int(amplify_type) & 0xFF
    data["amplify_value"] = int(amplify_value) & 0xFFFF
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "AMPLIFY_ACTION_INVEST", "AMPLIFY_ACTION_PURE_GOLD", "AMPLIFY_ACTION_REINVEST",
    "AMPLIFY_ITEM_PVF_PATH", "AMPLIFY_LEVEL_MAX", "AMPLIFY_TYPES",
    "AMPLIFY_TYPE_INTELLIGENCE", "AMPLIFY_TYPE_SPIRIT", "AMPLIFY_TYPE_STRENGTH",
    "AMPLIFY_TYPE_UNIDENTIFIED", "AMPLIFY_TYPE_VITALITY",
    "AMPLIFY_UPGRADE_PVF_PATH", "AmplifyMaterialRule", "AmplifyOptionConfig",
    "AmplifyWeightedLevel", "clear_amplify_option_config_cache",
    "get_amplify_option_config", "merge_amplify_state",
    "parse_amplification_random_values", "parse_amplify_option_config",
    "parse_amplify_state", "warmup_amplify_option_config",
]
