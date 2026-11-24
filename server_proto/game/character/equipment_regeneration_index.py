"""PVF-derived pools and costs for Stage3 equipment regeneration."""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING
from typing import Any, Iterable

from game.character.item_metadata import job_label_for_id
from pvf import pvf_runtime


CONFIG_PATH = "etc/randomoption/regenerationrandomoption.etc"
EQUIPMENT_LIST_PATH = "equipment/equipment.lst"
EQUIPMENT_REGENERATION_CACHE_DOMAIN = "equipment_regeneration"
EQUIPMENT_REGENERATION_CACHE_VERSION = 2
WEIGHT_SCALE = 1_000_000

_LST_PAIR_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_SECTION_TEMPLATE = r"\[{name}\]\s*(.*?)\[/{name}\]"
_FIELD_TEMPLATE = r"\[{name}\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)"
_TOKEN_RE = re.compile(r"`([^`]*)`|(-?\d+(?:\.\d+)?)")
_LOCK = threading.RLock()
_INDEX: "EquipmentRegenerationIndex | None" = None


def _normalized_text(value: object) -> str:
    text = str(value or "").strip().replace("`", "").lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return " ".join(text.split())


def _normalize_job(value: object) -> str:
    text = _normalized_text(value)
    aliases = {
        "atgunner": "at gunner",
        "atfighter": "at fighter",
        "atmage": "at mage",
        "atswordman": "at swordman",
        "atpriest": "at priest",
        "demonicswordman": "demonic swordman",
        "demonic lancer": "demonic lancer",
        "demoniclancer": "demonic lancer",
        "creatormage": "creator mage",
        "darkknight": "demonic swordman",
        "dark knight": "demonic swordman",
    }
    return aliases.get(text, aliases.get(text.replace(" ", ""), text))


def _sections(text: str, name: str) -> list[str]:
    return re.findall(
        _SECTION_TEMPLATE.format(name=re.escape(str(name))),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )


def _one_section(text: str, name: str) -> str:
    matches = _sections(text, name)
    if len(matches) != 1:
        raise ValueError(
            "expected one [%s] section, found %d" % (name, len(matches))
        )
    return matches[0]


def _tokens(text: str) -> list[str]:
    return [tick if tick else number for tick, number in _TOKEN_RE.findall(text)]


def _integer_tokens(text: str, *, context: str) -> list[int]:
    values: list[int] = []
    for token in _tokens(text):
        try:
            values.append(int(token, 10))
        except ValueError as exc:
            raise ValueError(
                "non-integer token in %s: %r" % (context, token)
            ) from exc
    return values


def _field_body(text: str, name: str) -> str:
    match = re.search(
        _FIELD_TEMPLATE.format(name=re.escape(str(name))),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _field_int(text: str, name: str) -> int | None:
    body = _field_body(text, name)
    match = re.search(r"-?\d+", body)
    return int(match.group(0)) if match is not None else None


def _field_tick(text: str, name: str) -> str:
    body = _field_body(text, name)
    tick = re.search(r"`([^`]*)`", body)
    if tick is not None:
        return _normalized_text(tick.group(1))
    for line in body.splitlines():
        if line.strip():
            return _normalized_text(line)
    return ""


def _field_jobs(text: str) -> tuple[str, ...]:
    body = _field_body(text, "usable job")
    jobs: list[str] = []
    for raw in re.findall(r"`([^`]*)`", body):
        job = _normalize_job(raw)
        if job and job not in jobs:
            jobs.append(job)
    return tuple(jobs)


def _parse_equipment_paths(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    seen: set[int] = set()
    for raw_id, raw_path in _LST_PAIR_RE.findall(str(text or "")):
        item_id = int(raw_id)
        relative = str(raw_path or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        if item_id in seen:
            raise ValueError("duplicate equipment item id=%d" % item_id)
        seen.add(item_id)
        path = (
            relative
            if relative.lower().startswith("equipment/")
            else "equipment/" + relative
        )
        entries.append((item_id, path))
    if not entries:
        raise ValueError("equipment/equipment.lst has no positive entries")
    return tuple(entries)


@dataclass(frozen=True)
class RegenerationMaterialCost:
    item_id: int
    count: int


@dataclass(frozen=True)
class RegenerationPriceRow:
    level: int
    currency_item_id: int
    base_cost: int


@dataclass(frozen=True)
class EquipmentRegenerationConfig:
    groups_by_category: dict[int, tuple[str, ...]]
    category_by_group: dict[str, int]
    excluded_item_ids: frozenset[int]
    probability_weights: dict[int, int]
    special_materials_by_rarity: dict[int, tuple[RegenerationMaterialCost, ...]]
    special_level_multipliers: dict[int, tuple[tuple[int, Decimal], ...]]
    price_max_level: int
    prices_by_job_key: dict[int, tuple[RegenerationPriceRow, ...]]
    rarity_price_rates: dict[str, int]

    def category_for_group(self, group_name: str) -> int | None:
        return self.category_by_group.get(_normalized_text(group_name))

    def material_costs(self, rarity: int, level: int) -> tuple[RegenerationMaterialCost, ...]:
        base = self.special_materials_by_rarity.get(int(rarity), ())
        if not base:
            return ()
        rows = self.special_level_multipliers.get(int(rarity), ())
        if not rows:
            raise ValueError(
                "special regeneration rarity has no level multiplier rarity=%d"
                % int(rarity)
            )
        item_level = max(0, int(level))
        chosen = rows[0][1]
        for row_level, multiplier in rows:
            if row_level > item_level:
                break
            chosen = multiplier
        costs: list[RegenerationMaterialCost] = []
        for requirement in base:
            count = int(
                (Decimal(requirement.count) * chosen).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
            if count > 0:
                costs.append(
                    RegenerationMaterialCost(requirement.item_id, count)
                )
        return tuple(costs)

    def gold_cost(self, rarity: int, level: int, character_job: int) -> int:
        if int(rarity) in self.special_materials_by_rarity:
            return 0
        job_key = _price_job_key(int(character_job))
        rows = self.prices_by_job_key.get(job_key)
        if not rows:
            raise ValueError("regeneration price job key is missing key=%d" % job_key)
        item_level = min(max(0, int(level)), int(self.price_max_level))
        chosen = rows[0]
        for row in rows:
            if row.level > item_level:
                break
            chosen = row
        if chosen.currency_item_id != 0:
            raise ValueError(
                "unsupported regeneration price currency item=%d"
                % chosen.currency_item_id
            )
        rate_key = "common" if int(rarity) <= 2 else "unique"
        rate = self.rarity_price_rates.get(rate_key)
        if rate is None:
            raise ValueError("regeneration rarity price rate missing key=%s" % rate_key)
        return max(
            0,
            int(
                (Decimal(chosen.base_cost) * Decimal(rate) / Decimal(100))
                .to_integral_value(rounding=ROUND_CEILING)
            ),
        )


def _price_job_key(character_job: int) -> int:
    job = int(character_job)
    if 0 <= job <= 11:
        return job + 1
    return {12: 15, 13: 101, 14: 201}.get(job, job + 1)


def parse_equipment_regeneration_config(text: str) -> EquipmentRegenerationConfig:
    groups_by_category: dict[int, tuple[str, ...]] = {}
    category_by_group: dict[str, int] = {}
    for body in _sections(text, "choose part"):
        values = _tokens(body)
        if len(values) < 3:
            raise ValueError("truncated [choose part] section")
        category = int(values[0])
        groups = tuple(
            value
            for value in (_normalized_text(raw) for raw in values[2:])
            if value
        )
        if category <= 0 or category in groups_by_category or not groups:
            raise ValueError("invalid regeneration category=%d" % category)
        for group in groups:
            previous = category_by_group.get(group)
            if previous is not None and previous != category:
                raise ValueError(
                    "equipment group belongs to multiple categories group=%s"
                    % group
                )
            category_by_group[group] = category
        groups_by_category[category] = groups
    if set(groups_by_category) != {1, 2, 3, 4}:
        raise ValueError(
            "regeneration choose-part categories must be 1..4 actual=%s"
            % sorted(groups_by_category)
        )

    excluded = frozenset(
        _integer_tokens(
            _one_section(text, "except item index"),
            context="except item index",
        )
    )

    probability_tokens = _tokens(
        _one_section(text, "probability exception handling")
    )
    if len(probability_tokens) % 2:
        raise ValueError("probability exception table has an odd token count")
    probability_weights: dict[int, int] = {}
    for offset in range(0, len(probability_tokens), 2):
        item_id = int(probability_tokens[offset])
        factor = Decimal(probability_tokens[offset + 1])
        weight = int(
            (factor * Decimal(WEIGHT_SCALE)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if item_id <= 0 or weight <= 0:
            raise ValueError("invalid probability exception item=%d" % item_id)
        probability_weights[item_id] = weight

    special_values = _integer_tokens(
        _one_section(text, "compound need material for specific"),
        context="compound need material for specific",
    )
    special_materials: dict[int, tuple[RegenerationMaterialCost, ...]] = {}
    offset = 0
    while offset < len(special_values):
        if offset + 2 > len(special_values):
            raise ValueError("truncated special regeneration material header")
        rarity = special_values[offset]
        count = special_values[offset + 1]
        offset += 2
        if rarity < 0 or count <= 0 or offset + count * 2 > len(special_values):
            raise ValueError("invalid special material row rarity=%d" % rarity)
        requirements: list[RegenerationMaterialCost] = []
        for _index in range(count):
            item_id = special_values[offset]
            quantity = special_values[offset + 1]
            offset += 2
            if item_id <= 0 or quantity <= 0:
                raise ValueError("invalid special material requirement")
            requirements.append(RegenerationMaterialCost(item_id, quantity))
        if rarity in special_materials:
            raise ValueError("duplicate special material rarity=%d" % rarity)
        special_materials[rarity] = tuple(requirements)

    multiplier_values = _tokens(
        _one_section(text, "compound specific apply cost by level")
    )
    if len(multiplier_values) % 3:
        raise ValueError("special level multiplier table is truncated")
    level_multipliers: dict[int, list[tuple[int, Decimal]]] = {}
    for offset in range(0, len(multiplier_values), 3):
        rarity = int(multiplier_values[offset])
        level = int(multiplier_values[offset + 1])
        multiplier = Decimal(multiplier_values[offset + 2])
        if level < 0 or multiplier <= 0:
            raise ValueError("invalid special level multiplier rarity=%d" % rarity)
        level_multipliers.setdefault(rarity, []).append((level, multiplier))
    normalized_multipliers: dict[int, tuple[tuple[int, Decimal], ...]] = {}
    for rarity, rows in level_multipliers.items():
        ordered = tuple(sorted(rows, key=lambda row: row[0]))
        if len({row[0] for row in ordered}) != len(ordered):
            raise ValueError("duplicate special multiplier level rarity=%d" % rarity)
        normalized_multipliers[rarity] = ordered
    if set(special_materials) - set(normalized_multipliers):
        raise ValueError("special material rarity has no multiplier table")

    rate_tokens = _tokens(
        _one_section(text, "rarity regeneration price rate")
    )
    if len(rate_tokens) % 2:
        raise ValueError("rarity price rate table is truncated")
    rarity_rates: dict[str, int] = {}
    for offset in range(0, len(rate_tokens), 2):
        key = _normalized_text(rate_tokens[offset])
        value = int(rate_tokens[offset + 1])
        if not key or value < 0:
            raise ValueError("invalid rarity price rate")
        rarity_rates[key] = value
    if not {"common", "unique"}.issubset(rarity_rates):
        raise ValueError("rarity price rates require common and unique")

    price_values = _integer_tokens(
        _one_section(text, "regeneration price"),
        context="regeneration price",
    )
    if not price_values or price_values[0] <= 0:
        raise ValueError("regeneration price table has no maximum level")
    max_level = int(price_values[0])
    prices_by_job: dict[int, tuple[RegenerationPriceRow, ...]] = {}
    offset = 1
    while offset < len(price_values):
        job_key = int(price_values[offset])
        offset += 1
        rows: list[RegenerationPriceRow] = []
        previous_level = -1
        while True:
            if offset + 3 > len(price_values):
                raise ValueError("truncated regeneration price job=%d" % job_key)
            level, currency_item_id, base_cost = price_values[offset : offset + 3]
            offset += 3
            if level <= previous_level or level > max_level:
                raise ValueError("invalid regeneration price level job=%d" % job_key)
            if currency_item_id < 0 or base_cost < 0:
                raise ValueError("negative regeneration price job=%d" % job_key)
            rows.append(
                RegenerationPriceRow(level, currency_item_id, base_cost)
            )
            previous_level = level
            if level == max_level:
                break
        if job_key in prices_by_job:
            raise ValueError("duplicate regeneration price job=%d" % job_key)
        prices_by_job[job_key] = tuple(rows)
    if not prices_by_job:
        raise ValueError("regeneration price table has no jobs")

    return EquipmentRegenerationConfig(
        groups_by_category=groups_by_category,
        category_by_group=category_by_group,
        excluded_item_ids=excluded,
        probability_weights=probability_weights,
        special_materials_by_rarity=special_materials,
        special_level_multipliers=normalized_multipliers,
        price_max_level=max_level,
        prices_by_job_key=prices_by_job,
        rarity_price_rates=rarity_rates,
    )


@dataclass(frozen=True)
class EquipmentRegenerationEntry:
    item_id: int
    category: int
    item_group: str
    rarity: int
    minimum_level: int
    usable_jobs: tuple[str, ...]
    source_path: str
    attach_type: str
    probability_weight: int = WEIGHT_SCALE

    def allows_job(self, character_job: int) -> bool:
        if not self.usable_jobs or "all" in self.usable_jobs:
            return True
        label = _normalize_job(job_label_for_id(int(character_job)))
        return bool(label and label in self.usable_jobs)

    def to_cache_row(self) -> list[Any]:
        return [
            int(self.item_id),
            int(self.category),
            str(self.item_group),
            int(self.rarity),
            int(self.minimum_level),
            list(self.usable_jobs),
            str(self.source_path),
            str(self.attach_type),
            int(self.probability_weight),
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "EquipmentRegenerationEntry":
        if not isinstance(row, list) or len(row) != 9 or not isinstance(row[5], list):
            raise ValueError("invalid equipment regeneration cache row")
        return cls(
            item_id=int(row[0]),
            category=int(row[1]),
            item_group=_normalized_text(row[2]),
            rarity=int(row[3]),
            minimum_level=max(0, int(row[4])),
            usable_jobs=tuple(_normalize_job(value) for value in row[5]),
            source_path=str(row[6] or ""),
            attach_type=_normalized_text(row[7]),
            probability_weight=int(row[8]),
        )


@dataclass(frozen=True)
class EquipmentRegenerationIndex:
    config: EquipmentRegenerationConfig
    entries_by_item_id: dict[int, EquipmentRegenerationEntry]
    pools_by_key: dict[tuple[int, int, int], tuple[EquipmentRegenerationEntry, ...]]
    scanned_equipment_count: int
    missing_equipment_script_count: int = 0
    cache_status: str = ""

    def entry_for(self, item_id: int) -> EquipmentRegenerationEntry | None:
        return self.entries_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    def candidates(
        self,
        *,
        category: int,
        rarity: int,
        minimum_level: int,
        character_job: int,
    ) -> tuple[EquipmentRegenerationEntry, ...]:
        pool = self.pools_by_key.get(
            (int(category), int(rarity), max(0, int(minimum_level))),
            (),
        )
        return tuple(entry for entry in pool if entry.allows_job(character_job))

    def roll(
        self,
        *,
        category: int,
        rarity: int,
        minimum_level: int,
        character_job: int,
        rng: Any = None,
    ) -> EquipmentRegenerationEntry:
        candidates = self.candidates(
            category=category,
            rarity=rarity,
            minimum_level=minimum_level,
            character_job=character_job,
        )
        if not candidates:
            raise RuntimeError(
                "equipment regeneration pool is empty category=%d rarity=%d level=%d job=%d"
                % (category, rarity, minimum_level, character_job)
            )
        total = sum(max(0, int(entry.probability_weight)) for entry in candidates)
        if total <= 0:
            raise RuntimeError("equipment regeneration pool has no positive weights")
        roll = (
            int(rng.randrange(total))
            if rng is not None
            else int(secrets.randbelow(total))
        )
        cursor = 0
        for entry in candidates:
            cursor += int(entry.probability_weight)
            if roll < cursor:
                return entry
        return candidates[-1]

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_equipment_count": int(self.scanned_equipment_count),
            "missing_equipment_script_count": int(
                self.missing_equipment_script_count
            ),
            "entries": [
                entry.to_cache_row()
                for _item_id, entry in sorted(self.entries_by_item_id.items())
            ],
        }

    @classmethod
    def from_cache_payload(
        cls,
        payload: Any,
        *,
        config: EquipmentRegenerationConfig,
    ) -> "EquipmentRegenerationIndex":
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise ValueError("invalid equipment regeneration cache payload")
        scanned = int(payload.get("scanned_equipment_count") or 0)
        missing = int(payload.get("missing_equipment_script_count") or 0)
        if scanned <= 0:
            raise ValueError("invalid cached regeneration scan count")
        if missing < 0 or missing > scanned:
            raise ValueError("invalid cached regeneration missing-script count")
        return build_equipment_regeneration_index(
            config,
            (EquipmentRegenerationEntry.from_cache_row(row) for row in payload["entries"]),
            scanned_equipment_count=scanned,
            missing_equipment_script_count=missing,
            cache_status="hit",
        )


def build_equipment_regeneration_index(
    config: EquipmentRegenerationConfig,
    entries: Iterable[EquipmentRegenerationEntry],
    *,
    scanned_equipment_count: int,
    missing_equipment_script_count: int = 0,
    cache_status: str = "",
) -> EquipmentRegenerationIndex:
    by_id: dict[int, EquipmentRegenerationEntry] = {}
    pools: dict[tuple[int, int, int], list[EquipmentRegenerationEntry]] = {}
    for entry in entries:
        if entry.item_id <= 0 or entry.item_id > 0xFFFFFFFF:
            raise ValueError("invalid regeneration equipment id=%d" % entry.item_id)
        expected_category = config.category_for_group(entry.item_group)
        if expected_category != int(entry.category):
            raise ValueError(
                "regeneration category mismatch item=%d group=%s"
                % (entry.item_id, entry.item_group)
            )
        if entry.item_id in config.excluded_item_ids:
            raise ValueError("excluded regeneration item was indexed item=%d" % entry.item_id)
        if entry.rarity < 0 or entry.minimum_level < 0 or entry.probability_weight <= 0:
            raise ValueError("invalid regeneration entry item=%d" % entry.item_id)
        if entry.item_id in by_id:
            raise ValueError("duplicate regeneration item=%d" % entry.item_id)
        by_id[entry.item_id] = entry
        pools.setdefault(
            (entry.category, entry.rarity, entry.minimum_level), []
        ).append(entry)
    if not by_id:
        raise ValueError("equipment regeneration index has no entries")
    missing_count = max(0, int(missing_equipment_script_count))
    if missing_count > max(0, int(scanned_equipment_count)):
        raise ValueError("regeneration missing-script count exceeds scan count")
    normalized_pools = {
        key: tuple(sorted(values, key=lambda value: value.item_id))
        for key, values in pools.items()
    }
    for category in config.groups_by_category:
        if not any(key[0] == category for key in normalized_pools):
            raise ValueError(
                "equipment regeneration category has no candidates category=%d"
                % category
            )
    return EquipmentRegenerationIndex(
        config=config,
        entries_by_item_id=by_id,
        pools_by_key=normalized_pools,
        scanned_equipment_count=max(0, int(scanned_equipment_count)),
        missing_equipment_script_count=missing_count,
        cache_status=str(cache_status or ""),
    )


def build_equipment_regeneration_index_from_reader(
    reader: Any,
    *,
    config: EquipmentRegenerationConfig | None = None,
) -> EquipmentRegenerationIndex:
    cfg = config or parse_equipment_regeneration_config(
        reader.read_text(CONFIG_PATH)
    )
    equipment_paths = _parse_equipment_paths(
        reader.read_text(EQUIPMENT_LIST_PATH)
    )
    entries: list[EquipmentRegenerationEntry] = []
    missing_equipment_script_count = 0
    for item_id, source_path in equipment_paths:
        if item_id in cfg.excluded_item_ids:
            continue
        try:
            script = reader.read_text(source_path)
        except KeyError:
            missing_equipment_script_count += 1
            continue
        item_group = _field_tick(script, "item group name")
        category = cfg.category_for_group(item_group)
        if category is None:
            continue
        rarity = _field_int(script, "rarity")
        minimum_level = _field_int(script, "minimum level")
        if rarity is None or minimum_level is None:
            continue
        entries.append(
            EquipmentRegenerationEntry(
                item_id=item_id,
                category=category,
                item_group=item_group,
                rarity=max(0, int(rarity)),
                minimum_level=max(0, int(minimum_level)),
                usable_jobs=_field_jobs(script),
                source_path=source_path,
                attach_type=_field_tick(script, "attach type"),
                probability_weight=cfg.probability_weights.get(
                    item_id, WEIGHT_SCALE
                ),
            )
        )
    return build_equipment_regeneration_index(
        cfg,
        entries,
        scanned_equipment_count=len(equipment_paths),
        missing_equipment_script_count=missing_equipment_script_count,
    )


def install_equipment_regeneration_index(index: EquipmentRegenerationIndex) -> None:
    if not isinstance(index, EquipmentRegenerationIndex):
        raise ValueError("invalid equipment regeneration index")
    if not index.entries_by_item_id:
        raise ValueError("equipment regeneration index is empty")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_equipment_regeneration_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_equipment_regeneration_index() -> EquipmentRegenerationIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("equipment regeneration index is not warmed")
    return index


def warmup_equipment_regeneration_index(cache: Any = None) -> dict[str, Any]:
    reader = pvf_runtime.ensure_shared_reader()
    config = parse_equipment_regeneration_config(reader.read_text(CONFIG_PATH))
    index: EquipmentRegenerationIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(
            EQUIPMENT_REGENERATION_CACHE_DOMAIN,
            EQUIPMENT_REGENERATION_CACHE_VERSION,
        )
        if payload is not None:
            try:
                index = EquipmentRegenerationIndex.from_cache_payload(
                    payload,
                    config=config,
                )
                cache_status = "hit"
            except Exception:
                cache.discard(EQUIPMENT_REGENERATION_CACHE_DOMAIN)
    if index is None:
        index = build_equipment_regeneration_index_from_reader(
            reader,
            config=config,
        )
        cache_status = "miss" if cache is not None else "disabled"
        if cache is not None:
            cache.write(
                EQUIPMENT_REGENERATION_CACHE_DOMAIN,
                EQUIPMENT_REGENERATION_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=len(index.entries_by_item_id),
            )
    index = replace(index, cache_status=cache_status)
    install_equipment_regeneration_index(index)
    return {
        "ok": 1,
        "candidate_count": len(index.entries_by_item_id),
        "pool_count": len(index.pools_by_key),
        "category_count": len(index.config.groups_by_category),
        "excluded_count": len(index.config.excluded_item_ids),
        "scanned_equipment_count": index.scanned_equipment_count,
        "missing_equipment_script_count": index.missing_equipment_script_count,
        "cache": cache_status,
    }


__all__ = [
    "CONFIG_PATH",
    "EQUIPMENT_REGENERATION_CACHE_DOMAIN",
    "EQUIPMENT_REGENERATION_CACHE_VERSION",
    "EquipmentRegenerationConfig",
    "EquipmentRegenerationEntry",
    "EquipmentRegenerationIndex",
    "RegenerationMaterialCost",
    "RegenerationPriceRow",
    "build_equipment_regeneration_index",
    "build_equipment_regeneration_index_from_reader",
    "clear_equipment_regeneration_index",
    "get_equipment_regeneration_index",
    "install_equipment_regeneration_index",
    "parse_equipment_regeneration_config",
    "warmup_equipment_regeneration_index",
]
