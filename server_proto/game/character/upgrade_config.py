"""Runtime-PVF configuration for normal equipment reinforcement."""
from __future__ import annotations

import math
import re
import struct
import threading
from dataclasses import dataclass
from typing import Dict, Tuple

from pvf import pvf_runtime


UPGRADE_PVF_PATH = "etc/upgrade.etc"
AMPLIFY_UPGRADE_PVF_PATH = "etc/amplifyupgrade.etc"
UPGRADE_WEIGHT_SCALE = 100000
UPGRADE_PACKED_LEVEL_MAX = 31
_UPGRADE_ROW_WIDTH = 24

_RARITY_NAMES = (
    "common", "uncommon", "rare", "unique", "epic", "chronicle", "legendary",
)
_EQUIPMENT_TYPE_INDEX = {
    "[weapon]": 0, "[title]": 1, "[title name]": 1, "[coat]": 2,
    "[shoulder]": 3, "[pants]": 4, "[shoes]": 5, "[waist]": 6,
    "[amulet]": 7, "[wrist]": 8, "[ring]": 9, "[support]": 10,
    "[magic stone]": 11, "[support weapon]": 12,
}
_UPGRADE_TARGET_TYPES = frozenset({
    "[weapon]", "[coat]", "[shoulder]", "[pants]", "[shoes]", "[waist]",
    "[amulet]", "[wrist]", "[ring]", "[support]", "[magic stone]",
    "[support weapon]", "[earring]",
})
# 01B0DCCB dispatches the actual native equipment type, not the row order.
# Type25 (earring) reads output5; type31 updates other stats and does not
# assign the unused output4 to the instance anti-evil field.
_ANTI_EVIL_CATEGORY_BY_NATIVE_TYPE = {
    12: 0, 14: 1, 15: 1, 16: 1, 17: 1, 18: 1,
    19: 2, 20: 2, 21: 2, 22: 3, 23: 3, 25: 5,
}
_TOKEN_RE = re.compile(r"`[^`]*`|-?\d+(?:\.\d+)?", re.IGNORECASE)
_SECTION_RE_TEMPLATE = r"\[{name}\]\s*(.*?)\s*\[/{name}\]"
_LOCK = threading.RLock()
_CONFIG: "UpgradeConfig | None" = None
_AMPLIFY_CONFIG: "UpgradeConfig | None" = None


def _round_away_from_zero(value: float) -> int:
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


def _normalize_token(value: str) -> str:
    return " ".join(str(value or "").strip().strip("`").strip().lower().split())


def _section(text: str, name: str) -> str:
    match = re.search(
        _SECTION_RE_TEMPLATE.format(name=re.escape(name)), text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _field(text: str, name: str) -> str:
    match = re.search(
        r"\[%s\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)" % re.escape(name), text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _tokens(text: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(text or "")]


def _numbers(text: str) -> list[float]:
    return [float(token) for token in _tokens(text) if not token.startswith("`")]


def _ints(text: str) -> list[int]:
    return [_round_away_from_zero(value) for value in _numbers(text)]


def _named_ints(text: str) -> Dict[str, int]:
    tokens = _tokens(text)
    out: Dict[str, int] = {}
    for index in range(0, len(tokens) - 1, 2):
        if tokens[index].startswith("`"):
            out[_normalize_token(tokens[index])] = _round_away_from_zero(float(tokens[index + 1]))
    return out


def _named_floats(text: str) -> Dict[str, float]:
    tokens = _tokens(text)
    out: Dict[str, float] = {}
    for index in range(0, len(tokens) - 1, 2):
        if tokens[index].startswith("`"):
            out[_normalize_token(tokens[index])] = float(tokens[index + 1])
    return out


def _upgrade_table_numbers(text: str, table_type: str) -> list[float]:
    expected = _normalize_token(table_type)
    for match in re.finditer(
        r"\[table\]\s*(.*?)\s*\[/table\]", text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = match.group(1)
        table_match = re.search(
            r"\[table\s+type\]\s*`([^`]*)`", body, flags=re.IGNORECASE,
        )
        if table_match is not None and _normalize_token(table_match.group(1)) == expected:
            return _numbers(body[table_match.end():])
    raise ValueError("upgrade table %s missing" % expected)


def _normal_table_numbers(text: str) -> list[float]:
    return _upgrade_table_numbers(text, "normal")


@dataclass(frozen=True)
class UpgradePolicy:
    failure_weight: int
    penalty_type: int
    penalty_value: int

    @property
    def success_weight(self) -> int:
        return max(0, min(UPGRADE_WEIGHT_SCALE, UPGRADE_WEIGHT_SCALE - self.failure_weight))


@dataclass(frozen=True)
class UpgradeLevelRow:
    target_level: int
    weapon_growth: Tuple[float, ...]
    weapon_failure_weight: int
    other_failure_weight: int
    weapon_penalty_type: int
    weapon_penalty_value: int
    other_penalty_type: int
    other_penalty_value: int
    material_item_id: int
    material_count: int
    other_growth: Tuple[float, ...]

    def policy(self, *, is_weapon: bool) -> UpgradePolicy:
        if is_weapon:
            return UpgradePolicy(
                self.weapon_failure_weight, self.weapon_penalty_type, self.weapon_penalty_value,
            )
        return UpgradePolicy(
            self.other_failure_weight, self.other_penalty_type, self.other_penalty_value,
        )


@dataclass(frozen=True)
class UpgradeDestroyConfig:
    bonus_item_id: int
    equipment_grade_minimum: int
    level_base: int
    weight_by_rarity: Dict[str, float]
    correction_grade_by_rarity: Dict[str, int]

    def reward_count(self, *, current_level: int, equipment_grade: int, rarity: int) -> int:
        if self.bonus_item_id <= 0 or current_level <= self.level_base:
            return 0
        rarity_name = rarity_name_for_index(rarity)
        corrected_grade = max(
            0, int(equipment_grade) - int(self.correction_grade_by_rarity.get(rarity_name, 0)),
        )
        if self.equipment_grade_minimum > 0 and corrected_grade < self.equipment_grade_minimum:
            return 0
        weight = float(self.weight_by_rarity.get(rarity_name, 0.0))
        if weight <= 0:
            return 0
        distance = int(current_level) - int(self.level_base)
        return max(0, int(distance * distance * weight))


def _native_f32(value: float) -> float:
    try:
        value = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise ValueError("invalid native anti-evil coefficient") from exc
    if not math.isfinite(value):
        raise ValueError("nonfinite native anti-evil coefficient")
    return value


@dataclass(frozen=True)
class AntiEvilUpgradeConfig:
    rarity_weights: Tuple[float, ...]
    type_weights: Tuple[int, ...]
    divisor: int

    def bonuses(self, *, rarity: int, target_level: int) -> Tuple[int, ...]:
        """Six native contribution categories, not a character total.

        02E3AD1C..02E3AFB1 reads rarity[7], type[6], divisor, then uses
        MULSS/DIVSS/MULSS/ADDSS and CVTTSS2SI. The input table index is
        zero-based; target_level is that index plus one. Instance assignment
        and complete player multipliers remain separate.
        """
        if (type(rarity) is not int or not 0 <= rarity < len(self.rarity_weights)
                or type(target_level) is not int or not 1 <= target_level <= 256):
            raise ValueError("invalid native anti-evil rarity or table row")
        scale = _native_f32(self.rarity_weights[rarity] * 4.0)
        values = []
        for weight in self.type_weights:
            value = _native_f32(_native_f32(float(weight)) * scale)
            value = _native_f32(value / _native_f32(float(self.divisor)))
            value = _native_f32(value * _native_f32(float(target_level)))
            rounded = int(_native_f32(value + 0.5))
            if not 0 <= rounded <= 0x7fffffff:
                raise ValueError("native anti-evil contribution exceeds signed integer")
            values.append(rounded)
        return tuple(values)


def parse_anti_evil_upgrade(text: str) -> AntiEvilUpgradeConfig | None:
    """Absent policy stays unknown; a present incomplete policy is invalid."""
    starts = list(re.finditer(r"(?mi)^\s*\[anti evil upgrade\]\s*$", text))
    ends = list(re.finditer(r"(?mi)^\s*\[/anti evil upgrade\]\s*$", text))
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0].end() >= ends[0].start():
        raise ValueError("anti evil upgrade requires one complete policy")
    body = text[starts[0].end():ends[0].start()]
    tags = list(re.finditer(r"(?mi)^\s*\[([^\]]+)\]\s*$", body))
    names = tuple(tag.group(1).lower() for tag in tags)
    if len(names) != 3 or set(names) != {"rarity weights", "type weights", "div weights"}:
        raise ValueError("anti evil upgrade fields missing or repeated")
    if body[:tags[0].start()].strip():
        raise ValueError("unexpected anti evil upgrade prefix")
    fields = {name: body[tag.end():tags[i + 1].start() if i + 1 < len(tags) else len(body)].split()
              for i, (name, tag) in enumerate(zip(names, tags))}
    rarity, types, div = (fields[name] for name in ("rarity weights", "type weights", "div weights"))
    if (len(rarity) != 7 or len(types) != 6 or len(div) != 1
            or any(re.fullmatch(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value) is None for value in rarity)
            or any(re.fullmatch(r"\d+", value) is None for value in types + div)):
        raise ValueError("invalid anti evil upgrade coefficient shape")
    rarity_values = tuple(_native_f32(float(value)) for value in rarity)
    type_values, divisor = tuple(int(value) for value in types), int(div[0])
    if divisor <= 0 or divisor > 0x7fffffff or any(value > 0x7fffffff for value in type_values):
        raise ValueError("invalid anti evil upgrade integer coefficient")
    return AntiEvilUpgradeConfig(rarity_values, type_values, divisor)


@dataclass(frozen=True)
class UpgradeConfig:
    rows: Tuple[UpgradeLevelRow, ...]
    notice_level: int
    costs: Tuple[int, ...]
    rarity_cost_weights: Tuple[float, ...]
    equipment_type_weights: Tuple[float, ...]
    cost_weight_by_level: Dict[int, Tuple[float, float]]
    max_level_by_rarity: Dict[str, int]
    destroy: UpgradeDestroyConfig
    anti_evil_upgrade: AntiEvilUpgradeConfig | None = None

    def anti_evil_bonuses(self, *, rarity: int, target_level: int) -> Tuple[int, ...]:
        if self.anti_evil_upgrade is None or self.row_for_target_level(target_level) is None:
            raise ValueError("native anti-evil policy or actual upgrade row unavailable")
        return self.anti_evil_upgrade.bonuses(rarity=rarity, target_level=target_level)

    def anti_evil_bonus(
        self, *, rarity: int, target_level: int, native_equipment_type: int,
    ) -> int:
        """The proven positive-upgrade instance contribution at native +8CC.

        Missing policy/row and unsupported native types remain unknown. This
        does not include the template, forging maximum, or player effects.
        """
        if (type(native_equipment_type) is not int
                or native_equipment_type not in _ANTI_EVIL_CATEGORY_BY_NATIVE_TYPE):
            raise ValueError("native anti-evil equipment type unavailable")
        category = _ANTI_EVIL_CATEGORY_BY_NATIVE_TYPE[native_equipment_type]
        return self.anti_evil_bonuses(rarity=rarity, target_level=target_level)[category]

    def row_for_target_level(self, target_level: int) -> UpgradeLevelRow | None:
        index = int(target_level) - 1
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def max_level(self, rarity: int) -> int:
        configured = int(self.max_level_by_rarity.get(
            rarity_name_for_index(rarity), UPGRADE_PACKED_LEVEL_MAX,
        ))
        if configured <= 0:
            configured = UPGRADE_PACKED_LEVEL_MAX
        return min(UPGRADE_PACKED_LEVEL_MAX, configured, len(self.rows))

    def gold_cost(
        self, *, minimum_level: int, rarity: int, equipment_type: str, current_level: int,
    ) -> int | None:
        cost_index = max(0, int(minimum_level)) + max(0, int(rarity) - 1) * 2
        if cost_index >= len(self.costs):
            return None
        rarity_weight = (
            float(self.rarity_cost_weights[int(rarity)])
            if 0 <= int(rarity) < len(self.rarity_cost_weights) else 1.0
        )
        normalized_type = _normalize_token(equipment_type)
        type_index = _EQUIPMENT_TYPE_INDEX.get(normalized_type)
        type_weight = (
            float(self.equipment_type_weights[type_index])
            if type_index is not None and type_index < len(self.equipment_type_weights) else 1.0
        )
        level_weights = self.cost_weight_by_level.get(int(current_level), (1.0, 1.0))
        level_weight = float(level_weights[0] if is_weapon_type(normalized_type) else level_weights[1])
        total = max(0, int(self.costs[cost_index])) * rarity_weight * type_weight * level_weight
        return max(0, _round_away_from_zero(total))


def rarity_name_for_index(rarity: int) -> str:
    index = int(rarity)
    return _RARITY_NAMES[index] if 0 <= index < len(_RARITY_NAMES) else ""


def is_weapon_type(equipment_type: str) -> bool:
    return _normalize_token(equipment_type) in {"[weapon]", "[support weapon]"}


def is_upgrade_target_type(equipment_type: str) -> bool:
    return _normalize_token(equipment_type) in _UPGRADE_TARGET_TYPES


def parse_upgrade_config(
    text: str,
    *,
    table_type: str = "normal",
    source_name: str = "upgrade.etc",
) -> UpgradeConfig:
    normalized_table_type = _normalize_token(table_type)
    table_values = _upgrade_table_numbers(text, normalized_table_type)
    if not table_values or len(table_values) % _UPGRADE_ROW_WIDTH:
        raise ValueError(
            "%s %s table width mismatch values=%d width=%d"
            % (source_name, normalized_table_type, len(table_values), _UPGRADE_ROW_WIDTH)
        )
    rows: list[UpgradeLevelRow] = []
    for offset in range(0, len(table_values), _UPGRADE_ROW_WIDTH):
        values = table_values[offset:offset + _UPGRADE_ROW_WIDTH]
        row = UpgradeLevelRow(
            target_level=len(rows) + 1,
            weapon_growth=tuple(values[0:8]),
            weapon_failure_weight=_round_away_from_zero(values[8]),
            other_failure_weight=_round_away_from_zero(values[9]),
            weapon_penalty_type=_round_away_from_zero(values[10]),
            weapon_penalty_value=_round_away_from_zero(values[11]),
            other_penalty_type=_round_away_from_zero(values[12]),
            other_penalty_value=_round_away_from_zero(values[13]),
            material_item_id=_round_away_from_zero(values[14]),
            material_count=_round_away_from_zero(values[15]),
            other_growth=tuple(values[16:24]),
        )
        if not 0 <= row.weapon_failure_weight <= UPGRADE_WEIGHT_SCALE:
            raise ValueError("weapon failure weight out of range target=%d" % row.target_level)
        if not 0 <= row.other_failure_weight <= UPGRADE_WEIGHT_SCALE:
            raise ValueError("other failure weight out of range target=%d" % row.target_level)
        if row.weapon_penalty_type not in (1, 2, 3) or row.other_penalty_type not in (1, 2, 3):
            raise ValueError("penalty type invalid target=%d" % row.target_level)
        if row.material_item_id <= 0 or row.material_count <= 0:
            raise ValueError("material invalid target=%d" % row.target_level)
        rows.append(row)

    level_values = _numbers(_section(text, "cost weight by upgrade level"))
    if len(level_values) % 3:
        raise ValueError("cost weight by upgrade level must contain triples")
    level_costs: Dict[int, Tuple[float, float]] = {}
    for offset in range(0, len(level_values), 3):
        level_costs[_round_away_from_zero(level_values[offset])] = (
            float(level_values[offset + 1]), float(level_values[offset + 2]),
        )

    disjoint = _section(text, "disjoint")
    destroy = UpgradeDestroyConfig(
        bonus_item_id=next(iter(_ints(_field(disjoint, "disjoint bonus item"))), 0),
        equipment_grade_minimum=next(iter(_ints(_field(disjoint, "equip level const"))), 0),
        level_base=next(iter(_ints(_field(disjoint, "upgrade const for bonus item count"))), 0),
        weight_by_rarity=_named_floats(_section(disjoint, "upgrade failed bonus weight by rarity")),
        correction_grade_by_rarity=_named_ints(_section(disjoint, "correction grade by rarity")),
    )
    config = UpgradeConfig(
        rows=tuple(rows),
        notice_level=next(iter(_ints(_field(text, "notice"))), -1),
        costs=tuple(_ints(_section(text, "cost"))),
        rarity_cost_weights=tuple(_numbers(_section(text, "cost weights by rarity"))),
        equipment_type_weights=tuple(_numbers(_section(text, "type"))),
        cost_weight_by_level=level_costs,
        max_level_by_rarity=_named_ints(_section(text, "max upgrade level by rarity")),
        destroy=destroy,
        anti_evil_upgrade=parse_anti_evil_upgrade(text),
    )
    if len(config.rows) < UPGRADE_PACKED_LEVEL_MAX:
        raise ValueError("%s table has fewer than 31 rows" % normalized_table_type)
    if not config.costs or not config.rarity_cost_weights or not config.equipment_type_weights:
        raise ValueError("%s cost sections missing" % source_name)
    return config


def parse_amplify_upgrade_config(text: str) -> UpgradeConfig:
    return parse_upgrade_config(
        text,
        table_type="amplify",
        source_name="amplifyupgrade.etc",
    )


def get_upgrade_config() -> UpgradeConfig:
    global _CONFIG
    with _LOCK:
        if _CONFIG is None:
            _CONFIG = parse_upgrade_config(pvf_runtime.read_text(UPGRADE_PVF_PATH))
        return _CONFIG


def get_amplify_upgrade_config() -> UpgradeConfig:
    global _AMPLIFY_CONFIG
    with _LOCK:
        if _AMPLIFY_CONFIG is None:
            _AMPLIFY_CONFIG = parse_amplify_upgrade_config(
                pvf_runtime.read_text(AMPLIFY_UPGRADE_PVF_PATH)
            )
        return _AMPLIFY_CONFIG


def clear_upgrade_config_cache() -> None:
    global _CONFIG, _AMPLIFY_CONFIG
    with _LOCK:
        _CONFIG = None
        _AMPLIFY_CONFIG = None


def clear_amplify_upgrade_config_cache() -> None:
    global _AMPLIFY_CONFIG
    with _LOCK:
        _AMPLIFY_CONFIG = None


def warmup_upgrade_config() -> dict[str, int | str]:
    config = get_upgrade_config()
    return {
        "path": UPGRADE_PVF_PATH, "table_rows": len(config.rows),
        "cost_rows": len(config.costs), "notice_level": config.notice_level,
        "destroy_bonus_item": config.destroy.bonus_item_id,
    }


__all__ = [
    "AMPLIFY_UPGRADE_PVF_PATH", "UPGRADE_PACKED_LEVEL_MAX", "UPGRADE_WEIGHT_SCALE",
    "UpgradeConfig",
    "AntiEvilUpgradeConfig", "parse_anti_evil_upgrade",
    "UpgradeDestroyConfig", "UpgradeLevelRow", "UpgradePolicy",
    "clear_amplify_upgrade_config_cache", "clear_upgrade_config_cache",
    "get_amplify_upgrade_config", "get_upgrade_config", "is_upgrade_target_type",
    "is_weapon_type", "parse_amplify_upgrade_config", "parse_upgrade_config",
    "rarity_name_for_index", "warmup_upgrade_config",
]
