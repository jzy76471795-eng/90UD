"""PVF-derived emblem pools and reward rules for avatar recasting."""
from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from game.character.item_metadata import (
    ItemMetadata,
    SLOT_RANGE_AVATAR_EMBLEM,
    resolve_item_metadata,
)
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
AVATAR_EMBLEM_STACKABLE_TYPE = "[avatar emblem]"
AVATAR_RECAST_CACHE_DOMAIN = "avatar_recast_emblems"
AVATAR_RECAST_CACHE_VERSION = 2
REQUIRED_REWARD_RARITIES = (1, 2, 3)

TARGET_KIND_ORDINARY = "ordinary"
TARGET_KIND_CLONE = "clone"
TARGET_KIND_RARE_CLONE = "rare_clone"
TARGET_KIND_ARTIFACT = "artifact"

_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_SECTION_RE = re.compile(
    r"\[stackable\s+type\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_LOCK = threading.RLock()
_INDEX: "AvatarEmblemIndex | None" = None


def _normalize_type(value: object) -> str:
    text = str(value or "").strip().replace("`", "").lower()
    return " ".join(text.split())


def _is_avatar_emblem_type(value: object) -> bool:
    normalized = _normalize_type(value)
    return normalized == AVATAR_EMBLEM_STACKABLE_TYPE or normalized.startswith(
        AVATAR_EMBLEM_STACKABLE_TYPE + " "
    )


def _stackable_base_type(text: str) -> str:
    match = _SECTION_RE.search(str(text or ""))
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


def _parse_stackable_paths(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        if item_id in seen:
            raise ValueError("duplicate stackable item id=%d" % item_id)
        seen.add(item_id)
        source_path = (
            relative
            if relative.lower().startswith("stackable/")
            else "stackable/" + relative
        )
        entries.append((item_id, source_path))
    if not entries:
        raise ValueError("stackable.lst has no item paths")
    return tuple(entries)


@dataclass(frozen=True)
class AvatarEmblemEntry:
    item_id: int
    rarity: int
    metadata: ItemMetadata

    def to_cache_row(self) -> list[Any]:
        return [
            int(self.item_id),
            int(self.rarity),
            int(self.metadata.grade),
            str(self.metadata.stackable_type),
            int(self.metadata.stack_limit),
            str(self.metadata.source_path),
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "AvatarEmblemEntry":
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("invalid avatar emblem cache row")
        item_id = int(row[0])
        rarity = int(row[1])
        grade = int(row[2])
        metadata = ItemMetadata(
            item_id=item_id,
            item_kind="stackable",
            stackable_type=str(row[3] or ""),
            stack_limit=int(row[4]),
            source_path=str(row[5] or ""),
            rarity=rarity,
            grade=grade,
            note="pvf_index_cache",
        )
        return _entry_from_metadata(metadata, expected_rarity=rarity)


def _entry_from_metadata(
    metadata: ItemMetadata,
    *,
    expected_item_id: int | None = None,
    expected_rarity: int | None = None,
) -> AvatarEmblemEntry:
    item_id = int(metadata.item_id)
    rarity = int(metadata.rarity)
    if item_id <= 0 or item_id > 0xFFFFFFFF:
        raise ValueError("invalid avatar emblem item id=%d" % item_id)
    if expected_item_id is not None and item_id != int(expected_item_id):
        raise ValueError(
            "avatar emblem metadata mismatch expected=%d actual=%d"
            % (int(expected_item_id), item_id)
        )
    if not metadata.is_stackable or not _is_avatar_emblem_type(
        metadata.stackable_type
    ):
        raise ValueError("item %d is not an avatar emblem" % item_id)
    if rarity < 0:
        raise ValueError(
            "avatar emblem item=%d has negative rarity=%d" % (item_id, rarity)
        )
    if int(metadata.grade) < 0:
        raise ValueError(
            "avatar emblem item=%d has negative grade=%d"
            % (item_id, int(metadata.grade))
        )
    if expected_rarity is not None and rarity != int(expected_rarity):
        raise ValueError(
            "avatar emblem item=%d rarity changed expected=%d actual=%d"
            % (item_id, int(expected_rarity), rarity)
        )
    if tuple(map(int, metadata.slot_range())) != SLOT_RANGE_AVATAR_EMBLEM:
        raise ValueError("avatar emblem item=%d has invalid slot range" % item_id)
    # A limit of one is valid: each emblem occupies its own physical slot.
    return AvatarEmblemEntry(item_id=item_id, rarity=rarity, metadata=metadata)


@dataclass(frozen=True)
class AvatarEmblemIndex:
    pools_by_rarity: dict[int, tuple[AvatarEmblemEntry, ...]]
    scanned_stackable_count: int
    cache_status: str = ""

    def pool(self, rarity: int) -> tuple[AvatarEmblemEntry, ...]:
        return tuple(self.pools_by_rarity.get(int(rarity), ()))

    def require_pool(self, rarity: int) -> tuple[AvatarEmblemEntry, ...]:
        pool = self.pool(rarity)
        if not pool:
            raise RuntimeError("avatar emblem rarity pool is empty: %d" % int(rarity))
        return pool

    @property
    def emblem_count(self) -> int:
        return sum(len(pool) for pool in self.pools_by_rarity.values())

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_stackable_count": int(self.scanned_stackable_count),
            "pools": [
                [int(rarity), [entry.to_cache_row() for entry in pool]]
                for rarity, pool in sorted(self.pools_by_rarity.items())
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any) -> "AvatarEmblemIndex":
        if not isinstance(payload, dict) or not isinstance(payload.get("pools"), list):
            raise ValueError("invalid avatar emblem cache payload")
        scanned = int(payload.get("scanned_stackable_count") or 0)
        if scanned <= 0:
            raise ValueError("invalid cached stackable count")
        entries: list[AvatarEmblemEntry] = []
        seen_rarities: set[int] = set()
        for raw_pool in payload["pools"]:
            if not isinstance(raw_pool, list) or len(raw_pool) != 2:
                raise ValueError("invalid cached rarity pool")
            rarity = int(raw_pool[0])
            if (
                rarity < 0
                or rarity in seen_rarities
                or not isinstance(raw_pool[1], list)
            ):
                raise ValueError("invalid cached rarity=%d" % rarity)
            seen_rarities.add(rarity)
            for row in raw_pool[1]:
                entry = AvatarEmblemEntry.from_cache_row(row)
                if entry.rarity != rarity:
                    raise ValueError("cached avatar emblem rarity mismatch")
                entries.append(entry)
        return build_avatar_emblem_index(
            entries,
            scanned_stackable_count=scanned,
            cache_status="hit",
        )


def build_avatar_emblem_index(
    records: Iterable[ItemMetadata | AvatarEmblemEntry],
    *,
    scanned_stackable_count: int = 0,
    cache_status: str = "",
) -> AvatarEmblemIndex:
    pools: dict[int, list[AvatarEmblemEntry]] = {}
    seen_ids: set[int] = set()
    for record in records:
        if isinstance(record, AvatarEmblemEntry):
            entry = _entry_from_metadata(
                record.metadata,
                expected_item_id=record.item_id,
                expected_rarity=record.rarity,
            )
        else:
            if not record.is_stackable or not _is_avatar_emblem_type(
                record.stackable_type
            ):
                continue
            entry = _entry_from_metadata(record)
        if entry.item_id in seen_ids:
            raise ValueError("duplicate avatar emblem item id=%d" % entry.item_id)
        seen_ids.add(entry.item_id)
        pools.setdefault(entry.rarity, []).append(entry)
    normalized = {
        rarity: tuple(sorted(values, key=lambda entry: entry.item_id))
        for rarity, values in pools.items()
    }
    return AvatarEmblemIndex(
        pools_by_rarity=normalized,
        scanned_stackable_count=max(0, int(scanned_stackable_count)),
        cache_status=str(cache_status or ""),
    )


def build_avatar_emblem_index_from_reader(
    reader: Any,
    *,
    metadata_resolver: Callable[[int], ItemMetadata] | None = None,
) -> AvatarEmblemIndex:
    resolver = metadata_resolver or resolve_item_metadata
    paths = _parse_stackable_paths(reader.read_text(STACKABLE_LIST_PATH))
    entries: list[AvatarEmblemEntry] = []
    for item_id, source_path in paths:
        text = reader.read_text(source_path)
        if _stackable_base_type(text) != AVATAR_EMBLEM_STACKABLE_TYPE:
            continue
        metadata = resolver(item_id)
        entries.append(
            _entry_from_metadata(metadata, expected_item_id=item_id)
        )
    index = build_avatar_emblem_index(
        entries,
        scanned_stackable_count=len(paths),
    )
    if index.emblem_count <= 0:
        raise RuntimeError("PVF contains no avatar emblems")
    return index


def install_avatar_emblem_index(index: AvatarEmblemIndex) -> None:
    if not isinstance(index, AvatarEmblemIndex) or index.emblem_count <= 0:
        raise ValueError("invalid avatar emblem index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_avatar_emblem_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_avatar_emblem_index() -> AvatarEmblemIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("avatar emblem index is not warmed")
    return index


def warmup_avatar_emblem_index(cache: Any = None) -> dict[str, Any]:
    index: AvatarEmblemIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(
            AVATAR_RECAST_CACHE_DOMAIN,
            AVATAR_RECAST_CACHE_VERSION,
        )
        if payload is not None:
            try:
                index = AvatarEmblemIndex.from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(AVATAR_RECAST_CACHE_DOMAIN)
    if index is None:
        reader = pvf_runtime.ensure_shared_reader()
        index = build_avatar_emblem_index_from_reader(reader)
        if cache is not None:
            cache.write(
                AVATAR_RECAST_CACHE_DOMAIN,
                AVATAR_RECAST_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=index.emblem_count,
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(cache.last_status.get(AVATAR_RECAST_CACHE_DOMAIN, "miss"))
            )
    index = replace(index, cache_status=cache_status)
    for rarity in REQUIRED_REWARD_RARITIES:
        index.require_pool(rarity)
    install_avatar_emblem_index(index)
    result: dict[str, Any] = {
        "ok": 1,
        "scanned_stackable_count": int(index.scanned_stackable_count),
        "emblem_count": int(index.emblem_count),
        "rarity_count": len(index.pools_by_rarity),
        "cache": cache_status,
    }
    for rarity, pool in sorted(index.pools_by_rarity.items()):
        result["rarity_%d_count" % rarity] = len(pool)
    return result


def classify_avatar_recast_target(metadata: ItemMetadata) -> str:
    if not metadata.is_avatar:
        raise ValueError("avatar recast target is not an avatar")
    rarity = int(metadata.rarity)
    if rarity == 3:
        return TARGET_KIND_ARTIFACT
    if metadata.is_clone_avatar and rarity == 2:
        return TARGET_KIND_RARE_CLONE
    if metadata.is_clone_avatar:
        return TARGET_KIND_CLONE
    return TARGET_KIND_ORDINARY


def roll_avatar_recast_rarities(
    target_kind: str,
    *,
    rng: Any = None,
) -> tuple[int, ...]:
    source = rng if rng is not None else random
    kind = str(target_kind or "")
    if kind == TARGET_KIND_ORDINARY:
        return tuple(
            2 if int(source.randrange(100)) < 33 else 1
            for _index in range(3)
        )
    if kind == TARGET_KIND_CLONE:
        return tuple(
            3 if int(source.randrange(100)) < 33 else 2
            for _index in range(4)
        )
    if kind == TARGET_KIND_RARE_CLONE:
        return (3, 3, 2, 2, 1, 1)
    if kind == TARGET_KIND_ARTIFACT:
        return (3,) * 8
    raise ValueError("unknown avatar recast target kind=%r" % target_kind)


@dataclass(frozen=True)
class AvatarRecastRewardPlan:
    target_kind: str
    reward_rarities: tuple[int, ...]
    entries: tuple[AvatarEmblemEntry, ...]


def plan_avatar_recast_rewards(
    target_metadata: ItemMetadata,
    *,
    index: AvatarEmblemIndex | None = None,
    rng: Any = None,
) -> AvatarRecastRewardPlan:
    target_kind = classify_avatar_recast_target(target_metadata)
    reward_rarities = roll_avatar_recast_rarities(target_kind, rng=rng)
    emblem_index = index if index is not None else get_avatar_emblem_index()
    source = rng if rng is not None else random
    entries = tuple(
        source.choice(emblem_index.require_pool(rarity))
        for rarity in reward_rarities
    )
    return AvatarRecastRewardPlan(
        target_kind=target_kind,
        reward_rarities=reward_rarities,
        entries=entries,
    )


__all__ = [
    "AVATAR_EMBLEM_STACKABLE_TYPE",
    "AVATAR_RECAST_CACHE_DOMAIN",
    "AVATAR_RECAST_CACHE_VERSION",
    "AvatarEmblemEntry",
    "AvatarEmblemIndex",
    "AvatarRecastRewardPlan",
    "REQUIRED_REWARD_RARITIES",
    "STACKABLE_LIST_PATH",
    "TARGET_KIND_ARTIFACT",
    "TARGET_KIND_CLONE",
    "TARGET_KIND_ORDINARY",
    "TARGET_KIND_RARE_CLONE",
    "build_avatar_emblem_index",
    "build_avatar_emblem_index_from_reader",
    "classify_avatar_recast_target",
    "clear_avatar_emblem_index",
    "get_avatar_emblem_index",
    "install_avatar_emblem_index",
    "plan_avatar_recast_rewards",
    "roll_avatar_recast_rarities",
    "warmup_avatar_emblem_index",
]
