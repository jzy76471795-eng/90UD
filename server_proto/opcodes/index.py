# -*- coding: utf-8 -*-
"""Game C->S opcode 唯一路由表。

``game_server.py`` 负责 socket、拆包、解码和统一日志；本文件只根据
flag/opcode 分派到对应处理。

约定：
1. 新增 opcode = 在 ``opcodes/`` 新增 ``opcode{hex}-{desc}.py`` + 本文件登记
2. 本文件只做路由，不拼业务包体
3. 领域逻辑放 ``game/quest|character|dungeon/``，不进本目录
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from game.chain_loader import load_chain

GameSessionState = load_chain("flow-enter_game.py").GameSessionState
_character_flow_branch = load_chain("flow-character_native_branch.py")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


@dataclass
class GameRouteResult:
    """一次动态 Game 请求路由结果。"""

    handled: bool
    response_packets: list[bytes]
    note: str
    create_character_userinfo_refresh_pending: bool


# 已登记、由本路由表接管的 C->S opcode（flag==1）
ROUTED_OPCODES: frozenset[int] = frozenset(
    {
        0x005E, 0x005F, 0x0060, 0x0086, 0x013B, 0x0144,  # native mailbox
        0x0497,  # authenticated native four-group Coupon redemption
        0x00B6, 0x00B7, 0x00B8, 0x00B9, 0x00BA, 0x00BB, 0x00BC, 0x00BD, 0x014E,  # auction
        0x02AC,  # authenticated high-value gold-spend confirmation
        0x0004,
        0x0087, 0x01FE,  # native character rename submit / activate
        0x0005,
        0x0006,
        0x0007,
        0x0008,
        0x000C, 0x000D,  # authenticated ordinary-channel party create/leave
        0x000F,
        0x0010,
        0x0011, 0x0298,  # authenticated native chat and raid notice
        0x02B7,  # authenticated native raid skill request
        0x0308,  # C->S 红色引导进本（无主线时；复用 0010 grant）
        0x0012,
        0x02CC,  # NPC shop purchase count snapshot
        0x0013,
        0x0014,  # C->S SORT_ITEM（背包/个人金库/账号金库持久化整理）
        0x0015,  # C->S BUY_ITEM（与 S->C ACCEPTABLE_QUEST 同号不同向）
        0x0016,  # C->S SELL_ITEM（与 S->C USER_POSITION 同号不同向）
        0x0019,  # C->S COMPOUND_ITEM（PVF [recipe] 制作）
        0x001A,  # C->S DISJOINT_ITEM（与 S->C 副本 UDP host 同号不同向）
        0x001B,  # C->S open PVF [legacy]/[upgradable legacy] lottery item
        0x001C,
        0x001D,
        0x001E,  # C->S INCREASE_STATUS (Main-slot EXP/SP/TP consumables)
        0x002A,
        0x002B,
        0x002C,  # C->S USE_STACKABLE
        0x022E,  # C->S CARGO_TRANSPORT_STONE
        0x002F,
        0x0084,
        0x001F,
        0x0020,
        0x0021,
        0x0022,
        0x0025,
        0x0026,
        0x0027,
        0x0028,  # C->S DIE_CHARACTER: arms the revive-state transition
        0x0029,  # C->S USE_REVIVE_COIN: u16 pending actor id
        0x0023,
        0x0024,
        0x002D,
        0x002E,
        0x0040,  # C->S Cera-shop purchase (PVF commodity -> item)
        0x0043,  # C->S guild member-list request; S1 initializes guild panel
        0x008C,  # C->S complete guild member-list request; S1 shares 0043 reader
        0x0045,  # C->S score-scroll timer elapsed; drives ordinary auto flip
        0x0047,
        0x0048,
        0x0050,  # C->S equipment/amplification/guild-medal reinforcement
        0x0063,  # C->S compound two same-part list1 avatars with a normal binder
        0x0064,  # C->S rename the equipped creature with a list7 rename card
        0x0066,  # C->S hatch a creature egg using PVF [output index]
        0x008F,
        0x009C,  # C->S guild create-screen name availability check
        0x009F,  # C->S death-tower floor start/complete report
        0x00FF,  # C->S altar cleared-wave monster sequence list
        0x0100,  # C->S compound 2-5 same-grade avatar emblems
        0x0102,  # C->S altar map/UI ready; empty body
        0x010B,  # C->S protect one equipment item immediately
        0x010C,  # C->S remove one equipment protection immediately
        0x0133,  # C->S account-cargo money deposit; exact u32 amount
        0x0134,  # C->S account-cargo money withdrawal; exact u32 amount
        0x013D,  # C->S ultimate altar selection; u8 value 1=A or 2=B
        0x00A0,  # C->S open direct PVF [booster] item
        0x00BF,  # C->S DUNGEON_EVENT_STORY_PAUSE（剧情对话暂停；回 S0/00AA）
        0x00C5,
        0x00C6,  # C->S 热键整表 SAVE_HOTKEY_OPTION（Stage3 文本 Ixx\\tval）
        0x00C9,  # C->S insert avatar emblems into opened sockets
        0x00CA,  # C->S recast one list1 avatar into emblem rewards
        0x00CD,  # C->S PVF red-letter invest/reinvest/Pure Gold option
        0x00D0,  # C->S open PVF [multi upgradable legacy] random box
        0x00ED,  # C->S town teleport item; S0/0018 enters destination Seria room
        0x00EE,  # C->S PVF equipment design recipe with preserved instance state
        0x0110,  # C->S bead enchantment (PVF restrictions + replacement)
        0x014B,  # C->S 技能释放命令自定义（全局 codec passthrough 明文）
        0x014C,  # C->S 技能释放命令全部默认
        0x015C,  # C->S guild-join application request
        0x015E,  # C->S approve one pending guild-join application
        0x0160,  # C->S pending guild-join application list
        0x01B7,  # C->S ITEM_DICTIONARY（Stage3 request-vector probe）
        0x01C0,  # C->S 角色选项/技能锁定大包
        0x01FD,  # C->S Dark Knight 独有连击技能页完整保存
        0x053B,  # C->S Aiken individual equipment-option adjustment
        0x053C,  # C->S Aiken dynamic part-set assignment
        0x053D,  # C->S Aiken equipment-grade upgrade
        0x0547,  # C->S Stage3 saved wish equipment list
        0x0127,
        0x0191,  # C->S UNSEAL_RANDOM_OPTION（魔法封印开启）
        0x0197,  # C->S REGENERATION_RANDOM_OPTION（装备合成）
        0x01B6,  # C->S CHANGE_RANDOM_OPTION（魔法封印单条属性转换）
        0x01A2,  # C->S PVF [level up ticket] character level-up
        0x01CA,  # C->S Shonan tournament reward select-state (empty body)
        0x01CB,  # C->S Shonan tournament reward select (u8 type + u8 card)
        0x01CC,  # C->S selected avatar ability adjustment
        0x019C,  # C->S TITLE_BOOK PUT（装备栏→称号簿）
        0x019D,  # C->S TITLE_BOOK GET（称号簿→装备栏）
        0x01BA,
        0x01BB,  # C->S start one account-wide adventure dispatch by u8 area
        0x01DE,
        0x01EC,  # C->S 技能窗：mode0=SKILL_INIT / mode1=AUTO_SKILL
        0x0204,  # C->S use one PVF-backed stackable action
        0x0207,  # C->S open PVF usable Cera avatar package
        0x0211,  # C->S special-dungeon conditional monster summon
        0x0218,  # C->S cube-contract crystal selection (00 + 0..5/FF)
        0x0233,  # C->S fatigue-burning activity state change (current u8 state)
        0x0292,  # C->S CHANGE_DECK_INFO（骑士盾 1 主 4 副整 deck）
        0x0311,  # C->S 技能窗 skill_ui_refresh 回声（18ADC30 发；最小 ACK）
        0x0313,  # C->S Epic Book craft target (exact u32 equipment id)
        0x0315,  # C->S 主线区域完成（u32 quest_id；无任务奖励）
        0x0342,  # C->S compound 2-4 same-rarity guardian gems
        0x035F,  # C->S compound 2-4 same-rarity guild medals
        0x033C,  # C->S insert one guardian gem into worn guild medal socket 0..3
        0x04B5,  # C->S Neo Premium Plus rental-weapon selection
        0x04B6,  # C->S Neo Premium Plus daily-gift claim
        0x0507,  # C->S select one owned damage font by u32 font id
        0x02A8,
        0x02E7,  # C->S current account guild associations
        0x02E8,  # C->S guild summary lookup by name
        0x02E2,  # C->S guild-create invite-auth text check
        0x02B3,  # C->S guild invite-auth request; retain trace without an unverified S2C snapshot
        0x02E4,  # C->S guild-create authentication target character name
        0x02E5,  # C->S guild-create authentication invite request
        0x02E6,  # C->S guild-create authentication target acceptance
        0x02B5,
        0x02B6,  # C->S Another Arad 随机历史副本：u32 dungeon + u32 crack quest
        0x02BC,  # C->S Daily Challenge group reward: u32 zero-based group index
        0x02C1,
        0x02CA,  # C->S Ctrl+left-click sets one main-bag equipment sort lock
        0x02CB,  # C->S Ctrl+left-click clears one main-bag equipment sort lock
        0x0355,
        0x0356,  # C->S special-dungeon selector state request (u16 player UID)
        0x0357,  # C->S special-dungeon selector records request (u16 player UID)
        0x035E,  # C->S open the aura-skin appearance slot (u32 sentinel)
        0x0453,  # C->S select one closet set for the UI/mannequin display
        0x0454,  # C->S exchange one fixed-slot closet set with worn avatars
        0x0458,  # C->S 90US weekly attendance compatibility request
        0x0578,
        0x057C,  # C->S return the active adventure dispatch
        0x057D,  # C->S claim completed dispatch rewards for one u32 character
        0x057E,  # C->S refresh current character mercenary points
        0x057F,
        0x0583,  # C->S buy one u32 adventure point-shop content id
    }
)


def route_dynamic_game_packet(server: Any, **kwargs) -> GameRouteResult:
    """Keep the native dungeon handler inside the raid's owned request scope."""
    opcode = kwargs['opcode']
    if kwargs['flag'] != 1 or opcode not in (0x0007, 0x000F, 0x0010, 0x0025, 0x0027,
            0x0028, 0x0029, 0x002D, 0x002E, 0x002A, 0x0048, 0x0084, 0x057F):
        return _route_dynamic_game_packet(server, **kwargs)
    from game.raid.local_bindings import local_raid_hooks
    hooks = local_raid_hooks(server)
    if hooks is None:
        return _route_dynamic_game_packet(server, **kwargs)
    from game.raid.squad_runs import route as route_squad
    squad_result = route_squad(server, opcode, kwargs['plain'], kwargs['session'])
    if squad_result is not None:
        packets, note = squad_result
        return GameRouteResult(True, packets, note, kwargs['create_character_userinfo_refresh_pending'])
    navigation = 0
    raw = bytes(kwargs['plain']) if opcode == 0x0048 else b''
    session = kwargs['session']
    if (opcode == 0x0048 and len(raw) in (2, 8, 16) and raw[:1] == b'\x01'
            and raw[1] in (1, 2) and getattr(session, 'selected_channel_id', None) in (23, 32)
            and (int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2
                 or raw[1] == 2 and getattr(hooks, 'has_rejected_entry', lambda _session: False)(session))
            and hooks.battle.raid_for_session(session) is not None):
        # 00FEE850: the raid board uses0048(1,1) to enter and (1,2) to
        # return. This is distinct from both a portal and settlement EPLP.
        navigation = raw[1]
        if navigation == 2:
            opcode = 0x0084
            kwargs = dict(kwargs, opcode=opcode, plain=b'')
    original = None
    def ordinary(body, *, session):
        nonlocal original
        original = _route_dynamic_game_packet(server, **dict(kwargs, plain=body, session=session))
        return original.response_packets, original.note
    packets, note = hooks.handle_opcode(opcode, ordinary, kwargs['plain'], session=kwargs['session'])
    squad = getattr(server, 'raid_squad_runs', None)
    if squad is not None and opcode in (0x0084, 0x002A):
        squad.confirm_town(kwargs['session'])
    if navigation and not note.startswith('raid_transport reject'):
        if navigation == 1 or packets:
            # 015F83B0 consumes success, committed state and destination.
            packets = [server._codec_response(1, 0x0048, bytes((1, 1, navigation))), *packets]
            note = 'native raid navigation action%d; %s' % (navigation, note)
    session = kwargs['session']
    if (opcode == 0x0010 and not packets and note.startswith('raid_transport reject')
            and session is not None
            and ((not getattr(session, 'in_dungeon_run', False) and hooks.bridge.current(session) is None)
                 or hooks.has_native_entry_intent(session))):
        # 015FD8B0 releases SELECT's pending UI on failure and may send 0084.
        # 015FC200 reads one extra selector byte even for its simple error13.
        # Without an explicit navigation intent, an owned duplicate keeps its UI.
        packets = [server._codec_response(1, 0x0010, b'\x00\x13\x00')]
        note += '; native SELECT failure13 returned'
    if (note.startswith('raid_transport reject') or opcode in (0x0028, 0x0029, 0x002A)
            or opcode == 0x0084
            and getattr(session, 'selected_channel_id', None) in (23, 32)):
        logger = getattr(server, 'log', None)
        if callable(logger):
            tag = 'RaidDungeon' if getattr(session, 'selected_channel_id', None) in (23, 32) else 'DungeonLife'
            logger('[%s] opcode=%04X cid=%s %s' % (
                tag, opcode, getattr(session, 'selected_character_id', None), note))
    pending = (original.create_character_userinfo_refresh_pending if original is not None
               else kwargs['create_character_userinfo_refresh_pending'])
    return GameRouteResult(original.handled if original is not None else True, packets, note, pending)


def _route_dynamic_game_packet(
    server: Any,
    *,
    flag: int,
    opcode: int,
    plain: bytes,
    session: GameSessionState,
    create_character_userinfo_refresh_pending: bool,
    charselect_0004_sent: bool,
) -> GameRouteResult:
    """把 C->S Game 包路由到对应处理。

    迁移期仍经 GameServer 薄包装 ``_handle_*`` 调用各 ``opcode*.py`` /
    领域 flow；登记与分支必须集中在本文件，禁止再往 ``game_server`` 堆
    ``if opcode == ...``。
    """
    pending = create_character_userinfo_refresh_pending
    if flag != 1:
        return GameRouteResult(False, [], "", pending)

    if opcode in (0x0087, 0x01FE):
        module = load_chain("opcode0087-change_character_name.py" if opcode == 0x0087
                            else "opcode01fe-use_rename_card.py")
        packets, note = module.handle(server, bytes(plain), session=session)
        return GameRouteResult(True, packets, note, pending)

    if opcode == 0x0004:
        from game.character.character_rename import pending_ids
        repo = server._repo()
        ids = pending_ids(repo.db_path)
        if ids:
            from native_core_loader import plan_select_character
            records = repo.list_by_account(server.account_id)
            _, _, selected, _ = plan_select_character(bytes(plain), range(len(records)))
            if 0 <= selected < len(records) and int(records[selected].character_id) in ids:
                return GameRouteResult(True,
                    [server._codec_response(1, 0x0004, b"\0\x02")],
                    "SELECT_CHARACTER reject=rename_pending", pending)

    # C0002 supplies ordinary-party addresses too; raid feature switches must
    # not disable the authenticated address-report route.
    if opcode == 0x0002:
        _udp_report = load_chain("opcode0002-set_udp_ip_port.py")
        packets, note = _udp_report.handle_udp_address_report(server, bytes(plain), session)
        return GameRouteResult(True, packets, note, pending)

    if opcode in (0x0011, 0x0298):
        from game.native_chat import route
        packets, note = route(server, opcode, plain, session)
        return GameRouteResult(True, packets, note, pending)

    from game.raid.lobby_bindings import try_route as route_raid_lobby
    lobby_result = route_raid_lobby(server, opcode, plain, session)
    if lobby_result is not None:
        packets, note = lobby_result
        return GameRouteResult(True, packets, note, pending)

    from game.normal_party import route as route_normal_party
    normal_party_result = route_normal_party(server, opcode, plain, session)
    if normal_party_result is not None:
        packets, note = normal_party_result
        return GameRouteResult(True, packets, note, pending)

    from game.raid.local_bindings import enabled as raid_bridge_enabled
    if raid_bridge_enabled():
        from game.raid.router import is_raid_command, route_raid_packet
        if is_raid_command(opcode, plain, session):
            packets, note = route_raid_packet(server, opcode, plain, session)
            return GameRouteResult(True, packets, note, pending)

    if (
        type(flag) is int
        and type(opcode) is int
        and opcode == 0x033E
        and _env_bool("USDOF_RESTORE_RAID_EMPTY_LIST_02F2", False)
    ):
        _raid_empty_list = load_chain("opcode033e-raid_empty_list.py")
        raid_result = _raid_empty_list.try_handle_raid_empty_list_033e(
            server, flag=flag, plain=plain, session=session
        )
        if raid_result is not None:
            packets, note = raid_result
            return GameRouteResult(True, packets, note, pending)

    if (
        opcode == 0x0299
        and _env_bool("USDOF_EXPERIMENT_RAID_CREATE_ACK_0299", False)
    ):
        _raid_create_probe = load_chain("opcode0299-raid_create_ack_probe.py")
        probe_result = _raid_create_probe.try_handle_raid_create_ack_probe_0299(
            server, flag=flag, opcode=opcode, plain=plain, session=session
        )
        if probe_result is not None:
            packets, note = probe_result
            return GameRouteResult(True, packets, note, pending)
        # Probe opt-in must not fall through to a generic success receipt.
        return GameRouteResult(False, [], "", pending)

    if (
        opcode == 0x054E
        and _env_bool("USDOF_EXPERIMENT_RAID_MEMBER_REFRESH_054E", False)
    ):
        _raid_member_refresh = load_chain("opcode0299-raid_create_ack_probe.py")
        refresh_result = _raid_member_refresh.try_handle_raid_member_refresh_probe_054e(
            server, flag=flag, opcode=opcode, plain=plain, session=session
        )
        if refresh_result is not None:
            packets, note = refresh_result
            return GameRouteResult(True, packets, note, pending)
        # Enabled probe refusal remains silent and cannot become generic success.
        return GameRouteResult(False, [], "", pending)

    if opcode == 0x02AC:
        confirmation = load_chain("opcode02ac-confirm_gold_trade.py")
        packets, note = confirmation.handle_confirm_gold_trade_02ac(
            server, plain, session=session,
        )
        return GameRouteResult(True, packets, note, pending)

    if opcode in (0x00B6,0x00B7,0x00B8,0x00B9,0x00BA,0x00BB,0x00BC,0x00BD,0x014E):
        from game.native_auction import route
        packets,note=route(server,opcode,plain,session)
        return GameRouteResult(True,packets,note,pending)

    if opcode in (0x005E,0x005F,0x0060,0x0086,0x013B,0x0144):
        if opcode == 0x0060:
            from game.native_auction import before_mailbox
            before_mailbox(server,session)
        from game.native_mailbox import route
        packets,note=route(server,opcode,plain,session)
        return GameRouteResult(True,packets,note,pending)

    if opcode == 0x0497:
        coupon = load_chain('opcode0497-native_coupon.py')
        packets, note = coupon.handle(server, plain, session=session)
        return GameRouteResult(True, packets, note, pending)

    if opcode not in ROUTED_OPCODES:
        # 未知 C->S：仅兜底回执，禁止提前（角色页/握手 silent）。
        # 见 opcodes/helper-unknown_receipt.py
        _unknown = load_chain("helper-unknown_receipt.py")
        if _unknown.should_auto_receipt(flag=flag, opcode=opcode, session=session):
            packets, note = _unknown.handle_unknown_receipt(
                server, opcode=opcode, plain=plain
            )
            return GameRouteResult(True, packets, note, pending)
        return GameRouteResult(False, [], "", pending)

    character_branch = _character_flow_branch.dispatch_character_flow(
        opcode,
        plain,
        error_logger=getattr(server, "log", None),
        server=server,
        session=session,
    )
    if character_branch is not None and character_branch.handled:
        branch_pending = character_branch.create_character_userinfo_refresh_pending
        if branch_pending is not None:
            pending = bool(branch_pending)
        return GameRouteResult(
            True,
            list(character_branch.response_packets),
            character_branch.note,
            pending,
        )

    if opcode == 0x0045:
        _ordinary_card_auto = load_chain("flow-card_reward_auto_flip.py")
        if _ordinary_card_auto.is_shonan_tournament_session(session):
            return GameRouteResult(
                False,
                [],
                "C1/0045 excluded=shonan_tournament",
                pending,
            )

    if opcode == 0x000F:
        packets, note = server._handle_enter_select_dungeon_000f(plain, session=session)
    elif opcode == 0x0010:
        packets, note = server._handle_select_dungeon_0010(plain, session=session)
    elif opcode == 0x0308:
        packets, note = server._handle_guide_enter_dungeon_0308(plain, session=session)
    elif opcode == 0x02B6:
        _crack = load_chain("opcode02b6-crack_of_dimension.py")
        packets, note = _crack.handle_crack_of_dimension_02b6(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x02BC:
        _daily_challenge = load_chain("opcode02bc-daily_challenge_reward.py")
        packets, note = _daily_challenge.handle_daily_challenge_reward_02bc(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x02CA:
        _sort_lock = load_chain("opcode02ca-toggle_sort_item_lock.py")
        packets, note = _sort_lock.handle_set_sort_item_lock_02ca(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x02CB:
        _sort_unlock = load_chain("opcode02cb-clear_sort_item_lock.py")
        packets, note = _sort_unlock.handle_clear_sort_item_lock_02cb(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x010B:
        _equipment_lock = load_chain("opcode010b-lock_equipment.py")
        packets, note = _equipment_lock.handle_lock_equipment_010b(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x010C:
        _equipment_unlock = load_chain("opcode010c-unlock_equipment.py")
        packets, note = _equipment_unlock.handle_unlock_equipment_010c(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0043:
        packets, note = server._handle_guild_member_list_0043(
            plain,
            session=session,
        )
    elif opcode == 0x008C:
        packets, note = server._handle_guild_all_member_list_008c(
            plain,
            session=session,
        )
    elif opcode == 0x015C:
        packets, note = server._handle_guild_request_join_015c(
            plain,
            session=session,
        )
    elif opcode == 0x015E:
        packets, note = server._handle_guild_approve_join_015e(
            plain,
            session=session,
        )
    elif opcode == 0x0160:
        packets, note = server._handle_guild_join_list_0160(
            plain,
            session=session,
        )
    elif opcode == 0x02E7:
        packets, note = server._handle_guild_info_of_my_chars_02e7(
            plain,
            session=session,
        )
    elif opcode == 0x02E8:
        packets, note = server._handle_guild_search_for_join_02e8(
            plain,
            session=session,
        )
    elif opcode == 0x02E2:
        packets, note = server._handle_check_guild_create_promote_02e2(plain)
    elif opcode == 0x02B3:
        packets, note = server._handle_guild_invite_auth_02b3(plain)
    elif opcode == 0x02E4:
        packets, note = server._handle_guild_create_target_02e4(
            plain,
            session=session,
        )
    elif opcode == 0x02E5:
        packets, note = server._handle_guild_create_permit_02e5(
            plain,
            session=session,
        )
    elif opcode == 0x02E6:
        packets, note = server._handle_guild_create_permit_reply_02e6(
            plain,
            session=session,
        )
    elif opcode == 0x0012:
        packets, note = server._handle_delete_item_0012(plain, session=session)
    elif opcode == 0x0013:
        packets, note = server._handle_move_itemspace_0013(plain, session=session)
    elif opcode == 0x0014:
        packets, note = server._handle_sort_item_0014(plain, session=session)
    elif opcode == 0x0015:
        packets, note = server._handle_buy_item_0015(plain, session=session)
    elif opcode == 0x02CC:
        from game.shop.purchase_limits import handle_purchase_count_request
        packets, note = handle_purchase_count_request(server, plain, session)
    elif opcode == 0x0016:
        packets, note = server._handle_sell_item_0016(plain, session=session)
    elif opcode == 0x0019:
        _compound_item = load_chain("opcode0019-compound_item.py")
        packets, note = _compound_item.handle_compound_item_0019(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x00EE:
        _compound_equipment = load_chain(
            "opcode00ee-compound_equipment_recipe.py"
        )
        packets, note = _compound_equipment.handle_compound_equipment_recipe_00ee(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x001A:
        packets, note = server._handle_disjoint_item_001a(plain, session=session)
    elif opcode == 0x001B:
        packets, note = server._handle_use_lottery_item_001b(
            plain, session=session
        )
    elif opcode == 0x0191:
        packets, note = server._handle_unseal_random_option_0191(
            plain, session=session
        )
    elif opcode == 0x0197:
        _equipment_regeneration = load_chain(
            "opcode0197-regeneration_random_option.py"
        )
        packets, note = (
            _equipment_regeneration.handle_regeneration_random_option_0197(
                server,
                plain,
                session=session,
            )
        )
    elif opcode == 0x01B6:
        _magic_seal_change = load_chain(
            "opcode01b6-change_random_option.py"
        )
        packets, note = _magic_seal_change.handle_change_random_option_01b6(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x01A2:
        _level_up_ticket = load_chain("opcode01a2-level_up_ticket.py")
        packets, note = _level_up_ticket.handle_level_up_ticket_01a2(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x01CA:
        _tournament_state = load_chain("opcode01ca-tournament_reward_select_state.py")
        packets, note = _tournament_state.TournamentRewardSelectState01CAService(
            server
        ).handle(plain, session=session)
    elif opcode == 0x01CB:
        _tournament_select = load_chain("opcode01cb-tournament_reward_select.py")
        packets, note = _tournament_select.TournamentRewardSelect01CBService(
            server
        ).handle(plain, session=session)
    elif opcode == 0x001C:
        packets, note = server._handle_change_skillslot_001c(plain, session=session)
    elif opcode == 0x001D:
        packets, note = server._handle_buy_skill_001d(plain, session=session)
    elif opcode == 0x001E:
        _increase_status = load_chain("opcode001e-increase_status.py")
        packets, note = _increase_status.handle_increase_status_001e(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0084:
        packets, note = server._handle_back_2_village_0084(plain, session=session)
    elif opcode == 0x002A:
        packets, note = server._handle_giveup_game_002a(plain, session=session)
    elif opcode == 0x002B:
        packets, note = server._handle_get_item_002b(plain, session=session)
    elif opcode == 0x002C:
        packets, note = server._handle_use_stackable_002c(plain, session=session)
    elif opcode == 0x022E:
        from game.character.cargo_transport import handle_cargo_transport

        packets, note = handle_cargo_transport(server, plain, session)
    elif opcode == 0x002F:
        packets, note = server._handle_drop_item_002f(plain, session=session)
    elif opcode == 0x0045:
        _score_scroll = load_chain("opcode0045-score_scroll_state.py")
        packets, note = _score_scroll.handle_score_scroll_state_0045(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0047:
        packets, note = server._handle_select_card_0047(plain, session=session)
    elif opcode == 0x0048:
        # 通关结算 EPLP（回城）与进选本 ENTER_PRE 共用 0x0048，按 session 分流
        packets, note = server._handle_opcode_0048(plain, session=session)
    elif opcode == 0x0050:
        packets, note = server._handle_upgrade_item_0050(plain, session=session)
    elif opcode == 0x0063:
        _compound_avatar = load_chain("opcode0063-compound_avatar.py")
        packets, note = _compound_avatar.handle_compound_avatar_0063(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0064:
        _rename_creature = load_chain("opcode0064-rename_creature.py")
        packets, note = _rename_creature.handle_rename_creature_0064(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0066:
        _hatch_creature = load_chain("opcode0066-hatch_creature.py")
        packets, note = _hatch_creature.handle_hatch_creature_0066(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x008F:
        packets, note = server._handle_tutorial_flag_008f(plain, session=session)
    elif opcode == 0x009C:
        packets, note = server._handle_check_guild_name_009c(plain, session=session)
    elif opcode == 0x009F:
        _tower_stage = load_chain("opcode009f-death_tower_stage.py")
        packets, note = _tower_stage.handle_death_tower_stage_009f(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x00FF:
        _altar_clear = load_chain("opcode00ff-altar_wave_clear.py")
        packets, note = _altar_clear.handle_altar_wave_clear_00ff(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0102:
        _altar_ready = load_chain("opcode0102-altar_ready.py")
        packets, note = _altar_ready.handle_altar_ready_0102(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x013D:
        _altar_selection = load_chain("opcode013d-altar_selection.py")
        packets, note = _altar_selection.handle_altar_selection_013d(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x00A0:
        packets, note = server._handle_use_booster_item_00a0(
            plain, session=session
        )
    elif opcode == 0x00BF:
        packets, note = server._handle_dungeon_event_story_pause_00bf(
            plain, session=session
        )
    elif opcode == 0x00C5:
        packets, note = server._handle_save_game_option_00c5(plain, session=session)
    elif opcode == 0x00C6:
        packets, note = server._handle_save_hotkey_option_00c6(plain, session=session)
    elif opcode == 0x00C9:
        packets, note = server._handle_use_avatar_emblem_00c9(
            plain, session=session
        )
    elif opcode == 0x00CA:
        _disjoint_avatar = load_chain("opcode00ca-disjoint_avatar.py")
        packets, note = _disjoint_avatar.handle_disjoint_avatar_00ca(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0100:
        _compound_emblem = load_chain("opcode0100-compound_emblem.py")
        packets, note = _compound_emblem.handle_compound_emblem_0100(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x00CD:
        packets, note = server._handle_invest_amplify_option_00cd(
            plain, session=session
        )
    elif opcode == 0x00D0:
        packets, note = server._handle_use_random_box_item_00d0(
            plain, session=session
        )
    elif opcode == 0x00ED:
        packets, note = server._handle_teleport_00ed(plain, session=session)
    elif opcode == 0x0110:
        packets, note = server._handle_enchant_by_bead_0110(
            plain, session=session
        )
    elif opcode == 0x014B:
        packets, note = server._handle_skill_command_customizing_014b(
            plain, session=session
        )
    elif opcode == 0x014C:
        packets, note = server._handle_skill_command_all_default_014c(
            plain, session=session
        )
    elif opcode == 0x01B7:
        packets, note = server._handle_item_dictionary_01b7(plain, session=session)
    elif opcode == 0x01C0:
        packets, note = server._handle_save_character_option_01c0(
            plain, session=session
        )
    elif opcode == 0x01CC:
        packets, note = server._handle_avatar_option_change_01cc(
            plain, session=session
        )
    elif opcode == 0x01FD:
        packets, note = server._handle_dark_knight_combo_skill_01fd(
            plain, session=session
        )
    elif opcode == 0x053B:
        _customability_option = load_chain(
            "opcode053b-customability_equip_option.py"
        )
        packets, note = (
            _customability_option.handle_customability_equip_option_053b(
                server,
                plain,
                session=session,
            )
        )
    elif opcode == 0x053C:
        _customability_set = load_chain(
            "opcode053c-customability_set_option.py"
        )
        packets, note = _customability_set.handle_customability_set_option_053c(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x053D:
        _customability_upgrade = load_chain(
            "opcode053d-customability_upgrade.py"
        )
        packets, note = _customability_upgrade.handle_customability_upgrade_053d(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0547:
        packets, note = server._handle_update_wish_items_0547(plain, session=session)
    elif opcode == 0x001F:
        packets, note = server._handle_accept_quest_001f(plain, session=session)
    elif opcode == 0x0020:
        packets, note = server._handle_giveup_quest_0020(plain, session=session)
    elif opcode == 0x0021:
        packets, note = server._handle_set_quest_trigger_0021(plain, session=session)
    elif opcode == 0x0022:
        packets, note = server._handle_finish_quest_0022(plain, session=session)
    elif opcode == 0x0315:
        packets, note = server._handle_complete_quest_area_0315(plain, session=session)
    elif opcode == 0x0025:
        packets, note = server._handle_finish_loading_0025(plain, session=session)
    elif opcode == 0x0026:
        packets, note = server._handle_monster_hit_0026(plain, session=session)
    elif opcode == 0x0027:
        packets, note = server._handle_die_monster_0027(plain, session=session)
    elif opcode == 0x0028:
        packets, note = server._handle_die_character_0028(plain, session=session)
    elif opcode == 0x0029:
        packets, note = server._handle_use_revive_coin_0029(plain, session=session)
    elif opcode == 0x002D:
        packets, note = server._handle_move_map_002d(plain, session=session)
    elif opcode == 0x002E:
        packets, note = server._handle_set_play_result_002e(plain, session=session)
    elif opcode == 0x0040:
        packets, note = server._handle_buy_cerashop_item_0040(
            plain, session=session
        )
    elif opcode == 0x0204:
        _damage_font_use = load_chain("opcode0204-use_damage_font.py")
        packets, note = _damage_font_use.handle_use_damage_font_0204(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0207:
        packets, note = server._handle_open_cera_package_0207(
            plain, session=session
        )
    elif opcode == 0x0211:
        _summon_monster = load_chain("opcode0211-summon_monster.py")
        packets, note = _summon_monster.handle_summon_monster_0211(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0218:
        packets, note = server._handle_update_cube_contract_0218(
            plain, session=session
        )
    elif opcode == 0x0233:
        _fatigue_burning = load_chain(
            "opcode0233-fatigue_acceleration_state_change.py"
        )
        packets, note = (
            _fatigue_burning.FatigueAccelerationStateChange0233Service(
                server
            ).handle(plain, session=session)
        )
    elif opcode == 0x01DE:
        packets, note = server._handle_dungeon_scene_uid_01de(plain, session=session)
    elif opcode == 0x01EC:
        packets, note = server._handle_skill_init_01ec(plain, session=session)
    elif opcode == 0x0292:
        packets, note = server._handle_change_deck_info_0292(plain, session=session)
    elif opcode == 0x0311:
        packets, note = server._handle_skill_ui_refresh_0311(plain, session=session)
    elif opcode == 0x0313:
        packets, note = server._handle_epic_book_make_item_0313(
            plain, session=session
        )
    elif opcode == 0x0342:
        _guild_compound = load_chain("opcode0342-compound_guild_item.py")
        packets, note = _guild_compound.handle_compound_guild_item_0342(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x035F:
        _medal_compound = load_chain("opcode035f-compound_guild_medal.py")
        packets, note = _medal_compound.handle_compound_guild_medal_035f(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x033C:
        _guardian_gem = load_chain("opcode033c-use_guardian_gem.py")
        packets, note = _guardian_gem.handle_use_guardian_gem_033c(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x04B5:
        packets, note = server._handle_rent_neo_premium_weapon_04b5(
            plain, session=session
        )
    elif opcode == 0x04B6:
        packets, note = server._handle_claim_neo_premium_gift_04b6(
            plain, session=session
        )
    elif opcode == 0x0507:
        _damage_font_select = load_chain("opcode0507-select_damage_font.py")
        packets, note = _damage_font_select.handle_select_damage_font_0507(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0453:
        _avatar_closet_select = load_chain(
            "opcode0453-select_avatar_closet_set.py"
        )
        packets, note = _avatar_closet_select.handle_select_avatar_closet_set_0453(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0454:
        _avatar_closet_change = load_chain(
            "opcode0454-change_avatar_closet_set.py"
        )
        packets, note = _avatar_closet_change.handle_change_avatar_closet_set_0454(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0023:
        packets, note = server._handle_set_user_position_0023(plain, session=session)
    elif opcode == 0x0024:
        packets, note = server._handle_set_user_area_0024(plain, session=session)
    elif opcode == 0x0127:
        packets, note = server._handle_change_charac_slot(plain)
    elif opcode == 0x019C:
        packets, note = server._handle_title_book_put_019c(plain, session=session)
    elif opcode == 0x019D:
        packets, note = server._handle_title_book_get_019d(plain, session=session)
    elif opcode == 0x02C1:
        packets, note = server._handle_charcard_click_02c1(plain)
    elif opcode == 0x0458:
        attendance = load_chain("opcode0458-weekly_attendance.py")
        packets, note = attendance.handle(server, bytes(plain), session=session)
    elif opcode == 0x0578:
        packets, note = server._handle_contents_play_info_0578(
            plain,
            session=session,
        )
    elif opcode == 0x01BB:
        _adventure_dispatch = load_chain(
            "opcode01bb-start_adventure_dispatch.py"
        )
        packets, note = _adventure_dispatch.handle_start_adventure_dispatch_01bb(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x057C:
        _adventure_return = load_chain(
            "opcode057c-return_adventure_dispatch.py"
        )
        packets, note = _adventure_return.handle_return_adventure_dispatch_057c(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x057D:
        _adventure_reward = load_chain(
            "opcode057d-claim_adventure_dispatch_reward.py"
        )
        packets, note = (
            _adventure_reward.handle_claim_adventure_dispatch_reward_057d(
                server,
                plain,
                session=session,
            )
        )
    elif opcode == 0x057E:
        _mercenary_points = load_chain(
            "opcode057e-refresh_mercenary_points.py"
        )
        packets, note = _mercenary_points.handle_refresh_mercenary_points_057e(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x0583:
        _adventure_shop = load_chain(
            "opcode0583-buy_adventure_shop_item.py"
        )
        packets, note = _adventure_shop.handle_buy_adventure_shop_item_0583(
            server,
            plain,
            session=session,
        )
    elif opcode == 0x057F:
        packets, note = server._handle_channel_move_selecting_scene_057f(plain, session=session)
    elif opcode == 0x02B5:
        packets, note = server._handle_check_name(plain)
    elif opcode == 0x02A8:
        packets, note = server._handle_load_extend_characs(plain)
    elif opcode == 0x01BA:
        packets, note = server._handle_load_extend_characs_finish(
            plain,
            suppress_direct_response=charselect_0004_sent and len(plain) == 0,
        )
    elif opcode == 0x0355:
        if pending:
            packets, note = server._handle_create_character_0355_refresh()
            pending = False
        else:
            packets, note = server._handle_extend_ready_0355(plain)
    elif opcode == 0x0356:
        _special_selector_state = load_chain(
            "opcode0356-special_dungeon_selector_state.py"
        )
        packets, note = _special_selector_state.SpecialDungeonSelectorState0356Service(
            server
        ).handle(plain, session=session)
    elif opcode == 0x0357:
        _special_selector_records = load_chain(
            "opcode0357-special_dungeon_selector_records.py"
        )
        packets, note = (
            _special_selector_records.SpecialDungeonSelectorRecords0357Service(
                server
            ).handle(plain, session=session)
        )
    elif opcode == 0x035E:
        _aura_skin_slot = load_chain("opcode035e-open_aura_skin_slot.py")
        packets, note = _aura_skin_slot.OpenAuraSkinSlot035EService(
            server
        ).handle_open_aura_skin_slot_035e(
            plain,
            session=session,
        )
    elif opcode == 0x0133:
        _deposit_money = load_chain("opcode0133-deposit_money.py")
        packets, note = _deposit_money.handle_deposit_money_0133(
            server, plain, session=session,
        )
    elif opcode == 0x0134:
        _withdraw_money = load_chain("opcode0134-withdraw_money.py")
        packets, note = _withdraw_money.handle_withdraw_money_0134(
            server, plain, session=session,
        )
    elif opcode == 0x0005:
        packets, note = server._handle_create_character(plain)
        if (
            note.startswith("CREATE_CHARACTER stored")
            and not _env_bool("USDOF_CREATE_CHARACTER_SEND_USERINFO_REFRESH", False)
            and _env_bool("USDOF_CREATE_CHARACTER_REFRESH_ON_0355", True)
        ):
            pending = True
    elif opcode == 0x0008:
        packets, note = server._handle_get_userinfo(plain, session=session)
    elif opcode == 0x0006:
        packets, note = server._handle_delete_character(plain)
    elif opcode == 0x0007:
        packets, note = server._handle_return_select_character(plain, session=session)
    else:
        # 0x0004 等选角入口
        packets, note = server._handle_select_character(plain, session=session)

    return GameRouteResult(True, packets, note, pending)
