"""Pregenerated level-1 acceptable quests for START_MAP (no Game PVF read).

Source JSON: ``pytools/_extract_acceptable_quests_json.py``
(S4A12 ComputeAcceptableQuests-lite against n_quest).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from config import BASE_DIR

DEFAULT_ACCEPTABLE_QUESTS_JSON = BASE_DIR / "data" / "resources" / "acceptable_quests_by_job_lv1.json"


def resolve_acceptable_quests_json_path() -> Path | None:
    raw = (os.environ.get("USDOF_ACCEPTABLE_QUESTS_JSON") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(DEFAULT_ACCEPTABLE_QUESTS_JSON)
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def _load_jobs_table() -> dict[str, dict]:
    path = resolve_acceptable_quests_json_path()
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    jobs_raw = payload.get("jobs") or {}
    out: dict[str, dict] = {}
    for job_key, entry in jobs_raw.items():
        try:
            key = str(int(job_key) & 0xFF)
        except Exception:
            continue
        acceptable = []
        for qid in (entry or {}).get("acceptable_ids") or []:
            try:
                acceptable.append(int(qid) & 0xFFFF)
            except Exception:
                continue
        roots = []
        for qid in (entry or {}).get("starter_root_ids") or []:
            try:
                roots.append(int(qid) & 0xFFFF)
            except Exception:
                continue
        out[key] = {
            "acceptable_ids": tuple(acceptable),
            "starter_root_ids": tuple(roots),
        }
    return out


def get_acceptable_quest_ids_for_job(job: int) -> Sequence[int]:
    """Return level-1 acceptable quest ids for job (may be empty)."""
    table = _load_jobs_table()
    entry = table.get(str(int(job) & 0xFF)) or {}
    return entry.get("acceptable_ids") or ()


def get_starter_root_quest_ids_for_job(job: int) -> Sequence[int]:
    """No-prereq epic/side roots (for optional START pair seed)."""
    table = _load_jobs_table()
    entry = table.get(str(int(job) & 0xFF)) or {}
    return entry.get("starter_root_ids") or ()


def build_start_active_quest_pairs(
    job: int,
    *,
    max_pairs: int = 0,
    trigger: int = 0,
) -> list[tuple[int, int]]:
    """Optional START body 30-pair seed (debug only; default empty).

    Fresh / first-enter characters MUST keep all 30 pairs at 0xFFFF until
    the client accepts via C1/001F (and server ACKs). Non-FFFF pairs are
    registered as in-progress on START (client sub_1394650) and will skip
    intro/accept flow — do not enable for new-character default path.
    """
    roots = list(get_starter_root_quest_ids_for_job(job))
    # Prefer epic roots 101 / 1016 first when present.
    preferred = [101, 1016]
    ordered: list[int] = []
    for qid in preferred:
        if qid in roots and qid not in ordered:
            ordered.append(qid)
    for qid in roots:
        if qid not in ordered:
            ordered.append(qid)
    limit = max(0, min(30, int(max_pairs)))
    return [(int(qid) & 0xFFFF, int(trigger) & 0xFFFFFFFF) for qid in ordered[:limit]]
