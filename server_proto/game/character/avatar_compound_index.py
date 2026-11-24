"""PVF-backed ordinary avatar compound rules for the Stage3 client."""
from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Tuple

from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from pvf import pvf_runtime


COMPOUND_AVATAR_LIST_PATH = "etc/compoundavatar.etc"
SERVER_PARAMETER_PATHS = (
    "etc/(r)serverparameter.etc",
    "etc/(f)serverparameter.etc",
    "etc/serverparameter.etc",
    "Etc/ServerParameter.etc",
)
COMPOUND_AVATAR_PARTS = (
    "hat",
    "hair",
    "face",
    "neck",
    "coat",
    "pants",
    "belt",
    "shoes",
)
_EQUIPMENT_TYPE_TO_PART = {
    "[hat avatar]": "hat",
    "[hair avatar]": "hair",
    "[face avatar]": "face",
    "[breast avatar]": "neck",
    "[coat avatar]": "coat",
    "[pants avatar]": "pants",
    "[waist avatar]": "belt",
    "[shoes avatar]": "shoes",
}
_JOB_LABELS = (
    "swordman",
    "fighter",
    "gunner",
    "mage",
    "priest",
    "at gunner",
    "thief",
    "at fighter",
    "at mage",
    "demonic swordman",
    "creator mage",
    "at swordman",
    "knight",
    "demonic lancer",
    "at priest",
)
_INDEX_ROW_RE = re.compile(r"(?m)^\s*(\d+)\s*(?:\r?\n|\s+)`([^`]+)`")
_LOCK = threading.RLock()
_INDEX = None  # type: Optional[AvatarCompoundIndex]


def _normalize_token(value: object) -> str:
    text = str(value or "").strip().replace("`", "").lower()
    return " ".join(text.split())


def avatar_compound_part(metadata: ItemMetadata) -> str:
    if not bool(getattr(metadata, "is_avatar", False)):
        return ""
    return _EQUIPMENT_TYPE_TO_PART.get(
        _normalize_token(getattr(metadata, "equipment_type", "")),
        "",
    )


def _section_matches(text: str, key: str) -> Tuple[str, ...]:
    pattern = re.compile(
        r"(?ims)^\s*\["
        + re.escape(key)
        + r"\]\s*(.*?)(?=^\s*\[[^\]]+\]\s*$|\Z)"
    )
    return tuple(match.group(1) for match in pattern.finditer(str(text or "")))


def _required_section(text: str, key: str, *, last: bool = False) -> str:
    matches = _section_matches(text, key)
    if not matches:
        raise ValueError("compound-avatar section missing: [%s]" % key)
    return matches[-1] if last else matches[0]


def _integers(text: str) -> Tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"-?\d+", str(text or "")))


def _single_int(text: str, key: str) -> int:
    values = _integers(_required_section(text, key))
    if len(values) != 1:
        raise ValueError("compound-avatar [%s] must contain one integer" % key)
    return int(values[0])


def _parse_rates(text: str, key: str) -> dict[str, int]:
    body = _required_section(text, key)
    tokens = re.findall(r"`([^`]*)`|(-?\d+)", body)
    flattened = [left or right for left, right in tokens]
    if len(flattened) % 2:
        raise ValueError("compound-avatar [%s] contains an odd token count" % key)
    rates = {}
    for offset in range(0, len(flattened), 2):
        part = _normalize_token(flattened[offset])
        rate = int(flattened[offset + 1])
        if part not in COMPOUND_AVATAR_PARTS or not 0 <= rate <= 10000:
            raise ValueError(
                "compound-avatar [%s] invalid part/rate: %s=%d"
                % (key, part, rate)
            )
        rates[part] = rate
    if set(rates) != set(COMPOUND_AVATAR_PARTS):
        raise ValueError("compound-avatar [%s] does not cover all eight parts" % key)
    return rates


@dataclass(frozen=True)
class AvatarCompoundCandidate:
    item_id: int
    weight: int
    metadata: ItemMetadata


@dataclass(frozen=True)
class AvatarCompoundPartRule:
    part: str
    rare_rate: int
    upper_rare_rate: int
    rare_pool: Tuple[AvatarCompoundCandidate, ...]
    ordinary_pool: Tuple[AvatarCompoundCandidate, ...]


@dataclass(frozen=True)
class AvatarCompoundJobRule:
    job: int
    job_label: str
    source_path: str
    grade: int
    upper_grade: int
    normal_material_item_id: int
    upper_material_item_id: int
    parts: dict[str, AvatarCompoundPartRule]


@dataclass(frozen=True)
class AvatarCompoundIndex:
    jobs: dict[int, AvatarCompoundJobRule]
    candidate_count: int
    server_parameter_path: str
    upper_bind_cube_rate_bonus: int

    def require_job(self, job: int) -> AvatarCompoundJobRule:
        rule = self.jobs.get(int(job))
        if rule is None:
            raise ValueError("avatar compound job is not configured: %d" % int(job))
        return rule


@dataclass(frozen=True)
class AvatarCompoundPlan:
    result: AvatarCompoundCandidate
    job: int
    part: str
    rare_rate: int
    roll: int
    hit_rare: bool
    first_grade: int
    second_grade: int
    material_kind: str
    material_rate_bonus: int


def _material_item_id(text: str, key: str) -> int:
    values = _integers(_required_section(text, key, last=True))
    if len(values) >= 3:
        count, item_id, consume_count = values[:3]
        if count != 1 or consume_count != 1:
            raise ValueError("compound-avatar [%s] has unsupported material shape" % key)
        return int(item_id)
    if len(values) == 1:
        return int(values[0])
    raise ValueError("compound-avatar [%s] has no material item id" % key)


def _read_first_text(reader: Any, paths: Iterable[str]) -> Tuple[str, str]:
    for path in paths:
        try:
            text = str(reader.read_text(path) or "")
        except Exception:
            continue
        if text:
            return str(path), text
    raise ValueError("compound-avatar serverparameter.etc is missing")


def _candidate(
    item_id: int,
    weight: int,
    *,
    job: int,
    part: str,
    rare: bool,
    resolver: Callable[[int], ItemMetadata],
) -> AvatarCompoundCandidate:
    if item_id <= 0 or weight <= 0:
        raise ValueError("avatar compound candidate id/weight must be positive")
    metadata = resolver(int(item_id))
    if int(metadata.item_id) != int(item_id) or not metadata.is_avatar:
        raise ValueError("avatar compound candidate is not an avatar: %d" % item_id)
    actual_part = avatar_compound_part(metadata)
    if actual_part != part:
        raise ValueError(
            "avatar compound candidate part mismatch item=%d expected=%s actual=%s"
            % (item_id, part, actual_part)
        )
    if not metadata.is_job_allowed(int(job)):
        raise ValueError(
            "avatar compound candidate job mismatch item=%d job=%d" % (item_id, job)
        )
    if rare and int(metadata.rarity) != 2:
        raise ValueError(
            "avatar compound rare candidate rarity mismatch item=%d rarity=%d"
            % (item_id, int(metadata.rarity))
        )
    if not rare and int(metadata.rarity) == 2:
        raise ValueError("avatar compound ordinary pool contains rare item=%d" % item_id)
    return AvatarCompoundCandidate(int(item_id), int(weight), metadata)


def _parse_part_rule(
    text: str,
    *,
    job: int,
    part: str,
    rare_rate: int,
    upper_rare_rate: int,
    resolver: Callable[[int], ItemMetadata],
) -> AvatarCompoundPartRule:
    values = _integers(_required_section(text, part + " avatar"))
    if not values:
        raise ValueError("avatar compound part pool is empty: %s" % part)
    rare_count = int(values[0])
    pair_values = values[1:]
    if rare_count <= 0 or len(pair_values) % 2:
        raise ValueError("avatar compound part pool has invalid shape: %s" % part)
    pairs = tuple(
        (int(pair_values[offset]), int(pair_values[offset + 1]))
        for offset in range(0, len(pair_values), 2)
    )
    if rare_count >= len(pairs):
        raise ValueError("avatar compound ordinary pool is empty: %s" % part)
    rare_pool = tuple(
        _candidate(
            item_id,
            weight,
            job=job,
            part=part,
            rare=True,
            resolver=resolver,
        )
        for item_id, weight in pairs[:rare_count]
    )
    ordinary_pool = tuple(
        _candidate(
            item_id,
            weight,
            job=job,
            part=part,
            rare=False,
            resolver=resolver,
        )
        for item_id, weight in pairs[rare_count:]
    )
    return AvatarCompoundPartRule(
        part=part,
        rare_rate=int(rare_rate),
        upper_rare_rate=int(upper_rare_rate),
        rare_pool=rare_pool,
        ordinary_pool=ordinary_pool,
    )


def build_avatar_compound_job_rule(
    job: int,
    source_path: str,
    text: str,
    *,
    resolver: Callable[[int], ItemMetadata] = resolve_item_metadata,
) -> AvatarCompoundJobRule:
    job_id = int(job)
    if not 0 <= job_id < len(_JOB_LABELS):
        raise ValueError("avatar compound job index is invalid: %d" % job_id)
    character_type = _normalize_token(_required_section(text, "character type"))
    character_type = character_type.strip("[]")
    if character_type != _JOB_LABELS[job_id]:
        raise ValueError(
            "avatar compound character type mismatch job=%d expected=%s actual=%s"
            % (job_id, _JOB_LABELS[job_id], character_type)
        )
    grade = _single_int(text, "grade")
    upper_grade = _single_int(text, "upper grade")
    if grade < 0 or upper_grade <= grade:
        raise ValueError("avatar compound input grades are invalid")
    rare_rates = _parse_rates(text, "rare rate")
    upper_rates = _parse_rates(text, "upper rare rate")
    parts = {
        part: _parse_part_rule(
            text,
            job=job_id,
            part=part,
            rare_rate=rare_rates[part],
            upper_rare_rate=upper_rates[part],
            resolver=resolver,
        )
        for part in COMPOUND_AVATAR_PARTS
    }
    material_item_id = _material_item_id(text, "material")
    upper_material_item_id = _material_item_id(text, "material_upper")
    if material_item_id <= 0:
        raise ValueError("avatar compound normal material item id is invalid")
    if upper_material_item_id <= 0 or upper_material_item_id == material_item_id:
        raise ValueError("avatar compound upper material item id is invalid")
    return AvatarCompoundJobRule(
        job=job_id,
        job_label=_JOB_LABELS[job_id],
        source_path=str(source_path),
        grade=grade,
        upper_grade=upper_grade,
        normal_material_item_id=material_item_id,
        upper_material_item_id=upper_material_item_id,
        parts=parts,
    )


def build_avatar_compound_index_from_reader(
    reader: Any,
    *,
    resolver: Callable[[int], ItemMetadata] = resolve_item_metadata,
) -> AvatarCompoundIndex:
    listing = reader.read_text(COMPOUND_AVATAR_LIST_PATH)
    server_parameter_path, server_parameter_text = _read_first_text(
        reader,
        SERVER_PARAMETER_PATHS,
    )
    upper_bind_cube_rate_bonus = _single_int(
        server_parameter_text,
        "upper bind cube rate bonus",
    )
    if not 0 <= upper_bind_cube_rate_bonus <= 10000:
        raise ValueError("avatar compound upper bind cube rate bonus is invalid")
    file_by_job = {}
    for match in _INDEX_ROW_RE.finditer(str(listing or "")):
        file_index = int(match.group(1))
        filename = str(match.group(2) or "").replace("\\", "/").strip()
        if file_index <= 0 or not filename:
            continue
        job = file_index - 1
        if job in file_by_job:
            raise ValueError("duplicate avatar compound job index=%d" % job)
        file_by_job[job] = filename
    if set(file_by_job) != set(range(len(_JOB_LABELS))):
        raise ValueError("compoundavatar.etc does not map all Stage3 jobs")

    jobs = {}
    candidate_ids = set()
    for job in range(len(_JOB_LABELS)):
        source_path = "etc/" + file_by_job[job].lstrip("/")
        rule = build_avatar_compound_job_rule(
            job,
            source_path,
            reader.read_text(source_path),
            resolver=resolver,
        )
        jobs[job] = rule
        for part_rule in rule.parts.values():
            candidate_ids.update(entry.item_id for entry in part_rule.rare_pool)
            candidate_ids.update(entry.item_id for entry in part_rule.ordinary_pool)
    return AvatarCompoundIndex(
        jobs=jobs,
        candidate_count=len(candidate_ids),
        server_parameter_path=server_parameter_path,
        upper_bind_cube_rate_bonus=upper_bind_cube_rate_bonus,
    )


def install_avatar_compound_index(index: AvatarCompoundIndex) -> None:
    if not isinstance(index, AvatarCompoundIndex) or len(index.jobs) != len(_JOB_LABELS):
        raise ValueError("invalid avatar compound index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def clear_avatar_compound_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None


def get_avatar_compound_index() -> AvatarCompoundIndex:
    with _LOCK:
        index = _INDEX
    if index is None:
        raise RuntimeError("avatar compound index is not warmed")
    return index


def warmup_avatar_compound_index() -> dict[str, int]:
    reader = pvf_runtime.ensure_shared_reader()
    index = build_avatar_compound_index_from_reader(reader)
    install_avatar_compound_index(index)
    return {
        "job_count": len(index.jobs),
        "part_count": sum(len(rule.parts) for rule in index.jobs.values()),
        "candidate_count": int(index.candidate_count),
        "upper_bind_cube_rate_bonus": int(index.upper_bind_cube_rate_bonus),
    }


def _weighted_pick(
    pool: Iterable[AvatarCompoundCandidate],
    rng: Any,
) -> AvatarCompoundCandidate:
    candidates = tuple(pool)
    total = sum(int(candidate.weight) for candidate in candidates)
    if not candidates or total <= 0:
        raise ValueError("avatar compound weighted pool is empty")
    roll = int(rng.randrange(total))
    cursor = 0
    for candidate in candidates:
        cursor += int(candidate.weight)
        if roll < cursor:
            return candidate
    return candidates[-1]


def plan_avatar_compound(
    *,
    job: int,
    first: ItemMetadata,
    second: ItemMetadata,
    material_item_id: int,
    rng: Any = None,
) -> AvatarCompoundPlan:
    index = get_avatar_compound_index()
    rule = index.require_job(int(job))
    first_part = avatar_compound_part(first)
    second_part = avatar_compound_part(second)
    if not first_part or first_part != second_part:
        raise ValueError("avatar compound materials must use the same basic part")
    if not first.is_job_allowed(int(job)) or not second.is_job_allowed(int(job)):
        raise ValueError("avatar compound material job mismatch")
    first_grade = int(first.grade)
    second_grade = int(second.grade)
    allowed_grades = {int(rule.grade), int(rule.upper_grade)}
    if first_grade not in allowed_grades or second_grade not in allowed_grades:
        raise ValueError(
            "avatar compound material grade mismatch: %d,%d"
            % (first_grade, second_grade)
        )
    material_id = int(material_item_id)
    if material_id == int(rule.normal_material_item_id):
        material_kind = "normal"
        material_rate_bonus = 0
    elif material_id == int(rule.upper_material_item_id):
        material_kind = "upper"
        material_rate_bonus = int(index.upper_bind_cube_rate_bonus)
    else:
        raise ValueError("avatar compound material is not a configured binder")

    part_rule = rule.parts[first_part]
    upper_count = int(first_grade == rule.upper_grade) + int(
        second_grade == rule.upper_grade
    )
    rare_rate = int(part_rule.rare_rate) + (
        int(part_rule.upper_rare_rate) * upper_count
    ) // 2
    rare_rate += material_rate_bonus
    rare_rate = max(0, min(10000, rare_rate))
    random_source = rng if rng is not None else random.SystemRandom()
    roll = int(random_source.randrange(10000))
    hit_rare = bool(roll < rare_rate)
    pool = part_rule.rare_pool if hit_rare else part_rule.ordinary_pool
    result = _weighted_pick(pool, random_source)
    return AvatarCompoundPlan(
        result=result,
        job=int(job),
        part=first_part,
        rare_rate=rare_rate,
        roll=roll,
        hit_rare=hit_rare,
        first_grade=first_grade,
        second_grade=second_grade,
        material_kind=material_kind,
        material_rate_bonus=material_rate_bonus,
    )


__all__ = [
    "AvatarCompoundCandidate",
    "AvatarCompoundIndex",
    "AvatarCompoundJobRule",
    "AvatarCompoundPartRule",
    "AvatarCompoundPlan",
    "avatar_compound_part",
    "build_avatar_compound_index_from_reader",
    "build_avatar_compound_job_rule",
    "clear_avatar_compound_index",
    "get_avatar_compound_index",
    "install_avatar_compound_index",
    "plan_avatar_compound",
    "warmup_avatar_compound_index",
]
