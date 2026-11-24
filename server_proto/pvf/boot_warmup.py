"""Process boot warmup for runtime PVF domain indexes.

Hard standard (AGENTS 5.1):
- Script.pvf source bytes plus complete path/name/group metadata are prepared
  in one shared memory reader before listeners start.
- State-building domains for Channel, Game, maps, quests, drops and required
  item metadata warm here and hard-fail before clients are accepted.
- The Win10/Win11 ``core`` profile keeps every player-runtime hard domain but
  defers the GM-only full item catalog until the GM console requests it.  The
  ``full`` profile remains available for maintenance validation and delegates
  the GM catalog domain to the same dedicated worker process.
- Other operation-only catalog scans are declared here but deferred by
  default.  An item-ID-based craft/enchant/container request may parse only its
  requested script through the same frozen shared reader; it must not open a
  private reader or scan the unpack tree.

Call ``warmup_all_runtime_indexes()`` from ``data`` after either selective
memory preparation or the diagnostic full-residency preparation.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional


PvfStatusCallback = Callable[[str, Dict[str, Any]], None]
PvfTimingCallback = Callable[[str, Dict[str, Any]], None]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _timed(label: str, fn) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        payload = fn() or {}
        if not isinstance(payload, dict):
            payload = {"result": payload}
        payload.setdefault("ok", 1)
    except Exception as exc:
        payload = {
            "ok": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    payload["elapsed_ms"] = int(round((time.perf_counter() - t0) * 1000.0))
    payload["label"] = label
    return payload


def format_pvf_domain_status_line(name: str, payload: dict[str, Any]) -> str:
    """Format one boot-time PVF domain result for the startup console."""
    if payload.get("ok"):
        if name == "cerashop_index":
            skipped_count = int(payload.get("skipped_duplicate_count") or 0)
            if skipped_count > 0:
                details = tuple(payload.get("skipped_duplicate_details") or ())
                detail_text = " | ".join(str(detail) for detail in details)
                return (
                    "PVF-%s 数据加载成功；跳过重复商品 %d 条：%s"
                    % (name, skipped_count, detail_text)
                )
        if name == "cera_package_index":
            skipped_count = int(payload.get("skipped_package_count") or 0)
            filtered_count = int(payload.get("filtered_reward_count") or 0)
            messages = []
            if skipped_count > 0:
                details = tuple(payload.get("skipped_package_details") or ())
                detail_text = " | ".join(str(detail) for detail in details)
                messages.append(
                    "跳过定义错误礼包 %d 个：%s" % (skipped_count, detail_text)
                )
            if filtered_count > 0:
                details = tuple(payload.get("filtered_reward_details") or ())
                detail_text = " | ".join(str(detail) for detail in details)
                messages.append(
                    "保留礼包并在开启时过滤无效奖励 %d 条：%s"
                    % (filtered_count, detail_text)
                )
            if messages:
                return "PVF-%s 数据加载成功；%s" % (name, "；".join(messages))
        if name == "booster_item_index":
            skipped_count = int(
                payload.get("booster_skipped_zero_count_candidate_count") or 0
            )
            if skipped_count > 0:
                details = tuple(
                    payload.get("booster_skipped_zero_count_candidate_details") or ()
                )
                return (
                    "PVF-%s 数据加载成功；跳过数量为 0 的奖励候选 %d 条：%s"
                    % (name, skipped_count, " | ".join(str(detail) for detail in details))
                )
        return f"PVF-{name} 数据加载成功。"
    error = payload.get("error") or "unknown"
    return f"PVF-{name} 数据加载失败，原因：{error}"


def _notify_pvf_domain_status(
    callback: Optional[PvfStatusCallback],
    name: str,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(name, payload)
    except Exception:
        # Startup logging must not change whether a domain itself warmed.
        pass


def _notify_pvf_timing(
    callback: Optional[PvfTimingCallback],
    event: str,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(event, payload)
    except Exception:
        # Metrics are observational and must not alter the warmup result.
        pass


def _require_shared_preloaded() -> Any:
    """Compatibility-named guard for the prepared shared memory reader."""
    from pvf import pvf_runtime
    from config import PVF_PATH

    shared = pvf_runtime.get_shared_reader(PVF_PATH)
    if shared is None or not pvf_runtime.is_memory_prepared(PVF_PATH):
        # Force the shared memory path once more; never open a private reader.
        shared = pvf_runtime.ensure_shared_reader(PVF_PATH)
    if shared is None or not pvf_runtime.is_memory_prepared(PVF_PATH):
        raise RuntimeError(
            "shared Script.pvf memory structure is not prepared; boot warmup refused. "
            "Prepare the shared memory reader before warmup_all_runtime_indexes()."
        )
    return shared


def warmup_all_runtime_indexes(
    *,
    require_preloaded: bool = True,
    status_callback: Optional[PvfStatusCallback] = None,
    timing_callback: Optional[PvfTimingCallback] = None,
    profile: str = "full",
) -> dict[str, Any]:
    """Warm required PVF-backed runtime indexes used by Channel/Game.

    Returns a structured report with per-domain elapsed_ms. Raises on hard
    failure when require_preloaded=True (default).
    """
    warmup_profile = str(profile or "").strip().lower()
    if warmup_profile not in {"core", "full"}:
        raise ValueError("invalid PVF warmup profile: %s" % profile)

    t0 = time.perf_counter()
    report: dict[str, Any] = {
        "ok": 1,
        "profile": warmup_profile,
        "domains": {},
    }

    if require_preloaded:
        shared = _require_shared_preloaded()
        report["shared_reader"] = type(shared).__name__
        report["groups_preloaded"] = int(bool(getattr(shared, "groups_preloaded", False)))
        report["memory_prepared"] = 1
    else:
        from pvf import pvf_runtime
        from config import PVF_PATH

        shared = pvf_runtime.ensure_shared_reader(PVF_PATH)
        report["shared_reader"] = type(shared).__name__ if shared is not None else None
        report["groups_preloaded"] = int(
            bool(shared is not None and getattr(shared, "groups_preloaded", False))
        )
        report["memory_prepared"] = int(
            bool(shared is not None and pvf_runtime.is_memory_prepared(PVF_PATH))
        )

    # Cache identity is created only after shared memory preparation. Cached
    # indexes accelerate parsing only; they never replace PVF source/groups.
    from pvf.index_cache import create_cache_context

    cache = create_cache_context()
    report["pvf_sha256"] = cache.identity.sha_prefix
    report["cache_cleanup_count"] = cache.cleanup_count
    shop_bundle = {"loaded": False, "write_needed": False, "cache": "miss"}
    shop_bundle_schema_version = 11

    def _warm_quest_index() -> dict[str, Any]:
        from game.quest.quest_index import warmup_quest_index

        return dict(
            warmup_quest_index(
                full_metas=True,
                full_dungeons=True,
                cache=cache,
            )
        )

    def _warm_daily_challenge_index() -> dict[str, Any]:
        from game.quest.daily_challenge_index import (
            warmup_daily_challenge_index,
        )

        return dict(warmup_daily_challenge_index())

    def _warm_main_quest_chains() -> dict[str, Any]:
        from game.chain_loader import load_chain

        chain_helper = load_chain("helper-epic_chain_complete.py")
        return dict(chain_helper.warmup_main_quest_chain_index())

    def _warm_dungeon_map_index() -> dict[str, Any]:
        from game.chain_loader import load_chain

        map_index = load_chain("helper-dungeon_map_monsters.py")
        special_conditions = load_chain("helper-dungeon_special_conditions.py")
        tower_actors = load_chain("helper-death_tower_actors.py")
        tower_drops = load_chain("helper-tower_drop_policy.py")
        another_arad = load_chain("helper-another_arad.py")
        circle_dungeon = load_chain("helper-circle_dungeon_info.py")
        altar = load_chain("helper-altar-pvf.py")
        ridable_objects = load_chain("helper-dungeon_ridable_objects.py")
        tournament = load_chain("helper-dungeon_tournament.py")
        guild_dungeon = load_chain("helper-guild_dungeon_purification.py")
        license_boss = load_chain("helper-dungeon_license_boss.py")
        result = dict(map_index.warmup_map_index())
        result.update(ridable_objects.warmup_ridable_object_index())
        result.update(special_conditions.warmup_special_condition_index())
        result.update(tower_actors.warmup_death_tower_actor_config())
        result.update(tower_drops.warmup_death_tower_item_index())
        result.update(another_arad.warmup_another_arad_index())
        result.update(circle_dungeon.warmup_circle_dungeon_info_index())
        result.update(altar.warmup_altar_index())
        result.update(tournament.warmup_tournament_index())
        result.update(
            guild_dungeon.warmup_guild_dungeon_purification_index()
        )
        result.update(license_boss.warmup_license_dungeon_boss_index())
        return result

    def _warm_hell_party_index() -> dict[str, Any]:
        from game.chain_loader import load_chain

        hell_party = load_chain("helper-dungeon_hell_party.py")
        return dict(hell_party.warmup_hell_party_index())

    def _warm_quest_rewards() -> dict[str, Any]:
        from game.quest.quest_rewards import warmup_quest_reward_index

        return dict(warmup_quest_reward_index(full_sections=True, cache=cache))

    def _warm_default_unlock_index() -> dict[str, Any]:
        from game.quest.default_unlocks import warmup_default_unlock_index

        return dict(warmup_default_unlock_index())

    def _warm_item_metadata() -> dict[str, Any]:
        from game.character.item_metadata import warmup_item_metadata_index

        return dict(warmup_item_metadata_index())

    def _warm_magic_seal_random_option_index() -> dict[str, Any]:
        from game.character.magic_seal_random_option_index import (
            warmup_magic_seal_random_option_index,
        )

        return dict(warmup_magic_seal_random_option_index())

    def _warm_damage_font_index() -> dict[str, Any]:
        from game.character.damage_font_index import warmup_damage_font_index

        return dict(warmup_damage_font_index(cache))

    def _warm_customability_index() -> dict[str, Any]:
        from game.character.customability_index import warmup_customability_index

        return dict(warmup_customability_index())

    def _warm_equipment_regeneration_index() -> dict[str, Any]:
        from game.character.equipment_regeneration_index import (
            warmup_equipment_regeneration_index,
        )

        return dict(warmup_equipment_regeneration_index(cache))

    def _warm_guild_item_compound_index() -> dict[str, Any]:
        from game.character.guild_item_compound_index import (
            warmup_guild_item_compound_index,
        )

        return dict(warmup_guild_item_compound_index(cache))

    def _warm_avatar_recast_emblem_index() -> dict[str, Any]:
        from game.character.avatar_recast_emblem_index import (
            warmup_avatar_emblem_index,
        )

        return dict(warmup_avatar_emblem_index(cache))

    def _warm_avatar_compound_index() -> dict[str, Any]:
        from game.character.avatar_compound_index import (
            warmup_avatar_compound_index,
        )

        return dict(warmup_avatar_compound_index())

    def _warm_emblem_compound_config() -> dict[str, Any]:
        from game.character.emblem_compound_config import (
            warmup_emblem_compound_config,
        )

        return dict(warmup_emblem_compound_config())

    def _warm_compound_item_index() -> dict[str, Any]:
        from pvf.compound_item_index import warmup_compound_item_index

        return dict(warmup_compound_item_index())

    def _warm_enchant_by_bead_index() -> dict[str, Any]:
        from game.character.enchant_by_bead import warmup_enchant_by_bead_index

        return dict(warmup_enchant_by_bead_index(cache))

    def _warm_gm_item_catalog() -> dict[str, Any]:
        from game.character.gm_item_catalog import warmup_item_catalog_index

        # The compatibility entry delegates parsing to a child process.  GM
        # requests later restore a separate object from its validated artifact.
        return dict(warmup_item_catalog_index(cache, install_runtime=False))

    def _warm_disjoint_config() -> dict[str, Any]:
        from game.chain_loader import load_chain

        disjoint_h = load_chain("helper-disjoint_config.py")
        return dict(disjoint_h.warmup_disjoint_config())

    def _warm_upgrade_config() -> dict[str, Any]:
        from game.character.upgrade_config import warmup_upgrade_config

        return dict(warmup_upgrade_config())

    def _warm_upgrade_ticket_index() -> dict[str, Any]:
        from game.character.upgrade_ticket import warmup_upgrade_ticket_index

        return dict(warmup_upgrade_ticket_index(cache))

    def _warm_exp_bonus_consumable_index() -> dict[str, Any]:
        from game.character.exp_bonus_consumable import (
            warmup_exp_bonus_consumable_index,
        )

        return dict(warmup_exp_bonus_consumable_index(cache))

    def _warm_experience_consumable_index() -> dict[str, Any]:
        from game.character.experience_consumable import (
            warmup_experience_consumable_index,
        )

        return dict(warmup_experience_consumable_index())

    def _warm_weapon_forging_config() -> dict[str, Any]:
        from game.character.weapon_forging import warmup_weapon_forging_config

        return dict(warmup_weapon_forging_config())

    def _warm_amplify_option_config() -> dict[str, Any]:
        from game.character.amplify_option import warmup_amplify_option_config

        return dict(warmup_amplify_option_config())

    def _warm_initial_equipment() -> dict[str, Any]:
        from game.character.initial_equipment import warmup_initial_equipment_index

        return dict(warmup_initial_equipment_index())

    def _warm_hotkey_defaults() -> dict[str, Any]:
        from game.chain_loader import load_chain

        hotkey_h = load_chain("helper-hotkey_defaults.py")
        return dict(hotkey_h.warmup_hotkey_defaults())

    def _warm_skill_point_tables() -> dict[str, Any]:
        from game.chain_loader import load_chain

        points = load_chain("helper-skill_points.py")
        return dict(points.warmup_skill_point_tables())

    def _warm_offline_json() -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            from game.character.initial_skills import get_initial_skills_for_job

            warmed = 0
            for job in range(0, 16):
                if get_initial_skills_for_job(job):
                    warmed += 1
            out["skills_jobs"] = warmed
        except Exception as exc:
            out["skills_error"] = f"{type(exc).__name__}: {exc}"
        try:
            from game.quest.initial_quests import get_acceptable_quest_ids_for_job

            warmed = 0
            for job in range(0, 16):
                if get_acceptable_quest_ids_for_job(job):
                    warmed += 1
            out["acceptable_json_jobs"] = warmed
        except Exception as exc:
            out["acceptable_json_error"] = f"{type(exc).__name__}: {exc}"
        try:
            from game.town_cera_rooms import get_cera_room_for_town

            room = get_cera_room_for_town(1)
            out["town_cera_town1"] = {
                "area": getattr(room, "area_id", None),
                "pos": (getattr(room, "pos_x", None), getattr(room, "pos_y", None)),
                "source": getattr(room, "source", None),
            }
        except Exception as exc:
            out["town_cera_error"] = f"{type(exc).__name__}: {exc}"
        try:
            from game.character.character_stats import compute_combat_stats

            stats = compute_combat_stats(job=12, level=1, grow_type=0)
            out["stats_job12_lv1"] = getattr(stats, "to_dict", lambda: {})()
        except Exception as exc:
            out["stats_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def _warm_exp_tables() -> dict[str, Any]:
        from game.chain_loader import load_chain

        exp_h = load_chain("helper-character_exp.py")
        return dict(exp_h.warmup_exp_tables())

    def _warm_adventure_group_index() -> dict[str, Any]:
        from pvf.adventure_group_index import warmup_adventure_group_index

        return dict(warmup_adventure_group_index())

    def _warm_adventure_group_feature_index() -> dict[str, Any]:
        from pvf.adventure_group_feature_index import (
            warmup_adventure_group_feature_index,
        )

        return dict(warmup_adventure_group_feature_index())

    def _warm_drop_index() -> dict[str, Any]:
        from game.drop.hell_drop_config import HellDropConfig, install_hell_drop_config
        from game.drop.drop_warmup import warmup_drop_index

        semantic_version = 1
        hell_cache_status = None
        payload = cache.read("hell_drop", semantic_version)
        if payload is not None:
            try:
                install_hell_drop_config(HellDropConfig.from_cache_payload(payload))
                hell_cache_status = "hit"
            except Exception:
                cache.discard("hell_drop")
                hell_cache_status = "rebuild"
        try:
            result = dict(warmup_drop_index(cache=cache))
        except Exception:
            if hell_cache_status != "hit":
                raise
            cache.discard("hell_drop")
            hell_cache_status = "rebuild"
            result = dict(warmup_drop_index(cache=cache))
        if hell_cache_status != "hit":
            if cache.enabled:
                from game.drop.hell_drop_config import get_hell_drop_config

                cache.write(
                    "hell_drop",
                    semantic_version,
                    get_hell_drop_config().to_cache_payload(),
                    record_count=int(result.get("equipment_count") or 0),
                )
                hell_cache_status = str(
                    cache.last_status.get("hell_drop") or "miss"
                )
            else:
                hell_cache_status = "disabled"
        pool_cache_status = str(
            ((result.get("pools") or {}).get("cache") or "miss")
        )
        statuses = {pool_cache_status, str(hell_cache_status or "miss")}
        if statuses == {"hit"}:
            result["cache"] = "hit"
        elif "rebuild" in statuses:
            result["cache"] = "rebuild"
        elif statuses == {"disabled"}:
            result["cache"] = "disabled"
        else:
            result["cache"] = "miss"
        result["hell_cache"] = hell_cache_status or "miss"
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_epic_piece_index() -> dict[str, Any]:
        from pvf.epic_piece_index import warmup_epic_piece_index

        return dict(warmup_epic_piece_index())

    def _warm_titlebook_index() -> dict[str, Any]:
        from game.titlebook.titlebook_index import warmup_titlebook_index

        return dict(warmup_titlebook_index())

    def _warm_cerashop_index() -> dict[str, Any]:
        from game.shop.cerashop_index import warmup_cerashop_index
        from pvf.index_cache import install_shop_bundle_payload

        payload = cache.read("shop_bundle", shop_bundle_schema_version)
        if payload is not None:
            try:
                install_shop_bundle_payload(payload)
                shop_bundle["loaded"] = True
                shop_bundle["cache"] = "hit"
            except Exception:
                cache.discard("shop_bundle")
        if not shop_bundle["loaded"]:
            shop_bundle["write_needed"] = bool(cache.enabled)
            shop_bundle["cache"] = "disabled" if not cache.enabled else cache.last_status.get("shop_bundle", "miss")
        result = dict(warmup_cerashop_index())
        result["cache"] = shop_bundle["cache"]
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_clear_reward_index() -> dict[str, Any]:
        # Ordinary settlement cards consume the already-warmed gold/equipment
        # pools, so this must follow ``drop_index`` in the domain order.
        from game.dungeon.clear_reward import warmup_clear_reward_index

        result = dict(warmup_clear_reward_index())
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_premium_contract_index() -> dict[str, Any]:
        from game.shop.premium_contract_index import warmup_premium_contract_index

        result = dict(warmup_premium_contract_index())
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_neo_premium_rental_index() -> dict[str, Any]:
        from game.shop.neo_premium_benefits import warmup_neo_premium_rental_index

        result = dict(warmup_neo_premium_rental_index(cache))
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_cera_package_index() -> dict[str, Any]:
        from game.shop.cera_package_index import warmup_cera_package_index
        result = dict(warmup_cera_package_index())
        result["cache"] = shop_bundle["cache"]
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_booster_item_index() -> dict[str, Any]:
        from game.shop.booster_item_index import warmup_booster_item_index
        result = dict(warmup_booster_item_index())
        if shop_bundle["write_needed"]:
            from pvf.index_cache import build_shop_bundle_payload, shop_bundle_record_count

            payload = build_shop_bundle_payload()
            cache.write(
                "shop_bundle",
                shop_bundle_schema_version,
                payload,
                record_count=shop_bundle_record_count(payload),
            )
            shop_bundle["write_needed"] = False
            shop_bundle["cache"] = "disabled" if not cache.enabled else cache.last_status.get("shop_bundle", "miss")
        result["cache"] = shop_bundle["cache"]
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    def _warm_seria_luck_index() -> dict[str, Any]:
        from game.character.seria_luck_index import warmup_seria_luck_index

        return warmup_seria_luck_index()

    def _warm_random_container_index() -> dict[str, Any]:
        from game.character.random_container_index import (
            warmup_random_container_index,
        )

        result = dict(warmup_random_container_index())
        result["pvf_sha256"] = cache.identity.sha_prefix
        return result

    # These domains directly build Channel/Game state before a player can act.
    # The GM-only catalog is not part of the Win10/Win11 core startup path.
    def _warm_account_cargo_gold_caps() -> dict[str, Any]:
        from game.character.account_cargo_gold_cap import warmup_account_cargo_gold_caps

        return warmup_account_cargo_gold_caps()

    domains = [
        ("account_cargo_gold_caps", _warm_account_cargo_gold_caps),
        ("quest_index", _warm_quest_index),
        ("daily_challenge_index", _warm_daily_challenge_index),
        ("main_quest_chains", _warm_main_quest_chains),
        ("default_unlock_index", _warm_default_unlock_index),
        ("dungeon_map_index", _warm_dungeon_map_index),
        ("hell_party_index", _warm_hell_party_index),
        ("quest_rewards", _warm_quest_rewards),
        ("item_metadata", _warm_item_metadata),
        ("magic_seal_random_option_index", _warm_magic_seal_random_option_index),
        ("damage_font_index", _warm_damage_font_index),
        ("customability_index", _warm_customability_index),
        ("equipment_regeneration_index", _warm_equipment_regeneration_index),
        ("guild_item_compound_index", _warm_guild_item_compound_index),
    ]
    if warmup_profile == "full":
        domains.append(("gm_item_catalog", _warm_gm_item_catalog))
    else:
        report["deferred_domains"] = ["gm_item_catalog"]
    domains.extend([
        ("avatar_compound_index", _warm_avatar_compound_index),
        ("avatar_recast_emblem_index", _warm_avatar_recast_emblem_index),
        ("emblem_compound_config", _warm_emblem_compound_config),
    ])
    optional_domains = [
        ("compound_item_index", _warm_compound_item_index),
        ("enchant_by_bead_index", _warm_enchant_by_bead_index),
    ]
    if _env_bool("USDOF_PVF_WARMUP_OPTIONAL", False):
        domains.extend(optional_domains)
    else:
        report.setdefault("deferred_domains", []).extend(
            name for name, _fn in optional_domains
        )

    domains.extend([
        ("disjoint_config", _warm_disjoint_config),
        ("upgrade_config", _warm_upgrade_config),
        ("upgrade_ticket_index", _warm_upgrade_ticket_index),
        ("exp_bonus_consumable_index", _warm_exp_bonus_consumable_index),
        ("experience_consumable_index", _warm_experience_consumable_index),
        ("weapon_forging_config", _warm_weapon_forging_config),
        ("amplify_option_config", _warm_amplify_option_config),
        ("initial_equipment", _warm_initial_equipment),
        ("hotkey_defaults", _warm_hotkey_defaults),
        ("skill_point_tables", _warm_skill_point_tables),
        ("exp_tables", _warm_exp_tables),
        ("adventure_group_index", _warm_adventure_group_index),
        ("adventure_group_feature_index", _warm_adventure_group_feature_index),
        ("drop_index", _warm_drop_index),
        ("clear_reward_index", _warm_clear_reward_index),
        ("epic_piece_index", _warm_epic_piece_index),
        ("titlebook_index", _warm_titlebook_index),
        ("premium_contract_index", _warm_premium_contract_index),
        ("neo_premium_rental_index", _warm_neo_premium_rental_index),
        ("cerashop_index", _warm_cerashop_index),
        ("seria_luck_index", _warm_seria_luck_index),
        ("cera_package_index", _warm_cera_package_index),
        ("booster_item_index", _warm_booster_item_index),
        ("offline_json", _warm_offline_json),
    ])
    if _env_bool("USDOF_PVF_WARMUP_OPTIONAL", False):
        domains.insert(-1, ("random_container_index", _warm_random_container_index))
    else:
        report.setdefault("deferred_domains", []).append("random_container_index")

    hard_fail = False
    hard_errors: list[str] = []
    begin_text_cache = getattr(shared, "begin_warmup_text_cache", None)
    end_text_cache = getattr(shared, "end_warmup_text_cache", None)
    text_cache_started = callable(begin_text_cache) and callable(end_text_cache)
    if text_cache_started:
        begin_text_cache()
    try:
        for name, fn in domains:
            _notify_pvf_timing(
                timing_callback,
                "warmup_domain_begin",
                {"domain": name, "profile": warmup_profile},
            )
            payload = _timed(name, fn)
            report["domains"][name] = payload
            timing_payload = dict(payload)
            timing_payload["domain"] = name
            timing_payload["profile"] = warmup_profile
            _notify_pvf_timing(
                timing_callback,
                "warmup_domain_end",
                timing_payload,
            )
            if name != "offline_json":
                _notify_pvf_domain_status(status_callback, name, payload)
            if not payload.get("ok", 0):
                if name == "offline_json":
                    continue
                hard_fail = True
                hard_errors.append(f"{name}: {payload.get('error', 'unknown')}")
    finally:
        report["warmup_text_cache"] = (
            end_text_cache()
            if text_cache_started
            else {
                "enabled": 0,
                "entry_count": 0,
                "hits": 0,
                "misses": 0,
                "decoded_chars": 0,
            }
        )

    report["elapsed_ms"] = int(round((time.perf_counter() - t0) * 1000.0))
    if hard_fail:
        report["ok"] = 0
        report["error"] = "; ".join(hard_errors)
        if require_preloaded:
            raise RuntimeError(f"PVF boot warmup hard-failed: {report['error']}")
    return report


def format_warmup_report(report: dict[str, Any]) -> str:
    """One-line + multi-domain summary for server boot logs."""
    parts = [
        f"ok={int(bool(report.get('ok')))}",
        f"elapsed_ms={int(report.get('elapsed_ms') or 0)}",
    ]
    if report.get("profile"):
        parts.append(f"profile={report['profile']}")
    if report.get("pvf_sha256"):
        parts.append(f"pvf_sha256={report['pvf_sha256']}")
    if "cache_cleanup_count" in report:
        parts.append(f"cache_cleanup={int(report['cache_cleanup_count'])}")
    text_cache = report.get("warmup_text_cache") or {}
    if text_cache.get("enabled"):
        parts.append(
            "text_cache=%d_entries/%d_hits/%d_chars"
            % (
                int(text_cache.get("entry_count") or 0),
                int(text_cache.get("hits") or 0),
                int(text_cache.get("decoded_chars") or 0),
            )
        )
    deferred = tuple(report.get("deferred_domains") or ())
    if deferred:
        parts.append("deferred=" + ",".join(str(name) for name in deferred))
    domains = report.get("domains") or {}
    for name in (
        "quest_index",
        "daily_challenge_index",
        "main_quest_chains",
        "default_unlock_index",
        "dungeon_map_index",
        "hell_party_index",
        "quest_rewards",
        "item_metadata",
        "magic_seal_random_option_index",
        "damage_font_index",
        "customability_index",
        "equipment_regeneration_index",
        "guild_item_compound_index",
        "gm_item_catalog",
        "avatar_recast_emblem_index",
        "emblem_compound_config",
        "compound_item_index",
        "enchant_by_bead_index",
        "disjoint_config",
        "upgrade_config",
        "upgrade_ticket_index",
        "exp_bonus_consumable_index",
        "experience_consumable_index",
        "amplify_option_config",
        "initial_equipment",
        "hotkey_defaults",
        "skill_point_tables",
        "exp_tables",
        "adventure_group_index",
        "adventure_group_feature_index",
        "drop_index",
        "epic_piece_index",
        "premium_contract_index",
        "cerashop_index",
        "cera_package_index",
        "booster_item_index",
        "random_container_index",
        "offline_json",
    ):
        d = domains.get(name) or {}
        if not d:
            continue
        extra = []
        for key in (
            "quest_paths",
            "quest_metas",
            "group_count",
            "group_reward_count",
            "metas_fully_loaded",
            "acceptable_warmed",
            "main_quest_count",
            "main_chain_count",
            "area_quest_count",
            "area_chain_count",
            "quest_count",
            "option_count",
            "grade_count",
            "grade_weight_total",
            "part_set_option_count",
            "part_set_group_count",
            "part_set_max_point_rows",
            "worldmap_count",
            "hell_quest_count",
            "hell_chain_quest_count",
            "slot_expansion_quest_count",
            "ignored_hell_quest_count",
            "hell_dungeon_count",
            "dungeon_count",
            "license_boss_dungeon_count",
            "license_boss_maze_count",
            "license_boss_map_count",
            "license_boss_monster_code",
            "dungeon_cached",
            "dungeons_fully_loaded",
            "circle_group_count",
            "circle_entry_count",
            "map_count",
            "death_tower_apc_config_map_count",
            "death_tower_apc_definition_count",
            "hell_group_count",
            "hell_group_entries",
            "hell_map_count",
            "sections_cached",
            "sections_fully_loaded",
            "equipment_count",
            "selection_count",
            "cost_row_count",
            "candidate_count",
            "pool_count",
            "category_count",
            "excluded_count",
            "item_count",
            "damage_font_item_count",
            "font_resource_count",
            "guild_item_count",
            "medal_count",
            "guardian_gem_count",
            "medal_rarity_count",
            "guardian_gem_rarity_count",
            "scanned_equipment_count",
            "scanned_stackable_count",
            "missing_equipment_script_count",
            "missing_stackable_script_count",
            "emblem_count",
            "rarity_count",
            "ticket_count",
            "exp_bonus_consumable_count",
            "exp_bonus_minimum_level_count",
            "exp_bonus_maximum_level_count",
            "exp_bonus_job_restricted_count",
            "exp_bonus_hardcore_blocked_count",
            "experience_consumable_count",
            "skill_point_consumable_count",
            "reinforcement_ticket_count",
            "amplification_ticket_count",
            "weapon_forging_ticket_count",
            "weapon_forging_probability_ticket_count",
            "weapon_forging_additional_ticket_count",
            "probability_ticket_count",
            "additional_ticket_count",
            "protection_ticket_count",
            "reinforcement_protection_ticket_count",
            "amplification_protection_ticket_count",
            "portable_reinforcer_count",
            "portable_reinforcer_bonus_count",
            "portable_amplifier_count",
            "portable_amplifier_bonus_count",
            "rarity_0_count",
            "rarity_1_count",
            "rarity_2_count",
            "rarity_3_count",
            "rarity_6_count",
            "recipe_count",
            "stackable_count",
            "avatar_count",
            "pet_count",
            "scanned_count",
            "mapping_count",
            "effective_mapping_count",
            "candidate_count",
            "bead_count",
            "card_count",
            "invalid_bead_count",
            "table_rows",
            "cost_rows",
            "material_count",
            "pure_gold_material_count",
            "pure_gold_random_material_count",
            "pure_gold_missing_random_count",
            "amplification_const_count",
            "equip_count",
            "normal_assigned",
            "creator_assigned",
            "slot_count",
            "creator_job",
            "sp_rows",
            "tp_rows",
            "tp_source",
            "max_adventure_level",
            "adventure_exp_rate",
            "exp_threshold_rows",
            "attribute_levels",
            "stat_attribute_levels",
            "stackable_count",
            "independent_entries",
            "independent_monsters",
            "world_levels",
            "drop_prob_rows",
            "gold_levels",
            "mob_lst",
            "jobs_warmed",
            "character_lst",
            "commodity_count",
            "type_count",
            "item_alias_count",
            "minute_alias_count",
            "item_count",
            "avatar_count",
            "job_restricted_count",
            "package_count",
            "reward_item_count",
            "reward_units",
            "avatar_units",
            "main_units",
            "job_restricted_rewards",
            "booster_count",
            "booster_group_count",
            "booster_reward_item_count",
            "booster_avatar_reward_count",
            "booster_job_restricted_rewards",
            "booster_selection_count",
            "booster_selection_category_count",
            "booster_selection_reward_count",
            "cache",
            "pvf_sha256",
        ):
            if key in d:
                extra.append(f"{key}={d[key]}")
        detail = ",".join(extra)
        if detail:
            parts.append(f"{name}({d.get('elapsed_ms', 0)}ms:{detail})")
        else:
            parts.append(f"{name}({d.get('elapsed_ms', 0)}ms)")
    if report.get("error"):
        parts.append(f"error={report['error']}")
    return " ".join(parts)
