from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config import BASE_DIR, PVF_PATH
from pvf import pvf_runtime


CREATE_EQUIPMENT_SLOT_MAP = {
    "[weapon]": 12,
    "[coat]": 14,
    "[shoulder]": 15,
    "[pants]": 16,
    "[shoes]": 17,
    "[waist]": 18,
    "[amulet]": 19,
    "[wrist]": 20,
    "[ring]": 21,
    "[support]": 22,
    "[magic stone]": 23,
    "[earring]": 24,
}

DEFAULT_RESOURCE_READABLE_PVF = BASE_DIR / "resources" / "pvf" / "USDOF_client_script_readable_20260705.pvf"
DEFAULT_LOG_READABLE_PVF = BASE_DIR / "logs" / "USDOF_Script_rekey_pvfUtility_readable_20260705.pvf"
DEFAULT_INITIAL_GROW_TYPE = 0


@dataclass(frozen=True)
class CharacterClassInfo:
    job_id: int
    base_character_id: int
    chr_path: str
    job_token: str | None = None


def _candidate_pvf_paths() -> list[Path]:
    raw_candidates = [
        os.environ.get("USDOF_INITIAL_EQUIPMENT_PVF"),
        os.environ.get("USDOF_CLIENT_PVF_READABLE"),
        str(DEFAULT_RESOURCE_READABLE_PVF),
        str(DEFAULT_LOG_READABLE_PVF),
        os.environ.get("USDOF_PVF_PATH"),
        PVF_PATH,
    ]
    paths: list[Path] = []
    for raw in raw_candidates:
        if not raw:
            continue
        for part in str(raw).split(";"):
            part = part.strip()
            if part:
                paths.append(Path(part))
    return paths


def resolve_initial_equipment_pvf_path() -> Path | None:
    for path in _candidate_pvf_paths():
        if path.exists():
            return path
    return None


def _read_pvf_text(path: Path, relative: str) -> str:
    """Read one PVF script text for create-equipment via shared preload only.

    Runtime hard rule (AGENTS 4.1.2): no private/light PvfReader. Server boot
    must preload Script.pvf before any create-equipment / class-identity read.
    """
    target = str(Path(path).resolve())
    if not pvf_runtime.is_memory_prepared(target):
        # Force the shared memory path; never open a private cold reader.
        pvf_runtime.ensure_shared_reader(target)
    if not pvf_runtime.is_memory_prepared(target):
        raise RuntimeError(
            f"shared memory PVF unavailable for initial_equipment: {target}"
        )
    return pvf_runtime.read_text(relative, pvf_path=target)


def _extract_character_chr_path(character_lst_text: str, job: int) -> str | None:
    same_line = re.search(rf"(?m)^\s*{int(job)}\s+`([^`]+)`", character_lst_text)
    if same_line:
        return same_line.group(1).replace("\\", "/").strip("/")

    lines = [line.strip() for line in character_lst_text.splitlines()]
    for index, line in enumerate(lines):
        if not re.fullmatch(r"-?\d+", line):
            continue
        if int(line) != int(job):
            continue
        for next_line in lines[index + 1 : index + 5]:
            match = re.search(r"`([^`]+)`", next_line)
            if match:
                return match.group(1).replace("\\", "/").strip("/")
    return None


def _parse_chr_job_token(chr_text: str) -> str | None:
    match = re.search(r"(?is)^\s*\[job\]\s*`([^`]+)`", chr_text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


@lru_cache(maxsize=64)
def get_character_class_info(job: int, pvf_path: str | os.PathLike[str] | None = None) -> CharacterClassInfo | None:
    """Resolve the current client's character class identity from PVF.

    For USDOF's ``USERINFO type=2`` DSTR row the byte copied into row ``+0x04``
    is the base character/model id.  The current client PVF uses the numeric
    key in ``character/character.lst`` for that unadvanced model family.

    Do not use this helper as authority for the first byte after the DSTR name:
    live tracing shows ``0x018D212B`` only pushes that byte into
    ``sub_15F4DE0(value)+0x102C``.  The normal local roster keeps that vector
    key at zero to avoid the non-Slayer forced red/change-name card state.
    """

    job_id = int(job) & 0xFF
    path = Path(pvf_path) if pvf_path is not None else resolve_initial_equipment_pvf_path()
    if path is None or not path.exists():
        return None

    try:
        character_lst = _read_pvf_text(path, "character/character.lst")
        chr_path = _extract_character_chr_path(character_lst, job_id)
        if not chr_path:
            return None
        job_token = None
        try:
            job_token = _parse_chr_job_token(_read_pvf_text(path, "character/" + chr_path))
        except Exception:
            job_token = None
        return CharacterClassInfo(
            job_id=job_id,
            base_character_id=job_id,
            chr_path=chr_path,
            job_token=job_token,
        )
    except Exception:
        return None


def get_userinfo_job_base_for_job(job: int, pvf_path: str | os.PathLike[str] | None = None) -> tuple[int, int]:
    job_id = int(job) & 0xFF
    # Hot path for the character-select USERINFO roster.  Current USDOF
    # reversing shows unadvanced character rows use the character.lst numeric
    # key for both job_id and base/model id.  Opening the readable PVF here is
    # very expensive and repeats once per distinct job on a cold server start.
    if pvf_path is None and os.environ.get("USDOF_USERINFO_JOB_BASE_FROM_PVF", "0").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return job_id, job_id

    info = get_character_class_info(int(job), pvf_path)
    if info is None:
        return job_id, job_id
    return info.job_id & 0xFF, info.base_character_id & 0xFF


def _parse_create_equipment_list(chr_text: str) -> tuple[tuple[int, int], ...]:
    entries: list[tuple[int, int]] = []
    in_section = False
    current_slot: int | None = None
    for raw_line in chr_text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower == "[create equipment list]":
            in_section = True
            current_slot = None
            continue
        if in_section and lower == "[/create equipment list]":
            break
        if not in_section or not line:
            continue

        inline = re.match(r"`?(\[[^\]]+\])`?\s+(-?\d+)", line)
        if inline:
            slot = CREATE_EQUIPMENT_SLOT_MAP.get(inline.group(1).lower())
            if slot is not None:
                item_id = int(inline.group(2))
                if item_id > 0:
                    entries.append((slot, item_id))
            current_slot = None
            continue

        normalized_slot_name = lower.strip("`").strip()
        slot = CREATE_EQUIPMENT_SLOT_MAP.get(normalized_slot_name)
        if slot is not None:
            current_slot = slot
            continue
        if current_slot is not None and re.fullmatch(r"-?\d+", line):
            item_id = int(line)
            if item_id > 0:
                entries.append((current_slot, item_id))
            current_slot = None
    return tuple(entries)


def _env_initial_grow_type(job: int) -> int | None:
    keys = (
        f"USDOF_CREATE_INITIAL_GROW_TYPE_{int(job) & 0xFF}",
        "USDOF_CREATE_INITIAL_GROW_TYPE",
    )
    for key in keys:
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw, 0) & 0xFF
        except ValueError:
            continue
    return None


def _parse_growtype_sections(chr_text: str) -> tuple[int, ...]:
    values = {
        int(match.group(1))
        for match in re.finditer(r"(?im)^\s*\[growtype\s+(-?\d+)\]\s*$", chr_text)
    }
    return tuple(sorted(values))


@lru_cache(maxsize=64)
def get_initial_grow_type_for_job(job: int, pvf_path: str | os.PathLike[str] | None = None) -> int:
    """Resolve the roster growtype to store for a newly-created character.

    Current USDOF reverse evidence shows the roster renderer consumes
    ``base_character_id`` separately, then splits the packed grow/sub-grow byte.
    In ``sub_1CBDAB0``, Knight ``grow_type=1`` resolves to the advanced
    profession text (Elven Knight).  A freshly-created, unadvanced character
    should therefore stay at grow ``0`` unless an explicit environment override
    requests a class-change probe.
    """

    override = _env_initial_grow_type(int(job))
    if override is not None:
        return override

    if os.environ.get("USDOF_CREATE_INITIAL_GROW_FROM_PVF", "0").strip().lower() in {"0", "false", "no", "off"}:
        return DEFAULT_INITIAL_GROW_TYPE

    path = Path(pvf_path) if pvf_path is not None else resolve_initial_equipment_pvf_path()
    if path is None or not path.exists():
        return DEFAULT_INITIAL_GROW_TYPE

    try:
        character_lst = _read_pvf_text(path, "character/character.lst")
        chr_path = _extract_character_chr_path(character_lst, int(job) & 0xFF)
        if not chr_path:
            return DEFAULT_INITIAL_GROW_TYPE
        chr_text = _read_pvf_text(path, "character/" + chr_path)
        sections = _parse_growtype_sections(chr_text)
        for value in sections:
            if value > 0:
                return value & 0xFF
        if sections:
            return sections[0] & 0xFF
    except Exception:
        return DEFAULT_INITIAL_GROW_TYPE
    return DEFAULT_INITIAL_GROW_TYPE


@lru_cache(maxsize=64)
def get_initial_equipment_for_job(job: int, pvf_path: str | os.PathLike[str] | None = None) -> tuple[tuple[int, int], ...]:
    if os.environ.get("USDOF_CREATE_INITIAL_EQUIPMENT", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ()

    path = Path(pvf_path) if pvf_path is not None else resolve_initial_equipment_pvf_path()
    if path is None or not path.exists():
        return ()

    try:
        character_lst = _read_pvf_text(path, "character/character.lst")
        chr_path = _extract_character_chr_path(character_lst, int(job) & 0xFF)
        if not chr_path:
            return ()
        chr_text = _read_pvf_text(path, "character/" + chr_path)
        return _parse_create_equipment_list(chr_text)
    except Exception:
        return ()


def warmup_initial_equipment_index(*, job_max: int = 16) -> dict[str, int | str]:
    """Boot-time create-equipment / character.lst warmup (shared PVF only)."""
    path = resolve_initial_equipment_pvf_path()
    if path is None:
        return {"ok": 0, "error": "initial equipment PVF path not found"}
    target = str(Path(path).resolve())
    if not pvf_runtime.is_memory_prepared(target):
        pvf_runtime.ensure_shared_reader(target)
    if not pvf_runtime.is_memory_prepared(target):
        return {"ok": 0, "error": f"shared memory PVF unavailable: {target}"}
    # Touch character.lst once, then warm common job create lists.
    _ = _read_pvf_text(path, "character/character.lst")
    jobs_warmed = 0
    for job in range(0, max(0, int(job_max))):
        try:
            rows = get_initial_equipment_for_job(job, path)
        except Exception:
            rows = ()
        if rows:
            jobs_warmed += 1
        # Also warm class identity path used by USERINFO helpers.
        try:
            get_character_class_info(job, path)
        except Exception:
            pass
    return {
        "ok": 1,
        "pvf": target,
        "character_lst": 1,
        "jobs_warmed": jobs_warmed,
        "job_max": int(job_max),
    }
