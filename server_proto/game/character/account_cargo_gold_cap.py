"""Stage3 accountcargo gold limits from the existing shared memory PVF.

The native [upgrade info] row has six integers. Its first integer is the
selection key and second integer is the total cargo-gold capacity. The
current Stage3 profile requires all ten keys; no default for an unknown key.
"""
from __future__ import annotations

import re
import threading
from typing import Any

from config import PVF_PATH
from pvf import pvf_runtime

ACCOUNT_CARGO_SCRIPT_PATH = "etc/accountcargo.etc"
_EXPECTED_KEYS = frozenset(range(8, 81, 8))
_LOCK = threading.RLock()
_READER: Any = None
_CAPS: dict[int, int] | None = None


def parse_account_cargo_gold_caps(text: str) -> dict[int, int]:
    sections = re.findall(
        r"\[upgrade info\]\s*(.*?)\s*\[/upgrade info\]", text,
        re.IGNORECASE | re.DOTALL,
    )
    if len(sections) != 1:
        raise ValueError("accountcargo requires exactly one [upgrade info] section")
    tokens = sections[0].split()
    if not tokens or len(tokens) % 6 or any(not re.fullmatch(r"-?\d+", token) for token in tokens):
        raise ValueError("accountcargo upgrade rows require six integer fields")
    values = [int(token) for token in tokens]
    caps: dict[int, int] = {}
    for offset in range(0, len(values), 6):
        key, gold_cap = values[offset:offset + 2]
        if key not in _EXPECTED_KEYS or key in caps:
            raise ValueError("accountcargo duplicate or unknown selection key")
        if not 0 < gold_cap <= 0xFFFFFFFF:
            raise ValueError("accountcargo gold capacity outside u32 range")
        caps[key] = gold_cap
    if set(caps) != _EXPECTED_KEYS:
        raise ValueError("accountcargo upgrade selection keys incomplete")
    return caps


def clear_account_cargo_gold_cap_cache() -> None:
    global _READER, _CAPS
    with _LOCK:
        _READER = None
        _CAPS = None


def _prepared_reader() -> Any:
    reader = pvf_runtime.get_shared_reader(PVF_PATH)
    if reader is None or not pvf_runtime.is_memory_prepared(PVF_PATH):
        raise RuntimeError("accountcargo limits require the prepared shared PVF reader")
    return reader


def _warm_caps() -> dict[int, int]:
    global _READER, _CAPS
    reader = _prepared_reader()
    if reader is not _READER or _CAPS is None:
        # Read only the known script; do not open another reader or scan paths.
        caps = parse_account_cargo_gold_caps(reader.read_text(ACCOUNT_CARGO_SCRIPT_PATH))
        _READER, _CAPS = reader, caps
    return _CAPS


def warmup_account_cargo_gold_caps() -> dict[str, int | str]:
    """Hard boot domain: call after shared PVF preparation, before listeners."""
    with _LOCK:
        caps = _warm_caps()
        return {
            "script": ACCOUNT_CARGO_SCRIPT_PATH,
            "selection_key_count": len(caps),
            "max_gold": max(caps.values()),
        }


def get_account_cargo_gold_cap(selection_key: int) -> int:
    """Return a validated key's total capacity; fail closed if unavailable."""
    if type(selection_key) is not int or selection_key not in _EXPECTED_KEYS:
        raise ValueError("accountcargo selection key unknown or invalid")
    with _LOCK:
        reader = _prepared_reader()
        if reader is not _READER or _CAPS is None:
            raise RuntimeError("accountcargo limits require explicit warmup for this shared reader")
        return _CAPS[selection_key]
