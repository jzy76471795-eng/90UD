"""PVF-backed lottery-item and random-box definitions.

The frozen Stage3 client uses two request families for these containers:

* C1/001B for ``[legacy]`` and ``[upgradable legacy]`` items whose rewards
  are stored in ``[int data]``.
* C1/00D0 for ``[multi upgradable legacy]`` boxes whose independent reward
  groups and optional material cost are stored below ``[random]``.

Normal gameplay resolves the requested container through the shared memory
reader.  The complete ``stackable/stackable.lst`` scan remains available for
maintenance validation, but is not required before a player can enter town.
"""
from __future__ import annotations

import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Iterable

from game.character.item_metadata import resolve_item_metadata
from pvf import pvf_runtime


STACKABLE_LIST_PATH = "stackable/stackable.lst"
EQUIPMENT_LOTTERY_TYPE = "[upgradable legacy]"
ITEM_LOTTERY_TYPE = "[legacy]"
RANDOM_BOX_TYPE = "[multi upgradable legacy]"
RANDOM_BOX_GROUP_NAMES = frozenset(("monthly item", "default list", "random list"))
RANDOM_BOX_CHILD_NAMES = RANDOM_BOX_GROUP_NAMES | frozenset(("sealing removal item",))

_TAG_RE = re.compile(r"^\[(/?)([^\]]+)\]$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"`([^`]*)`|([^\s]+)")
_LST_RE = re.compile(r"(\d+)\s+`([^`]*)`")
_LOCK = threading.RLock()
_INDEX: "RandomContainerIndex | None" = None
_LAZY_RESOLVED_IDS: set[int] = set()


def _normalize_label(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _strip_tick(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1].strip()
    return text


def _parse_int(value: str, *, context: str) -> int:
    try:
        return int(_strip_tick(value), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer %s value=%r" % (context, value)) from exc


def _parse_datetime(value: str, *, context: str) -> datetime:
    raw = _strip_tick(value)
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("invalid datetime %s value=%r" % (context, value)) from exc
    return parsed


def _tokens(lines: Iterable[str]) -> list[str]:
    values: list[str] = []
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        for match in _TOKEN_RE.finditer(line):
            values.append(match.group(1) if match.group(1) is not None else match.group(2))
    return values


def _parse_lst_entries(text: str) -> tuple[tuple[int, str], ...]:
    entries: list[tuple[int, str]] = []
    for match in _LST_RE.finditer(str(text or "")):
        item_id = int(match.group(1))
        relative = str(match.group(2) or "").replace("\\", "/").strip().lstrip("/")
        if item_id <= 0 or not relative:
            continue
        path = relative if relative.lower().startswith("stackable/") else "stackable/" + relative
        entries.append((item_id, path))
    return tuple(entries)


def _first_section_token(text: str, section_name: str) -> str:
    target = _normalize_label(section_name)
    active = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        tag = _TAG_RE.fullmatch(line)
        if tag is not None:
            closing = bool(tag.group(1))
            name = _normalize_label(tag.group(2))
            if not closing and name == target:
                active = True
                continue
            if active:
                break
            continue
        if active and line:
            values = _tokens((line,))
            return _normalize_label(values[0]) if values else ""
    return ""


def _section_token_groups(text: str, section_name: str) -> tuple[tuple[str, ...], ...]:
    target = _normalize_label(section_name)
    groups: list[tuple[str, ...]] = []
    active = False
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        tag = _TAG_RE.fullmatch(line)
        if tag is None:
            if active and line:
                lines.append(line)
            continue
        closing = bool(tag.group(1))
        name = _normalize_label(tag.group(2))
        if not active:
            if not closing and name == target:
                active = True
                lines = []
            continue
        if closing and name == target:
            groups.append(tuple(_tokens(lines)))
            active = False
            lines = []
            continue
        raise ValueError("nested or mismatched [%s] section at tag=%s" % (target, line))
    if active:
        raise ValueError("unclosed [%s] section" % target)
    return tuple(groups)


def _random_children(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    children: list[tuple[str, tuple[str, ...]]] = []
    in_random = False
    random_count = 0
    child_name = ""
    child_lines: list[str] = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        tag = _TAG_RE.fullmatch(line)
        if not in_random:
            if tag is not None and not tag.group(1) and _normalize_label(tag.group(2)) == "random":
                in_random = True
                random_count += 1
            continue
        if tag is None:
            if child_name and line:
                child_lines.append(line)
            continue

        closing = bool(tag.group(1))
        name = _normalize_label(tag.group(2))
        if child_name:
            if not closing or name != child_name:
                raise ValueError("nested or mismatched [random] child tag=%s" % line)
            children.append((child_name, tuple(_tokens(child_lines))))
            child_name = ""
            child_lines = []
            continue

        if closing and name == "random":
            in_random = False
            continue
        if closing:
            continue
        if name not in RANDOM_BOX_CHILD_NAMES:
            # Some boxes place scalar UI text tags directly below [random]
            # without a matching close tag. They are not reward definitions.
            continue
        child_name = name
        child_lines = []

    if in_random or child_name:
        raise ValueError("unclosed [random] section")
    if random_count <= 0:
        raise ValueError("missing [random] section")
    return tuple(children)


def _select_weighted(
    candidates: Iterable["RandomRewardCandidate"],
    *,
    randbelow: Callable[[int], int],
) -> "RandomRewardCandidate":
    eligible = tuple(row for row in candidates if int(row.weight) > 0)
    total_weight = sum(int(row.weight) for row in eligible)
    if total_weight <= 0:
        raise ValueError("reward group has no positive weight")
    pick = int(randbelow(total_weight))
    if pick < 0 or pick >= total_weight:
        raise ValueError("randbelow returned out-of-range value=%d total=%d" % (pick, total_weight))
    cumulative = 0
    for candidate in eligible:
        cumulative += int(candidate.weight)
        if pick < cumulative:
            return candidate
    raise ValueError("weighted selection underflow")


@dataclass(frozen=True)
class RandomRewardCandidate:
    item_id: int
    weight: int
    count: int
    start_time: datetime | None = None
    end_time: datetime | None = None

    def active_at(self, now: datetime) -> bool:
        if self.start_time is None and self.end_time is None:
            return True
        if self.start_time is None or self.end_time is None:
            return False
        return self.start_time <= now <= self.end_time


@dataclass(frozen=True)
class RandomRewardGroup:
    name: str
    candidates: tuple[RandomRewardCandidate, ...]

    @property
    def is_monthly(self) -> bool:
        return self.name == "monthly item"

    def roll(
        self,
        *,
        now: datetime,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> RandomRewardCandidate | None:
        eligible = self.candidates
        if self.is_monthly:
            eligible = tuple(row for row in eligible if row.active_at(now))
            if not eligible:
                return None
        return _select_weighted(eligible, randbelow=randbelow)


@dataclass(frozen=True)
class SealingRemovalItem:
    item_id: int
    count: int


@dataclass(frozen=True)
class LotteryContainerDefinition:
    source_item_id: int
    stackable_type: str
    reward_kind: str
    candidates: tuple[RandomRewardCandidate, ...]
    source_path: str = ""

    def roll(
        self,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> RandomRewardCandidate:
        return _select_weighted(self.candidates, randbelow=randbelow)


@dataclass(frozen=True)
class RandomBoxDefinition:
    source_item_id: int
    stackable_type: str
    groups: tuple[RandomRewardGroup, ...]
    sealing_removal_item: SealingRemovalItem | None = None
    source_path: str = ""

    def roll(
        self,
        *,
        now: datetime | None = None,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> tuple[RandomRewardCandidate, ...]:
        when = now if now is not None else datetime.now()
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        rewards: list[RandomRewardCandidate] = []
        for group in self.groups:
            selected = group.roll(now=when, randbelow=randbelow)
            if selected is not None:
                rewards.append(selected)
        return tuple(rewards)


@dataclass(frozen=True)
class RandomContainerSkipped:
    source_item_id: int
    source_path: str
    stackable_type: str
    reason: str


@dataclass(frozen=True)
class RandomContainerIndex:
    lotteries: dict[int, LotteryContainerDefinition]
    random_boxes: dict[int, RandomBoxDefinition]
    skipped: tuple[RandomContainerSkipped, ...] = ()
    scanned_stackable_count: int = 0
    filtered_reward_count: int = 0

    def resolve_lottery(self, item_id: int) -> LotteryContainerDefinition | None:
        return self.lotteries.get(int(item_id) & 0xFFFFFFFF)

    def resolve_random_box(self, item_id: int) -> RandomBoxDefinition | None:
        return self.random_boxes.get(int(item_id) & 0xFFFFFFFF)


def _validate_candidate_values(
    source_item_id: int,
    candidates: Iterable[RandomRewardCandidate],
    *,
    group_name: str,
) -> tuple[RandomRewardCandidate, ...]:
    normalized = tuple(candidates)
    if not normalized:
        raise ValueError("empty reward group source=%d group=%s" % (source_item_id, group_name))
    for index, row in enumerate(normalized):
        if row.item_id < 0 or row.item_id > 0xFFFFFFFF:
            raise ValueError(
                "invalid reward item source=%d group=%s row=%d item=%d"
                % (source_item_id, group_name, index, row.item_id)
            )
        if row.weight < 0 or row.weight > 0x7FFFFFFFFFFFFFFF:
            raise ValueError(
                "invalid reward weight source=%d group=%s row=%d weight=%d"
                % (source_item_id, group_name, index, row.weight)
            )
        if row.count <= 0 or row.count > 0xFFFFFFFF:
            raise ValueError(
                "invalid reward count source=%d group=%s row=%d count=%d"
                % (source_item_id, group_name, index, row.count)
            )
    if not any(row.weight > 0 for row in normalized):
        raise ValueError("reward group has no positive weight source=%d group=%s" % (source_item_id, group_name))
    return normalized


def parse_lottery_container_text(
    source_item_id: int,
    text: str,
    *,
    stackable_type: str,
    source_path: str = "",
) -> LotteryContainerDefinition:
    item_id = int(source_item_id) & 0xFFFFFFFF
    normalized_type = _normalize_label(stackable_type)
    if item_id <= 0:
        raise ValueError("invalid lottery source item=%r" % source_item_id)
    if normalized_type == EQUIPMENT_LOTTERY_TYPE:
        stride = 3
        reward_kind = "equipment"
    elif normalized_type == ITEM_LOTTERY_TYPE:
        stride = 2
        reward_kind = "item"
    else:
        raise ValueError("unsupported lottery type item=%d type=%r" % (item_id, stackable_type))

    sections = _section_token_groups(text, "int data")
    if not sections:
        raise ValueError("missing [int data] source=%d" % item_id)
    values = tuple(token for section in sections for token in section)
    if not values or len(values) % stride != 0:
        raise ValueError(
            "invalid [int data] shape source=%d tokens=%d stride=%d"
            % (item_id, len(values), stride)
        )

    candidates: list[RandomRewardCandidate] = []
    for offset in range(0, len(values), stride):
        reward_item_id = _parse_int(values[offset], context="lottery item")
        weight = _parse_int(values[offset + 1], context="lottery weight")
        count = _parse_int(values[offset + 2], context="lottery count") if stride == 3 else 1
        candidates.append(
            RandomRewardCandidate(
                item_id=reward_item_id,
                weight=weight,
                count=count,
            )
        )
    return LotteryContainerDefinition(
        source_item_id=item_id,
        stackable_type=normalized_type,
        reward_kind=reward_kind,
        candidates=_validate_candidate_values(item_id, candidates, group_name="int data"),
        source_path=str(source_path or ""),
    )


def _parse_random_reward_group(
    source_item_id: int,
    name: str,
    values: tuple[str, ...],
) -> RandomRewardGroup:
    if not values or len(values) % 4 != 0:
        raise ValueError(
            "invalid random child shape source=%d group=%s tokens=%d"
            % (source_item_id, name, len(values))
        )
    candidates: list[RandomRewardCandidate] = []
    for offset in range(0, len(values), 4):
        reward_item_id = _parse_int(values[offset], context="random item")
        weight = _parse_int(values[offset + 1], context="random weight")
        if name == "monthly item":
            start = _parse_datetime(values[offset + 2], context="monthly start")
            end = _parse_datetime(values[offset + 3], context="monthly end")
            if end < start:
                raise ValueError(
                    "monthly time range reversed source=%d item=%d start=%s end=%s"
                    % (source_item_id, reward_item_id, start, end)
                )
            candidates.append(
                RandomRewardCandidate(
                    item_id=reward_item_id,
                    weight=weight,
                    count=1,
                    start_time=start,
                    end_time=end,
                )
            )
        else:
            count = _parse_int(values[offset + 2], context="random count")
            candidates.append(
                RandomRewardCandidate(
                    item_id=reward_item_id,
                    weight=weight,
                    count=count,
                )
            )
    return RandomRewardGroup(
        name=name,
        candidates=_validate_candidate_values(source_item_id, candidates, group_name=name),
    )


def parse_random_box_text(
    source_item_id: int,
    text: str,
    *,
    stackable_type: str,
    source_path: str = "",
) -> RandomBoxDefinition:
    item_id = int(source_item_id) & 0xFFFFFFFF
    normalized_type = _normalize_label(stackable_type)
    if item_id <= 0:
        raise ValueError("invalid random-box source item=%r" % source_item_id)
    if normalized_type != RANDOM_BOX_TYPE:
        raise ValueError("unsupported random-box type item=%d type=%r" % (item_id, stackable_type))

    groups: list[RandomRewardGroup] = []
    sealing_rows: list[SealingRemovalItem] = []
    for child_name, values in _random_children(text):
        if child_name in RANDOM_BOX_GROUP_NAMES:
            groups.append(_parse_random_reward_group(item_id, child_name, values))
            continue
        if child_name != "sealing removal item":
            continue
        if len(values) != 2:
            raise ValueError(
                "invalid sealing removal shape source=%d tokens=%d" % (item_id, len(values))
            )
        material_id = _parse_int(values[0], context="sealing removal item")
        material_count = _parse_int(values[1], context="sealing removal count")
        if material_id == -1 and material_count == 0:
            continue
        if material_id <= 0 or material_id > 0xFFFFFFFF:
            raise ValueError("invalid sealing removal item source=%d item=%d" % (item_id, material_id))
        if material_count <= 0 or material_count > 0xFFFFFFFF:
            raise ValueError("invalid sealing removal count source=%d count=%d" % (item_id, material_count))
        sealing_rows.append(SealingRemovalItem(item_id=material_id, count=material_count))

    if not groups:
        raise ValueError("random box has no reward groups source=%d" % item_id)
    if len(sealing_rows) > 1:
        raise ValueError("multiple sealing removal items source=%d" % item_id)
    return RandomBoxDefinition(
        source_item_id=item_id,
        stackable_type=normalized_type,
        groups=tuple(groups),
        sealing_removal_item=sealing_rows[0] if sealing_rows else None,
        source_path=str(source_path or ""),
    )


def merge_random_rewards(
    rewards: Iterable[RandomRewardCandidate],
) -> tuple[RandomRewardCandidate, ...]:
    """Merge equal item ids while preserving first-draw display order."""
    merged: "OrderedDict[int, RandomRewardCandidate]" = OrderedDict()
    for reward in rewards:
        current = merged.get(int(reward.item_id))
        if current is None:
            merged[int(reward.item_id)] = RandomRewardCandidate(
                item_id=int(reward.item_id),
                weight=int(reward.weight),
                count=int(reward.count),
            )
            continue
        total = int(current.count) + int(reward.count)
        if total > 0xFFFFFFFF:
            raise ValueError("merged reward count overflow item=%d" % reward.item_id)
        merged[int(reward.item_id)] = RandomRewardCandidate(
            item_id=int(current.item_id),
            weight=int(current.weight),
            count=total,
        )
    return tuple(merged.values())


def _deliverable_candidates(
    candidates: Iterable[RandomRewardCandidate],
    *,
    allow_gold: bool = False,
) -> tuple[tuple[RandomRewardCandidate, ...], int]:
    accepted: list[RandomRewardCandidate] = []
    filtered = 0
    for candidate in candidates:
        if candidate.item_id == 0:
            if allow_gold:
                accepted.append(candidate)
            else:
                filtered += 1
            continue
        metadata = resolve_item_metadata(candidate.item_id)
        if candidate.item_id < 0 or metadata.item_kind not in {"equipment", "stackable"}:
            filtered += 1
            continue
        accepted.append(candidate)
    return tuple(accepted), filtered


def _sanitize_lottery_definition(
    definition: LotteryContainerDefinition,
) -> tuple[LotteryContainerDefinition, int]:
    candidates, filtered = _deliverable_candidates(
        definition.candidates,
        allow_gold=definition.stackable_type == EQUIPMENT_LOTTERY_TYPE,
    )
    if not candidates or not any(row.weight > 0 for row in candidates):
        raise ValueError(
            "lottery has no deliverable positive-weight rewards source=%d"
            % definition.source_item_id
        )
    return replace(definition, candidates=candidates), filtered


def _sanitize_random_box_definition(
    definition: RandomBoxDefinition,
) -> tuple[RandomBoxDefinition, int]:
    filtered_total = 0
    groups: list[RandomRewardGroup] = []
    for group in definition.groups:
        candidates, filtered = _deliverable_candidates(group.candidates)
        filtered_total += filtered
        if not candidates or not any(row.weight > 0 for row in candidates):
            raise ValueError(
                "random box group has no deliverable positive-weight rewards "
                "source=%d group=%s"
                % (definition.source_item_id, group.name)
            )
        groups.append(replace(group, candidates=candidates))
    material = definition.sealing_removal_item
    if material is not None:
        material_meta = resolve_item_metadata(material.item_id)
        if not material_meta.is_stackable:
            raise ValueError(
                "random-box material is not stackable source=%d item=%d kind=%s"
                % (definition.source_item_id, material.item_id, material_meta.item_kind)
            )
    return replace(definition, groups=tuple(groups)), filtered_total


def _load_index_from_shared_reader() -> RandomContainerIndex:
    entries = _parse_lst_entries(pvf_runtime.read_text(STACKABLE_LIST_PATH))
    lotteries: dict[int, LotteryContainerDefinition] = {}
    random_boxes: dict[int, RandomBoxDefinition] = {}
    skipped: list[RandomContainerSkipped] = []
    filtered_reward_count = 0

    for item_id, source_path in entries:
        text = pvf_runtime.read_text(source_path)
        stackable_type = _first_section_token(text, "stackable type")
        if stackable_type not in (EQUIPMENT_LOTTERY_TYPE, ITEM_LOTTERY_TYPE, RANDOM_BOX_TYPE):
            continue
        try:
            if stackable_type == RANDOM_BOX_TYPE:
                definition = parse_random_box_text(
                    item_id,
                    text,
                    stackable_type=stackable_type,
                    source_path=source_path,
                )
                definition, filtered = _sanitize_random_box_definition(definition)
                random_boxes[item_id] = definition
            else:
                definition = parse_lottery_container_text(
                    item_id,
                    text,
                    stackable_type=stackable_type,
                    source_path=source_path,
                )
                definition, filtered = _sanitize_lottery_definition(definition)
                lotteries[item_id] = definition
            filtered_reward_count += int(filtered)
        except ValueError as exc:
            skipped.append(
                RandomContainerSkipped(
                    source_item_id=item_id,
                    source_path=source_path,
                    stackable_type=stackable_type,
                    reason=str(exc),
                )
            )

    return RandomContainerIndex(
        lotteries=lotteries,
        random_boxes=random_boxes,
        skipped=tuple(skipped),
        scanned_stackable_count=len(entries),
        filtered_reward_count=filtered_reward_count,
    )


def get_random_container_index() -> RandomContainerIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None or _INDEX.scanned_stackable_count <= 0:
            _INDEX = _load_index_from_shared_reader()
        return _INDEX


def _load_single_container(
    item_id: int,
) -> tuple[LotteryContainerDefinition | None, RandomBoxDefinition | None, int]:
    template_id = int(item_id) & 0xFFFFFFFF
    if template_id <= 0:
        return None, None, 0
    metadata = resolve_item_metadata(template_id)
    if not bool(getattr(metadata, "is_stackable", False)):
        return None, None, 0
    source_path = str(getattr(metadata, "source_path", "") or "")
    if not source_path:
        return None, None, 0
    text = pvf_runtime.read_text(source_path)
    stackable_type = _first_section_token(text, "stackable type")
    try:
        if stackable_type == RANDOM_BOX_TYPE:
            definition, filtered = _sanitize_random_box_definition(
                parse_random_box_text(
                    template_id,
                    text,
                    stackable_type=stackable_type,
                    source_path=source_path,
                )
            )
            return None, definition, int(filtered)
        if stackable_type in (EQUIPMENT_LOTTERY_TYPE, ITEM_LOTTERY_TYPE):
            definition, filtered = _sanitize_lottery_definition(
                parse_lottery_container_text(
                    template_id,
                    text,
                    stackable_type=stackable_type,
                    source_path=source_path,
                )
            )
            return definition, None, int(filtered)
    except ValueError:
        return None, None, 0
    return None, None, 0


def _resolve_single_container(
    item_id: int,
) -> tuple[LotteryContainerDefinition | None, RandomBoxDefinition | None]:
    """Load only one requested lottery/random box on the normal game path."""
    global _INDEX
    template_id = int(item_id) & 0xFFFFFFFF
    with _LOCK:
        existing = _INDEX
        if existing is not None and existing.scanned_stackable_count > 0:
            return existing.resolve_lottery(template_id), existing.resolve_random_box(template_id)
        if existing is not None:
            lottery = existing.resolve_lottery(template_id)
            random_box = existing.resolve_random_box(template_id)
            if lottery is not None or random_box is not None:
                return lottery, random_box
        if template_id in _LAZY_RESOLVED_IDS:
            return None, None
        _LAZY_RESOLVED_IDS.add(template_id)
        lottery, random_box, filtered = _load_single_container(template_id)
        if lottery is None and random_box is None:
            return None, None
        lotteries = dict(existing.lotteries) if existing is not None else {}
        random_boxes = dict(existing.random_boxes) if existing is not None else {}
        if lottery is not None:
            lotteries[template_id] = lottery
        if random_box is not None:
            random_boxes[template_id] = random_box
        _INDEX = RandomContainerIndex(
            lotteries=lotteries,
            random_boxes=random_boxes,
            skipped=existing.skipped if existing is not None else (),
            scanned_stackable_count=0,
            filtered_reward_count=(
                int(existing.filtered_reward_count) if existing is not None else 0
            ) + int(filtered),
        )
        return lottery, random_box


def resolve_lottery_container(item_id: int) -> LotteryContainerDefinition | None:
    lottery, _random_box = _resolve_single_container(item_id)
    return lottery


def resolve_random_box(item_id: int) -> RandomBoxDefinition | None:
    _lottery, random_box = _resolve_single_container(item_id)
    return random_box


def clear_random_container_index_cache() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _LAZY_RESOLVED_IDS.clear()


def install_random_container_index(index: RandomContainerIndex) -> None:
    if not isinstance(index, RandomContainerIndex):
        raise ValueError("invalid random container index")
    global _INDEX
    with _LOCK:
        _INDEX = index
        _LAZY_RESOLVED_IDS.clear()


def warmup_random_container_index() -> dict[str, int]:
    index = get_random_container_index()
    reward_ids: set[int] = set()
    equipment_lotteries = 0
    item_lotteries = 0
    lottery_rows = 0
    random_box_groups = 0
    monthly_groups = 0
    default_groups = 0
    random_groups = 0
    sealing_boxes = 0

    for definition in index.lotteries.values():
        if definition.reward_kind == "equipment":
            equipment_lotteries += 1
        else:
            item_lotteries += 1
        lottery_rows += len(definition.candidates)
        for candidate in definition.candidates:
            reward_ids.add(candidate.item_id)

    for definition in index.random_boxes.values():
        if definition.sealing_removal_item is not None:
            sealing_boxes += 1
        random_box_groups += len(definition.groups)
        for group in definition.groups:
            if group.name == "monthly item":
                monthly_groups += 1
            elif group.name == "default list":
                default_groups += 1
            elif group.name == "random list":
                random_groups += 1
            for candidate in group.candidates:
                reward_ids.add(candidate.item_id)

    return {
        "ok": 1,
        "scanned_stackable_count": int(index.scanned_stackable_count),
        "lottery_count": len(index.lotteries),
        "equipment_lottery_count": equipment_lotteries,
        "item_lottery_count": item_lotteries,
        "lottery_reward_row_count": lottery_rows,
        "random_box_count": len(index.random_boxes),
        "random_box_group_count": random_box_groups,
        "monthly_group_count": monthly_groups,
        "default_group_count": default_groups,
        "random_group_count": random_groups,
        "sealing_material_box_count": sealing_boxes,
        "reward_item_count": len(reward_ids),
        "filtered_reward_count": int(index.filtered_reward_count),
        "skipped_container_count": len(index.skipped),
    }


__all__ = [
    "EQUIPMENT_LOTTERY_TYPE",
    "ITEM_LOTTERY_TYPE",
    "LotteryContainerDefinition",
    "RANDOM_BOX_TYPE",
    "RandomBoxDefinition",
    "RandomContainerIndex",
    "RandomRewardCandidate",
    "RandomRewardGroup",
    "SealingRemovalItem",
    "clear_random_container_index_cache",
    "get_random_container_index",
    "install_random_container_index",
    "merge_random_rewards",
    "parse_lottery_container_text",
    "parse_random_box_text",
    "resolve_lottery_container",
    "resolve_random_box",
    "warmup_random_container_index",
]
