# -*- coding: utf-8 -*-
"""Precompute room drops before publishing actors to the client."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from game.chain_loader import load_chain
from game.drop.drop_service import (
    bind_passive_object_item_entries,
    generate_monster_drop_result,
    generate_passive_object_drop_result,
    register_preloaded_drop_result,
)
from game.drop.models import (
    DropInfo,
    MonsterDropResult,
    PreloadedDropBundle,
)
from game.drop.wish_service import valid_wish_item_ids
from game.quest.quest_index import get_quest_index, passive_object_items_for_maze
from game.shop.premium_contract_index import get_active_premium_effects


_map_actors = load_chain("helper-dungeon_map_monsters.py")
_hell_party = load_chain("helper-dungeon_hell_party.py")

_ACTOR_BUNDLE_KEY = "_usdof_preloaded_actor_drop"
_SPECIAL_ACTOR_BUNDLE_KEY = "_usdof_preloaded_special_hell_actor_drop"
_GROUP_BUNDLE_KEY = "_usdof_preloaded_group_drop"
_SPECIAL_GROUP_BUNDLE_KEY = "_usdof_preloaded_special_hell_group_drop"
_PRELOAD_STATUS_KEY = "_usdof_drop_preload_status"


@dataclass
class RoomDropPreloadStats:
    visible: int = 0
    generated: int = 0
    cached: int = 0
    skipped: int = 0
    groups: int = 0
    reserved_rows: int = 0
    errors: int = 0
    source: str = "room"

    def note(self) -> str:
        return (
            "drop_preload source=%s visible=%d generated=%d cached=%d "
            "skipped=%d groups=%d reserved=%d errors=%d"
            % (
                self.source,
                self.visible,
                self.generated,
                self.cached,
                self.skipped,
                self.groups,
                self.reserved_rows,
                self.errors,
            )
        )


def _int_field(row: Any, *names: str, default: int = 0) -> int:
    for name in names:
        try:
            value = row.get(name) if isinstance(row, dict) else getattr(row, name)
        except (AttributeError, TypeError):
            continue
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return int(default)


def _active_dungeon_id(session: Any) -> int:
    for name in ("cur_dungeon_id", "selected_dungeon_id"):
        value = _int_field(session, name, default=0)
        if value > 0:
            return value
    return -1


def _active_difficulty(session: Any) -> int:
    for name in ("cur_dungeon_difficulty", "selected_difficulty"):
        try:
            value = getattr(session, name, None)
        except Exception:
            value = None
        if value is not None:
            try:
                return int(value) & 0xFF
            except (TypeError, ValueError):
                continue
    return 0


def _owner_actor_id(session: Any) -> int:
    for name in ("user_id", "actor_id", "selected_character_id"):
        value = _int_field(session, name, default=0)
        if value > 0:
            return value & 0xFFFF
    return 0


def _passive_object_index(actor: dict[str, Any]) -> int:
    for name in ("packet_index", "slot_index", "object_index"):
        if actor.get(name) is None:
            continue
        try:
            return max(0, int(actor[name]))
        except (TypeError, ValueError):
            continue
    return 0



def _preserves_client_last_action(actor: dict[str, Any]) -> bool:
    if not _map_actors.is_passive_actor(actor):
        return False
    source = str(actor.get("passive_source_kind") or "").strip().lower()
    return source == "action_created" and not bool(actor.get("wire_visible", True))


def _passive_identity(session: Any, actor: dict[str, Any]) -> tuple[int, ...]:
    return (
        _int_field(
            actor,
            "runtime_map_index",
            default=_int_field(session, "dungeon_map_index", default=0),
        )
        & 0xFFFFFFFF,
        _int_field(actor, "code", "monster_code", default=0) & 0xFFFFFFFF,
        _int_field(actor, "x", default=0) & 0xFFFFFFFF,
        _int_field(actor, "y", default=0) & 0xFFFFFFFF,
        _passive_object_index(actor),
    )


def _visible_actor_rows(
    session: Any,
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    primaries: list[dict[str, Any]] = []
    aliases: dict[int, list[dict[str, Any]]] = {}
    passive_by_identity: dict[tuple[int, ...], dict[str, Any]] = {}

    for actor in list(getattr(session, "dungeon_room_mons", None) or []):
        if not isinstance(actor, dict):
            continue
        primaries.append(actor)
        if _map_actors.is_passive_actor(actor):
            passive_by_identity[_passive_identity(session, actor)] = actor

    for actor in list(getattr(session, "dungeon_room_passive_objects", None) or []):
        if not isinstance(actor, dict):
            continue
        identity = _passive_identity(session, actor)
        primary = passive_by_identity.get(identity)
        if primary is None:
            primaries.append(actor)
            passive_by_identity[identity] = actor
            continue
        aliases.setdefault(id(primary), []).append(actor)
    return primaries, aliases


def _mirror_preload_keys(
    primary: dict[str, Any],
    aliases: Iterable[dict[str, Any]],
) -> None:
    for alias in aliases:
        for key in (
            _ACTOR_BUNDLE_KEY,
            _SPECIAL_ACTOR_BUNDLE_KEY,
            _GROUP_BUNDLE_KEY,
            _SPECIAL_GROUP_BUNDLE_KEY,
            _PRELOAD_STATUS_KEY,
        ):
            if key in primary:
                alias[key] = primary[key]


def set_preloaded_actor_drop(
    actor: dict[str, Any],
    result: MonsterDropResult,
    *,
    scope: str = "actor",
) -> PreloadedDropBundle:
    """Attach a prepared result; exposed for focused protocol tests."""
    bundle = PreloadedDropBundle(result=result, scope=str(scope or "actor"))
    actor[_ACTOR_BUNDLE_KEY] = bundle
    actor[_PRELOAD_STATUS_KEY] = "ready"
    return bundle


def _set_error_bundle(
    actor: dict[str, Any],
    exc: Exception,
    *,
    scope: str,
    key: str = _ACTOR_BUNDLE_KEY,
) -> PreloadedDropBundle:
    result = MonsterDropResult(
        note="preload_error scope=%s type=%s" % (scope, type(exc).__name__)
    )
    bundle = PreloadedDropBundle(result=result, scope=scope)
    actor[key] = bundle
    actor[_PRELOAD_STATUS_KEY] = "error"
    return bundle


def _special_hell_boss_room(session: Any) -> bool:
    if not bool(getattr(session, "dungeon_special_hell", False)):
        return False
    return (
        _int_field(session, "dungeon_cell_x", default=-1)
        == _int_field(session, "dungeon_boss_x", default=-2)
        and _int_field(session, "dungeon_cell_y", default=-1)
        == _int_field(session, "dungeon_boss_y", default=-2)
    )


def _preferred_wish_items(session: Any) -> tuple[int, ...]:
    try:
        return valid_wish_item_ids(
            tuple(getattr(session, "dungeon_wish_item_ids", ()) or ())
        )
    except Exception:
        return ()


def _premium_independent_bonus(
    server: Any,
    session: Any,
    difficulty: int,
) -> int:
    try:
        effects = get_active_premium_effects(
            server._repo(),
            int(getattr(server, "account_id", 1) or 1),
            session=session,
        )
        return max(0, int(effects.independent_drop_bonus_for_difficulty(difficulty)))
    except Exception:
        return 0


def _generate_actor_result(
    session: Any,
    actor: dict[str, Any],
    *,
    dungeon_id: int,
    difficulty: int,
    basis_level: int,
    owner_actor_id: int,
    wish_item_ids: tuple[int, ...],
    independent_bonus: int,
) -> MonsterDropResult:
    actor_level = max(1, _int_field(actor, "level", default=basis_level or 1))
    actor_type = _int_field(actor, "type", "mon_type", default=0)
    actor_code = _int_field(actor, "code", "monster_code", default=0)
    is_hell_actor = bool(actor.get("is_hell_party_actor"))
    return generate_monster_drop_result(
        session,
        monster_level=actor_level,
        monster_type=actor_type,
        monster_code=actor_code,
        difficulty=difficulty,
        dungeon_basis_level=max(1, int(basis_level or actor_level)),
        dungeon_id=dungeon_id,
        owner_actor_id=owner_actor_id,
        is_hell_party_actor=is_hell_actor,
        hell_party_mode=_int_field(actor, "hell_party_mode", default=0),
        hell_reward_roll_count=0,
        wish_item_ids=wish_item_ids,
        independent_drop_rate_bonus_percent=independent_bonus,
        include_hell_group_reward=not is_hell_actor,
    )


def _generate_hell_result(
    session: Any,
    actor: dict[str, Any],
    *,
    dungeon_id: int,
    difficulty: int,
    basis_level: int,
    owner_actor_id: int,
    wish_item_ids: tuple[int, ...],
    independent_bonus: int,
    reward_roll_count: int,
    special_hell_final_boss: bool,
    include_independent: bool,
    include_group_reward: bool,
) -> MonsterDropResult:
    return generate_monster_drop_result(
        session,
        monster_level=max(1, _int_field(actor, "level", default=basis_level or 1)),
        monster_type=_int_field(actor, "type", "mon_type", default=0),
        monster_code=_int_field(actor, "code", "monster_code", default=0),
        difficulty=difficulty,
        dungeon_basis_level=max(1, int(basis_level or 1)),
        dungeon_id=dungeon_id,
        owner_actor_id=owner_actor_id,
        is_hell_party_actor=bool(actor.get("is_hell_party_actor")),
        hell_party_mode=(
            _int_field(session, "dungeon_special_hell_party_mode", default=1)
            if special_hell_final_boss
            else _int_field(actor, "hell_party_mode", default=0)
        ),
        hell_reward_roll_count=max(0, int(reward_roll_count)),
        special_hell_final_boss=bool(special_hell_final_boss),
        wish_item_ids=wish_item_ids,
        independent_drop_rate_bonus_percent=independent_bonus,
        include_hell_independent_drop=bool(include_independent),
        include_hell_group_reward=bool(include_group_reward),
    )


def _attach_group_bundle(
    actors: Sequence[dict[str, Any]],
    bundle: PreloadedDropBundle,
    *,
    key: str = _GROUP_BUNDLE_KEY,
) -> None:
    for actor in actors:
        actor[key] = bundle


def _existing_group_bundle(
    actors: Sequence[dict[str, Any]],
    *,
    key: str = _GROUP_BUNDLE_KEY,
) -> Optional[PreloadedDropBundle]:
    for actor in actors:
        bundle = actor.get(key)
        if isinstance(bundle, PreloadedDropBundle):
            return bundle
    return None


def preload_visible_room_drops(
    server: Any,
    session: Any,
    *,
    source: str = "room_publish",
) -> RoomDropPreloadStats:
    """Roll every visible actor's drop outcome before its actor packet is sent."""
    stats = RoomDropPreloadStats(source=str(source or "room"))
    if session is None or not bool(getattr(session, "in_dungeon_run", False)):
        return stats

    actors, aliases = _visible_actor_rows(session)
    stats.visible = len(actors) + sum(len(rows) for rows in aliases.values())
    if not actors:
        return stats

    dungeon_id = _active_dungeon_id(session)
    difficulty = _active_difficulty(session)
    basis_level = max(1, _int_field(session, "dungeon_basis_level", default=1))
    owner_actor_id = _owner_actor_id(session)
    wish_item_ids = _preferred_wish_items(session)

    eligible_non_passive = any(
        not bool(actor.get("killed"))
        and _map_actors.actor_counts_for_combat(actor)
        and not _map_actors.is_passive_actor(actor)
        for actor in actors
    )
    independent_bonus = (
        _premium_independent_bonus(server, session, difficulty)
        if eligible_non_passive
        else 0
    )

    special_items: list[Any] = []
    if any(_map_actors.is_passive_actor(actor) for actor in actors):
        try:
            dungeon = get_quest_index().get_dungeon(dungeon_id)
            special_items = passive_object_items_for_maze(
                dungeon, _int_field(session, "dungeon_maze_index", default=0)
            )
        except Exception:
            special_items = []

    for actor in actors:
        if _ACTOR_BUNDLE_KEY in actor or _PRELOAD_STATUS_KEY in actor:
            stats.cached += 1
            continue
        if bool(actor.get("killed")):
            actor[_PRELOAD_STATUS_KEY] = "skip_killed"
            stats.skipped += 1
            continue
        if not _map_actors.actor_counts_for_combat(actor):
            actor[_PRELOAD_STATUS_KEY] = "skip_noncombat"
            stats.skipped += 1
            continue
        if _preserves_client_last_action(actor):
            actor[_PRELOAD_STATUS_KEY] = "skip_client_last_action"
            stats.skipped += 1
            continue

        try:
            if _map_actors.is_passive_actor(actor):
                result = generate_passive_object_drop_result(
                    session,
                    object_level=basis_level,
                    difficulty=difficulty,
                    object_code=_int_field(actor, "code", "monster_code", default=0),
                    object_index=_passive_object_index(actor),
                    dungeon_id=dungeon_id,
                    special_items=(
                        bind_passive_object_item_entries(special_items, [actor])
                    ),
                )
            else:
                result = _generate_actor_result(
                    session,
                    actor,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                    basis_level=basis_level,
                    owner_actor_id=owner_actor_id,
                    wish_item_ids=wish_item_ids,
                    independent_bonus=independent_bonus,
                )
            set_preloaded_actor_drop(actor, result)
            stats.generated += 1
            stats.reserved_rows += len(result.drops or [])
        except Exception as exc:
            _set_error_bundle(actor, exc, scope="actor")
            stats.errors += 1

    hell_groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for actor in actors:
        if not bool(actor.get("is_hell_party_actor")):
            continue
        if bool(actor.get("is_hell_monster_script")):
            continue
        group_id = _int_field(actor, "hell_party_group_id", default=0)
        order = _int_field(actor, "hell_party_order", default=0)
        if group_id > 0:
            hell_groups.setdefault((group_id, order), []).append(actor)

    for group_actors in hell_groups.values():
        existing = _existing_group_bundle(group_actors)
        if existing is not None:
            _attach_group_bundle(group_actors, existing)
            continue
        reward_roll_count = max(
            _int_field(actor, "hell_reward_roll_count", default=0)
            for actor in group_actors
        )
        if reward_roll_count <= 0:
            continue
        representative = max(
            group_actors,
            key=lambda row: (
                _int_field(row, "level", default=0),
                _int_field(row, "seq", default=0),
            ),
        )
        try:
            result = _generate_hell_result(
                session,
                representative,
                dungeon_id=dungeon_id,
                difficulty=difficulty,
                basis_level=basis_level,
                owner_actor_id=owner_actor_id,
                wish_item_ids=wish_item_ids,
                independent_bonus=independent_bonus,
                reward_roll_count=reward_roll_count,
                special_hell_final_boss=False,
                include_independent=False,
                include_group_reward=True,
            )
            bundle = PreloadedDropBundle(result=result, scope="hell_group")
            _attach_group_bundle(group_actors, bundle)
            stats.groups += 1
            stats.reserved_rows += len(result.drops or [])
        except Exception as exc:
            bundle = _set_error_bundle(
                representative,
                exc,
                scope="hell_group",
                key=_GROUP_BUNDLE_KEY,
            )
            _attach_group_bundle(group_actors, bundle)
            stats.errors += 1

    if _special_hell_boss_room(session):
        candidates = [
            actor
            for actor in actors
            if not bool(actor.get("killed"))
            and not _map_actors.is_passive_actor(actor)
            and _map_actors.actor_counts_for_combat(actor)
            and bool(actor.get("is_blocking", True))
        ]
        for actor in candidates:
            if _SPECIAL_ACTOR_BUNDLE_KEY in actor:
                continue
            try:
                result = _generate_hell_result(
                    session,
                    actor,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                    basis_level=basis_level,
                    owner_actor_id=owner_actor_id,
                    wish_item_ids=wish_item_ids,
                    independent_bonus=independent_bonus,
                    reward_roll_count=0,
                    special_hell_final_boss=True,
                    include_independent=True,
                    include_group_reward=False,
                )
                actor[_SPECIAL_ACTOR_BUNDLE_KEY] = PreloadedDropBundle(
                    result=result,
                    scope="special_hell_actor",
                )
                stats.generated += 1
                stats.reserved_rows += len(result.drops or [])
            except Exception as exc:
                _set_error_bundle(
                    actor,
                    exc,
                    scope="special_hell_actor",
                    key=_SPECIAL_ACTOR_BUNDLE_KEY,
                )
                stats.errors += 1

        existing = _existing_group_bundle(
            candidates,
            key=_SPECIAL_GROUP_BUNDLE_KEY,
        )
        if candidates and existing is None:
            representative = max(
                candidates,
                key=lambda row: (
                    int(_int_field(row, "type", "mon_type", default=0) == 3),
                    _int_field(row, "level", default=0),
                    _int_field(row, "seq", default=0),
                ),
            )
            reward_roll_count = _hell_party.hell_party_reward_roll_count_for_mode(
                _int_field(session, "dungeon_special_hell_party_mode", default=1)
            )
            try:
                result = _generate_hell_result(
                    session,
                    representative,
                    dungeon_id=dungeon_id,
                    difficulty=difficulty,
                    basis_level=basis_level,
                    owner_actor_id=owner_actor_id,
                    wish_item_ids=wish_item_ids,
                    independent_bonus=independent_bonus,
                    reward_roll_count=reward_roll_count,
                    special_hell_final_boss=True,
                    include_independent=False,
                    include_group_reward=True,
                )
                existing = PreloadedDropBundle(
                    result=result,
                    scope="special_hell_group",
                )
                stats.groups += 1
                stats.reserved_rows += len(result.drops or [])
            except Exception as exc:
                existing = _set_error_bundle(
                    representative,
                    exc,
                    scope="special_hell_group",
                    key=_SPECIAL_GROUP_BUNDLE_KEY,
                )
                stats.errors += 1
        if existing is not None:
            _attach_group_bundle(
                candidates,
                existing,
                key=_SPECIAL_GROUP_BUNDLE_KEY,
            )

    for primary in actors:
        _mirror_preload_keys(primary, aliases.get(id(primary), ()))
    return stats


def preload_room_drops_before_visibility(
    server: Any,
    session: Any,
    *,
    source: str,
) -> str:
    """Prepare the bound room at the common actor-publication boundary.

    A source that publishes an actor must run this before its corresponding
    S2C packet.  Per-actor failures become cached error bundles, while a
    boundary-level failure remains observable in the source's protocol note.
    """
    try:
        return preload_visible_room_drops(
            server,
            session,
            source=str(source or "room_publish"),
        ).note()
    except Exception as exc:
        return "drop_preload=fatal type=%s" % type(exc).__name__


def _merge_results(
    bundles: Sequence[PreloadedDropBundle],
) -> MonsterDropResult:
    drops: List[DropInfo] = []
    gold_amount = 0
    notes: list[str] = []
    seed_before = 0
    seed_after = 0
    for index, bundle in enumerate(bundles):
        result = bundle.result
        if index == 0:
            seed_before = int(result.seed_before)
        seed_after = int(result.seed_after)
        gold_amount += int(result.gold_amount)
        drops.extend(list(result.drops or []))
        notes.append("%s=(%s)" % (bundle.scope, result.note or "none"))
    return MonsterDropResult(
        gold_amount=gold_amount,
        drops=drops,
        seed_before=seed_before,
        seed_after=seed_after,
        note=";".join(notes) if notes else "preload_empty",
    )


def consume_preloaded_actor_drops(
    session: Any,
    actor: dict[str, Any],
    *,
    release_group_reward: bool = False,
    special_hell_final_boss: bool = False,
) -> tuple[MonsterDropResult, str]:
    """Register prepared rows for one death; this path performs no drop rolls."""
    if not isinstance(actor, dict):
        return MonsterDropResult(note="preload_miss_no_actor"), "drop_preload=miss_no_actor"

    ordinary = actor.get(_ACTOR_BUNDLE_KEY)
    special = actor.get(_SPECIAL_ACTOR_BUNDLE_KEY)
    special_cache_hit = bool(
        special_hell_final_boss and isinstance(special, PreloadedDropBundle)
    )
    selected = special if special_cache_hit else ordinary
    if not isinstance(selected, PreloadedDropBundle):
        status = str(actor.get(_PRELOAD_STATUS_KEY) or "missing")
        return (
            MonsterDropResult(note="preload_%s" % status),
            "drop_preload=miss status=%s" % status,
        )
    if selected.consumed:
        return (
            MonsterDropResult(note="preload_already_consumed"),
            "drop_preload=already_consumed scope=%s" % selected.scope,
        )

    selected.consumed = True
    bundles: list[PreloadedDropBundle] = [selected]
    if isinstance(ordinary, PreloadedDropBundle) and ordinary is not selected:
        ordinary.consumed = True
    if isinstance(special, PreloadedDropBundle) and special is not selected:
        special.consumed = True

    group_released = 0
    group_key = (
        _SPECIAL_GROUP_BUNDLE_KEY
        if bool(special_hell_final_boss)
        else _GROUP_BUNDLE_KEY
    )
    if bool(release_group_reward):
        group_candidates = [actor.get(group_key)]
        if (
            not bool(special_hell_final_boss)
            and bool(actor.get("is_hell_party_actor"))
        ):
            actor_seq = _int_field(actor, "seq", default=0)
            hell_actors = [
                row
                for row in list(getattr(session, "dungeon_room_mons", None) or [])
                if isinstance(row, dict)
                and bool(row.get("is_hell_party_actor"))
                and not bool(row.get("is_hell_monster_script"))
            ]
            other_alive = any(
                row is not actor
                and not (
                    actor_seq > 0
                    and _int_field(row, "seq", default=0) == actor_seq
                )
                and not bool(row.get("killed"))
                for row in hell_actors
            )
            if not other_alive:
                # Some installed Hell helpers defer all groups to the final
                # death. Consume every prepared group then, without rerolling
                # or losing the groups owned by actors that died earlier.
                group_candidates.extend(row.get(group_key) for row in hell_actors)
        seen_groups: set[int] = set()
        for group in group_candidates:
            if not isinstance(group, PreloadedDropBundle):
                continue
            identity = id(group)
            if identity in seen_groups or group.consumed:
                continue
            seen_groups.add(identity)
            group.consumed = True
            bundles.append(group)
            group_released += 1

    result = _merge_results(bundles)
    register_preloaded_drop_result(session, result)
    return result, (
        "drop_preload=hit scope=%s special=%d group=%d drops=%d"
        % (
            selected.scope,
            int(special_cache_hit),
            group_released,
            len(result.drops or []),
        )
    )
