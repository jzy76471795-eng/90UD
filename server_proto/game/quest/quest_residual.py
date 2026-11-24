"""Quest residual progress (kill count / collect / SET_TRIGGER channels).

Stage3 对齐依据：
- S4A12 ``QuestService`` packed trigger 9-bit×3 通道 + SET_TRIGGER type 掩码
- 90qiyuan ``hunt_enemy.go`` 杀怪按 PVF int-data 匹配通道并递减
- 本地 ``quest_index.compute_init_trigger`` 已用同一 packing 生成 initTrigger

本模块只做纯计算 + 基于共享 PVF meta 的匹配；DB/发包由 chain 接线。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Optional, Sequence

from pvf import pvf_runtime
from game.quest.quest_index import (
    QuestMeta,
    SeekingCaptureSpec,
    get_quest_index,
    seeking_capture_spec,
    use_item_targets,
)


CHANNEL_MASK = 0x1FF
CHANNEL_BITS = 9


@lru_cache(maxsize=4096)
def _has_slot_expansion_attribute(raw_text: str) -> bool:
    return bool(re.search(
        r"(?im)^\s*\[attribute\]\s*\n\s*`slot expansion`\s*$",
        raw_text or "",
    ))


def hunt_kill_difficulty_matches(
    meta: QuestMeta,
    required_difficulty: int,
    actual_difficulty: int,
) -> bool:
    """Equipment-slot system hunt tasks use their authored minimum difficulty.

    Other task domains retain exact difficulty matching.  The same predicate
    is used by actor selection and kill progress, with no runtime PVF reads.
    """
    required = int(required_difficulty)
    actual = int(actual_difficulty)
    if required < 0 or required == actual:
        return True
    if actual < required:
        return False
    return (
        _normalize_type(getattr(meta, "grade", "")) == "system"
        and _normalize_type(getattr(meta, "quest_type", "")) == "hunt monster"
        and _has_slot_expansion_attribute(str(getattr(meta, "raw_text", "") or ""))
    )


def pack_trigger_channels(f0: int = 0, f1: int = 0, f2: int = 0) -> int:
    return (
        ((int(f2) & CHANNEL_MASK) << 18)
        | ((int(f1) & CHANNEL_MASK) << 9)
        | (int(f0) & CHANNEL_MASK)
    ) & 0xFFFFFFFF


def unpack_trigger_channels(trigger: int) -> tuple[int, int, int]:
    t = int(trigger) & 0xFFFFFFFF
    return (
        t & CHANNEL_MASK,
        (t >> 9) & CHANNEL_MASK,
        (t >> 18) & CHANNEL_MASK,
    )


def adjust_trigger_channel(trigger: int, channel: int, delta: int) -> int:
    """channel: 0/1/2 → bits 0/9/18。"""
    ch = int(channel)
    if ch < 0 or ch > 2:
        return int(trigger) & 0xFFFFFFFF
    shift = ch * CHANNEL_BITS
    t = int(trigger) & 0xFFFFFFFF
    cur = (t >> shift) & CHANNEL_MASK
    nxt = max(0, min(CHANNEL_MASK, int(cur) + int(delta)))
    t = (t & ~(CHANNEL_MASK << shift)) | ((nxt & CHANNEL_MASK) << shift)
    return t & 0xFFFFFFFF


def replace_trigger_channel(trigger: int, channel: int, value: int) -> int:
    """Replace one packed 9-bit trigger channel without changing the others."""
    ch = int(channel)
    if ch < 0 or ch > 2:
        return int(trigger) & 0xFFFFFFFF
    shift = ch * CHANNEL_BITS
    next_value = max(0, min(CHANNEL_MASK, int(value)))
    current = int(trigger) & 0xFFFFFFFF
    return (
        (current & ~(CHANNEL_MASK << shift))
        | ((next_value & CHANNEL_MASK) << shift)
    ) & 0xFFFFFFFF


def decrement_trigger_channel(trigger: int, channel: int) -> int:
    return adjust_trigger_channel(trigger, channel, -1)


def apply_set_trigger_delta(
    old_trigger: int,
    *,
    trigger_type: int,
    is_increment: bool,
) -> int:
    """S4A12 SET_TRIGGER 语义。

    - trigger_type == 1: raw +1
    - trigger_type == 0: raw -1（到 0 为止）
    - trigger_type 含 0x10/0x20/0x40: 通道 0/1/2 增减
    - is_increment: True 增 / False 减
    """
    old = int(old_trigger) & 0xFFFFFFFF
    tt = int(trigger_type) & 0xFF
    if tt == 1:
        return (old + 1) & 0xFFFFFFFF
    if tt == 0:
        if is_increment:
            return (old + 1) & 0xFFFFFFFF
        return (old - 1) & 0xFFFFFFFF if old > 0 else 0
    new = old
    if is_increment:
        if tt & 0x10:
            new = adjust_trigger_channel(new, 0, 1)
        if tt & 0x20:
            new = adjust_trigger_channel(new, 1, 1)
        if tt & 0x40:
            new = adjust_trigger_channel(new, 2, 1)
    else:
        if old == 0:
            return 0
        if tt & 0x10:
            new = adjust_trigger_channel(new, 0, -1)
        if tt & 0x20:
            new = adjust_trigger_channel(new, 1, -1)
        if tt & 0x40:
            new = adjust_trigger_channel(new, 2, -1)
    return int(new) & 0xFFFFFFFF


def _normalize_type(tag: str) -> str:
    t = (tag or "").strip().lower()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
    return t


def _parse_int_list(text: str) -> list[int]:
    out: list[int] = []
    for tok in (text or "").replace(",", " ").split():
        try:
            if tok.startswith("0x") or tok.startswith("0X"):
                out.append(int(tok, 16))
            else:
                out.append(int(tok, 10))
        except Exception:
            continue
    return out


@dataclass(frozen=True)
class KillMatch:
    quest_id: int
    channel: int
    quest_type: str
    path: str = ""


@dataclass(frozen=True)
class SeekingCaptureUse:
    """One validated native capture-tool target in the current dungeon room."""

    spec: SeekingCaptureSpec
    target_seq: int
    target_code: int
    target_actor_identity: int


@dataclass(frozen=True)
class PendingSeekingCaptureDrop:
    """One scanner result that must become a ground drop on its target's DIE."""

    capture: SeekingCaptureUse
    dungeon_id: int
    dungeon_seed: int


_PENDING_SEEKING_CAPTURE_ATTR = "quest_pending_seeking_capture_drop"


def _session_dungeon_id(session: object | None) -> int:
    if session is None:
        return 0
    for attr in ("selected_dungeon_id", "cur_dungeon_id"):
        try:
            value = int(getattr(session, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _session_dungeon_seed(session: object | None) -> int:
    if session is None:
        return 0
    try:
        return int(getattr(session, "dungeon_seed", 0) or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def _clear_pending_seeking_capture_drop(session: object | None) -> None:
    if session is None:
        return
    try:
        setattr(session, _PENDING_SEEKING_CAPTURE_ATTR, None)
    except Exception:
        pass


def pending_seeking_capture_drop(
    session: object | None,
) -> tuple[PendingSeekingCaptureDrop | None, str]:
    """Return the current-run scanner result, clearing stale session state."""
    if session is None:
        return None, "capture_drop=none"
    pending = getattr(session, _PENDING_SEEKING_CAPTURE_ATTR, None)
    if not isinstance(pending, PendingSeekingCaptureDrop):
        return None, "capture_drop=none"
    if not bool(getattr(session, "in_dungeon_run", False)):
        _clear_pending_seeking_capture_drop(session)
        return None, "capture_drop=stale_not_in_dungeon"
    if (
        _session_dungeon_id(session) != int(pending.dungeon_id)
        or _session_dungeon_seed(session) != int(pending.dungeon_seed)
    ):
        _clear_pending_seeking_capture_drop(session)
        return None, "capture_drop=stale_run"
    return pending, "capture_drop=pending q=%d target=%d/%d" % (
        int(pending.capture.spec.quest_id),
        int(pending.capture.target_code),
        int(pending.capture.target_seq),
    )


def arm_seeking_capture_ground_drop(
    session: object | None,
    capture: SeekingCaptureUse,
) -> tuple[bool, str]:
    """Bind a validated scanner use to one live target in this dungeon run."""
    if session is None:
        return False, "capture_drop=reject no_session"
    pending, _pending_note = pending_seeking_capture_drop(session)
    if pending is not None:
        return False, "capture_drop=reject already_armed q=%d target=%d/%d" % (
            int(pending.capture.spec.quest_id),
            int(pending.capture.target_code),
            int(pending.capture.target_seq),
        )
    if not bool(getattr(session, "in_dungeon_run", False)):
        return False, "capture_drop=reject not_in_dungeon"
    dungeon_id = _session_dungeon_id(session)
    if dungeon_id != int(capture.spec.dungeon_id):
        return False, "capture_drop=reject dungeon=%d expected=%d" % (
            dungeon_id,
            int(capture.spec.dungeon_id),
        )
    try:
        pending = PendingSeekingCaptureDrop(
            capture=capture,
            dungeon_id=int(dungeon_id),
            dungeon_seed=_session_dungeon_seed(session),
        )
        setattr(session, _PENDING_SEEKING_CAPTURE_ATTR, pending)
    except Exception as exc:
        return False, "capture_drop=reject session_store=%s" % type(exc).__name__
    return True, "capture_drop=armed q=%d item=%d target=%d/%d" % (
        int(capture.spec.quest_id),
        int(capture.spec.output_item_id),
        int(capture.target_code),
        int(capture.target_seq),
    )


def consume_seeking_capture_ground_drop(
    session: object | None,
    *,
    target_seq: int,
    target_code: int,
    target_actor: object | None,
) -> tuple[SeekingCaptureUse | None, str]:
    """Claim the scanner result only for the exact actor that was captured."""
    pending, pending_note = pending_seeking_capture_drop(session)
    if pending is None:
        return None, pending_note
    capture = pending.capture
    if (
        int(target_seq) != int(capture.target_seq)
        or int(target_code) != int(capture.target_code)
    ):
        return None, "capture_drop=wait q=%d target=%d/%d got=%d/%d" % (
            int(capture.spec.quest_id),
            int(capture.target_code),
            int(capture.target_seq),
            int(target_code),
            int(target_seq),
        )
    expected_identity = int(capture.target_actor_identity or 0)
    if (
        expected_identity > 0
        and target_actor is not None
        and int(id(target_actor)) != expected_identity
    ):
        return None, "capture_drop=wait target_identity"
    _clear_pending_seeking_capture_drop(session)
    return capture, "capture_drop=match q=%d item=%d target=%d/%d" % (
        int(capture.spec.quest_id),
        int(capture.spec.output_item_id),
        int(capture.target_code),
        int(capture.target_seq),
    )


def resolve_seeking_capture_use(
    active_pairs: Sequence[tuple[int, int]],
    *,
    item_id: int,
    session: object | None,
) -> tuple[SeekingCaptureUse | None, str]:
    """Validate a scanner item use against the active quest maze and target.

    C1/002C only identifies the bag stack.  The native Stage3 item script
    performs the target and sub-20-percent HP check locally, while this server
    gate binds that successful use to the matching active quest maze and one
    live actor whose PVF ``[catch item]`` declares the required quest material.
    """
    used_item_id = int(item_id or 0)
    if used_item_id <= 0:
        return None, "capture=none"

    spec, spec_note = _resolve_active_seeking_capture_spec(
        active_pairs,
        item_id=used_item_id,
        note_prefix="capture",
    )
    if spec is None:
        return None, spec_note
    capture, capture_note = _resolve_seeking_capture_target(
        spec,
        session=session,
        note_prefix="capture",
    )
    if capture is None:
        return None, capture_note
    return (
        capture,
        "capture=match quest=%d tool=%d target=%d/%d"
        % (
            int(spec.quest_id),
            used_item_id,
            int(capture.target_code),
            int(capture.target_seq),
        ),
    )


def _resolve_active_seeking_capture_spec(
    active_pairs: Sequence[tuple[int, int]],
    *,
    item_id: Optional[int],
    note_prefix: str,
) -> tuple[SeekingCaptureSpec | None, str]:
    """Select one active PVF capture task, optionally by its supplied tool."""
    index = get_quest_index()
    candidates: list[SeekingCaptureSpec] = []
    for raw_quest_id, raw_trigger in active_pairs or ():
        quest_id = int(raw_quest_id) & 0xFFFF
        if quest_id <= 0 or int(raw_trigger) == 0:
            continue
        spec = seeking_capture_spec(index.get_meta(quest_id))
        if spec is None:
            continue
        if item_id is not None and int(spec.tool_item_id) != int(item_id):
            continue
        candidates.append(spec)
    if not candidates:
        return None, "%s=none" % note_prefix
    if len(candidates) != 1:
        return None, "%s=reject ambiguous_active_specs=%d" % (
            note_prefix,
            len(candidates),
        )
    return candidates[0], "%s=active q=%d" % (
        note_prefix,
        int(candidates[0].quest_id),
    )


_CATCH_ITEM_SECTION = re.compile(
    r"(?is)\[catch\s+item\](.*?)(?:\[/catch\s+item\]|(?=\n\s*\[)|\Z)"
)


@lru_cache(maxsize=1)
def _monster_script_paths() -> dict[int, str]:
    """Return the shared-PVF monster script path keyed by monster code."""
    try:
        from game.drop.pvf_section import parse_lst_entries

        text = pvf_runtime.read_text("monster/monster.lst")
    except Exception:
        return {}
    paths: dict[int, str] = {}
    for raw_code, raw_path in parse_lst_entries(text or ""):
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            continue
        path = str(raw_path or "").replace("\\", "/").lstrip("/")
        if code <= 0 or not path:
            continue
        if not path.lower().startswith("monster/"):
            path = "monster/" + path
        paths[code] = path
    return paths


@lru_cache(maxsize=4096)
def _monster_catch_items(monster_code: int) -> tuple[tuple[int, int, int], ...]:
    """Parse one monster's native ``[catch item]`` rows from the shared PVF."""
    try:
        code = int(monster_code)
    except (TypeError, ValueError):
        return ()
    path = _monster_script_paths().get(code, "")
    if not path:
        return ()
    try:
        raw = pvf_runtime.read_text(path)
    except Exception:
        return ()
    match = _CATCH_ITEM_SECTION.search(raw or "")
    if match is None:
        return ()
    values = _parse_int_list(match.group(1))
    rows: list[tuple[int, int, int]] = []
    for offset in range(0, len(values) - 2, 3):
        item_id = int(values[offset])
        count = int(values[offset + 1])
        rate = int(values[offset + 2])
        if item_id > 0 and count > 0 and rate > 0:
            rows.append((item_id, count, rate))
    return tuple(rows)


def _actor_declares_capture_item(
    actor: object,
    *,
    item_id: int,
    required_count: int,
) -> bool:
    if not isinstance(actor, dict):
        return False
    if bool(actor.get("killed")) or bool(actor.get("is_ally")):
        return False
    if actor.get("is_hostile") is False or actor.get("killable") is False:
        return False
    try:
        monster_code = int(actor.get("code") or actor.get("monster_code") or 0)
    except (TypeError, ValueError):
        return False
    for catch_item_id, catch_count, _catch_rate in _monster_catch_items(monster_code):
        if int(catch_item_id) == int(item_id) and int(catch_count) >= int(required_count):
            return True
    return False


def _resolve_seeking_capture_target(
    spec: SeekingCaptureSpec,
    *,
    session: object | None,
    note_prefix: str,
) -> tuple[SeekingCaptureUse | None, str]:
    """Resolve the only live native catch-item actor for a capture quest."""
    if session is None or not bool(getattr(session, "in_dungeon_run", False)):
        return None, "%s=reject not_in_dungeon" % note_prefix
    dungeon_id = _session_dungeon_id(session)
    if dungeon_id != int(spec.dungeon_id):
        return None, "%s=reject dungeon=%d expected=%d" % (
            note_prefix,
            dungeon_id,
            int(spec.dungeon_id),
        )
    try:
        maze_index = int(getattr(session, "dungeon_maze_index", -1))
        dungeon = get_quest_index().get_dungeon(dungeon_id)
        maze = dungeon.mazes[maze_index]
    except (AttributeError, IndexError, TypeError, ValueError):
        return None, "%s=reject maze_unresolved" % note_prefix
    connection = {
        int(value) & 0xFFFF
        for value in (getattr(maze, "quest_connection", None) or ())
        if int(value) > 0
    }
    if int(spec.quest_id) not in connection:
        return None, "%s=reject maze_connection" % note_prefix
    targets: list[tuple[int, int, int]] = []
    for actor in list(getattr(session, "dungeon_room_mons", None) or ()):
        if not _actor_declares_capture_item(
            actor,
            item_id=int(spec.output_item_id),
            required_count=int(spec.output_count),
        ):
            continue
        try:
            actor_seq = int(actor.get("seq") or 0)
            actor_code = int(actor.get("code") or actor.get("monster_code") or 0)
        except (TypeError, ValueError):
            continue
        if actor_seq <= 0 or actor_code <= 0:
            continue
        targets.append((actor_seq, actor_code, int(id(actor))))
    if len(targets) != 1:
        return None, "%s=reject live_capture_targets=%d" % (
            note_prefix,
            len(targets),
        )
    target_seq, target_code, target_actor_identity = targets[0]
    return (
        SeekingCaptureUse(
            spec=spec,
            target_seq=int(target_seq),
            target_code=int(target_code),
            target_actor_identity=int(target_actor_identity),
        ),
        "%s=target q=%d target=%d/%d"
        % (
            note_prefix,
            int(spec.quest_id),
            int(target_code),
            int(target_seq),
        ),
    )


def resolve_seeking_capture_target_die(
    active_pairs: Sequence[tuple[int, int]],
    *,
    session: object | None,
    target_seq: int,
    target_code: int,
    target_actor: object | None,
) -> tuple[SeekingCaptureUse | None, str]:
    """Match a terminal capture-target event to a PVF seeking-capture task.

    Stage3's native throw script owns the success check and does not emit a
    C1/002C use packet.  Like the generic QuestDropService flow, the server
    therefore derives the result from the verified terminal actor event.
    """
    spec, spec_note = _resolve_active_seeking_capture_spec(
        active_pairs,
        item_id=None,
        note_prefix="capture_death",
    )
    if spec is None:
        return None, spec_note
    capture, capture_note = _resolve_seeking_capture_target(
        spec,
        session=session,
        note_prefix="capture_death",
    )
    if capture is None:
        return None, capture_note
    if (
        int(target_seq) != int(capture.target_seq)
        or int(target_code) != int(capture.target_code)
    ):
        return None, "capture_death=skip target=%d/%d expected=%d/%d" % (
            int(target_code),
            int(target_seq),
            int(capture.target_code),
            int(capture.target_seq),
        )
    if (
        target_actor is None
        or int(id(target_actor)) != int(capture.target_actor_identity)
    ):
        return None, "capture_death=skip target_identity"
    return capture, "capture_death=match q=%d item=%d target=%d/%d" % (
        int(capture.spec.quest_id),
        int(capture.spec.output_item_id),
        int(capture.target_code),
        int(capture.target_seq),
    )


def match_hunt_kill_channel(
    meta: QuestMeta,
    *,
    monster_code: int,
    dungeon_id: int = -1,
    difficulty: int = -1,
    enemy_type: int = 1,
    enemy_rank: int = -1,
) -> int | None:
    """匹配 kill 应递减的 packed 通道下标；无匹配返回 None。"""
    type_tag = _normalize_type(meta.quest_type)
    values = _parse_int_list(meta.int_data)
    mon = int(monster_code)
    if mon <= 0 or not values:
        return None

    if type_tag == "hunt enemy":
        # dungeon, difficulty, enemyCode, enemyType, count  — stride 5
        stride = 5
        for ch, off in enumerate(range(0, len(values) - stride + 1, stride)):
            if ch > 2:
                break
            t_dungeon = values[off]
            t_diff = values[off + 1]
            t_code = values[off + 2]
            t_etype = values[off + 3]
            if t_etype <= 0:
                t_etype = 1
            if t_dungeon != -1 and t_dungeon != int(dungeon_id):
                continue
            if t_diff >= 0 and t_diff != int(difficulty):
                continue
            if int(enemy_type) > 0 and t_etype != int(enemy_type):
                continue
            if t_code > 0 and t_code != mon:
                continue
            if t_code == -1 and int(enemy_rank) != 0:
                continue
            if t_code == -2 and int(enemy_rank) not in {1, 2}:
                continue
            if t_code == -3 and int(enemy_rank) != 3:
                continue
            if t_code <= 0 and t_code not in {-1, -2, -3}:
                continue
            return ch
        return None

    if type_tag == "hunt monster":
        # Frozen Stage3 PVF audit (2026-07-29): all 103 epic quests / 127
        # target rows use dungeon, difficulty, monsterCode, count.  The first
        # two values are authoritative scope, not alternate target fields.
        stride = 4
        for ch, off in enumerate(range(0, len(values) - stride + 1, stride)):
            if ch > 2:
                break
            t_dungeon = int(values[off])
            t_diff = int(values[off + 1])
            t_code = int(values[off + 2])
            if t_code != mon:
                continue
            if t_dungeon != -1 and t_dungeon != int(dungeon_id):
                continue
            if not hunt_kill_difficulty_matches(meta, t_diff, int(difficulty)):
                continue
            return ch
        return None

    if type_tag == "hunting":
        # Preserve the pre-existing legacy alias outside the audited Stage3
        # [hunt monster] domain.  No epic quest in the frozen PVF uses it.
        for ch, off in enumerate(range(0, len(values) - 1, 2)):
            if ch > 2:
                break
            if int(values[off]) == mon:
                return ch
        return None

    return None


def apply_kill_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    monster_code: int,
    dungeon_id: int = -1,
    difficulty: int = -1,
    enemy_type: int = 1,
    enemy_rank: int = -1,
) -> list[tuple[int, int, int, str]]:
    """对进行中任务应用一次击杀。

    返回 [(quest_id, old_trigger, new_trigger, note), ...] 仅含变化项。
    """
    idx = get_quest_index()
    changed: list[tuple[int, int, int, str]] = []
    for qid, trig in list(active_pairs or ()):
        q = int(qid) & 0xFFFF
        old = int(trig) & 0xFFFFFFFF
        if q <= 0 or old == 0:
            continue
        meta = idx.get_meta(q)
        if meta is None:
            continue
        ch = match_hunt_kill_channel(
            meta,
            monster_code=int(monster_code),
            dungeon_id=int(dungeon_id),
            difficulty=int(difficulty),
            enemy_type=int(enemy_type),
            enemy_rank=int(enemy_rank),
        )
        if ch is None:
            continue
        new = decrement_trigger_channel(old, ch)
        if new == old:
            continue
        note = "kill q=%s type=%s ch=%s %s→%s" % (
            q,
            _normalize_type(meta.quest_type),
            ch,
            old,
            new,
        )
        changed.append((q, old, new, note))
    return changed


def normalize_enemy_rank(actor_type: int, enemy_type: int) -> int:
    """Map Stage3 wire actor types to PVF hunt-enemy rank selectors.

    Frozen PVF labels selector -1 as normal, -2 as named, and -3 as boss.
    Monster wire types already use 0..3.  APC wire types use 5..8 with the
    same normal/champion/super-champion/boss ordering.
    """
    wire_type = int(actor_type)
    domain = int(enemy_type)
    if domain == 1 and wire_type in {0, 1, 2, 3}:
        return wire_type
    if domain == 2 and wire_type in {5, 6, 7, 8}:
        return wire_type - 5
    return -1


def _seeking_requirement_pairs(meta: QuestMeta) -> list[tuple[int, int]]:
    """Return valid ``(resource_id, count)`` pairs, preserving gold id 0."""
    quest_type = _normalize_type(getattr(meta, "quest_type", ""))
    values = _parse_int_list(getattr(meta, "int_data", ""))
    if quest_type == "seek n meet npc":
        if len(values) >= 2 and int(values[0]) > 0 and int(values[1]) > 0:
            return [(int(values[0]), int(values[1]))]
        return []
    if quest_type != "seeking":
        return []
    pairs: list[tuple[int, int]] = []
    for i in range(0, len(values) - 1, 2):
        iid = int(values[i])
        cnt = int(values[i + 1])
        if iid >= 0 and cnt > 0:
            pairs.append((iid, cnt))
    return pairs


def seeking_required_pairs(meta: QuestMeta) -> list[tuple[int, int]]:
    """Return positive item requirements for item-seeking quest types."""
    capture = seeking_capture_spec(meta)
    if capture is not None:
        return [(int(capture.output_item_id), int(capture.output_count))]
    return [
        (resource_id, count)
        for resource_id, count in _seeking_requirement_pairs(meta)
        if int(resource_id) > 0
    ]


def seeking_completion_consume_pairs(meta: QuestMeta) -> list[tuple[int, int]]:
    """Return inventory stacks that a successful quest submission consumes.

    A capture task progresses only from its generated capture material, so its
    supplied throw tool must stay out of ``seeking_required_pairs``.  The
    native capture path can omit C1/002C, however, leaving that tool in the
    server inventory until task submission.  Finish cleanup consumes both
    stacks while the residual calculator continues to depend only on the
    capture material.
    """
    capture = seeking_capture_spec(meta)
    if capture is not None:
        return [
            (int(capture.output_item_id), int(capture.output_count)),
            (int(capture.tool_item_id), int(capture.tool_count)),
        ]
    return seeking_required_pairs(meta)


def seeking_required_gold(meta: QuestMeta) -> int:
    """Return the wallet-gold requirement encoded with resource id 0."""
    return sum(
        int(count)
        for resource_id, count in _seeking_requirement_pairs(meta)
        if int(resource_id) == 0
    )


def compute_seeking_trigger(
    meta: QuestMeta,
    *,
    held_count_by_item: dict[int, int],
    current_trigger: int = 0,
) -> int | None:
    """Recompute the item residual from current holdings.

    Plain seeking uses the missing total as its scalar trigger.  Seek-and-meet
    replaces only packed channel 0, preserving the NPC condition in channel 1.
    """
    type_tag = _normalize_type(getattr(meta, "quest_type", ""))
    pairs = seeking_required_pairs(meta)
    if not pairs:
        if type_tag not in {"seeking", "seek n meet npc"}:
            return None
        if type_tag == "seek n meet npc":
            return replace_trigger_channel(current_trigger, 0, 0)
        return 0
    missing = 0
    for iid, need in pairs:
        held = int(held_count_by_item.get(int(iid), 0) or 0)
        if held < need:
            missing += need - held
    if type_tag == "seek n meet npc":
        return replace_trigger_channel(current_trigger, 0, missing)
    return int(missing) & 0xFFFFFFFF


def apply_seeking_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    held_count_by_item: dict[int, int],
    item_filter: Sequence[int] | None = None,
) -> list[tuple[int, int, int, str]]:
    """收集类任务 residual 重算。

    item_filter 非空时只处理需要这些道具的 seeking 任务。
    """
    idx = get_quest_index()
    filt = {int(x) for x in (item_filter or ()) if int(x) > 0}
    changed: list[tuple[int, int, int, str]] = []
    for qid, trig in list(active_pairs or ()):
        q = int(qid) & 0xFFFF
        old = int(trig) & 0xFFFFFFFF
        if q <= 0:
            continue
        meta = idx.get_meta(q)
        if meta is None:
            continue
        pairs = seeking_required_pairs(meta)
        if not pairs:
            continue
        if filt and not any(iid in filt for iid, _n in pairs):
            continue
        new = compute_seeking_trigger(
            meta,
            held_count_by_item=held_count_by_item,
            current_trigger=old,
        )
        if new is None or int(new) == old:
            continue
        note = "seeking q=%s %s→%s" % (q, old, new)
        changed.append((q, old, int(new), note))
    return changed


def apply_used_item_to_active_quests(
    active_pairs: Sequence[tuple[int, int]],
    *,
    item_id: int,
    used_count: int = 1,
) -> list[tuple[int, int, int, str]]:
    """Apply a successfully consumed stackable to active use-item quests."""
    used_item_id = int(item_id)
    delta = max(0, int(used_count))
    if used_item_id <= 0 or delta <= 0:
        return []
    index = get_quest_index()
    changed: list[tuple[int, int, int, str]] = []
    for raw_quest_id, raw_trigger in active_pairs or ():
        quest_id = int(raw_quest_id) & 0xFFFF
        old = int(raw_trigger) & 0xFFFFFFFF
        if quest_id <= 0 or old == 0:
            continue
        meta = index.get_meta(quest_id)
        targets = use_item_targets(meta)
        if not any(int(target_id) == used_item_id for target_id, _count in targets):
            continue
        new = max(0, old - delta)
        if new == old:
            continue
        changed.append(
            (
                quest_id,
                old,
                new,
                "use_item q=%s item=%s %s->%s"
                % (quest_id, used_item_id, old, new),
            )
        )
    return changed
