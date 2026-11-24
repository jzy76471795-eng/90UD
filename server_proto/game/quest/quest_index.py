"""Quest PVF index: acceptable filter, initTrigger, drops, dungeon mazes.

Runtime rule: use shared pvf_runtime reader only (no private PvfReader.open).
Design source: task system doc + s4a12 QuestData flow/names.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from pvf import pvf_runtime
from game.content_blocking import active_blocked_quest_ids

SELECTABLE_GRADES = {
    "",
    "[normal]",
    "[side]",
    "[sub]",
    "[epic]",
    "[training]",
    "[achievement]",
    "[daily]",
    "[daily random]",
    "[normaly repeat]",
    "[special daily]",
    "[circle]",
    "[common unique]",
    "[realization]",
    "[system]",
    # Current PVF wraps these two map-gate grades as ``[`[grade]`\nflag]``.
    "[ancient]",
    "[blackhole]",
}

# These are authored map-gate task families, not generic event/challenge
# tasks.  Keep the list at the PVF grade level rather than pinning quest IDs.
_SPECIAL_MAP_GATE_GRADES = frozenset({"[ancient]", "[blackhole]"})
_DAILY_GRADES = frozenset({"[daily]", "[daily random]", "[special daily]"})
_REPEATABLE_GRADES = _DAILY_GRADES | frozenset({"[normaly repeat]"})
# character/character.lst 0..14 精确标签（与 item_metadata.CHARACTER_JOB_LABELS 对齐）。
# 每个职业只匹配自身标签，禁止把 9/10/13/14 串回鬼剑/法师/守护者基职。
CHARACTER_JOB_TAGS: dict[int, str] = {
    0: "[swordman]",
    1: "[fighter]",
    2: "[gunner]",
    3: "[mage]",
    4: "[priest]",
    5: "[at gunner]",
    6: "[thief]",
    7: "[at fighter]",
    8: "[at mage]",
    9: "[demonic swordman]",
    10: "[creator mage]",
    11: "[at swordman]",
    12: "[knight]",
    13: "[demonic lancer]",
    14: "[at priest]",
}

# 兼容旧名：仅作诊断/对照，资格匹配不再用 base/at 折叠。
JOB_NAMES = ["[swordman]", "[fighter]", "[gunner]", "[mage]", "[priest]", "[thief]", "[knight]"]
AT_JOB_NAMES = ["[at swordman]", "[at fighter]", "[at gunner]", "[at mage]", "[at priest]", "[at thief]", "[at knight]"]

_LOCK = threading.RLock()
_INDEX = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except Exception:
        return int(default)


def _strip_tick(value: str) -> str:
    s = (value or "").strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1].strip()
    return s


def _normalize_tag(value: str) -> str:
    s = _strip_tick(value).strip().lower()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    return s


def _parse_int_list(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", text or "")]


def _parse_lst_pairs(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not text:
        return out
    for m in re.finditer(r"(?m)^\s*(-?\d+)\s+`([^`]+)`", text):
        out[int(m.group(1))] = m.group(2).replace("\\", "/").strip("/")
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not re.fullmatch(r"-?\d+", line):
            continue
        sid = int(line)
        if sid in out:
            continue
        for nxt in lines[i + 1 : i + 4]:
            m = re.search(r"`([^`]+)`", nxt)
            if m:
                out[sid] = m.group(1).replace("\\", "/").strip("/")
                break
    return out


def _section_chunks(text: str, tag: str) -> list[str]:
    """Parse PVF ``[tag]`` sections without duplicating multi-line bodies.

    Rules:
    - Multi-line section: ``[tag]`` alone on a line → body until next ``[`` line
      (``[/tag]`` closing lines are stripped).
    - Same-line section: ``[tag] rest...`` only when the rest is non-empty on the
      *same physical line* (must not treat newline after ``]`` as same-line).
    - Multiple sections of the same tag are preserved as separate chunks
      (e.g. several ``[pre required quest]`` groups → OR semantics).
    """
    out: list[str] = []
    src = text or ""
    # Same-line only: horizontal whitespace after ], not newline.
    same_line_re = re.compile(
        rf"(?im)^\s*\[{re.escape(tag)}\][ \t]+(.+?)\s*$"
    )
    multi_line_re = re.compile(rf"(?im)^\s*\[{re.escape(tag)}\]\s*$")
    # Collect by source offset so multi + same-line stay in file order.
    events: list[tuple[int, str]] = []
    for m in multi_line_re.finditer(src):
        start = m.end()
        rest = src[start:]
        end_m = re.search(r"(?m)^\s*\[", rest)
        chunk = rest[: end_m.start()] if end_m else rest
        # Drop explicit closing ``[/tag]`` remnants and blank-only tails.
        cleaned_lines: list[str] = []
        for ln in chunk.splitlines():
            if re.match(rf"(?i)^\s*\[/{re.escape(tag)}\]\s*$", ln):
                continue
            cleaned_lines.append(ln)
        chunk = "\n".join(cleaned_lines).strip()
        events.append((m.start(), chunk))
    for m in same_line_re.finditer(src):
        events.append((m.start(), m.group(1).strip()))
    events.sort(key=lambda x: x[0])
    for _pos, chunk in events:
        if chunk:
            out.append(chunk)
    return out


def _section_first(text: str, tag: str, default: str = "") -> str:
    chunks = _section_chunks(text, tag)
    return chunks[0] if chunks else default


def _section_text(text: str, tag: str) -> str:
    return "\n".join(_section_chunks(text, tag))


@dataclass(frozen=True)
class ConnectQuestUnit:
    """One bounded ``[connect quest unit]`` from connectquestlist.lst.

    ``[sequence]`` is an ordered story presentation list.  Branch edges still
    come from each QST's prerequisite, answer and collision fields; adjacent
    sequence rows are not necessarily both mandatory.
    """

    unit_type: str
    parent_key: int
    worldmap_index: int
    name: str
    level_section: tuple[int, ...]
    sequence: tuple[int, ...]


_CONNECT_QUEST_UNIT_RE = re.compile(
    r"(?ims)^\s*\[connect\s+quest\s+unit\]\s*$"
    r"(.*?)"
    r"^\s*\[/connect\s+quest\s+unit\]\s*$"
)


def _parse_connect_quest_units(text: str) -> tuple[ConnectQuestUnit, ...]:
    """Parse complete connect-quest units without flattening nested fields."""
    units: list[ConnectQuestUnit] = []
    for match in _CONNECT_QUEST_UNIT_RE.finditer(str(text or "")):
        chunk = match.group(1)
        sequence: list[int] = []
        seen: set[int] = set()
        for raw_quest_id in _parse_int_list(_section_text(chunk, "sequence")):
            quest_id = int(raw_quest_id)
            if quest_id <= 0 or quest_id > 0xFFFF:
                raise ValueError(
                    "connectquestlist sequence quest_id out of u16 range: %s"
                    % raw_quest_id
                )
            if quest_id in seen:
                raise ValueError(
                    "connectquestlist duplicate quest_id=%s inside one sequence"
                    % quest_id
                )
            seen.add(quest_id)
            sequence.append(quest_id)
        parent_values = _parse_int_list(_section_text(chunk, "parent key"))
        worldmap_values = _parse_int_list(_section_text(chunk, "worldmap index"))
        units.append(
            ConnectQuestUnit(
                unit_type=_normalize_tag(_section_first(chunk, "type", "")),
                parent_key=int(parent_values[0]) if parent_values else -1,
                worldmap_index=int(worldmap_values[0]) if worldmap_values else -1,
                name=_strip_tick(_section_first(chunk, "name", "")),
                level_section=tuple(
                    _parse_int_list(_section_text(chunk, "level section"))
                ),
                sequence=tuple(sequence),
            )
        )
    if not units:
        raise ValueError("n_quest/connectquestlist.lst has no connect quest units")
    return tuple(units)


def _character_job_tag(character_job: int) -> str | None:
    """Exact PVF job tag for character.lst id, or None if unknown."""
    return CHARACTER_JOB_TAGS.get(int(character_job))


def _base_job_index(job: int) -> int:
    """Legacy base-class index (diagnostic only; not used for eligibility)."""
    mapping = {
        0: 0,
        1: 1,
        2: 2,
        3: 3,
        4: 4,
        5: 2,
        6: 5,
        7: 1,
        8: 3,
        9: 0,
        10: 3,
        11: 0,
        12: 6,
        13: -1,
        14: 4,
    }
    return mapping.get(int(job), -1)


def _is_at_variant(job: int) -> bool:
    return int(job) in {5, 7, 8, 11, 14}


def _job_tag_tokens(job_str: str) -> list[str]:
    """Extract ``[tag]`` tokens from a job / target field; keep bare text as one token."""
    job_str = (job_str or "").strip().lower()
    if not job_str:
        return []
    tokens = re.findall(r"\[[^\]]+\]", job_str)
    if tokens:
        return [t.strip().lower() for t in tokens]
    bare = _normalize_tag(job_str)
    if not bare:
        return []
    return [f"[{bare}]"]


def _matches_job(job_str: str, character_job: int) -> bool:
    """Match ``[job]`` restriction by exact character.lst tag.

    - empty / ``[all]`` → open to any job
    - restricted job field + unknown character job id → no match
    - otherwise require the character's exact tag among listed tags
    """
    job_str = (job_str or "").strip().lower()
    if not job_str or job_str == "[all]":
        return True
    tokens = _job_tag_tokens(job_str)
    if not tokens or any(t == "[all]" for t in tokens):
        return True
    own = _character_job_tag(character_job)
    if own is None:
        # Restricted quest + unknown job id → do not open.
        return False
    return own in tokens


def _split_grow_type(grow_type: int) -> tuple[int, int]:
    """Packed grow_type: low nibble = first job branch, high nibble = awakening."""
    g = int(grow_type) & 0xFF
    return g & 0x0F, (g >> 4) & 0x0F


@dataclass(frozen=True)
class TargetCharacterRule:
    """One ``[target character]`` triple: job tag, first-grow, awakening (-1 = any)."""

    job_tag: str
    first_grow: int = -1
    awakening: int = -1


def _parse_target_character_rules(text: str) -> list[TargetCharacterRule]:
    """Parse ``[target character]`` body into (job, first_grow, awakening) groups.

    Tokens alternate: job tag (string/backtick), int, int. Multi-group OR.
    ``-1`` on either int dimension means wildcard.
    """
    raw = text or ""
    # Keep backtick strings and bare ints in order.
    tokens: list[Any] = []
    for m in re.finditer(r"`([^`]*)`|(\[[^\]]+\])|(-?\d+)", raw):
        if m.group(1) is not None:
            tokens.append(m.group(1).strip())
        elif m.group(2) is not None:
            tokens.append(m.group(2).strip())
        else:
            tokens.append(int(m.group(3)))
    rules: list[TargetCharacterRule] = []
    i = 0
    while i + 2 < len(tokens):
        job_tok = tokens[i]
        g1 = tokens[i + 1]
        g2 = tokens[i + 2]
        if not isinstance(job_tok, str) or not isinstance(g1, int) or not isinstance(g2, int):
            i += 1
            continue
        tag = job_tok.strip().lower()
        if not tag:
            i += 1
            continue
        if not (tag.startswith("[") and tag.endswith("]")):
            bare = _normalize_tag(tag)
            tag = f"[{bare}]" if bare else tag
        rules.append(
            TargetCharacterRule(
                job_tag=tag,
                first_grow=int(g1),
                awakening=int(g2),
            )
        )
        i += 3
    return rules


def _matches_target_character_rules(
    rules: Sequence[TargetCharacterRule],
    character_job: int,
    grow_type: int = 0,
) -> bool:
    """Any complete rule match → ok. Empty rules → no restriction."""
    if not rules:
        return True
    first_grow, awakening = _split_grow_type(grow_type)
    own = _character_job_tag(character_job)
    if own is None:
        return False
    for rule in rules:
        tag = (rule.job_tag or "").strip().lower()
        if tag and tag != own:
            continue
        if int(rule.first_grow) != -1 and int(rule.first_grow) != int(first_grow):
            continue
        if int(rule.awakening) != -1 and int(rule.awakening) != int(awakening):
            continue
        return True
    return False


def _matches_target_character(
    target: str,
    character_job: int,
    grow_type: int = 0,
    rules: Sequence[TargetCharacterRule] | None = None,
) -> bool:
    """Prefer structured rules; fall back to job-tag-only text match."""
    if rules is not None:
        if rules:
            return _matches_target_character_rules(rules, character_job, grow_type)
        # Explicit empty rules + empty text → open.
        if not (target or "").strip():
            return True
    target = (target or "").strip().lower()
    if not target:
        return True
    # Legacy text-only path: job tags without grow/awakening numbers.
    parsed = _parse_target_character_rules(target)
    if parsed:
        return _matches_target_character_rules(parsed, character_job, grow_type)
    return _matches_job(target, character_job)


def _pack_trigger(f0: int, f1: int = 0, f2: int = 0) -> int:
    return ((int(f2) & 0x1FF) << 18) | ((int(f1) & 0x1FF) << 9) | (int(f0) & 0x1FF)


def _trigger_from_int_data(int_data: str, stride: int) -> int:
    values = _parse_int_list(int_data)
    if not values or stride <= 0:
        return 1
    count_offset = stride - 1
    channels: list[int] = []
    for i in range(0, len(values) - stride + 1, stride):
        channels.append(values[i + count_offset])
    if not channels:
        return 1
    f0 = channels[0] if len(channels) > 0 else 0
    f1 = channels[1] if len(channels) > 1 else 0
    f2 = channels[2] if len(channels) > 2 else 0
    return _pack_trigger(f0, f1, f2)


def _condition_under_clear_trigger(meta: "QuestMeta") -> int:
    """Build the initial residual using the PVF layout for this clear subtype.

    Normal clear (sub type 6) has ``dungeon, difficulty, count`` triples.
    Hell Party uses ``-2, worldmap, difficulty, count`` for region rules and
    ``selector, difficulty, count`` otherwise.  The remaining subtypes retain
    the historical four-field layout until their settlement facts are modeled.
    """
    subtype = int(meta.sub_type)
    values = _parse_int_list(meta.int_data)
    if subtype == 6:
        return _trigger_from_int_data(meta.int_data, 3)
    if subtype == 23 and values and values[0] != -2:
        return _trigger_from_int_data(meta.int_data, 3)
    return _trigger_from_int_data(meta.int_data, 4)


@dataclass
class MonsterRewardEntry:
    monster_code: int
    dungeon_id: int = -1
    difficulty: int = -1
    item_id: int = 0
    count: int = 1
    drop_rate: int = 100
    max_stack: int = 1


@dataclass
class EnemyRewardEntry:
    """PVF ``[enemy reward item]``：8 元组。

    enemy_code, enemy_type, dungeon_id, difficulty, item_id, count, drop_rate, max_stack
    enemy_type: 1=monster 2=ai 3=passive object（S4A12 QuestDropProvider）
    """

    enemy_code: int
    enemy_type: int = 1
    dungeon_id: int = -1
    difficulty: int = -1
    item_id: int = 0
    count: int = 1
    drop_rate: int = 100
    max_stack: int = 1


@dataclass
class SpecialPassiveObjectItem:
    """One item in a DGN drop group referenced by MAP ``[item] group``.

    ``index`` is the authored drop-group id, never a building instance id.
    Several special-passive instances may reference the same group. Repeated
    declarations in the same scope retain all their configured item rows.
    ``source_group_index`` records declaration order for diagnostics only.

    DropRate 量级 10000（roll Next(10000) < rate）。
    """

    # DGN drop-group id, matched against MAP special-passive [item] references.
    index: int = 0
    # Obsolete pre-fix name retained only for positional-call compatibility.
    # It is never used to select a building or a drop.
    level_override: int = 0
    item_id: int = 0
    drop_rate: int = 0
    # Source declaration order is diagnostic metadata, not a MAP index.
    source_group_index: int = 0
    # Retain the PVF field until its remaining client-side meaning is closed.
    flag: int = -1


@dataclass
class QuestMeta:
    quest_id: int
    path: str
    name: str = ""
    grade: str = ""
    quest_type: str = ""
    sub_type: int = -1
    min_level: int = 1
    max_level: int = 99
    job: str = ""
    grow_type: int = -1
    jcq: int = 0
    exposed: int = -1
    first_exposed: int = -1
    is_event: bool = False
    creature_kind: int = -1
    expert_job_type: int = -1
    expert_job_level: int = -1
    target_character: str = ""
    # Parsed ``[target character]`` triples; raw text kept in target_character.
    target_character_rules: list[TargetCharacterRule] = field(default_factory=list)
    npc_index: int = -1
    int_data: str = ""
    dungeon_info: tuple[int, ...] = ()
    dialog_map_ids: tuple[int, ...] = ()
    dialog_apc_codes: tuple[int, ...] = ()
    pre_groups: list[list[int]] = field(default_factory=list)
    pre_required_answers: list[tuple[int, int]] = field(default_factory=list)
    question_answer_count: int = 0
    collision: list[int] = field(default_factory=list)
    monster_rewards: list[MonsterRewardEntry] = field(default_factory=list)
    enemy_rewards: list[EnemyRewardEntry] = field(default_factory=list)
    raw_text: str = ""


@dataclass(frozen=True)
class SeekingCaptureSpec:
    """PVF ``[seeking]`` task whose supplied throw item captures a boss.

    The frozen Stage3 form has exactly two ``[int data]`` item pairs.  One is
    also present in ``[depend give item]`` and is a ``[throw]`` stackable;
    the other is the collected capture result.  This deliberately excludes
    ordinary seeking tasks which happen to grant a quest item on acceptance.
    """

    quest_id: int
    dungeon_id: int
    tool_item_id: int
    tool_count: int
    output_item_id: int
    output_count: int


def _worldmap_hunt_monster_values(meta: QuestMeta | None) -> list[int] | None:
    """Return the verified five-field regional hunt layout, if present.

    Frozen Stage3 PVF has a small non-epic family whose ``[hunt monster]``
    ``[int data]`` rows are ``-2, worldmap, difficulty, selector, count``.
    The first field selects a world-map scope; unlike the ordinary
    ``dungeon, difficulty, monster, count`` form, the residual count is the
    fifth field.
    """
    if meta is None or _normalize_tag(meta.quest_type) != "hunt monster":
        return None
    values = _parse_int_list(meta.int_data)
    if not values or len(values) % 5 != 0:
        return None
    for offset in range(0, len(values), 5):
        if int(values[offset]) != -2 or int(values[offset + 4]) <= 0:
            return None
    return values


def hunt_monster_initial_trigger(meta: QuestMeta | None) -> int | None:
    """Build a hunt-monster residual from its verified Stage3 PVF layout."""
    if meta is None or _normalize_tag(meta.quest_type) != "hunt monster":
        return None
    if _worldmap_hunt_monster_values(meta) is not None:
        return _trigger_from_int_data(meta.int_data, 5)
    return _trigger_from_int_data(meta.int_data, 4)


def repair_worldmap_hunt_monster_trigger(
    meta: QuestMeta | None,
    trigger_value: int,
) -> int:
    """Clamp impossible regional hunt channels without resetting real progress.

    Earlier builds read the selector (the fourth field) as a count.  Negative
    selectors therefore entered the packed nine-bit channel as values near
    511.  A remaining count can never exceed the PVF initial count, so only
    oversaturated configured channels are corrected; lower partial progress
    and unrelated channels are preserved.
    """
    current = int(trigger_value) & 0xFFFFFFFF
    values = _worldmap_hunt_monster_values(meta)
    if values is None:
        return current

    repaired = current
    for channel, offset in enumerate(range(0, len(values), 5)):
        if channel >= 3:
            break
        expected = int(values[offset + 4]) & 0x1FF
        if expected <= 0:
            continue
        shift = channel * 9
        actual = (repaired >> shift) & 0x1FF
        if actual <= expected:
            continue
        repaired = (repaired & ~(0x1FF << shift)) | (expected << shift)
    return repaired & 0xFFFFFFFF


def quest_clear_required_quest_ids(meta: QuestMeta | None) -> tuple[int, ...]:
    """Return the referenced quests from one ``[quest clear]`` condition."""
    if meta is None or _normalize_tag(meta.quest_type) not in {
        "quest clear",
        "clear quest",
    }:
        return ()
    return tuple(
        int(value) & 0xFFFF
        for value in _parse_int_list(meta.int_data)
        if int(value) > 0
    )


def quest_clear_trigger(
    meta: QuestMeta | None,
    finished_quest_ids: Sequence[int] = (),
) -> int | None:
    """Compute a quest-completion dependency residual from finished flags.

    Subtype 1 is the mutually exclusive branch form used by epic quest 2899:
    any referenced quest satisfies it.  Legacy rows have one referenced quest
    and retain ordinary all-required counting semantics.
    """
    required = quest_clear_required_quest_ids(meta)
    if meta is None or _normalize_tag(meta.quest_type) not in {
        "quest clear",
        "clear quest",
    }:
        return None
    if not required:
        return 1
    finished = {
        int(quest_id) & 0xFFFF
        for quest_id in finished_quest_ids or ()
        if int(quest_id) > 0
    }
    if int(meta.sub_type) == 1:
        return 0 if any(quest_id in finished for quest_id in required) else 1
    return sum(1 for quest_id in required if quest_id not in finished)


def use_item_targets(meta: QuestMeta | None) -> tuple[tuple[int, int], ...]:
    """Parse ``targetCount, (itemId, useCount) * targetCount``."""
    if meta is None or _normalize_tag(meta.quest_type) != "use item":
        return ()
    values = _parse_int_list(meta.int_data)
    if not values or int(values[0]) <= 0:
        return ()
    declared = int(values[0])
    targets: list[tuple[int, int]] = []
    for offset in range(1, min(len(values), 1 + declared * 2), 2):
        if offset + 1 >= len(values):
            break
        item_id = int(values[offset])
        count = int(values[offset + 1])
        if item_id > 0 and count > 0:
            targets.append((item_id, count))
    return tuple(targets)


def use_item_initial_trigger(meta: QuestMeta | None) -> int | None:
    if meta is None or _normalize_tag(meta.quest_type) != "use item":
        return None
    targets = use_item_targets(meta)
    # Frozen main/achievement subtype 1 rows each have one explicit target.
    # Legacy title rows list many alternatives and historically use one raw
    # trigger, so retain that behavior outside the proven single-target form.
    if int(meta.sub_type) == 1 and len(targets) == 1:
        return max(1, int(targets[0][1]))
    return 1


def seeking_capture_spec(meta: QuestMeta | None) -> SeekingCaptureSpec | None:
    """Parse the Stage3 scanner/capture seeking shape without ID hard-coding.

    Quest 2257 and its mirrored row 15268 declare ``3315,1,6056,1`` while
    supplying 6056 through ``[depend give item]``.  Item 6056 is a native
    throw/capture tool, not an inventory requirement; 3315 is the resulting
    record capsule.  The same distinction must be available to both the
    residual calculator and C1/002C before either mutates inventory state.
    """
    if meta is None or _normalize_tag(meta.quest_type) != "seeking":
        return None
    try:
        dungeon_id = int((meta.dungeon_info or (0,))[0])
    except (TypeError, ValueError):
        return None
    if dungeon_id <= 0:
        return None

    values = _parse_int_list(meta.int_data)
    if len(values) != 4:
        return None
    requirements = (
        (int(values[0]), int(values[1])),
        (int(values[2]), int(values[3])),
    )
    if any(item_id <= 0 or count <= 0 for item_id, count in requirements):
        return None

    depend_values = _parse_int_list(_section_text(meta.raw_text, "depend give item"))
    if len(depend_values) != 2:
        return None
    tool_pair = (int(depend_values[0]), int(depend_values[1]))
    if tool_pair not in requirements or tool_pair[0] <= 0 or tool_pair[1] <= 0:
        return None

    output_pair = requirements[1] if requirements[0] == tool_pair else requirements[0]
    try:
        # Keep normal supplied quest materials out of this path.  The native
        # client owns the capture target/HP check for a throw item; the server
        # then persists only its proven item and residual consequences.
        from game.character.item_metadata import resolve_item_metadata

        tool_meta = resolve_item_metadata(int(tool_pair[0]))
        stackable_type = str(getattr(tool_meta, "stackable_type", "") or "").strip().lower()
    except Exception:
        return None
    if not stackable_type.startswith("[throw]"):
        return None

    return SeekingCaptureSpec(
        quest_id=int(meta.quest_id) & 0xFFFF,
        dungeon_id=int(dungeon_id),
        tool_item_id=int(tool_pair[0]),
        tool_count=int(tool_pair[1]),
        output_item_id=int(output_pair[0]),
        output_count=int(output_pair[1]),
    )


def question_answer_count(meta: QuestMeta | None) -> int | None:
    if meta is None or _normalize_tag(getattr(meta, "quest_type", "")) != "question":
        return None
    return max(0, int(getattr(meta, "question_answer_count", 0)))


def resolve_question_answer_flag(
    meta: QuestMeta | None,
    *,
    trigger_value: int,
    reward_select_idx: int = -1,
) -> int | None:
    """Map a completed question choice to its persisted one-based flag."""
    answer_count = question_answer_count(meta)
    if answer_count is None:
        return None
    trigger = int(trigger_value) & 0xFFFFFFFF
    if answer_count <= 0:
        return 1 if trigger == 0 else None
    if trigger == 0:
        return 1
    if 1 <= trigger <= answer_count:
        return trigger
    selected = int(reward_select_idx)
    if 0 <= selected < answer_count:
        return selected + 1
    return None


@dataclass(frozen=True)
class MazeClearCondition:
    """One target/count pair from a maze ``[clear condition]`` block."""

    kind: str
    target_id: int
    count: int


@dataclass(frozen=True)
class MazeRidableObject:
    """One interactive vehicle from maze ``[randomized object creation]``."""

    map_x: int
    map_y: int
    object_index: int
    pos_x: int
    pos_y: int
    faction: int = 0


@dataclass(frozen=True)
class MazeRidableScript:
    """Maze-level selection rules for interactive vehicle objects."""

    select_count: int = 0
    regenerate: bool = False
    minimap_icon: int = 0
    objects: tuple[MazeRidableObject, ...] = ()


@dataclass
class MazeInfo:
    index: int
    quest_connection: tuple[int, ...] | None = None
    clear_conditions: list[MazeClearCondition] = field(default_factory=list)
    open_door_even_enemy: bool = False
    start_maps: list[tuple[int, int]] = field(default_factory=list)
    boss_maps: list[tuple[int, int]] = field(default_factory=list)
    map_specs: list[dict] = field(default_factory=list)
    name: str = ""
    escape_hell: bool = False
    ridable_script: MazeRidableScript | None = None
    # None inherits the dungeon table; [] is an explicitly empty local table.
    special_passive_object_items: list[SpecialPassiveObjectItem] | None = None


def passive_object_items_for_maze(
    dungeon: Any, maze_index: int,
) -> list[SpecialPassiveObjectItem]:
    """Select the active maze's table instead of merging unrelated mazes."""
    if dungeon is None:
        return []
    mazes = list(getattr(dungeon, "mazes", None) or [])
    for maze in mazes:
        if int(maze.index) == int(maze_index):
            local = getattr(maze, "special_passive_object_items", None)
            if local is not None:
                return list(local)
            break
    else:
        if mazes:
            return []
    return list(getattr(dungeon, "special_passive_object_items", None) or [])


_MAZE_CLEAR_CONDITION_KINDS = (
    "destroy object",
    "seeking",
    "hunt monster",
    "hunt apc",
    "hunt boss",
)


def _parse_maze_open_door_even_enemy(block: str) -> bool:
    """A literal zero disables the flag; retain the legacy bare-tag form."""
    if not re.search(r"(?im)^\s*\[open\s+door\s+even\s+enemy\]\s*$", block):
        return False
    values = _parse_int_list(_section_text(block, "open door even enemy"))
    return bool(values[0]) if values else True


def _parse_maze_clear_conditions(block: str) -> list[MazeClearCondition]:
    """Parse nested maze clear-condition sections in source order.

    The general section helper intentionally stops at the next PVF tag, so an
    outer ``[clear condition]`` needs a dedicated pass before its child
    target/count sections can be read.
    """
    out: list[MazeClearCondition] = []
    outer_re = re.compile(
        r"(?is)\[clear\s+condition\](.*?)\[/clear\s+condition\]"
    )
    child_names = "|".join(re.escape(name) for name in _MAZE_CLEAR_CONDITION_KINDS)
    child_re = re.compile(
        r"(?im)^\s*\[(%s)\]\s*$" % child_names
    )
    any_tag_re = re.compile(r"(?m)^\s*\[")
    for outer in outer_re.finditer(block or ""):
        body = outer.group(1) or ""
        for child in child_re.finditer(body):
            value_start = child.end()
            next_tag = any_tag_re.search(body, value_start)
            value_end = next_tag.start() if next_tag is not None else len(body)
            values = _parse_int_list(body[value_start:value_end])
            kind = str(child.group(1) or "").strip().lower()
            for offset in range(0, len(values) - 1, 2):
                target_id = int(values[offset])
                count = int(values[offset + 1])
                if target_id <= 0 or count <= 0:
                    continue
                out.append(
                    MazeClearCondition(
                        kind=kind,
                        target_id=target_id,
                        count=count,
                    )
                )
    return out


def _parse_maze_ridable_script(block: str) -> MazeRidableScript | None:
    """Parse one maze's interactive vehicle declaration.

    This is a nested PVF section, so the general section helper cannot own the
    outer boundary: its body intentionally stops at the first child tag.
    """
    outer = re.search(
        r"(?is)\[randomized\s+object\s+creation\](.*?)"
        r"\[/randomized\s+object\s+creation\]",
        block or "",
    )
    if outer is None:
        return None
    body = outer.group(1) or ""
    select_values = _parse_int_list(_section_first(body, "select", ""))
    regenerate_values = _parse_int_list(
        _section_first(body, "regenerate", "")
    )
    minimap_values = _parse_int_list(_section_first(body, "minimap icon", ""))
    objects: list[MazeRidableObject] = []
    for object_match in re.finditer(
        r"(?is)\[object\](.*?)\[/object\]", body
    ):
        object_body = object_match.group(1) or ""
        map_values = _parse_int_list(_section_first(object_body, "map", ""))
        index_values = _parse_int_list(
            _section_first(object_body, "index", "")
        )
        pos_values = _parse_int_list(_section_first(object_body, "pos", ""))
        if len(map_values) < 2 or not index_values or len(pos_values) < 2:
            continue
        object_index = int(index_values[0])
        if object_index <= 0:
            continue
        faction = 0
        if re.search(r"(?im)^\s*\[monster\]\s*$", object_body):
            faction = 100
        elif re.search(r"(?im)^\s*\[neutral\]\s*$", object_body):
            faction = 200
        elif re.search(r"(?im)^\s*\[character\]\s*$", object_body):
            faction = 0
        objects.append(
            MazeRidableObject(
                map_x=int(map_values[0]),
                map_y=int(map_values[1]),
                object_index=object_index,
                pos_x=int(pos_values[0]),
                pos_y=int(pos_values[1]),
                faction=faction,
            )
        )
    return MazeRidableScript(
        select_count=max(0, int(select_values[0])) if select_values else 0,
        regenerate=bool(regenerate_values and int(regenerate_values[0]) != 0),
        minimap_icon=int(minimap_values[0]) if minimap_values else 0,
        objects=tuple(objects),
    )


@dataclass(frozen=True)
class LinkedDungeonNext:
    """One three-field DGN ``[linked dungeon] [next]`` row.

    The frozen PVF contains both deterministic ``target, 100, -1`` rows and a
    50/50 event-dungeon table.  Preserve all three source values here; the
    gameplay helper decides which forms have sufficiently closed semantics to
    execute.
    """

    target_dungeon_id: int
    weight: int
    condition: int


def _parse_linked_dungeon(
    text: str,
) -> tuple[tuple[int, ...], tuple[LinkedDungeonNext, ...]]:
    prev_ids: list[int] = []
    next_rows: list[LinkedDungeonNext] = []
    for match in re.finditer(
        r"(?is)\[linked\s+dungeon\](.*?)\[/linked\s+dungeon\]",
        text or "",
    ):
        block = match.group(1)
        prev_ids.extend(
            value
            for value in _parse_int_list(_section_text(block, "prev"))
            if int(value) > 0
        )
        values = _parse_int_list(_section_text(block, "next"))
        for offset in range(0, len(values) - 2, 3):
            next_rows.append(
                LinkedDungeonNext(
                    target_dungeon_id=int(values[offset]),
                    weight=int(values[offset + 1]),
                    condition=int(values[offset + 2]),
                )
            )
    return tuple(prev_ids), tuple(next_rows)


@dataclass
class DungeonFileInfo:
    dungeon_id: int
    path: str
    mazes: list[MazeInfo] = field(default_factory=list)
    # .dgn [difficulty level] five-slot availability table.  Slot index is the
    # Stage3 DUNGEON_PERMISSION clear-state value for that difficulty.
    difficulty_levels: tuple[int, ...] = ()
    minimum_required_level: int = 0
    basis_level: int = 0
    # Tower dungeons have no ordinary maze.  Their ordered floor maps are
    # carried directly by the DGN and are consumed by the dedicated tower
    # protocol rather than S0/001C + S0/001D.
    dungeon_type: str = ""
    death_tower_map_indexes: tuple[int, ...] = ()
    tower_max_clear_item_num: int = 0
    # .dgn [experience increasing point] — multiplies monster base exp (s4a12 weight)
    experience_increasing_point: float = 1.0
    # .dgn [story mode] — 剧情倍率/怪属性缩放（见 helper-dungeon_story_mode）
    story_mode: Any = None
    # PVF marks special Hell runs at dungeon scope.  A Temporal Rift-style
    # dungeon has both its initial ``[hell dungeon]`` maze and one or more
    # ``[escape hell]`` mazes, rather than a player-selected Hell Party room.
    hell_dungeon: bool = False
    has_escape_hell: bool = False
    # Licensed no-result runs do not wait for the ordinary 002E result UI
    # request before advancing authored clear-a-dungeon task conditions.
    license_dungeon: bool = False
    no_experience_monster_and_result: bool = False
    # .dgn [special passive object item] — 建筑固定掉落表（001D extra / 砸物）
    special_passive_object_items: list[SpecialPassiveObjectItem] = field(
        default_factory=list
    )
    # Dungeon-wide special-room targets declared below
    # ``[boss room entrance condition]``.  Entries preserve source order and
    # duplicates because some special dungeons assign the same target to
    # several different rooms.
    boss_entrance_hunt_monsters: tuple[int, ...] = ()
    # Monster codes authorized by the nested DGN boss-entrance
    # ``[summon monster]`` section.  MAP files provide the concrete actor
    # template; this dungeon-level list is the first authorization boundary.
    boss_entrance_summon_monsters: tuple[int, ...] = ()
    # DGN ``[linked dungeon]`` metadata.  Boot warmup parses the complete
    # source table; runtime transition policy lives in the dungeon domain.
    linked_prev_dungeon_ids: tuple[int, ...] = ()
    linked_next_dungeons: tuple[LinkedDungeonNext, ...] = ()

    def permission_clear_state(self) -> int | None:
        """Return the highest PVF-enabled difficulty slot for S0/0005.

        ``[difficulty level]`` is a five-slot flag table.  A missing table is
        intentionally distinct from an all-zero table: special maps without
        the field retain the caller's compatibility fallback, while an
        explicit all-zero table is a Normal-only map (state 0).
        """
        if not self.difficulty_levels:
            return None
        highest = 0
        for index, enabled in enumerate(self.difficulty_levels[:5]):
            if int(enabled) > 0:
                highest = index
        return highest


def _parse_map_specifications(data: str) -> list[dict]:
    """Parse [map specification] tokens: type x y mapId [extraIds...]."""
    tokens: list[str] = []
    for tok in re.split(r"[\s,]+", str(data or "")):
        tok = _strip_tick(tok)
        if tok:
            tokens.append(tok)
    out: list[dict] = []
    i = 0
    while i + 3 < len(tokens):
        typ = tokens[i].lower()
        try:
            x = int(tokens[i + 1], 0)
            y = int(tokens[i + 2], 0)
            map_id = int(tokens[i + 3], 0)
        except ValueError:
            i += 1
            continue
        candidates = [map_id]
        j = i + 4
        while j < len(tokens):
            try:
                candidates.append(int(tokens[j], 0))
                j += 1
            except ValueError:
                break
        out.append(
            {
                "type": typ,
                "x": x,
                "y": y,
                "map_id": candidates[0],
                "candidates": candidates,
            }
        )
        i = j
    return out


def _parse_monster_rewards(data: str) -> list[MonsterRewardEntry]:
    vals = _parse_int_list(data)
    out: list[MonsterRewardEntry] = []
    i = 0
    while i + 6 < len(vals):
        out.append(
            MonsterRewardEntry(
                monster_code=vals[i],
                dungeon_id=vals[i + 1],
                difficulty=vals[i + 2],
                item_id=vals[i + 3],
                count=vals[i + 4],
                drop_rate=vals[i + 5],
                max_stack=vals[i + 6],
            )
        )
        i += 7
    return out


def _parse_enemy_rewards(data: str) -> list[EnemyRewardEntry]:
    """``[enemy reward item]`` 8 元组（S4A12 EnemyRewardItemEntry）。"""
    vals = _parse_int_list(data)
    out: list[EnemyRewardEntry] = []
    i = 0
    while i + 7 < len(vals):
        out.append(
            EnemyRewardEntry(
                enemy_code=vals[i],
                enemy_type=vals[i + 1],
                dungeon_id=vals[i + 2],
                difficulty=vals[i + 3],
                item_id=vals[i + 4],
                count=vals[i + 5],
                drop_rate=vals[i + 6],
                max_stack=vals[i + 7],
            )
        )
        i += 8
    return out


def _parse_boss_entrance_hunt_monsters(text: str) -> tuple[int, ...]:
    """Parse DGN boss-entrance ``[hunt monster]`` target codes.

    Stage3 PVF stores the nested child tags as backtick strings and does not
    close the outer section.  The child payload is ``count`` followed by
    ``(monster_code, required_count, flag)`` triples.
    """
    source = str(text or "")
    outer = re.search(
        r"(?im)^\s*`?\[boss\s+room\s+entrance\s+condition\]`?\s*$",
        source,
    )
    if outer is None:
        return ()
    tail = source[outer.end() :]
    child = re.search(
        r"(?im)^\s*`?\[hunt\s+monster\]`?\s*$",
        tail,
    )
    if child is None:
        return ()
    values_text = tail[child.end() :]
    next_tag = re.search(r"(?m)^\s*`?\[", values_text)
    if next_tag is not None:
        values_text = values_text[: next_tag.start()]
    values = _parse_int_list(values_text)
    if not values:
        return ()
    declared = max(0, int(values[0]))
    out: list[int] = []
    for index in range(declared):
        offset = 1 + index * 3
        if offset >= len(values):
            break
        monster_code = int(values[offset])
        if monster_code > 0:
            out.append(monster_code)
    return tuple(out)


def _parse_boss_entrance_summon_monsters(text: str) -> tuple[int, ...]:
    """Parse DGN boss-entrance ``[summon monster]`` target codes.

    The nested payload is ``count`` followed by that many monster codes.  As
    with ``[hunt monster]``, the Stage3 source leaves the outer section open,
    so parsing stops at the next child tag.
    """
    source = str(text or "")
    outer = re.search(
        r"(?im)^\s*`?\[boss\s+room\s+entrance\s+condition\]`?\s*$",
        source,
    )
    if outer is None:
        return ()
    tail = source[outer.end() :]
    child = re.search(
        r"(?im)^\s*`?\[summon\s+monster\]`?\s*$",
        tail,
    )
    if child is None:
        return ()
    values_text = tail[child.end() :]
    next_tag = re.search(r"(?m)^\s*`?\[", values_text)
    if next_tag is not None:
        values_text = values_text[: next_tag.start()]
    values = _parse_int_list(values_text)
    if not values:
        return ()
    declared = max(0, int(values[0]))
    return tuple(
        int(monster_code)
        for monster_code in values[1 : 1 + declared]
        if int(monster_code) > 0
    )


def _parse_special_passive_object_items(data: str) -> list[SpecialPassiveObjectItem]:
    """.dgn ``[special passive object item]`` 多组解析。

    格式::

      dropGroup flag count  [itemId dropRate]×count   （可重复多组）

    首字段由 ``.map [special passive object]`` 的 ``[item]`` 引用。
    它不是建筑实例序号；同一作用域内重复声明的同组条目全部保留。

    例（蜘蛛王国）::

      0 -1 1 10088625 2000
      1 -1 1 10149508 5000
    """
    vals = _parse_int_list(data)
    out: list[SpecialPassiveObjectItem] = []
    pos = 0
    source_group_index = 0
    while pos + 2 < len(vals):
        object_index = vals[pos]
        flag = vals[pos + 1]
        count = vals[pos + 2]
        pos += 3
        if count < 0:
            break
        for _i in range(count):
            if pos + 1 >= len(vals):
                return out
            item_id = vals[pos]
            drop_rate = vals[pos + 1]
            pos += 2
            if item_id > 0 and drop_rate > 0:
                out.append(
                    SpecialPassiveObjectItem(
                        index=int(object_index),
                        source_group_index=int(source_group_index),
                        flag=int(flag),
                        item_id=int(item_id),
                        drop_rate=int(drop_rate),
                    )
                )
        source_group_index += 1
    return out


def _parse_pre_groups(text: str) -> list[list[int]]:
    """Each ``[pre required quest]`` section is one AND group; multi sections OR."""
    groups: list[list[int]] = []
    for chunk in _section_chunks(text, "pre required quest"):
        nums = [n for n in _parse_int_list(chunk) if n > 0]
        if nums:
            groups.append(nums)
    return groups


def _parse_pre_required_answers(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for chunk in _section_chunks(text, "pre required quest answer"):
        values = _parse_int_list(chunk)
        for offset in range(0, len(values) - 1, 2):
            quest_id = int(values[offset])
            answer_index = int(values[offset + 1])
            if quest_id > 0 and answer_index >= 0:
                out.append((quest_id & 0xFFFF, answer_index))
    return out


def _parse_question_answer_count(text: str) -> int:
    count = 0
    for chunk in _section_chunks(text, "question info"):
        # The first string is the prompt; the remaining strings are answers.
        strings = re.findall(r"`([^`]*)`", chunk, flags=re.DOTALL)
        count = max(count, max(0, len(strings) - 1))
    return count


def _parse_dialog_map_ids(text: str) -> tuple[int, ...]:
    """Return map ids from the fixed eight-integer quest dialog header.

    Job-dependent dialog sections prepend job/grow selectors.  Removing all
    backtick strings leaves those selectors plus the same eight-integer tail,
    whose fourth value is the map id.
    """
    out: list[int] = []
    seen: set[int] = set()
    for tag in ("dialog", "dialog depend on job"):
        for chunk in _section_chunks(text, tag):
            header = re.sub(r"`[^`]*`", " ", chunk, flags=re.DOTALL)
            values = _parse_int_list(header)
            if len(values) < 8:
                continue
            map_id = int(values[-5])
            if map_id <= 0 or map_id in seen:
                continue
            seen.add(map_id)
            out.append(map_id)
    return tuple(out)


def _parse_dialog_apc_codes(text: str) -> tuple[int, ...]:
    out: list[int] = []
    seen: set[int] = set()
    for match in re.finditer(r"(?i)<apc::(\d+)>", text or ""):
        code = int(match.group(1))
        if code <= 0 or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return tuple(out)


def _parse_quest_meta(quest_id: int, path: str, text: str) -> QuestMeta:
    levels = _parse_int_list(_section_text(text, "level"))
    min_level = levels[0] if levels else 1
    max_level = levels[1] if len(levels) > 1 else 99
    grade = _strip_tick(_section_first(text, "grade", "")).lower()
    if grade and not grade.startswith("["):
        grade = f"[{grade}]"
    qtype = _strip_tick(_section_first(text, "type", ""))
    sub_vals = _parse_int_list(_section_text(text, "sub type"))
    grow_vals = _parse_int_list(_section_text(text, "grow type"))
    jcq_vals = _parse_int_list(_section_text(text, "job change quest") or _section_text(text, "jcq"))
    exposed_vals = _parse_int_list(_section_text(text, "exposed by npc") or _section_text(text, "exposed_by_npc"))
    first_exposed_vals = _parse_int_list(_section_text(text, "first exposed") or _section_text(text, "first_exposed"))
    npc_vals = _parse_int_list(_section_text(text, "npc index"))
    creature_vals = _parse_int_list(_section_text(text, "creature type") or _section_text(text, "creature"))
    expert_type_vals = _parse_int_list(_section_text(text, "expert job type"))
    expert_lv_vals = _parse_int_list(_section_text(text, "expert job level"))
    is_event = bool(_section_chunks(text, "event")) or "event" in path.lower()
    collision = _parse_int_list(_section_text(text, "collision quest"))
    # Keep raw target body for diagnostics; also parse structured rules.
    target_raw = _section_text(text, "target character")
    target_rules = _parse_target_character_rules(target_raw)
    job_raw = _section_text(text, "job") or _section_first(text, "job", "[all]")
    job_joined = " ".join(
        _strip_tick(part).lower() for part in _section_chunks(text, "job") if part.strip()
    )
    if not job_joined:
        job_joined = _strip_tick(job_raw).lower() or "[all]"
    return QuestMeta(
        quest_id=int(quest_id) & 0xFFFF,
        path=path,
        name=_strip_tick(_section_first(text, "name", "")),
        grade=grade,
        quest_type=qtype,
        sub_type=sub_vals[0] if sub_vals else -1,
        min_level=int(min_level),
        max_level=int(max_level),
        job=job_joined,
        grow_type=grow_vals[0] if grow_vals else -1,
        jcq=jcq_vals[0] if jcq_vals else 0,
        exposed=exposed_vals[0] if exposed_vals else -1,
        first_exposed=first_exposed_vals[0] if first_exposed_vals else -1,
        is_event=is_event,
        creature_kind=creature_vals[0] if creature_vals else -1,
        expert_job_type=expert_type_vals[0] if expert_type_vals else -1,
        expert_job_level=expert_lv_vals[0] if expert_lv_vals else -1,
        target_character=target_raw.strip().lower(),
        target_character_rules=list(target_rules),
        npc_index=npc_vals[0] if npc_vals else -1,
        int_data=_section_text(text, "int data"),
        dungeon_info=tuple(_parse_int_list(_section_text(text, "dungeon info"))),
        dialog_map_ids=_parse_dialog_map_ids(text),
        dialog_apc_codes=_parse_dialog_apc_codes(text),
        pre_groups=_parse_pre_groups(text),
        pre_required_answers=_parse_pre_required_answers(text),
        question_answer_count=_parse_question_answer_count(text),
        collision=[c for c in collision if c > 0],
        monster_rewards=_parse_monster_rewards(_section_text(text, "monster reward item")),
        enemy_rewards=_parse_enemy_rewards(_section_text(text, "enemy reward item")),
        raw_text=text,
    )


def _grade_tag(grade: str) -> str:
    """Return the innermost PVF grade tag from a raw ``[grade]`` value.

    Most task scripts contain a plain ``[epic]``-style tag.  Map-gate
    scripts such as ``[ancient]`` include a second numeric line, and the
    generic section parser preserves their outer wrapper as
    ``[`[ancient]`\n1]``.  Eligibility must use the authored inner tag.
    """
    raw = _strip_tick(str(grade or "")).strip().lower()
    tags = re.findall(r"\[([^\[\]]+)\]", raw)
    if tags:
        return "[%s]" % str(tags[-1]).strip().lower()
    if raw and not raw.startswith("["):
        return "[%s]" % raw
    return raw


def _is_selectable(q: QuestMeta) -> bool:
    return _grade_tag(q.grade) in SELECTABLE_GRADES


def _is_repeatable_grade(grade: str) -> bool:
    return _grade_tag(grade) in _REPEATABLE_GRADES


def _is_daily_grade(grade: str) -> bool:
    return _grade_tag(grade) in _DAILY_GRADES


def is_daily_quest_meta(meta: QuestMeta | None) -> bool:
    """Whether a task is subject to the one-completion-per-server-day rule."""
    return meta is not None and _is_daily_grade(str(getattr(meta, "grade", "") or ""))


def is_repeatable_quest_meta(meta: QuestMeta | None) -> bool:
    """Whether a completed task can be accepted again after its own cooldown."""
    return meta is not None and _is_repeatable_grade(
        str(getattr(meta, "grade", "") or "")
    )


def daily_quest_day_key() -> str:
    """Return the local server calendar day used by daily-task persistence."""
    return date.today().isoformat()


_WORLDMAP_CONDITION_SPLIT = re.compile(
    r"(?im)^\s*(\[in progress\]|\[is clear quest\])\s*$"
)


def _worldmap_integer_lines(text: str) -> list[int]:
    """Return standalone integer rows from one WorldMap condition block."""
    return [
        int(value)
        for value in re.findall(r"(?m)^\s*(-?\d+)\s*$", str(text or ""))
    ]


def _parse_worldmap_dungeon_conditions(text: str) -> tuple[tuple[int, int, str], ...]:
    """Parse current-PVF ``worldmap/*.wdm`` quest-to-dungeon conditions.

    The primary block is ``dungeon_id, completed_quest_id``.  The later
    ``[in progress]`` and ``[is clear quest]`` blocks reverse that pair to
    ``quest_id, dungeon_id``.  Preserve the condition kind for diagnostics;
    eligibility only needs the quest IDs.
    """
    match = re.search(r"(?is)\[dungeon\](.*?)\[/dungeon\]", str(text or ""))
    if match is None:
        return ()
    blocks = _WORLDMAP_CONDITION_SPLIT.split(match.group(1))
    out: list[tuple[int, int, str]] = []

    base_values = _worldmap_integer_lines(blocks[0])
    for index in range(0, len(base_values) - 1, 2):
        dungeon_id = int(base_values[index])
        quest_id = int(base_values[index + 1])
        if dungeon_id > 0 and quest_id > 0:
            out.append((quest_id, dungeon_id, "finished"))

    for index in range(1, len(blocks) - 1, 2):
        mode = str(blocks[index]).strip().lower().strip("[]")
        values = _worldmap_integer_lines(blocks[index + 1])
        for value_index in range(0, len(values) - 1, 2):
            quest_id = int(values[value_index])
            dungeon_id = int(values[value_index + 1])
            if dungeon_id > 0 and quest_id > 0:
                out.append((quest_id, dungeon_id, mode))
    return tuple(out)


def _parse_town_need_quest_ids(text: str) -> tuple[int, ...]:
    """Return real task IDs from a town ``[permission] [need quest]`` gate."""
    out: list[int] = []
    seen: set[int] = set()
    for chunk in _section_chunks(text, "need quest"):
        for raw_id in _parse_int_list(chunk):
            raw_quest_id = int(raw_id)
            if raw_quest_id <= 0:
                continue
            quest_id = raw_quest_id & 0xFFFF
            if quest_id <= 0 or quest_id in seen:
                continue
            seen.add(quest_id)
            out.append(quest_id)
    return tuple(out)


class QuestIndex:
    def __init__(self) -> None:
        self.quest_paths: dict[int, str] = {}
        self.metas: dict[int, QuestMeta] = {}
        self.sorted_quest_ids: list[int] = []
        self.connect_quest_units: tuple[ConnectQuestUnit, ...] = ()
        self.episode_quest_units: tuple[ConnectQuestUnit, ...] = ()
        self.epic_sequence_ids: set[int] = set()
        self.unsequenced_epic_ids: set[int] = set()
        self.epic_sequence_positions: dict[int, tuple[int, int]] = {}
        self.dungeon_paths: dict[int, str] = {}
        self.dungeon_cache: dict[int, DungeonFileInfo] = {}
        self._dungeons_fully_loaded = False
        self.training_npcs: dict[int, set[int]] = {}
        self.local_unlock_ids: list[int] = []
        self.worldmap_condition_quest_ids: set[int] = set()
        self.worldmap_progression_quest_ids: set[int] = set()
        self.town_permission_quest_ids: set[int] = set()
        self.map_unlock_quest_ids: set[int] = set()
        self.acceptable_cache: dict[str, list[int]] = {}
        self.source = ""
        self.loaded = False

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        with _LOCK:
            if self.loaded:
                return
            self._load()
            self.loaded = True

    def _read_text(self, path: str) -> str:
        return pvf_runtime.read_text(path)

    def _load(self) -> None:
        # Paths first (cheap). Full quest metas are loaded lazily / on demand.
        lst = self._read_text("n_quest/quest.lst")
        pairs = _parse_lst_pairs(lst)
        self.quest_paths = {}
        self.metas = {}
        for qid, rel in pairs.items():
            if qid <= 0:
                continue
            path = rel if rel.lower().startswith("n_quest/") else f"n_quest/{rel}"
            path = path.replace("\\", "/").strip("/")
            self.quest_paths[int(qid) & 0xFFFF] = path
        self.sorted_quest_ids = sorted(self.quest_paths.keys())
        self._metas_fully_loaded = False
        connect_text = self._read_text("n_quest/connectquestlist.lst")
        self.connect_quest_units = _parse_connect_quest_units(connect_text)
        self.episode_quest_units = tuple(
            unit
            for unit in self.connect_quest_units
            if unit.unit_type == "episode quest"
        )
        if not self.episode_quest_units:
            raise ValueError("connectquestlist has no episode quest units")
        try:
            dgn_lst = self._read_text("dungeon/dungeon.lst")
            raw = _parse_lst_pairs(dgn_lst)
            self.dungeon_paths = {}
            for did, rel in raw.items():
                path = rel if rel.lower().startswith("dungeon/") else f"dungeon/{rel}"
                self.dungeon_paths[int(did)] = path.replace("\\", "/").strip("/")
        except Exception:
            self.dungeon_paths = {}
        self.training_npcs = self._load_training_npcs()
        self.local_unlock_ids = self._load_local_unlock_ids()
        self.worldmap_condition_quest_ids = self._load_worldmap_condition_quest_ids()
        self.source = "shared_pvf"
        # Acceptable precompute is done only by warmup_quest_index() after
        # ensure_all_metas_loaded(). Never call compute_acceptable from _load:
        # ensure_loaded is still False here and would recurse.

    def _load_worldmap_condition_quest_ids(self) -> set[int]:
        """Collect valid task IDs referenced by current-PVF worldmap nodes."""
        out: set[int] = set()
        for raw_path in pvf_runtime.list_paths("worldmap/"):
            path = str(raw_path).replace("\\", "/")
            if not path.lower().endswith(".wdm"):
                continue
            try:
                text = self._read_text(path)
            except Exception:
                continue
            for quest_id, _dungeon_id, _mode in _parse_worldmap_dungeon_conditions(text):
                # A WDM route remains an authored map gate even when its
                # destination is a transition/special dungeon omitted from
                # dungeon.lst (for example Moonlight Pub's area 511).
                if quest_id in self.quest_paths:
                    out.add(int(quest_id) & 0xFFFF)
        return out

    def _load_town_permission_quest_ids(self) -> set[int]:
        """Collect valid task IDs required by current-PVF town permissions."""
        out: set[int] = set()
        for raw_path in pvf_runtime.list_paths("town/"):
            path = str(raw_path).replace("\\", "/")
            if not path.lower().endswith(".twn"):
                continue
            try:
                text = self._read_text(path)
            except Exception:
                continue
            for quest_id in _parse_town_need_quest_ids(text):
                if quest_id in self.quest_paths:
                    out.add(int(quest_id) & 0xFFFF)
        return out

    def _build_worldmap_progression_quest_ids(self) -> None:
        """Close every current-PVF worldmap route over its prerequisites.

        A ``worldmap/*.wdm`` condition is authoritative map-unlock evidence.
        Its task family may be epic, side, hidden, achievement, or a
        profession route, so filtering it by a guessed grade silently breaks
        valid map progression.  Content blocks are role-local runtime policy,
        so this shared PVF index must retain every authored route and let
        ``compute_acceptable`` filter the selected character's blocked IDs.
        """
        pending = [
            int(quest_id) & 0xFFFF
            for quest_id in self.worldmap_condition_quest_ids
        ]
        out: set[int] = set()
        while pending:
            quest_id = int(pending.pop()) & 0xFFFF
            if quest_id in out:
                continue
            meta = self.metas.get(quest_id)
            if meta is None:
                continue
            out.add(quest_id)
            for group in meta.pre_groups:
                for raw_pre_quest_id in group:
                    pre_quest_id = int(raw_pre_quest_id) & 0xFFFF
                    if (
                        pre_quest_id in out
                        or pre_quest_id not in self.metas
                    ):
                        continue
                    pending.append(pre_quest_id)
        self.worldmap_progression_quest_ids = out

    def _build_map_unlock_quest_ids(self) -> None:
        """Build task-list routes that unlock authored map or town access.

        A non-event town ``[permission] [need quest]`` is a normal access
        gate and remains eligible for its prerequisite closure.  Event town
        permissions remain indexed for diagnostics only: they can gate an
        event-only transition or an internal town phase without being a
        generic task-list route.
        """
        self.town_permission_quest_ids = self._load_town_permission_quest_ids()
        pending = list(self.worldmap_progression_quest_ids)
        for quest_id in self.town_permission_quest_ids:
            meta = self.metas.get(int(quest_id) & 0xFFFF)
            if meta is not None and not meta.is_event:
                pending.append(int(quest_id) & 0xFFFF)
        pending.extend(
            quest_id
            for quest_id, meta in self.metas.items()
            if _grade_tag(meta.grade) in _SPECIAL_MAP_GATE_GRADES
        )

        out: set[int] = set()
        while pending:
            quest_id = int(pending.pop()) & 0xFFFF
            if quest_id in out:
                continue
            meta = self.metas.get(quest_id)
            if meta is None:
                continue
            out.add(quest_id)
            for group in meta.pre_groups:
                for raw_pre_quest_id in group:
                    pre_quest_id = int(raw_pre_quest_id) & 0xFFFF
                    if (
                        pre_quest_id in out
                        or pre_quest_id not in self.metas
                    ):
                        continue
                    pending.append(pre_quest_id)
        self.map_unlock_quest_ids = out

    def _load_training_npcs(self) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        try:
            text = self._read_text("n_quest/TrainingQuest.lst")
        except Exception:
            return out
        current_level = None
        for line in text.splitlines():
            nums = _parse_int_list(line)
            if not nums:
                continue
            if len(nums) == 1 and 1 <= nums[0] <= 99:
                current_level = nums[0]
                out.setdefault(current_level, set())
            elif current_level is not None and nums:
                out[current_level].add(nums[0])
        return out

    def _load_local_unlock_ids(self) -> list[int]:
        raw = (os.environ.get("USDOF_GAME_QUEST_LOCAL_UNLOCK_IDS") or "").strip()
        if raw:
            return [int(x) & 0xFFFF for x in re.findall(r"\d+", raw) if int(x) > 0]
        try:
            from config import BASE_DIR

            path = BASE_DIR / "data" / "resources" / "quest_local_unlock_ids.json"
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                ids = payload.get("quest_ids") or payload.get("ids") or []
                return [int(x) & 0xFFFF for x in ids if int(x) > 0]
        except Exception:
            pass
        return []

    def get_meta(self, quest_id: int) -> QuestMeta | None:
        self.ensure_loaded()
        qid = int(quest_id) & 0xFFFF
        if qid in self.metas:
            return self.metas[qid]
        path = self.quest_paths.get(qid)
        if not path:
            return None
        try:
            text = self._read_text(path)
        except Exception:
            return None
        meta = _parse_quest_meta(qid, path, text)
        self.metas[qid] = meta
        return meta

    def registered_quest_ids(self) -> frozenset[int]:
        """Return task IDs authored in ``n_quest/quest.lst``."""
        self.ensure_loaded()
        return frozenset(self.quest_paths)

    def ensure_all_metas_loaded(self) -> None:
        """Load all quest metas once (acceptable filter / boot warm)."""
        self.ensure_loaded()
        if self._metas_fully_loaded:
            return
        with _LOCK:
            if self._metas_fully_loaded:
                return
            for qid, path in self.quest_paths.items():
                if qid in self.metas:
                    continue
                try:
                    text = self._read_text(path)
                except Exception:
                    continue
                self.metas[qid] = _parse_quest_meta(qid, path, text)
            self.sorted_quest_ids = sorted(self.quest_paths.keys())
            self._build_epic_sequence_index()
            self._build_worldmap_progression_quest_ids()
            self._build_map_unlock_quest_ids()
            self._metas_fully_loaded = True

    def _build_epic_sequence_index(self) -> None:
        """Index only current mainline ``[epic]`` rows from episode sequences."""
        positions: dict[int, tuple[int, int]] = {}
        for unit_index, unit in enumerate(self.episode_quest_units):
            for sequence_index, quest_id in enumerate(unit.sequence):
                meta = self.metas.get(int(quest_id) & 0xFFFF)
                if meta is None or _grade_tag(meta.grade) != "[epic]":
                    continue
                qid = int(quest_id) & 0xFFFF
                previous = positions.get(qid)
                if previous is not None and previous[0] != unit_index:
                    raise ValueError(
                        "epic quest_id=%s appears in multiple episode sequences"
                        % qid
                    )
                positions[qid] = (unit_index, sequence_index)
        self.epic_sequence_positions = positions
        self.epic_sequence_ids = set(positions)
        self.unsequenced_epic_ids = {
            int(quest_id) & 0xFFFF
            for quest_id, meta in self.metas.items()
            if _grade_tag(meta.grade) == "[epic]"
            and (int(quest_id) & 0xFFFF) not in self.epic_sequence_ids
        }

    def is_unsequenced_epic_quest(self, quest_id: int) -> bool:
        """Whether a registered EpicQuest is absent from every episode chain."""
        self.ensure_all_metas_loaded()
        return (int(quest_id) & 0xFFFF) in self.unsequenced_epic_ids

    def get_unsequenced_epic_quest_ids(self) -> tuple[int, ...]:
        """Return the boot-audited EpicQuest rows withheld from players."""
        self.ensure_all_metas_loaded()
        return tuple(sorted(self.unsequenced_epic_ids))

    def _order_acceptable_by_epic_sequence(
        self,
        quest_ids: Sequence[int],
    ) -> list[int]:
        """Preserve non-epic placement while ordering each epic story region."""
        ordered = [int(quest_id) & 0xFFFF for quest_id in quest_ids]
        if len(ordered) < 2 or not self.epic_sequence_positions:
            return ordered
        present = set(ordered)
        for unit_index, unit in enumerate(self.episode_quest_units):
            unit_members = {
                quest_id
                for quest_id, position in self.epic_sequence_positions.items()
                if position[0] == unit_index
            }
            slots = [
                index
                for index, quest_id in enumerate(ordered)
                if quest_id in unit_members
            ]
            if len(slots) < 2:
                continue
            sequence_order = [
                quest_id
                for quest_id in unit.sequence
                if quest_id in present and quest_id in unit_members
            ]
            for slot, quest_id in zip(slots, sequence_order):
                ordered[slot] = quest_id
        return ordered

    def compute_init_trigger(
        self,
        quest_id: int,
        finished_quest_ids: Sequence[int] = (),
    ) -> int:
        meta = self.get_meta(quest_id)
        if meta is None:
            return 0
        type_tag = _normalize_tag(meta.quest_type)
        if type_tag in {"meet npc", "talk", "meet"}:
            return 0
        if type_tag == "seek n meet npc":
            values = _parse_int_list(meta.int_data)
            if len(values) < 3:
                return 1
            item_count = values[1] if values[1] > 0 else 1
            meet_npc_count = 1 if values[2] > 0 else 0
            return _pack_trigger(item_count, meet_npc_count, 0)
        if type_tag in {"quest clear", "clear quest"}:
            trigger = quest_clear_trigger(meta, finished_quest_ids)
            return int(trigger if trigger is not None else 1)
        if type_tag == "clear map":
            # ``int data`` contains target map and optional companion metadata,
            # never a residual count.  One target map is one trigger channel.
            return 1
        if type_tag == "clear a dungeon":
            # This type uses dungeon/difficulty pairs rather than a count.  It
            # must start incomplete; sub type 6 is settled from the proven
            # dungeon-clear context in quest_clear.
            return 1
        if type_tag == "condition under clear":
            return _condition_under_clear_trigger(meta)
        if type_tag == "condition under clear2":
            return _trigger_from_int_data(meta.int_data, 5)
        if type_tag == "hunt monster":
            trigger = hunt_monster_initial_trigger(meta)
            return int(trigger if trigger is not None else 1)
        if type_tag == "hunt enemy":
            return _trigger_from_int_data(meta.int_data, 5)
        if type_tag == "seeking":
            # Stage3 sub_1392A50 sends C1/0021 and optimistically applies
            # trigger=0 locally when the seeking condition completes.  The
            # int-data values are requirement tuples (including item_id=0 for
            # gold), not an initial trigger scalar.
            return 1
        if type_tag == "use item":
            trigger = use_item_initial_trigger(meta)
            return int(trigger if trigger is not None else 1)
        if type_tag == "question":
            return 1
        if type_tag == "pvp rank":
            # A rank requirement is not a meet/talk task. Zero here also arms
            # the 001F -> 0021 completion pulse, so it must stay pending until
            # the existing progress route explicitly satisfies the condition.
            # int_data is the target rank, not an initial residual count.
            return 1
        return 0

    def _acceptable_cache_key(
        self,
        *,
        level: int,
        job: int,
        grow_type: int,
        finished_ids: Sequence[int],
        finished_flags: Mapping[int, int] | None,
        active_ids: Sequence[int],
        daily_completed_ids: Sequence[int],
        blocked_quest_ids: Iterable[int],
        max_count: int,
    ) -> str:
        f = ",".join(str(int(x) & 0xFFFF) for x in sorted({int(x) & 0xFFFF for x in finished_ids if int(x) > 0}))
        v = ",".join(
            "%s:%s" % (int(quest_id) & 0xFFFF, int(flag) & 0xFF)
            for quest_id, flag in sorted((finished_flags or {}).items())
            if int(quest_id) > 0 and int(flag) != 0
        )
        a = ",".join(str(int(x) & 0xFFFF) for x in sorted({int(x) & 0xFFFF for x in active_ids if int(x) > 0}))
        d = ",".join(
            str(int(x) & 0xFFFF)
            for x in sorted(
                {int(x) & 0xFFFF for x in daily_completed_ids if int(x) > 0}
            )
        )
        b = ",".join(
            str(int(x) & 0xFFFF)
            for x in sorted(
                {int(x) & 0xFFFF for x in blocked_quest_ids if int(x) > 0}
            )
        )
        return f"{int(level)}|{int(job)}|{int(grow_type)}|{int(max_count)}|f={f}|v={v}|a={a}|d={d}|b={b}"

    def compute_acceptable(
        self,
        *,
        level: int,
        job: int,
        grow_type: int = 0,
        finished_ids: Sequence[int] = (),
        finished_flags: Mapping[int, int] | None = None,
        active_ids: Sequence[int] = (),
        daily_completed_ids: Sequence[int] = (),
        max_count: int = 0,
    ) -> list[int]:
        self.ensure_loaded()
        self.ensure_all_metas_loaded()
        max_n = int(max_count) if int(max_count) > 0 else _env_int("USDOF_GAME_ACCEPTABLE_QUEST_MAX", 0)
        blocked_quest_ids = active_blocked_quest_ids()
        key = self._acceptable_cache_key(
            level=level,
            job=job,
            grow_type=grow_type,
            finished_ids=finished_ids,
            finished_flags=finished_flags,
            active_ids=active_ids,
            daily_completed_ids=daily_completed_ids,
            blocked_quest_ids=blocked_quest_ids,
            max_count=max_n,
        )
        cached = self.acceptable_cache.get(key)
        if cached is not None:
            return list(cached)

        cleared = {int(x) & 0xFFFF for x in finished_ids if int(x) > 0}
        cleared_flags = {
            int(quest_id) & 0xFFFF: int(flag) & 0xFF
            for quest_id, flag in (finished_flags or {}).items()
            if int(quest_id) > 0 and int(flag) != 0
        }
        active = {int(x) & 0xFFFF for x in active_ids if int(x) > 0}
        daily_completed = {
            int(x) & 0xFFFF for x in daily_completed_ids if int(x) > 0
        }
        out: list[int] = []
        for qid in self.sorted_quest_ids:
            if qid <= 0 or qid > 29999:
                continue
            # These are task-list hard gates. Apply the selected character's
            # blocks first, then require an authored quest.lst registration
            # before evaluating task-specific acceptance conditions.
            if qid in blocked_quest_ids or qid not in self.quest_paths:
                continue
            if qid in active:
                continue
            q = self.metas.get(qid)
            if q is None:
                continue
            is_map_unlock = qid in getattr(self, "map_unlock_quest_ids", set())
            # ``[exposed by npc]`` controls the NPC dialog entry point, not
            # whether the player meets the task's authored prerequisites. The
            # generic acceptable list is this server's normal task-list path.
            if (
                q.is_event
                and not _env_bool("USDOF_GAME_ACCEPTABLE_INCLUDE_EVENT", False)
                and not is_map_unlock
            ):
                continue
            if q.creature_kind >= 0 and not _env_bool("USDOF_GAME_ACCEPTABLE_INCLUDE_CREATURE", False):
                continue
            if q.expert_job_type >= 0 and q.expert_job_level >= 0:
                if not _env_bool("USDOF_GAME_ACCEPTABLE_INCLUDE_EXPERT", False):
                    continue
            grade = _grade_tag(q.grade)
            if grade == "[training]":
                npcs = self.training_npcs.get(int(level), set())
                if q.npc_index <= 0 or (npcs and q.npc_index not in npcs):
                    if npcs:
                        continue
            if not _is_selectable(q) and not is_map_unlock:
                continue
            # ``quest.lst`` registers every script, including stale/auxiliary
            # EpicQuest rows.  Only [epic] rows present in an episode
            # ``[sequence]`` are current player-facing mainline tasks.
            if grade == "[epic]" and qid not in self.epic_sequence_ids:
                continue
            # [target character] triples (job, first grow, awakening); empty = open.
            if not _matches_target_character(
                q.target_character,
                int(job),
                grow_type=int(grow_type),
                rules=getattr(q, "target_character_rules", None) or (),
            ):
                continue
            if int(level) < int(q.min_level) or int(level) > int(q.max_level):
                continue
            if not _matches_job(q.job, int(job)):
                continue
            jcq = int(q.jcq or 0)
            first_grow, _awakening = _split_grow_type(int(grow_type))
            # jcq=1/10/20: 一转相关，不靠 grow_type 过滤（前置链控序）
            # 其它（含 jcq=2/3 觉醒与普通分支任务）: PVF [grow type] 存的是
            # 一转分支号（如 4），角色侧可能是打包值 0x14/0x24，统一比低半字节。
            if q.grow_type != -1 and jcq not in (1, 10, 20) and int(grow_type) >= 0:
                if int(q.grow_type) != int(first_grow):
                    continue
            if _is_daily_grade(grade) and qid in daily_completed:
                continue
            if not _is_repeatable_grade(grade) and qid in cleared:
                continue
            # 组内 AND、组间 OR；任一组全部完成即可。
            if q.pre_groups:
                pre_ok = False
                for group in q.pre_groups:
                    if all((int(pq) & 0xFFFF) in cleared for pq in group):
                        pre_ok = True
                        break
                if not pre_ok:
                    continue
            if q.pre_required_answers and any(
                int(cleared_flags.get(int(required_quest_id) & 0xFFFF, 0))
                != int(answer_index) + 1
                for required_quest_id, answer_index in q.pre_required_answers
            ):
                continue
            if any((int(cq) & 0xFFFF) in cleared for cq in q.collision):
                continue
            out.append(qid)
        out = self._order_acceptable_by_epic_sequence(out)
        if max_n > 0:
            out = out[:max_n]
        self.acceptable_cache[key] = list(out)
        if len(self.acceptable_cache) > 256:
            for old in list(self.acceptable_cache.keys())[:64]:
                self.acceptable_cache.pop(old, None)
        return list(out)

    def local_unlock_quest_ids(self) -> list[int]:
        self.ensure_loaded()
        return list(self.local_unlock_ids)

    def get_worldmap_progression_quest_ids(self) -> list[int]:
        """Return current-PVF task routes required by worldmap nodes."""
        self.ensure_all_metas_loaded()
        return sorted(getattr(self, "worldmap_progression_quest_ids", set()))

    def get_map_unlock_quest_ids(self) -> list[int]:
        """Return every current-PVF map gate and prerequisite task route."""
        self.ensure_all_metas_loaded()
        return sorted(getattr(self, "map_unlock_quest_ids", set()))

    def resolve_quest_monster_drops(
        self,
        active_quest_ids: Iterable[int],
        *,
        monster_code: int,
        dungeon_id: int = -1,
        difficulty: int = -1,
    ) -> list[tuple[int, int, int, int]]:
        """返回 (item_id, count, drop_rate, max_stack)。

        max_stack=-1 表示不限；与 S4A12 QuestDropProvider.RollDrop 一致。
        """
        self.ensure_loaded()
        results: list[tuple[int, int, int, int]] = []
        mc = int(monster_code)
        for qid in active_quest_ids or ():
            meta = self.get_meta(int(qid))
            if meta is None:
                continue
            for entry in meta.monster_rewards:
                if int(entry.monster_code) != mc:
                    continue
                if int(entry.dungeon_id) != -1 and int(entry.dungeon_id) != int(dungeon_id):
                    continue
                if int(entry.difficulty) >= 0 and int(entry.difficulty) != int(difficulty):
                    continue
                if entry.item_id > 0 and entry.count > 0:
                    results.append(
                        (
                            int(entry.item_id),
                            int(entry.count),
                            int(entry.drop_rate),
                            int(entry.max_stack),
                        )
                    )
        return results

    def resolve_quest_enemy_drops(
        self,
        active_quest_ids: Iterable[int],
        *,
        enemy_code: int,
        enemy_type: int,
        dungeon_id: int = -1,
        difficulty: int = -1,
    ) -> list[tuple[int, int, int, int]]:
        """``[enemy reward item]`` 匹配 → (item_id, count, drop_rate, max_stack)。

        用于 AI / passive object / 以及 Stage3 把怪材料也写在 enemy_reward type=1 的任务。
        S4A12 CheckEnemyDrop；enemy_type: 1=monster 2=ai 3=passive object。
        """
        self.ensure_loaded()
        results: list[tuple[int, int, int, int]] = []
        code = int(enemy_code)
        etype = int(enemy_type)
        if code <= 0 or etype <= 0:
            return results
        for qid in active_quest_ids or ():
            meta = self.get_meta(int(qid))
            if meta is None:
                continue
            for entry in getattr(meta, "enemy_rewards", None) or ():
                if int(entry.enemy_code) != code:
                    continue
                if int(entry.enemy_type) != etype:
                    continue
                if int(entry.dungeon_id) != -1 and int(entry.dungeon_id) != int(dungeon_id):
                    continue
                if int(entry.difficulty) >= 0 and int(entry.difficulty) != int(difficulty):
                    continue
                if entry.item_id > 0 and entry.count > 0:
                    results.append(
                        (
                            int(entry.item_id),
                            int(entry.count),
                            int(entry.drop_rate),
                            int(entry.max_stack),
                        )
                    )
        return results

    def resolve_quest_kill_drops(
        self,
        active_quest_ids: Iterable[int],
        *,
        source_code: int,
        enemy_type: int = 0,
        dungeon_id: int = -1,
        difficulty: int = -1,
    ) -> tuple[list[tuple[int, int, int, int]], str]:
        """击杀/砸毁统一任务材料解析（通用，不绑单任务）。

        Stage3 PVF 有两套表，须并联：

        | 表 | 何时查 |
        |----|--------|
        | ``[monster reward item]`` | enemy_type∈{0,1}（普通怪） |
        | ``[enemy reward item]`` type=1 | 同上；不少 seeking 主线只写这里（如 1849 棋子材料） |
        | ``[enemy reward item]`` type=2 | AI / APC |
        | ``[enemy reward item]`` type=3 | 可破坏建筑 / passive object |

        ``enemy_type<=0``（房间行未知）：对同一 code 合并 mon 表 + enemy type 1/2/3，
        避免建筑 DIE 用 code 当 seq 找不到 mon 时完全 miss。

        返回 ``(drops, source_note)``；drops 为 ``(item_id, count, rate, max_stack)``。
        """
        code = int(source_code or 0)
        if code <= 0:
            return [], "no_code"
        etype = int(enemy_type or 0)
        results: list[tuple[int, int, int, int]] = []
        tags: list[str] = []

        def _extend(part: list[tuple[int, int, int, int]], tag: str) -> None:
            if not part:
                return
            results.extend(part)
            tags.append("%s=%d" % (tag, len(part)))

        if etype <= 0:
            # 未知域：全表并联（按 code 精确匹配，不会串怪）
            _extend(
                self.resolve_quest_monster_drops(
                    active_quest_ids,
                    monster_code=code,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                ),
                "monster_reward",
            )
            for t in (1, 2, 3):
                _extend(
                    self.resolve_quest_enemy_drops(
                        active_quest_ids,
                        enemy_code=code,
                        enemy_type=t,
                        dungeon_id=dungeon_id,
                        difficulty=difficulty,
                    ),
                    "enemy_t%d" % t,
                )
            return results, ("all_tables " + ",".join(tags)) if tags else "none"
        if etype == 1:
            _extend(
                self.resolve_quest_monster_drops(
                    active_quest_ids,
                    monster_code=code,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                ),
                "monster_reward",
            )
            _extend(
                self.resolve_quest_enemy_drops(
                    active_quest_ids,
                    enemy_code=code,
                    enemy_type=1,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                ),
                "enemy_t1",
            )
            return results, ("monster_domain " + ",".join(tags)) if tags else "none"
        # type 2 AI / type 3 passive object
        _extend(
            self.resolve_quest_enemy_drops(
                active_quest_ids,
                enemy_code=code,
                enemy_type=etype,
                dungeon_id=dungeon_id,
                difficulty=difficulty,
            ),
            "enemy_t%d" % etype,
        )
        return results, ("enemy_domain type=%d " % etype + ",".join(tags)) if tags else (
            "none type=%d" % etype
        )

    def _parse_dungeon_file(self, dungeon_id: int) -> DungeonFileInfo:
        path = self.dungeon_paths.get(int(dungeon_id))
        if not path:
            return DungeonFileInfo(dungeon_id=int(dungeon_id), path="")
        try:
            text = self._read_text(path)
        except Exception:
            return DungeonFileInfo(dungeon_id=int(dungeon_id), path=path)
        mazes: list[MazeInfo] = []
        # USDOF PVF uses repeated open tags [maze info] without [/maze].
        # Also accept legacy [maze]...[/maze] if present.
        blocks = re.findall(r"(?is)\[maze\](.*?)\[/maze\]", text)
        if not blocks:
            parts = re.split(r"(?is)\[maze\s*info\]", text)
            blocks = [p for p in parts[1:] if str(p).strip()]
        if not blocks:
            blocks = [text]
        for idx, block in enumerate(blocks):
            # Stop at next maze-like boundary leftovers if any.
            block = re.split(r"(?is)\[maze\s*info\]|\[maze\]", block, maxsplit=1)[0]
            qc_vals = _parse_int_list(_section_text(block, "quest connection"))
            qc = tuple(qc_vals[:3]) if len(qc_vals) >= 2 else None
            starts: list[tuple[int, int]] = []
            start_vals = _parse_int_list(_section_text(block, "start map"))
            for i in range(0, len(start_vals) - 1, 2):
                starts.append((start_vals[i], start_vals[i + 1]))
            bosses: list[tuple[int, int]] = []
            boss_vals = _parse_int_list(_section_text(block, "boss map"))
            for i in range(0, len(boss_vals) - 1, 2):
                bosses.append((boss_vals[i], boss_vals[i + 1]))
            map_specs = _parse_map_specifications(_section_text(block, "map specification"))
            mazes.append(
                MazeInfo(
                    index=idx,
                    quest_connection=qc,
                    clear_conditions=_parse_maze_clear_conditions(block),
                    open_door_even_enemy=_parse_maze_open_door_even_enemy(block),
                    start_maps=starts,
                    boss_maps=bosses,
                    map_specs=map_specs,
                    name=_strip_tick(_section_first(block, "name", "")),
                    escape_hell=bool(
                        re.search(r"(?is)\[escape\s+hell\]\s*1\b", block)
                    ),
                    ridable_script=_parse_maze_ridable_script(block),
                    special_passive_object_items=(
                        _parse_special_passive_object_items(
                            _section_text(block, "special passive object item")
                        )
                        if re.search(r"(?is)\[maze(?:\s*info)?\]", text)
                        and re.search(r"(?is)\[special passive object item\]", block)
                        else None
                    ),
                )
            )
        min_req = _parse_int_list(_section_text(text, "minimum required level"))
        basis = _parse_int_list(_section_text(text, "basis level"))
        dungeon_type = _normalize_tag(_section_first(text, "dungeon type", ""))
        death_tower_values = _parse_int_list(
            _section_text(text, "death tower map indexes")
        )
        death_tower_maps: tuple[int, ...] = ()
        if death_tower_values:
            floor_count = max(0, int(death_tower_values[0]))
            by_floor: dict[int, int] = {}
            for offset in range(1, len(death_tower_values) - 1, 2):
                floor = int(death_tower_values[offset])
                map_index = int(death_tower_values[offset + 1])
                if 1 <= floor <= floor_count and map_index > 0:
                    by_floor[floor] = map_index
            if floor_count > 0 and all(
                floor in by_floor for floor in range(1, floor_count + 1)
            ):
                death_tower_maps = tuple(
                    by_floor[floor] for floor in range(1, floor_count + 1)
                )
        tower_max_clear_values = _parse_int_list(
            _section_text(text, "tower max clear item num")
        )
        difficulty_levels = tuple(
            _parse_int_list(_section_text(text, "difficulty level"))[:5]
        )
        exp_inc = 1.0
        exp_raw = _section_text(text, "experience increasing point")
        if exp_raw:
            for tok in re.split(r"[\s,]+", str(exp_raw).strip()):
                tok = _strip_tick(tok)
                if not tok:
                    continue
                try:
                    exp_inc = float(tok)
                    break
                except ValueError:
                    continue
        if exp_inc < 0:
            exp_inc = 1.0
        story_mode = None
        try:
            from game.chain_loader import load_chain

            _story = load_chain("helper-dungeon_story_mode.py")
            story_mode = _story.parse_story_mode_text(text)
        except Exception:
            story_mode = None
        # Maze-local declarations are separate tables. Flattening all of them
        # multiplies repeated group 0 rewards by the number of layouts.
        dungeon_scope = re.split(r"(?is)\[maze(?:\s*info)?\]", text, maxsplit=1)[0]
        spo_items = _parse_special_passive_object_items(
            _section_text(dungeon_scope, "special passive object item")
        )
        linked_prev_ids, linked_next_dungeons = _parse_linked_dungeon(text)
        return DungeonFileInfo(
            dungeon_id=int(dungeon_id),
            path=path,
            mazes=mazes,
            difficulty_levels=difficulty_levels,
            minimum_required_level=min_req[0] if min_req else 0,
            basis_level=basis[0] if basis else 0,
            dungeon_type=dungeon_type,
            death_tower_map_indexes=death_tower_maps,
            tower_max_clear_item_num=(
                max(0, int(tower_max_clear_values[0]))
                if tower_max_clear_values
                else 0
            ),
            experience_increasing_point=float(exp_inc),
            story_mode=story_mode,
            hell_dungeon=bool(
                (_parse_int_list(_section_text(text, "hell dungeon")) or [0])[0]
            ),
            has_escape_hell=bool(re.search(r"(?is)\[escape\s+hell\]\s*1\b", text)),
            license_dungeon=bool(re.search(r"(?im)^\s*\[license dungeon\]\s*$", text)),
            no_experience_monster_and_result=bool(re.search(
                r"(?im)^\s*\[no experience monster and result\]\s*$", text
            )),
            special_passive_object_items=spo_items,
            boss_entrance_hunt_monsters=(
                _parse_boss_entrance_hunt_monsters(text)
            ),
            boss_entrance_summon_monsters=(
                _parse_boss_entrance_summon_monsters(text)
            ),
            linked_prev_dungeon_ids=linked_prev_ids,
            linked_next_dungeons=linked_next_dungeons,
        )

    def ensure_all_dungeons_loaded(self) -> None:
        """Parse every dungeon/*.dgn once into dungeon_cache (boot warm)."""
        self.ensure_loaded()
        if getattr(self, "_dungeons_fully_loaded", False):
            return
        with _LOCK:
            if getattr(self, "_dungeons_fully_loaded", False):
                return
            for did in list(self.dungeon_paths.keys()):
                if int(did) in self.dungeon_cache:
                    continue
                self.dungeon_cache[int(did)] = self._parse_dungeon_file(int(did))
            self._dungeons_fully_loaded = True

    def get_dungeon(self, dungeon_id: int) -> DungeonFileInfo:
        self.ensure_loaded()
        did = int(dungeon_id)
        cached = self.dungeon_cache.get(did)
        if cached is not None:
            return cached
        info = self._parse_dungeon_file(did)
        self.dungeon_cache[did] = info
        return info

    @staticmethod
    def _quest_connection_name(meta: QuestMeta | None) -> str:
        """Return the stable name key used to pair authored circle variants."""
        if meta is None:
            return ""
        return " ".join(str(meta.name or "").split()).casefold()

    @staticmethod
    def _quest_connection_signature(meta: QuestMeta | None) -> tuple[str, int, tuple[int, ...]]:
        """Return the PVF completion fields that must survive a maze mirror.

        Task names are presentation data. The frozen PVF has an authored
        counterexample where the normal and circle task names are swapped,
        while their type/subtype/int-data completion fields still identify the
        intended counterpart. A mirror may never replace those fields with a
        different task's condition merely because its displayed name matches.
        """
        if meta is None:
            return "", -1, ()
        return (
            _normalize_tag(str(meta.quest_type or "")),
            int(meta.sub_type),
            tuple(_parse_int_list(str(meta.int_data or ""))),
        )

    @staticmethod
    def _has_target_map_requirement(meta: QuestMeta | None) -> bool:
        """Whether this task has a concrete PVF ``[clear map]`` target."""
        signature = QuestIndex._quest_connection_signature(meta)
        return (
            signature[0] == "clear map"
            and bool(signature[2])
            and int(signature[2][0]) > 0
        )

    @staticmethod
    def _maze_contains_target_map(maze: MazeInfo, target_map_id: int) -> bool:
        """Check a parsed DGN maze without opening PVF in the hot path."""
        wanted = int(target_map_id)
        for spec in tuple(maze.map_specs or ()):
            if not isinstance(spec, dict):
                continue
            candidates = [spec.get("map_id")]
            candidates.extend(spec.get("candidates") or ())
            for raw_map_id in candidates:
                try:
                    if int(raw_map_id) == wanted:
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    def _mirror_candidate_supports_source(
        self,
        info: DungeonFileInfo,
        *,
        required_type: int,
        candidate_id: int,
        source: QuestMeta,
    ) -> bool:
        """Keep a mirrored target-map task on a maze that can satisfy it."""
        signature = self._quest_connection_signature(source)
        if signature[0] != "clear map" or not signature[2]:
            return True
        target_map_id = int(signature[2][0])
        for maze in info.mazes:
            qc = maze.quest_connection
            if (
                not qc
                or len(qc) < 2
                or int(qc[0]) != int(required_type)
                or (int(qc[1]) & 0xFFFF) != int(candidate_id)
            ):
                continue
            if self._maze_contains_target_map(maze, target_map_id):
                return True
        return False

    @staticmethod
    def _quest_declares_dungeon(meta: QuestMeta | None, dungeon_id: int) -> bool:
        """Whether a task explicitly scopes itself to this dungeon.

        ``[dungeon info]`` is stored as ``dungeon_id, difficulty`` pairs.  A
        same-name task in another dungeon is not a mirror counterpart, even if
        this dungeon happens to have a circle task with that name.
        """
        if meta is None:
            return False
        values = tuple(int(value) for value in (meta.dungeon_info or ()))
        wanted = int(dungeon_id)
        return any(
            int(values[offset]) == wanted
            for offset in range(0, len(values), 2)
        )

    def _mirror_connection_quest_ids(
        self,
        info: DungeonFileInfo,
        *,
        required_type: int,
        quest_ids: set[int],
    ) -> set[int]:
        """Find uniquely paired circle/non-circle task connections for one DGN.

        Some PVF families retain the normal mainline task and add a ``[circle]``
        task with the same name and dungeon scope, while only one side is named
        by ``[quest connection]`` in the DGN.  The selected maze belongs to the
        connection side only; the active task ID is deliberately left unchanged
        so its own target, drop, and settlement rules remain authoritative.

        Direct connections always win. A mirror must have the same explicitly
        declared dungeon, opposite circle status, exactly one candidate, and
        matching completion fields. Exact name remains the normal pairing key.
        When a localization/content pass has crossed the two names, a unique
        exact completion-field match is allowed instead, provided a
        ``[clear map]`` target is actually present in the candidate maze.
        These constraints keep unrelated duplicate task names out of the story
        maze path and make future mirrored maps data-driven.
        """
        if not quest_ids:
            return set()

        direct_ids: set[int] = set()
        direct_metas: dict[int, QuestMeta] = {}
        for maze in info.mazes:
            qc = maze.quest_connection
            if not qc or len(qc) < 2 or int(qc[0]) != int(required_type):
                continue
            quest_id = int(qc[1]) & 0xFFFF
            if quest_id <= 0:
                continue
            direct_ids.add(quest_id)
            meta = self.get_meta(quest_id)
            if meta is not None:
                direct_metas[quest_id] = meta

        if not direct_metas:
            return set()

        direct_by_name: dict[str, set[int]] = {}
        direct_by_signature: dict[tuple[str, int, tuple[int, ...]], set[int]] = {}
        for quest_id, meta in direct_metas.items():
            name_key = self._quest_connection_name(meta)
            if name_key:
                direct_by_name.setdefault(name_key, set()).add(quest_id)
            signature = self._quest_connection_signature(meta)
            if signature[0]:
                direct_by_signature.setdefault(signature, set()).add(quest_id)

        mirrored_ids: set[int] = set()
        for quest_id in quest_ids:
            # A direct connection which merely fails its difficulty gate must
            # not be bypassed through its counterpart.
            if int(quest_id) in direct_ids:
                continue
            source = self.get_meta(int(quest_id))
            if source is None:
                continue
            name_key = self._quest_connection_name(source)
            source_signature = self._quest_connection_signature(source)
            if not source_signature[0] or not self._quest_declares_dungeon(
                source, info.dungeon_id
            ):
                continue
            source_is_circle = _grade_tag(str(source.grade or "")) == "[circle]"
            name_candidates = {
                candidate_id
                for candidate_id in direct_by_name.get(name_key, set())
                if (
                    _grade_tag(str(direct_metas[candidate_id].grade or ""))
                    == "[circle]"
                )
                != source_is_circle
                and self._quest_connection_signature(direct_metas[candidate_id])
                == source_signature
            }
            candidates = name_candidates
            if not candidates:
                candidates = {
                    candidate_id
                    for candidate_id in direct_by_signature.get(source_signature, set())
                    if (
                        _grade_tag(str(direct_metas[candidate_id].grade or ""))
                        == "[circle]"
                    )
                    != source_is_circle
                }
            if len(candidates) == 1:
                candidate_id = next(iter(candidates))
                if self._mirror_candidate_supports_source(
                    info,
                    required_type=required_type,
                    candidate_id=candidate_id,
                    source=source,
                ):
                    mirrored_ids.add(candidate_id)
        return mirrored_ids

    def select_dungeon_maze(
        self,
        dungeon_id: int,
        *,
        difficulty: int = 0,
        active_quest_ids: Sequence[int] = (),
        finished_quest_ids: Sequence[int] = (),
    ) -> tuple[int, MazeInfo | None, str]:
        info = self.get_dungeon(dungeon_id)
        if not info.mazes:
            return -1, None, "no_maze"
        def ordered_ids(values: Sequence[int]) -> tuple[int, ...]:
            ordered: list[int] = []
            seen: set[int] = set()
            for raw_quest_id in values:
                try:
                    quest_id = int(raw_quest_id) & 0xFFFF
                except (TypeError, ValueError):
                    continue
                if quest_id <= 0 or quest_id in seen:
                    continue
                seen.add(quest_id)
                ordered.append(quest_id)
            return tuple(ordered)

        active_raw = ordered_ids(active_quest_ids)
        # The incoming active sequence is oldest-first from the repository.
        # Preserve that order inside each tier, while allowing a concrete
        # target-MAP task to choose the only maze on which it can progress
        # before a generic clear task claims a different story maze.
        active = tuple(
            quest_id
            for quest_id in active_raw
            if self._has_target_map_requirement(self.get_meta(quest_id))
        ) + tuple(
            quest_id
            for quest_id in active_raw
            if not self._has_target_map_requirement(self.get_meta(quest_id))
        )
        finished = ordered_ids(finished_quest_ids)

        def find(required_type: int, quest_id: int) -> int:
            for maze in info.mazes:
                qc = maze.quest_connection
                if not qc or len(qc) < 2:
                    continue
                if int(qc[0]) != int(required_type):
                    continue
                if (int(qc[1]) & 0xFFFF) != int(quest_id):
                    continue
                if (
                    required_type == 0
                    and len(qc) >= 3
                    and int(qc[2]) >= 0
                    and int(difficulty) < int(qc[2])
                ):
                    continue
                return maze.index
            return -1

        def find_ordered(required_type: int, quest_ids: Sequence[int]) -> tuple[int, str]:
            for quest_id in quest_ids:
                idx = find(required_type, quest_id)
                if idx >= 0:
                    return idx, "connection"
                mirror_ids = self._mirror_connection_quest_ids(
                    info,
                    required_type=required_type,
                    quest_ids={int(quest_id)},
                )
                for mirror_id in mirror_ids:
                    idx = find(required_type, mirror_id)
                    if idx >= 0:
                        return idx, "connection_mirror"
            return -1, ""

        idx, reason = find_ordered(0, active)
        if idx >= 0:
            return idx, info.mazes[idx], "active_" + reason
        idx, reason = find_ordered(1, finished)
        if idx >= 0:
            return idx, info.mazes[idx], "finished_" + reason
        free = [m for m in info.mazes if not m.quest_connection]
        if free:
            return free[0].index, free[0], "no_connection"
        return info.mazes[0].index, info.mazes[0], "fallback_first"


def get_quest_index() -> QuestIndex:
    global _INDEX
    with _LOCK:
        if _INDEX is None:
            _INDEX = QuestIndex()
        return _INDEX


def get_warmed_dungeon_permission_clear_state(dungeon_id: int) -> int | None:
    """Return a boot-warmed PVF difficulty state without any runtime PVF read.

    Select-dungeon is a hot path.  The hard boot sequence already fully warms
    ``QuestIndex.dungeon_cache``; callers fall back to their existing state
    when unit tests or diagnostic tools have not completed that warmup.
    """
    with _LOCK:
        index = _INDEX
        if index is None or not index.loaded or not index._dungeons_fully_loaded:
            return None
        dungeon = index.dungeon_cache.get(int(dungeon_id))
        if dungeon is None:
            return None
        return dungeon.permission_clear_state()


def warmup_quest_index(
    *,
    full_metas: bool = True,
    full_dungeons: bool = True,
    warm_acceptable: bool | None = None,
    acceptable_job_max: int = 16,
    cache: Any = None,
) -> dict[str, int | str]:
    """Boot-time quest/dungeon index warmup.

    Hard standard: load path maps and, by default, parse every quest meta and
    every dungeon file so SELECT_CHARACTER / enter-dungeon never pays first-
    touch cold PVF parse cost.
    """
    idx = get_quest_index()
    cache_status = None
    if cache is not None and full_metas and full_dungeons:
        from game.quest.quest_index_cache import (
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            install,
        )

        payload = cache.read(CACHE_DOMAIN, CACHE_SEMANTIC_VERSION)
        if payload is not None:
            try:
                install(idx, payload)
                cache_status = "hit"
            except Exception:
                cache.discard(CACHE_DOMAIN)
                cache_status = "rebuild"
    try:
        pvf_runtime.ensure_shared_reader()
    except Exception as exc:
        return {"ok": 0, "error": str(exc)}
    if cache_status != "hit":
        idx.ensure_loaded()
        if full_metas:
            idx.ensure_all_metas_loaded()
        if full_dungeons:
            idx.ensure_all_dungeons_loaded()
    do_acc = (
        bool(warm_acceptable)
        if warm_acceptable is not None
        else _env_bool("USDOF_GAME_QUEST_BOOT_WARM_ACCEPTABLE", True)
    )
    acceptable_warmed = 0
    if do_acc:
        job_max = max(0, int(acceptable_job_max))
        for job in range(0, job_max):
            idx.compute_acceptable(
                level=1,
                job=job,
                grow_type=0,
                finished_ids=(),
                active_ids=(),
            )
            acceptable_warmed += 1
    result = {
        "ok": 1,
        "quest_paths": len(idx.quest_paths),
        "quest_metas": len(idx.metas),
        "quest_count": len(idx.metas),
        "metas_fully_loaded": int(bool(getattr(idx, "_metas_fully_loaded", False))),
        "connect_quest_units": len(idx.connect_quest_units),
        "episode_quest_units": len(idx.episode_quest_units),
        "episode_sequence_rows": sum(
            len(unit.sequence) for unit in idx.episode_quest_units
        ),
        "epic_sequence_quests": len(idx.epic_sequence_ids),
        "unsequenced_epic_quests": len(idx.unsequenced_epic_ids),
        "dungeon_count": len(idx.dungeon_paths),
        "dungeon_cached": len(idx.dungeon_cache),
        "dungeons_fully_loaded": int(bool(getattr(idx, "_dungeons_fully_loaded", False))),
        "unlock_seeds": len(idx.local_unlock_ids),
        "acceptable_warmed": acceptable_warmed,
        "source": idx.source,
    }
    if cache is not None and full_metas and full_dungeons:
        from game.quest.quest_index_cache import (
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            to_payload,
        )

        if cache_status != "hit":
            cache.write(
                CACHE_DOMAIN,
                CACHE_SEMANTIC_VERSION,
                to_payload(idx),
                record_count=len(idx.metas),
            )
            cache_status = str(cache.last_status.get(CACHE_DOMAIN) or "miss")
        result["cache"] = cache_status
    return result


def compute_acceptable_quest_ids(
    *,
    level: int,
    job: int,
    grow_type: int = 0,
    finished_ids: Sequence[int] = (),
    finished_flags: Mapping[int, int] | None = None,
    active_ids: Sequence[int] = (),
    daily_completed_ids: Sequence[int] = (),
) -> list[int]:
    return get_quest_index().compute_acceptable(
        level=level,
        job=job,
        grow_type=grow_type,
        finished_ids=finished_ids,
        finished_flags=finished_flags,
        active_ids=active_ids,
        daily_completed_ids=daily_completed_ids,
    )


def compute_quest_init_trigger(
    quest_id: int,
    finished_quest_ids: Sequence[int] = (),
) -> int:
    return get_quest_index().compute_init_trigger(
        quest_id,
        finished_quest_ids=finished_quest_ids,
    )


def compute_quest_clear_trigger(
    quest_id: int,
    finished_quest_ids: Sequence[int] = (),
) -> int | None:
    return quest_clear_trigger(
        get_quest_index().get_meta(quest_id),
        finished_quest_ids,
    )



def peek_local_unlock_quest_ids() -> list[int]:
    """Return unlock seeds without loading full quest metas.

    Order: env -> resources/quest_local_unlock_ids.json -> empty.
    """
    raw = (os.environ.get("USDOF_GAME_QUEST_LOCAL_UNLOCK_IDS") or "").strip()
    if raw:
        return [int(x) & 0xFFFF for x in re.findall(r"\d+", raw) if int(x) > 0]
    try:
        from config import BASE_DIR
        import json
        path = BASE_DIR / "data" / "resources" / "quest_local_unlock_ids.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            ids = payload.get("quest_ids") or payload.get("ids") or []
            return [int(x) & 0xFFFF for x in ids if int(x) > 0]
    except Exception:
        pass
    return []


def local_offline_unlock_quest_ids() -> list[int]:
    """Unlock seeds only (never forces full quest meta load)."""
    return peek_local_unlock_quest_ids()


