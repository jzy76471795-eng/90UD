"""Persisted Stage3 customability state and 108-byte item projection."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Union


CUSTOMABILITY_EXTRA_KEY = "customability"
CUSTOMABILITY_OPTION_COUNT = 2
CUSTOMABILITY_GRADE_MIN = 0
CUSTOMABILITY_GRADE_MAX = 3
CUSTOMABILITY_ENTRY_OPTION_OFFSET = 103
CUSTOMABILITY_ENTRY_SET_RECORD_OFFSET = 106
CUSTOMABILITY_ENTRY_GRADE_OFFSET = 107
_SET_RECORD_UNCHANGED = object()


@dataclass(frozen=True)
class CustomAbilityState:
    individual_options: tuple[int, int]
    grade: int
    set_record_id: int = 0


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


def _option_pair(raw: Any) -> Optional[tuple[int, int]]:
    if not isinstance(raw, (list, tuple)) or len(raw) != CUSTOMABILITY_OPTION_COUNT:
        return None
    values: list[int] = []
    for value in raw:
        try:
            option_id = int(value)
        except (TypeError, ValueError):
            return None
        if not 1 <= option_id <= 0xFF:
            return None
        values.append(option_id)
    return values[0], values[1]


def parse_customability_state(raw: Any) -> Optional[CustomAbilityState]:
    data = _extra_json_dict(raw)
    state = data.get(CUSTOMABILITY_EXTRA_KEY)
    if not isinstance(state, dict):
        return None
    options = _option_pair(state.get("individual_options"))
    if options is None:
        return None
    try:
        grade = int(state.get("grade"))
    except (TypeError, ValueError):
        return None
    if not CUSTOMABILITY_GRADE_MIN <= grade <= CUSTOMABILITY_GRADE_MAX:
        return None
    try:
        set_record_id = int(state.get("set_record_id", 0))
    except (TypeError, ValueError):
        return None
    if not 0 <= set_record_id <= 0xFF:
        return None
    return CustomAbilityState(
        individual_options=options,
        grade=grade,
        set_record_id=set_record_id,
    )


def merge_customability_state(
    raw: Any,
    individual_options: Union[tuple[int, int], list[int]],
    grade: int,
    *,
    set_record_id: Any = _SET_RECORD_UNCHANGED,
) -> str:
    options = _option_pair(individual_options)
    if options is None:
        raise ValueError("customability requires exactly two nonzero u8 option IDs")
    grade_value = int(grade)
    if not CUSTOMABILITY_GRADE_MIN <= grade_value <= CUSTOMABILITY_GRADE_MAX:
        raise ValueError("customability grade must be between 0 and 3")
    data = _extra_json_dict(raw)
    existing = parse_customability_state(data)
    if set_record_id is _SET_RECORD_UNCHANGED:
        record_id = existing.set_record_id if existing is not None else 0
    else:
        try:
            record_id = int(set_record_id)
        except (TypeError, ValueError):
            raise ValueError("customability set record ID must be a u8")
        if not 0 <= record_id <= 0xFF:
            raise ValueError("customability set record ID must be a u8")
    state_payload = {
        "individual_options": [int(options[0]), int(options[1])],
        "grade": grade_value,
    }
    if record_id:
        state_payload["set_record_id"] = record_id
    data[CUSTOMABILITY_EXTRA_KEY] = state_payload
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def apply_customability_state_to_entry108(row: bytearray, raw: Any) -> bool:
    if len(row) != 108:
        raise ValueError("customability projection requires a 108-byte item entry")
    state = parse_customability_state(raw)
    if state is None:
        return False
    row[CUSTOMABILITY_ENTRY_OPTION_OFFSET] = state.individual_options[0]
    row[CUSTOMABILITY_ENTRY_OPTION_OFFSET + 1] = state.individual_options[1]
    # +105 remains zero; Stage3 uses +106 as the character-local set record.
    row[CUSTOMABILITY_ENTRY_SET_RECORD_OFFSET] = state.set_record_id
    row[CUSTOMABILITY_ENTRY_GRADE_OFFSET] = state.grade
    return True


__all__ = [
    "CUSTOMABILITY_ENTRY_GRADE_OFFSET",
    "CUSTOMABILITY_ENTRY_OPTION_OFFSET",
    "CUSTOMABILITY_ENTRY_SET_RECORD_OFFSET",
    "CUSTOMABILITY_EXTRA_KEY",
    "CUSTOMABILITY_GRADE_MAX",
    "CUSTOMABILITY_GRADE_MIN",
    "CUSTOMABILITY_OPTION_COUNT",
    "CustomAbilityState",
    "apply_customability_state_to_entry108",
    "merge_customability_state",
    "parse_customability_state",
]
