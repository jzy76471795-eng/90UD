# -*- coding: utf-8 -*-
"""掉落会话 API：预计算、scene slot 预留与死亡时登记。"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from game.chain_loader import load_chain
from game.content_blocking import is_session_item_drop_blocked
from game.drop.dnf_lcg import DnfLcg, roll_weighted_occurrences
from game.drop.drop_generator import (
    generate_hell_party_drops,
    generate_monster_drops,
    generate_passive_object_drops,
)
from game.drop.equip_stackable_pools import is_registered_drop_item
from game.drop.models import DropInfo, MonsterDropRequest, MonsterDropResult

_tower_drop_policy = load_chain("helper-tower_drop_policy.py")


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _ensure_session_drop_state(session: Any) -> None:
    if session is None:
        return
    if getattr(session, "dungeon_drop_scene_slot", None) is None:
        session.dungeon_drop_scene_slot = 0
    if getattr(session, "dungeon_drop_lcg_seed", None) is None:
        # 进图未设 seed 时用时间混合，房间内连续击杀共享流
        session.dungeon_drop_lcg_seed = (int(time.time() * 1000) ^ 0xA5A5) & 0xFFFFFFFF
    if getattr(session, "dungeon_drops", None) is None:
        session.dungeon_drops = {}


def _filter_blocked_session_drops(
    session: Any,
    drops: Iterable[DropInfo],
) -> tuple[list[DropInfo], list[int]]:
    """Keep gold and allowed items while omitting the current rule-set's items."""
    allowed: list[DropInfo] = []
    blocked: list[int] = []
    for drop in drops or ():
        item_id = int(getattr(drop, "template_id", 0) or 0)
        if item_id > 0 and is_session_item_drop_blocked(session, item_id):
            blocked.append(item_id)
            continue
        allowed.append(drop)
    return allowed, blocked


def _filter_unregistered_session_drops(
    drops: Iterable[DropInfo],
    *,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> tuple[list[DropInfo], list[int]]:
    """Keep gold and lst-registered items only at the session boundary."""
    candidate_is_registered = allow_item or is_registered_drop_item
    allowed: list[DropInfo] = []
    rejected: list[int] = []
    for drop in drops or ():
        item_id = int(getattr(drop, "template_id", 0) or 0)
        if item_id > 0 and not candidate_is_registered(item_id):
            rejected.append(item_id)
            continue
        allowed.append(drop)
    return allowed, rejected


def raid_ground_drops_disabled(session: Any) -> bool:
    """Only active server-owned Anton/Luke runs suppress ground loot."""
    raid_id = getattr(session, "raid_id", None)
    channel = getattr(session, "selected_channel_id", None)
    return (bool(getattr(session, "in_dungeon_run", False))
            and type(raid_id) is int and raid_id > 0
            and type(channel) is int and channel in (23, 32))


def _apply_blocked_session_drop_filter(session: Any, result: MonsterDropResult) -> MonsterDropResult:
    if raid_ground_drops_disabled(session):
        result.drops = []
        result.gold_amount = 0
        result.note = "raid_ground_drops=disabled"
        return result
    drops, rejected = _filter_unregistered_session_drops(result.drops)
    drops, blocked = _filter_blocked_session_drops(session, drops)
    if not rejected and not blocked:
        return result
    result.drops = drops
    suffixes: list[str] = []
    if rejected:
        suffixes.append(
            "unregistered_drop_items=%s"
            % ",".join(str(item_id) for item_id in sorted(set(rejected)))
        )
    if blocked:
        suffixes.append(
            "content_blocked_items=%s"
            % ",".join(str(item_id) for item_id in sorted(set(blocked)))
        )
    suffix = ";".join(suffixes)
    result.note = "%s;%s" % (str(result.note or ""), suffix) if result.note else suffix
    return result


def get_session_lcg(session: Any) -> DnfLcg:
    _ensure_session_drop_state(session)
    seed = int(getattr(session, "dungeon_drop_lcg_seed", 1) or 1) & 0xFFFFFFFF
    return DnfLcg(seed)


def persist_session_lcg(session: Any, lcg: DnfLcg) -> None:
    if session is None:
        return
    session.dungeon_drop_lcg_seed = int(lcg.seed) & 0xFFFFFFFF


def register_drops_on_session(
    session: Any,
    drops: List[DropInfo],
    *,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> None:
    if session is None or not drops or raid_ground_drops_disabled(session):
        return
    drops, _rejected = _filter_unregistered_session_drops(
        drops,
        allow_item=allow_item,
    )
    drops, _blocked = _filter_blocked_session_drops(session, drops)
    if not drops:
        return
    _ensure_session_drop_state(session)
    bag: Dict[int, DropInfo] = getattr(session, "dungeon_drops", None) or {}
    for d in drops:
        # Stage3 GET_ITEM 用 u32 objectKey 回声；单机默认 = scene_slot
        bag[int(d.wire_object_key) & 0xFFFFFFFF] = d
    session.dungeon_drops = bag


def create_and_register_session_item_drop(
    session: Any,
    *,
    template_id: int,
    stack_count: int = 1,
    source: str = "",
    quest_id: int = 0,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> Optional[DropInfo]:
    """Reserve one new scene slot and expose a forced item ground drop."""
    item_id = int(template_id or 0)
    if session is None or item_id <= 0 or raid_ground_drops_disabled(session):
        return None
    if not (allow_item or is_registered_drop_item)(item_id):
        return None
    if is_session_item_drop_blocked(session, item_id):
        return None
    _ensure_session_drop_state(session)
    slot = int(getattr(session, "dungeon_drop_scene_slot", 0) or 0) & 0xFFFF
    slot = (slot + 1) & 0xFFFF
    if slot == 0:
        slot = 1
    drop = DropInfo(
        scene_slot=int(slot),
        template_id=item_id & 0xFFFFFFFF,
        stack_count=max(1, int(stack_count or 1)) & 0xFFFFFFFF,
        endurance=0,
        upgrade_level=0,
        source=str(source or ""),
        object_key=int(slot),
        quest_id=max(0, int(quest_id or 0)) & 0xFFFF,
    )
    session.dungeon_drop_scene_slot = int(slot) & 0xFFFF
    register_drops_on_session(session, [drop], allow_item=allow_item)
    return drop


def generate_and_register_monster_drops(
    session: Any,
    *,
    monster_level: int,
    monster_type: int,
    monster_code: int,
    difficulty: int,
    dungeon_basis_level: int,
    dungeon_id: int = -1,
    owner_actor_id: int = 0,
    is_hell_party_actor: bool = False,
    hell_party_mode: int = 0,
    hell_reward_roll_count: int = 0,
    special_hell_final_boss: bool = False,
    wish_item_ids: tuple[int, ...] = (),
    independent_drop_rate_bonus_percent: int = 0,
) -> MonsterDropResult:
    """Compatibility API: roll a result and register it immediately."""
    result = generate_monster_drop_result(
        session,
        monster_level=monster_level,
        monster_type=monster_type,
        monster_code=monster_code,
        difficulty=difficulty,
        dungeon_basis_level=dungeon_basis_level,
        dungeon_id=dungeon_id,
        owner_actor_id=owner_actor_id,
        is_hell_party_actor=is_hell_party_actor,
        hell_party_mode=hell_party_mode,
        hell_reward_roll_count=hell_reward_roll_count,
        special_hell_final_boss=special_hell_final_boss,
        wish_item_ids=wish_item_ids,
        independent_drop_rate_bonus_percent=independent_drop_rate_bonus_percent,
    )
    register_drops_on_session(session, result.drops)
    return result


def generate_monster_drop_result(
    session: Any,
    *,
    monster_level: int,
    monster_type: int,
    monster_code: int,
    difficulty: int,
    dungeon_basis_level: int,
    dungeon_id: int = -1,
    owner_actor_id: int = 0,
    is_hell_party_actor: bool = False,
    hell_party_mode: int = 0,
    hell_reward_roll_count: int = 0,
    special_hell_final_boss: bool = False,
    wish_item_ids: tuple[int, ...] = (),
    independent_drop_rate_bonus_percent: int = 0,
    include_hell_independent_drop: bool = True,
    include_hell_group_reward: bool = True,
) -> MonsterDropResult:
    """Roll and reserve scene slots without exposing the drops for pickup."""
    if raid_ground_drops_disabled(session):
        return MonsterDropResult(note="raid_ground_drops=disabled")
    if not _env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
        return MonsterDropResult(note="ITEM_DROP=OFF")

    _ensure_session_drop_state(session)
    lcg = get_session_lcg(session)
    slot = int(getattr(session, "dungeon_drop_scene_slot", 0) or 0) & 0xFFFF
    drop_scope = _tower_drop_policy.resolve_tower_drop_scope(int(dungeon_id))

    if drop_scope == _tower_drop_policy.DROP_SCOPE_DEATH_TOWER:
        result, new_slot = _tower_drop_policy.generate_death_tower_item_drops(
            ai_character_code=int(monster_code),
            lcg=lcg,
            slot_counter=slot,
        )
    elif drop_scope == _tower_drop_policy.DROP_SCOPE_QUEST_ONLY:
        result = _tower_drop_policy.empty_tower_ordinary_drop_result(lcg)
        new_slot = slot
    elif bool(is_hell_party_actor) or bool(special_hell_final_boss):
        result, new_slot = generate_hell_party_drops(
            monster_code=int(monster_code),
            dungeon_basis_level=int(dungeon_basis_level),
            dungeon_difficulty=int(difficulty),
            hell_party_mode=int(hell_party_mode),
            reward_roll_count=int(hell_reward_roll_count),
            lcg=lcg,
            slot_counter=slot,
            monster_level=int(monster_level),
            preferred_epic_item_ids=tuple(int(item_id) for item_id in wish_item_ids),
            preferred_epic_weight=1,
            special_wish_mode=bool(special_hell_final_boss),
            independent_drop_rate_bonus_percent=max(
                0, int(independent_drop_rate_bonus_percent)
            ),
            include_independent_drop=bool(include_hell_independent_drop),
            include_group_reward=bool(include_hell_group_reward),
        )
    else:
        req = MonsterDropRequest(
            monster_level=int(monster_level),
            monster_type=int(monster_type),
            monster_code=int(monster_code),
            difficulty=int(difficulty),
            dungeon_basis_level=int(dungeon_basis_level),
            dungeon_id=int(dungeon_id),
            owner_actor_id=int(owner_actor_id) & 0xFFFF,
            independent_drop_rate_bonus_percent=max(
                0, int(independent_drop_rate_bonus_percent)
            ),
        )
        result, new_slot = generate_monster_drops(req, lcg, slot)
    persist_session_lcg(session, lcg)
    if session is not None:
        session.dungeon_drop_scene_slot = int(new_slot) & 0xFFFF
    return _apply_blocked_session_drop_filter(session, result)


def generate_and_register_passive_object_drops(
    session: Any,
    *,
    object_level: int,
    difficulty: int,
    object_code: int,
    object_index: int,
    dungeon_id: int = -1,
    special_items: Optional[List[Any]] = None,
) -> MonsterDropResult:
    """Compatibility API: roll one passive-object result and register it."""
    result = generate_passive_object_drop_result(
        session,
        object_level=object_level,
        difficulty=difficulty,
        object_code=object_code,
        object_index=object_index,
        dungeon_id=dungeon_id,
        special_items=special_items,
    )
    register_drops_on_session(session, result.drops)
    return result


def generate_passive_object_drop_result(
    session: Any,
    *,
    object_level: int,
    difficulty: int,
    object_code: int,
    object_index: int,
    dungeon_id: int = -1,
    special_items: Optional[List[Any]] = None,
) -> MonsterDropResult:
    """Roll and reserve one passive-object result without registering it."""
    if raid_ground_drops_disabled(session):
        return MonsterDropResult(note="raid_ground_drops=disabled")
    if not _env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
        return MonsterDropResult(note="ITEM_DROP=OFF")

    _ensure_session_drop_state(session)
    lcg = get_session_lcg(session)
    slot = int(getattr(session, "dungeon_drop_scene_slot", 0) or 0) & 0xFFFF
    result, new_slot = generate_passive_object_drops(
        object_level=max(1, int(object_level or 1)),
        difficulty=int(difficulty or 0),
        object_code=int(object_code or 0),
        object_index=max(0, int(object_index or 0)),
        dungeon_id=int(dungeon_id),
        special_items=list(special_items or []),
        lcg=lcg,
        slot_counter=slot,
    )
    persist_session_lcg(session, lcg)
    if session is not None:
        session.dungeon_drop_scene_slot = int(new_slot) & 0xFFFF
    return _apply_blocked_session_drop_filter(session, result)


def register_preloaded_drop_result(
    session: Any,
    result: MonsterDropResult,
) -> None:
    """Expose one precomputed result only when its actor actually dies."""
    # A rule can change after room-entry preloading.  Mutate the returned
    # bundle before the DIE path serializes it so blocked entries are neither
    # shown to the client nor left as unpickable visual ground objects.
    _apply_blocked_session_drop_filter(session, result)
    register_drops_on_session(session, list(getattr(result, "drops", None) or []))


def bind_passive_object_item_entries(
    items: Iterable[Any], passive_objects: Iterable[Any],
) -> List[Dict[str, Any]]:
    """Resolve DGN groups to MAP special-passive instance ids, without rolling.

    Only explicit MAP [item] references may select DGN groups. Ordinary MAP
    objects, action-created objects and unreferenced groups never inherit them.
    The resulting object_index belongs to the 001D instance namespace.
    """
    def field(obj: Any, name: str, default: Any = None) -> Any:
        return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)

    groups: Dict[int, List[Dict[str, Any]]] = {}
    for entry in items or ():
        try:
            group = int(field(entry, "index", -1))
            item_id = int(field(entry, "item_id", 0))
            rate = int(field(entry, "drop_rate", 0))
        except (TypeError, ValueError):
            continue
        if group < 0 or item_id <= 0 or rate <= 0:
            continue
        groups.setdefault(group, []).append({
            "drop_group_index": group, "item_id": item_id, "drop_rate": rate,
            "source_group_index": field(entry, "source_group_index", -1),
        })

    bound: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    for actor in passive_objects or ():
        if str(field(actor, "kind", "")).strip().lower() != "special_passive":
            continue
        if str(field(actor, "passive_source_kind", "")).strip().lower() == "action_created":
            continue
        try:
            index = int(field(actor, "packet_index", -1))
        except (TypeError, ValueError):
            continue
        # 001D carries one byte. Never wrap an invalid id onto another building.
        if not 0 <= index <= 255 or index in seen:
            continue
        seen.add(index)
        for ref in field(actor, "item_drop_refs", ()) or ():
            try:
                group = int(ref[0])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            for entry in groups.get(group, ()):
                bound.append(dict(entry, object_index=index))
    return bound


def generate_passive_object_item_entries(
    session: Any,
    items: List[Any],
    *,
    passive_objects: Iterable[Any] = (),
    allow_item: Optional[Callable[[int], bool]] = None,
) -> List[Dict[str, Any]]:
    """从 .dgn ``[special passive object item]`` 表 roll **物品掉落条目**。

    每个 PVF weight 均按 ``weight // 10000`` 的保底次数，加上余数的一次
    ``Next(10000) < remainder`` 判定生成。这样大于 10000 的权重不会被截断
    成单次掉落。
    返回 dict 列表（object_index/global_seq/item_id/...），
    并登记到 session.dungeon_drops 供 GET_ITEM 拾取。

    DGN first fields are drop-group ids. Resolve each MAP special-passive
    [item] reference before rolling, then serialize the target instance id.
    Every referencing instance receives an independent roll of that group.
    Missing references fail closed; no ordinary-object index fallback exists.

    ``USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM`` 默认开启；显式设为 ``0`` 可用于
    窄范围诊断。Stage3 读取每条为 21B，``global_seq`` 是 u32。
    """
    if raid_ground_drops_disabled(session):
        return []
    if not _env_bool("USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM", True):
        return []
    candidate_is_registered = allow_item or is_registered_drop_item
    rows = bind_passive_object_item_entries(items, passive_objects)

    def _field(obj: Any, *names: str, default: Any = 0) -> Any:
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            if hasattr(obj, name):
                return getattr(obj, name)
        return default

    if not rows:
        return []

    import random as _random

    _ensure_session_drop_state(session)
    # 优先用会话 LCG；无 session 时用 random
    use_lcg = session is not None
    lcg = get_session_lcg(session) if use_lcg else None
    slot = int(getattr(session, "dungeon_drop_scene_slot", 0) or 0) & 0xFFFF if session else 0
    out: List[Dict[str, Any]] = []
    drops: List[DropInfo] = []
    for entry in rows:
        # The room packet carries at most 255 rows. Do not register invisible
        # excess ground objects after the serializer's count limit.
        if len(out) >= 255:
            break
        try:
            item_id = int(_field(entry, "item_id", default=0) or 0)
            drop_rate = int(_field(entry, "drop_rate", default=0) or 0)
            obj_index = int(_field(entry, "object_index", default=-1))
        except Exception:
            continue
        if item_id <= 0 or drop_rate <= 0:
            continue
        # This row is a direct PVF-configured candidate.  Validate its lst
        # identity before its rate can advance the session LCG.
        if not candidate_is_registered(item_id):
            continue
        # DGN special-passive rows are serialized directly into the room scene.
        # Apply account content blocks before random state or scene slots advance.
        if is_session_item_drop_blocked(session, item_id):
            continue
        if lcg is not None:
            occurrences = roll_weighted_occurrences(
                lcg,
                drop_rate,
                denominator=10000,
            )
        else:
            guaranteed, remainder = divmod(int(drop_rate), 10000)
            occurrences = int(guaranteed)
            if remainder > 0 and int(_random.randint(0, 9999)) < remainder:
                occurrences += 1
        for _ in range(min(occurrences, 255 - len(out))):
            slot = (int(slot) + 1) & 0xFFFF
            if slot == 0:
                slot = 1
            drop = DropInfo(
                scene_slot=int(slot),
                template_id=int(item_id) & 0xFFFFFFFF,
                stack_count=1,
                endurance=0,
                upgrade_level=0,
                source="passive_object_item",
                object_key=int(slot),
            )
            drops.append(drop)
            out.append(
                {
                    "object_index": int(obj_index),
                    "global_seq": int(slot) & 0xFFFF,
                    "item_id": int(item_id) & 0xFFFFFFFF,
                    "stack_count": 1,
                    "endurance": 0,
                }
            )
    if use_lcg and lcg is not None:
        persist_session_lcg(session, lcg)
        session.dungeon_drop_scene_slot = int(slot) & 0xFFFF
        register_drops_on_session(
            session,
            drops,
            allow_item=candidate_is_registered,
        )
    return out


def clear_session_drops(session: Any) -> None:
    if session is None:
        return
    session.dungeon_drops = {}
    session.dungeon_drop_scene_slot = 0
    if hasattr(session, "dungeon_drop_meta"):
        session.dungeon_drop_meta = {}
    if hasattr(session, "dungeon_last_die_seq"):
        session.dungeon_last_die_seq = 0


def note_die_entity_seq(session: Any, seq: int) -> None:
    """记录本局最近一次 C1/0027 对应的怪物 seq，供怪物死亡链路诊断。"""
    if session is None:
        return
    s = int(seq) & 0xFFFF
    if s <= 0:
        return
    session.dungeon_last_die_seq = s


def peek_session_drop(session: Any, object_key: int) -> Optional[DropInfo]:
    """按 DIE 下发的 u32 objectKey 查找（兼容低 16 位 scene_slot）。"""
    if session is None:
        return None
    bag: Dict[int, DropInfo] = getattr(session, "dungeon_drops", None) or {}
    key = int(object_key) & 0xFFFFFFFF
    drop = bag.get(key)
    if drop is not None:
        return drop
    # 兼容：仅低 16 位
    lo = key & 0xFFFF
    if lo and lo != key:
        drop = bag.get(lo)
        if drop is not None:
            return drop
    return None


def take_session_drop(session: Any, object_key: int) -> Optional[DropInfo]:
    """取出并移除场景掉落（拾取成功后调用）。"""
    if session is None:
        return None
    bag: Dict[int, DropInfo] = getattr(session, "dungeon_drops", None) or {}
    key = int(object_key) & 0xFFFFFFFF
    drop = bag.pop(key, None)
    if drop is None:
        lo = key & 0xFFFF
        if lo and lo != key:
            drop = bag.pop(lo, None)
    session.dungeon_drops = bag
    # 清理旁路 meta
    meta = getattr(session, "dungeon_drop_meta", None)
    if isinstance(meta, dict) and drop is not None:
        meta.pop(int(drop.wire_object_key) & 0xFFFFFFFF, None)
        session.dungeon_drop_meta = meta
    return drop
