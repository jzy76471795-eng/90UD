"""PVF-backed rules and weighted rewards for avatar-emblem compounding."""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable

from game.character.avatar_recast_emblem_index import (
    AvatarEmblemEntry,
    AvatarEmblemIndex,
    get_avatar_emblem_index,
)
from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from pvf import pvf_runtime


EMBLEM_COMPOUND_CONFIG_PATH = "etc/emblemcompound.etc"
MIN_MATERIAL_COUNT = 2
MAX_MATERIAL_COUNT = 5
MAX_RESULT_ROWS = 0xFF
MAX_REWARD_COUNT = 0xFFFFFFFF
MAX_WEIGHT = 0x7FFFFFFFFFFFFFFF

_LOCK = threading.RLock()
_CONFIG: "EmblemCompoundConfig | None" = None


def _normalized_tag(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _section_lines(text: str, section_name: str) -> list[str]:
    section = " ".join(str(section_name or "").strip().lower().split())
    opening = "[%s]" % section
    closing = "[/%s]" % section
    lines = [str(line).strip() for line in str(text or "").splitlines()]
    start: int | None = None
    stop: int | None = None
    for index, line in enumerate(lines):
        tag = _normalized_tag(line)
        if tag == opening:
            if start is not None:
                raise ValueError("duplicate %s section" % opening)
            start = index + 1
        elif tag == closing:
            if start is None or stop is not None or index < start:
                raise ValueError("invalid %s boundary" % closing)
            stop = index
    if start is None or stop is None:
        raise ValueError("missing or unclosed %s section" % opening)
    return [line for line in lines[start:stop] if line]


def _integer_tokens(lines: list[str], *, context: str) -> list[int]:
    values: list[int] = []
    for line in lines:
        for token in str(line or "").replace("`", " ").split():
            try:
                values.append(int(token, 0))
            except ValueError as exc:
                raise ValueError(
                    "invalid integer token context=%s token=%r" % (context, token)
                ) from exc
    return values


def parse_emblem_compound_mappings(text: str) -> dict[tuple[int, int], int]:
    """Parse explicit ``(source [grade], lookup count) -> booster`` cells."""
    values = _integer_tokens(
        _section_lines(text, "emblem compound info"),
        context="emblem compound info",
    )
    mappings: dict[tuple[int, int], int] = {}
    offset = 0
    while offset < len(values):
        if offset + 2 > len(values):
            raise ValueError("truncated emblem compound info header")
        config_grade = int(values[offset])
        lookup_ceiling = int(values[offset + 1])
        offset += 2
        if config_grade <= 0:
            raise ValueError(
                "invalid emblem compound config grade=%d" % config_grade
            )
        if not MIN_MATERIAL_COUNT <= lookup_ceiling <= MAX_MATERIAL_COUNT:
            raise ValueError(
                "invalid emblem compound lookup ceiling grade=%d count=%d"
                % (config_grade, lookup_ceiling)
            )
        row_size = lookup_ceiling - MIN_MATERIAL_COUNT + 1
        if offset + row_size > len(values):
            raise ValueError(
                "truncated emblem compound mapping grade=%d" % config_grade
            )
        source_grade = config_grade - 1
        for material_count in range(MIN_MATERIAL_COUNT, lookup_ceiling + 1):
            booster_item_id = int(values[offset])
            offset += 1
            key = (source_grade, material_count)
            if booster_item_id <= 0 or booster_item_id > 0xFFFFFFFF:
                raise ValueError(
                    "invalid emblem compound booster key=%r item=%d"
                    % (key, booster_item_id)
                )
            if key in mappings:
                raise ValueError("duplicate emblem compound mapping key=%r" % (key,))
            mappings[key] = booster_item_id
    if not mappings:
        raise ValueError("emblem compound info has no mappings")
    return mappings


def _parse_booster_stackable_values(text: str) -> list[int]:
    info = _section_lines(text, "booster info")
    child_name = ""
    child_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in info:
        tag = _normalized_tag(line)
        if tag.startswith("[/") and tag.endswith("]"):
            closing_name = tag[2:-1].strip()
            if not child_name or closing_name != child_name:
                raise ValueError("booster child close mismatch=%s" % closing_name)
            sections.append((child_name, child_lines))
            child_name = ""
            child_lines = []
            continue
        if tag.startswith("[") and tag.endswith("]"):
            if child_name:
                raise ValueError("nested booster child=%s" % tag)
            child_name = tag[1:-1].strip()
            child_lines = []
            continue
        if child_name:
            child_lines.append(line)
        elif line:
            raise ValueError("booster info contains values outside a reward child")
    if child_name:
        raise ValueError("unclosed booster child=%s" % child_name)
    if len(sections) != 1 or sections[0][0] != "stackable":
        raise ValueError(
            "emblem compound booster must contain one [stackable] child"
        )
    return _integer_tokens(sections[0][1], context="booster stackable rewards")


@dataclass(frozen=True)
class EmblemCompoundRewardCandidate:
    item_id: int
    weight: int
    count: int
    emblem: AvatarEmblemEntry


@dataclass(frozen=True)
class EmblemCompoundRule:
    source_grade: int
    material_count: int
    booster_item_id: int
    booster_source_path: str
    draw_count: int
    candidates: tuple[EmblemCompoundRewardCandidate, ...]
    total_weight: int

    def roll(
        self,
        *,
        randrange: Callable[[int], int] = secrets.randbelow,
    ) -> tuple[EmblemCompoundRewardCandidate, ...]:
        results: list[EmblemCompoundRewardCandidate] = []
        total = int(self.total_weight)
        if total <= 0:
            raise ValueError("emblem compound rule has no positive weight")
        for _index in range(int(self.draw_count)):
            pick = int(randrange(total))
            if pick < 0 or pick >= total:
                raise ValueError(
                    "emblem compound randrange returned out-of-range value=%d" % pick
                )
            cumulative = 0
            selected: EmblemCompoundRewardCandidate | None = None
            for candidate in self.candidates:
                cumulative += int(candidate.weight)
                if pick < cumulative:
                    selected = candidate
                    break
            if selected is None:
                raise ValueError("emblem compound weighted selection underflow")
            results.append(selected)
        return tuple(results)


def _parse_booster_rule(
    *,
    source_grade: int,
    material_count: int,
    booster_metadata: ItemMetadata,
    booster_text: str,
    emblems_by_id: dict[int, AvatarEmblemEntry],
) -> EmblemCompoundRule:
    booster_item_id = int(booster_metadata.item_id) & 0xFFFFFFFF
    booster_type = _normalized_tag(booster_metadata.stackable_type)
    if (
        booster_item_id <= 0
        or not booster_metadata.is_stackable
        or not booster_type.startswith("[booster]")
    ):
        raise ValueError(
            "emblem compound mapping target is not [booster] item=%d type=%r"
            % (booster_item_id, booster_metadata.stackable_type)
        )
    source_path = str(booster_metadata.source_path or "").strip()
    if not source_path:
        raise ValueError(
            "emblem compound booster source path missing item=%d" % booster_item_id
        )

    values = _parse_booster_stackable_values(booster_text)
    if len(values) < 4 or (len(values) - 1) % 3 != 0:
        raise ValueError(
            "emblem compound booster row width item=%d values=%d"
            % (booster_item_id, len(values))
        )
    draw_count = int(values[0])
    if draw_count <= 0 or draw_count > MAX_RESULT_ROWS:
        raise ValueError(
            "invalid emblem compound draw count item=%d count=%d"
            % (booster_item_id, draw_count)
        )

    candidates: list[EmblemCompoundRewardCandidate] = []
    total_weight = 0
    for offset in range(1, len(values), 3):
        item_id, weight, count = map(int, values[offset : offset + 3])
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError(
                "invalid emblem compound reward item source=%d item=%d"
                % (booster_item_id, item_id)
            )
        if weight <= 0 or weight > MAX_WEIGHT:
            raise ValueError(
                "invalid emblem compound reward weight source=%d item=%d weight=%d"
                % (booster_item_id, item_id, weight)
            )
        if count <= 0 or count > MAX_REWARD_COUNT:
            raise ValueError(
                "invalid emblem compound reward count source=%d item=%d count=%d"
                % (booster_item_id, item_id, count)
            )
        emblem = emblems_by_id.get(item_id)
        if emblem is None:
            raise ValueError(
                "emblem compound booster reward is not a PVF avatar emblem "
                "source=%d item=%d" % (booster_item_id, item_id)
            )
        total_weight += weight
        if total_weight > MAX_WEIGHT:
            raise ValueError(
                "emblem compound total weight overflow source=%d" % booster_item_id
            )
        candidates.append(
            EmblemCompoundRewardCandidate(
                item_id=item_id,
                weight=weight,
                count=count,
                emblem=emblem,
            )
        )
    if not candidates or total_weight <= 0:
        raise ValueError(
            "emblem compound booster has no candidates item=%d" % booster_item_id
        )
    return EmblemCompoundRule(
        source_grade=int(source_grade),
        material_count=int(material_count),
        booster_item_id=booster_item_id,
        booster_source_path=source_path,
        draw_count=draw_count,
        candidates=tuple(candidates),
        total_weight=total_weight,
    )


@dataclass(frozen=True)
class EmblemCompoundConfig:
    rules: dict[tuple[int, int], EmblemCompoundRule]
    emblems_by_id: dict[int, AvatarEmblemEntry]
    highest_emblem_grade: int
    highest_reroll_rule: EmblemCompoundRule

    def resolve_rule(
        self,
        source_grade: int,
        material_count: int,
    ) -> EmblemCompoundRule | None:
        grade = int(source_grade)
        count = int(material_count)
        if not MIN_MATERIAL_COUNT <= count <= MAX_MATERIAL_COUNT:
            return None
        if grade == int(self.highest_emblem_grade):
            return self.highest_reroll_rule
        direct = self.rules.get((grade, count))
        if direct is not None:
            return direct

        # PVF omits repeated terminal boosters while the protocol accepts up to five.
        configured_counts = sorted(
            configured_count
            for configured_grade, configured_count in self.rules
            if configured_grade == grade
        )
        if not configured_counts:
            return None
        if count <= configured_counts[-1]:
            return None
        return self.rules.get((grade, configured_counts[-1]))

    def effective_rule_count(self) -> int:
        source_grades = {key[0] for key in self.rules}
        source_grades.add(int(self.highest_emblem_grade))
        return sum(
            self.resolve_rule(source_grade, material_count) is not None
            for source_grade in source_grades
            for material_count in range(MIN_MATERIAL_COUNT, MAX_MATERIAL_COUNT + 1)
        )

    def resolve_emblem(self, item_id: int) -> AvatarEmblemEntry | None:
        return self.emblems_by_id.get(int(item_id) & 0xFFFFFFFF)


def _build_highest_reroll_rule(
    rules: dict[tuple[int, int], EmblemCompoundRule],
    *,
    highest_emblem_grade: int,
) -> EmblemCompoundRule:
    eligible: list[
        tuple[EmblemCompoundRule, tuple[EmblemCompoundRewardCandidate, ...]]
    ] = []
    for rule in rules.values():
        highest_candidates = tuple(
            EmblemCompoundRewardCandidate(
                item_id=int(candidate.item_id),
                weight=int(candidate.weight),
                count=1,
                emblem=candidate.emblem,
            )
            for candidate in rule.candidates
            if int(candidate.emblem.metadata.grade) == int(highest_emblem_grade)
        )
        if highest_candidates:
            eligible.append((rule, highest_candidates))
    if not eligible:
        raise ValueError(
            "emblem compound has no highest-grade reward candidates grade=%d"
            % int(highest_emblem_grade)
        )

    template, candidates = max(
        eligible,
        key=lambda pair: (
            int(pair[0].source_grade),
            int(pair[0].material_count),
        ),
    )
    total_weight = sum(int(candidate.weight) for candidate in candidates)
    if total_weight <= 0 or total_weight > MAX_WEIGHT:
        raise ValueError(
            "emblem compound highest-grade reroll weight invalid grade=%d"
            % int(highest_emblem_grade)
        )
    return EmblemCompoundRule(
        source_grade=int(highest_emblem_grade),
        material_count=int(template.material_count),
        booster_item_id=int(template.booster_item_id),
        booster_source_path=str(template.booster_source_path),
        draw_count=1,
        candidates=candidates,
        total_weight=total_weight,
    )


def build_emblem_compound_config(
    config_text: str,
    *,
    emblem_index: AvatarEmblemIndex,
    metadata_resolver: Callable[[int], ItemMetadata] = resolve_item_metadata,
    text_reader: Callable[[str], str] = pvf_runtime.read_text,
) -> EmblemCompoundConfig:
    emblems_by_id: dict[int, AvatarEmblemEntry] = {}
    grades_with_emblems: set[int] = set()
    for pool in emblem_index.pools_by_rarity.values():
        for emblem in pool:
            if emblem.item_id in emblems_by_id:
                raise ValueError(
                    "duplicate avatar emblem item id=%d" % emblem.item_id
                )
            emblems_by_id[emblem.item_id] = emblem
            grades_with_emblems.add(int(emblem.metadata.grade))
    if not emblems_by_id:
        raise ValueError("avatar emblem index is empty")

    mappings = parse_emblem_compound_mappings(config_text)
    rules: dict[tuple[int, int], EmblemCompoundRule] = {}
    parsed_boosters: dict[int, tuple[ItemMetadata, str]] = {}
    for key, booster_item_id in sorted(mappings.items()):
        source_grade, material_count = key
        if source_grade not in grades_with_emblems:
            raise ValueError(
                "emblem compound source grade has no PVF emblems grade=%d"
                % source_grade
            )
        cached = parsed_boosters.get(booster_item_id)
        if cached is None:
            metadata = metadata_resolver(booster_item_id)
            source_path = str(metadata.source_path or "").strip()
            if not source_path:
                raise ValueError(
                    "emblem compound booster source path missing item=%d"
                    % booster_item_id
                )
            cached = (metadata, text_reader(source_path))
            parsed_boosters[booster_item_id] = cached
        rules[key] = _parse_booster_rule(
            source_grade=source_grade,
            material_count=material_count,
            booster_metadata=cached[0],
            booster_text=cached[1],
            emblems_by_id=emblems_by_id,
        )
    highest_emblem_grade = max(grades_with_emblems)
    highest_reroll_rule = _build_highest_reroll_rule(
        rules,
        highest_emblem_grade=highest_emblem_grade,
    )
    return EmblemCompoundConfig(
        rules=rules,
        emblems_by_id=emblems_by_id,
        highest_emblem_grade=highest_emblem_grade,
        highest_reroll_rule=highest_reroll_rule,
    )


def install_emblem_compound_config(config: EmblemCompoundConfig) -> None:
    if not isinstance(config, EmblemCompoundConfig) or not config.rules:
        raise ValueError("invalid emblem compound config")
    global _CONFIG
    with _LOCK:
        _CONFIG = config


def clear_emblem_compound_config() -> None:
    global _CONFIG
    with _LOCK:
        _CONFIG = None


def get_emblem_compound_config() -> EmblemCompoundConfig:
    with _LOCK:
        config = _CONFIG
    if config is None:
        raise RuntimeError("emblem compound config is not warmed")
    return config


def warmup_emblem_compound_config() -> dict[str, Any]:
    config = build_emblem_compound_config(
        pvf_runtime.read_text(EMBLEM_COMPOUND_CONFIG_PATH),
        emblem_index=get_avatar_emblem_index(),
    )
    install_emblem_compound_config(config)
    booster_ids = {rule.booster_item_id for rule in config.rules.values()}
    candidate_count = sum(len(rule.candidates) for rule in config.rules.values())
    source_grades = sorted({key[0] for key in config.rules})
    return {
        "ok": 1,
        "mapping_count": len(config.rules),
        "effective_mapping_count": config.effective_rule_count(),
        "booster_count": len(booster_ids),
        "candidate_count": candidate_count,
        "source_grades": source_grades,
        "highest_emblem_grade": int(config.highest_emblem_grade),
        "highest_reroll_booster_id": int(
            config.highest_reroll_rule.booster_item_id
        ),
        "highest_reroll_candidate_count": len(
            config.highest_reroll_rule.candidates
        ),
    }


__all__ = [
    "EMBLEM_COMPOUND_CONFIG_PATH",
    "MAX_MATERIAL_COUNT",
    "MIN_MATERIAL_COUNT",
    "EmblemCompoundConfig",
    "EmblemCompoundRewardCandidate",
    "EmblemCompoundRule",
    "build_emblem_compound_config",
    "clear_emblem_compound_config",
    "get_emblem_compound_config",
    "install_emblem_compound_config",
    "parse_emblem_compound_mappings",
    "warmup_emblem_compound_config",
]
