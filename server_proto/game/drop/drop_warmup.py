# -*- coding: utf-8 -*-
"""掉落索引 boot warmup（仅共享 PVF）。

硬规则（AGENTS 4.1.2）：
- 必须在 ``start.py`` / ``data.preload_script_pvf_and_warmup`` 中、
  接客户端之前完成；
- 失败 → 抛异常 → boot hard-fail，**禁止半开服**；
- actor 发布前的结果预加载与 DIE 消费路径禁止冷读 independent / ItemDictionary / monseter。
"""
from __future__ import annotations

from typing import Any, Dict


def warmup_drop_index(*, cache: Any = None) -> Dict[str, Any]:
    """预热 monseter/object + gold + pools + independent + world + mob lst。

    失败抛异常 → ``boot_warmup`` hard-fail。
    """
    from game.drop.equip_stackable_pools import (
        get_equip_stackable_pools,
        install_equip_stackable_pools,
        warmup_equip_stackable_pools,
    )
    from game.drop.gold_table import warmup_gold_table
    from game.drop.hell_drop_config import warmup_hell_drop_config
    from game.drop.independent_drop import warmup_independent_drop
    from game.drop.mob_item_pool import warmup_mob_item_pool
    from game.drop.monster_drop_config import warmup_monster_drop_config
    from game.drop.object_drop_config import warmup_object_drop_config
    from game.drop.world_drop import warmup_world_drop

    out: Dict[str, Any] = {"ok": 1}
    # The two authoritative lst registries must exist before any configured
    # item source is allowed to construct its weighted candidates.
    pool_cache_status: str | None = None
    if cache is not None:
        from game.drop.drop_pool_cache import (
            CACHE_DOMAIN,
            CACHE_SEMANTIC_VERSION,
            restore,
        )

        payload = cache.read(CACHE_DOMAIN, CACHE_SEMANTIC_VERSION)
        if payload is not None:
            try:
                out["pools"] = install_equip_stackable_pools(restore(payload))
                pool_cache_status = "hit"
            except Exception:
                cache.discard(CACHE_DOMAIN)
                pool_cache_status = "rebuild"
    if pool_cache_status != "hit":
        out["pools"] = warmup_equip_stackable_pools()
        if cache is not None and cache.enabled:
            from game.drop.drop_pool_cache import (
                CACHE_DOMAIN,
                CACHE_SEMANTIC_VERSION,
                record_count,
                to_payload,
            )

            pools = get_equip_stackable_pools()
            cache.write(
                CACHE_DOMAIN,
                CACHE_SEMANTIC_VERSION,
                to_payload(pools),
                record_count=record_count(pools),
            )
            pool_cache_status = str(
                cache.last_status.get(CACHE_DOMAIN) or "miss"
            )
        elif cache is not None:
            pool_cache_status = "disabled"
    if cache is not None:
        out["pools"]["cache"] = pool_cache_status or "miss"
    out["monseter"] = warmup_monster_drop_config()
    out["object"] = warmup_object_drop_config()
    out["gold"] = warmup_gold_table()
    out["independent"] = warmup_independent_drop()
    out["world"] = warmup_world_drop()
    out["mob_item"] = warmup_mob_item_pool()
    out["hell"] = warmup_hell_drop_config()

    from game.drop.equip_stackable_pools import get_equip_stackable_pools
    from game.drop.hell_drop_config import clear_hell_drop_config, get_hell_drop_config

    try:
        out["hell"]["unregistered_items"] = get_hell_drop_config().retain_registered_equipment(
            get_equip_stackable_pools().is_registered_item
        )
    except Exception:
        # A restored Hell cache with stale identities must not remain installed
        # while boot_warmup discards and rebuilds that cache.
        clear_hell_drop_config()
        raise

    equip_count = int((out["pools"] or {}).get("equip_count") or 0)
    stackable_count = int((out["pools"] or {}).get("stackable_count") or 0)
    independent_entries = int((out["independent"] or {}).get("entries") or 0)
    independent_monsters = int((out["independent"] or {}).get("monsters") or 0)
    world_levels = int((out["world"] or {}).get("levels") or 0)
    drop_prob_rows = int((out["monseter"] or {}).get("drop_prob_rows") or 0)
    object_drop_prob_rows = int(
        (out["object"] or {}).get("drop_prob_rows") or 0
    )
    gold_levels = int((out["gold"] or {}).get("levels") or 0)
    mob_lst = int((out["mob_item"] or {}).get("monster_lst") or 0)
    hell_drop_prob_rows = int((out["hell"] or {}).get("drop_prob_rows") or 0)
    hell_equipment_count = int((out["hell"] or {}).get("equipment_count") or 0)

    out["equip_count"] = equip_count
    out["stackable_count"] = stackable_count
    out["independent_entries"] = independent_entries
    out["independent_monsters"] = independent_monsters
    out["world_levels"] = world_levels
    out["drop_prob_rows"] = drop_prob_rows
    out["object_drop_prob_rows"] = object_drop_prob_rows
    out["gold_levels"] = gold_levels
    out["mob_lst"] = mob_lst
    out["hell_drop_prob_rows"] = hell_drop_prob_rows
    out["hell_equipment_count"] = hell_equipment_count
    out["unregistered_item_candidates"] = sum((
        int((out["pools"] or {}).get("equip_non_lst_rejected") or 0),
        int((out["independent"] or {}).get("unregistered_items") or 0),
        int((out["world"] or {}).get("unregistered_items") or 0),
        int((out["hell"] or {}).get("unregistered_items") or 0),
    ))

    # 最小完备性：主表 + 金表必须有；装备/材料池至少一侧可随机；independent 应有条目
    errors: list[str] = []
    if drop_prob_rows <= 0:
        errors.append("monseter drop_prob_rows=0")
    if object_drop_prob_rows <= 0:
        errors.append("object drop_prob_rows=0")
    if gold_levels <= 0:
        errors.append("gold table empty")
    if equip_count <= 0 and stackable_count <= 0:
        errors.append("equip+stackable pools both empty")
    if independent_entries <= 0:
        errors.append("independent_drop entries=0")
    if world_levels <= 0:
        errors.append("worlddrop levels=0")
    if hell_drop_prob_rows <= 0:
        errors.append("hell drop_prob_rows=0")
    if hell_equipment_count <= 0:
        errors.append("hell equipment_count=0")
    if errors:
        out["ok"] = 0
        raise RuntimeError("drop_index warmup incomplete: " + "; ".join(errors))
    return out


def is_drop_index_ready() -> bool:
    """供启动后自检：关键索引是否已 load。"""
    try:
        from game.drop.equip_stackable_pools import get_equip_stackable_pools
        from game.drop.gold_table import get_gold_table
        from game.drop.hell_drop_config import get_hell_drop_config
        from game.drop.independent_drop import get_independent_drop_index
        from game.drop.monster_drop_config import get_monster_drop_config
        from game.drop.object_drop_config import get_object_drop_config
        from game.drop.world_drop import get_world_drop_index

        return bool(
            get_monster_drop_config().loaded
            and get_object_drop_config().loaded
            and get_gold_table().loaded
            and get_equip_stackable_pools().loaded
            and get_independent_drop_index().loaded
            and get_world_drop_index().loaded
            and get_hell_drop_config().loaded
        )
    except Exception:
        return False
