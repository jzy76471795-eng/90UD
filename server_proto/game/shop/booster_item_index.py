"""PVF-backed direct-open definitions for Stage3 C1/00A0 booster items."""
from __future__ import annotations

import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from game.character.item_metadata import ItemMetadata, resolve_item_metadata
from game.shop.cera_package_index import get_cera_package_index
from game.shop.cerashop_index import get_cerashop_index
from game.shop.booster_selection_index import (
    BoosterSelectionDefinition,
    is_deliverable_selection_reward,
    is_selection_booster_metadata,
    parse_booster_selection_text,
)
from pvf import pvf_runtime


DIRECT_BOOSTER_TYPE_PREFIXES: tuple[str, ...] = (
    "[booster]",
    "[cera booster]",
    "[booster random]",
)
_TAG_RE = re.compile(r"^\[(/?)([^\]]+)\]$", re.IGNORECASE)
_LOCK = threading.RLock()
_INDEX: "BoosterItemIndex | None" = None
_EXTERNAL_DIRECT_DEFINITIONS: dict[int, "BoosterItemDefinition"] = {}
_EXTERNAL_SELECTION_DEFINITIONS: dict[int, BoosterSelectionDefinition] = {}


@dataclass(frozen=True)
class BoosterRewardCandidate:
    item_id: int
    weight: int
    count: int
    option_value: int = 0


@dataclass(frozen=True)
class BoosterRewardGroup:
    draw_count: int
    candidates: tuple[BoosterRewardCandidate, ...]


@dataclass(frozen=True)
class BoosterSkippedCandidate:
    """A zero-count PVF candidate retained solely for boot diagnostics."""

    source_item_id: int
    source_path: str
    child_name: str
    group_index: int
    candidate_index: int
    item_id: int
    weight: int
    count: int


@dataclass(frozen=True)
class BoosterItemDefinition:
    source_item_id: int
    stackable_type: str
    groups: tuple[BoosterRewardGroup, ...]
    source_path: str = ""
    skipped_zero_count_candidates: tuple[BoosterSkippedCandidate, ...] = ()

    def roll(
        self,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> tuple[BoosterRewardCandidate, ...]:
        rolled: list[BoosterRewardCandidate] = []
        for group in self.groups:
            total_weight = sum(int(row.weight) for row in group.candidates)
            if total_weight <= 0:
                raise ValueError(
                    "booster reward group has no positive weight item=%d"
                    % self.source_item_id
                )
            for _ in range(int(group.draw_count)):
                pick = int(randbelow(total_weight))
                if pick < 0 or pick >= total_weight:
                    raise ValueError("randbelow returned out-of-range value=%d" % pick)
                cumulative = 0
                selected: BoosterRewardCandidate | None = None
                for candidate in group.candidates:
                    cumulative += int(candidate.weight)
                    if pick < cumulative:
                        selected = candidate
                        break
                if selected is None:
                    raise ValueError("booster weighted selection underflow")
                rolled.append(selected)
        return tuple(rolled)


@dataclass(frozen=True)
class BoosterItemIndex:
    definitions: dict[int, BoosterItemDefinition]
    selection_definitions: dict[int, BoosterSelectionDefinition]

    def resolve(self, item_id: int) -> BoosterItemDefinition | None:
        return self.definitions.get(int(item_id) & 0xFFFFFFFF)

    def resolve_selection(
        self,
        item_id: int,
    ) -> BoosterSelectionDefinition | None:
        return self.selection_definitions.get(int(item_id) & 0xFFFFFFFF)


def normalize_booster_stackable_type(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def is_direct_booster_metadata(metadata: object) -> bool:
    stackable_type = normalize_booster_stackable_type(
        str(getattr(metadata, "stackable_type", "") or "")
    )
    return bool(getattr(metadata, "is_stackable", False)) and any(
        stackable_type.startswith(prefix)
        for prefix in DIRECT_BOOSTER_TYPE_PREFIXES
    )


def _parse_int_tokens(lines: list[str]) -> list[int]:
    values: list[int] = []
    for line in lines:
        for token in str(line or "").replace("`", " ").split():
            try:
                values.append(int(token, 0))
            except ValueError:
                continue
    return values


def _booster_info_children(text: str) -> list[tuple[str, list[int]]]:
    children: list[tuple[str, list[int]]] = []
    in_info = False
    child_name = ""
    child_lines: list[str] = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        low = line.lower()
        if low == "[booster info]":
            if in_info:
                raise ValueError("nested [booster info]")
            in_info = True
            continue
        if low == "[/booster info]":
            if not in_info or child_name:
                raise ValueError("invalid [/booster info] boundary")
            in_info = False
            continue
        if not in_info or not line:
            continue

        tag = _TAG_RE.fullmatch(low)
        if tag is None:
            if child_name:
                child_lines.append(line)
            continue

        closing = bool(tag.group(1))
        name = " ".join(tag.group(2).strip().split())
        if closing:
            if not child_name or name != child_name:
                raise ValueError("booster child close mismatch=%s" % name)
            children.append((child_name, _parse_int_tokens(child_lines)))
            child_name = ""
            child_lines = []
        else:
            if child_name:
                raise ValueError("nested booster child=%s" % name)
            child_name = name
            child_lines = []

    if in_info or child_name:
        raise ValueError("unclosed [booster info] section")
    if not children:
        raise ValueError("missing [booster info] rewards")
    return children


def _parse_group(
    source_item_id: int,
    child_name: str,
    values: list[int],
    *,
    source_path: str,
    group_index: int,
    skipped_zero_count_candidates: list[BoosterSkippedCandidate],
) -> BoosterRewardGroup | None:
    if child_name == "avatar":
        stride = 5
    else:
        stride = 3

    start = 0
    draw_count = 1
    if len(values) >= stride + 1 and (len(values) - 1) % stride == 0:
        draw_count = int(values[0])
        start = 1
    elif not values or len(values) % stride != 0:
        raise ValueError(
            "booster child shape item=%d child=%s values=%d stride=%d"
            % (source_item_id, child_name, len(values), stride)
        )
    if draw_count <= 0 or draw_count > 0xFFFF:
        raise ValueError(
            "invalid booster draw count item=%d child=%s draw=%d"
            % (source_item_id, child_name, draw_count)
        )

    candidates: list[BoosterRewardCandidate] = []
    for candidate_index, offset in enumerate(range(start, len(values), stride)):
        item_id = int(values[offset])
        weight = int(values[offset + 1])
        count = int(values[offset + 2])
        option_value = int(values[offset + 4]) if stride == 5 else 0
        if count == 0:
            skipped_zero_count_candidates.append(
                BoosterSkippedCandidate(
                    source_item_id=int(source_item_id),
                    source_path=str(source_path or ""),
                    child_name=str(child_name),
                    group_index=int(group_index),
                    candidate_index=int(candidate_index),
                    item_id=item_id,
                    weight=weight,
                    count=count,
                )
            )
            continue
        if item_id <= 0 or item_id > 0xFFFFFFFF:
            raise ValueError(
                "invalid booster reward item source=%d child=%s item=%d"
                % (source_item_id, child_name, item_id)
            )
        if weight < 0 or weight > 0x7FFFFFFFFFFFFFFF:
            raise ValueError(
                "invalid booster reward weight source=%d item=%d weight=%d"
                % (source_item_id, item_id, weight)
            )
        if count < 0 or count > 0xFFFFFFFF:
            raise ValueError(
                "invalid booster reward count source=%d item=%d count=%d"
                % (source_item_id, item_id, count)
            )
        if option_value < 0 or option_value > 0xFF:
            raise ValueError(
                "invalid booster avatar option source=%d item=%d option=%d"
                % (source_item_id, item_id, option_value)
            )
        candidates.append(
            BoosterRewardCandidate(
                item_id=item_id,
                weight=weight,
                count=count,
                option_value=option_value,
            )
        )
    if not candidates:
        return None
    return BoosterRewardGroup(
        draw_count=draw_count,
        candidates=tuple(candidates),
    )


def parse_booster_item_text(
    source_item_id: int,
    text: str,
    *,
    stackable_type: str,
    source_path: str = "",
) -> BoosterItemDefinition:
    item_id = int(source_item_id) & 0xFFFFFFFF
    normalized_type = normalize_booster_stackable_type(stackable_type)
    if item_id <= 0:
        raise ValueError("invalid booster source item=%r" % source_item_id)
    if not any(
        normalized_type.startswith(prefix)
        for prefix in DIRECT_BOOSTER_TYPE_PREFIXES
    ):
        raise ValueError(
            "unsupported direct booster type item=%d type=%r"
            % (item_id, stackable_type)
        )
    skipped_zero_count_candidates: list[BoosterSkippedCandidate] = []
    groups = tuple(
        group
        for group in (
            _parse_group(
                item_id,
                child_name,
                values,
                source_path=source_path,
                group_index=group_index,
                skipped_zero_count_candidates=skipped_zero_count_candidates,
            )
            for group_index, (child_name, values) in enumerate(
                _booster_info_children(text)
            )
        )
        if group is not None
    )
    return BoosterItemDefinition(
        source_item_id=item_id,
        stackable_type=normalized_type,
        groups=groups,
        source_path=str(source_path or ""),
        skipped_zero_count_candidates=tuple(skipped_zero_count_candidates),
    )


def _load_index_from_shared_reader() -> BoosterItemIndex:
    shop = get_cerashop_index()
    cera_packages = get_cera_package_index()
    pending: deque[int] = deque(
        sorted({int(product.item_id) for product in shop.products.values()})
    )
    for package in cera_packages.packages.values():
        pending.extend(int(reward.item_id) for reward in package.rewards)

    seen: set[int] = set()
    definitions: dict[int, BoosterItemDefinition] = {}
    selection_definitions: dict[int, BoosterSelectionDefinition] = {}
    while pending:
        item_id = int(pending.popleft()) & 0xFFFFFFFF
        if item_id <= 0 or item_id in seen:
            continue
        seen.add(item_id)
        metadata: ItemMetadata = resolve_item_metadata(item_id)
        if not is_direct_booster_metadata(metadata) and not is_selection_booster_metadata(
            metadata
        ):
            continue
        source_path = str(metadata.source_path or "").strip()
        if not source_path:
            raise RuntimeError("booster source path missing item=%d" % item_id)
        source_text = pvf_runtime.read_text(source_path)
        if is_selection_booster_metadata(metadata):
            selection = parse_booster_selection_text(
                item_id,
                source_text,
                stackable_type=metadata.stackable_type,
                source_path=source_path,
            )
            selection_definitions[item_id] = selection
            pending.extend(selection.reward_item_ids())
            continue
        definition = parse_booster_item_text(
            item_id,
            source_text,
            stackable_type=metadata.stackable_type,
            source_path=source_path,
        )
        definitions[item_id] = definition
        for group in definition.groups:
            pending.extend(candidate.item_id for candidate in group.candidates)
    return BoosterItemIndex(
        definitions=definitions,
        selection_definitions=selection_definitions,
    )


def get_booster_item_index() -> BoosterItemIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = _load_index_from_shared_reader()
        return _INDEX


def _resolve_external_direct_booster(item_id: int) -> BoosterItemDefinition | None:
    """Parse one valid non-shop direct booster from the shared PVF on demand."""
    iid = int(item_id) & 0xFFFFFFFF
    with _LOCK:
        cached = _EXTERNAL_DIRECT_DEFINITIONS.get(iid)
        if cached is not None:
            return cached

        metadata = resolve_item_metadata(iid)
        if not is_direct_booster_metadata(metadata):
            return None
        source_path = str(metadata.source_path or "").strip()
        if not source_path:
            return None
        definition = parse_booster_item_text(
            iid,
            pvf_runtime.read_text(source_path),
            stackable_type=metadata.stackable_type,
            source_path=source_path,
        )
        _EXTERNAL_DIRECT_DEFINITIONS[iid] = definition
        return definition


def _resolve_external_selection_booster(
    item_id: int,
) -> BoosterSelectionDefinition | None:
    """Parse one valid non-shop selection booster from the shared PVF on demand."""
    iid = int(item_id) & 0xFFFFFFFF
    with _LOCK:
        cached = _EXTERNAL_SELECTION_DEFINITIONS.get(iid)
        if cached is not None:
            return cached

        metadata = resolve_item_metadata(iid)
        if not is_selection_booster_metadata(metadata):
            return None
        source_path = str(metadata.source_path or "").strip()
        if not source_path:
            return None
        definition = parse_booster_selection_text(
            iid,
            pvf_runtime.read_text(source_path),
            stackable_type=metadata.stackable_type,
            source_path=source_path,
        )
        _EXTERNAL_SELECTION_DEFINITIONS[iid] = definition
        return definition


def resolve_booster_item(item_id: int) -> BoosterItemDefinition | None:
    iid = int(item_id) & 0xFFFFFFFF
    definition = get_booster_item_index().resolve(iid)
    if definition is not None:
        return definition
    return _resolve_external_direct_booster(iid)


def resolve_booster_selection_item(
    item_id: int,
) -> BoosterSelectionDefinition | None:
    iid = int(item_id) & 0xFFFFFFFF
    definition = get_booster_item_index().resolve_selection(iid)
    if definition is not None:
        return definition
    return _resolve_external_selection_booster(iid)


def clear_booster_item_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _EXTERNAL_DIRECT_DEFINITIONS.clear()
        _EXTERNAL_SELECTION_DEFINITIONS.clear()


def install_booster_item_index(index: BoosterItemIndex) -> None:
    if not isinstance(index, BoosterItemIndex):
        raise ValueError("invalid booster item index")
    global _INDEX
    with _LOCK:
        _INDEX = index


def warmup_booster_item_index() -> dict[str, object]:
    index = get_booster_item_index()
    reward_ids: set[int] = set()
    group_count = 0
    skipped_zero_count_candidates: list[BoosterSkippedCandidate] = []
    avatar_reward_count = 0
    job_restricted_rewards = 0
    for definition in index.definitions.values():
        group_count += len(definition.groups)
        skipped_zero_count_candidates.extend(definition.skipped_zero_count_candidates)
        for group in definition.groups:
            for candidate in group.candidates:
                metadata = resolve_item_metadata(candidate.item_id)
                if metadata.item_kind not in {"equipment", "stackable"} or not str(
                    metadata.note or ""
                ).startswith("ok"):
                    raise RuntimeError(
                        "booster reward metadata unresolved source=%d item=%d kind=%s note=%s"
                        % (
                            definition.source_item_id,
                            candidate.item_id,
                            metadata.item_kind,
                            metadata.note,
                        )
                    )
                reward_ids.add(candidate.item_id)
                if metadata.is_avatar:
                    avatar_reward_count += 1
                labels = tuple(metadata.usable_jobs) + tuple(metadata.suitable_jobs)
                normalized = tuple(str(value).strip().lower() for value in labels)
                if normalized and not all(
                    value in {"all", "[all]"} for value in normalized
                ):
                    job_restricted_rewards += 1
    selection_category_count = 0
    selection_reward_count = 0
    for definition in index.selection_definitions.values():
        selection_category_count += len(definition.categories)
        for category in definition.categories.values():
            selection_reward_count += len(category.rewards)
            for reward in category.rewards:
                if not is_deliverable_selection_reward(reward):
                    continue
                metadata = resolve_item_metadata(reward.item_id)
                reward_ids.add(reward.item_id)
                metadata_ok = bool(
                    metadata.item_kind in {"equipment", "stackable"}
                    and str(metadata.note or "").startswith("ok")
                )
                if metadata.is_avatar or (
                    not metadata_ok and reward.child_name == "avatar"
                ):
                    avatar_reward_count += 1
                if not metadata_ok:
                    continue
                labels = tuple(metadata.usable_jobs) + tuple(metadata.suitable_jobs)
                normalized = tuple(str(value).strip().lower() for value in labels)
                if normalized and not all(
                    value in {"all", "[all]"} for value in normalized
                ):
                    job_restricted_rewards += 1
    return {
        "ok": 1,
        "booster_count": len(index.definitions),
        "booster_group_count": group_count,
        "booster_reward_item_count": len(reward_ids),
        "booster_avatar_reward_count": avatar_reward_count,
        "booster_job_restricted_rewards": job_restricted_rewards,
        "booster_selection_count": len(index.selection_definitions),
        "booster_selection_category_count": selection_category_count,
        "booster_selection_reward_count": selection_reward_count,
        "booster_skipped_zero_count_candidate_count": len(
            skipped_zero_count_candidates
        ),
        "booster_skipped_zero_count_candidate_details": tuple(
            "source=%d path=%s child=%s group=%d candidate=%d item=%d weight=%d count=%d"
            % (
                candidate.source_item_id,
                candidate.source_path or "-",
                candidate.child_name,
                candidate.group_index,
                candidate.candidate_index,
                candidate.item_id,
                candidate.weight,
                candidate.count,
            )
            for candidate in skipped_zero_count_candidates
        ),
    }


__all__ = [
    "BoosterItemDefinition",
    "BoosterItemIndex",
    "BoosterRewardCandidate",
    "BoosterRewardGroup",
    "BoosterSkippedCandidate",
    "DIRECT_BOOSTER_TYPE_PREFIXES",
    "clear_booster_item_index_cache",
    "get_booster_item_index",
    "is_direct_booster_metadata",
    "normalize_booster_stackable_type",
    "parse_booster_item_text",
    "resolve_booster_item",
    "resolve_booster_selection_item",
    "warmup_booster_item_index",
]
