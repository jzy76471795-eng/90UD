"""Dungeon in-run room state (session-local, temporary).

本局地下城内：已清完怪的格子不再刷怪；离开地图 / 离开副本时重置。
对齐常见 DNF 行为：房间清完后再进为空房，不重刷。

每个首次进入的 ``(maze, cell, map)`` 保存 actor、seed 与首序号；重访时
客户端走 ``001D room_flag=0`` 并复用自己的房间对象图，服务端重新绑定同一
actor 列表。这样怪物的部分击杀、机关状态和建筑状态都不会因重新解析 PVF
而复位。

另：可破坏建筑（type9 / ``[passive object]``）本局摧毁后按
``(map_index, code, x, y)`` 记录，重进同图不再刷该建筑
（蚁后巢穴卵/蚁穴等；与清房 mon=0 正交——房间仍可能有怪）。
"""
from __future__ import annotations

from typing import Any, Iterable


def cell_key(cell_x: int, cell_y: int) -> tuple[int, int]:
    return (int(cell_x) & 0xFF, int(cell_y) & 0xFF)


def destroyed_object_key(
    map_index: int,
    object_code: int,
    x: int = 0,
    y: int = 0,
) -> tuple[int, int, int, int]:
    """本局已摧毁建筑键：map + object code + 坐标（同 code 多实例可区分）。"""
    return (
        int(map_index) & 0xFFFFFFFF,
        int(object_code) & 0xFFFFFFFF,
        int(x) & 0xFFFFFFFF,
        int(y) & 0xFFFFFFFF,
    )


def room_state_key(
    maze_index: int,
    cell_x: int,
    cell_y: int,
    map_index: int,
) -> tuple[int, int, int, int]:
    """Stable key for one physical room variant in the current run."""
    return (
        int(maze_index),
        int(cell_x) & 0xFF,
        int(cell_y) & 0xFF,
        int(map_index) & 0xFFFFFFFF,
    )


def _room_states(session: Any) -> dict[tuple[int, int, int, int], dict[str, Any]]:
    if session is None:
        return {}
    raw = getattr(session, "dungeon_room_states", None)
    if isinstance(raw, dict):
        return raw
    out: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    try:
        session.dungeon_room_states = out
    except Exception:
        return {}
    return out


def _wire_actor_count(monsters: Iterable[dict]) -> int:
    return sum(1 for mon in list(monsters or []) if bool(mon.get("wire_visible", True)))


def cache_room_state(
    session: Any,
    *,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    map_index: int,
    monsters: Iterable[dict],
    first_seq: int,
    seed: int,
    normal_map_index: int = 0,
) -> dict[str, Any] | None:
    """Store one initialized room and keep its mutable actor rows authoritative."""
    if session is None or int(map_index or 0) <= 0:
        return None
    key = room_state_key(maze_index, cell_x, cell_y, map_index)
    states = _room_states(session)
    rows = list(monsters or [])
    state = states.get(key)
    if state is None:
        state = {
            "key": key,
            "maze_index": int(maze_index),
            "cell_x": int(cell_x) & 0xFF,
            "cell_y": int(cell_y) & 0xFF,
            "map_index": int(map_index) & 0xFFFFFFFF,
            "normal_map_index": int(normal_map_index or map_index) & 0xFFFFFFFF,
            "monsters": rows,
            "first_seq": int(first_seq),
            "seed": int(seed) & 0xFFFFFFFF,
        }
        states[key] = state
    else:
        # The current room list contains live ``killed``/mechanism state.  Keep
        # the same row objects when capturing it before a move.
        state["monsters"] = rows
        state["first_seq"] = int(first_seq)
        state["seed"] = int(seed) & 0xFFFFFFFF
        state["normal_map_index"] = int(normal_map_index or map_index) & 0xFFFFFFFF
    cursor = int(first_seq) + _wire_actor_count(rows)
    try:
        session.dungeon_room_sequence_cursor = max(
            int(getattr(session, "dungeon_room_sequence_cursor", 0) or 0),
            cursor,
        )
        session.dungeon_room_states = states
    except Exception:
        pass
    return state


def get_room_state(
    session: Any,
    *,
    maze_index: int,
    cell_x: int,
    cell_y: int,
    map_index: int,
) -> dict[str, Any] | None:
    if session is None:
        return None
    return _room_states(session).get(
        room_state_key(maze_index, cell_x, cell_y, map_index)
    )


def cache_current_room_state(session: Any) -> dict[str, Any] | None:
    """Capture the currently bound room before switching to another cell."""
    if session is None:
        return None
    return cache_room_state(
        session,
        maze_index=int(getattr(session, "dungeon_maze_index", 0) or 0),
        cell_x=int(getattr(session, "dungeon_cell_x", 0) or 0),
        cell_y=int(getattr(session, "dungeon_cell_y", 0) or 0),
        map_index=int(getattr(session, "dungeon_map_index", 0) or 0),
        normal_map_index=int(
            getattr(session, "dungeon_normal_map_index", 0)
            or getattr(session, "dungeon_map_index", 0)
            or 0
        ),
        monsters=list(getattr(session, "dungeon_room_mons", None) or []),
        first_seq=int(getattr(session, "dungeon_room_first_seq", 0) or 0),
        seed=int(getattr(session, "dungeon_seed", 0) or 0),
    )


def next_room_first_seq(session: Any) -> int:
    """Return the global actor-sequence cursor without advancing on revisits."""
    if session is None:
        return 0
    cursor = int(getattr(session, "dungeon_room_sequence_cursor", 0) or 0)
    if cursor > 0:
        return cursor
    first_seq = int(getattr(session, "dungeon_room_first_seq", 0) or 0)
    return first_seq + _wire_actor_count(
        list(getattr(session, "dungeon_room_mons", None) or [])
    )


def allocate_runtime_actor_key(session: Any) -> int:
    """Allocate one session-unique u16 actor key for an in-room summon."""
    if session is None:
        return 0
    occupied: set[int] = {0, 0xFFFF}
    try:
        occupied.add(int(getattr(session, "dungeon_player_actor_key", 0)) & 0xFFFF)
    except (TypeError, ValueError):
        pass
    for state in _room_states(session).values():
        for actor in list(state.get("monsters") or []):
            try:
                occupied.add(int(actor.get("seq") or 0) & 0xFFFF)
            except (TypeError, ValueError):
                continue
    for actor in list(getattr(session, "dungeon_room_mons", None) or []):
        try:
            occupied.add(int(actor.get("seq") or 0) & 0xFFFF)
        except (TypeError, ValueError):
            continue

    cursor = int(getattr(session, "dungeon_room_sequence_cursor", 0) or 0)
    start = max(1, min(0xFFFE, cursor + 1))
    for candidate in range(start, 0xFFFF):
        if candidate not in occupied:
            session.dungeon_room_sequence_cursor = candidate
            return candidate
    for candidate in range(1, start):
        if candidate not in occupied:
            session.dungeon_room_sequence_cursor = max(cursor, candidate)
            return candidate
    return 0


def _cleared_set(session: Any) -> set[tuple[int, int]]:
    if session is None:
        return set()
    raw = getattr(session, "dungeon_cleared_cells", None)
    if raw is None:
        out: set[tuple[int, int]] = set()
        try:
            session.dungeon_cleared_cells = out
        except Exception:
            return set()
        return out
    if isinstance(raw, set):
        return raw
    # 兼容 list/tuple 快照
    out = set()
    for item in list(raw or []):
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.add(cell_key(item[0], item[1]))
            elif isinstance(item, str) and "," in item:
                a, b = item.split(",", 1)
                out.add(cell_key(int(a), int(b)))
        except Exception:
            continue
    try:
        session.dungeon_cleared_cells = out
    except Exception:
        pass
    return out


def _destroyed_set(session: Any) -> set[tuple[int, int, int, int]]:
    if session is None:
        return set()
    raw = getattr(session, "dungeon_destroyed_objects", None)
    if raw is None:
        out: set[tuple[int, int, int, int]] = set()
        try:
            session.dungeon_destroyed_objects = out
        except Exception:
            return set()
        return out
    if isinstance(raw, set):
        return raw
    out: set[tuple[int, int, int, int]] = set()
    for item in list(raw or []):
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 4:
                out.add(
                    destroyed_object_key(item[0], item[1], item[2], item[3])
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                # 兼容仅 (map, code)
                out.add(destroyed_object_key(item[0], item[1], 0, 0))
        except Exception:
            continue
    try:
        session.dungeon_destroyed_objects = out
    except Exception:
        pass
    return out


def init_run_room_state(session: Any) -> None:
    """进本时初始化（新 run 清空旧格子 / 已毁建筑 / 条件 / 同伴）。"""
    if session is None:
        return
    try:
        session.dungeon_boss_map_variants = {}
    except Exception:
        pass
    try:
        session.dungeon_cleared_cells = set()
    except Exception:
        pass
    try:
        session.dungeon_required_cells = set()
    except Exception:
        pass
    try:
        session.dungeon_destroyed_objects = set()
    except Exception:
        pass
    try:
        session.dungeon_room_states = {}
        session.dungeon_room_sequence_cursor = 0
        session.dungeon_room_passive_objects = []
    except Exception:
        pass
    try:
        session.dungeon_layered_map_cell = None
        session.dungeon_layered_map_index = -1
    except Exception:
        pass
    try:
        session.dungeon_clear_condition_state = None
    except Exception:
        pass
    try:
        session.dungeon_boss_condition_config_key = None
        session.dungeon_boss_condition_assignments = []
        session.dungeon_boss_condition_complete = False
        session.dungeon_boss_condition_gate_pending = False
    except Exception:
        pass
    try:
        session.dungeon_ally_companions = {}
    except Exception:
        pass
    for name, value in (
        ("dungeon_tournament_active", False),
        ("dungeon_tournament_completed", False),
        ("dungeon_tournament_dungeon_id", 0),
        ("dungeon_tournament_map_index", 0),
        ("dungeon_tournament_seed", 0),
        ("dungeon_tournament_round", 0),
        ("dungeon_tournament_current_seq", 0),
        ("dungeon_tournament_current_seqs", ()),
        ("dungeon_tournament_first_actor_seq", 0),
        ("dungeon_tournament_party_limit", 0),
        ("dungeon_tournament_rounds", ()),
        ("dungeon_tournament_path_actors", ()),
        ("dungeon_tournament_opponents", ()),
        ("dungeon_tournament_completed_rounds", 0),
        ("dungeon_tournament_reward_exp", 0),
        ("dungeon_tournament_rewards", ()),
        ("dungeon_tournament_reward_selection", None),
        ("dungeon_tournament_round_fatigue", 0),
        ("dungeon_tournament_enter_without_fatigue", False),
    ):
        try:
            setattr(session, name, value)
        except Exception:
            pass


def reset_run_room_state(session: Any) -> None:
    """离开地图 / 回城 / giveup：丢弃本局房间清理记录与同伴跟随。"""
    if session is None:
        return
    try:
        session.dungeon_boss_map_variants = {}
    except Exception:
        pass
    try:
        session.dungeon_cleared_cells = set()
    except Exception:
        pass
    try:
        session.dungeon_required_cells = set()
    except Exception:
        pass
    try:
        session.dungeon_destroyed_objects = set()
    except Exception:
        pass
    try:
        session.dungeon_room_states = {}
        session.dungeon_room_sequence_cursor = 0
    except Exception:
        pass
    try:
        session.dungeon_layered_map_cell = None
        session.dungeon_layered_map_index = -1
    except Exception:
        pass
    try:
        session.dungeon_clear_condition_state = None
    except Exception:
        pass
    try:
        session.dungeon_boss_condition_config_key = None
        session.dungeon_boss_condition_assignments = []
        session.dungeon_boss_condition_complete = False
        session.dungeon_boss_condition_gate_pending = False
    except Exception:
        pass
    try:
        if hasattr(session, "dungeon_room_mons"):
            session.dungeon_room_mons = []
        if hasattr(session, "dungeon_room_passive_objects"):
            session.dungeon_room_passive_objects = []
    except Exception:
        pass
    try:
        session.dungeon_ally_companions = {}
    except Exception:
        pass
    try:
        session.dungeon_tower_inventory = {}
        session.dungeon_tower_persistent_main_slots = set()
        session.dungeon_tower_kind = None
        session.dungeon_tower_floor = 0
        session.dungeon_tower_final_floor = 0
        session.dungeon_tower_maps = ()
        session.dungeon_tower_stage_state = 0
        session.dungeon_tower_stage_metrics = ()
        session.dungeon_tower_floor_cleared = False
        session.dungeon_tower_awaiting_move = False
        session.dungeon_tower_settled = False
        session.dungeon_tower_max_clear_items = 0
    except Exception:
        pass
    for name, value in (
        ("dungeon_hell_enabled", False),
        ("dungeon_hell_room", None),
        ("dungeon_hell_party_mode", 0),
        ("dungeon_hell_x", 0xFF),
        ("dungeon_hell_y", 0xFF),
        ("dungeon_hell_map_index", 0),
        ("dungeon_hell_normal_map_index", 0),
        ("dungeon_hell_room_active", False),
        ("dungeon_hell_hidden_count", 0),
        ("dungeon_hell_phase", ""),
    ):
        try:
            setattr(session, name, value)
        except Exception:
            pass
    for name, value in (
        ("dungeon_tournament_active", False),
        ("dungeon_tournament_completed", False),
        ("dungeon_tournament_dungeon_id", 0),
        ("dungeon_tournament_map_index", 0),
        ("dungeon_tournament_seed", 0),
        ("dungeon_tournament_round", 0),
        ("dungeon_tournament_current_seq", 0),
        ("dungeon_tournament_current_seqs", ()),
        ("dungeon_tournament_first_actor_seq", 0),
        ("dungeon_tournament_party_limit", 0),
        ("dungeon_tournament_rounds", ()),
        ("dungeon_tournament_path_actors", ()),
        ("dungeon_tournament_opponents", ()),
        ("dungeon_tournament_completed_rounds", 0),
        ("dungeon_tournament_reward_exp", 0),
        ("dungeon_tournament_rewards", ()),
        ("dungeon_tournament_reward_selection", None),
        ("dungeon_tournament_round_fatigue", 0),
        ("dungeon_tournament_enter_without_fatigue", False),
    ):
        try:
            setattr(session, name, value)
        except Exception:
            pass


def is_room_cleared(session: Any, cell_x: int, cell_y: int) -> bool:
    if session is None:
        return False
    return cell_key(cell_x, cell_y) in _cleared_set(session)


def mark_room_cleared(session: Any, cell_x: int, cell_y: int) -> bool:
    """标记格子已清完。返回是否新标记。"""
    if session is None:
        return False
    key = cell_key(cell_x, cell_y)
    s = _cleared_set(session)
    if key in s:
        return False
    s.add(key)
    try:
        session.dungeon_cleared_cells = s
    except Exception:
        pass
    return True


def mark_current_room_cleared_if_empty(session: Any) -> bool:
    """若当前房挡路 mon 全死（或无挡路 actor），标记当前 cell 已清。

    AI character / special passive 的 ``is_blocking=false``，不挡门。
    """
    if session is None:
        return False
    mons = list(getattr(session, "dungeon_room_mons", None) or [])
    blocking = [m for m in mons if bool(m.get("is_blocking", True))]
    if blocking and any(not m.get("killed") for m in blocking):
        return False
    # 无挡路 actor（空房 / 仅 APC）→ 可视为已清
    if not blocking:
        # 仅当曾经 load 过（有 first_seq 或 map_index）时标记
        if not getattr(session, "dungeon_map_index", None):
            return False
    cx = int(getattr(session, "dungeon_cell_x", 0) or 0)
    cy = int(getattr(session, "dungeon_cell_y", 0) or 0)
    return mark_room_cleared(session, cx, cy)


def cleared_cells_snapshot(session: Any) -> list[tuple[int, int]]:
    if session is None:
        return []
    return sorted(_cleared_set(session))


def mark_object_destroyed(
    session: Any,
    *,
    map_index: int,
    object_code: int,
    x: int = 0,
    y: int = 0,
) -> bool:
    """记录本局已摧毁的建筑。返回是否新标记。"""
    if session is None:
        return False
    code = int(object_code or 0)
    mid = int(map_index or 0)
    if code <= 0 or mid <= 0:
        return False
    key = destroyed_object_key(mid, code, x, y)
    s = _destroyed_set(session)
    if key in s:
        return False
    s.add(key)
    try:
        session.dungeon_destroyed_objects = s
    except Exception:
        pass
    return True


def is_object_destroyed(
    session: Any,
    *,
    map_index: int,
    object_code: int,
    x: int = 0,
    y: int = 0,
) -> bool:
    if session is None:
        return False
    key = destroyed_object_key(map_index, object_code, x, y)
    return key in _destroyed_set(session)


def filter_destroyed_objects(
    session: Any,
    mons: Iterable[dict],
    *,
    map_index: int,
) -> list[dict]:
    """从 mon 表剔除本局已摧毁的 type9 / passive 建筑。

    非建筑 actor 原样保留。无坐标时仅按 (map, code) 匹配（x=y=0 键）。
    """
    rows = list(mons or [])
    if session is None or not rows:
        return rows
    destroyed = _destroyed_set(session)
    if not destroyed:
        return rows
    mid = int(map_index or 0)
    out: list[dict] = []
    for mon in rows:
        kind = str(mon.get("kind") or "").lower()
        mon_type = 0
        try:
            mon_type = int(mon.get("type") or mon.get("mon_type") or 0)
        except Exception:
            mon_type = 0
        is_obj = kind in {
            "special_passive",
            "passive_object",
            "passive",
            "map_passive",
        } or mon_type == 9
        if not is_obj:
            out.append(mon)
            continue
        code = int(mon.get("code") or mon.get("monster_code") or 0)
        try:
            ox = int(mon.get("x") or 0)
        except Exception:
            ox = 0
        try:
            oy = int(mon.get("y") or 0)
        except Exception:
            oy = 0
        key = destroyed_object_key(mid, code, ox, oy)
        # 也接受仅 (map, code, 0, 0) 的宽松键（无坐标记录时）
        key_loose = destroyed_object_key(mid, code, 0, 0)
        if key in destroyed or key_loose in destroyed:
            continue
        out.append(mon)
    # 过滤后重排 seq 连续性（保持 first_seq 语义由调用方 first_seq 决定）
    return out


def bind_room_passive_objects(
    session: Any,
    passive_objects: Iterable[dict],
    *,
    map_index: int,
) -> list[dict]:
    """Bind client-owned passive PVF rows without adding them to 001D actors."""
    rows = [dict(row) for row in list(passive_objects or [])]
    source_map = int(map_index or 0) & 0xFFFFFFFF
    for row in rows:
        row["runtime_map_index"] = source_map
        row.setdefault("killed", False)
    rows = filter_destroyed_objects(session, rows, map_index=source_map)
    if session is not None:
        try:
            session.dungeon_room_passive_objects = rows
        except Exception:
            pass
    return rows


def resequence_monsters(
    mons: Iterable[dict],
    *,
    first_seq: int = 0,
) -> list[dict]:
    """过滤后重写 seq = first_seq+1..n，避免空洞。"""
    out: list[dict] = []
    for i, mon in enumerate(list(mons or [])):
        row = dict(mon)
        row["seq"] = (int(first_seq) + i + 1) & 0xFFFF
        out.append(row)
    return out


def destroyed_objects_snapshot(
    session: Any,
) -> list[tuple[int, int, int, int]]:
    if session is None:
        return []
    return sorted(_destroyed_set(session))
