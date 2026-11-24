"""USDOF local single-player standard launch profile (2026-07-14/15 baseline).

Verified path:
  region cain (bright) -> first PVF channel only (cain-pvf-paired)
  -> charselect -> START_MAP enter Seria
  charselect Select Channel: 057F (no Exit prep)->0323+020C+0244 + reentry; Exit: 00C2/0505/0506+057F->0244 only
  channel->charselect: cold 0001→0004→real 0004 PIV→USERINFO; armed reentry keeps immediate fallback
  START_MAP Seria: S1/0004 262B + init + town-sync 0131/16/17/18/007C + 00AD(from account_settings)
  game option: C->S 0x00C5 persist account_settings; S0/00AD reload on START (opt[74]=1)
  charselect drag slot: C->S 0x0127 swap two occupied slots + USERINFO refresh; slots stay 0..n-1

Do not invent packets here; only env/CLI defaults that select already-proven chains.
Rolepage-only A/B should explicitly disable START_MAP / multi-channel via
pytools/restart_rolepage_server.py overrides.
"""

from __future__ import annotations

import os

from config import (
    DEFAULT_CHANNELINFO_CUR_USERS,
    DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID,
    DEFAULT_CHANNELINFO_MAX_USERS,
    DEFAULT_CHANNELINFO_PROFILE,
    DEFAULT_CHANNELINFO_REGION_NAME,
    DEFAULT_GAME_CLIENT_TIMEOUT,
    DEFAULT_GAME_ENTER_SERIA_START_MAP,
    DEFAULT_SCRIPT_PROFILE,
)

STANDARD_CHANNELINFO_MAX_CHANNELS = 1


def standard_local_flow_env() -> dict[str, str]:
    """Return bare USDOF_* env for the normal single-channel local path."""

    start_map = "1" if DEFAULT_GAME_ENTER_SERIA_START_MAP else "0"
    return {
        # Phase-2 server compute core. Native kernels are now the primary
        # implementation; the compatibility facade retains Python fallback
        # for module/runtime errors without changing packet semantics.
        "USDOF_NATIVE_CORE": "1",
        "USDOF_NATIVE_CORE_FALLBACK": "1",
        "USDOF_NATIVE_CORE_SUMMARY_EVERY": "100",
        # Daily and share sessions measure the complete player-core warmup
        # before listeners start; startup latency must not be hidden behind a
        # listeners-first background phase.
        "USDOF_PVF_BOOT_MODE": "core",
        # Character lifecycle routing always probes the native branch first;
        # unhandled operations continue through the frozen Python handlers.
        "USDOF_CHARACTER_FLOW_CORE": "cpp",
        # Local sessions retain the complete C2S stream and per-packet sequence
        # so a manual client action can always be correlated with its opcode.
        # The Win10/Win11 share launcher overrides both switches back to 0.
        "USDOF_GAME_DIAGNOSTIC_TRACE": "1",
        "USDOF_CODEC_TRACE_LIMIT": "0",
        "USDOF_PACKET_SEQUENCE_LOG": "1",
        "USDOF_GAME_HOTPATH_METRICS": "1",
        "USDOF_GAME_TCP_NODELAY": "1",
        "USDOF_GAME_MAP_TEMPLATE_CACHE": "1",
        # Each business operation still commits before its response. WAL/NORMAL
        # avoids a physical flush on every click; startup/shutdown maintenance
        # uses FULL while recovering and checkpointing the committed WAL.
        "USDOF_SQLITE_JOURNAL_MODE": "WAL",
        "USDOF_SQLITE_SYNCHRONOUS": "NORMAL",
        # Derived PVF indexes remain local to this machine. They are keyed by
        # the complete preloaded Script.pvf SHA-256 and rebuild after changes.
        "USDOF_PVF_INDEX_CACHE": "1",
        "USDOF_PVF_INDEX_CACHE_CLEAR": "0",
        "USDOF_SCRIPT_PROFILE": DEFAULT_SCRIPT_PROFILE,
        "USDOF_CHANNELINFO_PROFILE": DEFAULT_CHANNELINFO_PROFILE,
        "USDOF_CHANNELINFO_MAX_CHANNELS": str(STANDARD_CHANNELINFO_MAX_CHANNELS),
        "USDOF_CHANNELINFO_REGION_NAME": DEFAULT_CHANNELINFO_REGION_NAME,
        "USDOF_CHANNELINFO_APPEND_CHANNEL_ID": "1",
        "USDOF_CHANNELINFO_CUR_USERS": str(DEFAULT_CHANNELINFO_CUR_USERS),
        "USDOF_CHANNELINFO_MAX_USERS": str(DEFAULT_CHANNELINFO_MAX_USERS),
        "USDOF_CHANNELINFO_FIRST_CHANNEL_ID": str(DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID),
        # Must match the channel the client actually entered; used by S->C 0x020C
        "USDOF_GAME_CHANNEL_INDEX": str(DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID),
        # One displayed channel uses the primary Game port directly. This keeps
        # startup independent of unavailable secondary channel ports.
        "USDOF_CHANNELINFO_UNIQUE_PORTS": "0",
        "USDOF_CHANNELINFO_PORT_BASE": "10052",
        "USDOF_GAME_CLIENT_TIMEOUT": str(DEFAULT_GAME_CLIENT_TIMEOUT),
        # Charselect Select Channel (0x057F without Exit prep): settle 0323+020C+0244 + reentry.
        # Exit Game (0x00C2/0x0505/0x0506 then 0x057F): 0244 then 0x0003 SuccessAck; no reentry.
        # Baseline doc: docs/baseline_select_channel_exit_20260714.md (list_count is NOT Exit enum).
        "USDOF_GAME_EXIT_CLOSE_DELAY_MS": "200",
        "USDOF_GAME_CHANNEL_LEAVE_CLOSE_DELAY_MS": "250",
        "USDOF_GAME_EXIT_SETTLE_GAP_MS": "0",
        "USDOF_GAME_057F_SETTLE_PROFILE": "channel",
        "USDOF_GAME_EXIT_SEND_SUCCESS_ACK": "1",
        "USDOF_GAME_EXIT_057F_SEND_0244": "1",
        "USDOF_GAME_EXIT_ACK_MODE": "codec",
        "USDOF_GAME_CHANNEL_LEAVE_SEND_0007": "0",
        "USDOF_GAME_CHANNEL_LEAVE_0007_MODE": "codec",
        "USDOF_GAME_CHANNEL_LEAVE_0007_FLAG": "1",
        "USDOF_GAME_CHANNEL_LEAVE_SEND_SUCCESS_ACK": "0",
        "USDOF_GAME_CHANNEL_LEAVE_SKIP_DUP_EARLY_ACK": "0",
        "USDOF_GAME_CHANNEL_LEAVE_SEND_0244": "0",
        "USDOF_GAME_CHANNEL_MOVE_0323_MODE": "codec",
        "USDOF_GAME_CHANNEL_MOVE_SEND_0323": "1",
        "USDOF_GAME_CHANNEL_MOVE_020C_MODE": "codec",
                "USDOF_GAME_CHANNEL_MOVE_SEND_020C": "1",
        "USDOF_GAME_CHANNEL_MOVE_SEND_0208": "0",
        "USDOF_GAME_CHANNEL_MOVE_0208_MODE": "raw",
        "USDOF_GAME_CHANNEL_MOVE_SEND_0244": "1",
        "USDOF_GAME_CHANNEL_MOVE_SEND_EARLY_0003": "0",
        "USDOF_GAME_CHANNEL_MOVE_EARLY_0003_MODE": "codec",
        "USDOF_GAME_CHANNEL_MOVE_EARLY_0003_FLAG": "1",
        "USDOF_GAME_CHANNEL_MOVE_CLOSE_AFTER_SETTLE": "0",
        "USDOF_GAME_CHANNEL_MOVE_SEND_0007": "0",
        "USDOF_GAME_CHANNEL_MOVE_0007_MODE": "codec",
        "USDOF_GAME_CHANNEL_MOVE_0007_FLAG": "1",
        "USDOF_GAME_CHANNEL_MOVE_SEND_0242": "0",
        "USDOF_GAME_CHANNEL_MOVE_0242_MODE": "codec",
        "USDOF_GAME_CHANNEL_MOVE_ORDER": "0323,020C,0244",
        "USDOF_GAME_CHANNEL_MOVE_0244_MODE": "codec",
        "USDOF_GAME_CHANNEL_MOVE_FLUSH_MS": "0",
        "USDOF_GAME_CHANNEL_MOVE_0244_NEXT_GAP_MS": "40",
        "USDOF_GAME_CHANNEL_MOVE_0323_020C_GAP_MS": "40",
        "USDOF_GAME_REENTRY_USERINFO_FALLBACK": "1",
        # Only used when OPEN_BEFORE_USERINFO=0 (A/B legacy wait). Default open-before
        # skips wait for both cold first entry and armed reentry.
        "USDOF_GAME_REENTRY_USERINFO_WAIT_MS": "350",
        "USDOF_GAME_REENTRY_USERINFO_ARM_TTL_S": "30",
        "USDOF_GAME_REENTRY_USERINFO_REQUIRE_ARM": "0",
        "USDOF_GAME_REENTRY_OPEN_CHARSELECT_ON_01BA": "1",
        "USDOF_GAME_REENTRY_SYNTHETIC_0004_PIV_SEEN": "1",
        # Hard order for channel→role (cold OR reentry; black if USERINFO before open):
        #   opcode1 -> LOGIN 0x0001 -> CHARSELECT 0x0004 -> USERINFO 0x0002
        # Default 1: open 0001->0004 before USERINFO for cold first entry and armed reentry.
        # 2026-07-19 live: cold USERINFO-first → black UI + blind 0x02c1 clicks.
        "USDOF_GAME_REENTRY_OPEN_BEFORE_USERINFO": "1",
        "USDOF_GAME_REENTRY_SEND_USERINFO_AFTER_OPEN": "1",
        # Cold Game connect keeps 0001->0004 adjacent, then waits for the
        # client's real 0004 PIV before USERINFO. Armed channel reentry keeps
        # its immediate fallback because that path may not emit a PIV.
        "USDOF_GAME_COLD_USERINFO_GATE_ON_0004_PIV": "1",
        # S1/0001 also emits native C1/0008 ff ff 02. The cold PIV gate already
        # owns the first roster, so suppress that one duplicate full refresh.
        "USDOF_GAME_COLD_SUPPRESS_DUP_0008_AFTER_USERINFO": "1",
        # Keep 0: any gap between 0001 and 0004 lets client fire C1/0008 before
        # 0004 PIV, and deferred GET_USERINFO blacks the roster (2026-07-19 live).
        "USDOF_GAME_REENTRY_0001_0004_GAP_MS": "0",
        "USDOF_GAME_REENTRY_0004_USERINFO_GAP_MS": "0",
        # Town/world -> charselect (C1/0007):
        # IDA op7 builds type=12 UI itself — do NOT also send S1/0001+0004 (double-open =
        # black screen but clickable roster). S4A12: ACK + proactive USERINFO; Stage3 gates
        # USERINFO until C->S PIV of flag1/0x0007 so teardown finishes first.
        "USDOF_GAME_RETURN_SELECT_0007_MODE": "codec",
        "USDOF_GAME_RETURN_SELECT_OPEN_BEFORE_USERINFO": "0",
        "USDOF_GAME_RETURN_SELECT_SEND_0001": "0",
        "USDOF_GAME_RETURN_SELECT_SEND_0004": "0",
        "USDOF_GAME_RETURN_SELECT_SEND_USERINFO": "1",
        "USDOF_GAME_RETURN_SELECT_USERINFO_FOLLOWUPS": "1",
        "USDOF_GAME_RETURN_SELECT_GATE_USERINFO_ON_0007_PIV": "1",
        "USDOF_GAME_RETURN_SELECT_MARK_CHARSELECT_READY": "1",
        "USDOF_GAME_RETURN_SELECT_SYNTHETIC_0004_PIV_SEEN": "0",
        # 门闩未放完时客户端常再发 C1/0008；抑制避免 0286/01BA 叠层 → 城镇鼠标点不了
        "USDOF_GAME_RETURN_SELECT_SUPPRESS_DUP_0008": "1",
        # 进房前丢掉角色页残留 P1（return-select / 0008-gated followups）
        "USDOF_GAME_ENTER_SERIA_DROP_CHARSELECT_P1_LEFTOVER": "1",
        # Character slot drag (C->S 0x0127): persist swap + refresh roster
        "USDOF_CHANGE_CHARAC_SLOT_SEND_USERINFO_REFRESH": "1",
        "USDOF_CHANGE_CHARAC_SLOT_USERINFO_FOLLOWUPS": "0",
        "USDOF_GAME_ENTER_SERIA_START_MAP": start_map,
        # 所有职业跳过首次教程，直接走正常赛丽亚房间登录。
        # START 同步客户端 flag31，避免客户端继续启动首次教程控制器。
        # 保留教程链供显式测试启用；不改角色存档或任务完成记录。
        "USDOF_GAME_NEWBIE_FIRST_ENTER": "0",
        "USDOF_GAME_TUTORIAL_AFTER_0023": "1",
        "USDOF_GAME_TUTORIAL_SKIP_SERIA_ROOM": "1",  # 仅 NEWBIE 开时生效
        "USDOF_GAME_TUTORIAL_GRANT_AFTER_SETUP": "1",  # 仅 NEWBIE 开时生效
        "USDOF_GAME_TUTORIAL_DUNGEON_GRANT": "0",  # 旧 hybrid，永关
        # 008F：存 flag；flag=31 标教程完成（不自动回城包，靠 leave 链）
        "USDOF_GAME_TUTORIAL_HANDLE_008F": "1",
        "USDOF_GAME_TUTORIAL_008F_SEND_ACK": "1",
        "USDOF_GAME_TUTORIAL_008F_FLAG31_COMPLETE": "1",
        # Quest system (modular opcode chains): accept/giveup/trigger/finish ON
        "USDOF_GAME_QUEST_HANDLE_001F": "1",
        "USDOF_GAME_QUEST_001F_SEND_ACK": "1",
        "USDOF_GAME_QUEST_001F_SEND_FAIL_ACK": "1",
        "USDOF_GAME_QUEST_HANDLE_0020": "1",
        "USDOF_GAME_QUEST_0020_SEND_ACK": "1",
        "USDOF_GAME_QUEST_HANDLE_0021": "1",
        "USDOF_GAME_QUEST_0021_SEND_ACK": "1",
        "USDOF_GAME_QUEST_HANDLE_0022": "1",
        "USDOF_GAME_QUEST_0022_SEND_ACK": "1",
        # Stage3 主线“完成任务”按钮：C1/0315 u32 quest_id，由服务端按 PVF 区域完成。
        "USDOF_GAME_QUEST_HANDLE_0315": "1",
        "USDOF_GAME_QUEST_PERSIST_ACTIVE": "1",
        "USDOF_GAME_QUEST_PERSIST_FINISHED": "1",
        "USDOF_GAME_QUEST_APPLY_ECONOMY": "1",
        "USDOF_GAME_REPLY_ACCEPTED_QUEST_023F": "1",
        # 0164 is a full finished-task bitmap replacement.  Ordinary 0022
        # completion must not overwrite the live task-event/fragment view;
        # daily-claim and explicit area-completion paths send their own
        # authoritative snapshot when required.
        "USDOF_GAME_REPLY_FINISH_QUEST_SEND_0164": "0",
        # Personality and external time-fragment tasks are the first exception:
        # their completed IDs drive native character state and need an
        # immediate full completion projection.
        "USDOF_GAME_QUEST_FINISH_SPECIAL_REWARD_SEND_0164": "1",
        # PVF-backed map/town gate tasks are the second exception. Their
        # completion bits directly release native area and worldmap access.
        "USDOF_GAME_QUEST_FINISH_MAP_UNLOCK_SEND_0164": "1",
        # 放弃后立刻重推可接列表（0015），否则客户端只从进行中移除，要重进角色才能再接
        "USDOF_GAME_REPLY_GIVEUP_QUEST_REFRESH_0015": "1",
        "USDOF_GAME_QUEST_ACCEPT_REQUIRE_ACCEPTABLE": "1",
        "USDOF_GAME_QUEST_FINISH_DEFAULT_EXP": "0",
        "USDOF_GAME_QUEST_FINISH_DEFAULT_GOLD": "0",
        "USDOF_GAME_QUEST_REWARD_FROM_PVF": "1",
        "USDOF_GAME_QUEST_GRANT_REWARDS": "1",
        "USDOF_GAME_QUEST_GRANT_ITEMS": "1",
        "USDOF_GAME_QUEST_GRANT_ACCEPT_ITEMS": "1",
        # 旧 0022/001F 整章实验只保留显式 A/B；日常按钮唯一入口为 0315。
        "USDOF_GAME_QUEST_FINISH_EPIC_CHAIN_NO_REWARD": "0",
        "USDOF_GAME_QUEST_COMPLETE_MAIN_CHAIN_NO_REWARD": "0",
        # 思路.md 6.5：residual trigger 必须先被 0021 清 0 才能交
        "USDOF_GAME_QUEST_FINISH_REQUIRE_TRIGGER0": "1",
        # 多奖励动画（Stage3 IDA 2026-07-18 二次修正）：
        #   真 handler = mapA sub_162C740（不是 mapB play-result sub_1635AF0）
        #   S1/0022 ACK 带 19B insert（S4A12=18B 会错位；90qiyuan=无gold+20B 不可照搬）
        #   S0/000E 默认关（insert 已驱动动画+客户端入包）
        "USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_ITEMS": "1",
        "USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_EXP": "1",
        "USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_GOLD": "1",
        "USDOF_GAME_QUEST_FINISH_INSERT_NOTIFY": "0",
        "USDOF_GAME_QUEST_FINISH_SEND_000E": "0",
        "USDOF_GAME_QUEST_FINISH_000E_INCLUDE_GOLD": "1",
        # 全量 000D 默认关；USERINFO1 仍延迟刷 exp
        "USDOF_GAME_QUEST_FINISH_DELAYED_ITEMLIST": "0",
        "USDOF_GAME_QUEST_FINISH_DELAYED_USERINFO1": "1",
        "USDOF_GAME_QUEST_ACCEPT_ACK_INCLUDE_ITEMS": "1",
        "USDOF_GAME_QUEST_ACCEPT_DELAYED_ITEMLIST": "1",
        # 0021：S1 ACK 已更新 trigger/可交闪烁；默认不再叠 023F（会冲掉动效）
        "USDOF_GAME_QUEST_0021_REFRESH_023F": "0",
        "USDOF_GAME_QUEST_0021_CHANNEL_SEMANTICS": "1",
        "USDOF_GAME_QUEST_0021_FLAG1_MEANS_CLEAR": "1",
        "USDOF_GAME_QUEST_TRIGGER_KEEP_ON_FLAG0": "0",
        # 杀怪计数 / 收集 residual：发 S1/0021 驱动可交闪烁；023F 默认关
        "USDOF_GAME_QUEST_RESIDUAL_KILL": "1",
        "USDOF_GAME_QUEST_RESIDUAL_SEEKING": "1",
        "USDOF_GAME_QUEST_RESIDUAL_SEND_0021": "1",
        "USDOF_GAME_QUEST_RESIDUAL_REFRESH_023F": "0",
        # init_trigger=0 接任务后补 S1/0021；进房可交闪烁改到首次 C1/0023（UI 就绪）
        "USDOF_GAME_QUEST_ACCEPT_ARM_COMPLETEABLE_0021": "1",
        # start-map BURST 阶段城镇 UI 未就绪：默认不在此发 0021（F2 能动、右方/底部仍静）
        "USDOF_GAME_QUEST_START_ARM_COMPLETEABLE_0021": "0",
        # 进房后首次走路/坐标 C1/0023 时 pulse 1→0 点亮右方+底部完成图标动效
        "USDOF_GAME_QUEST_0023_ARM_COMPLETEABLE_0021": "1",
        "USDOF_GAME_QUEST_COMPLETEABLE_0021_PULSE": "1",
        # S1/0004：DB 已接任务写入 30 pairs + 右侧 4 槽 display_ids（quest_mark）
        "USDOF_GAME_ENTER_SERIA_START_QUEST_PAIRS_FROM_DB": "1",
        "USDOF_GAME_ENTER_SERIA_START_QUEST_DISPLAY_FROM_DB": "1",
        # Quest system parity (任务系统思路.md)
        "USDOF_GAME_ACCEPTABLE_QUEST_FROM_PVF": "1",
        "USDOF_GAME_QUEST_BOOT_WARM_ACCEPTABLE": "1",
        "USDOF_GAME_QUEST_LOCAL_UNLOCK_ENABLE": "1",
        "USDOF_GAME_QUEST_LOCAL_UNLOCK_IDS": "",
        # PVF [hell quest] prerequisites, Ep2-Ep4 side-story completion, and
        # Stage3 extended equipment slots are persisted at boot, creation,
        # selection, and before map select. Complete the full Ep1-Ep4 chain
        # for the local default profile.
        "USDOF_GAME_DEFAULT_UNLOCK_HELL": "1",
        "USDOF_GAME_DEFAULT_UNLOCK_HELL_EP1": "1",
        "USDOF_GAME_DEFAULT_UNLOCK_EX_EQUIP": "1",
        "USDOF_GAME_DUNGEON_QUEST_MONSTER_DROP": "1",
        # 任务怪掉落后 S0/000E 实时刷背包（108B 条目；默认 ON）
        "USDOF_GAME_QUEST_DROP_SEND_000E": "1",
        # 地下城怪物材料/道具掉落（多通道并联；非翻牌/非组队）
        "USDOF_GAME_DUNGEON_ITEM_DROP": "1",
        # 可破坏建筑：object.etc 普通池 + DGN 匹配组专属材料。
        "USDOF_GAME_DUNGEON_PASSIVE_OBJECT_DROP": "1",
        # 当前 MAP 普通 [passive object] 的 DGN 固定材料预挂（Stage3 001D 21B 行）。
        "USDOF_GAME_DUNGEON_PASSIVE_OBJECT_ITEM": "1",
        "USDOF_GAME_DUNGEON_INDEPENDENT_DROP": "1",
        "USDOF_GAME_DUNGEON_WORLD_DROP": "1",
        "USDOF_GAME_DUNGEON_MOB_ITEM_DROP": "1",
        "USDOF_GAME_DUNGEON_ITEM_PICKUP": "1",
        "USDOF_GAME_DUNGEON_PICKUP_SEND_000E": "1",
        "USDOF_GAME_DUNGEON_DROP_CONTROL": "0",
        "USDOF_GAME_DUNGEON_HELL_DROP": "1",
        # Temporary Hell test switch: opt-in only; standard sessions keep ETC rarity rolls.
        "USDOF_GAME_HELL_EPIC_ONLY": "0",
        # all: 每个 PVF [hellparty] order 都出现；random_one: N 个 order 随机出现 1 个。
        "USDOF_GAME_DUNGEON_HELL_WAVE_MODE": "all",
        "USDOF_GAME_REPLY_FINISH_QUEST_EXP_NOTI": "0",
        # 0123 count=u16（IDA sub_164DD20）；默认 OFF，与 023F 主路径并存可选
        "USDOF_GAME_REPLY_QUEST_TRIGGER_LIST_0123": "0",
        # 空 0164 默认 OFF（START_MAP / finish 仅 non-empty）
        "USDOF_GAME_REPLY_FINISH_QUEST_CLEAR_0164": "0",
        # 容器默认满展（2026-07-20 IDA + PVF）：
        #   背包 Main list_param16=16（Stage3 UI 档2；56 会闪退）
        #   个人金库 list2=200（用户确认基线；S4 券档仅到 152，本地直接满档）
        #   账号金库 selection_key=80（PVF accountcargo.etc 顶档）
        # 账号金库 PVF min_level=60 本地屏蔽（min_level=1）
        "USDOF_GAME_INVENTORY_MAIN_LIST_PARAM": "16",
        "USDOF_GAME_INVENTORY_PERSONAL_CARGO_LIST_PARAM": "200",
        "USDOF_GAME_ACCOUNT_CARGO_SELECTION_KEY": "80",
        "USDOF_GAME_ACCOUNT_CARGO_MIN_LEVEL": "1",
        "USDOF_GAME_INVENTORY_ALLOW_EXPAND": "1",
        # 消耗品入包：优先角色快捷栏 3..8，再背包 65..120；背包满仍可进快捷空槽
        "USDOF_GAME_GRANT_CONSUMABLE_PREFER_QUICK_SLOT": "1",
        # H2: dungeon enter-select UI on C->S 0x000F; SELECT grant still OFF
        "USDOF_GAME_DUNGEON_ENTER_HANDLE": "1",
        "USDOF_GAME_DUNGEON_ENTER_GRANT": "1",
        # 红色引导按钮 C1/0x0308 → 复用 SELECT 0010 grant（无主线时）
        "USDOF_GAME_DUNGEON_GUIDE_0308": "1",
        "USDOF_GAME_DUNGEON_ENTER_001D_CELL": "start",
        "USDOF_GAME_DUNGEON_ENTER_001D_ROOM_FLAG": "1",
        "USDOF_GAME_DUNGEON_ENTER_001D_LAYERED": "0",
        "USDOF_GAME_DUNGEON_ENTER_001D_PARTY_MEMBER": "0",
        # 友方 AI：map 原生 [ai character] 才刷；跨房 001D type5 克隆 live 闪退
        # （2026-07-22 22:38 mon=22→23 inject=31509 再证伪）→ INJECT 硬关。
        # 会话仍 harvest；跟身等控权 v2 稳后再开 inject 或走客户端路径。
        "USDOF_GAME_DUNGEON_ALLY_COMPANIONS": "1",
        "USDOF_GAME_DUNGEON_ALLY_COMPANIONS_QUEST_SEED": "1",
        "USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT": "0",
        "USDOF_GAME_DUNGEON_ALLY_COMPANIONS_INJECT_AS_FACE": "0",
        # Live 20260717: grant with S0/0009 first only PIV'd 0009 then channel-full;
        # friend 副本实现A is 001C->001D only. 0009 handler can re-enter game state.
        # Stage3 party slot may already exist from town; keep 001D partyMemberIndex=0.
        # Grant-time 0009: OFF (friend 001C+001D). Preload on ENTER_SELECT instead.
        "USDOF_GAME_DUNGEON_ENTER_SEND_0009": "0",
        "USDOF_GAME_DUNGEON_ENTER_PRELOAD_0009": "1",
        # 剧情地下城：按 PVF [quest connection] + 角色 active/finished 选迷宫（对照 90qiyuan
        # selectDungeonMaze）。有关联任务 → 剧情 maze；无匹配 → 自由 maze（无 connection）。
        # 关=0 仅作 A/B 强制自由迷宫。
        "USDOF_GAME_DUNGEON_QUEST_MAZE": "1",
        # 001C 必须带真实 maze_index；否则客户端拓扑与 001D 剧情 start map 可能错位。
        "USDOF_GAME_DUNGEON_001C_MAZE_INDEX": "1",
        # Monsters ON after enter+MOVE_MAP green (2026-07-17). PVF mon rows verified for 144.
        "USDOF_DUNGEON_MONSTERS": "1",
        "USDOF_DUNGEON_MONSTER_LIMIT": "0",
        "USDOF_GAME_DUNGEON_FINISH_LOADING_HANDLE": "1",
        # S0/0321 remains an explicit A/B probe only. The 2026-08-08 live
        # session consumed its PIV and then crashed in the client with a
        # stack-overflow bucket, so normal sessions stay on ACK0025 -> 001E.
        "USDOF_GAME_DUNGEON_WEAPON_AVATAR_RENDER": "0",
        # Map installation creates the worn medal object from USERINFO1, while
        # guardian-gem socket words are carried by the 108B list3 entry.
        "USDOF_GAME_DUNGEON_GUARDIAN_GEM_REFRESH": "1",
        # 剧情对话暂停 C1/0x00BF → S0/0x00AA（特殊本/教程对话头像状态机）
        "USDOF_GAME_DUNGEON_STORY_PAUSE_00BF": "1",
        "USDOF_GAME_DUNGEON_STORY_PAUSE_REQUIRE_RUN": "1",
        # Phase C runtime (副本实现A §5.3–5.6) — Stage3-safe defaults
        "USDOF_GAME_DUNGEON_MOVE_MAP_HANDLE": "1",
        "USDOF_GAME_DUNGEON_DIE_COMBAT_HANDLE": "1",
        "USDOF_GAME_DUNGEON_DIE_PERSIST_EXP": "1",
        # EXP — 服务端落库后单发累计 0025；包体按 Stage3 IDA reader 对齐。
        #   DIE: 0026 → 落库 → 0025(78B Stage3 + idx9) → 升级 0013 → 清房 001F
        #   结算: 0022 → 0025(78B + idx9) → 0023 CLEAR_REWARD
        #   friend_87 / s4a12_95 仅显式 A/B，禁止默认发送旧错位布局
        #   完整角色信息仅在主对象建立阶段下发；FINISH_LOADING 只回 ACK + 001E。
        #   001C/001D 复用主对象，禁止进本后再次 USERINFO bind/finalize。
        "USDOF_GAME_DUNGEON_DIE_SEND_EXP": "1",
        "USDOF_GAME_DUNGEON_DIE_0025_ONLY_ON_LEVELUP": "0",
        # 升级后禁止空 0013：空 skillinfo 会清空客户端技能树/技能栏（2026-07-18 live）。
        # 等级靠 0025 cascade；无已学技能 DB 前默认不发 0013。
        "USDOF_GAME_DUNGEON_DIE_SEND_SKILLINFO": "0",
        "USDOF_GAME_DUNGEON_EXP_NOTI_LAYOUT": "stage3_78",
        # 地下城可交易丢弃：S0/0026 带掉落行刷脚底（默认开；关则只登记 session）
        "USDOF_GAME_DROP_ITEM_SPAWN_DIE_0026": "1",
        "USDOF_GAME_DUNGEON_SET_PLAY_RESULT_HANDLE": "1",
        # 结算经验落库后，用同一原生 0025 下发服务端权威累计值。
        "USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_EXP": "1",
        "USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_0023": "1",
        "USDOF_GAME_DUNGEON_PLAY_RESULT_APPLY_EXP": "1",
        # free 翻牌金币：SKIP 时在 002E 直接发放；非 SKIP 走 0047
        "USDOF_GAME_DUNGEON_PLAY_RESULT_GRANT_GOLD": "0",
        # 通关经验：s4a12 CalculateClearRewardExp
        #   clear_base = GetExpRewardBase(basic_lv) * weight * difficulty_rate
        #   + score_bonus(rank)；无 *10（*10 是 friend legacy）
        "USDOF_GAME_DUNGEON_CLEAR_EXP_MODE": "s4a12",
        # Stage3 live 2026-08-02：清关后客户端会依次发送 C1/0047 00 00
        # （免费）和 01 00（付费侧）。日常走免费交互翻牌；0045/0046 仍是
        # 通关结算态的必要布局，不能与翻牌开关绑定。Stage3 在各地图
        # 本地计时结束后发送 C1/0045，服务端不回 0045，直接复用同一
        # 0047 免费牌事务，避免重新进入一次翻牌 UI。
        "USDOF_GAME_DUNGEON_SKIP_CARD_FLIP": "0",
        "USDOF_GAME_DUNGEON_SKIP_CARD_DELIVER_REWARD": "0",
        "USDOF_GAME_DUNGEON_SKIP_CARD_SEND_000E": "0",
        "USDOF_GAME_DUNGEON_CARD_AUTO_FLIP": "1",
        # 结算布局 + EPLP 回城；ENABLE_CLEAR 后 MOVE_MAP 空怪（防重刷 boss）
        "USDOF_GAME_DUNGEON_PLAY_RESULT_SEND_CARD_LAYOUT": "1",
        # 辅助 X 在 BOSS 清房后复用 002E 结算；给 Stage3 消费 ENABLE_CLEAR 的短间隔。
        "USDOF_GAME_CONTROL_BOSS_SETTLEMENT_DELAY_MS": "250",
        "USDOF_GAME_DUNGEON_MOVE_MAP_FREEZE_AFTER_CLEAR": "1",
        "USDOF_GAME_DUNGEON_DIE_IGNORE_AFTER_SETTLE": "1",
        "USDOF_GAME_DUNGEON_EPLP_HANDLE": "1",
        "USDOF_GAME_DUNGEON_SELECT_CARD_HANDLE": "1",
        "USDOF_GAME_DUNGEON_SELECT_CARD_DELIVER_REWARD": "1",
        "USDOF_GAME_DUNGEON_SELECT_CARD_SEND_000E": "1",
        # 杀怪经验（616ff337 本地完整公式）:
        #   raw = BaseExp * .dgn weight * [dungeon difficulty exp bonusrate]
        #   gained = max(1, int(raw * BaseExpPenalty))
        # calc = 仅 raw（无等级差）；fixed/KILL_EXP_FIXED 仅 A/B
        "USDOF_GAME_DUNGEON_KILL_EXP_MODE": "friend",
        "USDOF_GAME_DUNGEON_SCENE_UID_HANDLE": "1",
        # Stage3 PacketEnum: 0x0117=C->S LAG_STATISTICS, 0x019F=CHARAC_SLOT_EXTEND.
        # Friend DFOUSServer may S2C these; Stage3 treating them as enter follow-ups
        # soft-fails as channel-full. Keep OFF for this client.
        "USDOF_GAME_DUNGEON_ENTER_SEND_0117": "0",
        "USDOF_GAME_DUNGEON_ENTER_SEND_019F": "0",
        "USDOF_GAME_DUNGEON_ENTER_SEND_0164": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_02FE": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_0005": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_0017": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_0003": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_001A": "1",
        "USDOF_GAME_DUNGEON_ENTER_SEND_001B": "1",
        # If the live worn set has weapon-avatar slot 10, publish one complete
        # USERINFO1 between 0017 and 0003 while the town primary object exists.
        # The server-side builder skips every character without slot 10.
        "USDOF_GAME_DUNGEON_ENTER_SEND_0002": "1",
        "USDOF_GAME_DUNGEON_ENTER_0002_MODE": "codec",
        "USDOF_GAME_DUNGEON_ENTER_001A_MODE": "codec",
        "USDOF_GAME_DUNGEON_ENTER_001B_LAYOUT": "us",
        "USDOF_DUNGEON_PERM_EMPTY": "0",
        "USDOF_DUNGEON_PERM_MAX": "256",
        "USDOF_DUNGEON_PERM_CLEAR_STATE": "4",
        # Per-map [difficulty level] uses the boot-warmed current Script.pvf.
        # The fixed state above only applies to legacy maps without that field.
        "USDOF_DUNGEON_PERM_USE_PVF_DIFFICULTY": "1",
        "USDOF_DUNGEON_PERM_INCLUDE_FALLBACK_1_20": "0",
        "USDOF_GAME_DUNGEON_ENTER_S4_ORDER": "0",
        "USDOF_GAME_DUNGEON_ENTER_0005_POST_UI": "0",
        "USDOF_GAME_DUNGEON_LEAVE_HANDLE": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_0003": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_0017": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_0018": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_00CA": "1",
        # 副本/结算回城重同步背包与装备：list0 复位快捷栏消耗品状态；
        # S4A12 ReturnToVillage 发 subtype0；Stage3 另需 U1/166AED0 + 000E list3。
        "USDOF_GAME_DUNGEON_LEAVE_SEND_EQUIP_RESYNC": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_MAIN_ITEMLIST": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_USERINFO0": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_USERINFO1": "1",
        "USDOF_GAME_DUNGEON_LEAVE_USERINFO1_SKILLS": "1",
        "USDOF_GAME_DUNGEON_LEAVE_SEND_EQUIP_ITEMLIST": "1",

        "USDOF_GAME_ENTER_SERIA_START_FLAG": "1",
        "USDOF_GAME_ENTER_SERIA_START_BODY_LEN": "262",
        # map_key=0：跟随 login_spawn.town_id（与 S0/0017 同城）。
        # 禁止再写死 1：非银村选角会 START=银村 + 0017=本城 → 进房就播城镇切换插画。
        "USDOF_GAME_ENTER_SERIA_START_MAP_KEY": "0",
        # 禁止 burst：USERINFO/000E 依赖 0131；普通 000D/list36 依赖 USERINFO 发布全局容器。
        "USDOF_GAME_ENTER_SERIA_START_BURST_AFTER_START": "0",
        # 0131 即时后 → delayed：城镇 paint → 等 PIV 0131 → U0→0013→U1→000E→000D/list36
        "USDOF_GAME_ENTER_SERIA_AFTER_0131_DELAY_MS": "200",
        "USDOF_GAME_ENTER_SERIA_AFTER_0131_INTERPACKET_DELAY_MS": "60",
        "USDOF_GAME_ENTER_SERIA_BETWEEN_USERINFO_DELAY_MS": "100",
        # U1→000E：加大间隔，降低 166AED0 建槽未完成时 000E 静默空跑
        "USDOF_GAME_ENTER_SERIA_AFTER_USERINFO1_BEFORE_000E_DELAY_MS": "350",
        # U0/U1/000E 必须等 PIV 0131；普通 000D/list36 再等全局容器发布完成。
        "USDOF_GAME_ENTER_SERIA_GATE_EQUIP_ON_0131_PIV": "1",
        "USDOF_GAME_ENTER_SERIA_0131_PIV_TIMEOUT_MS": "2000",
        "USDOF_GAME_ENTER_SERIA_0131_PIV_CONTINUE_ON_TIMEOUT": "1",
        "USDOF_GAME_ENTER_SERIA_AFTER_0131_PIV_BEFORE_USERINFO_MS": "80",
        "USDOF_GAME_FOLLOWUP_DELAY_MS": "150",
        "USDOF_GAME_FOLLOWUP_INTERPACKET_DELAY_MS": "60",
        "USDOF_GAME_ENTER_SERIA_START_SEND_ITEMLIST": "1",
        # 进房同步个人金库 list2 + 账号金库 list12（S0/000D）
        "USDOF_GAME_ENTER_SERIA_START_SEND_CARGO_ITEMLIST": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_ACCOUNT_CARGO_ITEMLIST": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_0069": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_USERINFO0": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_USERINFO1": "1",
        # Enter-room wear (baseline_enter_seria_equip_bar_20260718):
        #   USERINFO0 sub_18CC430 19B 外观；USERINFO1 sub_166AED0 53B 建槽；000E list3 更新
        "USDOF_GAME_ENTER_SERIA_SEND_EQUIPMENT": "1",
        "USDOF_GAME_ENTER_SERIA_START_USERINFO1_EQUIPMENT": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_EQUIP_ITEMLIST": "1",
        "USDOF_GAME_MOVE_ITEMSPACE_SEND_EQUIP_ITEMLIST": "1",
        "USDOF_GAME_ENTER_SERIA_BACKFILL_INITIAL_EQUIPMENT": "1",
        "USDOF_GAME_ENTER_SERIA_PERSIST_BACKFILL_EQUIPMENT": "1",
        # 2026-07-19 加固：主路径 0131-PIV 门闩；post-settle 仅短 000E；
        # 008C UI settle 后 U1+000E 完整兜底（终态），确保装备栏必有装
        # 主进房已完成唯一一次 0013→U1→000E；settle 补刷会触发客户端主动卸装。
        "USDOF_GAME_ENTER_SERIA_POST_SETTLE_EQUIP_REFRESH": "0",
        "USDOF_GAME_ENTER_SERIA_POST_SETTLE_EQUIP_REFRESH_MS": "200",
        "USDOF_GAME_ENTER_SERIA_POST_SETTLE_RESEND_USERINFO1": "0",
        "USDOF_GAME_ENTER_SERIA_POST_SETTLE_RESEND_000E": "1",
        "USDOF_GAME_ENTER_SERIA_POST_SETTLE_U1_BEFORE_000E_MS": "300",
        "USDOF_GAME_ENTER_SERIA_ON_008C_EQUIP_REFRESH": "0",
        # 主进房已按 0131→U0→0013→U1→000E 建好装备/技能状态。
        # 008C 后二次 U1 会覆盖完整技能树后的客户端状态，剑魂会主动卸下光剑。
        "USDOF_GAME_ENTER_SERIA_ON_008C_RESEND_USERINFO1": "0",
        # 进房技能：grow=0 基线必须发 0013 + U1 技能页（2026-07-20 20:24 可长会话）。
        # 关掉后 grow=0 冷进约 3~5s 10054。grow=1 冷进另案，勿用关技能当通用默认。
        "USDOF_GAME_ENTER_SERIA_START_SEND_SKILLINFO": "1",
        "USDOF_GAME_ENTER_SERIA_START_SKILLINFO_FROM_JSON": "1",
        "USDOF_GAME_ENTER_SERIA_START_USERINFO1_SKILLS": "1",
        # Stage3 validates weapon mastery while decoding USERINFO1 equipment;
        # prime the skill tree between USERINFO0 and USERINFO1.
        "USDOF_GAME_ENTER_SERIA_START_SKILLINFO_BEFORE_USERINFO1": "1",
        # 骑士盾：2026-07-21 确认进房闪退真因是称号 S0/0166 缺 blobLen（2feb1af64），
        # 不是副手 slot24 本身。7/20 21:05 live 已成功下发 n=11 + slot24:113370003。
        # 默认打开 body wire + 进房 pre_sync（deck 主盾 → equipped slot24 → USERINFO/000E）。
        # 2026-07-22：S0/0245 SEND_DECK_INFO 已 IDA 钉死（sub_1674530）；进房/0013/0292 下发。
        # C1/0292 CHANGE_DECK_INFO = 拖底部 1 主 4 副。禁止 0x0244（频道 settle）。
        "USDOF_GAME_ENTER_SERIA_START_SEND_KNIGHT_DECK": "1",
        "USDOF_GAME_MOVE_ITEMSPACE_SEND_KNIGHT_DECK": "1",
        "USDOF_GAME_KNIGHT_SHIELD_SEND_DECK_NOTI": "1",
        "USDOF_GAME_HANDLE_CHANGE_DECK_0292": "1",
        "USDOF_GAME_KNIGHT_SHIELD_EMIT_BODY_WIRE": "1",
        # 技能栏槽位落库 character_skills；进房从 DB 还原
        "USDOF_GAME_SKILLS_PERSIST": "1",
        "USDOF_GAME_HANDLE_CHANGE_SKILLSLOT_001C": "1",
        # C1/01C0 persistence remains enabled. START_MAP sends S0/0187 only
        # when the character owns a real saved blob; no reference-derived
        # empty body is synthesized for a fresh role.
        "USDOF_GAME_SAVE_CHARACTER_OPTION_01C0": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_0187_CHARACTER_OPTION": "1",
        # 热键整表 C1/00C6 → 账号普通/Creator 分档；旧角色表仅首次迁移；
        # 进房 S0/01C7 Stage3 文本（u8 key_type + u32 + Ixx\\tvk；IDA sub_164D2F0）
        # 禁止默认 01C6：2026-07-22 live 进赛丽亚闪退；禁止 01C7 发 S4A12 u16[]
        "USDOF_GAME_SAVE_HOTKEY_OPTION_00C6": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_01C7_HOTKEY": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_01C6_HOTKEY": "0",
        # 技能释放命令 C1/014B（默认由全局 codec passthrough 收明文）/ 014C 清命令
        "USDOF_GAME_HANDLE_SKILL_COMMAND_014B": "1",
        "USDOF_GAME_HANDLE_SKILL_COMMAND_014C": "1",
        "USDOF_GAME_SKILL_COMMAND_PERSIST": "1",
        # 学技能 C1/001D + SP 账本 character_skill_points
        "USDOF_GAME_HANDLE_BUY_SKILL_001D": "1",
        # C1/01EC 双语义：mode=0 初始化重置 / mode=1 自动学习
        "USDOF_GAME_HANDLE_SKILL_INIT_01EC": "1",
        "USDOF_GAME_HANDLE_AUTO_SKILL_01EC": "1",
        "USDOF_GAME_SKILL_INIT_SEND_001D_REFRESH": "1",
        "USDOF_GAME_SKILL_INIT_BUY_GUARD": "1",
        "USDOF_GAME_SKILL_INIT_BUY_GUARD_SEC": "3",
        "USDOF_GAME_AUTO_SKILL_SEND_001D": "1",
        "USDOF_GAME_AUTO_SKILL_INCLUDE_TP": "0",
        # 进房由 START_MAP 的完整 USERINFO1 + SKILLINFO 一次建立角色状态；
        # FINISH_LOADING 只完成副本握手，杀怪与结算再发权威累计 0025。
        "USDOF_GAME_ENTER_SERIA_START_SEND_EXP_0025": "0",
        "USDOF_GAME_ENTER_SERIA_START_SEND_ACCEPTABLE_QUEST": "1",
        # Phase1B: 0015 可接列表开；START 30 pair 默认关（新号必须全 FFFF）
        "USDOF_GAME_ENTER_SERIA_QUEST_FROM_JSON": "1",
        "USDOF_GAME_ENTER_SERIA_START_QUEST_PAIRS_FROM_JSON": "0",
        "USDOF_GAME_ENTER_SERIA_START_QUEST_PAIR_MAX": "0",
        "USDOF_GAME_ENTER_SERIA_START_SEND_CLEAR_QUEST": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_DAILY_CHALLENGE": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_00AD": "1",
        "USDOF_GAME_SAVE_GAME_OPTION_00C5": "1",
        # Login spawn: use the saved town gate only when it is a Seria room;
        # towns without Seria fall back to the verified Silver Crown room.
        "USDOF_GAME_LOGIN_SPAWN_PERSIST": "1",
        # Town leave/move: C->S 0x0024 SET_USER_AREA -> S->C 0x0017/0x0018
        "USDOF_GAME_SET_USER_AREA_0024": "1",
        "USDOF_GAME_SET_USER_AREA_SEND_0017": "1",
        "USDOF_GAME_SET_USER_AREA_SEND_0018": "1",
        "USDOF_GAME_SET_USER_POSITION_0023": "1",
        "USDOF_GAME_SET_USER_POSITION_SEND_0016": "0",

        # Avatar Closet pedestal (Seria type 0xB1): NOTI 1086 metric seed after START
        "USDOF_GAME_ENTER_SERIA_START_SEND_1086_AVATAR_CLOSET": "1",
        "USDOF_GAME_AVATAR_CLOSET_1086_METRIC": "2",
        "USDOF_GAME_AVATAR_CLOSET_1086_FIELD": "0",
        # Friend recommended town-sync after START (loading/move completeness; not fort path)
        "USDOF_GAME_ENTER_SERIA_START_SEND_0131": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_0016": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_0017": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_0018": "1",
        "USDOF_GAME_ENTER_SERIA_START_SEND_007C": "1",
        # Legacy fort / dual-path probes stay OFF (START_MAP is the daily enter path)
        "USDOF_GAME_ENTER_SERIA_SEND_0131_PRIMARY_OBJECT": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_001D_ROOM_CONTEXT": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_00D3_CONTEXT": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0029_MAP_TABLE": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_002B_MAP_UPDATE": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE0": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE3": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE1": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0017_USER_AREA": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0016_USER_POSITION": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0018_AREA_USERS": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_0324_BIND": "0",
        "USDOF_GAME_ENTER_SERIA_SEND_00AD_GAME_OPTION": "0",
        "USDOF_GAME_ENTER_SERIA_SPLIT_AFTER_0131": "0",
        "USDOF_GAME_SELECT_CHARACTER_SEND_ACK_0004": "0",
        # 角色页纸娃娃：下发 DB 穿戴（装扮>装备>默认）；否则只显示职业默认武器模型
        "USDOF_USERINFO_ROSTER_SEND_EQUIPMENT": "1",
        # Knight 武器也下发；双武器重叠若再现再单独 A/B
        "USDOF_USERINFO_ROSTER_SEND_KNIGHT_WEAPON": "1",
        # 换装后同步 USERINFO0 外观模型（0013 → 000E + USERINFO0 [+ 城镇 USERINFO1 经验回写]）
        "USDOF_GAME_MOVE_ITEMSPACE_SEND_USERINFO0": "1",
        # 仅 U0 会脏经验条（十几亿）；城镇 U1 用 DB exp 绝对写回（默认开）
        "USDOF_GAME_APPEARANCE_RESEND_USERINFO1": "1",
        "USDOF_GAME_APPEARANCE_USERINFO1_SKILLS": "1",
        # 副本内换装禁止 U0+U1（U0 单独也会卡死 002D；见 071519 live）
        # 副本内只发 ACK0013 + 000D/000E；不以 USERINFO 重绑角色主对象。
        "USDOF_GAME_APPEARANCE_RESEND_USERINFO0_IN_DUNGEON": "0",
        "USDOF_GAME_APPEARANCE_RESEND_USERINFO1_IN_DUNGEON": "0",
        # 交任务 delayed U1 副本内默认关；城镇必须带 equipment（空 count 清栏）
        "USDOF_GAME_QUEST_FINISH_DELAYED_USERINFO1_IN_DUNGEON": "0",
        # 未知 opcode 兜底回执默认关（易引入未知错误）；需排查时再显式开
        "USDOF_GAME_UNKNOWN_RECEIPT": "0",
        "USDOF_GAME_SEND_CHARSELECT_0004_AFTER_01BA_PIV": "1",
        "USDOF_GAME_SEND_STATE_INIT_0001_BEFORE_CHARSELECT_0004": "1",
        # MUST stay 0. Non-zero gap → client 0x0008 before 0004 PIV → deferred
        # GET_USERINFO → black charselect (confirmed 2026-07-19).
        "USDOF_GAME_CHARSELECT_0001_0004_DELAY_MS": "0",
        "USDOF_USERINFO_ROSTER_PROFILE": "us-trace-dstr",
    }


def apply_standard_local_flow_env(
    env: dict[str, str] | None = None,
    *,
    force: bool = False,
) -> dict[str, str]:
    """Apply standard local flow defaults with setdefault (or force overwrite)."""

    target = os.environ if env is None else env
    for key, value in standard_local_flow_env().items():
        if force:
            target[key] = value
        else:
            target.setdefault(key, value)
    return target


def describe_standard_local_flow() -> str:
    return (
        f"script={DEFAULT_SCRIPT_PROFILE} channelinfo={DEFAULT_CHANNELINFO_PROFILE} "
        f"max_channels={STANDARD_CHANNELINFO_MAX_CHANNELS} "
        f"unique_ports={standard_local_flow_env()['USDOF_CHANNELINFO_UNIQUE_PORTS']} "
        f"first_channel_id={DEFAULT_CHANNELINFO_FIRST_CHANNEL_ID} "
        f"start_map={int(DEFAULT_GAME_ENTER_SERIA_START_MAP)} "
        f"idle_timeout={DEFAULT_GAME_CLIENT_TIMEOUT} "
        f"exit_settle={standard_local_flow_env().get('USDOF_GAME_057F_SETTLE_PROFILE', 'channel')}"
    )
