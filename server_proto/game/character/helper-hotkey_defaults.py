#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage3 PVF-backed hotkey defaults for job-specific keyboard layouts."""
from __future__ import annotations

import re
import threading
from typing import Dict, Sequence, Tuple

from pvf import pvf_runtime


CREATOR_JOB = 10
HOTKEY_SLOT_COUNT = 99
UNASSIGNED_KEY = 0x86
NORMAL_HOTKEY_PVF_PATH = "clientonly/hotkeysystem_ver2.co"
CREATOR_HOTKEY_PVF_PATH = "clientonly/hotkeysystemforcreator_ver2.co"

_KEY_BLOCK_RE = re.compile(
    r"(?ims)^\s*\[key\]\s*(.*?)(?=^\s*\[key\]\s*|\Z)"
)
_QUOTED_TOKEN_RE = re.compile(r"`([^`]*)`")
_INTEGER_LINE_RE = re.compile(r"(?m)^\s*(-?\d+)\s*$")
_HOTKEY_ID_RE = re.compile(r"^I(\d+)$", re.IGNORECASE)

_EXPLICIT_VK_TO_INTERNAL = {
    8: 15,
    9: 31,
    13: 16,
    16: 8,
    17: 5,
    18: 12,
    19: 107,
    20: 32,
    27: 26,
    32: 13,
    33: 19,
    34: 20,
    35: 22,
    36: 21,
    37: 0,
    38: 2,
    39: 1,
    40: 3,
    44: 93,
    45: 17,
    46: 18,
    106: 25,
    107: 23,
    109: 33,
    111: 30,
    145: 108,
    160: 8,
    161: 9,
    162: 5,
    163: 6,
    164: 12,
    165: 11,
    186: 27,
    187: 14,
    188: 29,
    189: 24,
    190: 28,
    191: 34,
    219: 104,
    221: 105,
    222: 106,
}

_LOCK = threading.RLock()
_DEFAULTS_BY_KEY_TYPE: Dict[int, Tuple[int, ...]] = {}


def stage3_internal_hotkey_code(virtual_key: int) -> int:
    """Mirror Stage3 ``sub_2C7E790`` (VK -> internal hotkey code)."""
    vk = int(virtual_key)
    if 48 <= vk <= 57:
        return vk - 3
    if 65 <= vk <= 90:
        return vk - 10
    if 96 <= vk <= 105:
        return vk - 61
    if 112 <= vk <= 123:
        return vk - 31
    return int(_EXPLICIT_VK_TO_INTERNAL.get(vk, UNASSIGNED_KEY))


def parse_hotkey_defaults_ver2(
    text: str,
    *,
    slot_count: int = HOTKEY_SLOT_COUNT,
) -> Tuple[list[int], int]:
    """Parse ``Ixx`` records from a Stage3 ``*_ver2.co`` hotkey table."""
    count = max(1, int(slot_count))
    slots = [UNASSIGNED_KEY] * count
    assigned = set()
    for block_match in _KEY_BLOCK_RE.finditer(str(text or "")):
        block = block_match.group(1)
        slot_index = None
        for token in _QUOTED_TOKEN_RE.findall(block):
            id_match = _HOTKEY_ID_RE.fullmatch(token.strip())
            if id_match is not None:
                slot_index = int(id_match.group(1)) - 1
                break
        if slot_index is None or not 0 <= slot_index < count:
            continue
        values = _INTEGER_LINE_RE.findall(block)
        if not values:
            continue
        slots[slot_index] = stage3_internal_hotkey_code(int(values[-1]))
        assigned.add(slot_index)
    return slots, len(assigned)


def _load_table(path: str) -> Tuple[Tuple[int, ...], int]:
    slots, assigned = parse_hotkey_defaults_ver2(pvf_runtime.read_text(path))
    if assigned < 64:
        raise RuntimeError(
            "hotkey table is incomplete: path=%s assigned=%s" % (path, assigned)
        )
    return tuple(slots), assigned


def warmup_hotkey_defaults() -> dict[str, object]:
    """Warm ordinary/Creator tables from the already-preloaded shared PVF."""
    normal, normal_assigned = _load_table(NORMAL_HOTKEY_PVF_PATH)
    creator, creator_assigned = _load_table(CREATOR_HOTKEY_PVF_PATH)
    with _LOCK:
        _DEFAULTS_BY_KEY_TYPE.clear()
        _DEFAULTS_BY_KEY_TYPE.update({0: normal, 1: creator})
    return {
        "normal_assigned": normal_assigned,
        "creator_assigned": creator_assigned,
        "slot_count": HOTKEY_SLOT_COUNT,
        "creator_job": CREATOR_JOB,
    }


def get_hotkey_slots_for_key_type(key_type: int) -> list[int]:
    kt = 1 if int(key_type) == 1 else 0
    with _LOCK:
        cached = _DEFAULTS_BY_KEY_TYPE.get(kt)
    if cached is None:
        warmup_hotkey_defaults()
        with _LOCK:
            cached = _DEFAULTS_BY_KEY_TYPE[kt]
    return list(cached)


def get_creator_hotkey_slots() -> list[int]:
    return get_hotkey_slots_for_key_type(1)


def is_creator_job(job: int | None) -> bool:
    try:
        return int(job) == CREATOR_JOB
    except (TypeError, ValueError):
        return False


__all__: Sequence[str] = (
    "CREATOR_HOTKEY_PVF_PATH",
    "CREATOR_JOB",
    "HOTKEY_SLOT_COUNT",
    "NORMAL_HOTKEY_PVF_PATH",
    "UNASSIGNED_KEY",
    "get_creator_hotkey_slots",
    "get_hotkey_slots_for_key_type",
    "is_creator_job",
    "parse_hotkey_defaults_ver2",
    "stage3_internal_hotkey_code",
    "warmup_hotkey_defaults",
)
