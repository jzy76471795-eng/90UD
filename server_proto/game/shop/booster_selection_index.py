"""PVF-backed definitions for Stage3 ``[booster selection]`` items."""
from __future__ import annotations

import re
from dataclasses import dataclass


SELECTION_BOOSTER_TYPE_PREFIX = "[booster selection]"
SELECTION_REWARD_TAGS: frozenset[str] = frozenset(
    {
        "avatar",
        "equipment",
        "stackable",
        "etc",
    }
)
_TAG_RE = re.compile(r"^\[(/?)([^\]]+)\]$", re.IGNORECASE)

# The old avatar picker infers the job from the visible item ID. These boxes
# contain newer avatar IDs outside all four native job-range tables.
ARTIFACT_AVATAR_BOXES = frozenset((491000001, 491000014, 491000015, 491000385,
                                   491000386, 491000387, 491000388))

def artifact_avatar_preview_id(box_id: int, item_id: int) -> int | None:
    if box_id not in ARTIFACT_AVATAR_BOXES:
        return None
    base_id = {2026112103: 412510124, 2026112104: 412560117}.get(item_id, item_id)
    if 400000000 <= base_id < 420000000 and base_id % 10000 < 1500:
        return base_id + 100008500
    return None


@dataclass(frozen=True)
class BoosterSelectionReward:
    item_id: int
    count: int
    child_name: str
    wire_avatar: bool = False
    avatar_option_mode: int = 0
    avatar_default_option: int = 0


@dataclass(frozen=True)
class BoosterSelectionCategory:
    category_a: int
    category_b: int
    rewards: tuple[BoosterSelectionReward, ...]


@dataclass(frozen=True)
class BoosterSelectionDefinition:
    source_item_id: int
    stackable_type: str
    category_dimensions: tuple[int, ...]
    selection_count: int
    categories: dict[tuple[int, int], BoosterSelectionCategory]
    source_path: str = ""

    def resolve_category(
        self,
        category_a: int,
        category_b: int,
    ) -> BoosterSelectionCategory | None:
        return self.categories.get(
            (int(category_a) & 0xFF, int(category_b) & 0xFF)
        )

    def reward_item_ids(self) -> tuple[int, ...]:
        return tuple(
            reward.item_id
            for category in self.categories.values()
            for reward in category.rewards
        )


def normalize_selection_stackable_type(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_selection_booster_metadata(metadata: object) -> bool:
    stackable_type = normalize_selection_stackable_type(
        str(getattr(metadata, "stackable_type", "") or "")
    )
    return bool(getattr(metadata, "is_stackable", False)) and (
        stackable_type.startswith(SELECTION_BOOSTER_TYPE_PREFIX)
    )


def _int_tokens(lines: list[str], *, context: str) -> list[int]:
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


def _simple_section_values(text: str, section_name: str) -> list[int]:
    target = "[%s]" % section_name.lower()
    found = False
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.lower() == target:
            if found:
                raise ValueError("duplicate %s section" % target)
            found = True
            continue
        if found:
            if line.startswith("["):
                break
            if line:
                lines.append(line)
    if not found:
        raise ValueError("missing %s section" % target)
    return _int_tokens(lines, context=section_name)


def _parse_category_dimensions(source_item_id: int, text: str) -> tuple[int, ...]:
    values = _simple_section_values(text, "booster category num")
    if not values:
        raise ValueError("empty booster category num item=%d" % source_item_id)
    dimension_count = int(values[0])
    if dimension_count < 0 or dimension_count > 2:
        raise ValueError(
            "unsupported category dimensions item=%d count=%d"
            % (source_item_id, dimension_count)
        )
    if len(values) != dimension_count + 1:
        raise ValueError(
            "category dimension shape item=%d values=%r"
            % (source_item_id, values)
        )
    dimensions = tuple(int(value) for value in values[1:])
    if any(value <= 0 or value > 0x100 for value in dimensions):
        raise ValueError(
            "invalid category dimension item=%d values=%r"
            % (source_item_id, dimensions)
        )
    return dimensions


def _parse_reward_section(
    source_item_id: int,
    category_key: tuple[int, int],
    child_name: str,
    lines: list[str],
) -> list[BoosterSelectionReward]:
    if child_name not in SELECTION_REWARD_TAGS:
        return []
    values = _int_tokens(
        lines,
        context="item=%d category=%r child=%s"
        % (source_item_id, category_key, child_name),
    )
    stride = 4 if child_name == "avatar" else 2
    if not values or len(values) % stride != 0:
        raise ValueError(
            "selection reward shape item=%d category=%r child=%s values=%d stride=%d"
            % (source_item_id, category_key, child_name, len(values), stride)
        )

    rewards: list[BoosterSelectionReward] = []
    for offset in range(0, len(values), stride):
        item_id = int(values[offset])
        count = int(values[offset + 1])
        option_mode = int(values[offset + 2]) if stride == 4 else 0
        default_option = int(values[offset + 3]) if stride == 4 else 0
        rewards.append(
            BoosterSelectionReward(
                item_id=item_id,
                count=count,
                child_name=child_name,
                wire_avatar=(child_name == "avatar"),
                avatar_option_mode=option_mode,
                avatar_default_option=default_option,
            )
        )
    return rewards


def is_deliverable_selection_reward(reward: BoosterSelectionReward) -> bool:
    """Return whether one parsed PVF reward row can be materialized safely."""
    item_id = int(reward.item_id)
    count = int(reward.count)
    child_name = " ".join(str(reward.child_name or "").strip().lower().split())
    option_mode = int(reward.avatar_option_mode)
    default_option = int(reward.avatar_default_option)
    return bool(
        child_name in SELECTION_REWARD_TAGS
        and 0 < item_id <= 0xFFFFFFFF
        and 0 < count <= 0xFFFFFFFF
        and -1 <= option_mode <= 0xFF
        and -1 <= default_option <= 0xFF
        and bool(reward.wire_avatar) == (child_name == "avatar")
    )


def _parse_categories(
    source_item_id: int,
    text: str,
) -> dict[tuple[int, int], BoosterSelectionCategory]:
    raw_lines = [line.strip() for line in str(text or "").splitlines()]
    categories: dict[tuple[int, int], BoosterSelectionCategory] = {}
    index = 0
    while index < len(raw_lines):
        if raw_lines[index].lower() != "[booster select category]":
            index += 1
            continue
        index += 1
        header_lines: list[str] = []
        child_sections: list[tuple[str, list[str]]] = []
        while index < len(raw_lines):
            line = raw_lines[index]
            low = line.lower()
            if low == "[/booster select category]":
                break
            tag = _TAG_RE.fullmatch(low)
            if tag is None:
                if line:
                    header_lines.append(line)
                index += 1
                continue
            if tag.group(1):
                raise ValueError(
                    "orphan selection child close item=%d tag=%s"
                    % (source_item_id, tag.group(2))
                )
            child_name = " ".join(tag.group(2).strip().split())
            close_tag = "[/%s]" % child_name
            child_lines: list[str] = []
            index += 1
            while index < len(raw_lines) and raw_lines[index].lower() != close_tag:
                child_lines.append(raw_lines[index])
                index += 1
            if index >= len(raw_lines):
                raise ValueError(
                    "unclosed selection child item=%d child=%s"
                    % (source_item_id, child_name)
                )
            child_sections.append((child_name, child_lines))
            index += 1
        if index >= len(raw_lines):
            raise ValueError(
                "unclosed booster select category item=%d" % source_item_id
            )

        header = _int_tokens(
            header_lines,
            context="item=%d category header" % source_item_id,
        )
        if len(header) != 2 or any(value < 0 or value > 0xFF for value in header):
            raise ValueError(
                "selection category header shape item=%d values=%r"
                % (source_item_id, header)
            )
        key = (int(header[0]), int(header[1]))
        if key in categories:
            raise ValueError(
                "duplicate selection category item=%d category=%r"
                % (source_item_id, key)
            )
        rewards: list[BoosterSelectionReward] = []
        for child_name, child_lines in child_sections:
            rewards.extend(
                _parse_reward_section(
                    source_item_id,
                    key,
                    child_name,
                    child_lines,
                )
            )
        categories[key] = BoosterSelectionCategory(
            category_a=key[0],
            category_b=key[1],
            rewards=tuple(rewards),
        )
        index += 1
    if not categories:
        raise ValueError(
            "missing booster select category item=%d" % source_item_id
        )
    if not any(category.rewards for category in categories.values()):
        raise ValueError(
            "all selection categories empty item=%d" % source_item_id
        )
    return categories


def parse_booster_selection_text(
    source_item_id: int,
    text: str,
    *,
    stackable_type: str,
    source_path: str = "",
) -> BoosterSelectionDefinition:
    item_id = int(source_item_id) & 0xFFFFFFFF
    normalized_type = normalize_selection_stackable_type(stackable_type)
    if item_id <= 0:
        raise ValueError("invalid selection booster source item=%r" % source_item_id)
    if not normalized_type.startswith(SELECTION_BOOSTER_TYPE_PREFIX):
        raise ValueError(
            "unsupported selection booster type item=%d type=%r"
            % (item_id, stackable_type)
        )

    dimensions = _parse_category_dimensions(item_id, text)
    selection_values = _simple_section_values(text, "booster selection num")
    if len(selection_values) != 1:
        raise ValueError(
            "selection count shape item=%d values=%r"
            % (item_id, selection_values)
        )
    selection_count = int(selection_values[0])
    if selection_count < 0 or selection_count > 0xFF:
        raise ValueError(
            "invalid selection count item=%d count=%d"
            % (item_id, selection_count)
        )
    categories = _parse_categories(item_id, text)

    for key in categories:
        category_a, category_b = key
        if not dimensions:
            valid = key == (0, 0)
        elif len(dimensions) == 1:
            valid = 0 <= category_a < dimensions[0] and category_b == 0
        else:
            valid = (
                0 <= category_a < dimensions[0]
                and 0 <= category_b < dimensions[1]
            )
        if not valid:
            raise ValueError(
                "selection category outside dimensions item=%d category=%r dims=%r"
                % (item_id, key, dimensions)
            )

    return BoosterSelectionDefinition(
        source_item_id=item_id,
        stackable_type=normalized_type,
        category_dimensions=dimensions,
        selection_count=selection_count,
        categories=categories,
        source_path=str(source_path or ""),
    )


__all__ = [
    "BoosterSelectionCategory",
    "BoosterSelectionDefinition",
    "BoosterSelectionReward",
    "SELECTION_BOOSTER_TYPE_PREFIX",
    "SELECTION_REWARD_TAGS",
    "is_selection_booster_metadata",
    "is_deliverable_selection_reward",
    "normalize_selection_stackable_type",
    "parse_booster_selection_text",
]
