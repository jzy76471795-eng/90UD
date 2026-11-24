"""Boot-warmed PVF index for Stage3 Daily Challenge."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from pvf import pvf_runtime
from game.quest.quest_index import (
    QuestMeta,
    _normalize_tag,
    _parse_int_list,
    _section_first,
    _section_text,
    get_quest_index,
)


DAILY_CHALLENGE_TABLE_PATH = "etc/dailychallengetable.etc"
STAGE3_CLAIM_FLAG_COUNT = 5
STAGE3_DYNAMIC_REWARD_THRESHOLD_GROUP_INDEX = 0

_LOCK = threading.RLock()
_INDEX: "DailyChallengeIndex | None" = None


@dataclass(frozen=True)
class DailyChallengeReward:
    item_id: int
    count: int


@dataclass(frozen=True)
class DailyChallengeSlotCount:
    min_level: int
    max_level: int
    count: int

    def contains(self, level: int) -> bool:
        return self.min_level <= int(level) <= self.max_level


@dataclass(frozen=True)
class DailyChallengeSlot:
    slot_number: int
    candidate_ids: tuple[int, ...]


@dataclass(frozen=True)
class DailyChallengeQuest:
    quest_id: int
    path: str
    quest_type: str
    min_level: int
    max_level: int
    target_value: int
    reward_items: tuple[DailyChallengeReward, ...]

    def eligible_at(self, level: int) -> bool:
        return self.min_level <= int(level) <= self.max_level


@dataclass(frozen=True)
class DailyChallengeGroup:
    group_index: int
    pvf_group_id: int
    min_level: int
    max_level: int
    slots: tuple[DailyChallengeSlot, ...]
    slot_counts: tuple[DailyChallengeSlotCount, ...]
    required_completion_count: int
    rewards: tuple[tuple[int, int, DailyChallengeReward], ...]

    def eligible_at(self, level: int) -> bool:
        return self.min_level <= int(level) <= self.max_level

    def active_slot_count(self, level: int) -> int:
        if not self.slot_counts:
            return len(self.slots)
        matches = [row.count for row in self.slot_counts if row.contains(level)]
        if len(matches) != 1:
            raise ValueError(
                "daily challenge group %d has %d slot-count rows for level %d"
                % (self.pvf_group_id, len(matches), int(level))
            )
        return int(matches[0])

    def reward_for_level(self, level: int) -> DailyChallengeReward:
        matches = [
            reward
            for min_level, max_level, reward in self.rewards
            if int(min_level) <= int(level) <= int(max_level)
        ]
        if len(matches) != 1:
            raise ValueError(
                "daily challenge group %d has %d rewards for level %d"
                % (self.pvf_group_id, len(matches), int(level))
            )
        return matches[0]


@dataclass(frozen=True)
class SelectedDailyChallengeEntry:
    entry_index: int
    slot_number: int
    quest_id: int
    target_value: int


@dataclass(frozen=True)
class SelectedDailyChallengeGroup:
    group_index: int
    entries: tuple[SelectedDailyChallengeEntry, ...]


def _parse_table_groups(text: str) -> list[dict[str, object]]:
    """Parse the small nested ETC grammar without flattening repeated slots."""
    known_sections = {
        "level",
        "slot num table",
        "slot",
        "reward challenge num",
        "reward table",
    }
    groups: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    section: str | None = None
    values: list[int] = []

    def flush_section() -> None:
        nonlocal section, values
        if current is None or section is None:
            section = None
            values = []
            return
        if section == "group header":
            if len(values) != 1:
                raise ValueError("daily challenge [group] requires one numeric id")
            current["group_id"] = int(values[0])
        elif section == "slot":
            slots = current.setdefault("slots", [])
            assert isinstance(slots, list)
            slots.append(tuple(int(value) for value in values))
        else:
            if section in current:
                raise ValueError(
                    "daily challenge group %s repeats [%s]"
                    % (current.get("group_id", "?"), section)
                )
            current[section] = tuple(int(value) for value in values)
        section = None
        values = []

    for line_number, raw_line in enumerate((text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            tag = line[1:-1].strip().lower()
            if tag == "group":
                if current is not None:
                    raise ValueError("nested daily challenge [group] at line %d" % line_number)
                current = {"slots": []}
                section = "group header"
                values = []
                continue
            if tag == "/group":
                flush_section()
                if current is None:
                    raise ValueError("orphan daily challenge [/group] at line %d" % line_number)
                groups.append(current)
                current = None
                continue
            if tag.startswith("/"):
                closing = tag[1:]
                if section != closing:
                    raise ValueError(
                        "daily challenge closing [%s] does not match [%s] at line %d"
                        % (tag, section or "none", line_number)
                    )
                flush_section()
                continue
            if current is None:
                raise ValueError(
                    "daily challenge section [%s] outside [group] at line %d"
                    % (tag, line_number)
                )
            if tag not in known_sections:
                raise ValueError(
                    "unknown daily challenge section [%s] at line %d"
                    % (tag, line_number)
                )
            flush_section()
            section = tag
            values = []
            continue

        if current is None or section is None:
            raise ValueError("orphan daily challenge value at line %d" % line_number)
        parsed = _parse_int_list(line)
        if not parsed:
            raise ValueError("non-numeric daily challenge value at line %d" % line_number)
        values.extend(parsed)

    if current is not None:
        raise ValueError("unterminated daily challenge [group]")
    if not groups:
        raise ValueError("daily challenge table has no groups")
    return groups


def _parse_level_range(values: object, *, label: str) -> tuple[int, int]:
    rows = tuple(int(value) for value in (values or ()))
    if len(rows) != 2 or rows[0] <= 0 or rows[1] < rows[0]:
        raise ValueError("%s must contain min_level,max_level" % label)
    return rows[0], rows[1]


def _parse_slot_counts(values: object, *, group_id: int) -> tuple[DailyChallengeSlotCount, ...]:
    rows = tuple(int(value) for value in (values or ()))
    if not rows:
        return ()
    if len(rows) % 3 != 0:
        raise ValueError("daily challenge group %d slot-count width is not 3" % group_id)
    out: list[DailyChallengeSlotCount] = []
    for offset in range(0, len(rows), 3):
        min_level, max_level, count = rows[offset : offset + 3]
        if min_level <= 0 or max_level < min_level or count <= 0:
            raise ValueError("daily challenge group %d has invalid slot-count row" % group_id)
        out.append(DailyChallengeSlotCount(min_level, max_level, count))
    return tuple(out)


def _parse_group_rewards(
    values: object,
    *,
    group_id: int,
) -> tuple[tuple[int, int, DailyChallengeReward], ...]:
    rows = tuple(int(value) for value in (values or ()))
    if not rows or len(rows) % 4 != 0:
        raise ValueError("daily challenge group %d reward-table width is not 4" % group_id)
    out: list[tuple[int, int, DailyChallengeReward]] = []
    for offset in range(0, len(rows), 4):
        min_level, max_level, item_id, count = rows[offset : offset + 4]
        if min_level <= 0 or max_level < min_level or item_id <= 0 or count <= 0:
            raise ValueError("daily challenge group %d has invalid reward row" % group_id)
        out.append((min_level, max_level, DailyChallengeReward(item_id, count)))
    return tuple(out)


def _parse_entry_rewards(meta: QuestMeta) -> tuple[DailyChallengeReward, ...]:
    reward_type = _normalize_tag(_section_first(meta.raw_text, "reward type", ""))
    if reward_type != "item":
        raise ValueError(
            "daily challenge quest %d uses unsupported reward type %r"
            % (meta.quest_id, reward_type)
        )
    values = _parse_int_list(_section_text(meta.raw_text, "reward int data"))
    if not values or len(values) % 2 != 0:
        raise ValueError(
            "daily challenge quest %d reward int data is not item,count pairs"
            % meta.quest_id
        )
    rewards: list[DailyChallengeReward] = []
    for offset in range(0, len(values), 2):
        item_id, count = int(values[offset]), int(values[offset + 1])
        if item_id <= 0 or count <= 0:
            raise ValueError(
                "daily challenge quest %d has invalid item reward %d x%d"
                % (meta.quest_id, item_id, count)
            )
        rewards.append(DailyChallengeReward(item_id, count))
    return tuple(rewards)


def _single_row_target(values: list[int], stride: int, *, meta: QuestMeta) -> int:
    if stride <= 0 or len(values) != stride:
        raise ValueError(
            "daily challenge quest %d type=%s subtype=%d requires one %d-field row, got %d"
            % (meta.quest_id, meta.quest_type, meta.sub_type, stride, len(values))
        )
    target = int(values[-1])
    if target <= 0:
        raise ValueError("daily challenge quest %d has non-positive target" % meta.quest_id)
    return target


def _target_value(meta: QuestMeta) -> int:
    quest_type = _normalize_tag(meta.quest_type)
    values = _parse_int_list(meta.int_data)
    if quest_type == "hunt monster":
        stride = 5 if values and values[0] == -2 else 4
        return _single_row_target(values, stride, meta=meta)
    if quest_type == "condition under clear":
        subtype = int(meta.sub_type)
        strides = {0: 4, 1: 3, 4: 3, 5: 4, 6: 3, 7: 4, 10: 3, 11: 4}
        if subtype == 23:
            stride = 4 if values and values[0] == -2 else 3
        else:
            stride = strides.get(subtype, 0)
        return _single_row_target(values, stride, meta=meta)
    if quest_type == "clear a dungeon":
        if not values or len(values) % 2 != 0:
            raise ValueError(
                "daily challenge quest %d clear-a-dungeon data is not selector,difficulty pairs"
                % meta.quest_id
            )
        return 1
    if quest_type in {"clear quest by grade", "use skill"}:
        if len(values) != 2 or int(values[1]) <= 0:
            raise ValueError(
                "daily challenge quest %d %s requires exactly two values"
                % (meta.quest_id, quest_type)
            )
        return int(values[1])
    if quest_type == "seeking":
        if not values or len(values) % 2 != 0:
            raise ValueError(
                "daily challenge quest %d seeking data is not item,count pairs"
                % meta.quest_id
            )
        return 1
    if quest_type == "disjoint item":
        check_count = _parse_int_list(_section_text(meta.raw_text, "check count"))
        if len(check_count) != 1 or int(check_count[0]) <= 0:
            raise ValueError(
                "daily challenge quest %d disjoint item requires one positive [check count]"
                % meta.quest_id
            )
        return int(check_count[0])
    raise ValueError(
        "daily challenge quest %d has unsupported type %r"
        % (meta.quest_id, quest_type)
    )


class DailyChallengeIndex:
    def __init__(self) -> None:
        self.groups: tuple[DailyChallengeGroup, ...] = ()
        self.quests: dict[int, DailyChallengeQuest] = {}
        self.candidate_ids: frozenset[int] = frozenset()
        self.pvf_sha256 = ""
        self.loaded = False

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        with _LOCK:
            if self.loaded:
                return
            self._load()
            self.loaded = True

    def _load(self) -> None:
        raw_groups = _parse_table_groups(
            pvf_runtime.read_text(DAILY_CHALLENGE_TABLE_PATH)
        )
        if len(raw_groups) != STAGE3_CLAIM_FLAG_COUNT:
            raise ValueError(
                "Stage3 daily challenge requires exactly %d groups, PVF has %d"
                % (STAGE3_CLAIM_FLAG_COUNT, len(raw_groups))
            )

        quest_index = get_quest_index()
        quest_index.ensure_all_metas_loaded()
        groups: list[DailyChallengeGroup] = []
        quests: dict[int, DailyChallengeQuest] = {}
        globally_seen: set[int] = set()
        for group_index, raw in enumerate(raw_groups):
            group_id = int(raw.get("group_id", 0) or 0)
            if group_id != group_index + 1:
                raise ValueError(
                    "daily challenge group order mismatch: index=%d pvf_id=%d"
                    % (group_index, group_id)
                )
            min_level, max_level = _parse_level_range(
                raw.get("level"), label="daily challenge group %d [level]" % group_id
            )
            slots: list[DailyChallengeSlot] = []
            for expected_slot, slot_row in enumerate(raw.get("slots", ()) or (), 1):
                values = tuple(int(value) for value in slot_row)
                if len(values) < 2 or values[0] != expected_slot:
                    raise ValueError(
                        "daily challenge group %d slot order mismatch at %d"
                        % (group_id, expected_slot)
                    )
                candidate_ids = tuple(int(value) for value in values[1:])
                if any(value <= 0 or value > 0xFFFF for value in candidate_ids):
                    raise ValueError(
                        "daily challenge group %d has candidate id outside u16 range"
                        % group_id
                    )
                for quest_id in candidate_ids:
                    if quest_id in globally_seen:
                        raise ValueError(
                            "daily challenge quest %d appears in more than one ETC slot"
                            % quest_id
                        )
                    globally_seen.add(quest_id)
                    meta = quest_index.get_meta(quest_id)
                    if meta is None:
                        raise ValueError(
                            "daily challenge quest %d is not registered by quest.lst"
                            % quest_id
                        )
                    normalized_path = str(meta.path or "").replace("\\", "/").lower()
                    if not normalized_path.startswith("n_quest/dailychallenge/"):
                        raise ValueError(
                            "daily challenge quest %d path is outside n_quest/Dailychallenge: %s"
                            % (quest_id, meta.path)
                        )
                    if _normalize_tag(meta.grade) != "challenge":
                        raise ValueError(
                            "daily challenge quest %d grade is not [challenge]"
                            % quest_id
                        )
                    if max(int(meta.min_level), min_level) > min(
                        int(meta.max_level), max_level
                    ):
                        raise ValueError(
                            "daily challenge quest %d level range does not intersect group %d"
                            % (quest_id, group_id)
                        )
                    quests[quest_id] = DailyChallengeQuest(
                        quest_id=quest_id,
                        path=str(meta.path),
                        quest_type=_normalize_tag(meta.quest_type),
                        min_level=int(meta.min_level),
                        max_level=int(meta.max_level),
                        target_value=_target_value(meta),
                        reward_items=_parse_entry_rewards(meta),
                    )
                slots.append(DailyChallengeSlot(expected_slot, candidate_ids))
            if not slots:
                raise ValueError("daily challenge group %d has no slots" % group_id)

            required_values = tuple(
                int(value) for value in (raw.get("reward challenge num", ()) or ())
            )
            if len(required_values) > 1 or (
                required_values and required_values[0] <= 0
            ):
                raise ValueError(
                    "daily challenge group %d has invalid [reward challenge num]"
                    % group_id
                )
            group = DailyChallengeGroup(
                group_index=group_index,
                pvf_group_id=group_id,
                min_level=min_level,
                max_level=max_level,
                slots=tuple(slots),
                slot_counts=_parse_slot_counts(
                    raw.get("slot num table"), group_id=group_id
                ),
                required_completion_count=(required_values[0] if required_values else 0),
                rewards=_parse_group_rewards(raw.get("reward table"), group_id=group_id),
            )
            groups.append(group)

        self.groups = tuple(groups)
        self.quests = quests
        self.candidate_ids = frozenset(quests)
        self.pvf_sha256 = pvf_runtime.source_sha256()
        self._validate_level_coverage()

    def _validate_level_coverage(self) -> None:
        for group in self.groups:
            if group.slot_counts:
                for row in group.slot_counts:
                    if row.min_level < group.min_level or row.max_level > group.max_level:
                        raise ValueError(
                            "daily challenge group %d slot-count range escapes group level"
                            % group.pvf_group_id
                        )
                    if row.count > len(group.slots):
                        raise ValueError(
                            "daily challenge group %d activates %d of %d slots"
                            % (group.pvf_group_id, row.count, len(group.slots))
                        )
            for level in range(group.min_level, group.max_level + 1):
                active_slots = group.active_slot_count(level)
                eligible_entries = 0
                for slot in group.slots[:active_slots]:
                    matches = [
                        quest_id
                        for quest_id in slot.candidate_ids
                        if self.quests[quest_id].eligible_at(level)
                    ]
                    if matches:
                        eligible_entries += 1
                if eligible_entries <= 0:
                    raise ValueError(
                        "daily challenge group %d has no eligible entries at level %d"
                        % (group.pvf_group_id, level)
                    )
                if group.group_index == STAGE3_DYNAMIC_REWARD_THRESHOLD_GROUP_INDEX:
                    required = max(0, eligible_entries - 1)
                else:
                    required = max(0, int(group.required_completion_count))
                if required > eligible_entries:
                    raise ValueError(
                        "daily challenge group %d requires %d of %d entries at level %d"
                        % (group.pvf_group_id, required, eligible_entries, level)
                    )
                group.reward_for_level(level)

    def is_candidate(self, quest_id: int) -> bool:
        self.ensure_loaded()
        return (int(quest_id) & 0xFFFF) in self.candidate_ids

    def quest(self, quest_id: int) -> DailyChallengeQuest | None:
        self.ensure_loaded()
        return self.quests.get(int(quest_id) & 0xFFFF)

    def group(self, group_index: int) -> DailyChallengeGroup | None:
        self.ensure_loaded()
        index = int(group_index)
        if index < 0 or index >= len(self.groups):
            return None
        return self.groups[index]

    def required_completion_count(
        self,
        group_index: int,
        level: int,
        active_entry_count: int,
    ) -> int:
        group = self.group(group_index)
        if group is None or not group.eligible_at(level):
            return 0
        if group.group_index == STAGE3_DYNAMIC_REWARD_THRESHOLD_GROUP_INDEX:
            return max(0, int(active_entry_count) - 1)
        if group.required_completion_count > 0:
            return int(group.required_completion_count)
        return 0

    def group_reward(self, group_index: int, level: int) -> DailyChallengeReward | None:
        group = self.group(group_index)
        if group is None or not group.eligible_at(level):
            return None
        return group.reward_for_level(level)

    def select_groups(
        self,
        *,
        character_id: int,
        day_key: str,
        level: int,
    ) -> tuple[SelectedDailyChallengeGroup, ...]:
        self.ensure_loaded()
        selected: list[SelectedDailyChallengeGroup] = []
        for group in self.groups:
            if not group.eligible_at(level):
                continue
            active_slot_count = group.active_slot_count(level)
            entries: list[SelectedDailyChallengeEntry] = []
            for slot in group.slots[:active_slot_count]:
                candidates = [
                    quest_id
                    for quest_id in slot.candidate_ids
                    if self.quests[quest_id].eligible_at(level)
                ]
                if not candidates:
                    continue
                seed = "%d|%s|%d|%d" % (
                    int(character_id),
                    str(day_key),
                    int(group.group_index),
                    int(slot.slot_number),
                )
                digest = hashlib.sha256(seed.encode("ascii", errors="strict")).digest()
                quest_id = candidates[int.from_bytes(digest[:8], "little") % len(candidates)]
                definition = self.quests[quest_id]
                entries.append(
                    SelectedDailyChallengeEntry(
                        entry_index=len(entries),
                        slot_number=slot.slot_number,
                        quest_id=quest_id,
                        target_value=definition.target_value,
                    )
                )
            if entries:
                selected.append(
                    SelectedDailyChallengeGroup(
                        group_index=group.group_index,
                        entries=tuple(entries),
                    )
                )
        return tuple(selected)


def get_daily_challenge_index() -> DailyChallengeIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = DailyChallengeIndex()
        return _INDEX


def get_daily_challenge_candidate_ids() -> frozenset[int]:
    """Return the PVF-owned IDs that must stay out of ordinary quest state."""
    index = get_daily_challenge_index()
    index.ensure_loaded()
    return index.candidate_ids


def warmup_daily_challenge_index() -> dict[str, int | str]:
    try:
        index = get_daily_challenge_index()
        index.ensure_loaded()
        slot_count = sum(len(group.slots) for group in index.groups)
        reward_count = sum(len(group.rewards) for group in index.groups)
        return {
            "ok": 1,
            "group_count": len(index.groups),
            "slot_count": slot_count,
            "candidate_count": len(index.candidate_ids),
            "group_reward_count": reward_count,
            "pvf_sha256": index.pvf_sha256[:16],
        }
    except Exception as exc:
        return {"ok": 0, "error": "%s: %s" % (type(exc).__name__, exc)}
