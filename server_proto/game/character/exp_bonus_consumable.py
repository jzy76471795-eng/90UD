"""PVF-backed dungeon-clear EXP consumables and persisted active effects."""
from __future__ import annotations

import re
import struct
import threading
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from game.character.item_metadata import job_label_for_id
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
EXP_BONUS_CACHE_DOMAIN = "exp_bonus_consumables"
EXP_BONUS_CACHE_VERSION = 2
EXP_BONUS_RATE_SCALE = 1_000_000
MAX_EXP_BONUS_RATE_PPM = 100 * EXP_BONUS_RATE_SCALE
MAX_EXP_BONUS_DURATION_MS = 365 * 24 * 60 * 60 * 1000
EXP_BONUS_COOLTIME_ITEM_INFO_OPCODE = 0x00AC
EXP_BONUS_EFFECT_ITEM_INFO_OPCODE = 0x00AE

_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_LOCK = threading.RLock()
_INDEX: "ExpBonusConsumableIndex | None" = None


def _section(text: str, name: str) -> str:
    match = re.search(
        r"\[" + re.escape(name) + r"\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)",
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _first_decimal(text: str) -> Decimal | None:
    match = _NUMBER_RE.search(str(text or ""))
    if match is None:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _first_integer(text: str) -> int | None:
    value = _first_decimal(text)
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


def _optional_integer_section(text: str, name: str, *, item_id: int) -> int | None:
    body = _section(text, name)
    if not body:
        return None
    value = _first_integer(body)
    if value is None:
        raise ValueError("invalid [%s] item=%d" % (name, int(item_id)))
    return int(value)


def _parse_stackable_paths(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        if item_id in seen_ids:
            raise ValueError("duplicate stackable item id=%d" % item_id)
        seen_ids.add(item_id)
        source_path = (
            relative
            if relative.lower().startswith("stackable/")
            else "stackable/" + relative
        )
        entries.append((item_id, source_path))
    if not entries:
        raise ValueError("stackable.lst has no item paths")
    return tuple(entries)


def _section_tokens(text: str, name: str) -> tuple[str, ...]:
    values = re.findall(r"`([^`]*)`", _section(text, name))
    normalized: list[str] = []
    for value in values:
        token = " ".join(str(value or "").strip().lower().split())
        if token.startswith("[") and token.endswith("]") and len(token) >= 2:
            token = " ".join(token[1:-1].strip().split())
        if token and token not in normalized:
            normalized.append(token)
    return tuple(normalized)


@dataclass(frozen=True)
class ExpBonusConsumableRule:
    item_id: int
    bonus_rate_ppm: int
    duration_ms: int
    cooltime_group: int = 0
    source_path: str = ""
    minimum_level: int | None = None
    maximum_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        rate = int(self.bonus_rate_ppm)
        duration = int(self.duration_ms)
        cooltime_group = int(self.cooltime_group)
        minimum_level = (
            None if self.minimum_level is None else int(self.minimum_level)
        )
        maximum_level = (
            None if self.maximum_level is None else int(self.maximum_level)
        )
        usable_jobs = tuple(
            " ".join(str(value or "").strip().lower().strip("[]").split())
            for value in self.usable_jobs
            if str(value or "").strip()
        )
        impossible_contents = tuple(
            " ".join(str(value or "").strip().lower().strip("[]").split())
            for value in self.impossible_contents
            if str(value or "").strip()
        )
        source_path = str(self.source_path or "").strip()
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError("invalid EXP consumable item id=%d" % item_id)
        if rate <= 0 or rate > MAX_EXP_BONUS_RATE_PPM:
            raise ValueError(
                "invalid EXP consumable rate item=%d rate_ppm=%d" % (item_id, rate)
            )
        if duration <= 0 or duration > MAX_EXP_BONUS_DURATION_MS:
            raise ValueError(
                "invalid EXP consumable duration item=%d duration_ms=%d"
                % (item_id, duration)
            )
        if cooltime_group < 0 or cooltime_group > 0xFFFF:
            raise ValueError(
                "invalid EXP consumable cooltime group item=%d group=%d"
                % (item_id, cooltime_group)
            )
        if minimum_level is not None and minimum_level < 0:
            raise ValueError("invalid EXP consumable minimum level item=%d" % item_id)
        if maximum_level is not None and maximum_level < 0:
            raise ValueError("invalid EXP consumable maximum level item=%d" % item_id)
        if (
            minimum_level is not None
            and maximum_level is not None
            and minimum_level > maximum_level
        ):
            raise ValueError("invalid EXP consumable level range item=%d" % item_id)
        if not source_path:
            raise ValueError("EXP consumable source path missing item=%d" % item_id)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "bonus_rate_ppm", rate)
        object.__setattr__(self, "duration_ms", duration)
        object.__setattr__(self, "cooltime_group", cooltime_group)
        object.__setattr__(self, "minimum_level", minimum_level)
        object.__setattr__(self, "maximum_level", maximum_level)
        object.__setattr__(self, "usable_jobs", usable_jobs)
        object.__setattr__(self, "impossible_contents", impossible_contents)
        object.__setattr__(self, "source_path", source_path)

    @property
    def duration_seconds(self) -> int:
        return max(1, (self.duration_ms + 999) // 1000)

    def compute_bonus_exp(self, clear_base_exp: int) -> int:
        base = max(0, int(clear_base_exp))
        return min(
            0xFFFFFFFF,
            base * int(self.bonus_rate_ppm) // EXP_BONUS_RATE_SCALE,
        )

    @property
    def is_hardcore_blocked(self) -> bool:
        return any("hardcore mode" in value for value in self.impossible_contents)

    def is_usable(
        self,
        *,
        level: int,
        job: int,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        current_level = max(1, int(level))
        if self.minimum_level is not None and current_level < self.minimum_level:
            return False, "below_minimum_level"
        if self.maximum_level is not None and current_level > self.maximum_level:
            return False, "above_maximum_level"
        if hardcore and self.is_hardcore_blocked:
            return False, "hardcore_mode"
        allowed = set(self.usable_jobs)
        if allowed and "all" not in allowed:
            label = job_label_for_id(int(job))
            if not label or label not in allowed:
                return False, "job_restricted"
        return True, "ok"

    def to_cache_row(self) -> list[Any]:
        return [
            int(self.item_id),
            int(self.bonus_rate_ppm),
            int(self.duration_ms),
            int(self.cooltime_group),
            self.minimum_level,
            self.maximum_level,
            list(self.usable_jobs),
            list(self.impossible_contents),
            str(self.source_path),
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "ExpBonusConsumableRule":
        if not isinstance(row, list) or len(row) != 9:
            raise ValueError("invalid cached EXP consumable row")
        if not isinstance(row[6], list) or not isinstance(row[7], list):
            raise ValueError("invalid cached EXP consumable eligibility fields")
        return cls(
            item_id=int(row[0]),
            bonus_rate_ppm=int(row[1]),
            duration_ms=int(row[2]),
            cooltime_group=int(row[3]),
            minimum_level=None if row[4] is None else int(row[4]),
            maximum_level=None if row[5] is None else int(row[5]),
            usable_jobs=tuple(str(value) for value in row[6]),
            impossible_contents=tuple(str(value) for value in row[7]),
            source_path=str(row[8]),
        )


def parse_exp_bonus_consumable_rule(
    item_id: int,
    text: str,
    *,
    source_path: str,
) -> ExpBonusConsumableRule | None:
    rate_body = _section(text, "exp bonus rate")
    if not rate_body:
        return None

    rate_decimal = _first_decimal(rate_body)
    duration_body = _section(text, "stat change duration")
    duration_ms = _first_integer(duration_body)
    if rate_decimal is None or not rate_decimal.is_finite() or rate_decimal <= 0:
        raise ValueError("invalid [exp bonus rate] item=%d path=%s" % (item_id, source_path))
    if duration_ms is None or duration_ms <= 0:
        raise ValueError(
            "invalid [stat change duration] item=%d path=%s" % (item_id, source_path)
        )
    if "myself" not in duration_body.lower().replace("`", " ").split():
        raise ValueError(
            "unsupported EXP consumable target item=%d path=%s"
            % (item_id, source_path)
        )

    rate_ppm = int(
        (rate_decimal * EXP_BONUS_RATE_SCALE).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    cooltime_group = _first_integer(_section(text, "cooltime group")) or 0
    minimum_level = _optional_integer_section(
        text,
        "minimum level",
        item_id=item_id,
    )
    maximum_level = _optional_integer_section(
        text,
        "maximum level",
        item_id=item_id,
    )
    return ExpBonusConsumableRule(
        item_id=int(item_id),
        bonus_rate_ppm=rate_ppm,
        duration_ms=int(duration_ms),
        cooltime_group=int(cooltime_group),
        minimum_level=minimum_level,
        maximum_level=maximum_level,
        usable_jobs=_section_tokens(text, "usable job"),
        impossible_contents=_section_tokens(text, "impossible contents"),
        source_path=source_path,
    )


@dataclass(frozen=True)
class ExpBonusConsumableIndex:
    rules_by_item_id: dict[int, ExpBonusConsumableRule]
    scanned_stackable_count: int
    cache_status: str = ""

    def rule_for(self, item_id: int) -> ExpBonusConsumableRule | None:
        return self.rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    @property
    def rule_count(self) -> int:
        return len(self.rules_by_item_id)

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_stackable_count": int(self.scanned_stackable_count),
            "rules": [
                rule.to_cache_row()
                for _item_id, rule in sorted(self.rules_by_item_id.items())
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any) -> "ExpBonusConsumableIndex":
        if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
            raise ValueError("invalid EXP consumable cache payload")
        scanned = int(payload.get("scanned_stackable_count") or 0)
        if scanned <= 0:
            raise ValueError("invalid cached EXP consumable stackable count")
        return build_exp_bonus_consumable_index(
            (ExpBonusConsumableRule.from_cache_row(row) for row in payload["rules"]),
            scanned_stackable_count=scanned,
            cache_status="hit",
        )


def build_exp_bonus_consumable_index(
    rules: Iterable[ExpBonusConsumableRule],
    *,
    scanned_stackable_count: int,
    cache_status: str = "",
) -> ExpBonusConsumableIndex:
    by_item_id: dict[int, ExpBonusConsumableRule] = {}
    for rule in rules:
        if not isinstance(rule, ExpBonusConsumableRule):
            raise TypeError("EXP consumable index accepts ExpBonusConsumableRule only")
        if rule.item_id in by_item_id:
            raise ValueError("duplicate EXP consumable item id=%d" % rule.item_id)
        by_item_id[rule.item_id] = rule
    if not by_item_id:
        raise ValueError("PVF contains no [exp bonus rate] consumables")
    return ExpBonusConsumableIndex(
        rules_by_item_id=dict(sorted(by_item_id.items())),
        scanned_stackable_count=max(0, int(scanned_stackable_count)),
        cache_status=str(cache_status or ""),
    )


def build_exp_bonus_consumable_index_from_reader(reader: Any) -> ExpBonusConsumableIndex:
    paths = _parse_stackable_paths(reader.read_text(STACKABLE_LIST_PATH))
    rules: list[ExpBonusConsumableRule] = []
    for item_id, source_path in paths:
        rule = parse_exp_bonus_consumable_rule(
            item_id,
            reader.read_text(source_path),
            source_path=source_path,
        )
        if rule is not None:
            rules.append(rule)
    return build_exp_bonus_consumable_index(
        rules,
        scanned_stackable_count=len(paths),
    )


def install_exp_bonus_consumable_index(index: ExpBonusConsumableIndex) -> None:
    if not isinstance(index, ExpBonusConsumableIndex) or index.rule_count <= 0:
        raise ValueError("invalid EXP consumable index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_exp_bonus_consumable_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_exp_bonus_consumable_index() -> ExpBonusConsumableIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("EXP consumable index is not warmed")
    return index


def resolve_exp_bonus_consumable(item_id: int) -> ExpBonusConsumableRule | None:
    """Resolve from the boot-installed index without a gameplay-time PVF read."""
    with _LOCK:
        index = _INDEX
    return index.rule_for(item_id) if index is not None else None


def warmup_exp_bonus_consumable_index(cache: Any = None) -> dict[str, Any]:
    index: ExpBonusConsumableIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(EXP_BONUS_CACHE_DOMAIN, EXP_BONUS_CACHE_VERSION)
        if payload is not None:
            try:
                index = ExpBonusConsumableIndex.from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(EXP_BONUS_CACHE_DOMAIN)
    if index is None:
        index = build_exp_bonus_consumable_index_from_reader(
            pvf_runtime.ensure_shared_reader()
        )
        if cache is not None:
            cache.write(
                EXP_BONUS_CACHE_DOMAIN,
                EXP_BONUS_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=index.rule_count,
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(cache.last_status.get(EXP_BONUS_CACHE_DOMAIN, "miss"))
            )
    index = replace(index, cache_status=cache_status)
    install_exp_bonus_consumable_index(index)
    return {
        "ok": 1,
        "scanned_stackable_count": index.scanned_stackable_count,
        "exp_bonus_consumable_count": index.rule_count,
        "exp_bonus_minimum_level_count": sum(
            rule.minimum_level is not None for rule in index.rules_by_item_id.values()
        ),
        "exp_bonus_maximum_level_count": sum(
            rule.maximum_level is not None for rule in index.rules_by_item_id.values()
        ),
        "exp_bonus_job_restricted_count": sum(
            bool(rule.usable_jobs) and "all" not in rule.usable_jobs
            for rule in index.rules_by_item_id.values()
        ),
        "exp_bonus_hardcore_blocked_count": sum(
            rule.is_hardcore_blocked for rule in index.rules_by_item_id.values()
        ),
        "cache": index.cache_status,
    }


@dataclass(frozen=True)
class ActiveExpBonusEffect:
    character_id: int
    source_item_id: int
    bonus_rate_ppm: int
    activated_at: int
    expires_at: int
    source_path: str = ""

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at) - int(time.time()))

    def compute_bonus_exp(self, clear_base_exp: int) -> int:
        base = max(0, int(clear_base_exp))
        return min(
            0xFFFFFFFF,
            base * max(0, int(self.bonus_rate_ppm)) // EXP_BONUS_RATE_SCALE,
        )


def _ensure_effect_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS character_exp_bonus_effects (
            character_id INTEGER PRIMARY KEY,
            source_item_id INTEGER NOT NULL CHECK (
                source_item_id > 0 AND source_item_id <= 4294967295
            ),
            bonus_rate_ppm INTEGER NOT NULL CHECK (
                bonus_rate_ppm > 0 AND bonus_rate_ppm <= 100000000
            ),
            activated_at INTEGER NOT NULL CHECK (activated_at >= 0),
            expires_at INTEGER NOT NULL CHECK (expires_at > activated_at),
            source_path TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(character_id) REFERENCES characters(character_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_character_exp_bonus_effects_expiry "
        "ON character_exp_bonus_effects(expires_at)"
    )


def activate_exp_bonus_consumable_at(
    repo: Any,
    character_id: int,
    *,
    list_type: int,
    slot_index: int,
    expected_item_id: int,
    rule: ExpBonusConsumableRule,
    now_unix: int | None = None,
    hardcore: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    """Consume one matching item and activate its effect in one transaction."""
    if not isinstance(rule, ExpBonusConsumableRule):
        raise TypeError("rule must be ExpBonusConsumableRule")
    cid = int(character_id)
    list_value = int(list_type) & 0xFF
    slot = int(slot_index)
    expected = int(expected_item_id) & 0xFFFFFFFF
    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    detail: dict[str, Any] = {
        "item_template_id": 0,
        "actual_item_id": 0,
        "item_kind": "",
        "original_count": 0,
        "removed": 0,
        "remaining": 0,
        "list_type": list_value,
        "slot_index": slot & 0xFFFF,
        "bonus_rate_ppm": int(rule.bonus_rate_ppm),
        "activated_at": 0,
        "expires_at": 0,
        "active_source_item_id": 0,
        "character_level": 0,
        "character_job": -1,
    }
    if cid <= 0:
        return False, "character_missing", detail
    if expected != int(rule.item_id):
        return False, "exp_bonus_rule_mismatch", detail
    if list_value == int(getattr(repo, "INVENTORY_LIST_MAIN", 0)) and slot in (0, 1):
        return False, "wallet_slot_forbidden", detail

    with repo.open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_effect_schema(conn)
        character = conn.execute(
            "SELECT level, job FROM characters "
            "WHERE character_id = ? AND delete_flag = 0",
            (cid,),
        ).fetchone()
        if character is None:
            return False, "character_missing", detail
        character_level = max(1, int(character[0] or 1))
        character_job = int(character[1] or 0)
        detail.update(
            {
                "character_level": character_level,
                "character_job": character_job,
            }
        )
        usable, usable_note = rule.is_usable(
            level=character_level,
            job=character_job,
            hardcore=bool(hardcore),
        )
        if not usable:
            return False, "exp_bonus_%s" % usable_note, detail

        active_row = conn.execute(
            "SELECT source_item_id, bonus_rate_ppm, activated_at, expires_at "
            "FROM character_exp_bonus_effects WHERE character_id = ?",
            (cid,),
        ).fetchone()
        if active_row is not None and int(active_row[3] or 0) > now:
            detail.update(
                {
                    "active_source_item_id": int(active_row[0] or 0),
                    "bonus_rate_ppm": int(active_row[1] or 0),
                    "activated_at": int(active_row[2] or 0),
                    "expires_at": int(active_row[3] or 0),
                }
            )
            return False, "exp_bonus_already_active", detail

        row = conn.execute(
            "SELECT item_uid, item_template_id, item_kind, stack_count "
            "FROM character_items "
            "WHERE character_id = ? AND list_type = ? AND slot_index = ? LIMIT 1",
            (cid, list_value, slot),
        ).fetchone()
        if row is None:
            return False, "slot_empty", detail
        item_uid = int(row[0])
        item_id = int(row[1]) & 0xFFFFFFFF
        item_kind = str(row[2] or "")
        original_count = max(0, int(row[3] or 0))
        detail.update(
            {
                "item_template_id": item_id,
                "actual_item_id": item_id,
                "item_kind": item_kind,
                "original_count": original_count,
                "remaining": original_count,
            }
        )
        if item_id <= 0 or original_count <= 0:
            return False, "invalid_item", detail
        if item_id != expected:
            return False, "item_mismatch", detail
        if item_kind == "equipment":
            return False, "equipment", detail

        remaining = original_count - 1
        if remaining <= 0:
            conn.execute("DELETE FROM character_items WHERE item_uid = ?", (item_uid,))
        else:
            conn.execute(
                "UPDATE character_items SET stack_count = ?, instance_value = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE item_uid = ?",
                (remaining, remaining, item_uid),
            )

        expires_at = min(0x7FFFFFFFFFFFFFFF, now + int(rule.duration_seconds))
        conn.execute(
            "INSERT INTO character_exp_bonus_effects "
            "(character_id, source_item_id, bonus_rate_ppm, activated_at, "
            "expires_at, source_path) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(character_id) DO UPDATE SET "
            "source_item_id = excluded.source_item_id, "
            "bonus_rate_ppm = excluded.bonus_rate_ppm, "
            "activated_at = excluded.activated_at, "
            "expires_at = excluded.expires_at, "
            "source_path = excluded.source_path, "
            "updated_at = CURRENT_TIMESTAMP",
            (
                cid,
                item_id,
                int(rule.bonus_rate_ppm),
                now,
                expires_at,
                str(rule.source_path),
            ),
        )
        detail.update(
            {
                "removed": 1,
                "remaining": max(0, remaining),
                "bonus_rate_ppm": int(rule.bonus_rate_ppm),
                "activated_at": now,
                "expires_at": expires_at,
                "active_source_item_id": item_id,
            }
        )
    return True, "exp_bonus_activated", detail


def get_active_exp_bonus_effect(
    repo: Any,
    character_id: int,
    *,
    now_unix: int | None = None,
) -> ActiveExpBonusEffect | None:
    cid = int(character_id)
    if cid <= 0:
        return None
    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    with repo.open() as conn:
        _ensure_effect_schema(conn)
        row = conn.execute(
            "SELECT source_item_id, bonus_rate_ppm, activated_at, expires_at, source_path "
            "FROM character_exp_bonus_effects WHERE character_id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            return None
        expires_at = int(row[3] or 0)
        rate = int(row[1] or 0)
        if expires_at <= now or rate <= 0 or rate > MAX_EXP_BONUS_RATE_PPM:
            conn.execute(
                "DELETE FROM character_exp_bonus_effects WHERE character_id = ?",
                (cid,),
            )
            return None
        return ActiveExpBonusEffect(
            character_id=cid,
            source_item_id=int(row[0] or 0),
            bonus_rate_ppm=rate,
            activated_at=int(row[2] or 0),
            expires_at=expires_at,
            source_path=str(row[4] or ""),
        )


def build_exp_bonus_item_state_restore_packets(
    server: Any,
    repo: Any,
    character_id: int,
    *,
    now_unix: int | None = None,
) -> tuple[list[bytes], str]:
    """Restore one persisted EXP item's cooldown and effect with one time snapshot."""
    now = int(time.time()) if now_unix is None else max(0, int(now_unix))
    effect = get_active_exp_bonus_effect(
        repo,
        int(character_id),
        now_unix=now,
    )
    if effect is None:
        return [], "EXP item state restore inactive"

    remaining = min(0xFFFFFFFF, max(0, int(effect.expires_at) - now))
    item_id = int(effect.source_item_id)
    if remaining <= 0 or item_id <= 0 or item_id > 0xFFFFFFFF:
        return [], "EXP item state restore invalid active row"

    body = struct.pack("<BII", 1, item_id, remaining)
    packets = [
        server._codec_response(
            0,
            EXP_BONUS_COOLTIME_ITEM_INFO_OPCODE,
            body,
        ),
        server._codec_response(
            0,
            EXP_BONUS_EFFECT_ITEM_INFO_OPCODE,
            body,
        ),
    ]
    return packets, (
        "S0/00AC->S0/00AE EXP item state restore "
        "item=%d remaining_seconds=%d" % (item_id, remaining)
    )


def clear_exp_bonus_effect(repo: Any, character_id: int) -> None:
    with repo.open() as conn:
        _ensure_effect_schema(conn)
        conn.execute(
            "DELETE FROM character_exp_bonus_effects WHERE character_id = ?",
            (int(character_id),),
        )


__all__ = [
    "ActiveExpBonusEffect",
    "EXP_BONUS_CACHE_DOMAIN",
    "EXP_BONUS_CACHE_VERSION",
    "EXP_BONUS_COOLTIME_ITEM_INFO_OPCODE",
    "EXP_BONUS_EFFECT_ITEM_INFO_OPCODE",
    "EXP_BONUS_RATE_SCALE",
    "ExpBonusConsumableIndex",
    "ExpBonusConsumableRule",
    "activate_exp_bonus_consumable_at",
    "build_exp_bonus_item_state_restore_packets",
    "build_exp_bonus_consumable_index",
    "build_exp_bonus_consumable_index_from_reader",
    "clear_exp_bonus_consumable_index",
    "clear_exp_bonus_effect",
    "get_active_exp_bonus_effect",
    "get_exp_bonus_consumable_index",
    "install_exp_bonus_consumable_index",
    "parse_exp_bonus_consumable_rule",
    "resolve_exp_bonus_consumable",
    "warmup_exp_bonus_consumable_index",
]
