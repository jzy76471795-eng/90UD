"""Enter Seria via START map path (friend task5-fix full chain).

Enable: USDOF_GAME_ENTER_SERIA_START_MAP=1 (standard_local_flow default ON)

Friend baseline order after C1/0004 (2026-07-15 修正; 2026-07-19 装备栏时序加固):
  即时（同批 SELECT 回包）:
    S1/0004 START 262B (map_key=当前城镇 id，local-key=cid)
    S0/000D ITEMLIST list0 背包 Main（list_param16=16，Stage3 UI 档2）
    S0/000E list0 复活币钱包固定槽（每次明确刷新，含余额为 0）
    S0/000D ITEMLIST list1 时装栏 Avatar（list_param16=105，总容量 210）
    S0/000D ITEMLIST list2 个人金库 PersonalCargo（本地默认满档 200）
    S0/000D ITEMLIST list7 宠物栏 Pet（238 格，无 list_param16）
    S0/000D ITEMLIST list12 账号金库 AccountCargo（默认开通+满档 selection_key=80；屏蔽 60 级）
    S0/0131 PRIMARY_USER_OBJECT   **必须先于 USERINFO1**（type1 用 1E7F0F0(key)）
  延时队列（0131 后 delay + inter-packet；禁止与 0131 同 burst）:
    S0/0069 CREATURE empty
    S0/0016 USER_POSITION
    S0/0017 USER_AREA
    S0/0018 AREA_USERS
    S0/0002 USERINFO0（19B 外观，不建槽）
    S0/0013 SKILLINFO（标准 profile：先灌技能树，供 USERINFO1 装备合法性校验）
    S0/0002 USERINFO1（53B sub_166AED0 **建槽**）
    S0/000E list3（108B 更新装备栏；**必须在 USERINFO1 之后**）
    S0/0500 艾肯套装效果记录（逐条恢复；必须在主对象和装备槽之后）
    S0/000D ITEMLIST 普通 list36 公会勋章/守护珠全量同步（USERINFO1 + list3 之后）
    S0/01EB FATIGUE_ACCELERATION_STATE（角色活动开关持久化同步）
    S0/01E8|01E9 + S0/01EA（公会状态变更 + 活动集合）
    S0/0286 + S0/0287 DAILY_CHALLENGE（先建立当天 ledger）
    S0/0015 ACCEPTABLE_QUEST
    S0/0164 CLEAR_QUEST / S0/023F ACCEPTED_QUEST（消费已建立的 Daily ledger）
    S0/0166 TITLEBOOK×5（**带条目**；默认无 0168；回退 b6e120）
    S0/007C / S0/00AD / S0/043E ...

硬规则：USDOF_GAME_ENTER_SERIA_START_BURST_AFTER_START 在含 USERINFO/000E 时强制关闭；
否则 0131+U1+000E 同 TCP 批 → 客户端可能跳过 type1（无 0002 PIV）或 000E 先于建槽 → 装备栏空。

Gate into room is START map branch; town-sync is recommended completeness.
Login always forces current-town Seria/gate spawn from town_cera_rooms_usdof.json.

map_key 规则（2026-07-21）：
  S1/0004 @0xDA 的 map_key 必须与 login_spawn.town_id / S0/0017.town 一致。
  日常 env 默认 USDOF_GAME_ENTER_SERIA_START_MAP_KEY=0 → 自动用当前城镇。
  若写死 map_key=1（银村）而 0017 已是其它城镇赛丽亚房，客户端会先绑银村再切城镇，
  进房瞬间就播「城镇信息」切换插画；正确应先进本城赛丽亚，出门换 area 后再播。

Success signal: C->S 0x0023 near that town gate coords (SilverCrown=474,234,dir5).
"""
from __future__ import annotations

import os
import time
from typing import Any

from game.quest.initial_quests import (
    build_start_active_quest_pairs,
)
from game.quest.daily_challenge import build_daily_challenge_init_0287
from game.quest.quest_session import hydrate_quest_lists
from game.character.initial_skills import get_initial_skills_for_job
from game.character.adventure_group import (
    calculate_adventure_group_for_character,
    record_adventure_group_login,
)
from game.character.epic_book import (
    EPIC_PIECE_BALANCE_SYNC_OPCODE,
    build_account_epic_piece_balance_sync_047e,
)
from game.character.exp_bonus_consumable import (
    build_exp_bonus_item_state_restore_packets,
)
from game.character.inventory_sort_lock_ui import (
    build_inventory_sort_lock_restore_packets,
)
from game.character.inventory_equipment_lock_ui import (
    build_equipment_lock_restore_packets,
)
from game.character.avatar_closet import resolved_expansion_item_targets
from game.chain_loader import load_chain
from game.raid.local_bindings import local_raid_hooks
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.guild.guild_repository import GuildRepository
from game.character.payloads import (
    build_account_cargo_item_list,
    build_area_users,
    build_avatar_closet_set_noti,
    build_avatar_item_list_mode0,
    build_creature_item_list,
    build_daily_challenge_status_0286,
    build_game_option_defaults,
    build_guild_medal_login_full_list36,
    build_item_list_mode0,
    build_pet_item_list_mode0,
    build_primary_user_object,
    build_selector_007c_empty,
    build_skillinfo_empty,
    build_skillinfo_from_pvf,
    build_start_game_success,
    build_user_area,
    build_user_position_seria,
    build_userinfo_subtype0_min,
    build_userinfo_subtype1_min,
)
from game.town_cera_rooms import resolve_login_spawn
from game.shop.cube_contract import (
    CUBE_CONTRACT_NO_SELECTION,
    CUBE_CONTRACT_STATE_OPCODE,
    build_cube_contract_state_body,
    resolve_cube_contract_state,
)
from game.shop.cera_booster_wrapper import migrate_cera_booster_wrappers_for_character
from game.shop.cerashop_index import get_mileage_shop_index
from game.shop.mileage import (
    MILEAGE_BALANCE_OPCODE,
    MILEAGE_SHOP_EVENT_OPCODE,
    build_mileage_balance,
    build_mileage_shop_event,
)
from game.shop.premium_contract_index import get_premium_contract_index
from native_core_loader import plan_select_character

_skills_helper = load_chain("helper-character_skills.py")
resolve_skills_for_character = _skills_helper.resolve_skills_for_character
_sp_helper = load_chain("helper-skill_points.py")
resolve_skill_point_protocol_state = _sp_helper.resolve_skill_point_protocol_state
_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_exp_notification_0025 = _dungeon_payloads.build_exp_notification_0025
build_wallet_revive_coin_update_000e = (
    _dungeon_payloads.build_wallet_revive_coin_update_000e
)
_activity_state = load_chain("helper-activity_state.py")
_quest_completion = load_chain("helper-quest_completion_snapshot.py")
_tower_item_cleanup = load_chain("helper-death_tower_item_cleanup.py")
_customability_set_helper = load_chain("helper-customability_set.py")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def resolve_saved_character_option_body(repo: Any, character_id: int):
    """Return a real saved S0/0187 body, never a synthetic empty fixture."""
    cid = int(character_id or 0)
    if cid <= 0:
        return None, "no_character_id"
    try:
        blob = repo.get_character_option_blob(cid)
    except Exception as exc:
        return None, "load_failed=%s:%s" % (type(exc).__name__, exc)
    if not blob:
        return None, "no_saved_blob cid=%s" % cid
    body = load_chain(
        "opcode01c0-save_character_option.py"
    ).build_character_option_noti_body(blob)
    return body, "character_option_blob len=%s cid=%s" % (len(blob), cid)


class _StartCharView:
    def __init__(self, selected: Any) -> None:
        self.character_id = int(selected.character_id)
        self.account_id = int(getattr(selected, "account_id", 0) or 0)
        self.slot = int(getattr(selected, "slot_index", 0) or 0)
        self.name_bytes = bytes(getattr(selected, "name_bytes", b"") or b"")
        if not self.name_bytes:
            name = str(getattr(selected, "display_name", "") or getattr(selected, "name", "") or "")
            self.name_bytes = name.encode("utf-8", errors="replace")
        self.job = int(getattr(selected, "job", 0) or 0)
        self.grow_type = int(getattr(selected, "grow_type", 0) or 0)
        # USERINFO0 and S0/0165 are built from this snapshot after a town
        # re-entry; carry the persisted render flags instead of falling back
        # to the default when the temporary view is used.
        self.visibility_bits = int(
            getattr(selected, "visibility_bits", 3) or 0
        ) & 0xFF
        self.level = max(1, int(getattr(selected, "level", 1) or 1))
        # characters.exp；缺省 0（旧 CharacterRecord 无字段时由 start 路径再从 DB 补）
        self.exp = int(getattr(selected, "exp", 0) or 0) & 0xFFFFFFFF
        self.expertise_exp = max(
            0,
            int(getattr(selected, "expertise_exp", 0) or 0),
        )
        # USERINFO1 uses this byte to recreate the auxiliary/magic-stone/earring slots.
        self.ex_equip_slot_stat = int(
            getattr(selected, "ex_equip_slot_stat", 0) or 0
        ) & 0xFF
        self.town_id = int(getattr(selected, "town_id", 1) or 1)
        self.area_id = int(getattr(selected, "area_id", 0) or 0)
        self.pos_x = int(getattr(selected, "pos_x", 474) or 474)
        self.pos_y = int(getattr(selected, "pos_y", 234) or 234)
        self.direction = int(getattr(selected, "direction", 5) or 5)
        self.area_state = int(getattr(selected, "area_state", 3) or 3)
        self.equipped_entries = tuple(
            getattr(selected, "equipped_entries", ()) or ()
        )
        self.equipped_creature_level = max(
            0,
            min(255, int(getattr(selected, "equipped_creature_level", 0) or 0)),
        )


class EnterSeriaStartMapService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def enabled(self) -> bool:
        return _env_bool("USDOF_GAME_ENTER_SERIA_START_MAP", False)

    def handle_select_character(
        self,
        body: bytes,
        *,
        session: Any,
        select_character_by_slot,
        native_branch: bool = False,
    ) -> tuple[list[bytes], str] | None:
        if not self.enabled():
            return None

        requested_slot = 0
        parse_source = "default-empty-body"
        native_selected_index = -1
        native_selection_kind = 0
        native_plan_note = ""
        if not native_branch and len(body) >= 2:
            candidates = [body]
            if len(body) > 3:
                candidates.append(body[3:])
            for index, candidate in enumerate(candidates):
                if len(candidate) >= 2:
                    requested_slot = int.from_bytes(candidate[:2], "little") & 0xFFFF
                    parse_source = "body" if index == 0 else "body_minus_reserved3"
                    break

        repo = self.server._repo()
        characters = repo.list_by_account(self.server.account_id)
        if native_branch:
            (
                requested_slot,
                native_parse_source,
                native_selected_index,
                native_selection_kind,
            ) = plan_select_character(
                body,
                tuple(range(len(characters))),
            )
            parse_source = "body" if native_parse_source == 1 else "default-empty-body"
            native_plan_note = (
                "; native_select_plan requested=%d selected_index=%d kind=%d"
                % (requested_slot, native_selected_index, native_selection_kind)
            )
        if not characters:
            clear_waiting_position = getattr(session, "clear_anton_waiting_position", None)
            if callable(clear_waiting_position):
                clear_waiting_position()
            session.selected_character_id = None
            session.selected_slot = None
            session.selected_character_name = None
            session.dungeon_wish_item_ids = ()
            session.enter_game_pending = True
            session.last_select_character_at = time.time()
            return [], (
                "START_MAP path: SELECT_CHARACTER but no DB characters; "
                f"requested_slot={requested_slot} source={parse_source}; enter_game_pending=1; "
                "chain_file=game/chains/flow-start_map_enter.py"
                f"{native_plan_note}"
            )

        selected = (
            characters[native_selected_index]
            if native_branch and 0 <= native_selected_index < len(characters)
            else select_character_by_slot(characters, requested_slot)
        )
        if native_branch and native_selection_kind == 3:
            fallback_note = (
                f"requested_slot={requested_slot} not found/out-of-range; "
                f"fallback_slot={selected.slot_index}"
            )
        elif selected is None:
            selected = characters[0]
            fallback_note = (
                f"requested_slot={requested_slot} not found/out-of-range; "
                f"fallback_slot={selected.slot_index}"
            )
        else:
            fallback_note = "slot matched DB roster"

        try:
            tower_item_cleanup_note = _tower_item_cleanup.cleanup_death_tower_items(
                repo, int(selected.character_id)
            ).note()
        except Exception as cleanup_exc:
            tower_item_cleanup_note = "death_tower_temp_items_cleanup=error %r" % (cleanup_exc,)

        resolved_slot = int(selected.slot_index)
        session.bind_selected_character(
            character_id=selected.character_id,
            slot=resolved_slot,
            name=selected.display_name,
        )
        wish_ids: tuple[int, ...] = ()
        wish_hydrated = False
        wish_note = "wish=skip"
        try:
            from game.drop.wish_service import hydrate_session_wish_items

            wish_ids = hydrate_session_wish_items(
                self.server,
                session,
                character_id=int(selected.character_id),
            )
            wish_hydrated = True
            wish_note = "wish_items=%s" % (
                ",".join(str(item_id) for item_id in wish_ids) or "-"
            )
        except Exception as wish_exc:
            wish_note = "wish_hydrate_error=%s" % type(wish_exc).__name__
        tutorial_state = int(getattr(selected, "tutorial_state", 0) or 0) & 0xFF
        # Phase-T0：仅 arm；Phase-T1 在 START 角色包 delayed 末尾（默认）或 C1/0023 fallback。
        # 禁止 START delayed 与城镇 paint(0016/0017/0018) 同批硬塞 001C+001D。
        newbie_first_enter_env = _env_bool("USDOF_GAME_NEWBIE_FIRST_ENTER", False)
        _tut_helper = load_chain("helper-tutorial_dungeon.py")
        _tut_flow = load_chain("flow-tutorial_first_enter.py")
        tutorial_enter = _tut_helper.should_start_tutorial_dungeon(
            tutorial_state=tutorial_state,
            level=int(getattr(selected, "level", 1) or 1),
            env_first_enter=newbie_first_enter_env,
        )
        tutorial_dungeon_id = 0
        tutorial_arm_note = "tutorial_arm=skip"
        tutorial_skip_room = False
        if tutorial_enter:
            tutorial_dungeon_id = int(
                _tut_helper.tutorial_dungeon_id_for_job(int(getattr(selected, "job", 0) or 0))
            )
            # town_id 稍后 login_spawn 再钉死；此处先用角色存档城镇
            arm_town = int(getattr(selected, "town_id", 1) or 1)
            if arm_town <= 0:
                arm_town = 1
            tutorial_arm_note = _tut_flow.arm_tutorial_on_start(
                session,
                dungeon_id=tutorial_dungeon_id,
                character_id=int(selected.character_id),
                job=int(getattr(selected, "job", 0) or 0),
                town_id=arm_town,
            )
            tutorial_skip_room = bool(
                _tut_flow.skip_seria_room_paint()
                and not _tut_flow.requires_town_ready(session)
            )

        view = _StartCharView(selected)
        # ``list_by_account`` already returned one transactionally consistent
        # character row.  Keep that record as the authority for every packet
        # which creates the client-side main object; re-reading individual
        # columns later can mix two different character states in one enter
        # sequence.
        character_id = int(view.character_id)
        character_job = int(view.job) & 0xFF
        character_grow = int(view.grow_type) & 0xFF
        character_level = max(1, int(view.level))
        character_exp = int(view.exp) & 0xFFFFFFFF
        character_expertise_exp = max(0, int(view.expertise_exp))
        try:
            _prepared_sets, customability_prepare_note = (
                _customability_set_helper.prepare_customability_set_records(
                    repo,
                    character_id,
                )
            )
        except Exception as customability_prepare_exc:
            customability_prepare_note = "customability_set_prepare_fail:%s:%s" % (
                type(customability_prepare_exc).__name__,
                customability_prepare_exc,
            )
        try:
            adventure_login = record_adventure_group_login(
                repo,
                int(view.account_id),
            )
            adventure_login_note = "adventure_login day=%d streak=%d" % (
                int(adventure_login.get("last_login_date", 0) or 0),
                int(adventure_login.get("consecutive_login_days", 0) or 0),
            )
        except Exception as adventure_login_exc:
            adventure_login_note = "adventure_login err=%s:%s" % (
                type(adventure_login_exc).__name__,
                adventure_login_exc,
            )
        account_cera = repo.get_account_cera(int(view.account_id))
        account_mileage = repo.get_account_mileage(int(view.account_id))
        premium_index = get_premium_contract_index()
        premium_migration = repo.activate_premium_items_from_inventory(
            int(view.account_id),
            premium_index.activation_map(),
        )
        if premium_migration.get("error"):
            raise RuntimeError(
                "legacy premium inventory activation failed: %s"
                % premium_migration["error"]
            )
        cera_booster_migration = migrate_cera_booster_wrappers_for_character(
            repo,
            account_id=int(view.account_id),
            character_id=character_id,
            now_unix=int(time.time()),
        )
        system_reward_migration = repo.activate_system_reward_items_from_inventory(
            int(view.account_id),
            now_unix=int(time.time()),
        )
        if system_reward_migration.get("error"):
            raise RuntimeError(
                "legacy system reward inventory activation failed: %s"
                % system_reward_migration["error"]
            )
        active_premium_rows = repo.list_active_account_premiums(int(view.account_id))
        account_premiums = [
            (
                int(row["premium_type"]),
                int(row["remaining_seconds"]),
            )
            for row in active_premium_rows
        ]
        try:
            session.account_premium_effects = None
        except Exception:
            pass
        premium_note = "premiums=n=%d types=%s migrated_rows=%d migrated_items=%d" % (
            len(account_premiums),
            ",".join(str(row[0]) for row in account_premiums) or "-",
            int(premium_migration.get("consumed_rows", 0) or 0),
            int(premium_migration.get("consumed_items", 0) or 0),
        )
        if cera_booster_migration.get("error"):
            cera_booster_note = "cera_booster_migration_error=%s" % (
                cera_booster_migration["error"],
            )
        else:
            cera_booster_note = (
                "cera_booster_migrated_rows=%d migrated_items=%d skipped=%d"
                % (
                    int(cera_booster_migration.get("migrated_rows", 0) or 0),
                    int(cera_booster_migration.get("migrated_items", 0) or 0),
                    len(cera_booster_migration.get("skipped", ()) or ()),
                )
            )
        system_reward_note = (
            "system_reward_migrated_rows=%d migrated_items=%d"
            % (
                int(system_reward_migration.get("consumed_rows", 0) or 0),
                int(system_reward_migration.get("consumed_items", 0) or 0),
            )
        )
        cube_contract_body = build_cube_contract_state_body(
            CUBE_CONTRACT_NO_SELECTION
        )
        cube_contract_note = "cube_contract active=0 stored=ff wire=ff"
        try:
            cube_contract_state = resolve_cube_contract_state(
                repo,
                account_id=int(view.account_id),
                character_id=character_id,
                active_premium_rows=active_premium_rows,
            )
            cube_contract_body = cube_contract_state.to_body()
            cube_contract_note = (
                "cube_contract active=%d stored=%02x wire=%02x"
                % (
                    int(cube_contract_state.active),
                    cube_contract_state.stored_selection,
                    cube_contract_state.wire_selection,
                )
            )
        except Exception as cube_contract_exc:
            cube_contract_note = "cube_contract load_fail=%s:%s wire=ff" % (
                type(cube_contract_exc).__name__,
                cube_contract_exc,
            )
        premium_note += (
            "; "
            + cera_booster_note
            + "; "
            + system_reward_note
            + "; "
            + cube_contract_note
        )

        fatigue_burning_enabled = False
        fatigue_burning_note = "fatigue_burning state=0"
        try:
            fatigue_burning_enabled = _activity_state.hydrate_fatigue_burning_state(
                repo,
                character_id,
                session=session,
            )
            fatigue_burning_note = "fatigue_burning state=%d" % int(
                fatigue_burning_enabled
            )
        except Exception as fatigue_burning_exc:
            fatigue_burning_note = "fatigue_burning load_fail=%s:%s state=0" % (
                type(fatigue_burning_exc).__name__,
                fatigue_burning_exc,
            )
        fatigue_burning_body = _activity_state.build_fatigue_burning_state_body(
            fatigue_burning_enabled
        )

        # Login spawn 必须先于 START 组包：map_key 与 0017/0018 共用同一城镇。
        # 包括安徒恩团本频道在内，只发既有登录规则解析的赛丽亚房。
        # 无赛丽亚城镇或无效数据沿用既有替代赛丽亚规则，副本快照也遵循该规则。
        saved_town_id = int(getattr(selected, "town_id", 1) or 1)
        if saved_town_id <= 0:
            saved_town_id = 1
        force_town = _env_int("USDOF_GAME_LOGIN_FORCE_TOWN_ID", 0)
        anton_waiting_spawn = (
            _env_bool("USDOF_RESTORE_ANTON_WAITING_SPAWN", False)
            and _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
            and type(getattr(session, "selected_channel_id", None)) is int
            and session.selected_channel_id == 23
            and not tutorial_enter
        )
        luke_waiting_spawn = (
            _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
            and (_env_bool("USDOF_RAID_LOBBY", False) or _env_bool("USDOF_RAID_LOCAL", False))
            and type(getattr(session, "selected_channel_id", None)) is int
            and session.selected_channel_id == 32
            and not tutorial_enter
        )
        raid_waiting_spawn = anton_waiting_spawn or luke_waiting_spawn
        dungeon_recovery = None
        recovery_note = "dungeon_login_recovery=none"
        if luke_waiting_spawn:
            if force_town > 0 or _env_int("USDOF_GAME_ENTER_SERIA_START_MAP_KEY", 0) > 0:
                raise ValueError("Luke waiting spawn conflicts with positive FORCE_TOWN_ID or START_MAP_KEY")
            if type(selected.character_id) is not int or selected.character_id <= 0:
                raise ValueError("Luke waiting spawn requires a positive integer character_id")
            from game.raid.bootstrap import runtime_data
            from game.raid.landing import resolve_raid_login_spawn

            spawn = resolve_raid_login_spawn(session, runtime_data().landings)
            if spawn is None:
                raise ValueError("Luke channel requires its validated raid landing")
            recovery_note = "dungeon_login_recovery=skip_luke_waiting"
        elif anton_waiting_spawn:
            if force_town > 0 or _env_int("USDOF_GAME_ENTER_SERIA_START_MAP_KEY", 0) > 0:
                raise ValueError(
                    "Anton waiting spawn conflicts with positive FORCE_TOWN_ID or START_MAP_KEY"
                )
            if type(selected.character_id) is not int or selected.character_id <= 0:
                raise ValueError("Anton waiting spawn requires a positive integer character_id")
            # Retain the raid channel and its isolated position ownership, but
            # use the raid town's Seria room verified in the current PVF.
            spawn = resolve_login_spawn(town_id=19)
            if (
                spawn.town_id != 19
                or spawn.area_id != 0
                or spawn.file_name.lower() != "battleship_raid.twn"
            ):
                raise ValueError(
                    "Anton Seria spawn requires town 19 area 0 BattleShip_Raid.twn"
                )
            recovery_note = "dungeon_login_recovery=skip_anton_seria"
        else:
            if force_town <= 0 and hasattr(repo, "get_dungeon_login_recovery"):
                try:
                    dungeon_recovery = repo.get_dungeon_login_recovery(int(selected.character_id))
                except Exception as exc:
                    recovery_note = "dungeon_login_recovery=read_fail err=%r" % (exc,)
            if dungeon_recovery:
                spawn = resolve_login_spawn(
                    town_id=int(dungeon_recovery["town_id"]),
                )
                recovery_note = "dungeon_login_recovery=use_gate_spawn"
            else:
                spawn = resolve_login_spawn(
                    town_id=saved_town_id,
                    force_town_id=force_town if force_town > 0 else None,
                )
        town_id = int(spawn.town_id) & 0xFF
        area_id = int(spawn.area_id) & 0xFF
        pos_x = int(spawn.pos_x)
        pos_y = int(spawn.pos_y)
        direction = int(spawn.direction) & 0xFF
        area_state = int(spawn.area_state) & 0xFF
        cid_u16 = int(view.character_id) & 0xFFFF

        if session is not None:
            session.cur_town_id = town_id
            session.cur_area_id = area_id
            session.cur_pos_x = pos_x
            session.cur_pos_y = pos_y
            session.cur_direction = direction
            session.cur_area_state = area_state
            if anton_waiting_spawn:
                session.anton_waiting_position_character_id = selected.character_id
            if luke_waiting_spawn:
                session.luke_waiting_position_character_id = selected.character_id

        persist_note = "login_spawn_persist=skip"
        if not raid_waiting_spawn and _env_bool("USDOF_GAME_LOGIN_SPAWN_PERSIST", True):
            try:
                ok = repo.update_position(
                    int(selected.character_id),
                    town_id=town_id,
                    area_id=area_id,
                    pos_x=pos_x,
                    pos_y=pos_y,
                    direction=direction,
                    area_state=area_state,
                )
                persist_note = (
                    "login_spawn_persist=%s town=%s area=%s pos=(%s,%s) src=%s file=%s"
                    % (
                        "ok" if ok else "miss",
                        town_id,
                        area_id,
                        pos_x,
                        pos_y,
                        spawn.source,
                        spawn.file_name or "-",
                    )
                )
                if dungeon_recovery and ok and hasattr(repo, "clear_dungeon_login_recovery"):
                    repo.clear_dungeon_login_recovery(int(selected.character_id))
                    recovery_note = "dungeon_login_recovery=used_and_cleared"
            except Exception as exc:
                persist_note = "login_spawn_persist_fail err=%r" % (exc,)

        # map_key=0（日常默认）→ 跟随 login_spawn.town_id；>0 仅 A/B 强制。
        map_key_env = _env_int("USDOF_GAME_ENTER_SERIA_START_MAP_KEY", 0)
        if map_key_env > 0:
            map_key = map_key_env & 0xFF
            map_key_src = "env_force"
        else:
            map_key = town_id if town_id > 0 else 1
            map_key_src = "login_spawn_town"
        map_key &= 0xFF

        body_len = max(226, _env_int("USDOF_GAME_ENTER_SERIA_START_BODY_LEN", 262))
        n2_8 = int(view.character_id) & 0xFFFF
        if _env_int("USDOF_GAME_ENTER_SERIA_START_N2_8", -1) >= 0:
            n2_8 = _env_int("USDOF_GAME_ENTER_SERIA_START_N2_8", n2_8) & 0xFFFF

        # START 任务段（IDA sub_1603250）：
        #   - 30 pairs @22：非 0xFFFF 会 sub_1394650 注册进行中（新号必须全 FFFF）
        #   - 4 display_ids @202：sub_1384900 → sub_1384700 写入右侧追踪栏（最多 4）
        # 右侧/底部可交闪烁依赖 display_ids 先挂上 tracker；仅 023F+S1/0021 不够
        # （023F 只灌列表/F2，不走 quest_mark）。
        # 已有 DB 已接任务：从 DB 填 pairs + display（可交 trigger==0 优先）。
        # 新号无已接：保持 FFFF / 全 0。JSON 种子仅调试 env。
        active_quests = None
        quest_display_ids = None
        start_pair_note = "start_pairs=FFFF"
        db_active_pairs: list[tuple[int, int]] = []
        try:
            rows = self.server._repo().list_active_quests(int(view.character_id))
            db_active_pairs = [
                (int(q) & 0xFFFF, int(t) & 0xFFFFFFFF)
                for _s, q, t in rows
                if (int(q) & 0xFFFF) > 0
            ]
        except Exception as exc:
            start_pair_note = "start_pairs=FFFF(db_fail err=%r)" % (exc,)

        if db_active_pairs and _env_bool(
            "USDOF_GAME_ENTER_SERIA_START_QUEST_PAIRS_FROM_DB", True
        ):
            active_quests = list(db_active_pairs[:30])
            start_pair_note = "start_pairs=db n=%s sample=%s" % (
                len(active_quests),
                ",".join("%s:%s" % (q, t) for q, t in active_quests[:4]),
            )
        elif _env_bool("USDOF_GAME_ENTER_SERIA_START_QUEST_PAIRS_FROM_JSON", False):
            max_pairs = _env_int("USDOF_GAME_ENTER_SERIA_START_QUEST_PAIR_MAX", 0)
            if max_pairs > 0:
                pairs = build_start_active_quest_pairs(
                    int(getattr(view, "job", 0) or 0),
                    max_pairs=max_pairs,
                    trigger=0,
                )
                if pairs:
                    active_quests = pairs
                    start_pair_note = "start_pairs=json %s" % (
                        ",".join("%s:0" % qid for qid, _tr in pairs)
                    )
                else:
                    start_pair_note = "start_pairs=FFFF(no_roots)"
            else:
                start_pair_note = "start_pairs=FFFF(max=0)"

        if db_active_pairs and _env_bool(
            "USDOF_GAME_ENTER_SERIA_START_QUEST_DISPLAY_FROM_DB", True
        ):
            # 可交（trigger==0）优先进右侧 4 槽，再补进行中
            zeros = [q for q, t in db_active_pairs if int(t) == 0]
            rest = [q for q, t in db_active_pairs if int(t) != 0]
            seen: set[int] = set()
            ordered: list[int] = []
            for q in zeros + rest:
                if q in seen:
                    continue
                seen.add(q)
                ordered.append(q)
                if len(ordered) >= 4:
                    break
            quest_display_ids = ordered
            start_pair_note += "; display_ids=%s" % (
                ",".join(str(x) for x in quest_display_ids) if quest_display_ids else "-"
            )
        else:
            start_pair_note += "; display_ids=0"

        # UI 指引 flag 回灌（商城/选图）：角色 ∪ 账号；空列表 = 全未看过
        tutorial_flags: list[int] = []
        tutorial_flags_note = "tutorial_flags=0"
        try:
            account_id = int(
                getattr(session, "account_id", None)
                or getattr(self.server, "account_id", 1)
                or 1
            )
            tutorial_flags = list(
                repo.merge_tutorial_flags_for_start(
                    character_id=int(view.character_id),
                    account_id=account_id,
                )
            )
            tutorial_flags_note = "tutorial_flags=n=%s sample=%s" % (
                len(tutorial_flags),
                ",".join(str(x) for x in tutorial_flags[:12]) if tutorial_flags else "-",
            )
        except Exception as tut_flag_exc:
            tutorial_flags = []
            tutorial_flags_note = "tutorial_flags=err:%r" % (tut_flag_exc,)

        if not newbie_first_enter_env:
            # Native 01074850 refuses to create the first-entry controller
            # (01071120, guide 31) when this START flag is already set.
            # Project only: preserve saved flags/quests so enabling tutorials
            # explicitly for diagnostics does not require a database reset.
            if 31 not in tutorial_flags:
                tutorial_flags.append(31)
            tutorial_flags_note += "; first_tutorial=disabled flag31=1"

        from game.character.fatigue_flow import login_balance
        fatigue_balance = login_balance(repo, int(view.character_id))
        start_clock_sample = time.time()
        start_body = build_start_game_success(
            character_id=int(view.character_id),
            map_key=map_key,
            n2_8=n2_8,
            v127=int(start_clock_sample),
            v123=fatigue_balance.used,
            n2_6=fatigue_balance.limit,
            account_cera=account_cera,
            account_premiums=account_premiums,
            body_length=body_len,
            active_quests=active_quests,
            quest_display_ids=quest_display_ids,
            tutorial_flags=tutorial_flags,
        )
        start_flag = _env_int("USDOF_GAME_ENTER_SERIA_START_FLAG", 1) & 0xFF
        start_mode = os.environ.get("USDOF_GAME_ENTER_SERIA_START_MODE", "codec").strip().lower()
        if start_mode == "raw":
            start_packet = self.server._raw_response(start_flag, 0x0004, start_body)
        else:
            start_packet = self.server._codec_response(start_flag, 0x0004, start_body)

        if start_flag == 1:
            from game.raid.native_clock import track_start_clock
            track_start_clock(session, start_packet, unix_seconds=int(start_clock_sample),
                              server_sample=start_clock_sample)

        send_itemlist = _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_ITEMLIST", True)
        guild_medal_login_full_sync: tuple[bytes, str] | None = None
        packets: list[bytes] = [
            start_packet,
            self.server._codec_response(0, MILEAGE_BALANCE_OPCODE, build_mileage_balance(account_mileage)),
        ]
        mileage_enabled = bool(get_mileage_shop_index())
        local_raid_features = _env_bool("USDOF_LOCAL_RAID_FEATURES", False)
        if mileage_enabled and not local_raid_features:
            packets.append(self.server._codec_response(0, MILEAGE_SHOP_EVENT_OPCODE, build_mileage_shop_event()))
        notes = [
            f"START_MAP enabled; S{start_flag}/0004 start_game_success plain_len={len(start_body)} "
            f"map_key={map_key} map_key_src={map_key_src} n2_8={n2_8} cid={view.character_id} "
            f"account_cera={account_cera} account_mileage={account_mileage} "
            f"{premium_note} mode={start_mode} {start_pair_note}; "
            f"tutorial_state={tutorial_state} "
            f"newbie_first_enter_env={int(newbie_first_enter_env)} "
            f"tutorial_enter={int(tutorial_enter)} tutorial_dungeon={tutorial_dungeon_id} "
            f"{tutorial_arm_note}; {tutorial_flags_note}; {adventure_login_note}; "
            f"{tower_item_cleanup_note}; "
            f"path={'START_MAP+TUTORIAL_ARM' if tutorial_enter else 'START_MAP_seria'}; "
            "login_spawn town=%s area=%s pos=(%s,%s) dir=%s state=%s src=%s saved_town=%s %s %s"
            % (
                town_id,
                area_id,
                pos_x,
                pos_y,
                direction,
                area_state,
                spawn.source,
                saved_town_id,
                persist_note,
                recovery_note,
            )
        ]
        notes.append(customability_prepare_note)

        if send_itemlist:
            # 进房物品同步：普通容器走 000D；公会栏的全局 list36 要在
            # USERINFO 发布容器后填充，因此放到完整角色初始化之后。
            #   list_type=0 背包 Main（list_param16=扩展量；Stage3 满展=16=档2，见 IDA）
            #   list_type=1 时装栏 Avatar（list_param16=扩展量）
            #   list_type=2 个人金库 PersonalCargo（list_param16=槽数；基线满档 200）
            #   list_type=36 公会勋章/守护珠（普通 000D：u8 type + u16 count）
            #   list_type=12 账号金库 AccountCargo（另形 body：selection_key+gold+count）
            #
            # 对照 S4A12 ItemListPacketBuilder：进房发 Main/Avatar/PersonalCargo/AccountCargo，
            # 不发 list3；Stage3 IDA sub_1666D80 对 list1 每条读取
            # 108B 固定体和两个 length-prefixed blob。
            # 本服：背包 16 + 个人仓 200 + 账号仓 key=80；屏蔽 PVF 60 级门槛。
            #
            # 装备栏 S0/000E list_type=3 **禁止**放在 USERINFO1/166AED0 之前：
            # Stage3 IDA loc_1667BD0：list_type==3 走角色对象 vtable+0xAC0(slot)
            # 取已有装备槽对象再写 108B；槽由 USERINFO1 sub_166AED0 创建（非 type0）。
            # 过早发 000E 时 slot 对象为空 → 静默跳过 → Inventory 装备槽空。
            # 装备栏同步见 delayed：USERINFO1 之后。
            try:
                # Repair historical wrong-tab grants / bad equipment seeds before ITEMLIST publish.
                try:
                    moved, migrate_note = repo.migrate_misplaced_inventory_items(int(view.character_id))
                    if moved:
                        notes.append(f"inventory_migrate moved={moved} {migrate_note}")
                    material_ok, material_note, material_detail = (
                        repo.consolidate_guild_medal_contribution_stacks(
                            int(view.character_id)
                        )
                    )
                    if not material_ok:
                        notes.append(
                            f"guild_medal_material_consolidate_fail:{material_note}"
                        )
                    elif int(material_detail.get("merged_stack_count") or 0) > 0:
                        notes.append(material_note)
                    fixed, dura_note = repo.repair_zero_durability_equipment(int(view.character_id))
                    if fixed:
                        notes.append(f"inventory_dura_fix fixed={fixed} {dura_note}")
                    iv_fixed, iv_note = repo.repair_equipment_instance_values(int(view.character_id))
                    if iv_fixed:
                        notes.append(f"inventory_iv_fix fixed={iv_fixed} {iv_note}")
                    raw_fixed, raw_note = repo.repair_equipped_raw_entries(int(view.character_id))
                    if raw_fixed:
                        notes.append(f"equip_raw_fix fixed={raw_fixed} {raw_note}")
                    closet_targets = resolved_expansion_item_targets()
                    closet_migrated, closet_note, _closet_detail = (
                        repo.migrate_avatar_closet_expansion_items(
                            int(view.character_id),
                            closet_targets,
                        )
                    )
                    if closet_migrated:
                        notes.append(closet_note)
                except Exception as migrate_exc:
                    notes.append(f"inventory_migrate_fail:{migrate_exc}")
            except Exception as pre_exc:
                notes.append(f"itemlist_pre_fail:{pre_exc}")

            char_level = int(getattr(view, "level", 0) or 0)
            if char_level <= 0:
                try:
                    with repo.open() as _lv_conn:
                        _lv_row = _lv_conn.execute(
                            "SELECT level FROM characters "
                            "WHERE character_id = ? AND delete_flag = 0",
                            (int(view.character_id),),
                        ).fetchone()
                    char_level = int(_lv_row[0] or 0) if _lv_row else 0
                except Exception:
                    char_level = 0

            # 强制抬底：背包 16 + 个人仓 152 + 账号仓 key=80（屏蔽 60 级）
            try:
                ok_seed, seed_note = repo.ensure_container_state(
                    int(view.character_id),
                    character_level=char_level,
                )
                notes.append(f"container_seed ok={int(ok_seed)} {seed_note}")
            except Exception as seed_exc:
                notes.append(f"container_seed_fail:{seed_exc}")

            # list0 必发；list1 时装栏、list33 衣柜和 list2 个人金库默认开。
            send_avatar = _env_bool(
                "USDOF_GAME_ENTER_SERIA_START_SEND_AVATAR_ITEMLIST", True
            )
            send_cargo = _env_bool(
                "USDOF_GAME_ENTER_SERIA_START_SEND_CARGO_ITEMLIST", True
            )
            list_types_to_send = [0]
            if send_avatar:
                list_types_to_send.extend((1, repo.INVENTORY_LIST_AVATAR_CLOSET))
            if send_cargo:
                list_types_to_send.append(2)
            if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_PET_ITEMLIST", True):
                list_types_to_send.append(7)
            list_types_to_send.append(repo.INVENTORY_LIST_GUILD_MEDAL)

            def _rows_from_raw(raw_items: list) -> list:
                from game.character.inventory_item_view import InventoryItemView

                return [InventoryItemView(row) for row in raw_items]

            for lt in list_types_to_send:
                item_rows = []
                list_param16 = 0
                try:
                    if lt not in (
                        repo.INVENTORY_LIST_AVATAR_CLOSET,
                        repo.INVENTORY_LIST_GUILD_MEDAL,
                    ):
                        list_param16 = int(
                            repo.get_container_list_param(
                                int(view.character_id),
                                list_type=lt,
                            )
                        )
                    raw_items = repo.list_inventory_items(
                        int(view.character_id), list_type=lt
                    )
                    # 已解封装备：extra_json.random_options → 000D RO tail
                    # 否则进房仍显示「魔法封印」
                    item_rows = _rows_from_raw(raw_items)
                except Exception as list_exc:
                    item_rows = []
                    list_param16 = 0
                    notes.append(f"itemlist_list{lt}_load_fail:{list_exc}")
                if lt in (1, repo.INVENTORY_LIST_AVATAR_CLOSET):
                    itemlist_body = build_avatar_item_list_mode0(
                        item_rows,
                        list_type=lt,
                        list_param16=list_param16,
                    )
                    itemlist_mode = "avatar_itemlist_mode0"
                elif lt == 7:
                    itemlist_body = build_pet_item_list_mode0(item_rows)
                    itemlist_mode = "pet_itemlist_mode0"
                elif lt == repo.INVENTORY_LIST_GUILD_MEDAL:
                    itemlist_body = build_guild_medal_login_full_list36(item_rows)
                    itemlist_mode = "guild_medal_full_snapshot_plain_list36"
                else:
                    itemlist_body = build_item_list_mode0(
                        item_rows,
                        list_type=lt,
                        list_param16=list_param16,
                    )
                    itemlist_mode = "itemlist_mode0"
                itemlist_packet = self.server._codec_response(0, 0x000D, itemlist_body)
                itemlist_note = (
                    f"S0/000D {itemlist_mode} list{lt} n={len(item_rows)} "
                    f"list_param16={list_param16} plain_len={len(itemlist_body)}"
                )
                if lt == repo.INVENTORY_LIST_GUILD_MEDAL:
                    guild_medal_login_full_sync = (
                        itemlist_packet,
                        "start-map delayed %s after_USERINFO1_166aed0_list3"
                        % itemlist_note,
                    )
                    notes.append("%s queued_after_userinfo1_list3" % itemlist_note)
                else:
                    packets.append(itemlist_packet)
                    notes.append(itemlist_note)
                if lt in (0, 1):
                    try:
                        restore_packets, restore_note = (
                            build_inventory_sort_lock_restore_packets(
                                self.server,
                                repo,
                                character_id=int(view.character_id),
                                list_type=lt,
                            )
                        )
                        packets.extend(restore_packets)
                        notes.append(restore_note)
                    except Exception as lock_exc:
                        notes.append(
                            "sort_lock_restore_fail:%s:%s"
                            % (type(lock_exc).__name__, lock_exc)
                        )
                if lt in (0, 1):
                    try:
                        equipment_restore_packets, equipment_restore_note = (
                            build_equipment_lock_restore_packets(
                                self.server,
                                repo,
                                character_id=int(view.character_id),
                                list_types=(lt,),
                            )
                        )
                        packets.extend(equipment_restore_packets)
                        notes.append(equipment_restore_note)
                    except Exception as lock_exc:
                        notes.append(
                            "equipment_lock_restore_fail:%s:%s"
                            % (type(lock_exc).__name__, lock_exc)
                        )
                if lt == 0:
                    try:
                        wallet_ok, wallet_note, wallet = repo.get_account_wallet(
                            int(view.character_id)
                        )
                        if not wallet_ok:
                            raise ValueError(wallet_note)
                        revive_coin_total = int(wallet.get("revive_coin", 0) or 0)
                        wallet_body = build_wallet_revive_coin_update_000e(
                            revive_coin_total
                        )
                        packets.append(
                            self.server._codec_response(0, 0x000E, wallet_body)
                        )
                        notes.append(
                            "S0/000E revive_wallet list0 slot=1 total=%d plain_len=%d"
                            % (revive_coin_total, len(wallet_body))
                        )
                    except Exception as wallet_sync_exc:
                        notes.append(
                            "revive_wallet_sync_fail:%s:%s"
                            % (type(wallet_sync_exc).__name__, wallet_sync_exc)
                        )

            # Stage3 专用图鉴账本，不属于 Main 槽位。reset+全量保证换号、重进房
            # 或账号切换时不会保留上一份客户端本地余额。
            try:
                epic_account_id = int(getattr(view, "account_id", 0) or 0)
                if epic_account_id <= 0:
                    epic_account_id = int(
                        repo.get_account_id_for_character(int(view.character_id)) or 0
                    )
                epic_body = build_account_epic_piece_balance_sync_047e(
                    repo,
                    epic_account_id,
                    reset_client=True,
                )
                packets.append(
                    self.server._codec_response(
                        0,
                        EPIC_PIECE_BALANCE_SYNC_OPCODE,
                        epic_body,
                    )
                )
                notes.append(
                    "S0/047E epic_piece_balance reset_flag=0 n=%d plain_len=%d"
                    % ((len(epic_body) - 5) // 8, len(epic_body))
                )
            except Exception as epic_sync_exc:
                notes.append(f"epic_piece_balance_sync_fail:{epic_sync_exc}")

            # list12 账号金库：默认开通+满档 selection_key=80（可 env 关）
            # 本地屏蔽 PVF min_level=60；任意等级都发 key=80
            send_account_cargo = _env_bool(
                "USDOF_GAME_ENTER_SERIA_START_SEND_ACCOUNT_CARGO_ITEMLIST", True
            )
            if send_account_cargo:
                try:
                    aid = int(getattr(view, "account_id", 0) or 0)
                    if aid <= 0:
                        try:
                            with repo.open() as _conn:
                                _ar = _conn.execute(
                                    "SELECT account_id FROM characters "
                                    "WHERE character_id = ? AND delete_flag = 0",
                                    (int(view.character_id),),
                                ).fetchone()
                            aid = int(_ar[0] or 0) if _ar else 0
                        except Exception:
                            aid = 0
                    if aid <= 0:
                        aid = 1
                    can_open = repo.character_can_open_account_cargo(char_level)
                    if can_open:
                        repo.ensure_account_cargo_state(
                            aid, character_level=char_level
                        )
                    ac_state = repo.get_account_cargo_state(
                        aid,
                        ensure_open=False,
                        character_level=char_level,
                    )
                    wire_key = (
                        int(ac_state.get("selection_key", 0) or 0)
                        if can_open
                        else 0
                    )
                    # 本地默认 can_open=True（min_level=1）；兜底仍强制满档
                    if can_open and wire_key <= 0:
                        wire_key = int(repo.DEFAULT_ACCOUNT_CARGO_SELECTION_KEY)
                    ac_raw = (
                        repo.list_account_cargo_items(
                            aid, character_level=char_level
                        )
                        if can_open
                        else []
                    )
                    ac_rows = _rows_from_raw(ac_raw)
                    ac_body = build_account_cargo_item_list(
                        ac_rows,
                        selection_key=wire_key,
                        cargo_gold=int(ac_state.get("value32", 0) or 0) if can_open else 0,
                    )
                    packets.append(self.server._codec_response(0, 0x000D, ac_body))
                    notes.append(
                        f"S0/000D account_cargo list12 n={len(ac_rows)} "
                        f"selection_key={wire_key} gold="
                        f"{int(ac_state.get('value32', 0) or 0) if can_open else 0} "
                        f"level={char_level} min_lv={repo.account_cargo_min_level()} "
                        f"open={int(can_open)} plain_len={len(ac_body)}"
                    )
                except Exception as ac_exc:
                    notes.append(f"itemlist_list12_fail:{ac_exc}")

        # 称号簿选角即时流：默认关（选角带条目 0166 曾闪退）。
        # 主路径改为进房 delayed 带条目 0166×5（见下方 delayed 段）。
        if _env_bool("USDOF_GAME_TITLEBOOK_SEND_ON_SELECT", False):
            try:
                from game.chain_loader import load_chain as _load_chain_tb_sel

                _tb_sel = _load_chain_tb_sel("helper-titlebook_grant.py")
                tb_pkts, tb_note = _tb_sel.pack_titlebook_sync_packets(
                    self.server,
                    character_id=int(view.character_id),
                    backfill_finished=True,
                    phase="select",
                )
                for i, pkt in enumerate(tb_pkts):
                    packets.append(pkt)
                if tb_pkts:
                    notes.append(
                        "SELECT_CHARACTER immediate titlebook n=%s %s"
                        % (len(tb_pkts), tb_note)
                    )
                else:
                    notes.append(tb_note)
            except Exception as tb_exc:
                notes.append("titlebook_select_fail:%r" % (tb_exc,))

        # C. 主对象 0131 必须在 USERINFO1 之前被客户端注册。
        # Stage3 type1: 1E7F0F0(key) 查主对象；找不到则属性 blob 整段跳过，PIV 也不回 0002。
        # 放进 SELECT 即时回包（与 START/ITEMLIST 同批），delayed USERINFO 再带 inter-packet 间隔，
        # 避免 0131 与 USERINFO 同 burst 时客户端尚未完成对象插入。
        # login_spawn / map_key 已在 START 组包前解析（见上）。

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0131", True):
            p131 = build_primary_user_object(cid_u16)
            packets.append(self.server._codec_response(0, 0x0131, p131))
            notes.append(
                "S0/0131 primary_user_object key=%s immediate-before-USERINFO1" % cid_u16
            )

        delayed: list[tuple[bytes, str]] = []
        if local_raid_features:
            event_body = build_mileage_shop_event(mileage_enabled=mileage_enabled, raid_enabled=True)
            delayed.append(
                (
                    self.server._codec_response(0, MILEAGE_SHOP_EVENT_OPCODE, event_body),
                    f"start-map delayed 0x006C feature_snapshot mileage={int(mileage_enabled)} raid=1",
                )
            )
        raid_userinfo0_town_sync_index: int | None = None

        # Ordinary login has no owned raid party to restore. Retire the exact
        # temporary party used by the solo/tutorial SELECT gate, including a
        # stale native roster from a previous character on the same client.
        # Do this before town paint; tutorial Phase-T1 publishes its own party
        # later if it needs the native 001D loading gate. Raid reconnects keep
        # their separate, authoritative roster publication.
        channel_id = getattr(session, 'selected_channel_id', None)
        from game.raid.ownership import owns_raid_membership
        if type(channel_id) is int and channel_id > 0 and not owns_raid_membership(self.server, session):
            clear = load_chain('helper-party_info.py').build_current_namespace_party_clear(party_id=1)
            delayed.append((self.server._codec_response(0, 0x0009, clear),
                            'start-map unadmitted login clears stale temporary solo party=1'))

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0069", True):
            creature_states = repo.list_creature_states(int(view.character_id))
            b69 = build_creature_item_list(creature_states)
            delayed.append(
                (
                    self.server._codec_response(0, 0x0069, b69),
                    "start-map delayed 0x0069 creature_item_list n=%s"
                    % len(creature_states),
                )
            )

        # 教程首进：跳过赛丽亚房间 paint（0016/0017/0018），不先展示房间再进本。
        # 角色对象仍靠 0131+USERINFO；进本走 Phase-T1 的 000F(area FF)+001C/001D。
        if tutorial_skip_room:
            notes.append(
                "tutorial_skip_seria_room_paint=1 skip=0016+0017+0018 "
                "(direct dungeon after character setup)"
            )
        if (
            not tutorial_skip_room
            and _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0016", True)
        ):
            if raid_userinfo0_town_sync_index is None:
                raid_userinfo0_town_sync_index = len(delayed)
            p16 = build_user_position_seria(
                character_id=cid_u16,
                pos_x=pos_x,
                pos_y=pos_y,
                direction=direction,
            )
            delayed.append(
                (
                    self.server._codec_response(0, 0x0016, p16),
                    "start-map delayed 0x0016 user_position seria (%s,%s,dir%s)"
                    % (pos_x, pos_y, direction),
                )
            )

        if (
            not tutorial_skip_room
            and _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0017", True)
        ):
            if raid_userinfo0_town_sync_index is None:
                raid_userinfo0_town_sync_index = len(delayed)
            p17 = build_user_area(
                character_id=cid_u16,
                town_id=town_id,
                area_id=area_id,
                pos_x=pos_x,
                pos_y=pos_y,
                direction=direction,
                state=area_state,
            )
            delayed.append(
                (
                    self.server._codec_response(0, 0x0017, p17),
                    "start-map delayed 0x0017 user_area town=%s area=%s"
                    % (town_id, area_id),
                )
            )

        if (
            not tutorial_skip_room
            and _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0018", True)
        ):
            if raid_userinfo0_town_sync_index is None:
                raid_userinfo0_town_sync_index = len(delayed)
            p18 = build_area_users(
                character_id=cid_u16,
                town_id=town_id,
                area_id=area_id,
                pos_x=pos_x,
                pos_y=pos_y,
                direction=direction,
                state=area_state,
            )
            delayed.append(
                (
                    self.server._codec_response(0, 0x0018, p18),
                    "start-map delayed 0x0018 area_users self_only",
                )
            )

        # 初始技能：优先 character_skills（含槽位持久化）；空则 seed JSON。
        job_for_skills = character_job
        level_for_sp = character_level
        sp_state = resolve_skill_point_protocol_state(
            level=level_for_sp,
            character_id=character_id,
            repo=repo,
            session=session,
            job=job_for_skills,
        )
        if session is not None:
            try:
                session.character_job = character_job
                session.character_grow_type = character_grow
                session.character_visibility_bits = int(
                    getattr(selected, "visibility_bits", 3) or 0
                ) & 0xFF
                session.character_level = character_level
                session.character_exp = character_exp
                session.character_expertise_exp = character_expertise_exp
                session.skill_remain_sp = int(sp_state.page0_sp)
                session.skill_remain_tp = int(sp_state.page0_tp)
            except Exception:
                pass
        notes.append(
            "character_snapshot cid=%s job=%s grow=%s lv=%s exp=%s expertise=%s "
            "ex_equip=0x%02X "
            "sp=%s/%s tp=%s/%s"
            % (
                character_id,
                character_job,
                character_grow,
                character_level,
                character_exp,
                character_expertise_exp,
                int(view.ex_equip_slot_stat) & 0xFF,
                int(sp_state.page0_sp),
                int(sp_state.page1_sp),
                int(sp_state.page0_tp),
                int(sp_state.page1_tp),
            )
        )
        level_title_backfill_grants = []
        level_title_backfill_note = "level_title backfill skip=level_not_above_85"
        level_title_helper = None
        level_title_backfill_checked = False
        if session is not None:
            try:
                level_title_backfill_checked = (
                    int(
                        getattr(
                            session,
                            "_level_title_backfill_checked_character_id",
                            0,
                        )
                        or 0
                    )
                    == int(view.character_id)
                )
            except Exception:
                level_title_backfill_checked = False
        if character_level > 85 and not level_title_backfill_checked:
            try:
                level_title_helper = load_chain("helper-level_milestone_titles.py")
                level_title_backfill_grants, level_title_backfill_note = (
                    level_title_helper.claim_level_title_rewards(
                        repo,
                        character_id=int(view.character_id),
                        previous_level=character_level,
                        current_level=character_level,
                        backfill=True,
                    )
                )
                if session is not None and "lookup_error=" not in level_title_backfill_note:
                    session._level_title_backfill_checked_character_id = int(
                        view.character_id
                    )
                if level_title_backfill_grants:
                    notes.append(level_title_backfill_note)
            except Exception as level_title_exc:
                level_title_backfill_note = "level_title backfill err=%s" % type(
                    level_title_exc
                ).__name__
                notes.append(level_title_backfill_note)
        if _env_bool("USDOF_GAME_SKILLS_PERSIST", True):
            skills, skills_resolve_note = resolve_skills_for_character(
                repo,
                int(view.character_id),
                job_for_skills,
                page_header=int(sp_state.page0_sp),
                seed_if_empty=True,
            )
            notes.append(skills_resolve_note)
            try:
                # 可选镜像剩余 SP 到 page_header（对照抓包，非权威）
                repo.update_character_skills_page_header(
                    int(view.character_id),
                    page=0,
                    page_header=int(sp_state.page0_sp),
                )
            except Exception:
                pass
        else:
            skills = get_initial_skills_for_job(job_for_skills)
            notes.append(
                "skills_resolve json_only n=%s (SKILLS_PERSIST=0)" % (len(skills),)
            )

        # 进房穿戴注入点（两层，数据同源 character_equipped_entries）：
        #   1) USERINFO0 type0 → sub_18CC430 19B×n：模型/纸娃娃紧凑表（不建 Inventory 槽）
        #   2) USERINFO1 type1 → sub_166AED0：完整建槽（vtable+1852）；对照 S4A12/90qiyuan subtype1
        #   3) S0/000E list3 → 108B 只更新已有槽（须在 166AED0 之后；否则静默空跑）
        # 角色页 type2 默认不发装备。
        #
        # 骑士盾 deck 主盾 → equipped slot24（[support weapon]）。
        # 日常默认开（standard_local_flow）：进房 pre_sync 后 USERINFO/000E 才有副手。
        # 主盾真源仍是 character_knight_shield_deck（MOVE_ITEMSPACE list31/32 + C1/0292）。
        # deck NOTI = S0/0245（Stage3 sub_1674530）；禁止 0x0244（频道 settle）。
        # 见 docs/baseline_knight_shield_deck_20260722.md。
        knight_deck_ids = None
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_KNIGHT_DECK", True):
            try:
                ks_pre = load_chain("helper-knight_shield.py")
                job_pre = int(getattr(view, "job", 0) or 0)
                if ks_pre.is_eligible_character(job=job_pre):
                    knight_deck_ids = repo.load_knight_shield_deck(int(view.character_id))
                    with repo.open() as _ks_conn:
                        repo._sync_support_weapon_from_main_shield(
                            _ks_conn,
                            character_id=int(view.character_id),
                            main_item_id=int(knight_deck_ids[0] or 0),
                        )
                    notes.append(
                        "knight_shield_pre_sync %s"
                        % ks_pre.describe_deck(knight_deck_ids)
                    )
            except Exception as ks_pre_exc:
                notes.append(
                    "knight_shield_pre_sync_fail:%s" % type(ks_pre_exc).__name__
                )

        equip_helper = load_chain("helper-userinfo_equipment.py")
        equipment, equip_note = equip_helper.resolve_enter_room_equipment(
            view,
            repo=repo,
        )
        notes.append(equip_note)

        # Some Stage3 clients validate body equipment while USERINFO1 is being
        # decoded, before the skill page at the tail of that same packet has
        # been applied.  Preload the authoritative skill tree ahead of the
        # USERINFO1 equipment rows so weapon-specific mastery (for example
        # Swordman light-sword mastery) is available during that validation.
        # Keep this behind a narrow A/B switch until the live client confirms
        # the ordering; the fallback path remains U0 -> U1 -> 000E -> 0013.
        skillinfo_before_userinfo1 = _env_bool(
            "USDOF_GAME_ENTER_SERIA_START_SKILLINFO_BEFORE_USERINFO1", False
        )
        send_skillinfo = _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_SKILLINFO", True)
        skill_body: bytes | None = None
        skill_note = ""
        if send_skillinfo:
            if _env_bool("USDOF_GAME_ENTER_SERIA_START_SKILLINFO_FROM_JSON", True):
                # Even an empty skill tree still needs two real SkillInfo pages:
                # their scalar fields initialize the client's SP/TP stores.
                skill_body = build_skillinfo_from_pvf(
                    skills,
                    page0_sp=int(sp_state.page0_sp),
                    page1_sp=int(sp_state.page1_sp),
                    page0_tp=int(sp_state.page0_tp),
                    page1_tp=int(sp_state.page1_tp),
                )
                skill_note = (
                    "start-map delayed 0x0013 skillinfo_from_db_or_json "
                    f"job={int(getattr(view, 'job', 0) or 0)} count={len(skills)} "
                    f"sp0={int(sp_state.page0_sp)} sp1={int(sp_state.page1_sp)} "
                    f"tp0={int(sp_state.page0_tp)} tp1={int(sp_state.page1_tp)} "
                    f"plain_len={len(skill_body)}"
                )
            else:
                skill_body = build_skillinfo_empty()
                skill_note = (
                    "start-map delayed 0x0013 skillinfo_empty "
                    f"job={int(getattr(view, 'job', 0) or 0)} skills={len(skills)}"
                )

        dark_knight_combo_body: bytes | None = None
        dark_knight_combo_note = ""
        if int(getattr(view, "job", 0) or 0) == 9:
            try:
                combo_helper = load_chain("helper-dark_knight_combo_skill.py")
                dark_knight_combo_body, dark_knight_combo_note = (
                    combo_helper.load_dark_knight_combo_notification(
                        repo,
                        int(view.character_id),
                        int(view.job),
                    )
                )
                if dark_knight_combo_body is None:
                    notes.append(dark_knight_combo_note)
            except Exception as combo_exc:
                notes.append(
                    "dark_knight_combo_load_fail:%s" % type(combo_exc).__name__
                )

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_USERINFO0", True):
            guild_id = 0
            guild_name = ""
            try:
                guild = GuildRepository(repo.db_path).get_guild_for_character(
                    int(view.character_id)
                )
                if guild is not None:
                    guild_id = int(guild.guild_id)
                    guild_name = guild.display_name
            except Exception as guild_exc:
                notes.append(
                    "start-map guild_identity_lookup_fail:%s"
                    % type(guild_exc).__name__
                )
            u0 = build_userinfo_subtype0_min(
                view,
                equipment,
                guild_name=guild_name,
                guild_id=guild_id,
            )
            u0_packet = self.server._codec_response(0, 0x0002, u0)
            if getattr(session, 'selected_channel_id', None) in (23, 32):
                raid_hooks = local_raid_hooks(self.server)
                if raid_hooks is not None:
                    raid_hooks.battle.service.native_lobby.prepare_self(session, u0_packet, u0)
            delayed.append(
                (
                    u0_packet,
                    "start-map delayed 0x0002 userinfo_subtype0_min "
                    "key=%s plain_len=%s %s guild_id=%s guild=%r"
                    % (cid_u16, len(u0), equip_note, guild_id, guild_name),
                )
            )
            if (
                _env_bool("USDOF_RESTORE_RAID_USERINFO0_ORDER", False)
                and _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
                and type(getattr(session, "selected_channel_id", None)) is int
                and session.selected_channel_id in (23, 32)
                and raid_userinfo0_town_sync_index is not None
            ):
                delayed.insert(raid_userinfo0_town_sync_index, delayed.pop())
            visibility_helper = load_chain("helper-avatar_visibility.py")
            delayed.append(
                (
                    visibility_helper.pack_avatar_visibility(self.server, view),
                    "start-map delayed 0x0165 avatar_visibility "
                    "key=%s bits=0x%02x"
                    % (cid_u16, int(getattr(view, "visibility_bits", 3)) & 0xFF),
                )
            )

        if skillinfo_before_userinfo1 and skill_body is not None:
            delayed.append(
                (
                    self.server._codec_response(0, 0x0013, skill_body),
                    skill_note + " skillinfo_before_userinfo1",
                )
            )
            notes.append("skillinfo_pre_userinfo1=1")

        guild_status_rows = ()
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_USERINFO1", True):
            from game.character.character_stats import compute_combat_stats, describe_growth_phase

            job = character_job
            grow = character_grow
            raw_sub = getattr(view, "sub_grow_type", None)
            sub_grow = None if raw_sub is None else int(raw_sub)
            level = character_level
            # Same DB snapshot as USERINFO0/SKILLINFO. sub_18CE440 writes this
            # absolute value into the newly created client character object.
            exp = character_exp
            phase = describe_growth_phase(grow, sub_grow_type=sub_grow)
            stats = compute_combat_stats(job, level, grow, sub_grow_type=sub_grow)
            # USERINFO1 技能页（18CCF80）与 S0/0013 技能真源。
            # Stage3 live 2026-07-20：
            #   - grow=0 冷进 skill_page0=16 + 0013 可长会话（20:24 已验证）
            #   - grow=0 关技能（U1/0013 都空）→ 进赛丽亚后 ~3~5s 10054
            #   - grow=1 冷进 skill_page0=19 另案；勿把「关技能」当通用默认
            skill_pages = ()
            u1_skill_note = "u1_skills=off"
            if _env_bool("USDOF_GAME_ENTER_SERIA_START_USERINFO1_SKILLS", True):
                skill_pages = (
                    skills
                    if skills
                    and _env_bool(
                        "USDOF_GAME_ENTER_SERIA_START_SKILLINFO_FROM_JSON", True
                    )
                    else ()
                )
                u1_skill_note = "u1_skills=on n=%s" % len(skill_pages)
            # 槽对象创建：sub_166AED0（type1）；equipment 空则 count=0
            send_u1_equip = _env_bool(
                "USDOF_GAME_ENTER_SERIA_START_USERINFO1_EQUIPMENT", True
            )
            u1_equipment = equipment if send_u1_equip else ()
            quest_snapshot = _quest_completion.load_finished_quest_snapshot(
                repo, int(view.character_id)
            )
            from game.quest.time_fragment_rewards import finished_time_fragment_count

            time_fragment_count = finished_time_fragment_count(
                quest_snapshot.finished_quest_ids,
                job_id=job,
            )
            adventure_progression = calculate_adventure_group_for_character(
                repo,
                view,
            )
            adventure_group_level = int(adventure_progression.adventure_level)
            guild_membership_active = has_active_guild_membership(repo, view)
            u1 = build_userinfo_subtype1_min(
                view,
                exp=exp,
                skills=skill_pages,
                entry_flag=0,
                equipment=u1_equipment,
                finished_quest_ids=quest_snapshot.finished_quest_ids,
                adventure_group_level=adventure_group_level,
                guild_membership_active=guild_membership_active,
            )
            u1_equip_n = len(u1_equipment) if send_u1_equip else 0
            delayed.append(
                (
                    self.server._codec_response(0, 0x0002, u1),
                    (
                        "start-map delayed 0x0002 userinfo_subtype1_stats "
                        f"job={job} grow={grow} sub={sub_grow} lv={level} exp={exp} "
                        f"key={cid_u16} after_0131=1 skill_page0={len(skill_pages)} "
                        f"{u1_skill_note} "
                        f"equip_166aed0={u1_equip_n} "
                        f"first={phase.first} second={phase.second} "
                        f"classed={int(phase.classed)} awakened={int(phase.awakened)} "
                        f"pre={phase.pre_class_table} post={phase.post_class_table} "
                        f"awk={phase.awakening_table or '-'} "
                        f"hp={stats.hp_max} mp={stats.mp_max} "
                        f"patk={stats.phys_atk} matk={stats.mag_atk} "
                        f"move={stats.move_speed} plain_len={len(u1)} "
                        f"time_fragments={time_fragment_count} "
                        f"adventure_level={adventure_group_level} "
                        f"{quest_snapshot.describe()} "
                        f"client_exp_prime=1"
                    ),
                )
            )
            guild_status_rows = pack_guild_membership_status_rows(
                self.server,
                guild_membership_active,
            )
            # IDA: USERINFO1/sub_18CE440 primes entity exp store for later S0/0025.
            if session is not None:
                try:
                    session.character_level = int(level)
                    session.character_exp = int(exp)
                    session.client_exp_primed = True
                except Exception:
                    pass
            notes.append("userinfo1_equip_166aed0 n=%s" % u1_equip_n)

        # Inventory 装备栏 000E list3：必须在 USERINFO1/166AED0 建槽之后。
        # sub_1667BD0 list_type==3 只更新已有槽；USERINFO0 的 18CC430 不建槽。
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_EQUIP_ITEMLIST", True):
            try:
                equip_bar_helper = load_chain("helper-equip_itemlist.py")
                equip_body, equip_n, equip_bar_note = (
                    equip_bar_helper.build_equipment_bar_update_000e_body(
                        repo,
                        int(view.character_id),
                    )
                )
                delayed.append(
                    (
                        self.server._codec_response(0, 0x000E, equip_body),
                        "start-map delayed %s after_USERINFO1_166aed0" % equip_bar_note,
                    )
                )
                notes.append("%s after_USERINFO1_166aed0" % equip_bar_note)
                try:
                    equipment_restore_packets, equipment_restore_note = (
                        build_equipment_lock_restore_packets(
                            self.server,
                            repo,
                            character_id=int(view.character_id),
                            list_types=(3,),
                        )
                    )
                    for restore_packet in equipment_restore_packets:
                        delayed.append(
                            (
                                restore_packet,
                                "start-map delayed %s after_USERINFO1_166aed0_list3"
                                % equipment_restore_note,
                            )
                        )
                    notes.append(equipment_restore_note)
                except Exception as lock_exc:
                    notes.append(
                        "equipment_lock_restore_fail:%s:%s"
                        % (type(lock_exc).__name__, lock_exc)
                    )
                try:
                    set_restore_packets, set_restore_note = (
                        _customability_set_helper.build_customability_set_restore_packets(
                            self.server,
                            repo,
                            character_id=int(view.character_id),
                            actor_id=int(view.character_id),
                        )
                    )
                    for restore_packet in set_restore_packets:
                        delayed.append(
                            (
                                restore_packet,
                                "start-map delayed %s after_USERINFO1_166aed0_list3"
                                % set_restore_note,
                            )
                        )
                    notes.append(set_restore_note)
                except Exception as set_restore_exc:
                    notes.append(
                        "customability_set_restore_fail:%s:%s"
                        % (type(set_restore_exc).__name__, set_restore_exc)
                    )
            except Exception as equip_exc:
                notes.append(f"equip_bar_000e_fail:{equip_exc}")

        try:
            exp_item_state_packets, exp_item_state_note = (
                build_exp_bonus_item_state_restore_packets(
                    self.server,
                    repo,
                    int(view.character_id),
                )
            )
            for exp_item_state_packet in exp_item_state_packets:
                delayed.append(
                    (
                        exp_item_state_packet,
                        "start-map delayed %s after_USERINFO1_166aed0_list3"
                        % exp_item_state_note,
                    )
                )
            notes.append(exp_item_state_note)
        except Exception as exp_item_state_exc:
            notes.append(
                "exp_item_state_restore_fail:%s:%s"
                % (type(exp_item_state_exc).__name__, exp_item_state_exc)
            )

        # USERINFO0 首次发布全局 list36，USERINFO1 建立装备槽；普通 000D/list36
        # 必须在该对象链就绪后写入 UI 实际读取的全局容器。放在 list3 之后，
        # 全量插入尾参数 (1, 0)，不会触发“新获得”通知。
        if guild_medal_login_full_sync is not None:
            delayed.append(guild_medal_login_full_sync)
            try:
                equipment_restore_packets, equipment_restore_note = (
                    build_equipment_lock_restore_packets(
                        self.server,
                        repo,
                        character_id=int(view.character_id),
                        list_types=(repo.INVENTORY_LIST_GUILD_MEDAL,),
                    )
                )
                for restore_packet in equipment_restore_packets:
                    delayed.append(
                        (
                            restore_packet,
                            "start-map delayed %s after_000D_list36"
                            % equipment_restore_note,
                        )
                    )
                notes.append(equipment_restore_note)
            except Exception as lock_exc:
                notes.append(
                    "equipment_lock_restore_fail:%s:%s"
                    % (type(lock_exc).__name__, lock_exc)
                )

        # Preserve the client-established USERINFO1 -> 000E equipment gap.
        # Guild status objects are independent and may be applied afterwards.
        delayed.extend(guild_status_rows)

        if wish_hydrated:
            try:
                from game.drop.wish_service import build_wish_items_snapshot_packet

                wish_snapshot_packet, wish_snapshot_note = (
                    build_wish_items_snapshot_packet(self.server, wish_ids)
                )
                delayed.append(
                    (
                        wish_snapshot_packet,
                        "start-map delayed %s after_USERINFO1" % wish_snapshot_note,
                    )
                )
                notes.append(wish_snapshot_note)
            except Exception as wish_snapshot_exc:
                notes.append(
                    "wish_snapshot_050a_fail:%s:%s"
                    % (type(wish_snapshot_exc).__name__, wish_snapshot_exc)
                )

        try:
            from game.character.damage_font import build_damage_font_list_packet

            damage_font_packet, damage_font_note = build_damage_font_list_packet(
                self.server,
                repo,
                int(view.character_id),
            )
            delayed.append(
                (
                    damage_font_packet,
                    "start-map delayed %s after_USERINFO1" % damage_font_note,
                )
            )
            notes.append(damage_font_note)
        except Exception as damage_font_exc:
            notes.append(
                "damage_font_04d9_fail:%s:%s"
                % (type(damage_font_exc).__name__, damage_font_exc)
            )

        delayed.append(
            (
                self.server._codec_response(
                    0,
                    CUBE_CONTRACT_STATE_OPCODE,
                    cube_contract_body,
                ),
                "start-map delayed S0/0383 %s after_USERINFO1" % cube_contract_note,
            )
        )
        delayed.append(
            (
                self.server._codec_response(
                    0,
                    _activity_state.FATIGUE_BURNING_STATE_OPCODE,
                    fatigue_burning_body,
                ),
                "start-map delayed S0/01EB %s after_USERINFO1"
                % fatigue_burning_note,
            )
        )

        # 骑士盾：主盾已在 resolve 前同步 slot24。
        # S0/0245 SEND_DECK_INFO（Stage3 sub_1674530）：进房必须下发 5×u32，
        # 否则 UI 底部 1 主 4 副 重启后变空（DB 有、客户端无）。
        # **禁止** 0x0244（频道 settle）。
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_KNIGHT_DECK", True):
            try:
                ks = load_chain("helper-knight_shield.py")
                job_ks = int(getattr(view, "job", 0) or 0)
                if ks.is_eligible_character(job=job_ks):
                    deck_ids = (
                        knight_deck_ids
                        if knight_deck_ids is not None
                        else repo.load_knight_shield_deck(int(view.character_id))
                    )
                    notes.append("knight_shield %s" % ks.describe_deck(deck_ids))
                    if ks.should_send_deck_notification():
                        body_deck = ks.build_send_deck_info_body(deck_ids)
                        delayed.append(
                            (
                                self.server._codec_response(
                                    0, int(ks.NOTI_SEND_DECK_INFO), body_deck
                                ),
                                "start-map delayed S0/%04X SEND_DECK_INFO %s"
                                % (
                                    int(ks.NOTI_SEND_DECK_INFO),
                                    ks.describe_deck(deck_ids),
                                ),
                            )
                        )
                    else:
                        notes.append("knight_deck_noti_off")
            except Exception as deck_exc:
                notes.append("knight_deck_fail:%s" % type(deck_exc).__name__)
        elif knight_deck_ids is not None:
            try:
                ks = load_chain("helper-knight_shield.py")
                notes.append(
                    "knight_shield %s (deck_noti_off)" % ks.describe_deck(knight_deck_ids)
                )
            except Exception:
                pass

        # grow=0 基线必须发 0013；默认 True。grow=1 冷进 A/B 可显式关。
        if skill_body is not None and not skillinfo_before_userinfo1:
            delayed.append(
                (self.server._codec_response(0, 0x0013, skill_body), skill_note)
            )
        elif not send_skillinfo:
            notes.append("skillinfo_skipped_default_off")

        # USERINFO1/sub_166AED0 replaces the job-9 character-specific object.
        # Restore the 6x5 matrix only after that replacement and after 000E, so
        # the established USERINFO1 -> equipment-list gap remains untouched.
        if dark_knight_combo_body is not None and skill_body is not None:
            delayed.append(
                (
                    self.server._codec_response(
                        0,
                        int(combo_helper.NOTI_DARK_KNIGHT_COMBO_SKILL),
                        dark_knight_combo_body,
                    ),
                    "start-map delayed %s after_USERINFO1_000E_and_S0/0013"
                    % dark_knight_combo_note,
                )
            )
        elif dark_knight_combo_body is not None:
            notes.append("dark_knight_combo_skip=skillinfo_not_sent")

        # 进房 SP：已由 0x0013 skillinfo page_field1 写入；**禁止**默认发 S0/0025。
        # Stage3 sub_166CB70：toast=packet_total-local；进房 local 未 prime 时
        # delta 暴涨 → 海量经验 / 决斗胜点（v121 槽误读）。仅调试可开 env。
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_EXP_0025", False):
            try:
                exp_for_0025 = character_exp
                level_for_0025 = character_level
                body_0025 = build_exp_notification_0025(
                    level_for_0025,
                    exp_for_0025,
                    expertise_exp=character_expertise_exp,
                    page0_sp=int(sp_state.page0_sp),
                    page1_sp=int(sp_state.page1_sp),
                    page0_tp=int(sp_state.page0_tp),
                    page1_tp=int(sp_state.page1_tp),
                    pvp_victory_point=0,
                    layout=(
                        os.environ.get("USDOF_GAME_DUNGEON_EXP_NOTI_LAYOUT")
                        or "stage3_78"
                    ),
                )
                delayed.append(
                    (
                        self.server._codec_response(0, 0x0025, body_0025),
                        (
                            "start-map delayed 0x0025 exp_sp_prime "
                            "lv=%s exp=%s expertise=%s sp=%s/%s tp=%s/%s "
                            "plain_len=%s"
                            % (
                                level_for_0025,
                                exp_for_0025,
                                character_expertise_exp,
                                int(sp_state.page0_sp),
                                int(sp_state.page1_sp),
                                int(sp_state.page0_tp),
                                int(sp_state.page1_tp),
                                len(body_0025),
                            )
                        ),
                    )
                )
                if session is not None:
                    try:
                        session.character_level = int(level_for_0025)
                        session.character_exp = int(exp_for_0025)
                        session.character_expertise_exp = int(
                            character_expertise_exp
                        )
                        session.client_exp_primed = True
                    except Exception:
                        pass
            except Exception as exp_exc:
                notes.append("enter_0025_fail:%r" % (exp_exc,))

        # 高等级历史补发与任务列表同步相互独立：即使运营配置暂时关闭
        # 0015/0164/023F，已经入簿的称号仍必须能走 0168 获取动画。
        level_title_backfill_packets = []
        level_title_backfill_packet_note = ""
        level_title_backfill_has_animation = False
        if level_title_backfill_grants and level_title_helper is not None:
            try:
                (
                    level_title_backfill_packets,
                    level_title_backfill_packet_note,
                ) = level_title_helper.pack_level_title_grant_packets(
                    self.server,
                    grants=level_title_backfill_grants,
                )
                level_title_backfill_has_animation = any(
                    len(packet) >= 3
                    and int.from_bytes(packet[1:3], "little") == 0x0168
                    for packet in level_title_backfill_packets
                )
            except Exception as level_title_exc:
                notes.append(
                    "level_title_backfill_packets err=%s"
                    % type(level_title_exc).__name__
                )

        # Daily Challenge owns a separate day ledger.  Establish and publish
        # it before hydrating the quest lists so the first 0164 snapshot can
        # include entries already claimed today.  0164 is a full replacement;
        # sending it first would make the client replay the challenge history.
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_DAILY_CHALLENGE", True):
            daily_status = build_daily_challenge_status_0286()
            delayed.append(
                (
                    self.server._codec_response(0, 0x0286, daily_status),
                    "start-map delayed 0x0286 daily_challenge_status",
                )
            )
            daily_body, _daily_state, daily_note = build_daily_challenge_init_0287(
                repo,
                character_id=character_id,
                level=character_level,
            )
            session._daily_challenge_identity = (character_id, _daily_state.day_key)
            delayed.append(
                (
                    self.server._codec_response(0, 0x0287, daily_body),
                    "start-map delayed %s" % daily_note,
                )
            )

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_ACCEPTABLE_QUEST", True) or _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_CLEAR_QUEST", True) or _env_bool("USDOF_GAME_REPLY_ACCEPTED_QUEST_023F", True):
            # Quest hydrate: filter acceptable by active/finished; push 0015/0164/023F.
            qdata = hydrate_quest_lists(
                self.server,
                character_id=int(view.character_id),
                job=int(getattr(view, "job", 0) or 0),
                level=int(getattr(view, "level", 1) or 1),
                grow_type=int(getattr(view, "grow_type", 0) or 0),
                session=session,
                ensure_unlock=True,
            )
            daily_active_removed = list(
                qdata.get("daily_challenge_active_removed_ids") or []
            )
            daily_active_cleanup_errors = list(
                qdata.get("daily_challenge_active_cleanup_errors") or []
            )
            if daily_active_removed:
                notes.append(
                    "daily-active-cleanup removed=%s"
                    % ",".join(str(value) for value in daily_active_removed)
                )
            if daily_active_cleanup_errors:
                notes.append(
                    "daily-active-cleanup errors=%s"
                    % "|".join(daily_active_cleanup_errors)
                )
            unlock = qdata.get("unlock") or {}
            if unlock:
                notes.append(
                    "unlock-quests n=%s finished=%s"
                    % (unlock.get("seeded", 0), len(qdata.get("finished_ids") or []))
                )
            if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_ACCEPTABLE_QUEST", True):
                aq = qdata["body_0015"]
                ids = qdata["acceptable_ids"]
                aq_note = (
                    "start-map delayed 0x0015 acceptable_quest "
                    "job=%s count=%s plain_len=%s sample=%s"
                    % (
                        int(getattr(view, "job", 0) or 0),
                        len(ids),
                        len(aq),
                        list(ids)[:6],
                    )
                )
                delayed.append((self.server._codec_response(0, 0x0015, aq), aq_note))
            quest_clear_insert_at = len(delayed)
            if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_CLEAR_QUEST", True):
                # 思路.md 6.1：START_MAP 推 non-empty 0164；空列表默认跳过防 desync
                cq = qdata["body_0164"]
                finished_ids = list(qdata.get("finished_ids") or [])
                sample = ",".join(str(x) for x in finished_ids[:8]) if finished_ids else "-"
                if finished_ids or _env_bool("USDOF_GAME_REPLY_FINISH_QUEST_CLEAR_0164", False):
                    delayed.append(
                        (
                            self.server._codec_response(0, 0x0164, cq),
                            "start-map delayed 0x0164 clear_quest finished=%s sample=%s plain_len=%s unlock_seeded=%s"
                            % (
                                len(finished_ids),
                                sample,
                                len(cq),
                                int((qdata.get("unlock") or {}).get("seeded", 0) or 0),
                            ),
                        )
                    )
                else:
                    notes.append("start-map 0x0164 skip_empty")
            if _env_bool("USDOF_GAME_REPLY_ACCEPTED_QUEST_023F", True):
                body_023f = qdata["body_023f"]
                delayed.append(
                    (
                        self.server._codec_response(0, 0x023F, body_023f),
                        "start-map delayed 0x023F accepted_quest count=%s plain_len=%s"
                        % (len(qdata["active_pairs"]), len(body_023f)),
                    )
                )
                # Empty 023F clears native completed quests; keep it before 0164.
                if (
                    _env_bool("USDOF_RESTORE_QUEST_HYDRATION_ORDER", False)
                    and len(body_023f) >= 4
                    and int.from_bytes(body_023f[:4], "little") == 0
                ):
                    delayed.insert(quest_clear_insert_at, delayed.pop())
            # 称号簿进房（默认开，对齐 13:xx live 成功态）:
            # delayed 带条目 0166×5，不发 0168；**默认不做 finished backfill**
            # （backfill 曾把 1028/26598 写回，导致 entries=2 闪退，掩盖「仅 26596 正常」对照）。
            # 当本轮补发实际携带 0168 时，不能再叠 0166；其获取动画由 0168 自己 put。
            if _env_bool("USDOF_GAME_TITLEBOOK_SEND_ON_ENTER", True):
                if level_title_backfill_has_animation:
                    notes.append("titlebook_enter skip_0166=level_title_0168")
                else:
                    try:
                        from game.chain_loader import load_chain as _load_chain_tb

                        _tb = _load_chain_tb("helper-titlebook_grant.py")
                        tb_pkts, tb_note = _tb.pack_titlebook_sync_packets(
                            self.server,
                            character_id=int(view.character_id),
                            backfill_finished=_env_bool(
                                "USDOF_GAME_TITLEBOOK_BACKFILL_FINISHED", False
                            ),
                            phase="enter",
                        )
                        for i, pkt in enumerate(tb_pkts):
                            delayed.append(
                                (
                                    pkt,
                                    "start-map delayed titlebook i=%s %s"
                                    % (i, tb_note if i == 0 else ""),
                                )
                            )
                        if not tb_pkts:
                            notes.append(tb_note)
                    except Exception as tb_exc:
                        notes.append("titlebook_enter_fail:%r" % (tb_exc,))
            # 已接且 trigger==0（可交未交）：
            # - 023F 只灌列表/数据态（F2 任务窗可显示可交），不点亮右方/底部 HUD 动效
            # - S1/0021 须在城镇 UI 就绪后发（首次 C1/0023）；BURST 阶段发会静默失败
            # - 默认 start-map 只 mark pending，由 opcode0023 做 pulse 1→0 arm
            if session is not None:
                zero_n = sum(
                    1
                    for _q, t in (qdata.get("active_pairs") or ())
                    if (int(t) & 0xFFFFFFFF) == 0
                )
                session.quest_hud_blink_pending = bool(zero_n > 0) and _env_bool(
                    "USDOF_GAME_QUEST_0023_ARM_COMPLETEABLE_0021", True
                )
                session.quest_hud_blink_armed = False
                if session.quest_hud_blink_pending:
                    notes.append(
                        "start-map quest_hud_blink_pending completeable=%s (arm-on-C1/0023)"
                        % zero_n
                    )
            if _env_bool("USDOF_GAME_QUEST_START_ARM_COMPLETEABLE_0021", False):
                from game.quest.quest_session import pack_completeable_trigger_acks

                arm_pkts, arm_note = pack_completeable_trigger_acks(
                    self.server, qdata.get("active_pairs") or ()
                )
                for pkt in arm_pkts:
                    delayed.append(
                        (
                            pkt,
                            "start-map delayed %s" % arm_note,
                        )
                    )

        if level_title_backfill_packets:
            for index, packet in enumerate(level_title_backfill_packets):
                delayed.append(
                    (
                        packet,
                        "start-map delayed level_title i=%s %s"
                        % (
                            index,
                            (
                                level_title_backfill_packet_note
                                if index == 0
                                else ""
                            ),
                        ),
                    )
                )
            notes.append(level_title_backfill_packet_note)

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_007C", True):
            p7c = build_selector_007c_empty()
            delayed.append(
                (
                    self.server._codec_response(0, 0x007C, p7c),
                    "start-map delayed 0x007C selector_empty",
                )
            )

        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_00AD", True):
            build_game_option_packet_body_from_main = load_chain('helper-00ad_defaults.py').build_game_option_packet_body_from_main
            account_id = int(getattr(self.server, "account_id", 1) or 1)
            main = None
            try:
                main = self.server._repo().get_main_game_option(account_id)
            except Exception:
                main = None
            if main:
                opt = build_game_option_packet_body_from_main(main)
                src = f"account_settings len={len(main)}"
            else:
                opt = build_game_option_defaults()
                src = "defaults"
            opt74 = int.from_bytes(opt[4 + 74 * 2 : 4 + 74 * 2 + 2], "little")
            delayed.append((self.server._codec_response(0, 0x00AD, opt), f"start-map delayed 0x00AD game_option src={src} opt74=0x{opt74:04x} plain_len={len(opt)}"))

        # 角色选项 / 技能锁定：C1/01C0 落库 → 进房 S0/0187 回放（S4A12 CharacterOptionBodyBuilder）
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_0187_CHARACTER_OPTION", True):
            cid_opt = 0
            try:
                cid_opt = int(view.character_id)
            except Exception:
                cid_opt = 0
            if cid_opt <= 0 and session is not None:
                try:
                    cid_opt = int(getattr(session, "selected_character_id", 0) or 0)
                except Exception:
                    cid_opt = 0
            body_0187, src_0187 = resolve_saved_character_option_body(
                self.server._repo(), cid_opt
            )
            if body_0187 is not None:
                delayed.append(
                    (
                        self.server._codec_response(0, 0x0187, body_0187),
                        "start-map delayed 0x0187 character_option src=%s plain_len=%s"
                        % (src_0187, len(body_0187)),
                    )
                )
            else:
                notes.append(
                    "start-map omit 0x0187 character_option %s" % src_0187
                )

        # 热键：C1/00C6 文本落库 → 进房 S0/01C7 Stage3 文本回放
        # IDA 2026-07-22 sub_164D2F0：u8 key_type + u32 text_len + Ixx\\tvk 文本
        # IDA 2026-07-28 sub_1877950：job10 Creator 用 key_type=1 选择 Creator .co
        # （禁止 S4A12 u16[] 当 01C7；禁止 0x01C6 当热键 NOTI）
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_01C7_HOTKEY", True):
            _hk = load_chain("opcode00c6-save_hotkey_option.py")
            resolve_hotkey_for_character = _hk.resolve_hotkey_for_character
            cid_hk = 0
            try:
                cid_hk = int(view.character_id)
            except Exception:
                cid_hk = 0
            if cid_hk <= 0 and session is not None:
                try:
                    cid_hk = int(getattr(session, "selected_character_id", 0) or 0)
                except Exception:
                    cid_hk = 0
            account_id_hk = int(getattr(self.server, "account_id", 1) or 1)
            slots_hk = None
            key_type_hk = 0
            body_01c7 = b""
            src_hk = "defaults"
            try:
                (
                    slots_hk,
                    key_type_hk,
                    body_01c7,
                    src_hk,
                ) = resolve_hotkey_for_character(
                    self.server._repo(),
                    cid_hk,
                    account_id=account_id_hk,
                    character_job=character_job,
                )
            except Exception as exc:
                slots_hk = _hk.default_hotkey_slots_for_job(character_job)
                key_type_hk = _hk.hotkey_key_type_for_job(character_job, 0)
                body_01c7 = _hk.build_hotkey_option_noti_body(
                    key_type_hk, slots_hk, stage3_text=True
                )
                src_hk = "defaults_exc=%r" % (exc,)
            delayed.append(
                (
                    self.server._codec_response(0, 0x01C7, body_01c7),
                    "start-map delayed 0x01C7 hotkey src=%s key_type=%s slots=%s plain_len=%s"
                    % (
                        src_hk,
                        int(key_type_hk),
                        len(slots_hk or []),
                        len(body_01c7),
                    ),
                )
            )
        elif _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_01C6_HOTKEY", False):
            # 实验门：已知闪退，默认关；仅 A/B 时手动开
            _hk = load_chain("opcode00c6-save_hotkey_option.py")
            slots_hk = _hk.default_hotkey_slots()
            body_01c6 = _hk.build_stage3_hotkey_text_core(slots_hk)
            delayed.append(
                (
                    self.server._codec_response(0, 0x01C6, body_01c6),
                    "start-map delayed 0x01C6 EXPERIMENTAL text plain_len=%s"
                    % (len(body_01c6),),
                )
            )

        # S0/043E restores this character's unlocked and displayed closet sets.
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_SEND_1086_AVATAR_CLOSET", True):
            try:
                closet_state = repo.get_avatar_closet_state(character_id)
                unlocked_sets = int(closet_state["unlocked_set_count"])
                current_set = int(closet_state["current_set_index"])
            except Exception as closet_exc:
                unlocked_sets = _env_int(
                    "USDOF_GAME_AVATAR_CLOSET_1086_METRIC",
                    2,
                )
                current_set = _env_int(
                    "USDOF_GAME_AVATAR_CLOSET_1086_FIELD",
                    0,
                )
                notes.append("avatar_closet_state_fail:%s" % closet_exc)
            body_1086 = build_avatar_closet_set_noti(unlocked_sets, current_set)
            delayed.append(
                (
                    self.server._codec_response(0, 0x043E, body_1086),
                    "start-map delayed 0x043E/1086 avatar_closet_set "
                    f"unlocked={unlocked_sets} current={current_set} "
                    f"plain_len={len(body_1086)}",
                )
            )

        # 新角色首进教程：
        #   推荐 Phase-T1：跳过房间 paint → 角色包后 delayed 末尾 000F+grant。
        #   旧 hybrid GRANT：与 0016/0017/0018 同批 001C+001D — 卡死，日常关。
        #   AFTER_0023 开启时永不 hybrid。
        # 教程回城坐标：钉死当前城镇赛丽亚（leave 用 pre_select）。
        if tutorial_enter and tutorial_dungeon_id > 0:
            try:
                _tut_flow.force_tutorial_return_spawn(session, town_id=town_id)
                notes.append(
                    "tutorial_return_spawn town=%s area=%s pos=(%s,%s)"
                    % (
                        int(getattr(session, "pre_select_town_id", town_id) or town_id),
                        int(getattr(session, "pre_select_area_id", area_id) or 0),
                        int(getattr(session, "pre_select_pos_x", pos_x) or 0),
                        int(getattr(session, "pre_select_pos_y", pos_y) or 0),
                    )
                )
            except Exception as spawn_exc:
                notes.append("tutorial_return_spawn_fail:%r" % (spawn_exc,))

        if (
            tutorial_enter
            and tutorial_dungeon_id > 0
            and _tut_helper.should_use_hybrid_grant()
        ):
            notes.append(
                "WARNING hybrid_tutorial_grant enabled "
                "(START delayed 001C+001D; may freeze town)"
            )
            try:
                grant_pkts, grant_note = self.server.dungeon_enter_000f_0010.grant_by_dungeon_id(
                    session=session,
                    dungeon_id=int(tutorial_dungeon_id),
                    difficulty=0,
                    source="newbie_first_enter_hybrid",
                )
                if grant_pkts:
                    for i, pkt in enumerate(grant_pkts):
                        delayed.append(
                            (
                                pkt,
                                "start-map delayed hybrid_tutorial_grant[%d/%d] dungeon=%s"
                                % (i + 1, len(grant_pkts), tutorial_dungeon_id),
                            )
                        )
                    try:
                        ok_state = _tut_helper.mark_tutorial_triggered(
                            repo, int(view.character_id)
                        )
                        notes.append(
                            "tutorial_state->2 hybrid persist=%s dungeon=%s job=%s"
                            % (
                                "ok" if ok_state else "fail",
                                tutorial_dungeon_id,
                                int(getattr(view, "job", 0) or 0),
                            )
                        )
                    except Exception as state_exc:
                        notes.append("tutorial_state_persist_fail:%r" % (state_exc,))
                    try:
                        session.tutorial_hybrid_granted = True
                        session.tutorial_armed = False
                        session.tutorial_phase_t1_done = True
                    except Exception:
                        pass
                    notes.append(
                        "hybrid_tutorial_grant n=%s dungeon=%s %s"
                        % (len(grant_pkts), tutorial_dungeon_id, grant_note)
                    )
                else:
                    try:
                        _tut_helper.mark_tutorial_triggered(repo, int(view.character_id))
                    except Exception:
                        pass
                    notes.append(
                        "hybrid_tutorial_grant empty dungeon=%s %s (state->2 anyway)"
                        % (tutorial_dungeon_id, grant_note)
                    )
            except Exception as tut_exc:
                try:
                    _tut_helper.mark_tutorial_triggered(repo, int(view.character_id))
                except Exception:
                    pass
                notes.append("hybrid_tutorial_grant_fail:%r (state->2)" % (tut_exc,))
        elif tutorial_enter and tutorial_dungeon_id > 0:
            # 默认：角色 setup delayed 之后直接 Phase-T1（跳过房间展示）
            try:
                t1_tagged, t1_note = _tut_flow.build_phase_t1_delayed_for_start(
                    self.server,
                    session=session,
                    character_id=int(view.character_id),
                )
                if t1_tagged:
                    delayed.extend(t1_tagged)
                    notes.append(
                        "tutorial_phase_t1_queued_after_setup n=%s dungeon=%s; %s"
                        % (len(t1_tagged), tutorial_dungeon_id, t1_note)
                    )
                else:
                    notes.append(
                        "tutorial_phase_t0_armed dungeon=%s wait=C1/0023_fallback "
                        "skip_room=%s; %s"
                        % (tutorial_dungeon_id, int(tutorial_skip_room), t1_note)
                    )
            except Exception as t1_exc:
                notes.append(
                    "tutorial_phase_t1_queue_fail:%r wait=C1/0023_fallback dungeon=%s"
                    % (t1_exc, tutorial_dungeon_id)
                )

        if raid_waiting_spawn and _env_bool(
            "USDOF_RESTORE_RAID_WAIT_NOTICE_04CE", False
        ):
            # Native 02BE3F10 consumes one ignored u8 and stamps manager+194.
            # Without it, 011995D0 keeps the search UI blocked indefinitely.
            delayed.append(
                (
                    self.server._codec_response(0, 0x04CE, b"\x00"),
                    "start-map delayed 0x04CE raid search ready notice u8=00",
                )
            )

        from game.raid.clear_quota import entrance_body, ledger_for, N_RAID_ENTRANCE_INFO
        from time import time as quota_time
        delayed.append((self.server._codec_response(0, N_RAID_ENTRANCE_INFO,
            entrance_body(ledger_for(self.server), selected.character_id, self.server.account_id, quota_time())),
            "start-map delayed 0x0519 character daily/weekly clear quotas"))

        if delayed:
            # 时序真源：0131 即时 → delay → town sync → U0 → 0165 → optional 0013
            # → U1 → 000E → 000D/list36 → guild-status → optional Dark Knight 01C0 → 其余
            # BURST 会把 delayed 并进 SELECT 即时回包，与 0131 同 TCP 批：
            #   - type1 查主对象失败则整段跳过且不回 0002 PIV
            #   - 000E list3 在 166AED0 建槽前到达则静默空跑 → 装备栏概率空
            use_burst = _env_bool("USDOF_GAME_ENTER_SERIA_START_BURST_AFTER_START", False)
            has_userinfo_or_equip = any(
                (
                    "userinfo_subtype" in note
                    or "guild_medal_full_snapshot" in note
                    or "equip_166aed0" in note
                    or "after_USERINFO1_166aed0" in note
                    or "equip_bar" in note
                    or "0x0002" in note
                    or "0x000E" in note
                    or "0x000e" in note
                )
                for _pkt, note in delayed
            )
            if use_burst and has_userinfo_or_equip:
                use_burst = False
                notes.append(
                    "burst_disabled_for_equip_order "
                    "(force timed equip-safe delayed order; "
                    "see AFTER_0131_*_DELAY_MS)"
                )
            if use_burst:
                packets.extend(p for p, _n in delayed)
                notes.append(f"burst delayed count={len(delayed)}")
            else:
                # enter-seria-after-0131：game_server 走 AFTER_0131 delay/interpacket，
                # 并在 U1→000E 之间再加 AFTER_USERINFO1_BEFORE_000E_DELAY_MS。
                tagged = [
                    (pkt, f"enter-seria-after-0131; enter-seria-start-map-delayed; {note}")
                    for pkt, note in delayed
                ]
                self.server.queue_delayed_response_packets(tagged)
                order = (
                    (
                        "townsync→U0→0165→0013→U1→000E→000D-list36→guild-status→DK01C0→rest"
                        if dark_knight_combo_body is not None
                        else "townsync→U0→0165→0013→U1→000E→000D-list36→guild-status→rest"
                    )
                    if skillinfo_before_userinfo1
                    else (
                        "townsync→U0→0165→U1→000E→000D-list36→guild-status→0013→DK01C0→rest"
                        if dark_knight_combo_body is not None
                        else "townsync→U0→0165→U1→000E→000D-list36→guild-status→0013→rest"
                    )
                )
                notes.append(f"queued_delayed={len(tagged)} timed-after-0131 order={order}")

        note = (
            "SELECT_CHARACTER START_MAP path "
            f"requested_slot={requested_slot} resolved_slot={resolved_slot} source={parse_source} "
            f"-> character_id={selected.character_id} name={selected.display_name!r} "
            f"job={selected.job} level={selected.level} "
            f"login_spawn_town={town_id} area={area_id} pos=({pos_x},{pos_y}) "
            f"direction={direction}; "
            f"db_saved_before=({getattr(selected, 'town_id', None)},{getattr(selected, 'area_id', None)},"
            f"{getattr(selected, 'pos_x', None)},{getattr(selected, 'pos_y', None)}); "
            f"{fallback_note}{native_plan_note}; {wish_note}; enter_game_pending=1; "
            + "; ".join(notes)
            + "; chain_file=game/chains/flow-start_map_enter.py; success_signal=C1/0023 gate coords"
        )
        return packets, note
