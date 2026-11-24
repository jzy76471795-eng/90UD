# -*- coding: utf-8 -*-
"""PVF-backed Neo Premium Plus rental catalog and daily reset rules."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from game.character import item_metadata
from game.character.item_metadata import CHARACTER_JOB_LABELS, job_label_for_id
from game.shop.premium_contract_index import get_premium_contract_index


NEO_PREMIUM_PLUS_TYPE = 118
# Legacy current-PVF value retained for callers/tests that need to name the
# historical single reward. C1/04B6 resolves the complete type-118 [items]
# list through ``neo_premium_daily_reward_items`` instead of using this ID.
NEO_PREMIUM_DAILY_GIFT_ITEM_ID = 50002547
NEO_PREMIUM_RENTAL_SERVICE = 3
NEO_PREMIUM_WEAPON_GROUP = 0
NEO_PREMIUM_RESET_UTC_OFFSET = 3600  # 09:00 China time == 01:00 UTC.
RENTAL_CACHE_DOMAIN = "neo_premium_rental"
RENTAL_CACHE_VERSION = 1

# C1/04B5 carries the menu ordinal, not the numeric PVF weapon subtype.
# event/rentalservice.evt lists sword jobs as short sword, great sword, blunt,
# katana, light saber. Keep fixed slots even if a template is unavailable.
_SWORD_RENTAL_MENU_SUBTYPES = (0, 3, 2, 1, 5)
_SWORD_RENTAL_JOBS = frozenset(("swordman", "demonic swordman", "at swordman"))


@dataclass(frozen=True)
class NeoPremiumRentalWeapon:
    item_id: int
    rental_service: int
    subtype: int
    minimum_level: int
    usable_jobs: tuple[str, ...]
    source_path: str


class NeoPremiumRentalIndex:
    def __init__(self, weapons: Iterable[NeoPremiumRentalWeapon]) -> None:
        normalized: list[NeoPremiumRentalWeapon] = []
        seen_ids: set[int] = set()
        allowed_jobs = set(CHARACTER_JOB_LABELS)
        for weapon in weapons:
            if not isinstance(weapon, NeoPremiumRentalWeapon):
                raise ValueError("invalid Neo Premium rental record")
            item_id = int(weapon.item_id)
            service = int(weapon.rental_service)
            subtype = int(weapon.subtype)
            minimum_level = int(weapon.minimum_level)
            jobs = tuple(str(value).strip().lower() for value in weapon.usable_jobs)
            source_path = str(weapon.source_path or "").replace("\\", "/")
            if item_id <= 0 or item_id > 0xFFFFFFFF or item_id in seen_ids:
                raise ValueError("duplicate or invalid rental item=%d" % item_id)
            if service != NEO_PREMIUM_RENTAL_SERVICE:
                raise ValueError("unsupported rental service item=%d service=%d" % (item_id, service))
            if subtype < 0 or subtype > 0xFF or minimum_level < 0 or minimum_level > 0xFF:
                raise ValueError("invalid rental level/subtype item=%d" % item_id)
            if not jobs or any(job not in allowed_jobs for job in jobs):
                raise ValueError("invalid rental usable jobs item=%d jobs=%r" % (item_id, jobs))
            if "/weapon/" not in source_path.lower() or not source_path.lower().endswith(".equ"):
                raise ValueError("invalid rental weapon path item=%d path=%s" % (item_id, source_path))
            seen_ids.add(item_id)
            normalized.append(
                NeoPremiumRentalWeapon(
                    item_id=item_id,
                    rental_service=service,
                    subtype=subtype,
                    minimum_level=minimum_level,
                    usable_jobs=jobs,
                    source_path=source_path,
                )
            )
        if not normalized:
            raise ValueError("empty Neo Premium rental catalog")
        self.weapons = tuple(sorted(normalized, key=lambda row: row.item_id))
        self._by_job: dict[str, tuple[NeoPremiumRentalWeapon, ...]] = {}
        for job in CHARACTER_JOB_LABELS:
            rows = tuple(weapon for weapon in self.weapons if job in weapon.usable_jobs)
            if rows:
                self._by_job[job] = rows

    def resolve(
        self,
        *,
        job: int,
        effective_level: int,
        group_index: int,
        item_index: int,
    ) -> NeoPremiumRentalWeapon:
        if int(group_index) != NEO_PREMIUM_WEAPON_GROUP:
            raise LookupError("unsupported rental group=%d" % int(group_index))
        job_label = job_label_for_id(int(job))
        rows = self._by_job.get(job_label, ())
        subtypes = (
            _SWORD_RENTAL_MENU_SUBTYPES
            if job_label in _SWORD_RENTAL_JOBS
            else sorted({int(row.subtype) for row in rows})
        )
        selected_index = int(item_index)
        if selected_index < 0 or selected_index >= len(subtypes):
            raise LookupError(
                "rental item index out of range job=%d index=%d count=%d"
                % (int(job), selected_index, len(subtypes))
            )
        selected_subtype = subtypes[selected_index]
        level = max(0, int(effective_level))
        candidates = [
            row
            for row in rows
            if int(row.subtype) == selected_subtype
            and int(row.minimum_level) <= level
        ]
        if not candidates:
            raise LookupError(
                "rental level unavailable job=%d subtype=%d level=%d"
                % (int(job), selected_subtype, level)
            )
        # Some current PVF families retain two templates at the same level.
        # equipment.lst order is item-id ascending, so the later template wins.
        return max(candidates, key=lambda row: (row.minimum_level, row.item_id))

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "weapons": [
                [
                    row.item_id,
                    row.rental_service,
                    row.subtype,
                    row.minimum_level,
                    list(row.usable_jobs),
                    row.source_path,
                ]
                for row in self.weapons
            ]
        }

    @classmethod
    def from_cache_payload(cls, payload: dict[str, Any]) -> "NeoPremiumRentalIndex":
        if not isinstance(payload, dict) or not isinstance(payload.get("weapons"), list):
            raise ValueError("invalid Neo Premium rental cache payload")
        weapons: list[NeoPremiumRentalWeapon] = []
        for raw in payload["weapons"]:
            if not isinstance(raw, list) or len(raw) != 6 or not isinstance(raw[4], list):
                raise ValueError("invalid Neo Premium rental cache row")
            weapons.append(
                NeoPremiumRentalWeapon(
                    item_id=int(raw[0]),
                    rental_service=int(raw[1]),
                    subtype=int(raw[2]),
                    minimum_level=int(raw[3]),
                    usable_jobs=tuple(str(value) for value in raw[4]),
                    source_path=str(raw[5]),
                )
            )
        return cls(weapons)


_INTEGER_FIELD = re.compile(r"(?m)^\s*(-?\d+)\s*$")
_INDEX_LOCK = threading.RLock()
_INDEX: NeoPremiumRentalIndex | None = None


def _read_integer_field(text: str, field: str) -> int:
    match = re.search(
        r"(?is)\[" + re.escape(field) + r"\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)",
        text,
    )
    if match is None:
        return 0
    value = _INTEGER_FIELD.search(match.group(1))
    return int(value.group(1)) if value is not None else 0


def build_neo_premium_rental_index() -> NeoPremiumRentalIndex:
    reader = item_metadata._get_reader()
    equipment = item_metadata._load_equipment_map(reader)
    weapons: list[NeoPremiumRentalWeapon] = []
    missing_scripts = 0
    for item_id, relative in sorted(equipment.items()):
        source_path = item_metadata._relative_script_path("equipment", relative)
        if "/weapon/" not in source_path.lower():
            continue
        try:
            text = reader.read_text(source_path)
        except KeyError:
            missing_scripts += 1
            continue
        rental_service = _read_integer_field(text, "Rental Service")
        if rental_service != NEO_PREMIUM_RENTAL_SERVICE:
            continue
        metadata = item_metadata.resolve_item_metadata(int(item_id))
        if not metadata.is_equipment or str(metadata.equipment_type).lower() != "[weapon]":
            raise ValueError("rental metadata is not a weapon item=%d" % int(item_id))
        if str(metadata.source_path).replace("\\", "/").lower() != source_path.lower():
            raise ValueError("rental metadata path mismatch item=%d" % int(item_id))
        weapons.append(
            NeoPremiumRentalWeapon(
                item_id=int(item_id),
                rental_service=rental_service,
                subtype=_read_integer_field(text, "sub type"),
                minimum_level=int(metadata.minimum_level),
                usable_jobs=tuple(metadata.usable_jobs),
                source_path=source_path,
            )
        )
    index = NeoPremiumRentalIndex(weapons)
    setattr(index, "missing_script_count", missing_scripts)
    return index


def install_neo_premium_rental_index(index: NeoPremiumRentalIndex) -> None:
    if not isinstance(index, NeoPremiumRentalIndex) or not index.weapons:
        raise ValueError("cannot install empty Neo Premium rental index")
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = index


def get_neo_premium_rental_index() -> NeoPremiumRentalIndex:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = build_neo_premium_rental_index()
        return _INDEX


def clear_neo_premium_rental_index() -> None:
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None


def resolve_neo_premium_rental_weapon(
    *,
    job: int,
    effective_level: int,
    group_index: int,
    item_index: int,
) -> NeoPremiumRentalWeapon:
    weapon = get_neo_premium_rental_index().resolve(
        job=job,
        effective_level=effective_level,
        group_index=group_index,
        item_index=item_index,
    )
    metadata = item_metadata.resolve_item_metadata(weapon.item_id)
    if (
        not metadata.is_equipment
        or str(metadata.equipment_type).lower() != "[weapon]"
        or not metadata.is_job_allowed(int(job))
        or int(metadata.minimum_level) != int(weapon.minimum_level)
    ):
        raise ValueError("rental weapon metadata changed item=%d" % weapon.item_id)
    return weapon


def neo_premium_rental_item_ids() -> tuple[int, ...]:
    """Return every current-PVF template owned by the Plus rental service."""
    return tuple(int(row.item_id) for row in get_neo_premium_rental_index().weapons)


def neo_premium_daily_period(now_unix: int) -> tuple[int, int]:
    now = max(0, int(now_unix))
    period_start = ((now - NEO_PREMIUM_RESET_UTC_OFFSET) // 86400) * 86400
    period_start += NEO_PREMIUM_RESET_UTC_OFFSET
    return period_start, period_start + 86400


def neo_premium_daily_reward_items() -> tuple[tuple[int, int], ...]:
    """Return the boot-warmed type-118 ``[items]`` reward sequence."""
    definition = get_premium_contract_index().resolve_type(NEO_PREMIUM_PLUS_TYPE)
    if definition is None:
        return ()
    rewards = tuple(
        (int(item_id), int(count))
        for item_id, count in definition.effects.daily_items
    )
    if any(
        item_id <= 0
        or item_id > 0xFFFFFFFF
        or count <= 0
        or count > 0xFFFFFFFF
        for item_id, count in rewards
    ):
        raise ValueError("Neo Premium Plus type 118 has invalid [items] data")
    return rewards


def warmup_neo_premium_rental_index(cache: Any) -> dict[str, Any]:
    payload = cache.read(RENTAL_CACHE_DOMAIN, RENTAL_CACHE_VERSION)
    if payload is not None:
        try:
            index = NeoPremiumRentalIndex.from_cache_payload(payload)
            install_neo_premium_rental_index(index)
            return {
                "ok": 1,
                "weapon_count": len(index.weapons),
                "job_count": len(index._by_job),
                "cache": "hit",
            }
        except Exception:
            cache.discard(RENTAL_CACHE_DOMAIN)
    index = build_neo_premium_rental_index()
    install_neo_premium_rental_index(index)
    cache.write(
        RENTAL_CACHE_DOMAIN,
        RENTAL_CACHE_VERSION,
        index.to_cache_payload(),
        record_count=len(index.weapons),
    )
    return {
        "ok": 1,
        "weapon_count": len(index.weapons),
        "job_count": len(index._by_job),
        "missing_script_count": int(getattr(index, "missing_script_count", 0)),
        "cache": "disabled"
        if not cache.enabled
        else cache.last_status.get(RENTAL_CACHE_DOMAIN, "miss"),
    }


__all__ = [
    "NEO_PREMIUM_DAILY_GIFT_ITEM_ID",
    "NEO_PREMIUM_PLUS_TYPE",
    "NEO_PREMIUM_RENTAL_SERVICE",
    "NEO_PREMIUM_WEAPON_GROUP",
    "NeoPremiumRentalIndex",
    "NeoPremiumRentalWeapon",
    "build_neo_premium_rental_index",
    "clear_neo_premium_rental_index",
    "get_neo_premium_rental_index",
    "install_neo_premium_rental_index",
    "neo_premium_daily_period",
    "neo_premium_daily_reward_items",
    "neo_premium_rental_item_ids",
    "resolve_neo_premium_rental_weapon",
    "warmup_neo_premium_rental_index",
]
