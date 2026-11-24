"""PVF-derived guild-medal and guardian-gem compound pools."""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from game.character.item_metadata import (
    ItemMetadata,
    SLOT_RANGE_GUILD_MEDAL,
    SLOT_RANGE_GUARDIAN_GEM,
    resolve_item_metadata,
)
from pvf import pvf_runtime


EQUIPMENT_LIST_PATH = "equipment/equipment.lst"
STACKABLE_LIST_PATH = "stackable/stackable.lst"
GUILD_ITEM_COMPOUND_CACHE_DOMAIN = "guild_item_compound"
GUILD_ITEM_COMPOUND_CACHE_VERSION = 2

KIND_GUILD_MEDAL = "guild_medal"
KIND_GUARDIAN_GEM = "guardian_gem"
GUILD_ITEM_KINDS = (KIND_GUILD_MEDAL, KIND_GUARDIAN_GEM)

_HIGHER_RARITY_PERCENT = {
    KIND_GUILD_MEDAL: {2: 0, 3: 10, 4: 30},
    KIND_GUARDIAN_GEM: {2: 10, 3: 30, 4: 50},
}
_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_SECTION_RE_TEMPLATE = r"\[{name}\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)"
_LOCK = threading.RLock()
_INDEX: "GuildItemCompoundIndex | None" = None


def _normalize_type(value: object) -> str:
    text = str(value or "").strip().replace("`", "").lower()
    return " ".join(text.split())


def _section_first_token(text: str, name: str) -> str:
    match = re.search(
        _SECTION_RE_TEMPLATE.format(name=re.escape(name)),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return ""
    body = match.group(1)
    tick = re.search(r"`([^`]*)`", body)
    if tick is not None:
        return _normalize_type(tick.group(1))
    for line in body.splitlines():
        token = line.strip()
        if token:
            return _normalize_type(token.split()[0])
    return ""


def _parse_list_paths(
    text: str,
    *,
    prefix: str,
) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    normalized_prefix = str(prefix or "").strip().replace("\\", "/").rstrip("/")
    if not normalized_prefix:
        raise ValueError("PVF list prefix is empty")
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        if item_id in seen_ids:
            raise ValueError(
                "duplicate %s item id=%d" % (normalized_prefix, item_id)
            )
        seen_ids.add(item_id)
        source_path = (
            relative
            if relative.lower().startswith(normalized_prefix.lower() + "/")
            else normalized_prefix + "/" + relative
        )
        entries.append((item_id, source_path))
    if not entries:
        raise ValueError("%s list has no item paths" % normalized_prefix)
    return tuple(entries)


@dataclass(frozen=True)
class GuildItemCompoundEntry:
    item_id: int
    kind: str
    rarity: int
    metadata: ItemMetadata

    def to_cache_row(self) -> list[Any]:
        type_text = (
            self.metadata.equipment_type
            if self.kind == KIND_GUILD_MEDAL
            else self.metadata.stackable_type
        )
        return [
            int(self.item_id),
            str(self.kind),
            int(self.rarity),
            int(self.metadata.stack_limit),
            int(self.metadata.durability),
            int(self.metadata.usable_period_days),
            int(self.metadata.expiration_timestamp),
            str(self.metadata.source_path),
            str(type_text),
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "GuildItemCompoundEntry":
        if not isinstance(row, list) or len(row) != 9:
            raise ValueError("invalid guild item compound cache row")
        item_id = int(row[0])
        kind = str(row[1] or "")
        rarity = int(row[2])
        common = {
            "item_id": item_id,
            "stack_limit": int(row[3]),
            "durability": int(row[4]),
            "usable_period_days": int(row[5]),
            "expiration_timestamp": int(row[6]),
            "source_path": str(row[7] or ""),
            "rarity": rarity,
            "note": "pvf_index_cache",
        }
        if kind == KIND_GUILD_MEDAL:
            metadata = ItemMetadata(
                item_kind="equipment",
                equipment_type=str(row[8] or ""),
                **common,
            )
        elif kind == KIND_GUARDIAN_GEM:
            metadata = ItemMetadata(
                item_kind="stackable",
                stackable_type=str(row[8] or ""),
                **common,
            )
        else:
            raise ValueError("invalid cached guild item kind=%r" % kind)
        return _entry_from_metadata(
            metadata,
            expected_item_id=item_id,
            expected_kind=kind,
            expected_rarity=rarity,
        )


def _entry_from_metadata(
    metadata: ItemMetadata,
    *,
    expected_item_id: int | None = None,
    expected_kind: str | None = None,
    expected_rarity: int | None = None,
) -> GuildItemCompoundEntry:
    item_id = int(metadata.item_id)
    rarity = int(metadata.rarity)
    if item_id <= 0 or item_id > 0xFFFFFFFF:
        raise ValueError("invalid guild item id=%d" % item_id)
    if expected_item_id is not None and item_id != int(expected_item_id):
        raise ValueError(
            "guild item metadata mismatch expected=%d actual=%d"
            % (int(expected_item_id), item_id)
        )
    if metadata.is_guild_medal:
        kind = KIND_GUILD_MEDAL
        expected_range = SLOT_RANGE_GUILD_MEDAL
    elif metadata.is_guardian_gem:
        kind = KIND_GUARDIAN_GEM
        expected_range = SLOT_RANGE_GUARDIAN_GEM
    else:
        raise ValueError("item %d is not a guild medal or guardian gem" % item_id)
    if expected_kind is not None and kind != str(expected_kind):
        raise ValueError(
            "guild item kind changed item=%d expected=%s actual=%s"
            % (item_id, expected_kind, kind)
        )
    if rarity < 0:
        raise ValueError("guild item has negative rarity item=%d" % item_id)
    if expected_rarity is not None and rarity != int(expected_rarity):
        raise ValueError(
            "guild item rarity changed item=%d expected=%d actual=%d"
            % (item_id, int(expected_rarity), rarity)
        )
    if tuple(map(int, metadata.slot_range())) != tuple(map(int, expected_range)):
        raise ValueError("guild item has invalid slot range item=%d" % item_id)
    if int(metadata.stack_limit) < 0:
        raise ValueError("guild item has negative stack limit item=%d" % item_id)
    return GuildItemCompoundEntry(
        item_id=item_id,
        kind=kind,
        rarity=rarity,
        metadata=metadata,
    )


@dataclass(frozen=True)
class GuildItemCompoundIndex:
    pools_by_kind: dict[str, dict[int, tuple[GuildItemCompoundEntry, ...]]]
    entries_by_item_id: dict[int, GuildItemCompoundEntry]
    scanned_equipment_count: int
    scanned_stackable_count: int
    missing_equipment_script_count: int = 0
    missing_stackable_script_count: int = 0
    cache_status: str = ""

    def entry_for(self, item_id: int) -> GuildItemCompoundEntry | None:
        return self.entries_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    def pool(
        self,
        kind: str,
        rarity: int,
    ) -> tuple[GuildItemCompoundEntry, ...]:
        return tuple(
            self.pools_by_kind.get(str(kind), {}).get(int(rarity), ())
        )

    def require_pool(
        self,
        kind: str,
        rarity: int,
    ) -> tuple[GuildItemCompoundEntry, ...]:
        pool = self.pool(kind, rarity)
        if not pool:
            raise RuntimeError(
                "guild item compound pool is empty kind=%s rarity=%d"
                % (kind, int(rarity))
            )
        return pool

    def kind_count(self, kind: str) -> int:
        return sum(
            len(pool)
            for pool in self.pools_by_kind.get(str(kind), {}).values()
        )

    def rarity_count(self, kind: str) -> int:
        return len(self.pools_by_kind.get(str(kind), {}))

    @property
    def item_count(self) -> int:
        return len(self.entries_by_item_id)

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_equipment_count": int(self.scanned_equipment_count),
            "scanned_stackable_count": int(self.scanned_stackable_count),
            "missing_equipment_script_count": int(
                self.missing_equipment_script_count
            ),
            "missing_stackable_script_count": int(
                self.missing_stackable_script_count
            ),
            "entries": [
                entry.to_cache_row()
                for _, entry in sorted(self.entries_by_item_id.items())
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any) -> "GuildItemCompoundIndex":
        if not isinstance(payload, dict) or not isinstance(
            payload.get("entries"), list
        ):
            raise ValueError("invalid guild item compound cache payload")
        scanned_equipment = int(payload.get("scanned_equipment_count") or 0)
        scanned_stackable = int(payload.get("scanned_stackable_count") or 0)
        missing_equipment = int(
            payload.get("missing_equipment_script_count") or 0
        )
        missing_stackable = int(
            payload.get("missing_stackable_script_count") or 0
        )
        if scanned_equipment <= 0 or scanned_stackable <= 0:
            raise ValueError("invalid guild item compound cached scan counts")
        if not 0 <= missing_equipment <= scanned_equipment:
            raise ValueError("invalid cached missing equipment-script count")
        if not 0 <= missing_stackable <= scanned_stackable:
            raise ValueError("invalid cached missing stackable-script count")
        return build_guild_item_compound_index(
            (
                GuildItemCompoundEntry.from_cache_row(row)
                for row in payload["entries"]
            ),
            scanned_equipment_count=scanned_equipment,
            scanned_stackable_count=scanned_stackable,
            missing_equipment_script_count=missing_equipment,
            missing_stackable_script_count=missing_stackable,
            cache_status="hit",
        )


def build_guild_item_compound_index(
    records: Iterable[ItemMetadata | GuildItemCompoundEntry],
    *,
    scanned_equipment_count: int = 0,
    scanned_stackable_count: int = 0,
    missing_equipment_script_count: int = 0,
    missing_stackable_script_count: int = 0,
    cache_status: str = "",
) -> GuildItemCompoundIndex:
    pools: dict[str, dict[int, list[GuildItemCompoundEntry]]] = {
        KIND_GUILD_MEDAL: {},
        KIND_GUARDIAN_GEM: {},
    }
    entries_by_id: dict[int, GuildItemCompoundEntry] = {}
    for record in records:
        if isinstance(record, GuildItemCompoundEntry):
            entry = _entry_from_metadata(
                record.metadata,
                expected_item_id=record.item_id,
                expected_kind=record.kind,
                expected_rarity=record.rarity,
            )
        else:
            if not record.is_guild_medal_inventory_item:
                continue
            entry = _entry_from_metadata(record)
        if entry.item_id in entries_by_id:
            raise ValueError("duplicate guild item id=%d" % entry.item_id)
        entries_by_id[entry.item_id] = entry
        pools[entry.kind].setdefault(entry.rarity, []).append(entry)
    normalized = {
        kind: {
            rarity: tuple(sorted(values, key=lambda entry: entry.item_id))
            for rarity, values in rarity_pools.items()
        }
        for kind, rarity_pools in pools.items()
    }
    missing_equipment = max(0, int(missing_equipment_script_count))
    missing_stackable = max(0, int(missing_stackable_script_count))
    if missing_equipment > max(0, int(scanned_equipment_count)):
        raise ValueError("guild missing equipment-script count exceeds scan count")
    if missing_stackable > max(0, int(scanned_stackable_count)):
        raise ValueError("guild missing stackable-script count exceeds scan count")
    return GuildItemCompoundIndex(
        pools_by_kind=normalized,
        entries_by_item_id=entries_by_id,
        scanned_equipment_count=max(0, int(scanned_equipment_count)),
        scanned_stackable_count=max(0, int(scanned_stackable_count)),
        missing_equipment_script_count=missing_equipment,
        missing_stackable_script_count=missing_stackable,
        cache_status=str(cache_status or ""),
    )


def build_guild_item_compound_index_from_reader(
    reader: Any,
    *,
    metadata_resolver: Callable[[int], ItemMetadata] | None = None,
) -> GuildItemCompoundIndex:
    resolver = metadata_resolver or resolve_item_metadata
    equipment_paths = _parse_list_paths(
        reader.read_text(EQUIPMENT_LIST_PATH),
        prefix="equipment",
    )
    stackable_paths = _parse_list_paths(
        reader.read_text(STACKABLE_LIST_PATH),
        prefix="stackable",
    )
    entries: list[GuildItemCompoundEntry] = []
    missing_equipment_script_count = 0
    for item_id, source_path in equipment_paths:
        try:
            script = reader.read_text(source_path)
        except KeyError:
            missing_equipment_script_count += 1
            continue
        if _section_first_token(script, "equipment type") != "[flag]":
            continue
        entries.append(
            _entry_from_metadata(
                resolver(item_id),
                expected_item_id=item_id,
                expected_kind=KIND_GUILD_MEDAL,
            )
        )
    missing_stackable_script_count = 0
    for item_id, source_path in stackable_paths:
        try:
            script = reader.read_text(source_path)
        except KeyError:
            missing_stackable_script_count += 1
            continue
        stackable_type = _section_first_token(script, "stackable type")
        if not (
            stackable_type == "[flag gem]"
            or stackable_type.startswith("[flag gem] ")
        ):
            continue
        entries.append(
            _entry_from_metadata(
                resolver(item_id),
                expected_item_id=item_id,
                expected_kind=KIND_GUARDIAN_GEM,
            )
        )
    return build_guild_item_compound_index(
        entries,
        scanned_equipment_count=len(equipment_paths),
        scanned_stackable_count=len(stackable_paths),
        missing_equipment_script_count=missing_equipment_script_count,
        missing_stackable_script_count=missing_stackable_script_count,
    )


def install_guild_item_compound_index(index: GuildItemCompoundIndex) -> None:
    if not isinstance(index, GuildItemCompoundIndex):
        raise ValueError("invalid guild item compound index")
    for kind in GUILD_ITEM_KINDS:
        if index.kind_count(kind) <= 0:
            raise ValueError("guild item compound index has no %s entries" % kind)
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_guild_item_compound_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_guild_item_compound_index() -> GuildItemCompoundIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("guild item compound index is not warmed")
    return index


def warmup_guild_item_compound_index(cache: Any = None) -> dict[str, Any]:
    index: GuildItemCompoundIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(
            GUILD_ITEM_COMPOUND_CACHE_DOMAIN,
            GUILD_ITEM_COMPOUND_CACHE_VERSION,
        )
        if payload is not None:
            try:
                index = GuildItemCompoundIndex.from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(GUILD_ITEM_COMPOUND_CACHE_DOMAIN)
    if index is None:
        reader = pvf_runtime.ensure_shared_reader()
        index = build_guild_item_compound_index_from_reader(reader)
        if cache is not None:
            cache.write(
                GUILD_ITEM_COMPOUND_CACHE_DOMAIN,
                GUILD_ITEM_COMPOUND_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=index.item_count,
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(
                    cache.last_status.get(
                        GUILD_ITEM_COMPOUND_CACHE_DOMAIN,
                        "miss",
                    )
                )
            )
    index = replace(index, cache_status=cache_status)
    install_guild_item_compound_index(index)
    result: dict[str, Any] = {
        "ok": 1,
        "scanned_equipment_count": int(index.scanned_equipment_count),
        "scanned_stackable_count": int(index.scanned_stackable_count),
        "missing_equipment_script_count": int(
            index.missing_equipment_script_count
        ),
        "missing_stackable_script_count": int(
            index.missing_stackable_script_count
        ),
        "guild_item_count": int(index.item_count),
        "medal_count": int(index.kind_count(KIND_GUILD_MEDAL)),
        "guardian_gem_count": int(index.kind_count(KIND_GUARDIAN_GEM)),
        "medal_rarity_count": int(index.rarity_count(KIND_GUILD_MEDAL)),
        "guardian_gem_rarity_count": int(
            index.rarity_count(KIND_GUARDIAN_GEM)
        ),
        "cache": cache_status,
    }
    for kind in GUILD_ITEM_KINDS:
        prefix = "medal" if kind == KIND_GUILD_MEDAL else "guardian_gem"
        for rarity, pool in sorted(index.pools_by_kind[kind].items()):
            result["%s_rarity_%d_count" % (prefix, rarity)] = len(pool)
    return result


def _randbelow(rng: Any, stop: int) -> int:
    limit = int(stop)
    if limit <= 0:
        raise ValueError("random range must be positive")
    if rng is None:
        value = secrets.randbelow(limit)
    else:
        method = getattr(rng, "randrange", None)
        if callable(method):
            value = method(limit)
        elif callable(rng):
            value = rng(limit)
        else:
            raise ValueError("invalid random source")
    result = int(value)
    if not 0 <= result < limit:
        raise ValueError("random source returned value outside range")
    return result


@dataclass(frozen=True)
class GuildItemCompoundRewardPlan:
    kind: str
    source_rarity: int
    result_rarity: int
    material_count: int
    higher_rarity_percent: int
    higher_pool_missing: bool
    entry: GuildItemCompoundEntry

    @property
    def upgraded(self) -> bool:
        return self.result_rarity == self.source_rarity + 1


def roll_guild_item_compound_reward(
    kind: str,
    source_rarity: int,
    material_count: int,
    *,
    index: GuildItemCompoundIndex | None = None,
    rng: Any = None,
) -> GuildItemCompoundRewardPlan:
    normalized_kind = str(kind or "")
    count = int(material_count)
    rarity = int(source_rarity)
    if normalized_kind not in GUILD_ITEM_KINDS:
        raise ValueError("invalid guild item compound kind=%r" % kind)
    if count not in (2, 3, 4):
        raise ValueError("invalid guild item compound material count=%d" % count)
    if rarity < 0:
        raise ValueError("invalid guild item compound source rarity=%d" % rarity)
    compound_index = index if index is not None else get_guild_item_compound_index()
    same_pool = compound_index.require_pool(normalized_kind, rarity)
    higher_pool = compound_index.pool(normalized_kind, rarity + 1)
    higher_percent = int(_HIGHER_RARITY_PERCENT[normalized_kind][count])
    selected_pool = same_pool
    result_rarity = rarity
    if higher_pool and higher_percent > 0:
        if _randbelow(rng, 100) < higher_percent:
            selected_pool = higher_pool
            result_rarity = rarity + 1
    entry = selected_pool[_randbelow(rng, len(selected_pool))]
    return GuildItemCompoundRewardPlan(
        kind=normalized_kind,
        source_rarity=rarity,
        result_rarity=result_rarity,
        material_count=count,
        higher_rarity_percent=higher_percent,
        higher_pool_missing=not bool(higher_pool),
        entry=entry,
    )


__all__ = [
    "EQUIPMENT_LIST_PATH",
    "GUILD_ITEM_COMPOUND_CACHE_DOMAIN",
    "GUILD_ITEM_COMPOUND_CACHE_VERSION",
    "GUILD_ITEM_KINDS",
    "GuildItemCompoundEntry",
    "GuildItemCompoundIndex",
    "GuildItemCompoundRewardPlan",
    "KIND_GUARDIAN_GEM",
    "KIND_GUILD_MEDAL",
    "STACKABLE_LIST_PATH",
    "build_guild_item_compound_index",
    "build_guild_item_compound_index_from_reader",
    "clear_guild_item_compound_index",
    "get_guild_item_compound_index",
    "install_guild_item_compound_index",
    "roll_guild_item_compound_reward",
    "warmup_guild_item_compound_index",
]
