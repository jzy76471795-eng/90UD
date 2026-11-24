"""PVF-backed character experience consumables.

The client sends only the Main inventory slot for C1/001E.  The item template,
not the quick-slot position, selects the grant.  This module is warmed once at
boot and is then read-only on the gameplay path.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"

KIND_FIXED = "fixed"
KIND_PERCENT = "percent"
KIND_TABLE = "table"
KIND_LEVEL_TABLE = "level_table"
KIND_CRACK = "crack_of_dimension"

_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
_LOCALIZED_NUMERAL_CHARS = (
    "零〇一二两兩三四五六七八九十拾百佰千仟万萬亿億"
    "壹贰貳叁參肆伍陆陸柒捌玖"
)
_LEGACY_AMOUNT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9][0-9,， ]*(?:[万萬亿億])?|"
    rf"[{_LOCALIZED_NUMERAL_CHARS}]+)(?![A-Za-z0-9])"
)
_LEGACY_EXPERIENCE_USE_WAV = "experience_up_item"
_SKILL_POINT_USE_WAV = "sp_up_item"
_STATUS_AMOUNT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9][0-9,， ]*(?:[万萬亿億])?|"
    rf"[{_LOCALIZED_NUMERAL_CHARS}]+)(?![A-Za-z0-9])"
)
_SKILL_POINT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sp", re.compile(r"(?<![A-Za-z])sp(?![A-Za-z])", re.IGNORECASE)),
    (
        "sp",
        re.compile(
            r"(?<!特性)技能(?:点|點)(?:数|數)?(?:书|書)?|skill\s*points?",
            re.IGNORECASE,
        ),
    ),
    ("tp", re.compile(r"(?<![A-Za-z])tp(?![A-Za-z])", re.IGNORECASE)),
    (
        "tp",
        re.compile(
            r"特性(?:技能)?(?:点|點)(?:数|數)?(?:书|書)?",
            re.IGNORECASE,
        ),
    ),
)
_STATUS_RELATION_WORDS = (
    "increases",
    "increased",
    "increase",
    "adds",
    "add",
    "points",
    "point",
    "by",
    "skill",
    "book",
    "技能点数",
    "技能點數",
    "技能点",
    "技能點",
    "技能",
    "特性技能点数",
    "特性技能點數",
    "特性技能点",
    "特性技能點",
    "特性点数",
    "特性點數",
    "特性点",
    "特性點",
    "点数",
    "點數",
    "点",
    "點",
    "书",
    "書",
    "增加了",
    "增加",
    "提升",
    "提高",
    "获得",
    "獲得",
    "获取",
    "獲取",
    "加",
)
_JOB_LABELS = (
    "swordman",
    "fighter",
    "gunner",
    "mage",
    "priest",
    "at gunner",
    "thief",
    "at fighter",
    "at mage",
    "demonic swordman",
    "creator mage",
    "at swordman",
    "knight",
    "demonic lancer",
    "at priest",
)

_INDEX_LOCK = threading.RLock()
_INDEX: "ExperienceConsumableIndex | None" = None


def _section(text: str, name: str) -> str:
    """Read one PVF section while retaining its backtick-wrapped child tag."""
    lines = str(text or "").splitlines()
    wanted = "[" + str(name).strip().lower().strip("[]") + "]"
    start = -1
    for index, line in enumerate(lines):
        if line.strip().lower() == wanted:
            start = index + 1
            break
    if start < 0:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[/"):
            break
        if stripped.startswith("[/"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _numbers(text: str) -> tuple[int, ...]:
    values: list[int] = []
    for token in _NUMBER_RE.findall(str(text or "")):
        try:
            value = int(float(token))
        except ValueError:
            continue
        values.append(value)
    return tuple(values)


def _child_tag(body: str) -> str:
    match = re.search(r"\[([^\]]+)\]", str(body or ""))
    return str(match.group(1)).strip().lower() if match else ""


def _scalar_token(body: str) -> str:
    text = str(body or "").strip()
    quoted = re.search(r"`([^`]*)`", text)
    if quoted is not None:
        return str(quoted.group(1)).strip().lower()
    first_line = text.splitlines()[0] if text else ""
    return str(first_line).strip().lower()


def _legacy_fixed_experience_value(text: str) -> int | None:
    """Read old books whose fixed amount exists only in display sections."""
    if _scalar_token(_section(text, "use wav")) != _LEGACY_EXPERIENCE_USE_WAV:
        return None

    def _positive_values(section_name: str) -> set[int]:
        values: set[int] = set()
        display = unicodedata.normalize("NFKC", _section(text, section_name))
        for token in _LEGACY_AMOUNT_RE.findall(display):
            value = _parse_localized_positive_int(token)
            if value is not None and value > 0:
                values.add(value)
        return values

    # The use-wav token identifies player EXP. Requiring the same unique value
    # in both display fields prevents unrelated numbers from becoming grants.
    matching = _positive_values("name") & _positive_values("explain")
    if len(matching) != 1:
        return None
    return next(iter(matching))


def _parse_localized_positive_int(value: str) -> int | None:
    """Parse an Arabic or common Chinese integer used in display text."""
    compact = unicodedata.normalize("NFKC", str(value or ""))
    compact = compact.replace(",", "").replace("，", "").replace(" ", "")
    compact = compact.translate(
        str.maketrans(
            {
                "兩": "两",
                "萬": "万",
                "億": "亿",
                "壹": "一",
                "贰": "二",
                "貳": "二",
                "叁": "三",
                "參": "三",
                "肆": "四",
                "伍": "五",
                "陆": "六",
                "陸": "六",
                "柒": "七",
                "捌": "八",
                "玖": "九",
                "拾": "十",
                "佰": "百",
                "仟": "千",
            }
        )
    )
    if compact.isdigit():
        result = int(compact)
        return result if result > 0 else None
    mixed_arabic = re.fullmatch(r"(\d+)([万亿]?)", compact)
    if mixed_arabic is not None:
        value_int = int(mixed_arabic.group(1))
        unit = mixed_arabic.group(2)
        multiplier = {"": 1, "万": 10_000, "亿": 100_000_000}[unit]
        result = value_int * multiplier
        return result if result > 0 else None
    if not compact or any(char not in "零〇一二两三四五六七八九十百千万亿" for char in compact):
        return None
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    small_units = {"十": 10, "百": 100, "千": 1000}
    large_units = {"万": 10_000, "亿": 100_000_000}
    if not any(char in small_units or char in large_units for char in compact):
        result = int("".join(str(digits[char]) for char in compact))
        return result if result > 0 else None
    total = 0
    section = 0
    current = 0
    for char in compact:
        if char in digits:
            current = digits[char]
            continue
        if char in small_units:
            section += (current or 1) * small_units[char]
            current = 0
            continue
        if char in large_units:
            section += current
            total += (section or 1) * large_units[char]
            section = 0
            current = 0
    result = total + section + current
    return result if result > 0 else None


def _status_relation_is_valid(value: str) -> bool:
    """Keep only connector text between a point marker and its amount."""
    compact = unicodedata.normalize("NFKC", str(value or "")).lower()
    compact = re.sub(r"[\s+\-=:,，。.!?;；、()（）\[\]{}]", "", compact)
    for word in sorted(_STATUS_RELATION_WORDS, key=len, reverse=True):
        compact = compact.replace(word, "")
    return not compact


def _skill_point_mentions(text: str) -> set[tuple[str, int]]:
    """Return explicit ``(sp|tp, amount)`` mentions from one display field.

    Stage3 uses both compact labels (``TP +5``) and prose (``Increases TP by
    5``). Translated PVFs commonly put the amount first (``增加5点TP``), so
    the parser accepts either order while keeping the semantic marker and
    positive amount mandatory.
    """
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    amount_matches = tuple(_STATUS_AMOUNT_RE.finditer(normalized))
    mentions: set[tuple[str, int]] = set()
    for point_kind, marker_pattern in _SKILL_POINT_MARKERS:
        for marker in marker_pattern.finditer(normalized):
            for amount_match in amount_matches:
                if amount_match.start() >= marker.end():
                    between = normalized[marker.end() : amount_match.start()]
                elif marker.start() >= amount_match.end():
                    between = normalized[amount_match.end() : marker.start()]
                else:
                    continue
                if len(between) > 32 or not _status_relation_is_valid(between):
                    continue
                amount = _parse_localized_positive_int(amount_match.group(0))
                if amount is not None and amount <= 0xFFFFFFFF:
                    mentions.add((point_kind, amount))
    return mentions


def _parse_paths(text: str) -> tuple[tuple[int, str], ...]:
    rows: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative or item_id in seen:
            continue
        seen.add(item_id)
        path = relative if relative.lower().startswith("stackable/") else "stackable/" + relative
        rows.append((item_id, path))
    if not rows:
        raise ValueError("stackable.lst has no entries")
    return tuple(rows)


def _parse_jobs(body: str) -> tuple[str, ...]:
    labels = [str(value).strip().lower() for value in re.findall(r"`([^`]*)`", body or "")]
    return tuple(label for label in labels if label)


def _optional_int(text: str) -> int | None:
    values = _numbers(text)
    return int(values[0]) if values else None


@dataclass(frozen=True)
class ExperienceConsumableRule:
    item_id: int
    kind: str
    values: tuple[int, ...]
    source_path: str
    minimum_level: int | None = None
    maximum_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()

    @property
    def is_hardcore_blocked(self) -> bool:
        return any("hardcore mode" in value for value in self.impossible_contents)

    def is_usable(self, *, level: int, job: int | None = None, hardcore: bool = False) -> tuple[bool, str]:
        current_level = max(1, int(level))
        if self.minimum_level is not None and current_level < int(self.minimum_level):
            return False, "below_minimum_level"
        if self.maximum_level is not None and current_level > int(self.maximum_level):
            return False, "above_maximum_level"
        if hardcore and self.is_hardcore_blocked:
            return False, "hardcore_mode"
        allowed = {label.lower().strip("[]") for label in self.usable_jobs}
        if allowed and "all" not in allowed and job is not None:
            index = int(job)
            label = _JOB_LABELS[index] if 0 <= index < len(_JOB_LABELS) else ""
            if label not in allowed:
                return False, "job_restricted"
        return True, "ok"

    def table_value(self, level: int) -> int:
        values = tuple(int(value) for value in self.values)
        for index in range(0, len(values) - 2, 3):
            minimum, maximum, amount = values[index : index + 3]
            if int(minimum) <= int(level) <= int(maximum):
                return max(0, int(amount))
        return 0

    def level_target(self, level: int) -> int:
        values = tuple(int(value) for value in self.values)
        for index in range(0, len(values) - 2, 3):
            minimum, maximum, target = values[index : index + 3]
            if int(minimum) <= int(level) <= int(maximum):
                return max(1, int(target))
        return 0


@dataclass(frozen=True)
class SkillPointConsumableRule:
    item_id: int
    point_kind: str
    amount: int
    source_path: str
    minimum_level: int | None = None
    maximum_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()

    @property
    def effect(self) -> tuple[int, int]:
        if self.point_kind == "sp":
            return int(self.amount), 0
        if self.point_kind == "tp":
            return 0, int(self.amount)
        return 0, 0

    @property
    def is_hardcore_blocked(self) -> bool:
        return any("hardcore mode" in value for value in self.impossible_contents)

    def is_usable(
        self,
        *,
        level: int,
        job: int | None = None,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        current_level = max(1, int(level))
        if self.minimum_level is not None and current_level < int(self.minimum_level):
            return False, "below_minimum_level"
        if self.maximum_level is not None and current_level > int(self.maximum_level):
            return False, "above_maximum_level"
        if hardcore and self.is_hardcore_blocked:
            return False, "hardcore_mode"
        allowed = {label.lower().strip("[]") for label in self.usable_jobs}
        if allowed and "all" not in allowed and job is not None:
            index = int(job)
            label = _JOB_LABELS[index] if 0 <= index < len(_JOB_LABELS) else ""
            if label not in allowed:
                return False, "job_restricted"
        return True, "ok"

@dataclass(frozen=True)
class ExperienceConsumableIndex:
    rules_by_item_id: dict[int, ExperienceConsumableRule]
    scanned_stackable_count: int
    skill_point_rules_by_item_id: dict[int, SkillPointConsumableRule] = field(
        default_factory=dict
    )

    @property
    def rule_count(self) -> int:
        return len(self.rules_by_item_id)

    def rule_for(self, item_id: int) -> ExperienceConsumableRule | None:
        return self.rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    @property
    def skill_point_rule_count(self) -> int:
        return len(self.skill_point_rules_by_item_id)

    def skill_point_rule_for(self, item_id: int) -> SkillPointConsumableRule | None:
        return self.skill_point_rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)


def parse_experience_consumable_rule(
    item_id: int,
    text: str,
    *,
    source_path: str,
) -> ExperienceConsumableRule | None:
    body = _section(text, "increase status type")
    effect = _child_tag(body)
    kind_by_effect = {
        "expup": KIND_FIXED,
        "exppercentup": KIND_PERCENT,
        "expupbypercent": KIND_PERCENT,
        "expupbytable": KIND_TABLE,
        "levelupbytable": KIND_LEVEL_TABLE,
        "expupbycrackofdimension": KIND_CRACK,
    }
    kind = kind_by_effect.get(effect)
    if kind is None:
        if effect:
            return None
        legacy_value = _legacy_fixed_experience_value(text)
        if legacy_value is None:
            return None
        kind = KIND_FIXED
        values = (legacy_value,)
    else:
        values = _numbers(body)
    if kind in {KIND_FIXED, KIND_PERCENT} and len(values) != 1:
        return None
    if kind == KIND_PERCENT and not 0 < values[0] <= 100:
        return None
    if kind in {KIND_TABLE, KIND_LEVEL_TABLE} and len(values) < 3:
        return None
    minimum = _optional_int(_section(text, "minimum level"))
    maximum = _optional_int(_section(text, "maximum level"))
    if minimum is not None and maximum is not None and minimum > maximum:
        return None
    stackable_type = _section(text, "stackable type").lower()
    if stackable_type and "[etc]" not in stackable_type:
        return None
    return ExperienceConsumableRule(
        item_id=int(item_id) & 0xFFFFFFFF,
        kind=kind,
        values=tuple(values),
        source_path=str(source_path),
        minimum_level=minimum,
        maximum_level=maximum,
        usable_jobs=_parse_jobs(_section(text, "usable job")),
        impossible_contents=tuple(
            value.strip().lower()
            for value in re.findall(r"`([^`]*)`", _section(text, "impossible contents"))
            if value.strip()
        ),
    )


def parse_skill_point_consumable_rule(
    item_id: int,
    text: str,
    *,
    source_path: str,
) -> SkillPointConsumableRule | None:
    """Parse a PVF-authored SP/TP book without relying on template IDs."""
    if _scalar_token(_section(text, "use wav")) != _SKILL_POINT_USE_WAV:
        return None
    stackable_type = _section(text, "stackable type").lower()
    if stackable_type and "[etc]" not in stackable_type:
        return None
    name_mentions = _skill_point_mentions(_section(text, "name"))
    explain_mentions = _skill_point_mentions(_section(text, "explain"))
    # Either display field may carry the machine-readable amount. If both do,
    # the union still has to describe exactly one effect.
    matching = name_mentions | explain_mentions
    if len(matching) != 1:
        return None
    point_kind, amount = next(iter(matching))
    minimum = _optional_int(_section(text, "minimum level"))
    maximum = _optional_int(_section(text, "maximum level"))
    if minimum is not None and maximum is not None and minimum > maximum:
        return None
    return SkillPointConsumableRule(
        item_id=int(item_id) & 0xFFFFFFFF,
        point_kind=str(point_kind),
        amount=int(amount),
        source_path=str(source_path),
        minimum_level=minimum,
        maximum_level=maximum,
        usable_jobs=_parse_jobs(_section(text, "usable job")),
        impossible_contents=tuple(
            value.strip().lower()
            for value in re.findall(r"`([^`]*)`", _section(text, "impossible contents"))
            if value.strip()
        ),
    )


def build_experience_consumable_index_from_reader(reader: Any) -> ExperienceConsumableIndex:
    paths = _parse_paths(reader.read_text(STACKABLE_LIST_PATH))
    rules: dict[int, ExperienceConsumableRule] = {}
    skill_point_rules: dict[int, SkillPointConsumableRule] = {}
    for item_id, source_path in paths:
        try:
            text = reader.read_text(source_path)
        except Exception:
            continue
        try:
            rule = parse_experience_consumable_rule(
                item_id, text, source_path=source_path
            )
        except Exception:
            rule = None
        if rule is not None:
            rules[int(item_id)] = rule
        try:
            skill_point_rule = parse_skill_point_consumable_rule(
                item_id, text, source_path=source_path
            )
        except Exception:
            skill_point_rule = None
        if skill_point_rule is not None:
            skill_point_rules[int(item_id)] = skill_point_rule
    if not rules and not skill_point_rules:
        raise ValueError("PVF contains no status consumables")
    return ExperienceConsumableIndex(
        rules_by_item_id=dict(sorted(rules.items())),
        scanned_stackable_count=len(paths),
        skill_point_rules_by_item_id=dict(sorted(skill_point_rules.items())),
    )


def install_experience_consumable_index(index: ExperienceConsumableIndex) -> None:
    if not isinstance(index, ExperienceConsumableIndex) or (
        index.rule_count <= 0 and index.skill_point_rule_count <= 0
    ):
        raise ValueError("invalid status consumable index")
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = index


def clear_experience_consumable_index() -> None:
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None


def get_experience_consumable_index() -> ExperienceConsumableIndex:
    with _INDEX_LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("experience consumable index is not warmed")
    return index


def resolve_experience_consumable(item_id: int) -> ExperienceConsumableRule | None:
    with _INDEX_LOCK:
        index = _INDEX
    return index.rule_for(item_id) if index is not None else None


def resolve_skill_point_consumable(item_id: int) -> SkillPointConsumableRule | None:
    with _INDEX_LOCK:
        index = _INDEX
    return index.skill_point_rule_for(item_id) if index is not None else None


def warmup_experience_consumable_index() -> dict[str, Any]:
    index = build_experience_consumable_index_from_reader(
        pvf_runtime.ensure_shared_reader()
    )
    install_experience_consumable_index(index)
    counts: dict[str, int] = {}
    for rule in index.rules_by_item_id.values():
        counts[rule.kind] = counts.get(rule.kind, 0) + 1
    skill_point_counts: dict[str, int] = {}
    for rule in index.skill_point_rules_by_item_id.values():
        skill_point_counts[rule.point_kind] = (
            skill_point_counts.get(rule.point_kind, 0) + 1
        )
    return {
        "ok": 1,
        "scanned_stackable_count": index.scanned_stackable_count,
        "experience_consumable_count": index.rule_count,
        "skill_point_consumable_count": index.skill_point_rule_count,
        "effect_counts": counts,
        "skill_point_effect_counts": skill_point_counts,
    }


def calculate_experience_gain(
    rule: ExperienceConsumableRule,
    *,
    level: int,
    current_exp: int,
    thresholds: Iterable[int],
) -> tuple[int, int | None]:
    """Return ``(exp_delta, level_target)`` for one item use."""
    current_level = max(1, int(level))
    if rule.kind == KIND_FIXED:
        return max(0, int(rule.values[0])), None
    if rule.kind == KIND_PERCENT:
        table = tuple(int(value) for value in thresholds)
        current_index = current_level - 1
        previous = 0 if current_level <= 1 else table[current_level - 2]
        if current_index < 0 or current_index >= len(table):
            return 0, None
        segment = max(0, table[current_index] - previous)
        return min(0xFFFFFFFF, segment * int(rule.values[0]) // 100), None
    if rule.kind == KIND_TABLE:
        return rule.table_value(current_level), None
    if rule.kind == KIND_LEVEL_TABLE:
        target = rule.level_target(current_level)
        if target <= current_level:
            return 0, target
        table = tuple(int(value) for value in thresholds)
        index = target - 2
        if index < 0 or index >= len(table):
            return 0, target
        return max(0, table[index] - int(current_exp)), target
    if rule.kind == KIND_CRACK:
        # Crack growth capsules grant 1% of the current level's complete EXP
        # segment, independent of the already-filled progress in that segment.
        table = tuple(int(value) for value in thresholds)
        current_index = current_level - 1
        previous = 0 if current_level <= 1 else table[current_level - 2]
        if 0 <= current_index < len(table):
            segment = max(0, table[current_index] - previous)
            return segment // 100, None
    return 0, None


__all__ = [
    "ExperienceConsumableIndex",
    "ExperienceConsumableRule",
    "SkillPointConsumableRule",
    "KIND_CRACK",
    "KIND_FIXED",
    "KIND_LEVEL_TABLE",
    "KIND_PERCENT",
    "KIND_TABLE",
    "build_experience_consumable_index_from_reader",
    "calculate_experience_gain",
    "clear_experience_consumable_index",
    "get_experience_consumable_index",
    "install_experience_consumable_index",
    "parse_experience_consumable_rule",
    "parse_skill_point_consumable_rule",
    "resolve_experience_consumable",
    "resolve_skill_point_consumable",
    "warmup_experience_consumable_index",
]
