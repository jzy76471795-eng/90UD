"""PVF-derived damage-font item rules for frozen Stage3."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable

from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
DAMAGE_FONT_LIST_PATH = "event/damagefontskin/damagefontskin.lst"
DAMAGE_FONT_ACTION_TYPE = "[add damage font skin]"
DAMAGE_FONT_CACHE_DOMAIN = "damage_font_items"
DAMAGE_FONT_CACHE_VERSION = 3

EXPIRATION_PERIOD = "period"
EXPIRATION_DATE = "date"

_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_ACTION_RE = re.compile(
    r"\[action\s+type\]\s*(.*?)\[/action\s+type\]",
    re.IGNORECASE | re.DOTALL,
)
_DAMAGE_FONT_INFO_RE = re.compile(
    r"\[damage\s+font\s+info\]\s*(.*?)\[/damage\s+font\s+info\]",
    re.IGNORECASE | re.DOTALL,
)
_LOCK = threading.RLock()
_INDEX: "DamageFontIndex | None" = None


def _normalize_token(value: object) -> str:
    text = str(value or "").strip().replace("`", "").lower()
    return " ".join(text.split())


def _first_token(text: str) -> str:
    tick = re.search(r"`([^`]*)`", str(text or ""))
    if tick is not None:
        return _normalize_token(tick.group(1))
    for line in str(text or "").splitlines():
        token = _normalize_token(line)
        if token:
            return token
    return ""


def _parse_list_paths(text: str, *, prefix: str) -> tuple[tuple[int, str], ...]:
    normalized_prefix = str(prefix or "").strip().replace("\\", "/").rstrip("/")
    if not normalized_prefix:
        raise ValueError("PVF list prefix is empty")
    entries: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _LST_RE.finditer(str(text or "")):
        entry_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if entry_id < 0 or not relative:
            continue
        if entry_id in seen:
            raise ValueError("duplicate %s list id=%d" % (normalized_prefix, entry_id))
        seen.add(entry_id)
        path = (
            relative
            if relative.lower().startswith(normalized_prefix.lower() + "/")
            else normalized_prefix + "/" + relative
        )
        entries.append((entry_id, path))
    if not entries:
        raise ValueError("%s list has no entries" % normalized_prefix)
    return tuple(entries)


def _field_integer(text: str, field_name: str) -> int | None:
    match = re.search(
        r"(?im)^\s*\[%s\]\s*$\s*`?\s*([+-]?\d+)\s*`?"
        % re.escape(field_name),
        str(text or ""),
    )
    return int(match.group(1)) if match is not None else None


@dataclass(frozen=True)
class DamageFontRule:
    item_id: int
    font_id: int
    expiration_mode: str
    expiration_value: int
    source_path: str = ""

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        font_id = int(self.font_id)
        if not 0 < item_id <= 0xFFFFFFFF:
            raise ValueError("invalid damage-font item id=%d" % item_id)
        if not 0 < font_id <= 0xFFFF:
            raise ValueError(
                "invalid damage-font resource id=%d item=%d" % (font_id, item_id)
            )
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "font_id", font_id)
        object.__setattr__(self, "expiration_mode", EXPIRATION_DATE)
        object.__setattr__(self, "expiration_value", 0)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    def to_cache_row(self) -> list[Any]:
        return [
            self.item_id,
            self.font_id,
            self.expiration_mode,
            self.expiration_value,
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "DamageFontRule":
        if not isinstance(row, list) or len(row) != 5:
            raise ValueError("invalid damage-font cache row")
        return cls(
            item_id=int(row[0]),
            font_id=int(row[1]),
            expiration_mode=str(row[2] or ""),
            expiration_value=int(row[3]),
            source_path=str(row[4] or ""),
        )


def parse_damage_font_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> DamageFontRule | None:
    action_match = _ACTION_RE.search(str(text or ""))
    if action_match is None:
        return None
    if _first_token(action_match.group(1)) != DAMAGE_FONT_ACTION_TYPE:
        return None
    info_match = _DAMAGE_FONT_INFO_RE.search(str(text or ""))
    if info_match is None:
        raise ValueError("damage-font item has no [damage font info] item=%d" % int(item_id))
    info = info_match.group(1)
    font_id = _field_integer(info, "index")
    if font_id is None:
        raise ValueError("incomplete damage-font info item=%d" % int(item_id))
    return DamageFontRule(
        item_id=int(item_id),
        font_id=font_id,
        expiration_mode=EXPIRATION_DATE,
        expiration_value=0,
        source_path=source_path,
    )


@dataclass(frozen=True)
class DamageFontIndex:
    rules_by_item_id: dict[int, DamageFontRule]
    resource_font_ids: frozenset[int]
    scanned_stackable_count: int
    missing_stackable_script_count: int = 0
    skipped_missing_resource_item_ids: tuple[int, ...] = ()
    cache_status: str = ""

    def rule_for(self, item_id: int) -> DamageFontRule | None:
        return self.rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    @property
    def item_count(self) -> int:
        return len(self.rules_by_item_id)

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_stackable_count": int(self.scanned_stackable_count),
            "missing_stackable_script_count": int(
                self.missing_stackable_script_count
            ),
            "skipped_missing_resource_item_ids": list(
                self.skipped_missing_resource_item_ids
            ),
            "resource_font_ids": sorted(self.resource_font_ids),
            "rules": [
                rule.to_cache_row()
                for _, rule in sorted(self.rules_by_item_id.items())
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any) -> "DamageFontIndex":
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("rules"), list)
            or not isinstance(payload.get("resource_font_ids"), list)
            or not isinstance(
                payload.get("skipped_missing_resource_item_ids"),
                list,
            )
        ):
            raise ValueError("invalid damage-font cache payload")
        return build_damage_font_index(
            (DamageFontRule.from_cache_row(row) for row in payload["rules"]),
            resource_font_ids=(int(value) for value in payload["resource_font_ids"]),
            scanned_stackable_count=int(payload.get("scanned_stackable_count") or 0),
            missing_stackable_script_count=int(
                payload.get("missing_stackable_script_count") or 0
            ),
            skipped_missing_resource_item_ids=(
                int(value)
                for value in payload["skipped_missing_resource_item_ids"]
            ),
            cache_status="hit",
        )


def build_damage_font_index(
    rules: Iterable[DamageFontRule],
    *,
    resource_font_ids: Iterable[int],
    scanned_stackable_count: int,
    missing_stackable_script_count: int = 0,
    skipped_missing_resource_item_ids: Iterable[int] = (),
    cache_status: str = "",
) -> DamageFontIndex:
    resources = frozenset(int(value) for value in resource_font_ids)
    if 0 not in resources:
        raise ValueError("damagefontskin.lst has no default font id 0")
    if any(value < 0 or value > 0xFFFF for value in resources):
        raise ValueError("damagefontskin.lst contains a font id outside u16")
    skipped_items = {int(value) for value in skipped_missing_resource_item_ids}
    if any(value <= 0 or value > 0xFFFFFFFF for value in skipped_items):
        raise ValueError("invalid skipped damage-font item id")
    by_item_id: dict[int, DamageFontRule] = {}
    for rule in rules:
        if not isinstance(rule, DamageFontRule):
            raise TypeError("damage-font index accepts DamageFontRule only")
        if rule.item_id in by_item_id:
            raise ValueError("duplicate damage-font item id=%d" % rule.item_id)
        if rule.font_id not in resources:
            skipped_items.add(rule.item_id)
            continue
        skipped_items.discard(rule.item_id)
        by_item_id[rule.item_id] = rule
    scanned = int(scanned_stackable_count)
    missing = int(missing_stackable_script_count)
    if scanned <= 0 or not 0 <= missing <= scanned:
        raise ValueError("invalid damage-font stackable scan counts")
    if not by_item_id:
        raise ValueError("PVF contains no [add damage font skin] items")
    return DamageFontIndex(
        rules_by_item_id=dict(sorted(by_item_id.items())),
        resource_font_ids=resources,
        scanned_stackable_count=scanned,
        missing_stackable_script_count=missing,
        skipped_missing_resource_item_ids=tuple(sorted(skipped_items)),
        cache_status=str(cache_status or ""),
    )


def build_damage_font_index_from_reader(reader: Any) -> DamageFontIndex:
    stackable_paths = _parse_list_paths(
        reader.read_text(STACKABLE_LIST_PATH),
        prefix="stackable",
    )
    font_paths = _parse_list_paths(
        reader.read_text(DAMAGE_FONT_LIST_PATH),
        prefix="event/damagefontskin",
    )
    rules: list[DamageFontRule] = []
    missing = 0
    for item_id, source_path in stackable_paths:
        try:
            text = reader.read_text(source_path)
        except KeyError:
            missing += 1
            continue
        rule = parse_damage_font_rule(item_id, text, source_path=source_path)
        if rule is not None:
            rules.append(rule)
    return build_damage_font_index(
        rules,
        resource_font_ids=(font_id for font_id, _path in font_paths),
        scanned_stackable_count=len(stackable_paths),
        missing_stackable_script_count=missing,
    )


def install_damage_font_index(index: DamageFontIndex) -> None:
    if not isinstance(index, DamageFontIndex) or index.item_count <= 0:
        raise ValueError("invalid damage-font index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_damage_font_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_damage_font_index() -> DamageFontIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("damage-font index is not warmed")
    return index


def warmup_damage_font_index(cache: Any = None) -> dict[str, Any]:
    index: DamageFontIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(DAMAGE_FONT_CACHE_DOMAIN, DAMAGE_FONT_CACHE_VERSION)
        if payload is not None:
            try:
                index = DamageFontIndex.from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(DAMAGE_FONT_CACHE_DOMAIN)
    if index is None:
        index = build_damage_font_index_from_reader(
            pvf_runtime.ensure_shared_reader()
        )
        if cache is not None:
            cache.write(
                DAMAGE_FONT_CACHE_DOMAIN,
                DAMAGE_FONT_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=index.item_count,
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(cache.last_status.get(DAMAGE_FONT_CACHE_DOMAIN, "miss"))
            )
    index = replace(index, cache_status=cache_status)
    install_damage_font_index(index)
    return {
        "ok": 1,
        "damage_font_item_count": index.item_count,
        "font_resource_count": len(index.resource_font_ids),
        "scanned_stackable_count": index.scanned_stackable_count,
        "missing_stackable_script_count": index.missing_stackable_script_count,
        "skipped_missing_resource_item_count": len(
            index.skipped_missing_resource_item_ids
        ),
        "skipped_missing_resource_item_ids": list(
            index.skipped_missing_resource_item_ids
        ),
        "cache": cache_status,
    }


__all__ = [
    "DAMAGE_FONT_ACTION_TYPE",
    "DAMAGE_FONT_CACHE_DOMAIN",
    "DAMAGE_FONT_CACHE_VERSION",
    "DAMAGE_FONT_LIST_PATH",
    "DamageFontIndex",
    "DamageFontRule",
    "EXPIRATION_DATE",
    "EXPIRATION_PERIOD",
    "STACKABLE_LIST_PATH",
    "build_damage_font_index",
    "build_damage_font_index_from_reader",
    "clear_damage_font_index",
    "get_damage_font_index",
    "install_damage_font_index",
    "parse_damage_font_rule",
    "warmup_damage_font_index",
]
