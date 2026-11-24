"""PVF-backed reinforcement and amplification ticket rules."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace
from typing import Any, Iterable

from game.character.item_metadata import job_label_for_id
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
UPGRADE_TICKET_CACHE_DOMAIN = "upgrade_tickets"
UPGRADE_TICKET_CACHE_VERSION = 6
UPGRADE_WEIGHT_SCALE = 100000

MODE_REINFORCE = 0
MODE_AMPLIFY = 1
APPLY_MODE_FIXED = "fixed"
APPLY_MODE_ADDITIONAL = "additional"
WEAPON_FORGING_TICKET_TAG = "equipment separate reinforcement ticket"

PROTECTION_ACTION_REINFORCE = "[protect equipment]"
PROTECTION_ACTION_REINFORCE_NEW = "[new protect equipment]"
PROTECTION_ACTION_AMPLIFY = "[amplify protect equipment]"
PROTECTION_ACTION_AMPLIFY_NEW = "[new amplify protect equipment]"
PORTABLE_AMPLIFY_ACTION = "[portable amplify]"
PORTABLE_AMPLIFY_ACTION_NEW = "[new portable amplify]"
PORTABLE_REINFORCE_ACTION = "[portable upgrade]"
PORTABLE_REINFORCE_ACTION_NEW = "[new portable upgrade]"

_TICKET_TAG_BY_MODE = {
    MODE_REINFORCE: "equipment reinforcement ticket",
    MODE_AMPLIFY: "equipment amplify reinforcement ticket",
}
_PROTECTION_MODE_BY_ACTION = {
    PROTECTION_ACTION_REINFORCE: MODE_REINFORCE,
    PROTECTION_ACTION_REINFORCE_NEW: MODE_REINFORCE,
    PROTECTION_ACTION_AMPLIFY: MODE_AMPLIFY,
    PROTECTION_ACTION_AMPLIFY_NEW: MODE_AMPLIFY,
}
_PORTABLE_MODE_BY_ACTION = {
    PORTABLE_REINFORCE_ACTION: MODE_REINFORCE,
    PORTABLE_REINFORCE_ACTION_NEW: MODE_REINFORCE,
    PORTABLE_AMPLIFY_ACTION: MODE_AMPLIFY,
    PORTABLE_AMPLIFY_ACTION_NEW: MODE_AMPLIFY,
}
_LST_RE = re.compile(r"(?m)^(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_LOCK = threading.RLock()
_INDEX: "UpgradeTicketIndex | None" = None


def _section(text: str, name: str) -> str:
    match = re.search(
        r"\[" + re.escape(name) + r"\]\s*(.*?)(?=\r?\n\s*\[[^\]]+\]|\Z)",
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match is not None else ""


def _integers(text: str) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"-?\d+", str(text or "")))


def _optional_integer_section(
    text: str,
    name: str,
    *,
    item_id: int,
) -> int | None:
    body = _section(text, name)
    if not body:
        return None
    values = _integers(body)
    if len(values) != 1:
        raise ValueError(
            "upgrade consumable [%s] requires one integer item=%d"
            % (name, int(item_id))
        )
    return int(values[0])


def _section_tokens(text: str, name: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in re.findall(r"`([^`]*)`", _section(text, name)):
        token = " ".join(str(value or "").strip().lower().split())
        if token.startswith("[") and token.endswith("]") and len(token) >= 2:
            token = " ".join(token[1:-1].strip().split())
        if token and token not in normalized:
            normalized.append(token)
    return tuple(normalized)


def _normalize_eligibility_tokens(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        token = " ".join(str(value or "").strip().lower().strip("[]").split())
        if token and token not in normalized:
            normalized.append(token)
    return tuple(normalized)


def _validate_eligibility(
    *,
    item_id: int,
    minimum_level: int | None,
    maximum_level: int | None,
) -> tuple[int | None, int | None]:
    minimum = None if minimum_level is None else int(minimum_level)
    maximum = None if maximum_level is None else int(maximum_level)
    if minimum is not None and minimum < 0:
        raise ValueError(
            "invalid upgrade consumable minimum character level item=%d"
            % int(item_id)
        )
    if maximum is not None and maximum < 0:
        raise ValueError(
            "invalid upgrade consumable maximum character level item=%d"
            % int(item_id)
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            "reversed upgrade consumable character level range item=%d"
            % int(item_id)
        )
    return minimum, maximum


def _eligibility_result(
    *,
    level: int,
    job: int,
    hardcore: bool,
    minimum_level: int | None,
    maximum_level: int | None,
    usable_jobs: tuple[str, ...],
    impossible_contents: tuple[str, ...],
) -> tuple[bool, str]:
    current_level = max(1, int(level))
    if minimum_level is not None and current_level < int(minimum_level):
        return False, "below_minimum_level"
    if maximum_level is not None and current_level > int(maximum_level):
        return False, "above_maximum_level"
    if bool(hardcore) and "hardcore mode" in impossible_contents:
        return False, "hardcore_mode"
    allowed = set(usable_jobs)
    if allowed and "all" not in allowed:
        job_label = job_label_for_id(int(job))
        if not job_label or job_label not in allowed:
            return False, "job_restricted"
    return True, "ok"


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


@dataclass(frozen=True)
class UpgradeTicketRule:
    item_id: int
    mode: int
    target_level: int
    success_rate_percent: int
    apply_mode: str
    apply_value: int
    minimum_equipment_level: int = -1
    maximum_equipment_level: int = -1
    allowed_item_ids: tuple[int, ...] = ()
    minimum_character_level: int | None = None
    maximum_character_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()
    source_path: str = ""

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        mode = int(self.mode)
        target_level = int(self.target_level)
        success_rate = int(self.success_rate_percent)
        apply_mode = str(self.apply_mode or "").strip().lower()
        apply_value = int(self.apply_value)
        minimum_level = int(self.minimum_equipment_level)
        maximum_level = int(self.maximum_equipment_level)
        allowed = tuple(int(value) for value in self.allowed_item_ids)
        minimum_character, maximum_character = _validate_eligibility(
            item_id=item_id,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
        )
        usable_jobs = _normalize_eligibility_tokens(self.usable_jobs)
        impossible_contents = _normalize_eligibility_tokens(
            self.impossible_contents
        )
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError("invalid upgrade ticket item id=%d" % item_id)
        if mode not in _TICKET_TAG_BY_MODE:
            raise ValueError("invalid upgrade ticket mode=%d item=%d" % (mode, item_id))
        if not 1 <= target_level <= 31:
            raise ValueError(
                "invalid upgrade ticket target level=%d item=%d"
                % (target_level, item_id)
            )
        if not 0 <= success_rate <= 100:
            raise ValueError(
                "invalid upgrade ticket success rate=%d item=%d"
                % (success_rate, item_id)
            )
        if apply_mode == APPLY_MODE_FIXED:
            if apply_value != -1:
                raise ValueError(
                    "fixed upgrade ticket apply value must be -1 item=%d" % item_id
                )
        elif apply_mode == APPLY_MODE_ADDITIONAL:
            if not target_level <= apply_value <= 31:
                raise ValueError(
                    "invalid additional upgrade ticket cap=%d item=%d"
                    % (apply_value, item_id)
                )
        else:
            raise ValueError(
                "unsupported upgrade ticket apply mode=%r item=%d"
                % (apply_mode, item_id)
            )
        if (minimum_level < 0) != (maximum_level < 0):
            raise ValueError("incomplete upgrade ticket equipment level range item=%d" % item_id)
        if minimum_level >= 0 and maximum_level < minimum_level:
            raise ValueError("reversed upgrade ticket equipment level range item=%d" % item_id)
        if any(value <= 0 or value > 0xFFFFFFFF for value in allowed):
            raise ValueError("invalid upgrade ticket allowed item id item=%d" % item_id)
        if len(set(allowed)) != len(allowed):
            raise ValueError("duplicate upgrade ticket allowed item id item=%d" % item_id)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "target_level", target_level)
        object.__setattr__(self, "success_rate_percent", success_rate)
        object.__setattr__(self, "apply_mode", apply_mode)
        object.__setattr__(self, "apply_value", apply_value)
        object.__setattr__(self, "minimum_equipment_level", minimum_level)
        object.__setattr__(self, "maximum_equipment_level", maximum_level)
        object.__setattr__(self, "allowed_item_ids", allowed)
        object.__setattr__(self, "minimum_character_level", minimum_character)
        object.__setattr__(self, "maximum_character_level", maximum_character)
        object.__setattr__(self, "usable_jobs", usable_jobs)
        object.__setattr__(self, "impossible_contents", impossible_contents)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    @property
    def success_weight(self) -> int:
        return int(self.success_rate_percent) * (UPGRADE_WEIGHT_SCALE // 100)

    def allows_target(self, *, item_id: int, equipment_level: int) -> bool:
        target_item_id = int(item_id) & 0xFFFFFFFF
        target_level = max(0, int(equipment_level))
        if self.allowed_item_ids and target_item_id not in self.allowed_item_ids:
            return False
        if (
            self.minimum_equipment_level >= 0
            and target_level < self.minimum_equipment_level
        ):
            return False
        if (
            self.maximum_equipment_level >= 0
            and target_level > self.maximum_equipment_level
        ):
            return False
        return True

    def success_level(self, current_level: int) -> int | None:
        current = max(0, min(31, int(current_level)))
        if self.apply_mode == APPLY_MODE_FIXED:
            if current >= self.target_level:
                return None
            return int(self.target_level)
        if current >= self.apply_value:
            return None
        return min(int(self.apply_value), current + int(self.target_level))

    def succeeds_for_roll(self, roll: int) -> bool:
        value = int(roll)
        if not 0 <= value < UPGRADE_WEIGHT_SCALE:
            raise ValueError("upgrade ticket roll outside weight scale")
        return value < self.success_weight

    def is_usable(
        self,
        *,
        level: int,
        job: int,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        return _eligibility_result(
            level=level,
            job=job,
            hardcore=hardcore,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
            usable_jobs=self.usable_jobs,
            impossible_contents=self.impossible_contents,
        )

    def to_cache_row(self) -> list[Any]:
        return [
            self.item_id,
            self.mode,
            self.target_level,
            self.success_rate_percent,
            self.apply_mode,
            self.apply_value,
            self.minimum_equipment_level,
            self.maximum_equipment_level,
            list(self.allowed_item_ids),
            self.minimum_character_level,
            self.maximum_character_level,
            list(self.usable_jobs),
            list(self.impossible_contents),
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "UpgradeTicketRule":
        if (
            not isinstance(row, list)
            or len(row) != 14
            or not isinstance(row[8], list)
            or not isinstance(row[11], list)
            or not isinstance(row[12], list)
        ):
            raise ValueError("invalid upgrade ticket cache row")
        return cls(
            item_id=int(row[0]),
            mode=int(row[1]),
            target_level=int(row[2]),
            success_rate_percent=int(row[3]),
            apply_mode=str(row[4]),
            apply_value=int(row[5]),
            minimum_equipment_level=int(row[6]),
            maximum_equipment_level=int(row[7]),
            allowed_item_ids=tuple(int(value) for value in row[8]),
            minimum_character_level=None if row[9] is None else int(row[9]),
            maximum_character_level=None if row[10] is None else int(row[10]),
            usable_jobs=tuple(str(value) for value in row[11]),
            impossible_contents=tuple(str(value) for value in row[12]),
            source_path=str(row[13]),
        )


@dataclass(frozen=True)
class WeaponForgingTicketRule:
    item_id: int
    target_level: int
    success_rate_percent: int
    apply_mode: str
    apply_value: int
    minimum_equipment_level: int = -1
    maximum_equipment_level: int = -1
    allowed_item_ids: tuple[int, ...] = ()
    minimum_character_level: int | None = None
    maximum_character_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()
    source_path: str = ""

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        target_level = int(self.target_level)
        success_rate = int(self.success_rate_percent)
        apply_mode = str(self.apply_mode or "").strip().lower()
        apply_value = int(self.apply_value)
        minimum_level = int(self.minimum_equipment_level)
        maximum_level = int(self.maximum_equipment_level)
        allowed = tuple(int(value) for value in self.allowed_item_ids)
        minimum_character, maximum_character = _validate_eligibility(
            item_id=item_id,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
        )
        usable_jobs = _normalize_eligibility_tokens(self.usable_jobs)
        impossible_contents = _normalize_eligibility_tokens(
            self.impossible_contents
        )
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError("invalid weapon forging ticket item id=%d" % item_id)
        if not 1 <= target_level <= 31:
            raise ValueError(
                "invalid weapon forging ticket target level=%d item=%d"
                % (target_level, item_id)
            )
        if not 0 <= success_rate <= 100:
            raise ValueError(
                "invalid weapon forging ticket success rate=%d item=%d"
                % (success_rate, item_id)
            )
        if apply_mode == APPLY_MODE_FIXED:
            if apply_value != -1:
                raise ValueError(
                    "fixed weapon forging ticket apply value must be -1 item=%d"
                    % item_id
                )
        elif apply_mode == APPLY_MODE_ADDITIONAL:
            if not target_level <= apply_value <= 31:
                raise ValueError(
                    "invalid additional weapon forging ticket cap=%d item=%d"
                    % (apply_value, item_id)
                )
        else:
            raise ValueError(
                "unsupported weapon forging ticket apply mode=%r item=%d"
                % (apply_mode, item_id)
            )
        if (minimum_level < 0) != (maximum_level < 0):
            raise ValueError(
                "incomplete weapon forging ticket equipment level range item=%d"
                % item_id
            )
        if minimum_level >= 0 and maximum_level < minimum_level:
            raise ValueError(
                "reversed weapon forging ticket equipment level range item=%d"
                % item_id
            )
        if any(value <= 0 or value > 0xFFFFFFFF for value in allowed):
            raise ValueError(
                "invalid weapon forging ticket allowed item id item=%d" % item_id
            )
        if len(set(allowed)) != len(allowed):
            raise ValueError(
                "duplicate weapon forging ticket allowed item id item=%d" % item_id
            )
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "target_level", target_level)
        object.__setattr__(self, "success_rate_percent", success_rate)
        object.__setattr__(self, "apply_mode", apply_mode)
        object.__setattr__(self, "apply_value", apply_value)
        object.__setattr__(self, "minimum_equipment_level", minimum_level)
        object.__setattr__(self, "maximum_equipment_level", maximum_level)
        object.__setattr__(self, "allowed_item_ids", allowed)
        object.__setattr__(self, "minimum_character_level", minimum_character)
        object.__setattr__(self, "maximum_character_level", maximum_character)
        object.__setattr__(self, "usable_jobs", usable_jobs)
        object.__setattr__(self, "impossible_contents", impossible_contents)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    @property
    def success_weight(self) -> int:
        return int(self.success_rate_percent) * (UPGRADE_WEIGHT_SCALE // 100)

    def allows_target(self, *, item_id: int, equipment_level: int) -> bool:
        target_item_id = int(item_id) & 0xFFFFFFFF
        target_level = max(0, int(equipment_level))
        if self.allowed_item_ids and target_item_id not in self.allowed_item_ids:
            return False
        if (
            self.minimum_equipment_level >= 0
            and target_level < self.minimum_equipment_level
        ):
            return False
        if (
            self.maximum_equipment_level >= 0
            and target_level > self.maximum_equipment_level
        ):
            return False
        return True

    def success_level(self, current_level: int) -> int | None:
        current = max(0, min(31, int(current_level)))
        if self.apply_mode == APPLY_MODE_FIXED:
            if current >= self.target_level:
                return None
            return int(self.target_level)
        if current >= self.apply_value:
            return None
        return min(int(self.apply_value), current + int(self.target_level))

    def succeeds_for_roll(self, roll: int) -> bool:
        value = int(roll)
        if not 0 <= value < UPGRADE_WEIGHT_SCALE:
            raise ValueError("weapon forging ticket roll outside weight scale")
        return value < self.success_weight

    def is_usable(
        self,
        *,
        level: int,
        job: int,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        return _eligibility_result(
            level=level,
            job=job,
            hardcore=hardcore,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
            usable_jobs=self.usable_jobs,
            impossible_contents=self.impossible_contents,
        )

    def to_cache_row(self) -> list[Any]:
        return [
            self.item_id,
            self.target_level,
            self.success_rate_percent,
            self.apply_mode,
            self.apply_value,
            self.minimum_equipment_level,
            self.maximum_equipment_level,
            list(self.allowed_item_ids),
            self.minimum_character_level,
            self.maximum_character_level,
            list(self.usable_jobs),
            list(self.impossible_contents),
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "WeaponForgingTicketRule":
        if (
            not isinstance(row, list)
            or len(row) != 13
            or not isinstance(row[7], list)
            or not isinstance(row[10], list)
            or not isinstance(row[11], list)
        ):
            raise ValueError("invalid weapon forging ticket cache row")
        return cls(
            item_id=int(row[0]),
            target_level=int(row[1]),
            success_rate_percent=int(row[2]),
            apply_mode=str(row[3]),
            apply_value=int(row[4]),
            minimum_equipment_level=int(row[5]),
            maximum_equipment_level=int(row[6]),
            allowed_item_ids=tuple(int(value) for value in row[7]),
            minimum_character_level=None if row[8] is None else int(row[8]),
            maximum_character_level=None if row[9] is None else int(row[9]),
            usable_jobs=tuple(str(value) for value in row[10]),
            impossible_contents=tuple(str(value) for value in row[11]),
            source_path=str(row[12]),
        )


@dataclass(frozen=True)
class UpgradeProtectionTicketRule:
    item_id: int
    mode: int
    action_type: str
    failure_level: int = 0
    minimum_character_level: int | None = None
    maximum_character_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()
    source_path: str = ""

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        mode = int(self.mode)
        action_type = " ".join(
            str(self.action_type or "").strip().lower().split()
        )
        failure_level = int(self.failure_level)
        minimum_character, maximum_character = _validate_eligibility(
            item_id=item_id,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
        )
        usable_jobs = _normalize_eligibility_tokens(self.usable_jobs)
        impossible_contents = _normalize_eligibility_tokens(
            self.impossible_contents
        )
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError("invalid upgrade protection ticket item id=%d" % item_id)
        if action_type not in _PROTECTION_MODE_BY_ACTION:
            raise ValueError(
                "unsupported upgrade protection action=%r item=%d"
                % (action_type, item_id)
            )
        if mode != _PROTECTION_MODE_BY_ACTION[action_type]:
            raise ValueError(
                "upgrade protection mode mismatch item=%d action=%s"
                % (item_id, action_type)
            )
        if not 0 <= failure_level <= 31:
            raise ValueError(
                "invalid upgrade protection failure level=%d item=%d"
                % (failure_level, item_id)
            )
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "failure_level", failure_level)
        object.__setattr__(self, "minimum_character_level", minimum_character)
        object.__setattr__(self, "maximum_character_level", maximum_character)
        object.__setattr__(self, "usable_jobs", usable_jobs)
        object.__setattr__(self, "impossible_contents", impossible_contents)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    def is_usable(
        self,
        *,
        level: int,
        job: int,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        return _eligibility_result(
            level=level,
            job=job,
            hardcore=hardcore,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
            usable_jobs=self.usable_jobs,
            impossible_contents=self.impossible_contents,
        )

    def to_cache_row(self) -> list[Any]:
        return [
            self.item_id,
            self.mode,
            self.action_type,
            self.failure_level,
            self.minimum_character_level,
            self.maximum_character_level,
            list(self.usable_jobs),
            list(self.impossible_contents),
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "UpgradeProtectionTicketRule":
        if (
            not isinstance(row, list)
            or len(row) != 9
            or not isinstance(row[6], list)
            or not isinstance(row[7], list)
        ):
            raise ValueError("invalid upgrade protection cache row")
        return cls(
            item_id=int(row[0]),
            mode=int(row[1]),
            action_type=str(row[2]),
            failure_level=int(row[3]),
            minimum_character_level=None if row[4] is None else int(row[4]),
            maximum_character_level=None if row[5] is None else int(row[5]),
            usable_jobs=tuple(str(value) for value in row[6]),
            impossible_contents=tuple(str(value) for value in row[7]),
            source_path=str(row[8]),
        )


@dataclass(frozen=True)
class PortableUpgradeRule:
    item_id: int
    action_type: str
    minimum_current_level: int
    maximum_current_level: int
    success_bonus_weight: int = 0
    minimum_equipment_level: int = -1
    maximum_equipment_level: int = -1
    allowed_item_ids: tuple[int, ...] = ()
    minimum_character_level: int | None = None
    maximum_character_level: int | None = None
    usable_jobs: tuple[str, ...] = ()
    impossible_contents: tuple[str, ...] = ()
    source_path: str = ""

    def __post_init__(self) -> None:
        item_id = int(self.item_id)
        action_type = " ".join(
            str(self.action_type or "").strip().lower().split()
        )
        minimum_current = int(self.minimum_current_level)
        maximum_current = int(self.maximum_current_level)
        bonus_weight = int(self.success_bonus_weight)
        minimum_equipment = int(self.minimum_equipment_level)
        maximum_equipment = int(self.maximum_equipment_level)
        allowed = tuple(int(value) for value in self.allowed_item_ids)
        minimum_character, maximum_character = _validate_eligibility(
            item_id=item_id,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
        )
        usable_jobs = _normalize_eligibility_tokens(self.usable_jobs)
        impossible_contents = _normalize_eligibility_tokens(
            self.impossible_contents
        )
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError("invalid portable upgrade item id=%d" % item_id)
        if action_type not in _PORTABLE_MODE_BY_ACTION:
            raise ValueError(
                "unsupported portable upgrade action=%r item=%d"
                % (action_type, item_id)
            )
        if minimum_current < 0 or maximum_current < minimum_current:
            raise ValueError(
                "invalid portable upgrade current level range item=%d" % item_id
            )
        if not 0 <= bonus_weight <= UPGRADE_WEIGHT_SCALE:
            raise ValueError(
                "invalid portable upgrade success bonus item=%d" % item_id
            )
        if (minimum_equipment < 0) != (maximum_equipment < 0):
            raise ValueError(
                "incomplete portable upgrade equipment level range item=%d" % item_id
            )
        if minimum_equipment >= 0 and maximum_equipment < minimum_equipment:
            raise ValueError(
                "reversed portable upgrade equipment level range item=%d" % item_id
            )
        if any(value <= 0 or value > 0xFFFFFFFF for value in allowed):
            raise ValueError("invalid portable upgrade allowed item id item=%d" % item_id)
        if len(set(allowed)) != len(allowed):
            raise ValueError("duplicate portable upgrade allowed item id item=%d" % item_id)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "minimum_current_level", minimum_current)
        object.__setattr__(self, "maximum_current_level", maximum_current)
        object.__setattr__(self, "success_bonus_weight", bonus_weight)
        object.__setattr__(self, "minimum_equipment_level", minimum_equipment)
        object.__setattr__(self, "maximum_equipment_level", maximum_equipment)
        object.__setattr__(self, "allowed_item_ids", allowed)
        object.__setattr__(self, "minimum_character_level", minimum_character)
        object.__setattr__(self, "maximum_character_level", maximum_character)
        object.__setattr__(self, "usable_jobs", usable_jobs)
        object.__setattr__(self, "impossible_contents", impossible_contents)
        object.__setattr__(self, "source_path", str(self.source_path or ""))

    @property
    def mode(self) -> int:
        return int(_PORTABLE_MODE_BY_ACTION[self.action_type])

    def allows_target(
        self,
        *,
        current_level: int,
        equipment_level: int,
        item_id: int = 0,
    ) -> bool:
        current = int(current_level)
        equipment = max(0, int(equipment_level))
        target_item_id = int(item_id) & 0xFFFFFFFF
        if self.allowed_item_ids and target_item_id not in self.allowed_item_ids:
            return False
        if not self.minimum_current_level <= current <= self.maximum_current_level:
            return False
        if (
            self.minimum_equipment_level >= 0
            and equipment < self.minimum_equipment_level
        ):
            return False
        if (
            self.maximum_equipment_level >= 0
            and equipment > self.maximum_equipment_level
        ):
            return False
        return True

    def is_usable(
        self,
        *,
        level: int,
        job: int,
        hardcore: bool = False,
    ) -> tuple[bool, str]:
        return _eligibility_result(
            level=level,
            job=job,
            hardcore=hardcore,
            minimum_level=self.minimum_character_level,
            maximum_level=self.maximum_character_level,
            usable_jobs=self.usable_jobs,
            impossible_contents=self.impossible_contents,
        )

    def adjusted_success_weight(self, base_weight: int) -> int:
        base = max(0, min(UPGRADE_WEIGHT_SCALE, int(base_weight)))
        adjusted = (
            base * (UPGRADE_WEIGHT_SCALE + self.success_bonus_weight)
            // UPGRADE_WEIGHT_SCALE
        )
        return max(0, min(UPGRADE_WEIGHT_SCALE, adjusted))

    def to_cache_row(self) -> list[Any]:
        return [
            self.item_id,
            self.action_type,
            self.minimum_current_level,
            self.maximum_current_level,
            self.success_bonus_weight,
            self.minimum_equipment_level,
            self.maximum_equipment_level,
            list(self.allowed_item_ids),
            self.minimum_character_level,
            self.maximum_character_level,
            list(self.usable_jobs),
            list(self.impossible_contents),
            self.source_path,
        ]

    @classmethod
    def from_cache_row(cls, row: Any) -> "PortableUpgradeRule":
        if (
            not isinstance(row, list)
            or len(row) != 13
            or not isinstance(row[7], list)
            or not isinstance(row[10], list)
            or not isinstance(row[11], list)
        ):
            raise ValueError("invalid portable upgrade cache row")
        return cls(
            item_id=int(row[0]),
            action_type=str(row[1]),
            minimum_current_level=int(row[2]),
            maximum_current_level=int(row[3]),
            success_bonus_weight=int(row[4]),
            minimum_equipment_level=int(row[5]),
            maximum_equipment_level=int(row[6]),
            allowed_item_ids=tuple(int(value) for value in row[7]),
            minimum_character_level=None if row[8] is None else int(row[8]),
            maximum_character_level=None if row[9] is None else int(row[9]),
            usable_jobs=tuple(str(value) for value in row[10]),
            impossible_contents=tuple(str(value) for value in row[11]),
            source_path=str(row[12]),
        )


# Compatibility name retained for callers that only handle amplification actions.
PortableAmplifyRule = PortableUpgradeRule


def parse_upgrade_ticket_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> UpgradeTicketRule | None:
    matched: list[tuple[int, str]] = []
    for mode, tag in _TICKET_TAG_BY_MODE.items():
        body = _section(text, tag)
        if body:
            matched.append((mode, body))
    if not matched:
        return None
    if len(matched) != 1:
        raise ValueError("upgrade ticket declares both modes item=%d" % int(item_id))
    mode, body = matched[0]
    values = _integers(body)
    mode_tokens = re.findall(r"`([^`]*)`", body)
    if len(values) != 3 or len(mode_tokens) != 1:
        raise ValueError(
            "upgrade ticket requires target, rate, apply mode and value item=%d"
            % int(item_id)
        )
    equipment_level_values = _integers(_section(text, "check usable itemlevel"))
    if equipment_level_values and len(equipment_level_values) != 2:
        raise ValueError(
            "upgrade ticket equipment level range requires two values item=%d"
            % int(item_id)
        )
    minimum_level, maximum_level = (-1, -1)
    if equipment_level_values:
        minimum_level, maximum_level = equipment_level_values
    allowed_item_ids = tuple(
        sorted(set(_integers(_section(text, "reinforcement usable item list"))))
    )
    return UpgradeTicketRule(
        item_id=int(item_id),
        mode=mode,
        target_level=values[0],
        success_rate_percent=values[1],
        apply_mode=mode_tokens[0],
        apply_value=values[2],
        minimum_equipment_level=minimum_level,
        maximum_equipment_level=maximum_level,
        allowed_item_ids=allowed_item_ids,
        minimum_character_level=_optional_integer_section(
            text,
            "minimum level",
            item_id=item_id,
        ),
        maximum_character_level=_optional_integer_section(
            text,
            "maximum level",
            item_id=item_id,
        ),
        usable_jobs=_section_tokens(text, "usable job"),
        impossible_contents=_section_tokens(text, "impossible contents"),
        source_path=source_path,
    )


def parse_weapon_forging_ticket_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> WeaponForgingTicketRule | None:
    body = _section(text, WEAPON_FORGING_TICKET_TAG)
    if not body:
        return None
    values = _integers(body)
    mode_tokens = re.findall(r"`([^`]*)`", body)
    if len(values) != 3 or len(mode_tokens) != 1:
        raise ValueError(
            "weapon forging ticket requires target, rate, apply mode and value item=%d"
            % int(item_id)
        )
    equipment_level_values = _integers(_section(text, "check usable itemlevel"))
    if equipment_level_values and len(equipment_level_values) != 2:
        raise ValueError(
            "weapon forging ticket equipment level range requires two values item=%d"
            % int(item_id)
        )
    minimum_level, maximum_level = (-1, -1)
    if equipment_level_values:
        minimum_level, maximum_level = equipment_level_values
    return WeaponForgingTicketRule(
        item_id=int(item_id),
        target_level=values[0],
        success_rate_percent=values[1],
        apply_mode=mode_tokens[0],
        apply_value=values[2],
        minimum_equipment_level=minimum_level,
        maximum_equipment_level=maximum_level,
        allowed_item_ids=tuple(
            sorted(
                set(
                    _integers(
                        _section(text, "reinforcement usable item list")
                    )
                )
            )
        ),
        minimum_character_level=_optional_integer_section(
            text,
            "minimum level",
            item_id=item_id,
        ),
        maximum_character_level=_optional_integer_section(
            text,
            "maximum level",
            item_id=item_id,
        ),
        usable_jobs=_section_tokens(text, "usable job"),
        impossible_contents=_section_tokens(text, "impossible contents"),
        source_path=source_path,
    )


def parse_upgrade_protection_ticket_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> UpgradeProtectionTicketRule | None:
    body = _section(text, "action type")
    if not body:
        return None
    actions = tuple(
        " ".join(value.strip().lower().split())
        for value in re.findall(r"`([^`]*)`", body)
    )
    matched = tuple(action for action in actions if action in _PROTECTION_MODE_BY_ACTION)
    if not matched:
        return None
    if len(matched) != 1:
        raise ValueError(
            "upgrade protection ticket declares multiple actions item=%d"
            % int(item_id)
        )
    parameters = _integers(body)
    if len(parameters) > 1:
        raise ValueError(
            "upgrade protection ticket has unsupported parameters item=%d"
            % int(item_id)
        )
    action_type = matched[0]
    return UpgradeProtectionTicketRule(
        item_id=int(item_id),
        mode=_PROTECTION_MODE_BY_ACTION[action_type],
        action_type=action_type,
        failure_level=parameters[0] if parameters else 0,
        minimum_character_level=_optional_integer_section(
            text,
            "minimum level",
            item_id=item_id,
        ),
        maximum_character_level=_optional_integer_section(
            text,
            "maximum level",
            item_id=item_id,
        ),
        usable_jobs=_section_tokens(text, "usable job"),
        impossible_contents=_section_tokens(text, "impossible contents"),
        source_path=source_path,
    )


def parse_portable_upgrade_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> PortableUpgradeRule | None:
    body = _section(text, "action type")
    if not body:
        return None
    actions = tuple(
        " ".join(value.strip().lower().split())
        for value in re.findall(r"`([^`]*)`", body)
    )
    matched = tuple(action for action in actions if action in _PORTABLE_MODE_BY_ACTION)
    if not matched:
        return None
    if len(actions) != 1 or len(matched) != 1:
        raise ValueError(
            "portable upgrade declares multiple actions item=%d" % int(item_id)
        )
    parameters = _integers(body)
    if len(parameters) not in (2, 3):
        raise ValueError(
            "portable upgrade requires current level min/max and optional bonus item=%d"
            % int(item_id)
        )
    equipment_levels = _integers(_section(text, "check usable itemlevel"))
    if equipment_levels and len(equipment_levels) != 2:
        raise ValueError(
            "portable upgrade equipment level range requires two values item=%d"
            % int(item_id)
        )
    minimum_equipment, maximum_equipment = (-1, -1)
    if equipment_levels:
        minimum_equipment, maximum_equipment = equipment_levels
    return PortableUpgradeRule(
        item_id=int(item_id),
        action_type=matched[0],
        minimum_current_level=parameters[0],
        maximum_current_level=parameters[1],
        success_bonus_weight=parameters[2] if len(parameters) == 3 else 0,
        minimum_equipment_level=minimum_equipment,
        maximum_equipment_level=maximum_equipment,
        allowed_item_ids=tuple(
            sorted(
                set(
                    _integers(
                        _section(text, "reinforcement usable item list")
                    )
                )
            )
        ),
        minimum_character_level=_optional_integer_section(
            text,
            "minimum level",
            item_id=item_id,
        ),
        maximum_character_level=_optional_integer_section(
            text,
            "maximum level",
            item_id=item_id,
        ),
        usable_jobs=_section_tokens(text, "usable job"),
        impossible_contents=_section_tokens(text, "impossible contents"),
        source_path=source_path,
    )


def parse_portable_amplify_rule(
    item_id: int,
    text: str,
    *,
    source_path: str = "",
) -> PortableAmplifyRule | None:
    """Parse only amplification actions for backward-compatible callers."""
    rule = parse_portable_upgrade_rule(
        item_id,
        text,
        source_path=source_path,
    )
    if rule is None or rule.mode != MODE_AMPLIFY:
        return None
    return rule


@dataclass(frozen=True)
class UpgradeTicketIndex:
    rules_by_item_id: dict[int, UpgradeTicketRule]
    weapon_forging_rules_by_item_id: dict[int, WeaponForgingTicketRule]
    protection_rules_by_item_id: dict[int, UpgradeProtectionTicketRule]
    portable_reinforce_rules_by_item_id: dict[int, PortableUpgradeRule]
    portable_amplify_rules_by_item_id: dict[int, PortableAmplifyRule]
    scanned_stackable_count: int
    cache_status: str = ""

    def rule_for(self, item_id: int) -> UpgradeTicketRule | None:
        return self.rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    def weapon_forging_rule_for(
        self,
        item_id: int,
    ) -> WeaponForgingTicketRule | None:
        return self.weapon_forging_rules_by_item_id.get(
            int(item_id) & 0xFFFFFFFF
        )

    def protection_rule_for(
        self,
        item_id: int,
    ) -> UpgradeProtectionTicketRule | None:
        return self.protection_rules_by_item_id.get(int(item_id) & 0xFFFFFFFF)

    def portable_amplify_rule_for(self, item_id: int) -> PortableAmplifyRule | None:
        return self.portable_amplify_rules_by_item_id.get(
            int(item_id) & 0xFFFFFFFF
        )

    def portable_upgrade_rule_for(self, item_id: int) -> PortableUpgradeRule | None:
        normalized = int(item_id) & 0xFFFFFFFF
        return self.portable_reinforce_rules_by_item_id.get(
            normalized
        ) or self.portable_amplify_rules_by_item_id.get(normalized)

    @property
    def ticket_count(self) -> int:
        return len(self.rules_by_item_id)

    @property
    def weapon_forging_ticket_count(self) -> int:
        return len(self.weapon_forging_rules_by_item_id)

    @property
    def protection_ticket_count(self) -> int:
        return len(self.protection_rules_by_item_id)

    @property
    def portable_amplifier_count(self) -> int:
        return len(self.portable_amplify_rules_by_item_id)

    @property
    def portable_reinforcer_count(self) -> int:
        return len(self.portable_reinforce_rules_by_item_id)

    def mode_count(self, mode: int) -> int:
        return sum(1 for rule in self.rules_by_item_id.values() if rule.mode == int(mode))

    def protection_mode_count(self, mode: int) -> int:
        return sum(
            1
            for rule in self.protection_rules_by_item_id.values()
            if rule.mode == int(mode)
        )

    def to_cache_payload(self) -> dict[str, Any]:
        return {
            "scanned_stackable_count": int(self.scanned_stackable_count),
            "rules": [
                rule.to_cache_row()
                for _, rule in sorted(self.rules_by_item_id.items())
            ],
            "weapon_forging_rules": [
                rule.to_cache_row()
                for _, rule in sorted(
                    self.weapon_forging_rules_by_item_id.items()
                )
            ],
            "protection_rules": [
                rule.to_cache_row()
                for _, rule in sorted(self.protection_rules_by_item_id.items())
            ],
            "portable_reinforce_rules": [
                rule.to_cache_row()
                for _, rule in sorted(self.portable_reinforce_rules_by_item_id.items())
            ],
            "portable_amplify_rules": [
                rule.to_cache_row()
                for _, rule in sorted(self.portable_amplify_rules_by_item_id.items())
            ],
        }

    @classmethod
    def from_cache_payload(cls, payload: Any) -> "UpgradeTicketIndex":
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("rules"), list)
            or not isinstance(payload.get("weapon_forging_rules"), list)
            or not isinstance(payload.get("protection_rules"), list)
            or not isinstance(payload.get("portable_reinforce_rules"), list)
            or not isinstance(payload.get("portable_amplify_rules"), list)
        ):
            raise ValueError("invalid upgrade ticket cache payload")
        scanned = int(payload.get("scanned_stackable_count") or 0)
        if scanned <= 0:
            raise ValueError("invalid cached upgrade ticket stackable count")
        return build_upgrade_ticket_index(
            (UpgradeTicketRule.from_cache_row(row) for row in payload["rules"]),
            weapon_forging_rules=(
                WeaponForgingTicketRule.from_cache_row(row)
                for row in payload["weapon_forging_rules"]
            ),
            protection_rules=(
                UpgradeProtectionTicketRule.from_cache_row(row)
                for row in payload["protection_rules"]
            ),
            portable_reinforce_rules=(
                PortableUpgradeRule.from_cache_row(row)
                for row in payload["portable_reinforce_rules"]
            ),
            portable_amplify_rules=(
                PortableAmplifyRule.from_cache_row(row)
                for row in payload["portable_amplify_rules"]
            ),
            scanned_stackable_count=scanned,
            cache_status="hit",
        )


def build_upgrade_ticket_index(
    rules: Iterable[UpgradeTicketRule],
    *,
    weapon_forging_rules: Iterable[WeaponForgingTicketRule] = (),
    protection_rules: Iterable[UpgradeProtectionTicketRule] = (),
    portable_reinforce_rules: Iterable[PortableUpgradeRule] = (),
    portable_amplify_rules: Iterable[PortableAmplifyRule] = (),
    scanned_stackable_count: int,
    cache_status: str = "",
) -> UpgradeTicketIndex:
    by_item_id: dict[int, UpgradeTicketRule] = {}
    for rule in rules:
        if not isinstance(rule, UpgradeTicketRule):
            raise TypeError("upgrade ticket index accepts UpgradeTicketRule only")
        if rule.item_id in by_item_id:
            raise ValueError("duplicate upgrade ticket item id=%d" % rule.item_id)
        by_item_id[rule.item_id] = rule
    weapon_forging_by_item_id: dict[int, WeaponForgingTicketRule] = {}
    for rule in weapon_forging_rules:
        if not isinstance(rule, WeaponForgingTicketRule):
            raise TypeError(
                "upgrade ticket index accepts WeaponForgingTicketRule only"
            )
        if rule.item_id in weapon_forging_by_item_id:
            raise ValueError(
                "duplicate weapon forging ticket item id=%d" % rule.item_id
            )
        if rule.item_id in by_item_id:
            raise ValueError(
                "item declares upgrade and weapon forging ticket rules item=%d"
                % rule.item_id
            )
        weapon_forging_by_item_id[rule.item_id] = rule
    protection_by_item_id: dict[int, UpgradeProtectionTicketRule] = {}
    for rule in protection_rules:
        if not isinstance(rule, UpgradeProtectionTicketRule):
            raise TypeError(
                "upgrade ticket index accepts UpgradeProtectionTicketRule only"
            )
        if rule.item_id in protection_by_item_id:
            raise ValueError(
                "duplicate upgrade protection ticket item id=%d" % rule.item_id
            )
        if rule.item_id in by_item_id or rule.item_id in weapon_forging_by_item_id:
            raise ValueError(
                "item declares upgrade, forging and protection ticket rules item=%d"
                % rule.item_id
            )
        protection_by_item_id[rule.item_id] = rule
    portable_reinforce_by_item_id: dict[int, PortableUpgradeRule] = {}
    portable_amplify_by_item_id: dict[int, PortableAmplifyRule] = {}
    occupied_ids = (
        set(by_item_id)
        | set(weapon_forging_by_item_id)
        | set(protection_by_item_id)
    )
    portable_groups = (
        (MODE_REINFORCE, portable_reinforce_rules, portable_reinforce_by_item_id),
        (MODE_AMPLIFY, portable_amplify_rules, portable_amplify_by_item_id),
    )
    for expected_mode, portable_rules, portable_by_item_id in portable_groups:
        for rule in portable_rules:
            if not isinstance(rule, PortableUpgradeRule):
                raise TypeError(
                    "upgrade ticket index accepts PortableUpgradeRule only"
                )
            if rule.mode != expected_mode:
                raise ValueError(
                    "portable upgrade mode mismatch item=%d" % rule.item_id
                )
            if rule.item_id in portable_by_item_id:
                raise ValueError(
                    "duplicate portable upgrade item id=%d" % rule.item_id
                )
            if rule.item_id in occupied_ids:
                raise ValueError(
                    "item declares multiple upgrade consumable rules item=%d"
                    % rule.item_id
                )
            portable_by_item_id[rule.item_id] = rule
            occupied_ids.add(rule.item_id)
    if not by_item_id:
        raise ValueError("PVF contains no reinforcement or amplification tickets")
    return UpgradeTicketIndex(
        rules_by_item_id=dict(sorted(by_item_id.items())),
        weapon_forging_rules_by_item_id=dict(
            sorted(weapon_forging_by_item_id.items())
        ),
        protection_rules_by_item_id=dict(sorted(protection_by_item_id.items())),
        portable_reinforce_rules_by_item_id=dict(
            sorted(portable_reinforce_by_item_id.items())
        ),
        portable_amplify_rules_by_item_id=dict(
            sorted(portable_amplify_by_item_id.items())
        ),
        scanned_stackable_count=max(0, int(scanned_stackable_count)),
        cache_status=str(cache_status or ""),
    )


def build_upgrade_ticket_index_from_reader(reader: Any) -> UpgradeTicketIndex:
    paths = _parse_stackable_paths(reader.read_text(STACKABLE_LIST_PATH))
    rules: list[UpgradeTicketRule] = []
    weapon_forging_rules: list[WeaponForgingTicketRule] = []
    protection_rules: list[UpgradeProtectionTicketRule] = []
    portable_reinforce_rules: list[PortableUpgradeRule] = []
    portable_amplify_rules: list[PortableAmplifyRule] = []
    for item_id, source_path in paths:
        text = reader.read_text(source_path)
        rule = parse_upgrade_ticket_rule(
            item_id,
            text,
            source_path=source_path,
        )
        if rule is not None:
            rules.append(rule)
        weapon_forging_rule = parse_weapon_forging_ticket_rule(
            item_id,
            text,
            source_path=source_path,
        )
        if weapon_forging_rule is not None:
            weapon_forging_rules.append(weapon_forging_rule)
        protection_rule = parse_upgrade_protection_ticket_rule(
            item_id,
            text,
            source_path=source_path,
        )
        if protection_rule is not None:
            protection_rules.append(protection_rule)
        portable_rule = parse_portable_upgrade_rule(
            item_id,
            text,
            source_path=source_path,
        )
        if portable_rule is not None:
            if portable_rule.mode == MODE_REINFORCE:
                portable_reinforce_rules.append(portable_rule)
            else:
                portable_amplify_rules.append(portable_rule)
    return build_upgrade_ticket_index(
        rules,
        weapon_forging_rules=weapon_forging_rules,
        protection_rules=protection_rules,
        portable_reinforce_rules=portable_reinforce_rules,
        portable_amplify_rules=portable_amplify_rules,
        scanned_stackable_count=len(paths),
    )


def install_upgrade_ticket_index(index: UpgradeTicketIndex) -> None:
    if not isinstance(index, UpgradeTicketIndex) or index.ticket_count <= 0:
        raise ValueError("invalid upgrade ticket index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_upgrade_ticket_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_upgrade_ticket_index() -> UpgradeTicketIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("upgrade ticket index is not warmed")
    return index


def find_upgrade_ticket_rule(item_id: int) -> UpgradeTicketRule | None:
    """Return an installed rule without performing a gameplay-time PVF read."""
    with _LOCK:
        index = _INDEX
    return index.rule_for(item_id) if index is not None else None


def find_weapon_forging_ticket_rule(
    item_id: int,
) -> WeaponForgingTicketRule | None:
    """Return a warmed forging-ticket identity without a gameplay PVF read."""
    with _LOCK:
        index = _INDEX
    return index.weapon_forging_rule_for(item_id) if index is not None else None


def find_upgrade_protection_ticket_rule(
    item_id: int,
) -> UpgradeProtectionTicketRule | None:
    """Return a warmed protection identity without a gameplay-time PVF read."""
    with _LOCK:
        index = _INDEX
    return index.protection_rule_for(item_id) if index is not None else None


def find_portable_amplify_rule(item_id: int) -> PortableAmplifyRule | None:
    """Return a warmed portable-amplifier rule without a gameplay-time PVF read."""
    with _LOCK:
        index = _INDEX
    return index.portable_amplify_rule_for(item_id) if index is not None else None


def find_portable_upgrade_rule(item_id: int) -> PortableUpgradeRule | None:
    """Return either warmed portable mode without a gameplay-time PVF read."""
    with _LOCK:
        index = _INDEX
    return index.portable_upgrade_rule_for(item_id) if index is not None else None


def warmup_upgrade_ticket_index(cache: Any = None) -> dict[str, Any]:
    index: UpgradeTicketIndex | None = None
    cache_status = "disabled"
    if cache is not None:
        payload = cache.read(
            UPGRADE_TICKET_CACHE_DOMAIN,
            UPGRADE_TICKET_CACHE_VERSION,
        )
        if payload is not None:
            try:
                index = UpgradeTicketIndex.from_cache_payload(payload)
                cache_status = "hit"
            except Exception:
                cache.discard(UPGRADE_TICKET_CACHE_DOMAIN)
    if index is None:
        index = build_upgrade_ticket_index_from_reader(
            pvf_runtime.ensure_shared_reader()
        )
        if cache is not None:
            cache.write(
                UPGRADE_TICKET_CACHE_DOMAIN,
                UPGRADE_TICKET_CACHE_VERSION,
                index.to_cache_payload(),
                record_count=(
                    index.ticket_count
                    + index.weapon_forging_ticket_count
                    + index.protection_ticket_count
                    + index.portable_reinforcer_count
                    + index.portable_amplifier_count
                ),
            )
            cache_status = (
                "disabled"
                if not bool(getattr(cache, "enabled", False))
                else str(cache.last_status.get(UPGRADE_TICKET_CACHE_DOMAIN, "miss"))
            )
    index = replace(index, cache_status=cache_status)
    install_upgrade_ticket_index(index)
    return {
        "ok": 1,
        "scanned_stackable_count": index.scanned_stackable_count,
        "ticket_count": index.ticket_count,
        "reinforcement_ticket_count": index.mode_count(MODE_REINFORCE),
        "amplification_ticket_count": index.mode_count(MODE_AMPLIFY),
        "weapon_forging_ticket_count": index.weapon_forging_ticket_count,
        "weapon_forging_probability_ticket_count": sum(
            1
            for rule in index.weapon_forging_rules_by_item_id.values()
            if rule.success_rate_percent < 100
        ),
        "weapon_forging_additional_ticket_count": sum(
            1
            for rule in index.weapon_forging_rules_by_item_id.values()
            if rule.apply_mode == APPLY_MODE_ADDITIONAL
        ),
        "probability_ticket_count": sum(
            1
            for rule in index.rules_by_item_id.values()
            if rule.success_rate_percent < 100
        ),
        "additional_ticket_count": sum(
            1
            for rule in index.rules_by_item_id.values()
            if rule.apply_mode == APPLY_MODE_ADDITIONAL
        ),
        "protection_ticket_count": index.protection_ticket_count,
        "reinforcement_protection_ticket_count": index.protection_mode_count(
            MODE_REINFORCE
        ),
        "amplification_protection_ticket_count": index.protection_mode_count(
            MODE_AMPLIFY
        ),
        "portable_reinforcer_count": index.portable_reinforcer_count,
        "portable_reinforcer_bonus_count": sum(
            1
            for rule in index.portable_reinforce_rules_by_item_id.values()
            if rule.success_bonus_weight > 0
        ),
        "portable_amplifier_count": index.portable_amplifier_count,
        "portable_amplifier_bonus_count": sum(
            1
            for rule in index.portable_amplify_rules_by_item_id.values()
            if rule.success_bonus_weight > 0
        ),
        "character_minimum_level_count": sum(
            rule.minimum_character_level is not None
            for rule in (
                tuple(index.rules_by_item_id.values())
                + tuple(index.weapon_forging_rules_by_item_id.values())
                + tuple(index.protection_rules_by_item_id.values())
                + tuple(index.portable_reinforce_rules_by_item_id.values())
                + tuple(index.portable_amplify_rules_by_item_id.values())
            )
        ),
        "character_maximum_level_count": sum(
            rule.maximum_character_level is not None
            for rule in (
                tuple(index.rules_by_item_id.values())
                + tuple(index.weapon_forging_rules_by_item_id.values())
                + tuple(index.protection_rules_by_item_id.values())
                + tuple(index.portable_reinforce_rules_by_item_id.values())
                + tuple(index.portable_amplify_rules_by_item_id.values())
            )
        ),
        "job_restricted_count": sum(
            bool(rule.usable_jobs) and "all" not in rule.usable_jobs
            for rule in (
                tuple(index.rules_by_item_id.values())
                + tuple(index.weapon_forging_rules_by_item_id.values())
                + tuple(index.protection_rules_by_item_id.values())
                + tuple(index.portable_reinforce_rules_by_item_id.values())
                + tuple(index.portable_amplify_rules_by_item_id.values())
            )
        ),
        "hardcore_blocked_count": sum(
            "hardcore mode" in rule.impossible_contents
            for rule in (
                tuple(index.rules_by_item_id.values())
                + tuple(index.weapon_forging_rules_by_item_id.values())
                + tuple(index.protection_rules_by_item_id.values())
                + tuple(index.portable_reinforce_rules_by_item_id.values())
                + tuple(index.portable_amplify_rules_by_item_id.values())
            )
        ),
        "cache": cache_status,
    }


__all__ = [
    "APPLY_MODE_ADDITIONAL",
    "APPLY_MODE_FIXED",
    "MODE_AMPLIFY",
    "MODE_REINFORCE",
    "PROTECTION_ACTION_AMPLIFY",
    "PROTECTION_ACTION_AMPLIFY_NEW",
    "PROTECTION_ACTION_REINFORCE",
    "PROTECTION_ACTION_REINFORCE_NEW",
    "PORTABLE_AMPLIFY_ACTION",
    "PORTABLE_AMPLIFY_ACTION_NEW",
    "PORTABLE_REINFORCE_ACTION",
    "PORTABLE_REINFORCE_ACTION_NEW",
    "UPGRADE_WEIGHT_SCALE",
    "WEAPON_FORGING_TICKET_TAG",
    "PortableAmplifyRule",
    "PortableUpgradeRule",
    "UpgradeProtectionTicketRule",
    "UpgradeTicketIndex",
    "UpgradeTicketRule",
    "WeaponForgingTicketRule",
    "build_upgrade_ticket_index",
    "build_upgrade_ticket_index_from_reader",
    "clear_upgrade_ticket_index",
    "find_portable_amplify_rule",
    "find_portable_upgrade_rule",
    "find_upgrade_protection_ticket_rule",
    "find_upgrade_ticket_rule",
    "find_weapon_forging_ticket_rule",
    "get_upgrade_ticket_index",
    "install_upgrade_ticket_index",
    "parse_portable_amplify_rule",
    "parse_portable_upgrade_rule",
    "parse_upgrade_ticket_rule",
    "parse_upgrade_protection_ticket_rule",
    "parse_weapon_forging_ticket_rule",
    "warmup_upgrade_ticket_index",
]
