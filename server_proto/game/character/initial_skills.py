"""Pregenerated initial skills for START_MAP SKILLINFO (no Game PVF read).

Source JSON is produced offline by ``pytools/_extract_initial_skills_json.py``
from character .chr ``[initial value]/[skill]`` pairs, with active/passive
slots assigned offline from .skl ``[type]`` (active 0+, passive 102+).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from config import BASE_DIR

DEFAULT_INITIAL_SKILLS_JSON = BASE_DIR / "data" / "resources" / "initial_skills_by_job.json"


@dataclass(frozen=True)
class InitialSkill:
    slot: int
    skill_id: int
    level: int
    # 技能释放命令字节（C1/014B → character_skills.extra_values）。
    # 进房 S0/0013 SkillTree field4 = 逐字节 int32；空则省略 field4。
    command_bytes: bytes = b""


def resolve_initial_skills_json_path() -> Path | None:
    raw = (os.environ.get("USDOF_INITIAL_SKILLS_JSON") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(DEFAULT_INITIAL_SKILLS_JSON)
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def _load_jobs_table() -> dict[str, list[InitialSkill]]:
    path = resolve_initial_skills_json_path()
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    jobs_raw = payload.get("jobs") or {}
    out: dict[str, list[InitialSkill]] = {}
    for job_key, entry in jobs_raw.items():
        skills_raw = (entry or {}).get("skills") or []
        skills: list[InitialSkill] = []
        for item in skills_raw:
            try:
                skills.append(
                    InitialSkill(
                        slot=int(item.get("slot", 0)) & 0xFF,
                        skill_id=int(item.get("skill_id", 0)) & 0xFFFF,
                        level=max(1, min(255, int(item.get("level", 1)))),
                        command_bytes=b"",
                    )
                )
            except Exception:
                continue
        out[str(int(job_key) & 0xFF)] = skills
    return out


def get_initial_skills_for_job(job: int) -> Sequence[InitialSkill]:
    """Return starter skills for job, or empty if JSON missing / job unknown."""
    table = _load_jobs_table()
    return table.get(str(int(job) & 0xFF), ())
