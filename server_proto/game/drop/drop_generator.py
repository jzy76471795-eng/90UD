# -*- coding: utf-8 -*-
"""击杀掉落合成管线（多通道并联，非互斥）。

顺序（对齐 90qiyuan + s4a12 DropGenerator）::

  1. monseter 金 rate
  2. monseter type1 材料
  3. monseter type2 装备
  4. monseter type3 材料
  5. area material（副本区域固定材料，独立判定）
  6. mob [item] 专属池（有效池每次必选一件）
  7. independent（每条 count 次）
  8. worlddrop

硬规则：**禁止** 掉落未在配置/池中、且无法在 equipment|stackable lst 解析的物品。
金币 template_id=0 例外。
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence, Tuple

from game.character.item_metadata import resolve_item_metadata
from game.drop.area_material_drop import area_material_item_id, roll_area_material_item
from game.drop.dnf_lcg import DnfLcg, roll_weighted_occurrences
from game.drop.equip_stackable_pools import get_equip_stackable_pools
from game.drop.gold_table import get_gold_table
from game.drop.hell_drop_config import (
    HELL_DROP_CHANCE_DENOMINATOR,
    HellEquipmentEntry,
    get_hell_drop_config,
    hell_epic_grade_levels,
)
from game.drop.independent_drop import get_independent_drop_index
from game.drop.mob_item_pool import get_mob_item_pool_index
from game.drop.models import DropInfo, MonsterDropRequest, MonsterDropResult
from game.drop.monster_drop_config import DROP_DENOMINATOR, get_monster_drop_config
from game.drop.object_drop_config import get_object_drop_config
from game.drop.world_drop import get_world_drop_index
from game.drop.wish_service import valid_wish_item_ids
from pvf.epic_piece_index import get_epic_piece_index


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _allow_configured_item(item_id: int) -> bool:
    """Only accept IDs registered by equipment.lst or stackable.lst."""
    iid = int(item_id)
    if iid <= 0:
        return False
    try:
        return get_equip_stackable_pools().is_registered_item(iid)
    except Exception:
        return False


def _append_item(
    drops: List[DropInfo],
    *,
    item_id: int,
    stack_count: int,
    slot_counter: int,
    source: str,
) -> int:
    iid = int(item_id)
    if iid <= 0:
        return slot_counter
    if not _allow_configured_item(iid):
        return slot_counter
    endurance = 0
    try:
        meta = resolve_item_metadata(iid)
        if meta.is_equipment:
            endurance = int(meta.durability or 0) & 0xFFFF
    except Exception:
        endurance = 0
    slot_counter += 1
    drops.append(
        DropInfo(
            scene_slot=int(slot_counter) & 0xFFFF,
            template_id=iid & 0xFFFFFFFF,
            stack_count=max(1, int(stack_count)) & 0xFFFFFFFF,
            endurance=endurance,
            upgrade_level=0,
            source=source,
        )
    )
    return slot_counter


def _append_area_material(
    drops: List[DropInfo],
    *,
    dungeon_id: int,
    lcg: DnfLcg,
    slot_counter: int,
    allow_item: Callable[[int], bool],
) -> Tuple[int, str]:
    try:
        configured_item_id = area_material_item_id(int(dungeon_id))
        # Reject an invalid static mapping before it can consume the room LCG.
        if configured_item_id <= 0 or not allow_item(configured_item_id):
            return int(slot_counter), ""
        item_id = roll_area_material_item(int(dungeon_id), lcg)
        if item_id <= 0 or not allow_item(item_id):
            return int(slot_counter), ""
        before = int(slot_counter)
        slot = _append_item(
            drops,
            item_id=item_id,
            stack_count=1,
            slot_counter=before,
            source="area_material",
        )
        if slot == before:
            return slot, ""
        return slot, "area_material=%d dgn=%d" % (item_id, dungeon_id)
    except Exception as exc:
        return int(slot_counter), "area_material_err=%s" % type(exc).__name__


def _choose_hell_equipment(
    lcg: DnfLcg,
    *,
    item_basis_level: int,
    rarity: Optional[int],
    preferred_item_ids: Sequence[int] = (),
    preferred_weight: int = 1,
    excluded_item_ids: Sequence[int] = (),
    include_preferred_outside_grade: bool = False,
    grade_levels: Optional[Sequence[int]] = None,
    equipment_levels: Optional[Sequence[int]] = None,
) -> Optional[HellEquipmentEntry]:
    """Choose one Hell Party equipment from the prewarmed real [.equ] index."""
    return get_hell_drop_config().choose_equipment(
        lcg,
        item_basis_level=item_basis_level,
        rarity=rarity,
        preferred_item_ids=preferred_item_ids,
        preferred_weight=preferred_weight,
        excluded_item_ids=excluded_item_ids,
        include_preferred_outside_grade=include_preferred_outside_grade,
        grade_levels=grade_levels,
        equipment_levels=equipment_levels,
    )


def generate_hell_party_drops(
    *,
    monster_code: int,
    dungeon_basis_level: int,
    dungeon_difficulty: int,
    hell_party_mode: int,
    reward_roll_count: int,
    lcg: DnfLcg,
    slot_counter: int,
    monster_level: int = 0,
    allow_item: Optional[Callable[[int], bool]] = None,
    preferred_epic_item_ids: Sequence[int] = (),
    preferred_epic_weight: int = 1,
    special_wish_mode: bool = False,
    wish_probability_percent: int = 25,
    independent_drop_rate_bonus_percent: int = 0,
    include_independent_drop: bool = True,
    include_group_reward: bool = True,
    epic_piece_bonus_count: int = 0,
) -> Tuple[MonsterDropResult, int]:
    """Generate PVF Hell Party independent drops plus one group reward.

    ``dungeon_basis_level`` remains the PVF probability-table input. Epic
    equipment and Epic fragments use the killed actor's five-level tier based
    on the crafted equipment's actual ``[minimum level]``. All Hell reward
    sources use the same normal/advanced/Epic rarity policy.
    """
    if not _env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
        return MonsterDropResult(note="ITEM_DROP=OFF"), int(slot_counter)
    if not _env_bool("USDOF_GAME_DUNGEON_HELL_DROP", True):
        return MonsterDropResult(note="HELL_DROP=OFF"), int(slot_counter)

    config = get_hell_drop_config()
    hell_epic_only = _env_bool("USDOF_GAME_HELL_EPIC_ONLY", False)
    basis_level = max(1, int(dungeon_basis_level))
    source_level = max(1, int(monster_level or basis_level))
    epic_equipment_levels = hell_epic_grade_levels(source_level)
    special_wish_ids = (
        valid_wish_item_ids(preferred_epic_item_ids, config=config)
        if bool(special_wish_mode)
        else ()
    )
    wish_percent = max(0, min(100, int(wish_probability_percent)))
    rate = config.drop_rate(basis_level, int(dungeon_difficulty))
    equipment_rolls = (
        max(0, int(reward_roll_count))
        if bool(include_group_reward) and rate > 0
        else 0
    )
    # Epic fragments use the same PVF-provided Hell reward-roll count, but
    # form an independent reward stream.  A zero equipment base rate must not
    # suppress their rolls.
    fragment_rolls = (
        max(0, int(reward_roll_count)) if bool(include_group_reward) else 0
    )
    if fragment_rolls:
        fragment_rolls += max(0, int(epic_piece_bonus_count))
    allow = allow_item or _allow_configured_item
    seed_before = int(lcg.seed)
    slot = int(slot_counter) & 0xFFFF
    drops: List[DropInfo] = []
    notes: List[str] = []

    if hell_epic_only:
        notes.append("hell_epic_only=1")

    # PVF independent_drop remains actor-scoped. It is separate from the
    # [difficulty] equipment rolls and therefore applies to every Hell actor.
    if bool(include_independent_drop) and _env_bool(
        "USDOF_GAME_DUNGEON_INDEPENDENT_DROP", True
    ):
        try:
            independent_ids = get_independent_drop_index().generate(
                monster_code=int(monster_code),
                difficulty=int(dungeon_difficulty),
                dungeon_basis_level=basis_level,
                lcg=lcg,
                allow_item=allow,
                drop_rate_bonus_percent=independent_drop_rate_bonus_percent,
            )
            for item_id in independent_ids:
                before = len(drops)
                slot = _append_item(
                    drops,
                    item_id=int(item_id),
                    stack_count=1,
                    slot_counter=slot,
                    source="hell_independent",
                )
                if len(drops) > before:
                    notes.append("hell_indep=%d" % int(item_id))
        except Exception as exc:
            notes.append("hell_indep_err=%s" % type(exc).__name__)

    if bool(include_group_reward) and rate <= 0 and int(reward_roll_count) > 0:
        notes.append("hell_rate=0")

    for index in range(equipment_rolls):
        chance_roll = int(lcg.next(HELL_DROP_CHANCE_DENOMINATOR))
        if chance_roll > rate:
            notes.append("hell_miss#%d=%d/%d" % (index + 1, chance_roll, rate))
            continue
        rolled_rarity = int(config.roll_rarity(lcg, hell_party_mode))
        # Temporary test mode still consumes the ETC rarity roll for a stable
        # sequence, then forces the equipment result to the Epic pool.
        roll_rarity = 4 if hell_epic_only else rolled_rarity
        if hell_epic_only:
            notes.append(
                "hell_epic_only_force#%d from_r=%d" % (index + 1, rolled_rarity)
            )
        elif roll_rarity < 0:
            notes.append("hell_rarity_empty#%d" % (index + 1))
            continue
        wish_hit = False
        wish_equip: Optional[HellEquipmentEntry] = None
        excluded_wish_ids: tuple[int, ...] = ()
        if int(roll_rarity) == 4 and special_wish_ids:
            wish_roll = int(lcg.next(100))
            if wish_roll < wish_percent:
                wish_hit = True
                wish_item_id = int(
                    special_wish_ids[lcg.next(len(special_wish_ids))]
                )
                wish_equip = config.equipment_for_item_id(wish_item_id)
                if wish_equip is not None:
                    notes.append(
                        "hell_wish_hit#%d=%d roll=%d/%d"
                        % (index + 1, wish_item_id, wish_roll, wish_percent)
                    )
                else:
                    notes.append(
                        "hell_wish_stale#%d=%d roll=%d/%d"
                        % (index + 1, wish_item_id, wish_roll, wish_percent)
                    )
            else:
                excluded_wish_ids = tuple(special_wish_ids)
                notes.append(
                    "hell_wish_miss#%d roll=%d/%d"
                    % (index + 1, wish_roll, wish_percent)
                )

        # A special-mode hit uses the selected ID directly.  A miss excludes
        # every wish ID from the ordinary Epic pool.  Other Hell paths retain
        # their existing preferred-item behavior.
        if wish_hit:
            equip = wish_equip
        else:
            equip = _choose_hell_equipment(
                lcg,
                item_basis_level=basis_level,
                rarity=roll_rarity,
                preferred_item_ids=(
                    preferred_epic_item_ids
                    if int(roll_rarity) == 4 and not special_wish_mode
                    else ()
                ),
                preferred_weight=(
                    preferred_epic_weight if not special_wish_mode else 1
                ),
                excluded_item_ids=excluded_wish_ids,
                include_preferred_outside_grade=False,
                equipment_levels=(
                    epic_equipment_levels if int(roll_rarity) == 4 else None
                ),
            )
        if equip is None or int(equip.item_id) <= 0:
            notes.append("hell_empty#%d roll_r=%d" % (index + 1, roll_rarity))
            continue
        if hell_epic_only and int(equip.rarity) != 4:
            # Defensive guard for stale/custom indexes and fallback paths.
            notes.append(
                "hell_epic_only_block#%d item=%d rarity=%d"
                % (index + 1, int(equip.item_id), int(equip.rarity))
            )
            continue
        item_id = int(equip.item_id)
        if not allow(item_id):
            notes.append("hell_blocked#%d roll_r=%d item=%d" % (index + 1, roll_rarity, item_id))
            continue
        before = len(drops)
        slot = _append_item(
            drops,
            item_id=item_id,
            stack_count=1,
            slot_counter=slot,
            source=(
                "hell_wish_equipment"
                if wish_hit
                else "hell_equipment"
            ),
        )
        if len(drops) > before:
            if wish_hit:
                notes.append(
                    "hell_wish_equipment=%d rarity=%d grade=%d"
                    % (item_id, int(equip.rarity), int(equip.grade))
                )
            else:
                notes.append(
                    "hell_equipment=%d rarity=%d grade=%d roll_r=%d"
                    % (item_id, int(equip.rarity), int(equip.grade), int(roll_rarity))
                )

    # Hell fragments are a separate completion reward, not contingent on the
    # equipment rolls reaching rarity=Epic or their base probability. Each
    # roll selects directly from prewarmed fragments keyed by the actual level
    # of the equipment they craft. Fragment targets never extend the tier for
    # wish items.
    fragment_drop_count = 0
    fragment_fallback_count = 0
    for index in range(fragment_rolls):
        fragment_target = None
        try:
            fragment_target = get_epic_piece_index().choose_piece(
                lcg,
                output_levels=epic_equipment_levels,
                preferred_output_item_ids=(
                    () if special_wish_mode else preferred_epic_item_ids
                ),
                preferred_weight=(
                    1 if special_wish_mode else preferred_epic_weight
                ),
            )
            piece_id = int(fragment_target.piece_id) if fragment_target is not None else 0
        except Exception as exc:
            piece_id = 0
            notes.append("epic_piece_err#%d=%s" % (index + 1, type(exc).__name__))
        if piece_id <= 0:
            notes.append(
                "epic_piece_empty#%d%s"
                % (index + 1, " epic_only" if hell_epic_only else "")
            )
            continue
        before = slot
        slot = _append_item(
            drops,
            item_id=piece_id,
            stack_count=1,
            slot_counter=slot,
            source="epic_piece",
        )
        if slot == before:
            notes.append("epic_piece_blocked#%d=%d" % (index + 1, piece_id))
            continue
        fragment_drop_count += 1
        notes.append(
            "epic_piece#%d=%d target=%d level=%d" % (
                index + 1,
                piece_id,
                int(fragment_target.output_item_id),
                int(fragment_target.output_level),
            )
        )
    notes.append(
        "hell_epic_levels=%s;epic_piece_rolls=%d drops=%d fallback=%d"
        % (
            "/".join(str(level) for level in epic_equipment_levels),
            fragment_rolls,
            fragment_drop_count,
            fragment_fallback_count,
        )
    )

    return (
        MonsterDropResult(
            gold_amount=0,
            drops=drops,
            seed_before=seed_before,
            seed_after=int(lcg.seed),
            note=";".join(notes) if notes else "hell_none",
        ),
        int(slot) & 0xFFFF,
    )


def generate_monster_drops(
    request: MonsterDropRequest,
    lcg: DnfLcg,
    slot_counter: int,
    *,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> Tuple[MonsterDropResult, int]:
    """生成掉落；返回 (result, new_slot_counter)。"""
    if not _env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
        return MonsterDropResult(note="ITEM_DROP=OFF"), int(slot_counter)

    allow = allow_item or _allow_configured_item
    seed_before = int(lcg.seed)
    drops: List[DropInfo] = []
    gold_amount = 0
    notes: List[str] = []
    slot = int(slot_counter) & 0xFFFF

    mon_lv = max(1, int(request.monster_level))
    mon_type = int(request.monster_type)
    mon_code = int(request.monster_code)
    difficulty = int(request.difficulty)
    basis_lv = int(request.dungeon_basis_level)

    cfg = get_monster_drop_config()
    gold_rate, type1_rate, type2_rate, type3_rate = cfg.get_all_drop_rates(
        mon_lv, mon_type, difficulty
    )

    # --- 1 金币通道 ---
    gold_table = get_gold_table()
    rolled_gold_base = gold_table.roll_amount(lcg, mon_lv, difficulty)
    rolled_gold_amt = max(
        1,
        int(float(rolled_gold_base) * cfg.difficulty_multiplier(0, difficulty)),
    )
    if gold_rate > lcg.next(DROP_DENOMINATOR):
        gold_amount = int(rolled_gold_amt)
        slot += 1
        drops.append(
            DropInfo(
                scene_slot=slot & 0xFFFF,
                template_id=0,
                stack_count=gold_amount & 0xFFFFFFFF,
                endurance=0,
                source="gold",
            )
        )
        notes.append("gold=%d" % gold_amount)
    else:
        gold_amount = 0

    pools = get_equip_stackable_pools()

    # --- 2 type1 材料 ---
    if type1_rate > lcg.next(DROP_DENOMINATOR):
        rarity = cfg.roll_rarity(lcg)
        item_id = pools.choose_stackable(lcg, mon_lv, rarity)
        if item_id > 0 and allow(item_id):
            slot = _append_item(
                drops, item_id=item_id, stack_count=1, slot_counter=slot, source="type1"
            )
            notes.append("type1=%d r=%d" % (item_id, rarity))

    # --- 3 type2 装备 ---
    if type2_rate > lcg.next(DROP_DENOMINATOR):
        rarity = cfg.roll_rarity(lcg)
        item_id = pools.choose_equipment(lcg, mon_lv, rarity)
        if item_id > 0 and allow(item_id):
            slot = _append_item(
                drops, item_id=item_id, stack_count=1, slot_counter=slot, source="type2"
            )
            notes.append("type2=%d r=%d" % (item_id, rarity))

    # --- 4 type3 材料 ---
    if type3_rate > lcg.next(DROP_DENOMINATOR):
        rarity = cfg.roll_rarity(lcg)
        item_id = pools.choose_stackable(lcg, mon_lv, rarity)
        if item_id > 0 and allow(item_id):
            slot = _append_item(
                drops, item_id=item_id, stack_count=1, slot_counter=slot, source="type3"
            )
            notes.append("type3=%d r=%d" % (item_id, rarity))

    # --- 5 区域通用材料 ---
    if _env_bool("USDOF_GAME_DUNGEON_AREA_MATERIAL_DROP", True):
        slot, area_note = _append_area_material(
            drops,
            dungeon_id=int(request.dungeon_id),
            lcg=lcg,
            slot_counter=slot,
            allow_item=allow,
        )
        if area_note:
            notes.append(area_note)

    # --- 6 mob [item] 专属独立必掉 ---
    if _env_bool("USDOF_GAME_DUNGEON_MOB_ITEM_DROP", True):
        try:
            mob_ids = get_mob_item_pool_index().generate(mon_code, lcg, allow)
            for iid in mob_ids:
                slot = _append_item(
                    drops, item_id=iid, stack_count=1, slot_counter=slot, source="mob_item"
                )
                notes.append("mob_item=%d" % iid)
        except Exception as exc:
            notes.append("mob_item_err=%s" % type(exc).__name__)

    # --- 7 independent（count 次判定）---
    if _env_bool("USDOF_GAME_DUNGEON_INDEPENDENT_DROP", True):
        try:
            ind_ids = get_independent_drop_index().generate(
                monster_code=mon_code,
                difficulty=difficulty,
                dungeon_basis_level=basis_lv,
                lcg=lcg,
                allow_item=allow,
                drop_rate_bonus_percent=(
                    request.independent_drop_rate_bonus_percent
                ),
            )
            for iid in ind_ids:
                slot = _append_item(
                    drops,
                    item_id=iid,
                    stack_count=1,
                    slot_counter=slot,
                    source="independent",
                )
                notes.append("indep=%d" % iid)
        except Exception as exc:
            notes.append("indep_err=%s" % type(exc).__name__)

    # --- 8 worlddrop ---
    if _env_bool("USDOF_GAME_DUNGEON_WORLD_DROP", True):
        try:
            w_ids = get_world_drop_index().generate(mon_lv, lcg, allow)
            for iid in w_ids:
                slot = _append_item(
                    drops, item_id=iid, stack_count=1, slot_counter=slot, source="world"
                )
                notes.append("world=%d" % iid)
        except Exception as exc:
            notes.append("world_err=%s" % type(exc).__name__)

    result = MonsterDropResult(
        gold_amount=gold_amount,
        drops=drops,
        seed_before=seed_before,
        seed_after=int(lcg.seed),
        note=";".join(notes) if notes else "none",
    )
    return result, int(slot) & 0xFFFF


def generate_passive_object_drops(
    *,
    object_level: int,
    difficulty: int,
    object_code: int,
    object_index: int,
    special_items: Sequence[object],
    lcg: DnfLcg,
    slot_counter: int,
    dungeon_id: int = -1,
    allow_item: Optional[Callable[[int], bool]] = None,
) -> Tuple[MonsterDropResult, int]:
    """Generate one destroyed passive object's ordinary and DGN-specific drops.

    ``special_items`` contains explicit instance bindings produced by
    bind_passive_object_item_entries, not raw DGN group rows. The default
    Stage3 pre-drop path owns these entries and suppresses DIE duplicates.
    """
    if not _env_bool("USDOF_GAME_DUNGEON_ITEM_DROP", True):
        return MonsterDropResult(note="ITEM_DROP=OFF"), int(slot_counter)
    if not _env_bool("USDOF_GAME_DUNGEON_PASSIVE_OBJECT_DROP", True):
        return MonsterDropResult(note="PASSIVE_OBJECT_DROP=OFF"), int(slot_counter)

    allow = allow_item or _allow_configured_item
    level = max(1, min(200, int(object_level or 1)))
    diff = max(0, min(4, int(difficulty or 0)))
    index = max(0, int(object_index or 0))
    slot = int(slot_counter) & 0xFFFF
    seed_before = int(lcg.seed)
    drops: List[DropInfo] = []
    notes: List[str] = []
    gold_amount = 0

    config = get_object_drop_config()
    gold_rate, type1_rate, type2_rate, type3_rate = config.get_all_drop_rates(
        level, diff
    )
    window = config.grade_window(level)
    pools = get_equip_stackable_pools()

    rolled_gold_base = get_gold_table().roll_amount(lcg, level, diff)
    rolled_gold_amount = max(
        1,
        int(float(rolled_gold_base) * config.difficulty_multiplier(0, diff)),
    )
    if gold_rate > lcg.next(DROP_DENOMINATOR):
        gold_amount = int(rolled_gold_amount)
        slot += 1
        drops.append(
            DropInfo(
                scene_slot=slot & 0xFFFF,
                template_id=0,
                stack_count=gold_amount & 0xFFFFFFFF,
                endurance=0,
                source="object_gold",
            )
        )
        notes.append("gold=%d" % gold_amount)

    for channel, rate, choose_equipment in (
        ("object_type1", type1_rate, False),
        ("object_type2", type2_rate, True),
        ("object_type3", type3_rate, False),
    ):
        if int(rate) <= lcg.next(DROP_DENOMINATOR):
            continue
        rarity = config.roll_rarity(lcg)
        if choose_equipment:
            item_id = pools.choose_equipment_in_window(
                lcg,
                level,
                rarity,
                down=window.down,
                up=window.up,
            )
        else:
            item_id = pools.choose_stackable_in_window(
                lcg,
                level,
                rarity,
                down=window.down,
                up=window.up,
            )
        if int(item_id) <= 0 or not allow(int(item_id)):
            continue
        before = slot
        slot = _append_item(
            drops,
            item_id=int(item_id),
            stack_count=1,
            slot_counter=slot,
            source=channel,
        )
        if slot != before:
            notes.append("%s=%d r=%d" % (channel, int(item_id), rarity))

    if _env_bool("USDOF_GAME_DUNGEON_AREA_MATERIAL_DROP", True):
        slot, area_note = _append_area_material(
            drops,
            dungeon_id=int(dungeon_id),
            lcg=lcg,
            slot_counter=slot,
            allow_item=allow,
        )
        if area_note:
            notes.append(area_note)

    client_pre_drop = _env_bool("USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM", True)
    if not client_pre_drop:
        for entry in list(special_items or ()):
            try:
                entry_index = int(
                    getattr(entry, "object_index", None)
                    if not isinstance(entry, dict)
                    else entry.get("object_index", -1)
                )
                item_id = int(
                    getattr(entry, "item_id", None)
                    if not isinstance(entry, dict)
                    else entry.get("item_id", 0)
                )
                drop_rate = int(
                    getattr(entry, "drop_rate", None)
                    if not isinstance(entry, dict)
                    else entry.get("drop_rate", 0)
                )
            except (TypeError, ValueError):
                continue
            if entry_index != index or item_id <= 0 or drop_rate <= 0:
                continue
            if not allow(item_id):
                continue
            before = slot
            occurrences = roll_weighted_occurrences(
                lcg,
                drop_rate,
                denominator=DROP_DENOMINATOR,
            )
            for _ in range(occurrences):
                slot = _append_item(
                    drops,
                    item_id=item_id,
                    stack_count=1,
                    slot_counter=slot,
                    source="passive_object_special",
                )
            if slot != before:
                notes.append(
                    "special[%d]=%dx%d"
                    % (entry_index, item_id, int(slot - before))
                )
    elif special_items:
        notes.append("special=owned_by_001d")

    if not notes:
        notes.append("none")
    notes.append("object=%d index=%d level=%d" % (int(object_code), index, level))
    return (
        MonsterDropResult(
            gold_amount=gold_amount,
            drops=drops,
            seed_before=seed_before,
            seed_after=int(lcg.seed),
            note=";".join(notes),
        ),
        int(slot) & 0xFFFF,
    )
