from __future__ import annotations

import socket
from game.game_socket import recv_game_chunk
import select
import threading
import queue
import contextlib
import os
import time
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from control.contracts import ControlApiError, QueuedControlCommand
from game.raid.runtime import RaidError
from game.raid.local_bindings import local_raid_hooks, local_notifications, note_local_send, pump_local_raid
from game.raid.local_bindings import send_game_packet as send_raid_packet, close_game_session
from game.raid.native_connection import native_game_connection, capture_login_send, confirm_login_send
from game.raid import lobby_bindings as raid_lobby
from game.raid.native_udp import native_udp_connection
from config import CAPTURE_DIR, GAME_PORT, LOCAL_HOST, LOG_DIR, ensure_dirs
from game.character.character_db import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_ACCOUNT_M_ID,
    DEFAULT_BUILTIN_CHARACTER_GROW_TYPE,
    DEFAULT_BUILTIN_CHARACTER_ID,
    DEFAULT_BUILTIN_CHARACTER_JOB,
    DEFAULT_BUILTIN_CHARACTER_LEVEL,
    DEFAULT_BUILTIN_CHARACTER_NAME,
    CharacterRepository,
    has_available_slot,
    parse_check_name_body,
    parse_create_character_body,
    parse_delete_character_body,
)
from game.character.native_loadout_bridge import note_native_loadout_piv
from login.session_registry import get_login_registry, is_loopback_address
from game.game_codec import (
    DecodeResult,
    codec0_seed0_encode,
    codec1_seed0_encode,
    codec10_xor_dword,
    codec11_seed0_encode,
    codec12_seed0_encode,
    codec13_seed0_encode,
    codec2_seed0_encode,
    codec3_seed0_encode,
    codec4_seed0_encode,
    codec5_seed0_encode,
    codec6_seed0_encode,
    codec7_seed0_encode,
    codec8_seed0_encode,
    codec9_seed0_encode,
    decode_payload_skeleton,
    parse_piv_plain,
)
from game.game_handlers import (
    ENTER_GAME_FOLLOWUP_OPCODES,
    build_userinfo_type2_roster_plaintext,
    handle_decoded_packet,
    named_plaintext_probe_payload,
    zero_pad_game_payload,
)
from game.character.initial_equipment import get_initial_equipment_for_job, get_initial_grow_type_for_job
from game.game_wire import (
    COMPACT_GAME_HEADER_SIZE,
    GameWireError,
    build_compact_game_packet,
    build_game_packet,
    describe_game_header,
    game_packet_body,
    parse_game_header,
)
from game.chain_loader import load_chain
EnterGameChainService = load_chain('flow-enter_game.py').EnterGameChainService
GameSessionState = load_chain('flow-enter_game.py').GameSessionState
_tower_item_cleanup = load_chain("helper-death_tower_item_cleanup.py")
_tower_inventory = load_chain("helper-death_tower_inventory.py")
CharCheckNameService = load_chain('opcode02b5-check_name.py').CharCheckNameService
GuildNameCheck009CService = load_chain(
    'opcode009c-check_guild_name.py'
).GuildNameCheck009CService
GuildMemberList0043Service = load_chain(
    'opcode0043-guild_member_list.py'
).GuildMemberList0043Service
GuildAllMemberList008CService = load_chain(
    'opcode008c-guild_all_member_list.py'
).GuildAllMemberList008CService
GuildRequestJoin015CService = load_chain(
    'opcode015c-guild_request_join.py'
).GuildRequestJoin015CService
GuildApproveJoin015EService = load_chain(
    'opcode015e-approve_guild_join.py'
).GuildApproveJoin015EService
GuildJoinList0160Service = load_chain(
    'opcode0160-guild_join_list.py'
).GuildJoinList0160Service
GuildInfoOfMyChars02E7Service = load_chain(
    'opcode02e7-guild_info_of_my_chars.py'
).GuildInfoOfMyChars02E7Service
GuildSearchForJoin02E8Service = load_chain(
    'opcode02e8-guild_search_for_join.py'
).GuildSearchForJoin02E8Service
GuildCreatePromoteCheck02E2Service = load_chain(
    'opcode02e2-guild_create_promote_check.py'
).GuildCreatePromoteCheck02E2Service
GuildInviteAuth02B3Service = load_chain(
    'opcode02b3-guild_invite_auth.py'
).GuildInviteAuth02B3Service
GuildCreateTarget02E4Service = load_chain(
    'opcode02e4-guild_create_target.py'
).GuildCreateTarget02E4Service
GuildCreatePermit02E5Service = load_chain(
    'opcode02e5-guild_create_permit.py'
).GuildCreatePermit02E5Service
GuildCreatePermitReply02E6Service = load_chain(
    'opcode02e6-guild_create_permit_reply.py'
).GuildCreatePermitReply02E6Service
GuildCreatePermitNotificationService = load_chain(
    'helper-guild_create_permit_notifications.py'
).GuildCreatePermitNotificationService
CharCreateCharacterService = load_chain('opcode0005-create_character.py').CharCreateCharacterService
CharDeleteCharacterService = load_chain('opcode0006-delete_character.py').CharDeleteCharacterService
CharGetUserinfoService = load_chain('opcode0008-get_userinfo.py').CharGetUserinfoService
ReturnSelectCharacterService = load_chain(
    'opcode0007-return_select_character.py'
).ReturnSelectCharacterService
CharExtendCharacsService = load_chain('flow-extend_characs.py').CharExtendCharacsService
CharCharcardClickService = load_chain('opcode02c1-charcard_click.py').CharCharcardClickService
ContentsPlayInfo0578Service = load_chain(
    'opcode0578-contents_play_info.py'
).ContentsPlayInfo0578Service
CharChangeCharacSlotService = load_chain('opcode0127-change_charac_slot.py').CharChangeCharacSlotService
TutorialFlag008FService = load_chain('opcode008f-tutorial_flag.py').TutorialFlag008FService
DungeonEventStoryPause00BFService = load_chain(
    'opcode00bf-dungeon_event_story_pause.py'
).DungeonEventStoryPause00BFService
from game.chain_loader import load_quest_chain
AcceptQuest001FService = load_quest_chain("opcode001f-accept_quest.py").AcceptQuest001FService
GiveupQuest0020Service = load_quest_chain("opcode0020-giveup_quest.py").GiveupQuest0020Service
SetQuestTrigger0021Service = load_quest_chain("opcode0021-set_quest_trigger.py").SetQuestTrigger0021Service
FinishQuest0022Service = load_quest_chain("opcode0022-finish_quest.py").FinishQuest0022Service
CompleteQuestArea0315Service = load_quest_chain(
    "opcode0315-complete_quest_area.py"
).CompleteQuestArea0315Service
DieMonster0027Service = load_quest_chain("opcode0027-die_monster.py").DieMonster0027Service
GetItem002BService = load_chain("opcode002b-get_item.py").GetItem002BService
ItemDictionary01B7Service = load_chain(
    "opcode01b7-item_dictionary.py"
).ItemDictionary01B7Service
EpicBookMakeItem0313Service = load_chain(
    "opcode0313-epic_book_make_item.py"
).EpicBookMakeItem0313Service
UseStackable002CService = load_chain("opcode002c-use_stackable.py").UseStackable002CService
Teleport00EDService = load_chain("opcode00ed-teleport.py").Teleport00EDService
DropItem002FService = load_chain("opcode002f-drop_item.py").DropItem002FService
DeleteItem0012Service = load_chain("opcode0012-delete_item.py").DeleteItem0012Service
BuyItem0015Service = load_chain("opcode0015-buy_item.py").BuyItem0015Service
BuyCerashopItem0040Service = load_chain(
    "opcode0040-buy_cerashop_item.py"
).BuyCerashopItem0040Service
RentNeoPremiumWeapon04B5Service = load_chain(
    "opcode04b5-rent_neo_premium_weapon.py"
).RentNeoPremiumWeapon04B5Service
ClaimNeoPremiumGift04B6Service = load_chain(
    "opcode04b6-claim_neo_premium_gift.py"
).ClaimNeoPremiumGift04B6Service
OpenCeraPackage0207Service = load_chain(
    "opcode0207-open_cera_package.py"
).OpenCeraPackage0207Service
UpdateCubeContract0218Service = load_chain(
    "opcode0218-update_cube_contract.py"
).UpdateCubeContract0218Service
UseBoosterItem00A0Service = load_chain(
    "opcode00a0-use_booster_item.py"
).UseBoosterItem00A0Service
UseLotteryItem001BService = load_chain(
    "opcode001b-use_lottery_item.py"
).UseLotteryItem001BService
UseRandomBoxItem00D0Service = load_chain(
    "opcode00d0-use_randombox_item.py"
).UseRandomBoxItem00D0Service
CeraPackageRefreshFlow = load_chain("flow-cera_package_refresh.py")
release_cera_package_refresh_on_piv = (
    CeraPackageRefreshFlow.release_cera_package_refresh_on_piv
)
SellItem0016Service = load_chain("opcode0016-sell_item.py").SellItem0016Service
DisjointItem001AService = load_chain("opcode001a-disjoint_item.py").DisjointItem001AService
UnsealRandomOption0191Service = load_chain(
    "opcode0191-unseal_random_option.py"
).UnsealRandomOption0191Service
UpgradeItem0050Service = load_chain(
    "opcode0050-upgrade_item.py"
).UpgradeItem0050Service
InvestAmplifyOption00CDService = load_chain(
    "opcode00cd-invest_amplify_option.py"
).InvestAmplifyOption00CDService
UseAvatarEmblem00C9Service = load_chain(
    "opcode00c9-use_avatar_emblem.py"
).UseAvatarEmblem00C9Service
EnchantByBead0110Service = load_chain(
    "opcode0110-enchant_by_bead.py"
).EnchantByBead0110Service
AvatarOptionChange01CCService = load_chain(
    "opcode01cc-avatar_option_change.py"
).AvatarOptionChange01CCService
MonsterHit0026Service = load_chain("opcode0026-monster_hit.py").MonsterHit0026Service
DieCharacter0028Service = load_chain(
    "opcode0028-die_character.py"
).DieCharacter0028Service
UseReviveCoin0029Service = load_chain(
    "opcode0029-map_table.py"
).UseReviveCoin0029Service
MoveMap002DService = load_chain("opcode002d-move_map.py").MoveMap002DService
SetPlayResult002EService = load_chain("opcode002e-set_play_result.py").SetPlayResult002EService
DungeonSceneUid01DEService = load_chain("opcode01de-dungeon_scene_uid.py").DungeonSceneUid01DEService
MoveItemspace0013Service = load_chain("opcode0013-move_itemspace.py").MoveItemspace0013Service
ChangeDeckInfo0292Service = load_chain(
    "opcode0292-change_deck_info.py"
).ChangeDeckInfo0292Service
SortItem0014Service = load_chain("opcode0014-sort_item.py").SortItem0014Service
TitleBookPut019CService = load_chain("opcode019c-title_book_put.py").TitleBookPut019CService
TitleBookGet019DService = load_chain("opcode019d-title_book_get.py").TitleBookGet019DService
ChangeSkillslot001CService = load_chain("opcode001c-change_skillslot.py").ChangeSkillslot001CService
DarkKnightComboSkill01FDService = load_chain(
    "opcode01fd-dark_knight_combo_skill.py"
).DarkKnightComboSkill01FDService
BuySkill001DService = load_chain("opcode001d-buy_skill.py").BuySkill001DService
SkillInit01ECService = load_chain("opcode01ec-skill_init.py").SkillInit01ECService
SkillUiRefresh0311Service = load_chain(
    "opcode0311-skill_ui_refresh_ack.py"
).SkillUiRefresh0311Service
SaveGameOption00C5Service = load_chain('opcode00c5-save_game_option.py').SaveGameOption00C5Service
SaveHotkeyOption00C6Service = load_chain(
    "opcode00c6-save_hotkey_option.py"
).SaveHotkeyOption00C6Service
SaveCharacterOption01C0Service = load_chain(
    "opcode01c0-save_character_option.py"
).SaveCharacterOption01C0Service
UpdateWishItems0547Service = load_chain(
    "opcode0547-update_wish_items.py"
).UpdateWishItems0547Service
SkillCommandCustomizing014BService = load_chain(
    "opcode014b-skill_command_customizing.py"
).SkillCommandCustomizing014BService
SkillCommandAllDefault014CService = load_chain(
    "opcode014c-skill_command_all_default.py"
).SkillCommandAllDefault014CService
SetUserPosition0023Service = load_chain('opcode0023-set_user_position.py').SetUserPosition0023Service
SetUserArea0024Service = load_chain('opcode0024-set_user_area.py').SetUserArea0024Service
DungeonEnter000F0010Service = load_chain('flow-dungeon_enter.py').DungeonEnter000F0010Service
FinishLoading0025Service = load_chain('opcode0025-finish_loading.py').FinishLoading0025Service
DungeonLeaveSelectService = load_chain('flow-dungeon_leave_select.py').DungeonLeaveSelectService
EplpCommand0048Service = load_chain("opcode0048-eplp_command.py").EplpCommand0048Service
SelectCard0047Service = load_chain("opcode0047-select_card.py").SelectCard0047Service
from game.character.force_exp_notify import ForceExpNotifyService
from opcodes.index import route_dynamic_game_packet
from game.opcode_trace import append_trace, ensure_trace_file
from packet_sequence_log import build_packet_extra, get_packet_sequence_logger
from game.hotpath_metrics import HotpathMetricsRecorder
from game.diagnostic_io import diagnostic_trace_enabled, open_stream_capture
from game.tcp_options import configure_game_tcp_socket


FLAG0_OPCODE_NAMES = {
    0x0000: "ENUM_NOTIPACKET_CHECK_CONNECTION",
    0x0001: "ENUM_NOTIPACKET_CHANNELINFO",
    0x0002: "ENUM_NOTIPACKET_USERINFO",
    0x0003: "ENUM_NOTIPACKET_USER_STATE",
    0x000A: "ENUM_NOTIPACKET_WALKOUT",
    0x006A: "ENUM_NOTIPACKET_CHARACTER_CARD_FRAME",
    0x00C1: "ENUM_NOTIPACKET_TIME_FRAGMENT_COUNT",
    0x00D9: "ENUM_NOTIPACKET_CLOSE_DISJOINT_STORE",
    # USDOF 90 character-select gate candidates; names/semantics are still
    # being verified from this executable, so keep the labels generic.
    0x0080: "ENUM_NOTIPACKET_0080_USDOF_GATE_CANDIDATE",
    0x008B: "ENUM_NOTIPACKET_008B_USDOF_STATE8_CANDIDATE",
    0x00D1: "ENUM_NOTIPACKET_00D1_USDOF_READY_SETTER_B_CANDIDATE",
    0x0286: "ENUM_NOTIPACKET_0286_USDOF_GATE_CANDIDATE",
    0x01BA: "ENUM_NOTIPACKET_01BA_USDOF_GATE_CANDIDATE",
    0x01F9: "ENUM_NOTIPACKET_01F9_USDOF_EXTEND_CHAR_LIST",
    0x01FA: "ENUM_NOTIPACKET_01FA_USDOF_OPEN_CHAR_SELECT_UI",
    0x020C: "ENUM_NOTIPACKET_CHANNEL_MOVE_SUCCESS",
    0x0323: "ENUM_NOTIPACKET_REQUEST_MOVING_CHANNEL_SELECTING_SCENE",
    0x0355: "ENUM_NOTIPACKET_0355_USDOF_EXTEND_READY_CANDIDATE",
}

FLAG1_OPCODE_NAMES = {
    0x0000: "ENUM_CMDPACKET_CHECK_CONNECTION",
    0x0001: "ENUM_CMDPACKET_LOGIN",
    0x0002: "ENUM_CMDPACKET_SET_UDP_IP_PORT",
    0x0003: "ENUM_CMDPACKET_EXIT",
    0x0004: "ENUM_CMDPACKET_SELECT_CHARACTER",
    0x0005: "ENUM_CMDPACKET_CREATE_CHARACTER",
    0x0006: "ENUM_CMDPACKET_DELETE_CHARACTER",
    0x0007: "ENUM_CMDPACKET_RETURN_SELECT_CHARACTER",
    0x0008: "ENUM_CMDPACKET_GET_USERINFO",
    0x0012: "ENUM_CMDPACKET_DELETE_ITEM",
    0x0013: "ENUM_CMDPACKET_MOVE_ITEMSPACE",
    0x0014: "ENUM_CMDPACKET_SORT_ITEM",
    0x0016: "ENUM_CMDPACKET_SELL_ITEM",
    0x00ED: "ENUM_CMDPACKET_TELEPORT",
    0x00D9: "ENUM_CMDPACKET_OVERFLOW_INFO",
    0x00DA: "ENUM_CMDPACKET_SERVER_MESSAGE_SEND",
    0x0131: "ENUM_CMDPACKET_CREATE_ACCOUNT_CARGO",
    0x013E: "ENUM_CMDPACKET_PIV",
    0x01BA: "ENUM_CMDPACKET_LOAD_EXTEND_CHARACS_FINISH",
    0x01F8: "ENUM_CMDPACKET_POST_CHARSELECT_USERINFO_REFRESH",
    0x02A8: "ENUM_CMDPACKET_LOAD_EXTEND_CHARACS",
    0x02B5: "ENUM_CMDPACKET_CHECK_DOUBLE_CHARACTER_NAME",
    0x02C1: "ENUM_CMDPACKET_CHARCARD_CLICK_SELECT",
    0x02EF: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_02EF",
    0x00C2: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_00C2",
    0x0505: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_0505",
    0x0506: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_0506",
    0x00FA: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_00FA",
    0x0117: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_0117",
    0x0355: "ENUM_CMDPACKET_0355_USDOF_EXTEND_READY",
    0x0555: "ENUM_CMDPACKET_ENTER_GAME_FOLLOWUP_0555",
    0x0578: "ENUM_CMDPACKET_CONTENTS_PLAY_INFO",
    0x057F: "ENUM_CMDPACKET_REQUEST_MOVING_CHANNEL_SELECTING_SCENE",
}

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def opcode_meaning(flag: int, opcode: int) -> str:
    names = FLAG0_OPCODE_NAMES if flag == 0 else FLAG1_OPCODE_NAMES if flag == 1 else {}
    return names.get(opcode, f"unknown flag=0x{flag:02x} opcode=0x{opcode:04x}")


def _env_hex_bytes(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    normalized = "".join(ch for ch in normalized if ch not in " \t\r\n:_-")
    if len(normalized) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits: {raw!r}")
    return bytes.fromhex(normalized)


def parse_probe_specs(text: str) -> list[tuple[int, int, bytes]]:
    """Parse comma-separated server-first Game probe specs.

    Accepted token forms:

    - ``0x0001``: flag 0, opcode 1, empty payload
    - ``0:0x0001``: explicit flag/opcode
    - ``0:0x0001:de ad be ef``: explicit payload hex

    The default prototype behavior is still the proven minimal
    ``flag=0/opcode=1`` probe unless this is overridden by an environment or
    CLI caller.
    """

    probes: list[tuple[int, int, bytes]] = []
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) == 1:
            flag = 0
            opcode_s = parts[0]
            payload_s = ""
        elif len(parts) == 2:
            flag = int(parts[0], 0)
            opcode_s = parts[1]
            payload_s = ""
        elif len(parts) == 3:
            flag = int(parts[0], 0)
            opcode_s = parts[1]
            payload_s = parts[2]
        else:
            raise ValueError(f"invalid probe token: {token!r}")
        opcode = int(opcode_s, 0)
        payload_hex = "".join(payload_s.split())
        if payload_s.strip().startswith("@"):
            payload = named_plaintext_probe_payload(payload_s.strip()[1:])
        else:
            payload = bytes.fromhex(payload_hex) if payload_hex else b""
        probes.append((flag & 0xFF, opcode & 0xFFFF, payload))
    return probes


def parse_plaintext_probe_specs(text: str) -> list[tuple[int, int, bytes]]:
    """Parse probe specs whose payload part is plaintext.

    For the current codecseed0 localtest build, opcode ``% 14 == 2`` uses the
    deterministic codec2 state captured in live traces, and opcode ``% 14 ==
    8`` uses the XTEA-style codec8 state reconstructed from ``0x02C19420``.
    This helper converts a convenient plaintext probe such as
    ``0:0x0002:0200...`` into the encoded wire payload that the client will
    decode before invoking the corresponding handler.  Other opcodes are
    passed through unchanged until their seed0 transform is proven.
    """

    probes = []
    for flag, opcode, payload in parse_probe_specs(text):
        idx = opcode % 14
        if idx == 1 and payload:
            payload = codec1_seed0_encode(zero_pad_game_payload(payload, 0x08))
        elif idx == 2 and payload:
            payload = codec2_seed0_encode(zero_pad_game_payload(payload, 0x10))
        elif idx == 3 and payload:
            payload = codec3_seed0_encode(zero_pad_game_payload(payload, 0x10))
        elif idx == 4 and payload:
            payload = codec4_seed0_encode(zero_pad_game_payload(payload, 0x10))
        elif idx == 5 and payload:
            payload = codec5_seed0_encode(zero_pad_game_payload(payload, 0x08))
        elif idx == 7 and payload:
            payload = codec7_seed0_encode(zero_pad_game_payload(payload, 0x08))
        elif idx == 8 and payload:
            payload = codec8_seed0_encode(zero_pad_game_payload(payload, 0x08))
        elif idx == 10 and payload:
            payload = codec10_xor_dword(zero_pad_game_payload(payload, 0x04))
        elif idx == 11 and payload:
            payload = codec11_seed0_encode(zero_pad_game_payload(payload, 0x08))
        elif idx == 12 and payload:
            payload = codec12_seed0_encode(zero_pad_game_payload(payload, 0x10))
        elif idx == 13 and payload:
            payload = codec13_seed0_encode(zero_pad_game_payload(payload, 0x08))
        probes.append((flag, opcode, payload))
    return probes


def describe_probe_specs(probes: list[tuple[int, int, bytes]]) -> str:
    if not probes:
        return "<none>"
    return ", ".join(
        f"flag={flag}/opcode=0x{opcode:04x}/payload={len(payload)}"
        for flag, opcode, payload in probes
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _legacy_plain_wire_enabled() -> bool:
    """Compatibility switch used when no directional override is present."""
    return _env_bool("USDOF_GAME_PLAIN_WIRE", False)


def _c2s_plain_wire_enabled() -> bool:
    """Whether the client encode hook sends direct C2S plaintext bodies."""
    return _env_bool(
        "USDOF_GAME_C2S_PLAIN_WIRE",
        _legacy_plain_wire_enabled(),
    )


def _s2c_plain_wire_enabled() -> bool:
    """Whether S2C codec responses bypass native client decoding."""
    return _env_bool(
        "USDOF_GAME_S2C_PLAIN_WIRE",
        _legacy_plain_wire_enabled(),
    )


def _plain_wire_header_mode() -> str:
    raw = os.environ.get("USDOF_GAME_PLAIN_WIRE_HEADER", "16").strip().lower()
    if raw in {"13", "compact", "compact13", "0x0d"}:
        return "13"
    return "16"


def _build_server_game_packet(flag: int, opcode: int, payload: bytes, **kwargs) -> bytes:
    if _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13":
        kwargs.pop("reserved", None)
        return build_compact_game_packet(opcode, payload, flag_or_magic=flag, **kwargs)
    return build_game_packet(opcode, payload, flag_or_magic=flag, **kwargs)


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    return int(text, 0)


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def _packet_key(packet: bytes) -> tuple[int, int]:
    header = parse_game_header(packet[:0x10])
    return header.flag_or_magic & 0xFF, header.opcode & 0xFFFF


def _packet_key_text(packet: bytes) -> str:
    flag, opcode = _packet_key(packet)
    return f"flag={flag}/opcode=0x{opcode:04x}"


def _enter_seria_followup_gap_ms(
    previous_packet: bytes | None,
    previous_note: str,
    next_packet: bytes,
    next_note: str,
    default_inter_ms: int,
) -> int:
    """Compute gap before sending the next enter-seria delayed packet.

    硬规则（进赛丽亚装备栏）:
      USERINFO0 → USERINFO1 之间至少 BETWEEN_USERINFO
      USERINFO1 → 000E list3 之间至少 AFTER_USERINFO1_BEFORE_000E
    000E 必须在 sub_166AED0 建槽之后；间隔过短会概率空栏。
    """

    gap = max(0, int(default_inter_ms))
    if previous_packet is None:
        return gap
    try:
        _prev_flag, prev_op = _packet_key(previous_packet)
        _next_flag, next_op = _packet_key(next_packet)
    except Exception:
        return gap

    prev_note_l = (previous_note or "").lower()
    next_note_l = (next_note or "").lower()

    # USERINFO0 → USERINFO1
    if prev_op == 0x0002 and next_op == 0x0002:
        between = max(
            0,
            _env_int("USDOF_GAME_ENTER_SERIA_BETWEEN_USERINFO_DELAY_MS", 100),
        )
        return max(gap, between)

    # USERINFO1 (or any USERINFO) → 000E equip bar
    if prev_op == 0x0002 and next_op == 0x000E:
        after_u1 = max(
            0,
            _env_int("USDOF_GAME_ENTER_SERIA_AFTER_USERINFO1_BEFORE_000E_DELAY_MS", 350),
        )
        return max(gap, after_u1)

    # Note-based fallback when opcode parse is ambiguous
    if ("userinfo_subtype1" in prev_note_l or "equip_166aed0" in prev_note_l) and (
        "0x000e" in next_note_l or "equip_bar" in next_note_l or "after_userinfo1" in next_note_l
    ):
        after_u1 = max(
            0,
            _env_int("USDOF_GAME_ENTER_SERIA_AFTER_USERINFO1_BEFORE_000E_DELAY_MS", 350),
        )
        return max(gap, after_u1)

    return gap


def _is_enter_seria_equip_core_note(note: str) -> bool:
    """True if a delayed packet needs the 0131/USERINFO character-object chain."""
    text = (note or "").lower()
    if "guild_medal_full_snapshot" in text:
        return True
    if "userinfo_subtype" in text or "equip_166aed0" in text:
        return True
    if "after_userinfo1_166aed0" in text or "equip_bar" in text:
        return True
    if "0x0002 userinfo" in text:
        return True
    if "0x000e" in text and "equip" in text:
        return True
    return False


def _split_enter_seria_delayed_for_0131_gate(
    delayed_response_packets: list[tuple[bytes, str]],
) -> tuple[list[tuple[bytes, str]], list[tuple[bytes, str]]]:
    """Split delayed stream: before object-bound core vs object-bound core+rest.

    前半：城镇 paint 等可在 0131 刚发出后发送。
    后半：从首个 USERINFO/000E（或其后的普通 000D/list36 全量刷新）起，必须等客户端 PIV 0131。
    """
    for index, (_pkt, note) in enumerate(delayed_response_packets):
        if _is_enter_seria_equip_core_note(note):
            return (
                list(delayed_response_packets[:index]),
                list(delayed_response_packets[index:]),
            )
    return list(delayed_response_packets), []


def _is_return_select_userinfo_gate_active(
    p1_waiting_for_piv_after: tuple[int, int] | None,
    p1_piv_followup_queue: list[tuple[bytes, str]],
) -> bool:
    """True while return-select USERINFO/followups still sit on the P1 PIV gate.

    2026-07-19 live: return-select 门闩未放完时客户端常立刻再发 C1/0008，
    split GET_USERINFO 会把新的 0286 叠进队列并改 wait-key，残留 01BA/0286
    随后被带进城镇 → 鼠标点不了 UI（半层选角 chrome + 城镇）。
    """

    if p1_waiting_for_piv_after == (1, 0x0007):
        return True
    for _packet, note in p1_piv_followup_queue:
        text = note or ""
        if "return-select-after-0007-piv" in text or "return-select" in text.lower():
            return True
    return False


def _format_p1_queue_notes(p1_piv_followup_queue: list[tuple[bytes, str]], limit: int = 6) -> str:
    if not p1_piv_followup_queue:
        return "<empty>"
    notes = [note for _packet, note in p1_piv_followup_queue[:limit]]
    extra = len(p1_piv_followup_queue) - len(notes)
    text = ", ".join(notes)
    if extra > 0:
        text += f", ...(+{extra})"
    return text


def _charselect_state_init_interpacket_delay_ms(previous_packet: bytes | None, current_packet: bytes) -> int:
    """Return the delay before sending 0x0004 after the 0x0001 state-init packet."""
    if previous_packet is None:
        return 0
    if _packet_key(previous_packet) != (1, 0x0001):
        return 0
    if _packet_key(current_packet) != (1, 0x0004):
        return 0
    return max(0, _env_int("USDOF_GAME_CHARSELECT_0001_0004_DELAY_MS", 0))


def _reentry_open_userinfo_gap_ms(previous_packet: bytes | None, current_packet: bytes) -> int:
    """Inter-packet gap for channel→charselect reentry hard order.

    Hard order: LOGIN 0x0001 → CHARSELECT 0x0004 → USERINFO 0x0002.

    Default gaps are **0**. Live 2026-07-19: any delay between 0001 and 0004 lets
    the client emit C1/0008 before PIV of 0004; deferred GET_USERINFO then leaves
    the roster black. Keep burst open unless explicitly A/B-testing gaps.
    """
    if previous_packet is None:
        return 0
    try:
        prev_key = _packet_key(previous_packet)
        cur_key = _packet_key(current_packet)
    except Exception:
        return 0
    # 0001 → 0004: reuse charselect delay; reentry floor defaults to 0.
    if prev_key == (1, 0x0001) and cur_key == (1, 0x0004):
        base = max(0, _env_int("USDOF_GAME_CHARSELECT_0001_0004_DELAY_MS", 0))
        reentry = max(0, _env_int("USDOF_GAME_REENTRY_0001_0004_GAP_MS", 0))
        return max(base, reentry)
    # 0004 → USERINFO (or 0001 → USERINFO if open A/B skipped 0004)
    if cur_key == (0, 0x0002) and prev_key in {(1, 0x0004), (1, 0x0001)}:
        return max(0, _env_int("USDOF_GAME_REENTRY_0004_USERINFO_GAP_MS", 0))
    return 0


def _partition_cold_charselect_queue_for_0004_piv(
    send_queue: list[tuple[bytes, str]],
    *,
    gate_enabled: bool,
) -> tuple[list[tuple[bytes, str]], list[tuple[bytes, str]]]:
    """Hold a cold-entry USERINFO stream until the real CHARSELECT PIV.

    LOGIN 0x0001 and CHARSELECT 0x0004 must remain adjacent.  Only the tail
    beginning with USERINFO is eligible for this gate; malformed or A/B queues
    pass through unchanged.
    """

    queue = list(send_queue)
    if not gate_enabled:
        return queue, []
    for index, (packet, _note) in enumerate(queue):
        if _packet_key(packet) != (1, 0x0004):
            continue
        tail = queue[index + 1 :]
        if not tail or _packet_key(tail[0][0]) != (0, 0x0002):
            return queue, []
        return queue[: index + 1], tail
    return queue, []


def _coalesce_reentry_open_packets(
    send_queue: list[tuple[bytes, str]],
) -> list[list[tuple[bytes, str]]]:
    """Group only the adjacent cold/reentry open packets for one TCP write.

    The packet bytes and their logical order stay unchanged.  The first
    ``LOGIN 0x0001 -> CHARSELECT 0x0004`` pair is the only group eligible for
    coalescing; all later packets remain individual writes so their existing
    PIV and inter-packet gates are preserved.
    """

    queue = list(send_queue)
    if len(queue) >= 2 and [_packet_key(packet) for packet, _note in queue[:2]] == [
        (1, 0x0001),
        (1, 0x0004),
    ]:
        return [queue[:2]] + [[item] for item in queue[2:]]
    return [[item] for item in queue]


def _is_get_userinfo_roster_request(body: bytes, session: GameSessionState) -> bool:
    """Only the native FFFF/2 request before selection owns charselect gates.

    Selected-character FFFF/2 requests are rejected by the handler. H/3 is
    online character inspection and must deliver both appearance and profile
    without changing the caller's cold/reentry USERINFO wait-key or queue.
    """
    raw = bytes(body)
    return (
        getattr(session, "selected_character_id", None) is None
        and len(raw) in (3, 8, 16)
        and raw[:3] == b"\xff\xff\x02"
        and not any(raw[3:])
    )


def _advance_cold_charselect_dup_0008_guard(
    *,
    armed: bool,
    flag: int,
    opcode: int,
    body: bytes,
) -> tuple[bool, bool]:
    """Advance the one-shot guard for the cold-entry startup GET_USERINFO.

    Stage3's S1/0001 reader emits C1/0008 with the native ``ff ff 02`` body.
    When the server has already scheduled the cold roster behind S1/0004's real
    PIV, replying to that startup request sends the same full roster twice.
    PIV and the two S1/0001 progress reports may precede C1/0008, so they keep
    the guard armed. Any other client action ends the startup window.
    """

    if not armed:
        return False, False
    packet_key = (int(flag) & 0xFF, int(opcode) & 0xFFFF)
    if packet_key in {
        (1, 0x013E),
        (1, 0x00AB),
        (1, 0x01DD),
    }:
        return True, False
    if packet_key == (1, 0x0008):
        return False, bytes(body) == b"\xff\xff\x02"
    return False, False


class _ColdCharselectRecovery:
    """One connection's requested roster cannot wait forever for an open PIV.

    A real 0004 PIV remains the normal release signal. Only an authenticated
    connection that actually requested FFFF/2 may use the bounded recovery.
    This records delivery progress separately from receipt of a real PIV.
    """

    def __init__(self, session):
        self.session = session
        self.identity = self._identity(session)
        self.open_sent_at = None
        self.requested = False
        self.released = False
        self.cancelled = False

    @staticmethod
    def _identity(session):
        return (getattr(session, "account_id", None),
                getattr(session, "login_session_id", None),
                getattr(session, "selected_channel_id", None))

    def open_sent(self, now):
        if self.open_sent_at is None and not self.cancelled:
            self.open_sent_at = float(now)

    def observe(self, flag, opcode, body):
        if int(flag) != 1 or self.cancelled or self.released:
            return
        if int(opcode) == 0x0008 and _is_get_userinfo_roster_request(body, self.session):
            self.requested = True
        elif int(opcode) in {0x0003, 0x0004, 0x0005, 0x0006, 0x0007,
                            0x0127, 0x01FE, 0x057F}:
            self.cancel()

    def cancel(self):
        self.cancelled = True

    def take_due(self, session, pending, now):
        if (session is not self.session or self._identity(session) != self.identity
                or self.cancelled or self.released or not self.requested
                or self.open_sent_at is None or float(now) - self.open_sent_at < 1.0
                or getattr(session, "selected_character_id", None) is not None
                or getattr(session, "enter_seria_in_progress", False)
                or not pending or _packet_key(pending[0][0]) != (0, 0x0002)):
            return []
        batch = list(pending)
        pending.clear()
        self.released = True
        return batch


def _normalize_followup_token(value: str) -> str:
    text = value.strip().lower().replace("_", "-")
    aliases = {
        "state": "userstate",
        "user-state": "userstate",
        "0x0003": "userstate",
        "0003": "userstate",
        "3": "userstate",
        "0x0080": "0080",
        "0080": "0080",
        "80": "0080",
        "0x008b": "008b",
        "008b": "008b",
        "8b": "008b",
        "0x0286": "0286",
        "286": "0286",
        "0x01ba": "01ba",
        "01ba": "01ba",
        "1ba": "01ba",
        "0x01f9": "01f9",
        "01f9": "01f9",
        "1f9": "01f9",
        "0x01fa": "01fa",
        "01fa": "01fa",
        "1fa": "01fa",
        "0x01fb": "01fb",
        "01fb": "01fb",
        "1fb": "01fb",
        "0x01fc": "01fc",
        "01fc": "01fc",
        "1fc": "01fc",
    }
    return aliases.get(text, text)


def _followup_token_for_packet(packet: bytes) -> str:
    flag, opcode = _packet_key(packet)
    if flag == 0 and opcode == 0x0003:
        return "userstate"
    if flag == 0 and opcode == 0x0080:
        return "0080"
    if flag == 0 and opcode == 0x008B:
        return "008b"
    if opcode == 0x0286:
        return "0286"
    if opcode == 0x01BA:
        return "01ba"
    if opcode == 0x01F9:
        return "01f9"
    if opcode == 0x01FA:
        return "01fa"
    if opcode == 0x01FB:
        return "01fb"
    if opcode == 0x01FC:
        return "01fc"
    return f"{flag}:0x{opcode:04x}"


PIV_USERINFO_TRIGGERS = {
    "piv",
    "after-piv",
    "piv-then-ack",
    "piv-then-ack-followups",
    "piv-ack-followups",
    # Experimentally important for USDOF90 character-select gate ordering:
    # keep the early PIV USERINFO because the client uses it to continue into
    # 0x0355/0x01BA/0x00D9, then resend USERINFO after ACK followups so the
    # USERINFO epilogue runs after the candidate gate packets.
    "piv-then-ack-followups-then-roster",
    "piv-ack-followups-then-roster",
}


ACK_FOLLOWUPS_THEN_ROSTER_TRIGGERS = {
    "ack-followups-then-roster",
    "ack-gates-then-roster",
    "ack-extras-then-roster",
    "piv-then-ack-followups-then-roster",
    "piv-ack-followups-then-roster",
}


ACK_QUEUE_AWARE_TRIGGERS = {
    "ack-roster-then-piv-followups",
    "ack-then-piv-followups",
    "ack-piv-followups",
    "ack-gated-followups",
    "ack-roster-piv",
}


_EPIC_BOOK_MAKE_ITEM_READER_OVERFLOW_00D9 = b"\x01\x13\x03"


def is_epic_book_make_item_overflow_report(opcode: int, plain_body: bytes) -> bool:
    """Return whether Stage3 reported a short/invalid S1/0313 response.

    The Game receive loop emits C1/00D9 as ``flag + opcode`` when its packet
    reader error pointer is set after dispatch.  This report is not a
    character-list USERINFO gate, so it must not enter the legacy 0286/01BA
    follow-up stream.
    """
    return (
        int(opcode) == 0x00D9
        and bytes(plain_body or b"")
        == _EPIC_BOOK_MAKE_ITEM_READER_OVERFLOW_00D9
    )


def should_suppress_dynamic_userinfo_for_dungeon_00d9(
    opcode: int, session: object = None
) -> bool:
    """Keep a dungeon reader report out of the character-select ACK stream.

    ``C1/00D9`` carries the flag/opcode of the client reader that reported the
    condition.  Character-select uses it as a legacy gate, but an active
    dungeon session must never receive its ``0286``/``01BA`` follow-ups.
    """
    return (
        int(opcode) == 0x00D9
        and session is not None
        and bool(getattr(session, "in_dungeon_run", False))
    )


@dataclass
class _ControlGameContext:
    """The sole Game TCP connection that local control commands may affect."""

    client: object
    session: object
    connection_id: str
    generation: int
    commands: object


class GameServer:
    def __init__(
        self,
        host: str = LOCAL_HOST,
        port: int = GAME_PORT,
        *,
        send_initial_probe: bool = True,
        initial_probes: list[tuple[int, int, bytes]] | None = None,
        ack_response_probes: list[tuple[int, int, bytes]] | None = None,
        piv_response_probes: list[tuple[int, int, bytes]] | None = None,
        dynamic_userinfo: bool = False,
        db_path: str | os.PathLike[str] | None = None,
        account_id: int = DEFAULT_ACCOUNT_ID,
        account_m_id: str = DEFAULT_ACCOUNT_M_ID,
        userinfo_roster_profile: str = "us-trace-dstr",
    ) -> None:
        self.host = host
        self.port = port
        # Extra Game listen ports for multi-channel unique-port mapping.
        # Populated by server_main from PVF channel ids (base + channel_id).
        self.listen_ports: list[int] = [int(port)]
        self.legacy_game_ports: dict[int, int] = {}
        self.dynamic_userinfo = dynamic_userinfo or os.environ.get("USDOF_GAME_DB_USERINFO", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.db_path = db_path or os.environ.get("USDOF_SQLITE_DB")
        self._account_context = threading.local()
        self._default_account_id = int(
            os.environ.get("USDOF_ACCOUNT_ID", str(account_id)), 0
        )
        self._default_account_m_id = os.environ.get("USDOF_ACCOUNT_M_ID", account_m_id)
        # The normal launcher/login chain carries a Nexon/Neople-side account
        # number and one-time auth token before the Game socket is entered.  In
        # the standalone P1 harness we intentionally bypass that upstream auth
        # flow, so keep the equivalent identity explicit and logged.  The DB
        # account_id remains the local primary key used for character storage;
        # launcher_account_id mirrors the numeric field in the local test
        # command line ("3012345" in the current scripts) for future packet
        # fields that prove to need the launcher identity instead.
        self.launcher_account_id = int(os.environ.get("USDOF_LAUNCHER_ACCOUNT_ID", "3012345"), 0)
        self.launcher_token = os.environ.get(
            "USDOF_LAUNCHER_TOKEN",
            "e3210f9193c9558c3638523332a864a2d93222fc94a85a9d966ab135c0125814",
        )
        self.userinfo_roster_profile = os.environ.get("USDOF_USERINFO_ROSTER_PROFILE", userinfo_roster_profile)
        self._character_repo: CharacterRepository | None = None
        if initial_probes is None:
            env_probes = os.environ.get("USDOF_GAME_PROBES")
            if env_probes is not None:
                initial_probes = parse_probe_specs(env_probes)
            elif send_initial_probe:
                initial_probes = [(0, 1, b"")]
            else:
                initial_probes = []
        self.initial_probes = list(initial_probes)
        self.send_initial_probe = bool(self.initial_probes)
        if ack_response_probes is None:
            ack_response_probes = parse_probe_specs(os.environ.get("USDOF_GAME_ACK_RESPONSES", ""))
            ack_response_probes.extend(
                parse_plaintext_probe_specs(os.environ.get("USDOF_GAME_ACK_PLAINTEXT_RESPONSES", ""))
            )
        if piv_response_probes is None:
            piv_response_probes = parse_probe_specs(os.environ.get("USDOF_GAME_PIV_RESPONSES", ""))
            piv_response_probes.extend(
                parse_plaintext_probe_specs(os.environ.get("USDOF_GAME_PIV_PLAINTEXT_RESPONSES", ""))
            )
        self.ack_response_probes = list(ack_response_probes)
        self.piv_response_probes = list(piv_response_probes)
        ensure_dirs()
        self.internal_protocol_log = _env_bool(
            "USDOF_INTERNAL_PROTOCOL_LOG", True
        )
        self.diagnostic_trace = diagnostic_trace_enabled()
        self.protocol_traffic_log = self.internal_protocol_log and _env_bool(
            "USDOF_PROTOCOL_TRAFFIC_LOG", True
        )
        if self.internal_protocol_log:
            ensure_trace_file()
        self.log_path = (
            LOG_DIR / f"game_server_{_timestamp()}.log"
            if self.internal_protocol_log
            else None
        )
        self.packet_sequence_logger = (
            get_packet_sequence_logger() if self.internal_protocol_log else None
        )
        self.hotpath_metrics = HotpathMetricsRecorder(self.log)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._delayed_response_context = threading.local()
        # The control API is single-player by design: there is exactly one
        # replaceable active Game connection and it owns all state mutation.
        self._control_context_lock = threading.Lock()
        self._control_active_context: _ControlGameContext | None = None
        self._control_generation = 0
        self._control_request_lock = threading.Lock()
        self._control_idempotency: dict[str, tuple[float, QueuedControlCommand]] = {}
        # Unit tests call the packet handlers directly without a socket-scoped
        # ``handle_client`` frame.  Real client connections receive their own
        # GameSessionState instance in ``handle_client`` and pass it into the
        # same handlers.
        self._default_session_state = GameSessionState(
            account_id=self._default_account_id,
            account_m_id=self._default_account_m_id,
        )
        self.enter_game_chain = EnterGameChainService(self)
        self.char_check_name = CharCheckNameService(self)
        self.guild_name_check_009c = GuildNameCheck009CService(self)
        self.guild_member_list_0043 = GuildMemberList0043Service(self)
        self.guild_all_member_list_008c = GuildAllMemberList008CService(self)
        self.guild_request_join_015c = GuildRequestJoin015CService(self)
        self.guild_approve_join_015e = GuildApproveJoin015EService(self)
        self.guild_join_list_0160 = GuildJoinList0160Service(self)
        self.guild_info_of_my_chars_02e7 = GuildInfoOfMyChars02E7Service(self)
        self.guild_search_for_join_02e8 = GuildSearchForJoin02E8Service(self)
        self.guild_create_promote_check_02e2 = GuildCreatePromoteCheck02E2Service(self)
        self.guild_invite_auth_02b3 = GuildInviteAuth02B3Service(self)
        self.guild_create_target_02e4 = GuildCreateTarget02E4Service(self)
        self.guild_create_permit_02e5 = GuildCreatePermit02E5Service(self)
        self.guild_create_permit_reply_02e6 = GuildCreatePermitReply02E6Service(self)
        self.guild_create_permit_notifications = GuildCreatePermitNotificationService(self)
        self.char_create = CharCreateCharacterService(self)
        self.char_delete = CharDeleteCharacterService(self)
        self.char_get_userinfo = CharGetUserinfoService(self)
        self.return_select_character = ReturnSelectCharacterService(self)
        self.char_extend = CharExtendCharacsService(self)
        self.char_charcard = CharCharcardClickService(self)
        self.contents_play_info_0578 = ContentsPlayInfo0578Service(self)
        self.char_change_slot = CharChangeCharacSlotService(self)
        self.tutorial_flag_008f = TutorialFlag008FService(self)
        self.dungeon_event_story_pause_00bf = DungeonEventStoryPause00BFService(self)
        self.accept_quest_001f = AcceptQuest001FService(self)
        self.giveup_quest_0020 = GiveupQuest0020Service(self)
        self.set_quest_trigger_0021 = SetQuestTrigger0021Service(self)
        self.finish_quest_0022 = FinishQuest0022Service(self)
        self.complete_quest_area_0315 = CompleteQuestArea0315Service(self)
        self.die_monster_0027 = DieMonster0027Service(self)
        self.get_item_002b = GetItem002BService(self)
        self.item_dictionary_01b7 = ItemDictionary01B7Service(self)
        self.epic_book_make_item_0313 = EpicBookMakeItem0313Service(self)
        self.use_stackable_002c = UseStackable002CService(self)
        self.teleport_00ed = Teleport00EDService(self)
        self.drop_item_002f = DropItem002FService(self)
        self.delete_item_0012 = DeleteItem0012Service(self)
        self.buy_item_0015 = BuyItem0015Service(self)
        self.buy_cerashop_item_0040 = BuyCerashopItem0040Service(self)
        self.rent_neo_premium_weapon_04b5 = RentNeoPremiumWeapon04B5Service(self)
        self.claim_neo_premium_gift_04b6 = ClaimNeoPremiumGift04B6Service(self)
        self.open_cera_package_0207 = OpenCeraPackage0207Service(self)
        self.update_cube_contract_0218 = UpdateCubeContract0218Service(self)
        self.use_booster_item_00a0 = UseBoosterItem00A0Service(self)
        self.use_lottery_item_001b = UseLotteryItem001BService(self)
        self.use_random_box_item_00d0 = UseRandomBoxItem00D0Service(self)
        self.sell_item_0016 = SellItem0016Service(self)
        self.disjoint_item_001a = DisjointItem001AService(self)
        self.unseal_random_option_0191 = UnsealRandomOption0191Service(self)
        self.upgrade_item_0050 = UpgradeItem0050Service(self)
        self.invest_amplify_option_00cd = InvestAmplifyOption00CDService(self)
        self.use_avatar_emblem_00c9 = UseAvatarEmblem00C9Service(self)
        self.enchant_by_bead_0110 = EnchantByBead0110Service(self)
        self.avatar_option_change_01cc = AvatarOptionChange01CCService(self)
        self.monster_hit_0026 = MonsterHit0026Service(self)
        self.die_character_0028 = DieCharacter0028Service(self)
        self.use_revive_coin_0029 = UseReviveCoin0029Service(self)
        self.move_map_002d = MoveMap002DService(self)
        self.set_play_result_002e = SetPlayResult002EService(self)
        self.dungeon_scene_uid_01de = DungeonSceneUid01DEService(self)
        self.move_itemspace_0013 = MoveItemspace0013Service(self)
        self.change_deck_info_0292 = ChangeDeckInfo0292Service(self)
        self.sort_item_0014 = SortItem0014Service(self)
        self.title_book_put_019c = TitleBookPut019CService(self)
        self.title_book_get_019d = TitleBookGet019DService(self)
        self.change_skillslot_001c = ChangeSkillslot001CService(self)
        self.dark_knight_combo_skill_01fd = DarkKnightComboSkill01FDService(self)
        self.buy_skill_001d = BuySkill001DService(self)
        self.skill_init_01ec = SkillInit01ECService(self)
        self.skill_ui_refresh_0311 = SkillUiRefresh0311Service(self)
        self.save_game_option_00c5 = SaveGameOption00C5Service(self)
        self.save_hotkey_option_00c6 = SaveHotkeyOption00C6Service(self)
        self.save_character_option_01c0 = SaveCharacterOption01C0Service(self)
        self.update_wish_items_0547 = UpdateWishItems0547Service(self)
        self.skill_command_customizing_014b = SkillCommandCustomizing014BService(
            self
        )
        self.skill_command_all_default_014c = SkillCommandAllDefault014CService(
            self
        )
        self.set_user_position_0023 = SetUserPosition0023Service(self)
        self.set_user_area_0024 = SetUserArea0024Service(self)
        self.dungeon_enter_000f_0010 = DungeonEnter000F0010Service(self)
        self.finish_loading_0025 = FinishLoading0025Service(self)
        self.dungeon_leave_select = DungeonLeaveSelectService(self)
        self.eplp_command_0048 = EplpCommand0048Service(self)
        self.select_card_0047 = SelectCard0047Service(self)
        from game.character.gm_commands import GmItemControlCommands
        from game.character.gm_currency_commands import GmCurrencyControlCommands
        from game.character.gm_loadout_commands import GmLoadoutControlCommands
        from game.character.gm_profession_commands import GmProfessionControlCommands
        from game.character.gm_pvf_resources import GmPvfResources
        from game.content_block_commands import ContentBlockControlCommands
        from game.quest.gm_commands import GmQuestControlCommands

        # The GM API restores this catalog after its dedicated PVF child worker
        # publishes the cache. Game/player paths retain their shared runtime
        # and never parse GM item scripts through this object.
        self.gm_pvf_resources = GmPvfResources()
        self.gm_item_commands = GmItemControlCommands(self)
        self.gm_currency_commands = GmCurrencyControlCommands(self)
        self.gm_loadout_commands = GmLoadoutControlCommands(self)
        self.gm_profession_commands = GmProfessionControlCommands(self)
        self.gm_quest_commands = GmQuestControlCommands(self)
        self.content_block_commands = ContentBlockControlCommands(self)
        from game.dungeon.test_support import DungeonTestCommands
        self.dungeon_test_commands = DungeonTestCommands(self)
        self._control_handlers = {
            "dungeon.test_invincibility": self.dungeon_test_commands.invincibility,
            "dungeon.kill_all_monsters": self._control_kill_all_monsters,
            "dungeon.move_adjacent": self._control_move_adjacent,
            "pvf.list_items": self.gm_item_commands.list_items,
            "inventory.grant_item": self.gm_item_commands.grant_item,
            "inventory.list": self.gm_item_commands.list_inventory,
            "inventory.add_item": self.gm_item_commands.add_inventory_item,
            "inventory.delete_item": self.gm_item_commands.delete_inventory_item,
            "inventory.clear_scope": self.gm_item_commands.clear_inventory_scope,
            "currency.add_cera": self.gm_currency_commands.add_cera,
            "currency.add_gold": self.gm_currency_commands.add_gold,
            "loadout.list": self.gm_loadout_commands.list_loadouts,
            "loadout.save": self.gm_loadout_commands.save_loadout,
            "loadout.read_current": self.gm_loadout_commands.read_current,
            "loadout.apply": self.gm_loadout_commands.apply_loadout,
            "character.list_professions": self.gm_profession_commands.list_professions,
            "character.change_job": self.gm_profession_commands.change_job,
            "character.change_profession": self.gm_profession_commands.change_profession,
            "character.set_level": self.gm_profession_commands.set_level,
            "quest.list_accepted": self.gm_quest_commands.list_accepted,
            "quest.complete_accepted": self.gm_quest_commands.complete_accepted,
            "quest.giveup_accepted": self.gm_quest_commands.giveup_accepted,
            "quest.list": self.gm_quest_commands.list_quests,
            "quest.reaccept": self.gm_quest_commands.reaccept_finished,
            "quest.accept_unfinished": self.gm_quest_commands.accept_unfinished,
            "content_block.list": self.content_block_commands.list_rules,
            "content_block.resolve": self.content_block_commands.resolve_ids,
            "content_block.candidates": self.content_block_commands.list_candidates,
            "content_block.apply": self.content_block_commands.apply_rules,
            "content_block.remove": self.content_block_commands.remove_rules,
        }
        # Cross-TCP arm for Select Channel short-leave reentry:
        # pure charselect leave often never emits C->S PIV 0x013E on second Game.
        # When armed, after opcode1 we wait briefly; if no PIV arrives, open roster.
        self._channel_leave_reentry_lock = threading.Lock()
        self._channel_leave_reentry_armed = False
        self._channel_leave_reentry_armed_at = 0.0
        # 调试：文件触发手动下发 S0/0025（见 game/force_exp_notify.py）
        self.force_exp_notify = ForceExpNotifyService(self)

    @property
    def account_id(self) -> int:
        return int(
            getattr(self._account_context, "account_id", self._default_account_id)
        )

    @account_id.setter
    def account_id(self, value: int) -> None:
        self._default_account_id = int(value)

    @property
    def account_m_id(self) -> str:
        return str(
            getattr(
                self._account_context,
                "account_m_id",
                self._default_account_m_id,
            )
        )

    @account_m_id.setter
    def account_m_id(self, value: str) -> None:
        self._default_account_m_id = str(value)

    @contextlib.contextmanager
    def _account_identity_scope(self, identity):
        previous_account_id = getattr(self._account_context, "account_id", None)
        previous_account_m_id = getattr(self._account_context, "account_m_id", None)
        had_account_id = hasattr(self._account_context, "account_id")
        had_account_m_id = hasattr(self._account_context, "account_m_id")
        if identity is not None:
            self._account_context.account_id = int(identity.account_id)
            self._account_context.account_m_id = str(identity.account_m_id)
        try:
            yield
        finally:
            if had_account_id:
                self._account_context.account_id = previous_account_id
            else:
                try:
                    del self._account_context.account_id
                except AttributeError:
                    pass
            if had_account_m_id:
                self._account_context.account_m_id = previous_account_m_id
            else:
                try:
                    del self._account_context.account_m_id
                except AttributeError:
                    pass

    def log(self, msg: str) -> None:
        if not self.internal_protocol_log:
            return
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        console_line = line.encode(console_encoding, errors="replace").decode(console_encoding, errors="replace")
        print(console_line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    @contextlib.contextmanager
    def _delayed_response_connection(self, session, connection_id: str):
        """Keep chain-produced packets on their owning Game connection thread."""
        previous = getattr(self._delayed_response_context, "active", None)
        context = (session, str(connection_id), [])
        self._delayed_response_context.active = context
        try:
            yield
        finally:
            pending = sum(len(batch) for _owner, batch in context[2])
            context[2].clear()
            self._delayed_response_context.active = previous
            if pending:
                self.log("[Game/Delayed] discarded on connection close conn=%s count=%d"
                         % (context[1], pending))

    @staticmethod
    def _delayed_response_owner(session):
        return (getattr(session, "selected_character_id", None),
                getattr(session, "chat_selection_generation", 0),
                getattr(session, "dungeon_run_generation", 0))

    def queue_delayed_response_packets(self, packets: list[tuple[bytes, str]]) -> None:
        """Queue only for the active connection and its current actor/run."""
        if not packets:
            return
        context = getattr(self._delayed_response_context, "active", None)
        if context is None:
            raise RuntimeError("delayed response requires an owning Game connection")
        owner = self._delayed_response_owner(context[0])
        batch = tuple(packets)
        context[2].append((owner, batch))
        if any("enter-seria" in note for _packet, note in batch):
            self.log("[Game/Delayed] queued enter conn=%s owner=%s count=%d"
                     % (context[1], owner, len(batch)))

    def pop_delayed_response_packets(self) -> list[tuple[bytes, str]]:
        context = getattr(self._delayed_response_context, "active", None)
        if context is None:
            return []
        owner = self._delayed_response_owner(context[0])
        batches = list(context[2])
        context[2].clear()
        packets = []
        stale = 0
        for queued_owner, batch in batches:
            if queued_owner == owner:
                packets.extend(batch)
            else:
                stale += len(batch)
        if stale:
            self.log("[Game/Delayed] discarded stale conn=%s owner=%s count=%d"
                     % (context[1], owner, stale))
        if any("enter-seria" in note for _packet, note in packets):
            self.log("[Game/Delayed] drained enter conn=%s owner=%s count=%d"
                     % (context[1], owner, len(packets)))
        return packets

    def _drain_tournament_reward_manual_refresh(
        self,
        context: _ControlGameContext,
    ) -> tuple[int, str]:
        """Refresh the native Shonan click state on the owning Game thread."""
        if not self._is_current_control_context(context):
            return 0, "tournament_manual_refresh=stale_context"

        refresh = load_chain("flow-tournament_reward_manual_refresh.py")
        packets, note = (
            refresh.collect_due_tournament_reward_manual_refresh_packets(
                self,
                session=context.session,
            )
        )
        if not packets:
            return 0, note
        if not self._is_current_control_context(context):
            self.log("[Tournament] manual refresh discarded for stale Game context")
            return 0, "tournament_manual_refresh=stale_after_collection"

        for index, response in enumerate(packets):
            self._log_game_sequence(
                direction="S->C tournament-manual-refresh",
                connection_id=context.connection_id,
                packet=response,
                compact_prefix=(
                    _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                ),
                semantic=opcode_meaning(*_packet_key(response)),
                note="manual refresh index=%d; %s" % (index, note),
            )
            if _s2c_plain_wire_enabled():
                self._log_plain_wire_packet("send-tournament-manual-refresh", response)
            send_raid_packet(self, context.session, context.client, response)
        self.log(
            "[Tournament] manual reward window refreshed packets=%d %s"
            % (len(packets), note)
        )
        return len(packets), note

    def _drain_altar_due_packets(
        self,
        context: _ControlGameContext,
    ) -> tuple[int, str]:
        """Send due altar waves and settlement packets on the Game owner thread."""
        if not self._is_current_control_context(context):
            return 0, "altar_pump=stale_context"

        altar = load_chain("flow-altar-random.py")
        packets, note = altar.collect_due_altar_packets(
            self,
            session=context.session,
        )
        if not packets:
            if "altar_pump=error" in note:
                self.log("[Altar] %s packets=0" % note)
            return 0, note
        if not self._is_current_control_context(context):
            self.log("[Altar] due packets discarded for stale Game context")
            return 0, "altar_pump=stale_after_collection"

        for index, response in enumerate(packets):
            self._log_game_sequence(
                direction="S->C altar-timer",
                connection_id=context.connection_id,
                packet=response,
                compact_prefix=(
                    _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                ),
                semantic=opcode_meaning(*_packet_key(response)),
                note="altar due index=%d; %s" % (index, note),
            )
            if _s2c_plain_wire_enabled():
                self._log_plain_wire_packet("send-altar-timer", response)
            send_raid_packet(self, context.session, context.client, response)
        self.log("[Altar] sent due packets=%d %s" % (len(packets), note))
        return len(packets), note

    def register_control_game_client(
        self,
        client: object,
        session: object,
        *,
        connection_id: str,
    ) -> _ControlGameContext:
        """Replace the active single-player Game connection."""

        with self._control_context_lock:
            self._control_generation += 1
            context = _ControlGameContext(
                client=client,
                session=session,
                connection_id=str(connection_id),
                generation=int(self._control_generation),
                commands=queue.Queue(maxsize=64),
            )
            self._control_active_context = context
        self.log("[Control] active Game session epoch=%s" % context.generation)
        return context

    def _current_control_context(self) -> _ControlGameContext | None:
        with self._control_context_lock:
            context = self._control_active_context
            if context is not None:
                try:
                    if int(context.client.fileno()) < 0:
                        self._control_active_context = None
                        context = None
                except Exception:
                    self._control_active_context = None
                    context = None
            return context

    def _is_current_control_context(self, context: _ControlGameContext) -> bool:
        with self._control_context_lock:
            return self._control_active_context is context

    def get_control_session_snapshot(self) -> dict:
        """Return a small, target-free view of the current local client."""

        context = self._current_control_context()
        if context is None:
            return {"connected": False, "session_epoch": None}
        session = context.session
        character_ready = bool(
            int(getattr(session, "selected_character_id", 0) or 0) > 0
        )
        in_dungeon = bool(getattr(session, "in_dungeon_run", False))
        town_ready = bool(
            character_ready
            and bool(getattr(session, "enter_seria_settled", False))
            and not in_dungeon
        )
        mons = list(getattr(session, "dungeon_room_mons", None) or [])
        alive = sum(
            1
            for mon in mons
            if bool(mon.get("is_blocking", True)) and not bool(mon.get("killed"))
        )
        return {
            "connected": True,
            "session_epoch": context.generation,
            "character_ready": character_ready,
            "character_id": int(getattr(session, "selected_character_id", 0) or 0) or None,
            "character_name": getattr(session, "selected_character_name", None),
            "town_ready": town_ready,
            "in_dungeon": in_dungeon,
            "dungeon_id": int(
                getattr(session, "selected_dungeon_id", None)
                or getattr(session, "cur_dungeon_id", 0)
                or 0
            ),
            "cell": {
                "x": int(getattr(session, "dungeon_cell_x", 0) or 0),
                "y": int(getattr(session, "dungeon_cell_y", 0) or 0),
            },
            "alive_blocking_count": alive,
            "clear_phase": int(getattr(session, "dungeon_clear_phase", 0) or 0),
        }

    def submit_control_command(
        self,
        *,
        request_id: str,
        command: str,
        params: dict,
        timeout: float,
    ) -> dict:
        """Queue one API command for the active Game connection thread."""

        context = self._current_control_context()
        if context is None:
            raise ControlApiError("NO_GAME_CLIENT", "no current Game client", status=503)
        key = str(request_id)
        now = time.monotonic()
        enqueue = False
        with self._control_request_lock:
            stale = [
                item_key
                for item_key, (created_at, item) in self._control_idempotency.items()
                if item.done.is_set() and now - created_at > 600.0
            ]
            for item_key in stale:
                self._control_idempotency.pop(item_key, None)
            cached = self._control_idempotency.get(key)
            if cached is not None:
                request = cached[1]
                if request.command != command or request.params != params:
                    raise ControlApiError(
                        "REQUEST_ID_CONFLICT",
                        "request_id was already used for a different command",
                        status=409,
                    )
            else:
                request = QueuedControlCommand(
                    request_id=key,
                    command=str(command),
                    params=dict(params),
                )
                self._control_idempotency[key] = (now, request)
                enqueue = True
        if enqueue:
            try:
                context.commands.put_nowait(request)
            except queue.Full:
                with self._control_request_lock:
                    self._control_idempotency.pop(key, None)
                raise ControlApiError("COMMAND_QUEUE_FULL", "control command queue is full", status=503)
        if not request.done.wait(max(0.1, float(timeout))):
            request.cancelled = True
            raise ControlApiError("COMMAND_TIMEOUT", "Game command did not finish in time", status=504)
        if request.error is not None:
            raise request.error
        return dict(request.result or {})

    @staticmethod
    def _control_is_current_boss_room(session: object) -> bool:
        """Return whether the active room is the run's recorded boss cell."""

        values = []
        for attr in (
            "dungeon_cell_x",
            "dungeon_cell_y",
            "dungeon_boss_x",
            "dungeon_boss_y",
        ):
            value = getattr(session, attr, None)
            if value is None:
                return False
            try:
                values.append(int(value) & 0xFF)
            except (TypeError, ValueError):
                return False
        return values[0:2] == values[2:4]

    def _control_kill_all_monsters(self, params: dict, session: object):
        if params:
            raise ControlApiError("INVALID_PARAMS", "kill_all_monsters does not accept parameters", status=400)
        if int(getattr(session, "selected_character_id", 0) or 0) <= 0:
            raise ControlApiError("SESSION_NOT_READY", "current client has no selected character")
        was_boss_room = self._control_is_current_boss_room(session)
        phase_before = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        packets, result, note = self.die_monster_0027.handle_control_kill_all_monsters(
            session=session
        )
        result = dict(result)
        result["is_boss_room"] = was_boss_room
        result["settlement_started"] = False

        # The normal combat death chain owns actor removal, drops, quest state,
        # and ENABLE_CLEAR.  Only an X clear that just cleared the recorded
        # boss room may continue into the established 002E settlement chain.
        phase_after_kills = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        if was_boss_room and phase_before == 0 and phase_after_kills == 1:
            raid_hooks = local_raid_hooks(self)
            binding = raid_hooks.bridge.current(session) if raid_hooks is not None else None
            if binding is not None:
                # A raid's ordinary 002E requires the successful 001F send,
                # not merely the kill handler's returned ENABLE_CLEAR object.
                session._control_raid_settlement = (binding, result)
                result['clear_phase'] = phase_after_kills
                return packets, result, note + '; control_raid_settlement=waiting_for_clear_send'
            settlement_packets, settlement_note = self.set_play_result_002e.handle(
                b"", session=session
            )
            packets.extend(settlement_packets)
            result["settlement_started"] = (
                int(getattr(session, "dungeon_clear_phase", 0) or 0) >= 2
            )
            note = "%s; control_boss_settlement started=%s %s" % (
                note,
                int(result["settlement_started"]),
                settlement_note,
            )
        else:
            note = "%s; control_boss_settlement skip=boss_room:%s phase:%s->%s" % (
                note,
                int(was_boss_room),
                phase_before,
                phase_after_kills,
            )
        result["clear_phase"] = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        return packets, result, note

    def _finish_control_raid_settlement(self, context, command):
        if command != 'dungeon.kill_all_monsters':
            return
        session = context.session
        pending = getattr(session, '_control_raid_settlement', None)
        if pending is None:
            return
        session._control_raid_settlement = None
        raid_hooks = local_raid_hooks(self)
        if (raid_hooks is None or raid_hooks.bridge.current(session) is not pending[0]
                or int(getattr(session, 'dungeon_clear_phase', 0) or 0) != 1):
            return
        packets, note = raid_hooks.handle_opcode(0x002E,
            self.set_play_result_002e.handle, b'', session=session)
        if packets:
            self._send_control_packets(context, packets,
                command='dungeon.kill_all_monsters.settlement', note=note)
        pending[1]['settlement_started'] = int(getattr(session, 'dungeon_clear_phase', 0) or 0) >= 2
        pending[1]['clear_phase'] = int(getattr(session, 'dungeon_clear_phase', 0) or 0)

    def _control_move_adjacent(self, params: dict, session: object):
        if set(params) != {"direction"}:
            raise ControlApiError("INVALID_PARAMS", "move_adjacent requires only direction", status=400)
        if int(getattr(session, "selected_character_id", 0) or 0) <= 0:
            raise ControlApiError("SESSION_NOT_READY", "current client has no selected character")
        return self.move_map_002d.handle_control_move_adjacent(
            direction=str(params.get("direction") or ""),
            session=session,
        )

    def _send_control_packets(
        self,
        context: _ControlGameContext,
        packets: list[bytes],
        *,
        command: str,
        note: str,
    ) -> None:
        boss_settlement_gap_ms = 0
        if command == "dungeon.kill_all_monsters":
            boss_settlement_gap_ms = max(
                0, _env_int("USDOF_GAME_CONTROL_BOSS_SETTLEMENT_DELAY_MS", 250)
            )
        clear_enable_sent = False
        for index, response in enumerate(packets):
            packet_key = _packet_key(response)
            if clear_enable_sent and packet_key == (0, 0x0022) and boss_settlement_gap_ms > 0:
                self.log(
                    "[Control] boss clear settlement gap=%sms before S0/0022"
                    % boss_settlement_gap_ms
                )
                time.sleep(boss_settlement_gap_ms / 1000.0)
                clear_enable_sent = False
            self._log_game_sequence(
                direction="S->C control",
                connection_id=context.connection_id,
                packet=response,
                compact_prefix=(
                    _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                ),
                semantic=opcode_meaning(*packet_key),
                note="control command=%s index=%s; %s" % (command, index, note),
            )
            if _s2c_plain_wire_enabled():
                self._log_plain_wire_packet("send-control", response)
            send_raid_packet(self, context.session, context.client, response)
            if packet_key == (0, 0x001F):
                clear_enable_sent = True
        self._finish_control_raid_settlement(context, command)

    def _drain_control_commands(self, context: _ControlGameContext) -> None:
        """Execute queued commands between complete client receive operations."""

        for _unused in range(16):
            try:
                request = context.commands.get_nowait()
            except queue.Empty:
                return
            quest_control_scope = request.command in {
                "quest.complete_accepted",
                "quest.accept_unfinished",
            }
            try:
                if request.cancelled:
                    request.error = ControlApiError(
                        "COMMAND_TIMEOUT",
                        "control command expired before execution",
                        status=504,
                    )
                    continue
                if not self._is_current_control_context(context):
                    request.error = ControlApiError(
                        "STALE_SESSION",
                        "the current Game connection changed",
                        status=409,
                    )
                    continue
                handler = self._control_handlers.get(request.command)
                if handler is None:
                    request.error = ControlApiError(
                        "UNKNOWN_COMMAND",
                        "unknown control command",
                        status=404,
                    )
                    continue
                packets, result, note = handler(request.params, context.session)
                control_delayed_packets: list[tuple[bytes, str]] = []
                if quest_control_scope:
                    control_delayed_packets = self.pop_delayed_response_packets()
                if not self._is_current_control_context(context):
                    request.error = ControlApiError(
                        "STALE_SESSION",
                        "the current Game connection changed",
                        status=409,
                    )
                    continue
                self._send_control_packets(
                    context,
                    list(packets),
                    command=request.command,
                    note=note,
                )
                if control_delayed_packets:
                    first_delay_ms = max(
                        0,
                        _env_int("USDOF_GAME_CONTROL_QUEST_FIRST_DELAY_MS", 150),
                    )
                    packet_gap_ms = max(
                        0,
                        _env_int("USDOF_GAME_CONTROL_QUEST_PACKET_GAP_MS", 60),
                    )
                    if first_delay_ms:
                        time.sleep(first_delay_ms / 1000.0)
                    for delayed_index, (packet, delayed_note) in enumerate(
                        control_delayed_packets
                    ):
                        if delayed_index and packet_gap_ms:
                            time.sleep(packet_gap_ms / 1000.0)
                        self._send_control_packets(
                            context,
                            [packet],
                            command=request.command,
                            note="control delayed index=%d; %s"
                            % (delayed_index, delayed_note),
                        )
                    result = dict(result)
                    result["delayed_packet_count"] = len(control_delayed_packets)
                request.result = dict(result)
                self.log(
                    "[Control] Game command=%s epoch=%s packets=%s %s"
                    % (request.command, context.generation, len(packets), note)
                )
            except ControlApiError as exc:
                request.error = exc
            except OSError as exc:
                request.error = ControlApiError(
                    "GAME_CLIENT_DISCONNECTED",
                    "Game client disconnected while sending command results",
                    status=503,
                )
                self.log("[Control] send failed: %r" % (exc,))
            except Exception as exc:
                request.error = ControlApiError(
                    "INTERNAL_ERROR",
                    "Game command failed",
                    status=500,
                )
                self.log("[Control] command=%s failed: %r" % (request.command, exc))
            finally:
                if quest_control_scope:
                    stale_delayed = self.pop_delayed_response_packets()
                    if stale_delayed:
                        self.log(
                            "[Control] discarded quest delayed packets after command "
                            "completion count=%d" % len(stale_delayed)
                        )
                request.done.set()

    def _decode_client_piv_echo(self, packet: bytes) -> tuple[int, int] | None:
        """Decode C->S 0x013E body to (echoed_flag, echoed_opcode) if possible."""
        try:
            header = parse_game_header(packet[:0x10])
        except Exception:
            return None
        if int(header.opcode) != 0x013E:
            return None
        payload_after = packet[0x10:]
        if int(getattr(header, "payload_len", 0) or 0) > 0:
            codec_body = header.reserved + payload_after
        else:
            codec_body = b""
        try:
            decode = decode_payload_skeleton(header.opcode, codec_body)
            piv = parse_piv_plain(decode.plain)
        except Exception:
            return None
        if piv is None:
            return None
        return (int(piv.echoed_flag) & 0xFF, int(piv.echoed_opcode) & 0xFFFF)

    def _send_cold_charselect_recovery(
        self, client, session, packets, *, connection_id, step_mode,
    ):
        """Send the already built roster once; retain the normal followup gap.

        Runs on this connection's request thread. No repository writes, global
        coordination lock, fake PIV, or retry of a potentially partial send.
        """
        first, *remaining = packets
        if getattr(session, "selected_character_id", None) is not None:
            raise RuntimeError("cold roster recovery after character selection")
        if int(self.account_id) != int(session.account_id):
            raise RuntimeError("cold roster recovery account ownership mismatch")
        response, note = first
        send_raid_packet(self, session, client, response)
        self.log(
            "[Game/ColdCharselect] recovered requested roster after missing 0004 PIV "
            f"account_id={session.account_id} conn={connection_id} bytes={len(response)}; {note}"
        )
        wait_key = _packet_key(response)
        if step_mode not in {"delay", "delayed", "time", "timer"}:
            return remaining, wait_key
        if remaining:
            time.sleep(max(0.05, _env_int("USDOF_GAME_FOLLOWUP_DELAY_MS", 150) / 1000.0))
        for index, (response, _note) in enumerate(remaining):
            if index:
                time.sleep(max(0.0, _env_int("USDOF_GAME_FOLLOWUP_INTERPACKET_DELAY_MS", 60) / 1000.0))
            send_raid_packet(self, session, client, response)
        if remaining:
            self.log(
                "[Game/ColdCharselect] recovered roster followups sent "
                f"account_id={session.account_id} conn={connection_id} count={len(remaining)}"
            )
        return [], None

    def _wait_for_client_piv_echo(
        self,
        client: socket.socket,
        buffer: bytearray,
        capture,
        *,
        want_flag: int,
        want_opcode: int,
        timeout_ms: int,
        connection_id: str = "",
    ) -> tuple[bool, str]:
        """等客户端 PIV 回显指定 S->C 包；匹配帧从 buffer 移除，其它完整帧保留。

        进房装备硬约束：USERINFO1/166AED0 必须在 0131 主对象就绪后。
        阻塞发送 delayed 前先 drain/wait 0131 PIV，避免 U1 落在对象未注册窗口。
        """
        want = (int(want_flag) & 0xFF, int(want_opcode) & 0xFFFF)
        timeout_ms = max(0, int(timeout_ms))
        if timeout_ms <= 0:
            return False, "piv_wait_disabled"
        deadline = time.time() + (timeout_ms / 1000.0)
        scanned = 0

        def _scan_once() -> bool:
            nonlocal scanned
            offset = 0
            while offset + COMPACT_GAME_HEADER_SIZE <= len(buffer):
                try:
                    header = parse_game_header(bytes(buffer[offset : offset + 0x10]))
                except GameWireError:
                    break
                total = int(header.length)
                if total <= 0 or offset + total > len(buffer):
                    break
                pkt = bytes(buffer[offset : offset + total])
                scanned += 1
                echoed = self._decode_client_piv_echo(pkt)
                if echoed == want:
                    del buffer[offset : offset + total]
                    return True
                offset += total
            return False

        if _scan_once():
            note = (
                "piv_already_buffered flag=%s/opcode=0x%04x scanned=%s buf=%s"
                % (want[0], want[1], scanned, len(buffer))
            )
            self.log("[Game/EnterSeria] %s conn=%s" % (note, connection_id or "-"))
            return True, note

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                note = (
                    "piv_timeout flag=%s/opcode=0x%04x timeout_ms=%s scanned=%s buf=%s"
                    % (want[0], want[1], timeout_ms, scanned, len(buffer))
                )
                self.log("[Game/EnterSeria] %s conn=%s" % (note, connection_id or "-"))
                return False, note
            try:
                ready, _, _ = select.select([client], [], [], min(0.05, remaining))
            except (OSError, ValueError) as exc:
                note = "piv_wait_select_fail %r" % (exc,)
                self.log("[Game/EnterSeria] %s" % note)
                return False, note
            if not ready:
                continue
            try:
                chunk = client.recv(4096)
            except OSError as exc:
                note = "piv_wait_recv_fail %r" % (exc,)
                self.log("[Game/EnterSeria] %s" % note)
                return False, note
            if not chunk:
                note = "piv_wait_client_closed"
                self.log("[Game/EnterSeria] %s" % note)
                return False, note
            try:
                capture.write(chunk)
                capture.flush()
            except Exception:
                pass
            buffer.extend(chunk)
            if _scan_once():
                note = (
                    "piv_seen flag=%s/opcode=0x%04x scanned=%s buf=%s"
                    % (want[0], want[1], scanned, len(buffer))
                )
                self.log("[Game/EnterSeria] %s conn=%s" % (note, connection_id or "-"))
                return True, note

    def _send_enter_seria_delayed_timed(
        self,
        client: socket.socket,
        delayed_response_packets: list[tuple[bytes, str]],
        *,
        connection_id: str,
        inter_ms: int,
        log_tag: str = "enter-seria-burst-after-0131-no0324",
        session_state: Any = None,
    ) -> str:
        """按装备栏硬间隔发送进房 delayed（U0→U1→000E）。"""
        if not delayed_response_packets:
            return "delayed_empty"
        prev_pkt: bytes | None = None
        prev_note = ""
        for index, (response, delayed_note) in enumerate(delayed_response_packets):
            if index > 0 and prev_pkt is not None:
                gap_ms = _enter_seria_followup_gap_ms(
                    prev_pkt,
                    prev_note,
                    response,
                    delayed_note,
                    inter_ms,
                )
                if gap_ms > 0:
                    if gap_ms > inter_ms:
                        self.log(
                            "[Game/P1Gate] enter-seria equip-order gap "
                            f"{gap_ms}ms before next "
                            f"({_packet_key_text(response)}; {delayed_note[:80]})"
                        )
                    time.sleep(gap_ms / 1000.0)
            try:
                self._log_game_sequence(
                    direction="S->C delayed",
                    connection_id=connection_id,
                    packet=response,
                    compact_prefix=(
                        _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                    ),
                    semantic=opcode_meaning(*_packet_key(response)),
                    note=(
                        "delayed response index="
                        + str(index)
                        + " "
                        + log_tag
                        + "; "
                        + delayed_note
                    ),
                )
                if _s2c_plain_wire_enabled():
                    self._log_plain_wire_packet("send-delayed", response)
                send_raid_packet(self, session_state, client, response)
                if self.diagnostic_trace and self.protocol_traffic_log:
                    self.log(
                        "[Game/P1Gate] "
                        + log_tag
                        + " index="
                        + str(index)
                        + " "
                        + delayed_note
                        + " bytes="
                        + str(len(response))
                        + " hex="
                        + response.hex(" ")
                    )
            except OSError as exc:
                self.log(
                    "[Game/P1Gate] enter-seria burst send failed: "
                    + str(exc)
                    + "; closing client handler"
                )
                raise
            prev_pkt = response
            prev_note = delayed_note
        return "sent=%s" % len(delayed_response_packets)

    def _send_enter_seria_equip_refresh(
        self,
        client: socket.socket,
        session_state: Any,
        *,
        reason: str,
        connection_id: str = "",
        apply_initial_delay: bool = False,
        mark_refreshed: bool | None = None,
    ) -> str:
        """发送进房装备栏补刷。

        见 ``helper-enter_seria_equip_refresh``。
        - post-settle：默认关闭，避免主进房后重复写装备槽
        - on-008c：默认关闭，避免 UI settle 后再次写装备槽
        """
        helper = load_chain("helper-enter_seria_equip_refresh.py")
        tag = str(reason or "post-settle").strip() or "post-settle"

        if tag.startswith("post-settle") and not helper.post_settle_equip_refresh_enabled():
            return "post_settle_equip_refresh=off"
        if tag.startswith("on-008c") and not helper.on_008c_equip_refresh_enabled():
            return "on_008c_equip_refresh=off"
        if bool(getattr(session_state, "enter_seria_equip_refreshed", False)):
            return "%s_equip_refresh=already" % tag
        # post-settle 仅 000E：同一次进房只发一次，避免刷屏；不阻塞 008C 终态
        if tag.startswith("post-settle") and bool(
            getattr(session_state, "enter_seria_post_settle_000e_sent", False)
        ):
            return "%s_equip_refresh=already_000e" % tag

        cid = getattr(session_state, "selected_character_id", None)
        if cid is None or int(cid) <= 0:
            return "%s_equip_refresh=skip no_cid" % tag

        account_id = int(getattr(self, "account_id", DEFAULT_ACCOUNT_ID) or DEFAULT_ACCOUNT_ID)
        packets, summary = helper.build_post_settle_equip_refresh_packets(
            self,
            character_id=int(cid),
            account_id=account_id,
            reason=tag,
        )
        if not packets:
            self.log(f"[Game/EnterSeria] {summary}")
            return summary

        delay_ms = helper.post_settle_equip_refresh_delay_ms() if apply_initial_delay else 0
        u1_gap_ms = helper.post_settle_equip_u1_before_000e_gap_ms()
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
            if bool(getattr(session_state, "enter_seria_equip_refreshed", False)):
                return "%s_equip_refresh=already_after_delay" % tag

        for index, (response, note) in enumerate(packets):
            try:
                self._log_game_sequence(
                    direction="S->C equip-refresh",
                    connection_id=connection_id or "game",
                    packet=response,
                    compact_prefix=(
                        _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                    ),
                    semantic=opcode_meaning(*_packet_key(response)),
                    note=note,
                )
                if _s2c_plain_wire_enabled():
                    self._log_plain_wire_packet("send-equip-refresh", response)
                send_raid_packet(self, session_state, client, response)
                self.log(
                    "[Game/EnterSeria] equip refresh reason=%s index=%s %s bytes=%s"
                    % (tag, index, note, len(response))
                )
            except OSError as exc:
                self.log(
                    "[Game/EnterSeria] equip refresh send failed reason=%s: %s"
                    % (tag, exc)
                )
                return "%s_equip_refresh=send_fail %s" % (tag, exc)
            # U1 → 000E：给 166AED0 建槽时间
            if index + 1 < len(packets) and u1_gap_ms > 0:
                try:
                    _pf, prev_op = _packet_key(response)
                    _nf, next_op = _packet_key(packets[index + 1][0])
                except Exception:
                    prev_op, next_op = (None, None)
                if prev_op == 0x0002 and next_op == 0x000E:
                    time.sleep(u1_gap_ms / 1000.0)

        # 终态 mark：008C 完整补刷默认 mark；post-settle 仅 000E 不 mark
        if mark_refreshed is None:
            mark_refreshed = bool(
                tag.startswith("on-008c") or helper.resend_userinfo1_enabled(tag)
            )
        try:
            if tag.startswith("post-settle"):
                session_state.enter_seria_post_settle_000e_sent = True
            if mark_refreshed:
                session_state.enter_seria_equip_refreshed = True
        except Exception:
            pass
        self.log(f"[Game/EnterSeria] {summary}")
        return summary

    def _maybe_send_post_settle_equip_refresh(
        self,
        client: socket.socket,
        session_state: Any,
        *,
        connection_id: str = "",
    ) -> str:
        """进房 delayed 完成后的 000E 兜底（短 delay、不 mark 终态，留给 008C）。"""
        return self._send_enter_seria_equip_refresh(
            client,
            session_state,
            reason="post-settle",
            connection_id=connection_id,
            apply_initial_delay=True,
            mark_refreshed=False,
        )

    def _maybe_send_equip_refresh_on_008c(
        self,
        client: socket.socket,
        session_state: Any,
        *,
        connection_id: str = "",
    ) -> str:
        """客户端进房后首个 C1/008C：UI settle 后补 000E（默认不重发 U1）。"""
        if not bool(getattr(session_state, "enter_seria_settled", False)):
            return "on_008c_equip_refresh=skip not_settled"
        if bool(getattr(session_state, "enter_seria_in_progress", False)):
            return "on_008c_equip_refresh=skip still_in_progress"
        return self._send_enter_seria_equip_refresh(
            client,
            session_state,
            reason="on-008c",
            connection_id=connection_id,
            apply_initial_delay=False,
            mark_refreshed=True,
        )

    def _maybe_send_wish_snapshot_on_008c(
        self,
        client: socket.socket,
        session_state: Any,
        *,
        connection_id: str = "",
    ) -> str:
        """Reassert the character wish snapshot after the enter-room UI settles.

        The initial ``S0/050A`` is part of the delayed START_MAP stream.  The
        frozen client acknowledges that packet before the first post-enter
        ``C1/008C`` UI request, so this boundary is the first server-owned
        point at which a second snapshot can repair a client-side projection
        without racing the character-object construction sequence.
        """
        if not bool(getattr(session_state, "enter_seria_settled", False)):
            return "post_settle_wish_snapshot=skip not_settled"
        if bool(getattr(session_state, "enter_seria_in_progress", False)):
            return "post_settle_wish_snapshot=skip still_in_progress"
        if bool(
            getattr(session_state, "enter_seria_wish_snapshot_post_settle_sent", False)
        ):
            return "post_settle_wish_snapshot=already"

        cid = int(getattr(session_state, "selected_character_id", 0) or 0)
        if cid <= 0:
            return "post_settle_wish_snapshot=skip no_cid"

        try:
            from game.drop.wish_service import (
                build_wish_items_snapshot_packet,
                hydrate_session_wish_items,
            )

            wish_ids = hydrate_session_wish_items(
                self,
                session_state,
                character_id=cid,
            )
            response, snapshot_note = build_wish_items_snapshot_packet(
                self,
                wish_ids,
            )
        except Exception as exc:
            return "post_settle_wish_snapshot=build_fail cid=%d err=%s:%s" % (
                cid,
                type(exc).__name__,
                exc,
            )

        try:
            self._log_game_sequence(
                direction="S->C wish-refresh",
                connection_id=connection_id or "game",
                packet=response,
                compact_prefix=(
                    _s2c_plain_wire_enabled() and _plain_wire_header_mode() == "13"
                ),
                semantic=opcode_meaning(*_packet_key(response)),
                note="post-settle C1/008C; " + snapshot_note,
            )
            if _s2c_plain_wire_enabled():
                self._log_plain_wire_packet("send-wish-refresh", response)
            send_raid_packet(self, session_state, client, response)
        except OSError as exc:
            self.log(
                "[Game/EnterSeria] wish snapshot send failed cid=%d: %s" % (cid, exc)
            )
            return "post_settle_wish_snapshot=send_fail cid=%d err=%s" % (cid, exc)

        try:
            session_state.enter_seria_wish_snapshot_post_settle_sent = True
        except Exception:
            pass
        self.log(
            "[Game/EnterSeria] wish snapshot post-settle sent cid=%d %s"
            % (cid, snapshot_note)
        )
        return "post_settle_wish_snapshot=sent cid=%d %s" % (cid, snapshot_note)

    def _maybe_wait_enter_seria_after_0131_gate(
        self,
        delayed_response_packets: list[tuple[bytes, str]],
    ) -> str:
        """Wait after S->C 0x0131 has been sent and before delayed enter-seria packets.

        中文维护说明：
        - 这个闸门只服务于当前 0x0324/0x0004 handler 的 x32dbg 动态验证，默认关闭。
        - 它运行在 immediate response 发送之后，因此客户端已经收到 0x0131；操作者可在
          角色点击后、0x0324/0x0004 前 attach x32dbg，避免提前 attach 让点击链路卡死。
        - release 文件出现后才放行延迟队列；若超时且 continue_on_timeout=1，则记录后继续。
        """

        if not any("enter-seria-after-0131" in note for _, note in delayed_response_packets):
            return ""
        if not _env_bool("USDOF_GAME_ENTER_SERIA_AFTER_0131_WAIT", False):
            return ""

        ready_path = Path(
            os.environ.get(
                "USDOF_GAME_ENTER_SERIA_AFTER_0131_READY_FILE",
                str(LOG_DIR / "enter_seria_after_0131_ready.json"),
            ).strip()
        )
        release_path = Path(
            os.environ.get(
                "USDOF_GAME_ENTER_SERIA_AFTER_0131_RELEASE_FILE",
                str(LOG_DIR / "enter_seria_after_0131_release.flag"),
            ).strip()
        )
        timeout_ms = max(0, _env_int("USDOF_GAME_ENTER_SERIA_AFTER_0131_TIMEOUT_MS", 30000))
        poll_seconds = max(0.05, _env_int("USDOF_GAME_ENTER_SERIA_AFTER_0131_POLL_MS", 100) / 1000.0)
        clear_release = _env_bool("USDOF_GAME_ENTER_SERIA_AFTER_0131_CLEAR_RELEASE", True)
        continue_on_timeout = _env_bool("USDOF_GAME_ENTER_SERIA_AFTER_0131_CONTINUE_ON_TIMEOUT", True)
        nonce = f"{int(time.time() * 1000)}-{os.getpid()}"
        payload = {
            "nonce": nonce,
            "stage": "enter-seria-after-0131",
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "delayed_packets": [
                {
                    "index": index,
                    "flag": _packet_key(packet)[0],
                    "opcode": f"0x{_packet_key(packet)[1]:04x}",
                    "wire_len": len(packet),
                    "note": note,
                }
                for index, (packet, note) in enumerate(delayed_response_packets)
            ],
            "release_file": str(release_path),
            "timeout_ms": timeout_ms,
            "note": (
                "S->C 0x0131 immediate response has already been sent; "
                "service is waiting before delayed S->C 0x0324/0x0004 packets"
            ),
        }

        try:
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            release_path.parent.mkdir(parents=True, exist_ok=True)
            if clear_release and release_path.exists():
                release_path.unlink()
            ready_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            note = (
                " enter_seria_after_0131_gate write_failed="
                f"{type(exc).__name__}:{exc} ready={ready_path} release={release_path}"
            )
            self.log("[Game/EnterSeriaGate]" + note)
            return note

        self.log(
            "[Game/EnterSeriaGate] after-0131 wait "
            f"nonce={nonce} ready={ready_path} release={release_path} timeout_ms={timeout_ms}"
        )
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if release_path.exists():
                try:
                    release_text = release_path.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    release_text = ""
                note = (
                    " enter_seria_after_0131_gate released "
                    f"nonce={nonce} ready={ready_path} release={release_path}"
                    f"{' release_note=' + release_text[:120] if release_text else ''}"
                )
                self.log("[Game/EnterSeriaGate] after-0131 released " f"nonce={nonce} release={release_path}")
                return note
            if timeout_ms == 0 or time.time() >= deadline:
                note = (
                    " enter_seria_after_0131_gate timeout "
                    f"nonce={nonce} timeout_ms={timeout_ms} ready={ready_path} release={release_path}"
                )
                self.log("[Game/EnterSeriaGate]" + note)
                if continue_on_timeout:
                    return note + " continue_on_timeout=1"
                while not release_path.exists():
                    time.sleep(poll_seconds)
                return note + " late_release_after_timeout"
            time.sleep(poll_seconds)

    def set_listen_ports(self, ports: list[int]) -> None:
        """Configure Game TCP listen ports (unique per channel_id mapping)."""

        cleaned: list[int] = []
        seen: set[int] = set()
        for port in ports:
            value = int(port) & 0xFFFF
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            cleaned.append(value)
        if not cleaned:
            cleaned = [int(self.port)]
        self.listen_ports = cleaned
        self.port = cleaned[0]

    def _resolve_channel_id_for_port(self, local_port: int) -> int | None:
        """Recover PVF channel_id from local Game listen port when unique ports are on."""

        try:
            from channel.channel_info import channel_id_from_game_port
        except Exception:
            return None
        try:
            return channel_id_from_game_port(int(local_port), base_port=int(os.environ.get("USDOF_CHANNELINFO_PORT_BASE", str(GAME_PORT))))
        except Exception:
            return None

    def serve_forever(self) -> None:
        self.log(f"[Game] listening on {self.host}:{self.port} (primary); multi-port enabled")
        self.log(f"[Game] initial probes: {describe_probe_specs(self.initial_probes)}")
        self.log(f"[Game] PIV follow-up probes: {describe_probe_specs(self.piv_response_probes)}")
        self.log(f"[Game] ACK follow-up probes: {describe_probe_specs(self.ack_response_probes)}")
        try:
            self.force_exp_notify.start()
        except Exception as exc:
            self.log(f"[ForceExp] start failed: {exc!r}")
        c2s_plain_wire = _c2s_plain_wire_enabled()
        s2c_plain_wire = _s2c_plain_wire_enabled()
        self.log(
            "[Game/CodecMode] "
            f"C2S={'plain-compact13' if c2s_plain_wire else 'native-codec'} "
            f"S2C={'plain-header-' + _plain_wire_header_mode() if s2c_plain_wire else 'native-codec'}"
        )
        if self.packet_sequence_logger is not None:
            self.log(f"[PacketSeq] enabled dir={self.packet_sequence_logger.log_dir}")
        if self.dynamic_userinfo:
            repo = self._repo()
            self.log(
                f"[Game/DB] dynamic USERINFO enabled account_id={self.account_id} "
                f"m_id={self.account_m_id!r} launcher_account_id={self.launcher_account_id} "
                f"db={repo.db_path} roster_profile={self.userinfo_roster_profile}"
            )
            self.log(
                "[Game/Auth] standalone builtin account/session is bound on Game accept; "
                f"db_account_id={self.account_id} m_id={self.account_m_id!r} "
                f"launcher_account_id={self.launcher_account_id} "
                f"launcher_token_sha1={self.launcher_token[:8]}..."
            )
        if self.ack_response_probes:
            self.log(
                "[Game] note: opcode2 plaintext follow-ups are encoded with the local codec2 seed0 vector; "
                "use the matching *_codecseed0_noti_gate_noskip client until real Game key sync is implemented"
            )
        ports = list(self.listen_ports) if getattr(self, "listen_ports", None) else [int(self.port)]
        ports.extend(self.legacy_game_ports)
        self.log(f"[Game] multi-listen ports={ports} count={len(ports)}")
        servers: list[socket.socket] = []
        try:
            for port in ports:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                servers.append(server)
                if port in self.legacy_game_ports and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, int(port)))
                server.listen(64)
                # Non-blocking accept + select: do NOT use per-socket 1s timeout,
                # otherwise ~N channels can delay Game accept by many seconds.
                server.setblocking(False)
            self._ready.set()
            self.log(f"[Game] multi-listen ready host={self.host} ports={len(servers)}")
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select(servers, [], [], 0.2)
                except (InterruptedError, ValueError, OSError):
                    if self._stop.is_set():
                        break
                    time.sleep(0.01)
                    continue
                for server in readable:
                    while True:
                        try:
                            client, addr = server.accept()
                        except BlockingIOError:
                            break
                        except OSError as exc:
                            self.log(f"[Game] accept failed on {server.getsockname()}: {exc}")
                            break
                        try:
                            client.setblocking(True)
                        except OSError:
                            pass
                        configure_game_tcp_socket(client, self.log)
                        try:
                            local_port = int(server.getsockname()[1])
                        except Exception:
                            local_port = int(self.port)
                        thread = threading.Thread(
                            target=self.handle_client,
                            args=(client, addr, local_port),
                            daemon=True,
                        )
                        thread.start()
        finally:
            self._ready.clear()
            for server in servers:
                try:
                    server.close()
                except Exception:
                    pass
            self._stopped.set()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.force_exp_notify.stop()
        except Exception:
            pass

    def wait_ready(self, timeout: float) -> bool:
        return bool(self._ready.wait(max(0.0, float(timeout))))

    def _repo(self) -> CharacterRepository:
        if self._character_repo is None:
            self._character_repo = CharacterRepository(self.db_path)
            self._character_repo.ensure_account(self.account_id, self.account_m_id)
            if _env_bool("USDOF_BOOTSTRAP_BUILTIN_CHARACTER", False):
                builtin = self._character_repo.ensure_builtin_character(
                    account_id=self.account_id,
                    character_id=int(
                        os.environ.get("USDOF_BUILTIN_CHARACTER_ID", str(DEFAULT_BUILTIN_CHARACTER_ID)),
                        0,
                    ),
                    name=os.environ.get("USDOF_BUILTIN_CHARACTER_NAME", DEFAULT_BUILTIN_CHARACTER_NAME),
                    job=int(os.environ.get("USDOF_BUILTIN_CHARACTER_JOB", str(DEFAULT_BUILTIN_CHARACTER_JOB)), 0),
                    grow_type=int(
                        os.environ.get("USDOF_BUILTIN_CHARACTER_GROW_TYPE", str(DEFAULT_BUILTIN_CHARACTER_GROW_TYPE)),
                        0,
                    ),
                    level=int(os.environ.get("USDOF_BUILTIN_CHARACTER_LEVEL", str(DEFAULT_BUILTIN_CHARACTER_LEVEL)), 0),
                    slot_index=int(os.environ.get("USDOF_BUILTIN_CHARACTER_SLOT", "0"), 0),
                )
                self.log(
                    "[Game/Auth] builtin character bound "
                    f"character_id={builtin.character_id} slot={builtin.slot_index} "
                    f"name={builtin.display_name!r} job={builtin.job} "
                    f"grow_type={builtin.grow_type} level={builtin.level}"
                )
            changed, total = self._character_repo.ensure_all_avatar_container_capacity()
            self.log(
                "[Game/DB] avatar container capacity "
                f"backfill changed={changed} total={total} list_param16="
                f"{self._character_repo.MAX_AVATAR_LIST_PARAM16}"
            )
        return self._character_repo

    def _log_plain_wire_packet(self, direction: str, packet: bytes) -> None:
        if not self.diagnostic_trace or not self.protocol_traffic_log:
            return
        try:
            header = parse_game_header(packet[:0x10])
            compact_prefix = not direction.startswith("send") or _plain_wire_header_mode() == "13"
            body = game_packet_body(packet, header, compact_prefix=compact_prefix)
            self.log(
                f"[Game/PlainWire] {direction} "
                f"flag=0x{header.flag_or_magic:02x} opcode=0x{header.opcode:04x} "
                f"total_len={header.length} body_len={len(body)} "
                f"body64={body[:64].hex(' ')}"
            )
        except Exception as exc:
            self.log(f"[Game/PlainWire] {direction} packet-log failed: {exc}; raw64={packet[:64].hex(' ')}")

    def _log_game_sequence(
        self,
        *,
        direction: str,
        connection_id: str,
        packet: bytes,
        compact_prefix: bool,
        plain_body: bytes | None = None,
        semantic: str = "",
        note: str = "",
        decode_note: str = "",
        decision_note: str = "",
    ) -> None:
        if self.packet_sequence_logger is None or not self.protocol_traffic_log:
            return
        try:
            header = parse_game_header(packet[:0x10])
            header_len = COMPACT_GAME_HEADER_SIZE if compact_prefix else len(header.raw)
            if plain_body is None:
                plain_body = game_packet_body(packet, header, compact_prefix=compact_prefix)
            extra_note_parts = [part for part in (note, decode_note, decision_note) if part]
            combined_note = "; ".join(extra_note_parts)
            extra = build_packet_extra(
                protocol="game",
                direction=direction,
                flag=header.flag_or_magic,
                opcode=header.opcode,
                plain_body=plain_body,
                semantic=semantic,
                note=combined_note,
                extra={
                    "codec_index": header.codec_index,
                    "checksum_or_tag": f"0x{header.checksum_or_tag:08X}",
                    "decode_note": decode_note,
                    "decision_note": decision_note,
                },
            )
            seq = self.packet_sequence_logger.log_packet(
                protocol="game",
                direction=direction,
                connection_id=connection_id,
                raw=packet[: header.length],
                plain_body=plain_body,
                flag=header.flag_or_magic,
                opcode=header.opcode,
                total_len=header.length,
                header_len=header_len,
                wire_body_len=max(0, header.length - header_len),
                tail_or_seq=header.sub_or_seq,
                semantic=semantic,
                note=combined_note,
                extra=extra,
            )
            if seq is not None:
                self.log(
                    f"[PacketSeq] seq={seq:06d} game {direction} "
                    f"flag=0x{header.flag_or_magic:02X} opcode=0x{header.opcode:04X} "
                    f"plain={len(plain_body)} raw={header.length}"
                )
        except Exception as exc:
            self.log(f"[PacketSeq] game {direction} log failed: {exc}; raw64={packet[:64].hex(' ')}")

    @staticmethod
    def _raw_response(flag: int, opcode: int, payload: bytes = b"", **packet_kwargs) -> bytes:
        return _build_server_game_packet(flag, opcode, payload, **packet_kwargs)

    @staticmethod
    def _codec_response(flag: int, opcode: int, plain_payload: bytes = b"", **packet_kwargs) -> bytes:
        payload = plain_payload
        if not _s2c_plain_wire_enabled():
            idx = opcode % 14
            if idx == 0 and plain_payload:
                # 0x001C DUNGEON_INFO etc. Outer unit 0x10; inner XTEA block 8.
                payload = codec0_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 1 and plain_payload:
                payload = codec1_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 2 and plain_payload:
                payload = codec2_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 3 and plain_payload:
                payload = codec3_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 4 and plain_payload:
                payload = codec4_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 5 and plain_payload:
                payload = codec5_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 6 and plain_payload:
                payload = codec6_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 7 and plain_payload:
                payload = codec7_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 8 and plain_payload:
                payload = codec8_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 9 and plain_payload:
                payload = codec9_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 10 and plain_payload:
                payload = codec10_xor_dword(zero_pad_game_payload(plain_payload, 0x04))
            elif idx == 11 and plain_payload:
                payload = codec11_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
            elif idx == 12 and plain_payload:
                payload = codec12_seed0_encode(zero_pad_game_payload(plain_payload, 0x10))
            elif idx == 13 and plain_payload:
                payload = codec13_seed0_encode(zero_pad_game_payload(plain_payload, 0x08))
        packet = _build_server_game_packet(flag, opcode, payload, **packet_kwargs)
        from game.character.native_profile_projection import stamp_encoded_profile
        return stamp_encoded_profile(packet, flag, opcode, plain_payload)

    def _encoded_userinfo_packet(self, reason: str) -> tuple[bytes, str]:
        repo = self._repo()
        template = repo.load_userinfo_template()
        characters = repo.list_by_account(self.account_id)
        from game.character.character_rename import pending_ids
        plain = build_userinfo_type2_roster_plaintext(
            characters,
            template=template,
            profile=self.userinfo_roster_profile,
            padded=True,
            rename_pending_ids=pending_ids(repo.db_path),
        )
        encoded = plain if _s2c_plain_wire_enabled() else codec2_seed0_encode(plain)
        names = ", ".join(
            f"{c.slot_index}:{c.character_id}:{c.display_name}/j{c.job}/g{c.grow_type}/lv{c.level}/eq{len(c.equipped_entries)}"
            for c in characters
        )
        if not names:
            names = "<empty>"
        type2_count_a = int.from_bytes(plain[0x02:0x04], "little") if len(plain) >= 0x04 else -1
        type2_count_b = int.from_bytes(plain[0x04:0x06], "little") if len(plain) >= 0x06 else -1
        type2_slot_or_count = int.from_bytes(plain[0x06:0x08], "little") if len(plain) >= 0x08 else -1
        type2_entry_count = int.from_bytes(plain[0x0C:0x0E], "little") if len(plain) >= 0x0E else -1
        row0_prefix = ""
        if self.diagnostic_trace and characters and len(plain) > 0x0E:
            row0_prefix = " row0_prefix=" + plain[0x0E : min(len(plain), 0x0E + 96)].hex(" ")
        note = (
            f"DB USERINFO reason={reason} account_id={self.account_id} chars={len(characters)} "
            f"slot_limit={template.slot_limit} routing_byte7={template.pkt0_routing_byte7 & 0xFF} "
            f"profile={self.userinfo_roster_profile} "
            f"type2_counts={type2_count_a}/{type2_count_b}/{type2_slot_or_count}/entries={type2_entry_count} "
            f"type2_account_or_state={os.environ.get('USDOF_USERINFO_TYPE2_ACCOUNT_OR_STATE', '0')} "
            f"type2_ui_flag={os.environ.get('USDOF_USERINFO_TYPE2_UI_FLAG', '0')} "
            f"plain={len(plain)} encoded={len(encoded)} rows={names}{row0_prefix}"
        )
        return (
            _build_server_game_packet(
                0,
                0x0002,
                encoded,
                checksum_or_tag=template.pkt0_routing_byte7 & 0xFF,
            ),
            note,
        )

    def _encoded_userinfo_type0_empty_packet(self, reason: str) -> tuple[bytes, str]:
        repo = self._repo()
        template = repo.load_userinfo_template()
        plain = b"\x00\x00\x00"
        encoded = (
            plain
            if _s2c_plain_wire_enabled()
            else codec2_seed0_encode(zero_pad_game_payload(plain, 0x10))
        )
        note = (
            f"DB USERINFO type0-empty reason={reason} "
            "section_type=0 entry_count=0 "
            f"plain={len(plain)} encoded={len(encoded)}"
        )
        return (
            _build_server_game_packet(
                0,
                0x0002,
                encoded,
                checksum_or_tag=template.pkt0_routing_byte7 & 0xFF,
            ),
            note,
        )

    def _user_state_body(self) -> tuple[bytes | None, str]:
        """Build ``ENUM_NOTIPACKET_USER_STATE`` body for the current roster.

        USDOF handler ``0x01679AD0`` parses:

        ``u8 count``
        repeated ``u16 user_id`` + ``u8 state``

        ServerS4A12's ``EnterSelectDungeonStateBuilder.BuildUserState`` emits
        the same shape, and its default ``PlayerContext.UserState`` is 0x01.
        For this P1 gate we send the first DB character as online/selectable.
        """

        override = _env_hex_bytes("USDOF_GAME_USER_STATE_BODY")
        if override is not None:
            return override, f"override-body={override.hex(' ')}"

        repo = self._repo()
        characters = repo.list_by_account(self.account_id)
        if not characters:
            empty = b"\x00"
            return empty, "empty-roster-body=00"

        char = characters[0]
        user_id = int(os.environ.get("USDOF_GAME_USER_STATE_USER_ID", str(char.character_id & 0xFFFF)), 0) & 0xFFFF
        default_state = char.user_state if char.user_state else 0x01
        state = int(os.environ.get("USDOF_GAME_USER_STATE_VALUE", str(default_state)), 0) & 0xFF
        body = bytes([0x01]) + user_id.to_bytes(2, "little") + bytes([state])
        return body, f"character_id={char.character_id}/user_id=0x{user_id:04x}/state=0x{state:02x}"

    def _maybe_user_state_packet(self) -> tuple[bytes | None, str]:
        enabled = os.environ.get("USDOF_GAME_SEND_USER_STATE", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            return None, "user-state-disabled"
        body, note = self._user_state_body()
        if body is None:
            return None, f"user-state-skipped:{note}"
        packet = self._codec_response(0, 0x0003, body)
        return packet, f"user-state flag=0/opcode=0x0003/plain={len(body)}/body={body.hex(' ')}/wire={len(packet)} {note}"

    def _card_frame_006a_packets(self) -> tuple[list[bytes], str]:
        """Build optional ``0x006A`` character-card frame refresh packets.

        Current USDOF EXE reversing maps ``0x006A`` to ``0x01632450``.  The
        handler reads ``u16 frame`` then ``u16 context``, finds the character
        row via ``0x01E7F0F0(context)``, stores ``frame`` at row ``+724``, and
        refreshes the select-character card frame.  Keep this behind an
        explicit switch until x32dbg confirms the final flag/context choice.
        """

        if not _env_bool("USDOF_GAME_SEND_CARD_FRAME_006A_AFTER_USERINFO", False):
            return [], ""

        repo = self._repo()
        characters = repo.list_by_account(self.account_id)
        if not characters:
            return [], "card-frame-006a-empty-roster"

        flag = _env_optional_int("USDOF_GAME_CARD_FRAME_006A_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        frame = int(os.environ.get("USDOF_GAME_CARD_FRAME_006A_FRAME", "3"), 0) & 0xFFFF
        context_mode = os.environ.get("USDOF_GAME_CARD_FRAME_006A_CONTEXT", "slot").strip().lower()
        context_override = _env_optional_int("USDOF_GAME_CARD_FRAME_006A_CONTEXT_VALUE")

        packets: list[bytes] = []
        parts: list[str] = []
        for index, character in enumerate(characters):
            if context_override is not None:
                context = context_override
                context_desc = "override"
            elif context_mode in {"slot", "slot-index", "slot_index"}:
                context = character.slot_index
                context_desc = "slot"
            elif context_mode in {"entry", "entry-index", "entry_index", "index"}:
                context = index
                context_desc = "entry-index"
            elif context_mode in {"character-id", "character_id", "char-id", "char_id", "id"}:
                context = character.character_id
                context_desc = "character-id"
            else:
                context = int(context_mode, 0)
                context_desc = "literal"
            context &= 0xFFFF
            body = frame.to_bytes(2, "little") + context.to_bytes(2, "little")
            packet = self._codec_response(flag, 0x006A, body)
            packets.append(packet)
            parts.append(
                f"slot={character.slot_index}/char={character.character_id}/name={character.display_name!r}"
                f"/frame={frame}/context={context}({context_desc})/plain={body.hex(' ')}/wire={len(packet)}"
            )
        return packets, (
            f"card-frame-006a flag={flag}/opcode=0x006a/count={len(packets)} "
            + "; ".join(parts)
        )

    def _db_userinfo_packet_stream(
        self,
        reason: str,
        *,
        include_roster: bool = True,
        include_followups: bool = True,
    ) -> tuple[list[bytes], str]:
        packets: list[bytes] = []
        followup_notes: list[str] = []
        if include_roster:
            packet, note = self._encoded_userinfo_packet(reason)
            packets.append(packet)
            repo = self._repo()
            characters = repo.list_by_account(self.account_id)
            if characters:
                template = repo.load_userinfo_template()
                target_key = int(template.pkt0_routing_byte7) & 0xFF
                adventure_packet, adventure_note = (
                    self.contents_play_info_0578.build_adventure_group_narrow_push_0545(
                        target_key=target_key,
                        reason=reason,
                    )
                )
                # Live Stage3 decodes this USERINFO routing byte as the current
                # character-select key; S0/0545 then initializes shared AG state.
                packets.append(adventure_packet)
                followup_notes.append(adventure_note)
            else:
                followup_notes.append(
                    "adventure-group-init skipped: empty character roster"
                )
            card_frame_packets, card_frame_note = self._card_frame_006a_packets()
            packets.extend(card_frame_packets)
            if card_frame_note:
                followup_notes.append(card_frame_note)
        else:
            repo = self._repo()
            characters = repo.list_by_account(self.account_id)
            names = ", ".join(
                f"{c.slot_index}:{c.character_id}:{c.display_name}/j{c.job}/g{c.grow_type}/lv{c.level}"
                for c in characters
            ) or "<empty>"
            note = (
                f"DB USERINFO reason={reason} account_id={self.account_id} "
                f"chars={len(characters)} profile={self.userinfo_roster_profile} "
                f"roster=not-sent rows={names}"
        )
        if include_followups:
            load_extend_status = (
                0 if _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SUPPRESS_ANIMATION", True) else 1
            )
            usdof90_compact_extras = [
                # USDOF 90 dispatches these as flag0 NOTIPACKETs.  Static
                # handlers confirm 0x0286 consumes exactly one u8, and 0x01BA
                # consumes u8+u32+u16.  Live empty-roster testing showed 0x04
                # maps to "The current Channel is full", so keep 0x00 as the
                # normal local-server clear/success baseline.
                (0, 0x0286, b"\x00"),
                (0, 0x01BA, bytes([load_extend_status]) + b"\x00\x00\x00\x00\x00\x00"),
            ]
            # Optional candidate gate recovered from the USDOF 90 runtime
            # registry: flag0/opcode 0x0080 -> handler 0x01642600.  The handler
            # consumes:
            #
            #   u32, u8, u8, u8, u16, u16, u8 countA, countA*(u8,u8,u16),
            #   u8 countB, countB*u16
            #
            # Keep it *off* by default until dynamic trace proves that this is
            # the missing Start Game -> character-select gate.  This is not a
            # ServerS4A12 body; it is the minimal byte shape read by the USDOF
            # 90 handler itself.
            if _env_bool("USDOF_GAME_SEND_0080_GATE", False):
                body_0080 = _env_hex_bytes("USDOF_GAME_0080_BODY")
                if body_0080 is None:
                    body_0080 = b"\x00" * 13
                usdof90_compact_extras.append((0, 0x0080, body_0080))
            # Optional state=8 transition candidate recovered from the USDOF 90
            # runtime registry: flag0/opcode 0x008B -> handler 0x0164D1B0.
            # Dynamic trace showed that sending it before ready==true enters the
            # handler but fails its ready gate, so keep it disabled by default
            # and use it only in controlled ready-trigger ordering experiments.
            if _env_bool("USDOF_GAME_SEND_008B_STATE8_GATE", False):
                body_008b = _env_hex_bytes("USDOF_GAME_008B_BODY")
                if body_008b is None:
                    body_008b = b""
                usdof90_compact_extras.append((0, 0x008B, body_008b))
            # Optional ReadySetterB candidate recovered from the USDOF 90
            # registry:
            #
            #   flag0/opcode 0x00D1 -> handler 0x016347E0
            #
            # Static disassembly shows the handler consumes four groups.  Each
            # group starts with a u8 entry count, and every entry consumes two
            # u32 values.  A non-empty group can call:
            #
            #   0x016348B6 -> 0x02AC84D0 -> 0x011DAC60
            #     -> 0x011DBBF0 -> 0x011DBEBF mov byte ptr [esi+5], 1
            #
            # This is a USDOF90-derived experiment, not a ServerS4A12 format.
            # Keep it disabled unless an explicit trace asks for it.
            if _env_bool("USDOF_GAME_SEND_00D1_READY_GATE", False):
                body_00d1 = _env_hex_bytes("USDOF_GAME_00D1_BODY")
                if body_00d1 is None:
                    # group0: count=1, u32=0, u32=0; group1-3: count=0
                    body_00d1 = b"\x01" + (0).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"\x00\x00\x00"
                usdof90_compact_extras.append((0, 0x00D1, body_00d1))
            extras_source = os.environ.get("USDOF_GAME_USERINFO_EXTRAS_SOURCE", "usdof90-compact").strip().lower()
            if extras_source in {"0", "none", "off", "disabled"}:
                extras = []
                followup_notes.append("extra-packets-disabled")
            elif extras_source in {"db", "database", "response-blob", "blob"}:
                repo = self._repo()
                extras = repo.load_getuserinfo_extra_packets()
                if not extras:
                    extras = list(usdof90_compact_extras)
                    followup_notes.append("db-empty-fallback-usdof90-compact-extra-packets")
                else:
                    followup_notes.append(f"db-extra-packets={len(extras)}")
            elif extras_source in {"s4a12", "s4a12-current", "legacy-s4a12"}:
                # Kept for controlled A/B comparison only.  USDOF 90 does not
                # consume this as the normal character-select gate format:
                # 0x0286 only reads the first byte, and flag1 does not enter
                # the concrete NOTIPACKET handlers for these follow-ups.
                extras = [
                    (1, 0x0286, b"\x00\x04"),
                    (1, 0x01BA, b"\x01\x00\x00\x00\x00\x00\x00"),
                ]
                followup_notes.append("s4a12-current-extra-packets")
            elif extras_source in {"ready-only", "gate-only", "gates-only"}:
                extras = [
                    (flag, opcode, body)
                    for flag, opcode, body in usdof90_compact_extras
                    if opcode in {0x0080, 0x008B, 0x00D1}
                ]
                followup_notes.append("ready-gate-only-extra-packets")
            else:
                extras = list(usdof90_compact_extras)
                followup_notes.append("usdof90-compact-extra-packets")
            override_0286 = _env_hex_bytes("USDOF_GAME_0286_BODY")
            override_01ba = _env_hex_bytes("USDOF_GAME_01BA_BODY")
            override_0080 = _env_hex_bytes("USDOF_GAME_0080_BODY")
            override_008b = _env_hex_bytes("USDOF_GAME_008B_BODY")
            override_00d1 = _env_hex_bytes("USDOF_GAME_00D1_BODY")
            if (
                override_0286 is not None
                or override_01ba is not None
                or override_0080 is not None
                or override_008b is not None
                or override_00d1 is not None
            ):
                extras = [
                    (
                        flag,
                        opcode,
                        override_0286
                        if opcode == 0x0286 and override_0286 is not None
                        else override_01ba
                        if opcode == 0x01BA and override_01ba is not None
                        else override_0080
                        if opcode == 0x0080 and override_0080 is not None
                        else override_008b
                        if opcode == 0x008B and override_008b is not None
                        else override_00d1
                        if opcode == 0x00D1 and override_00d1 is not None
                        else body,
                    )
                    for flag, opcode, body in extras
                ]
                if override_0286 is not None:
                    followup_notes.append(f"override-0x0286-body={override_0286.hex(' ')}")
                if override_01ba is not None:
                    followup_notes.append(f"override-0x01ba-body={override_01ba.hex(' ')}")
                if override_0080 is not None:
                    followup_notes.append(f"override-0x0080-body={override_0080.hex(' ')}")
                if override_008b is not None:
                    followup_notes.append(f"override-0x008b-body={override_008b.hex(' ')}")
                if override_00d1 is not None:
                    followup_notes.append(f"override-0x00d1-body={override_00d1.hex(' ')}")
            force_followup_flag = _env_optional_int("USDOF_GAME_FOLLOWUP_FLAG_OVERRIDE")
            if force_followup_flag is not None:
                extras = [
                    (force_followup_flag & 0xFF, opcode, body)
                    for _flag, opcode, body in extras
                ]
                followup_notes.append(f"override-followup-flag={force_followup_flag & 0xFF}")
            for flag, opcode, body in extras:
                packets.append(self._codec_response(flag, opcode, body))
                followup_notes.append(
                    f"flag={flag}/opcode=0x{opcode:04x}/plain={len(body)}/body={body[:32].hex(' ')}"
                    f"{'...' if len(body) > 32 else ''}/wire={len(packets[-1])}"
                )
            user_state_packet, user_state_note = self._maybe_user_state_packet()
            if user_state_packet is not None:
                packets.append(user_state_packet)
            followup_notes.append(user_state_note)
        if followup_notes:
            note += " followups=[" + ", ".join(followup_notes) + "]"
        return packets, note

    def _handle_check_name(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_check_name.handle_check_name(body)

    def _handle_check_guild_name_009c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_name_check_009c.handle_check_guild_name_009c(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_member_list_0043(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_member_list_0043.handle_guild_member_list_0043(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_all_member_list_008c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_all_member_list_008c.handle_guild_all_member_list_008c(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_request_join_015c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_request_join_015c.handle_guild_request_join_015c(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_join_list_0160(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_join_list_0160.handle_guild_join_list_0160(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_approve_join_015e(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_approve_join_015e.handle_guild_approve_join_015e(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_info_of_my_chars_02e7(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_info_of_my_chars_02e7.handle_guild_info_of_my_chars_02e7(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_search_for_join_02e8(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_search_for_join_02e8.handle_guild_search_for_join_02e8(
            body,
            session=self._session_state(session),
        )

    def _handle_check_guild_create_promote_02e2(
        self,
        body: bytes,
    ) -> tuple[list[bytes], str]:
        return self.guild_create_promote_check_02e2.handle_check_guild_create_promote_02e2(body)

    def _handle_guild_invite_auth_02b3(self, body: bytes) -> tuple[list[bytes], str]:
        return self.guild_invite_auth_02b3.handle_guild_invite_auth_02b3(body)

    def _handle_guild_create_target_02e4(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_create_target_02e4.handle_guild_create_target_02e4(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_create_permit_02e5(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_create_permit_02e5.handle_guild_create_permit_02e5(
            body,
            session=self._session_state(session),
        )

    def _handle_guild_create_permit_reply_02e6(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.guild_create_permit_reply_02e6.handle_guild_create_permit_reply_02e6(
            body,
            session=self._session_state(session),
        )

    def _maybe_wait_create_debug_gate(self, stage: str, context: dict[str, object]) -> str:
        return self.char_create.maybe_wait_create_debug_gate(stage, context)

    def _handle_create_character(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_create.handle_create_character(body)

    def _handle_get_userinfo(self, body=None, *, session=None) -> tuple[list[bytes], str]:
        return self.char_get_userinfo.handle_get_userinfo(body, session=session)

    def _handle_return_select_character(
        self,
        body: bytes = b"",
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.return_select_character.handle_return_select_character(
            body,
            session=session,
        )

    def _handle_create_character_0355_refresh(self) -> tuple[list[bytes], str]:
        return self.char_create.handle_create_character_0355_refresh()

    def _handle_load_extend_characs(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_extend.handle_load_extend_characs(body)

    def _handle_load_extend_characs_finish(
        self,
        body: bytes,
        *,
        suppress_direct_response: bool = False,
    ) -> tuple[list[bytes], str]:
        return self.char_extend.handle_load_extend_characs_finish(
            body,
            suppress_direct_response=suppress_direct_response,
        )

    def _handle_extend_ready_0355(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_extend.handle_extend_ready_0355(body)

    def _load_extend_characs_pkt2_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_01ba_packet()

    def _load_extend_characs_01f9_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_01f9_packet()

    def _load_extend_characs_01fa_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_01fa_packet()

    def _load_extend_characs_01fb_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_01fb_packet()

    def _load_extend_characs_01fc_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_01fc_packet()

    def _charselect_open_0004_body(self) -> bytes:
        return self.char_extend.build_charselect_open_0004_body()

    def _charselect_open_0004_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_charselect_open_0004_packet()

    def _login_state_init_0001_body(self, *, session: GameSessionState | None = None) -> bytes:
        return self.char_extend.build_login_state_init_0001_body(session=session)

    def _login_state_init_0001_packet(self, *, session: GameSessionState | None = None) -> tuple[bytes, str]:
        return self.char_extend.build_login_state_init_0001_packet(session=session)

    def _build_reentry_open_before_userinfo_queue(
        self,
        *,
        session: GameSessionState | None = None,
        reason: str,
        login_state_init_0001_sent: bool,
        charselect_0004_sent: bool,
    ) -> tuple[list[tuple[bytes, str]], bool, bool, bool, bool]:
        """Build hard-order reentry queue: LOGIN 0x0001 → CHARSELECT 0x0004 → USERINFO.

        Returns:
            (send_queue, login_sent, charselect_sent, synthetic_0004_piv, userinfo_sent)
        """
        send_queue: list[tuple[bytes, str]] = []
        login_sent = bool(login_state_init_0001_sent)
        charselect_sent = bool(charselect_0004_sent)
        synthetic_0004_piv = False
        userinfo_sent = False
        open_order = _env_bool("USDOF_GAME_REENTRY_OPEN_BEFORE_USERINFO", True)
        if open_order and not charselect_sent:
            if not login_sent and _env_bool(
                "USDOF_GAME_SEND_STATE_INIT_0001_BEFORE_CHARSELECT_0004",
                True,
            ):
                packet, note = self._login_state_init_0001_packet(session=session)
                send_queue.append((packet, f"reentry {reason}: {note}"))
                login_sent = True
            packet, note = self._charselect_open_0004_packet()
            send_queue.append((packet, f"reentry {reason}: {note}"))
            charselect_sent = True
            if _env_bool("USDOF_GAME_REENTRY_SYNTHETIC_0004_PIV_SEEN", True):
                synthetic_0004_piv = True
        if _env_bool("USDOF_GAME_REENTRY_SEND_USERINFO_AFTER_OPEN", True):
            packets, note = self._db_userinfo_packet_stream(
                f"reentry-{reason}",
                include_roster=True,
                include_followups=False,
            )
            for response in packets:
                send_queue.append(
                    (
                        response,
                        f"reentry USERINFO after open ({reason}): {note}",
                    )
                )
            userinfo_sent = True
        return send_queue, login_sent, charselect_sent, synthetic_0004_piv, userinfo_sent

    def _load_extend_characs_0286_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_0286_packet()

    def _load_extend_characs_0080_packet(self) -> tuple[bytes, str]:
        return self.char_extend.build_0080_packet()

    def _handle_delete_character(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_delete.handle_delete_character(body)

    def _session_state(self, session: GameSessionState | None = None) -> GameSessionState:
        return session if session is not None else self._default_session_state

    @staticmethod
    def _select_character_by_slot(characters, requested_slot: int):
        # ??????????????? game/chains/flow-enter_game.py?
        return EnterGameChainService.select_character_by_slot(characters, requested_slot)

    def _handle_select_character(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        # ??????????SELECT_CHARACTER ???? game/chains/flow-enter_game.py?
        session_state = self._session_state(session)
        packets, note = self.enter_game_chain.handle_select_character(
            body,
            session=session_state,
        )
        notify_packets, notify_note = (
            self.guild_create_permit_notifications.build_for_selected_character(
                session_state
            )
        )
        packets.extend(notify_packets)
        return packets, note + "; " + notify_note

    def _handle_select_character_cpp(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        """Run the SELECT_CHARACTER native-plan branch with Python host I/O."""
        session_state = self._session_state(session)
        packets, note = (
            self.enter_game_chain.select_character_ack_chain.handle_select_character(
                body,
                session=session_state,
                native_branch=True,
            )
        )
        notify_packets, notify_note = (
            self.guild_create_permit_notifications.build_for_selected_character(
                session_state
            )
        )
        packets.extend(notify_packets)
        return packets, note + "; " + notify_note

    def _handle_charcard_click_02c1(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_charcard.handle_charcard_click_02c1(body)

    def _handle_change_charac_slot(self, body: bytes) -> tuple[list[bytes], str]:
        return self.char_change_slot.handle_change_charac_slot(body)

    def _handle_tutorial_flag_008f(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.tutorial_flag_008f.handle_tutorial_flag_008f(body, session=session)

    def _handle_dungeon_event_story_pause_00bf(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        """C1/0x00BF 剧情暂停 → S0/0x00AA（闭合对话状态机，修无头像）。"""
        return self.dungeon_event_story_pause_00bf.handle(body, session=session)

    def _handle_save_game_option_00c5(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.save_game_option_00c5.handle_save_game_option_00c5(body, session=session)

    def _handle_save_hotkey_option_00c6(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.save_hotkey_option_00c6.handle_save_hotkey_option_00c6(
            body, session=session
        )

    def _handle_save_character_option_01c0(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.save_character_option_01c0.handle_save_character_option_01c0(
            body, session=session
        )

    def _handle_update_wish_items_0547(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.update_wish_items_0547.handle_update_wish_items_0547(
            body, session=session
        )

    def _handle_skill_command_customizing_014b(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.skill_command_customizing_014b.handle_skill_command_customizing_014b(
            body, session=session
        )

    def _handle_skill_command_all_default_014c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.skill_command_all_default_014c.handle_skill_command_all_default_014c(
            body, session=session
        )

    def _handle_set_user_position_0023(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.set_user_position_0023.handle_set_user_position_0023(body, session=session)

    def _handle_contents_play_info_0578(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.contents_play_info_0578.handle_contents_play_info_0578(
            body,
            session=session,
        )

    def _handle_set_user_area_0024(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.set_user_area_0024.handle_set_user_area_0024(body, session=session)

    def _handle_move_itemspace_0013(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.move_itemspace_0013.handle_move_itemspace_0013(body, session=session)

    def _handle_change_deck_info_0292(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.change_deck_info_0292.handle_change_deck_info_0292(
            body, session=session
        )

    def _handle_sort_item_0014(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.sort_item_0014.handle_sort_item_0014(body, session=session)

    def _handle_title_book_put_019c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.title_book_put_019c.handle_title_book_put_019c(body, session=session)

    def _handle_title_book_get_019d(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.title_book_get_019d.handle_title_book_get_019d(body, session=session)

    def _handle_change_skillslot_001c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.change_skillslot_001c.handle_change_skillslot_001c(
            body, session=session
        )

    def _handle_dark_knight_combo_skill_01fd(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.dark_knight_combo_skill_01fd.handle_dark_knight_combo_skill_01fd(
            body, session=session
        )

    def _handle_buy_skill_001d(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.buy_skill_001d.handle_buy_skill_001d(body, session=session)

    def _handle_skill_init_01ec(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.skill_init_01ec.handle_skill_init_01ec(body, session=session)

    def _handle_skill_ui_refresh_0311(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.skill_ui_refresh_0311.handle_skill_ui_refresh_0311(
            body, session=session
        )

    def _handle_accept_quest_001f(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.accept_quest_001f.handle_accept_quest_001f(body, session=session)

    def _handle_giveup_quest_0020(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.giveup_quest_0020.handle_giveup_quest_0020(body, session=session)

    def _handle_set_quest_trigger_0021(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.set_quest_trigger_0021.handle_set_quest_trigger_0021(body, session=session)

    def _handle_finish_quest_0022(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.finish_quest_0022.handle_finish_quest_0022(body, session=session)

    def _handle_complete_quest_area_0315(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.complete_quest_area_0315.handle_complete_quest_area_0315(
            body, session=session
        )

    def _handle_die_monster_0027(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
        defer_altar_progression: bool = False,
    ) -> tuple[list[bytes], str]:
        return self.die_monster_0027.handle_die_monster_0027(
            body,
            session=session,
            defer_altar_progression=defer_altar_progression,
        )

    def _handle_die_character_0028(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.die_character_0028.handle_die_character_0028(
            body, session=session
        )

    def _handle_use_revive_coin_0029(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_revive_coin_0029.handle_use_revive_coin_0029(
            body, session=session
        )

    def _handle_get_item_002b(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.get_item_002b.handle_get_item_002b(body, session=session)

    def _handle_item_dictionary_01b7(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.item_dictionary_01b7.handle_item_dictionary_01b7(
            body, session=session
        )

    def _handle_epic_book_make_item_0313(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.epic_book_make_item_0313.handle_epic_book_make_item_0313(
            body, session=session
        )

    def _handle_use_stackable_002c(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_stackable_002c.handle_use_stackable_002c(body, session=session)

    def _handle_teleport_00ed(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.teleport_00ed.handle_teleport_00ed(body, session=session)

    def _handle_drop_item_002f(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.drop_item_002f.handle_drop_item_002f(body, session=session)

    def _handle_delete_item_0012(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.delete_item_0012.handle_delete_item_0012(body, session=session)

    def _handle_buy_item_0015(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.buy_item_0015.handle_buy_item_0015(body, session=session)

    def _handle_buy_cerashop_item_0040(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.buy_cerashop_item_0040.handle_buy_cerashop_item_0040(
            body, session=session
        )

    def _handle_rent_neo_premium_weapon_04b5(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.rent_neo_premium_weapon_04b5.handle_rent_neo_premium_weapon_04b5(
            body, session=session
        )

    def _handle_claim_neo_premium_gift_04b6(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.claim_neo_premium_gift_04b6.handle_claim_neo_premium_gift_04b6(
            body, session=session
        )

    def _handle_open_cera_package_0207(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.open_cera_package_0207.handle_open_cera_package_0207(
            body, session=session
        )

    def _handle_update_cube_contract_0218(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.update_cube_contract_0218.handle_update_cube_contract_0218(
            body, session=session
        )

    def _handle_use_booster_item_00a0(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_booster_item_00a0.handle_use_booster_item_00a0(
            body, session=session
        )

    def _handle_use_lottery_item_001b(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_lottery_item_001b.handle_use_lottery_item_001b(
            body, session=session
        )

    def _handle_use_random_box_item_00d0(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_random_box_item_00d0.handle_use_random_box_item_00d0(
            body, session=session
        )

    def _handle_sell_item_0016(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.sell_item_0016.handle_sell_item_0016(body, session=session)

    def _handle_disjoint_item_001a(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        if session is not None and _env_bool("USDOF_GAME_DISJOINT_BATCH", True):
            return self.disjoint_item_001a.queue_disjoint_item_001a(
                body,
                session=session,
            )
        return self.disjoint_item_001a.handle_disjoint_item_001a(body, session=session)

    def _flush_disjoint_item_001a_batch(
        self,
        client: socket.socket,
        session: GameSessionState,
        *,
        connection_id: str,
        reason: str,
    ) -> tuple[int, str]:
        packets, note = self.disjoint_item_001a.flush_disjoint_item_001a_batch(
            session=session
        )
        if not packets:
            return 0, note
        for packet in packets:
            self._log_game_sequence(
                direction="S->C",
                connection_id=connection_id,
                packet=packet,
                compact_prefix=(
                    _s2c_plain_wire_enabled()
                    and _plain_wire_header_mode() == "13"
                ),
                semantic=opcode_meaning(*_packet_key(packet)),
                note="disjoint batch flush reason=%s" % reason,
            )
            if _s2c_plain_wire_enabled():
                self._log_plain_wire_packet("send", packet)
            client.sendall(packet)
        if self.diagnostic_trace:
            self.log("[Game/DB] %s; flush_reason=%s" % (note, reason))
        return len(packets), note

    def _handle_unseal_random_option_0191(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.unseal_random_option_0191.handle_unseal_random_option_0191(
            body, session=session
        )

    def _handle_upgrade_item_0050(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.upgrade_item_0050.handle_upgrade_item_0050(
            body, session=session
        )

    def _handle_invest_amplify_option_00cd(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.invest_amplify_option_00cd.handle_invest_amplify_option_00cd(
            body, session=session
        )

    def _handle_use_avatar_emblem_00c9(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.use_avatar_emblem_00c9.handle_use_avatar_emblem_00c9(
            body, session=session
        )

    def _handle_enchant_by_bead_0110(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.enchant_by_bead_0110.handle_enchant_by_bead_0110(
            body, session=session
        )

    def _handle_avatar_option_change_01cc(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.avatar_option_change_01cc.handle_avatar_option_change_01cc(
            body, session=session
        )

    def _handle_monster_hit_0026(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.monster_hit_0026.handle(body, session=session)

    def _handle_move_map_002d(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.move_map_002d.handle(body, session=session)

    def _handle_set_play_result_002e(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.set_play_result_002e.handle(body, session=session)

    def _handle_dungeon_scene_uid_01de(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.dungeon_scene_uid_01de.handle(body, session=session)

    def _handle_enter_select_dungeon_000f(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.dungeon_enter_000f_0010.handle_000f(body, session=session)


    def _handle_finish_loading_0025(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.finish_loading_0025.handle(body, session=session)

    def _handle_select_dungeon_0010(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.dungeon_enter_000f_0010.handle_0010(body, session=session)

    def _handle_guide_enter_dungeon_0308(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        """C1/0x0308 红色引导进本 → 复用 SELECT_DUNGEON grant。"""
        return self.dungeon_enter_000f_0010.handle_0308(body, session=session)

    def _handle_enter_select_pre_0048(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.dungeon_enter_000f_0010.handle_0048(body, session=session)

    def _handle_opcode_0048(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        """0x0048 dual: settlement EPLP (return town) vs enter-select pre."""
        eplp = getattr(self, "eplp_command_0048", None)
        if eplp is not None and eplp.is_settlement_context(session):
            return eplp.handle(body, session=session)
        return self._handle_enter_select_pre_0048(body, session=session)

    def _handle_select_card_0047(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        return self.select_card_0047.handle(body, session=session)

    def _handle_back_2_village_0084(
        self, body: bytes, session=None
    ) -> tuple[list[bytes], str]:
        return self.dungeon_leave_select.handle_0084(body, session=session)

    def _handle_giveup_game_002a(
        self, body: bytes, session=None
    ) -> tuple[list[bytes], str]:
        return self.dungeon_leave_select.handle_002a(body, session=session)


    def _handle_channel_move_selecting_scene_057f(
        self,
        body: bytes,
        *,
        session: GameSessionState | None = None,
    ) -> tuple[list[bytes], str]:
        # 0x057F ????????????????????? pending??????
        # ???????? game/chains/flow-enter_game.py??????????????
        return self.enter_game_chain.handle_channel_move_selecting_scene_057f(
            body,
            session=self._session_state(session),
        )

    def _filtered_p1_followups(self, packets: list[bytes]) -> list[tuple[bytes, str]]:
        """Return P1 follow-up packets in the configured queue order.

        ``_db_userinfo_packet_stream(include_roster=False, include_followups=True)``
        emits the ServerS4A12-style post-USERINFO set in its natural order
        (currently DB extras 0x0286, 0x01BA, then optional 0x0003 USER_STATE).
        For gate experiments we need to release those packets one by one after
        the client PIV-echoes USERINFO, and we need to A/B the order without
        editing code.  ``USDOF_GAME_FOLLOWUP_QUEUE_AFTER_USERINFO_PIV`` accepts
        comma/space separated tokens:

        - ``0286`` / ``0x0286``
        - ``01ba`` / ``0x01ba``
        - ``0080`` / ``0x0080`` (experimental USDOF 90 flag0 handler 0x01642600)
        - ``userstate`` / ``user-state`` / ``0x0003``
        - ``all`` / ``default`` for the natural order

        Default is ``0286,01ba,userstate`` to preserve the DB/S4A12 follow-up
        semantics while still respecting the USDOF PIV gate.
        """

        raw_order = os.environ.get("USDOF_GAME_FOLLOWUP_QUEUE_AFTER_USERINFO_PIV", "0286,01ba,userstate")
        tokens = [
            _normalize_followup_token(token)
            for token in raw_order.replace(";", ",").replace(" ", ",").split(",")
            if token.strip()
        ]
        if not tokens or any(token in {"all", "default", "natural", "*"} for token in tokens):
            return [(packet, _packet_key_text(packet)) for packet in packets]

        buckets: dict[str, list[bytes]] = {}
        for packet in packets:
            buckets.setdefault(_followup_token_for_packet(packet), []).append(packet)

        queued: list[tuple[bytes, str]] = []
        for token in tokens:
            for packet in buckets.pop(token, []):
                queued.append((packet, f"{_packet_key_text(packet)} token={token}"))
        return queued

    @contextlib.contextmanager
    def _cleanup_death_tower_items_on_game_close(
        self,
        session: GameSessionState,
        *,
        connection_id: str,
    ):
        """Discard Tower of Death state on every Game TCP close path."""
        try:
            yield
        finally:
            close_game_session(self, session)
            raid_lobby.disconnect(self, session)
            temporary_slots = _tower_inventory.clear_death_tower_inventory(
                session
            )
            try:
                character_id = int(
                    getattr(session, "selected_character_id", 0) or 0
                )
            except (TypeError, ValueError):
                character_id = 0
            if character_id > 0:
                try:
                    cleanup = _tower_item_cleanup.cleanup_death_tower_items(
                        self._repo(), character_id
                    )
                    self.log(
                        "[Game] connection close %s; "
                        "death_tower_session_slots_cleared=%d; %s"
                        % (connection_id, temporary_slots, cleanup.note())
                    )
                except Exception as exc:
                    self.log(
                        "[Game] connection close %s; death_tower_temp_items_cleanup=error %r"
                        % (connection_id, exc)
                    )

    def handle_client(self, client: socket.socket, addr, local_port: int | None = None) -> None:
        from login.native_tunnel import identify, enter_tunnel, leave_tunnel
        try:identity,tunneled=identify(client,addr[0],get_login_registry())
        except (ValueError,OSError):
            client.close()
            return
        require_external = _env_bool("USDOF_LOGIN_REQUIRE_EXTERNAL_SESSION", True)
        if identity is None and require_external and not is_loopback_address(addr[0]):
            self.log(
                "[Game/Auth] rejected external client without HTTP login "
                f"remote={addr[0]}:{addr[1]}"
            )
            try:
                client.close()
            except OSError:
                pass
            return
        release_native=None
        tunnel_context=None
        tunnel_entered=False
        try:
            if identity is not None:
                from login.session_registry import SessionConflictError
                try:release_native=get_login_registry().acquire_native_game(identity,client=client)
                except SessionConflictError:return
            tunnel_context=enter_tunnel(tunneled)
            tunnel_entered=True
            with self._account_identity_scope(identity):
                if local_port is None:
                    local_port = client.getsockname()[1]
                local_port = self.legacy_game_ports.get(local_port, local_port)
                self._handle_client_bound(client, addr, local_port, identity=identity)
        finally:
            try:
                if tunnel_entered:leave_tunnel(tunnel_context)
            finally:
                try:client.close()
                finally:
                    if release_native is not None:release_native()

    def _handle_client_bound(
        self,
        client: socket.socket,
        addr,
        local_port: int | None = None,
        *,
        identity=None,
    ) -> None:
        prefix = f"game_{_timestamp()}_{addr[0].replace('.', '_')}_{addr[1]}"
        session_state = GameSessionState(
            account_id=self.account_id,
            account_m_id=self.account_m_id,
            login_session_id=(
                str(identity.login_session_id) if identity is not None else None
            ),
        )
        # Keep the ownership lifetime outside the large protocol loop. Every
        # return/exception, including an early connection rejection, clears it.
        try:
            with self._delayed_response_connection(session_state, prefix):
                self._handle_client_connection(
                    client, addr, local_port, identity=identity,
                    session_state=session_state, prefix=prefix,
                )
        finally:
            # Match this socket's registration on every exit, including policy
            # rejection and exceptions before the protocol loop is entered.
            self.force_exp_notify.unregister_client(client)

    def _handle_client_connection(
        self, client: socket.socket, addr, local_port: int | None = None,
        *, identity=None, session_state, prefix: str,
    ) -> None:
        capture_path = CAPTURE_DIR / f"{prefix}_stream.bin"
        self.log(f"[Game] accepted client from {addr[0]}:{addr[1]}")
        if os.environ.get("USDOF_PVF_BOOT_MODE", "").strip().lower() == "lazy":
            from pvf import boot_state

            timeout = float(
                os.environ.get("USDOF_PVF_LAZY_GAME_WAIT_SECONDS", "3600") or "3600"
            )
            if not boot_state.wait_ready(timeout):
                detail = boot_state.error() or "background warmup timed out"
                self.log(f"[Game] lazy PVF gate failed: {detail}")
                try:
                    client.close()
                except OSError:
                    pass
                return
            self.log("[Game] lazy PVF runtime ready; accepting Game protocol")
        auth_mode = "http-login" if identity is not None else "builtin"
        self.log(
            f"[Game/Auth] client session mode={auth_mode} "
            f"db_account_id={self.account_id} m_id={self.account_m_id!r} "
            f"launcher_account_id={self.launcher_account_id}"
        )
        if local_port is None:
            try:
                local_port = int(client.getsockname()[1])
            except Exception:
                local_port = int(self.port)
        bound_channel_id = self._resolve_channel_id_for_port(int(local_port))
        if bound_channel_id is not None:
            session_state.selected_channel_id = int(bound_channel_id)
            self.log(
                f"[Game] bound session channel_id={bound_channel_id} from local_port={local_port}"
            )
        else:
            self.log(
                f"[Game] no unique channel mapping for local_port={local_port}; "
                "0x020C will fall back to env/default"
            )
        try:
            control_context = self.register_control_game_client(
                client,
                session_state,
                connection_id=prefix,
            )
        except ControlApiError as exc:
            if exc.code != "ACCESS_DENIED" or exc.status != 403:
                raise
            self.log(
                "[Game/Auth] rejected connection conn=%s code=%s status=%d"
                % (prefix, exc.code, exc.status)
            )
            return
        # Only admitted connections can receive file-triggered EXP notices.
        try:
            self.force_exp_notify.register_client(client, session_state)
        except Exception as exc:
            self.log(f"[ForceExp] register failed: {exc!r}")
        buffer = bytearray()
        first_packet_logged = False
        ack_response_sent = False
        piv_response_sent = False
        db_userinfo_sent = False
        db_userinfo_type0_sent = False
        load_extend_piv_refresh_sent = False
        load_extend_01ba_sent = False
        load_extend_0286_sent = False
        load_extend_01f9_sent = False
        load_extend_01fa_sent = False
        load_extend_01fb_sent = False
        load_extend_01fc_sent = False
        load_extend_0080_sent = False
        load_extend_0080_refresh_sent = False
        load_extend_01ba_piv_refresh_sent = False
        login_state_init_0001_sent = False
        charselect_0004_sent = False
        charselect_0004_piv_seen = False
        charselect_0004_piv_deferred_packets: list[tuple[bytes, str]] = []
        cold_charselect_dup_0008_guard_armed = False
        cold_charselect_recovery = _ColdCharselectRecovery(session_state)
        create_character_userinfo_refresh_pending = False
        post_charselect_01f8_userinfo_sent = False
        post_charselect_008c_userinfo_sent = False
        # After short-leave reentry USERINFO, client may skip PIV of 0x01BA;
        # open LOGIN/CHARSELECT on C->S 0x01BA instead of waiting for that PIV.
        reentry_charselect_pending = False
        # The USDOF 2017 client has a PIV/ACK gate between selected
        # server-to-client notipackets.  Live traces proved that sending
        # USERINFO immediately after the first PIV echo (for opcode 0x0001)
        # makes the client swallow it inside the PIV state machine, while
        # sending USERINFO after C->S 0x00D9 reaches the real USERINFO handler.
        #
        # The stable current-client entry path sends the roster on the first
        # PIV.  A later ACK-gated experiment waited for C->S 0x00D9, but live
        # testing on 2026-07-06 showed the client can stall after the opcode1
        # PIV echo before that ACK arrives.
        userinfo_trigger = (
            os.environ.get("USDOF_GAME_USERINFO_TRIGGER", "piv")
            .strip()
            .lower()
            .replace("_", "-")
        )
        p1_piv_followup_queue: list[tuple[bytes, str]] = []
        p1_waiting_for_piv_after: tuple[int, int] | None = None
        # Probe Z (2026-07-13): hold type0/0004 until C->S 0x0162 proves 0131 handler finished.
        enter_seria_after_0162_queue: list[tuple[bytes, str]] = []
        # ``piv`` is useful for reverse experiments, but current USDOF traces
        # show the client builds the USERINFO PIV echo internally without
        # flushing it to winsock.  ``delay`` therefore is the practical default:
        # emit USERINFO, let its handler/UI gate return, then send follow-ups
        # as separate TCP writes after a short delay.
        p1_queue_step_mode = os.environ.get("USDOF_GAME_FOLLOWUP_QUEUE_STEP_MODE", "delay").strip().lower().replace("_", "-")
        with self._cleanup_death_tower_items_on_game_close(
                session_state, connection_id=prefix
        ), native_game_connection(self, session_state, client, identity=identity), \
                native_udp_connection(self, session_state, addr[0]), client, self._repo().game_connection_scope(), \
                open_stream_capture(capture_path) as capture, \
                self.hotpath_metrics.connection(prefix):
            # Idle hang policy:
            # Local single-player AFK must not drop the Game TCP session.
            # Control commands are drained on this same owning thread, so use
            # a short receive poll and enforce an optional idle deadline here
            # instead of relying on a long blocking recv timeout.
            idle_timeout = float(os.environ.get("USDOF_GAME_CLIENT_TIMEOUT", "0") or "0")
            control_poll_seconds = 0.05
            send_timeout = max(1, _env_int("USDOF_GAME_SEND_TIMEOUT", 5))
            client.settimeout(send_timeout)
            last_client_receive_at = time.monotonic()
            if idle_timeout > 0:
                self.log(f"[Game] client idle timeout={idle_timeout:.0f}s (USDOF_GAME_CLIENT_TIMEOUT)")
            else:
                self.log("[Game] client idle timeout disabled; control queue polling enabled")
            if self.initial_probes:
                # Reverse note:
                # 0x02BF23D0 has a special receive path for packets with
                # flag_or_magic==0 and opcode==1.  It calls 0x02754790 on the
                # payload and returns success before the regular 14-codec
                # decode path.  A header-only packet is the least-invasive
                # server-first probe for discovering whether the Game phase
                # expects an initial unencrypted hello/key-sync packet.
                for flag, opcode, payload in self.initial_probes:
                    probe = _build_server_game_packet(flag, opcode, payload)
                    self._log_game_sequence(
                        direction="S->C",
                        connection_id=prefix,
                        packet=probe,
                        compact_prefix=(
                            _s2c_plain_wire_enabled()
                            and _plain_wire_header_mode() == "13"
                        ),
                        plain_body=payload,
                        semantic="server_initial_probe",
                        note="sent automatically on Game accept",
                    )
                    client.sendall(probe)
                    if _s2c_plain_wire_enabled():
                        self._log_plain_wire_packet("send", probe)
                    if self.protocol_traffic_log:
                        self.log(
                            f"[Game] sent initial opcode=0x{opcode:04x} flag={flag} "
                            f"payload={len(payload)} probe bytes={len(probe)}"
                        )
                    if self.diagnostic_trace:
                        append_trace(
                            raw_hex=probe.hex(" "),
                            header=parse_game_header(probe[:0x10]),
                            decode=None,
                            probable_handler=f"server-first unencrypted opcode 0x{opcode:04x} probe",
                            decision="sent automatically on accept; configurable by USDOF_GAME_PROBES/server args",
                            client_next="pending: expect FD_READ -> 0x02BF29A0/0x02BF23D0 and possible client response",
                            capture_path=capture_path,
                        )
            while True:
                recovered = cold_charselect_recovery.take_due(
                    session_state, charselect_0004_piv_deferred_packets, time.monotonic()
                )
                if recovered:
                    try:
                        remaining, wait_key = self._send_cold_charselect_recovery(
                            client, session_state, recovered,
                            connection_id=prefix, step_mode=p1_queue_step_mode,
                        )
                    except OSError as exc:
                        self.log(f"[Game/ColdCharselect] recovery send failed: {exc!r}; closing client")
                        break
                    p1_piv_followup_queue = remaining + p1_piv_followup_queue
                    p1_waiting_for_piv_after = wait_key
                try:
                    from game.native_chat import pump as pump_native_chat
                    pump_native_chat(self, session_state, client)
                    from game.cera_notifications import pump_balance
                    pump_balance(self, session_state, client)
                    from game.town_social import pump as pump_town_social
                    from game.normal_party import pump as pump_normal_party
                    pump_town_social(self, session_state, client)
                    pump_normal_party(self, session_state, client)
                    from game.character.daily_item_expiry import pump as pump_daily_items
                    pump_daily_items(self, session_state, client)
                    from game.quest.daily_rollover import pump as pump_daily_rollover
                    pump_daily_rollover(self, session_state, client)
                    pump_local_raid(self, session_state, client)
                except OSError as exc:
                    self.log('[Raid] progression send failed: %r; closing client' % (exc,))
                    break
                disjoint_deadline = (
                    self.disjoint_item_001a.pending_batch_deadline(session_state)
                )
                if (
                    disjoint_deadline is not None
                    and disjoint_deadline <= time.monotonic()
                ):
                    try:
                        self._flush_disjoint_item_001a_batch(
                            client,
                            session_state,
                            connection_id=prefix,
                            reason="quiet_window",
                        )
                    except OSError as exc:
                        self.log(
                            "[Game] disjoint batch send failed: %s; closing client"
                            % exc
                        )
                        break
                self._drain_control_commands(control_context)
                try:
                    self._drain_tournament_reward_manual_refresh(control_context)
                except OSError as exc:
                    self.log(
                        "[Tournament] manual refresh send failed: %r; closing client"
                        % (exc,)
                    )
                    break
                try:
                    self._drain_altar_due_packets(control_context)
                except OSError as exc:
                    self.log(
                        "[Altar] due packet send failed: %r; closing client"
                        % (exc,)
                    )
                    break
                poll_timeout = control_poll_seconds
                disjoint_deadline = (
                    self.disjoint_item_001a.pending_batch_deadline(session_state)
                )
                if disjoint_deadline is not None:
                    poll_timeout = min(
                        poll_timeout,
                        max(0.001, disjoint_deadline - time.monotonic()),
                    )
                if idle_timeout > 0:
                    poll_timeout = min(poll_timeout, idle_timeout)
                try:
                    chunk = recv_game_chunk(client, poll_timeout)
                except socket.timeout:
                    if idle_timeout > 0 and (time.monotonic() - last_client_receive_at) >= idle_timeout:
                        self.log(
                            "[Game] recv timeout; closing client "
                            "(idle USDOF_GAME_CLIENT_TIMEOUT; client may show channel full)"
                        )
                        break
                    continue
                except OSError as exc:
                    self.log(f"[Game] recv failed: {exc}; closing client")
                    break
                if not chunk:
                    self.log("[Game] client closed")
                    break
                last_client_receive_at = time.monotonic()
                capture.write(chunk)
                capture.flush()
                buffer.extend(chunk)
                if self.diagnostic_trace and self.protocol_traffic_log:
                    self.log(
                        f"[Game] recv chunk bytes={len(chunk)} "
                        f"hex={chunk[:64].hex(' ')}"
                    )
                while len(buffer) >= COMPACT_GAME_HEADER_SIZE:
                    try:
                        header = parse_game_header(buffer[:0x10])
                    except GameWireError as exc:
                        if self.diagnostic_trace:
                            append_trace(
                                raw_hex=bytes(buffer[: min(len(buffer), 128)]).hex(" "),
                                header=None,
                                decode=None,
                                decision=f"header parse failed: {exc}",
                                capture_path=capture_path,
                            )
                        self.log(f"[Game] header parse failed: {exc}; waiting for more/dumping raw")
                        break
                    if len(buffer) < header.length:
                        if not first_packet_logged:
                            self.log(f"[Game] partial packet header: {describe_game_header(header)} buffered={len(buffer)}")
                            first_packet_logged = True
                        break
                    packet = bytes(buffer[: header.length])
                    del buffer[: header.length]
                    if (
                        not (
                            int(header.flag_or_magic) == 1
                            and int(header.opcode) == 0x001A
                        )
                        and self.disjoint_item_001a.has_pending_batch(session_state)
                    ):
                        try:
                            self._flush_disjoint_item_001a_batch(
                                client,
                                session_state,
                                connection_id=prefix,
                                reason="before_flag%d_opcode_%04x"
                                % (
                                    int(header.flag_or_magic),
                                    int(header.opcode),
                                ),
                            )
                        except OSError as exc:
                            self.log(
                                "[Game] disjoint batch send failed: %s; closing client"
                                % exc
                            )
                            return
                    hotpath_request = self.hotpath_metrics.begin_request(
                        int(header.opcode)
                    )
                    if hotpath_request is not None:
                        hotpath_request.mark_decode_start()
                    # Track C->S opcodes so 0x057F can tell Exit Game prep
                    # (0x00C2/0x0505/0x0506) from reentry Select Channel (list_count=2).
                    try:
                        session_state.note_c2s_opcode(int(header.opcode))
                    except Exception:
                        pass
                    payload_after_0x10 = packet[0x10:]
                    if _c2s_plain_wire_enabled():
                        plain_body = game_packet_body(packet, header, compact_prefix=True)
                        decode = DecodeResult(
                            True,
                            header.codec_index,
                            plain_body,
                            "C2S plain-wire direct body from the client encode hook",
                        )
                        self._log_plain_wire_packet("recv", packet)
                    else:
                        # Important Stage3 wire quirk:
                        #
                        # 0x02BF23D0 still treats ``packet + 0x10`` as the
                        # regular decode input pointer, but the bytes at
                        # +0x0D..+0x0F are not semantically inert for encoded
                        # client packets.  Live opcode 0x013E (codec_index 10)
                        # proves those three bytes are the prefix of the codec
                        # body: reserved=9c4f3b plus payload=443b62f41d gives
                        # an 8-byte body that XOR-decodes to
                        # ``00 01 00 59 a7 2c cf 00``.  If we pass only
                        # packet[0x10:], idx10 is not 4-byte aligned and the
                        # useful plaintext is lost.
                        if header.payload_len > 0:
                            codec_body = header.reserved + payload_after_0x10
                        else:
                            codec_body = b""
                        # 当前默认链路由 usdof_codec_passthrough 全局接管，已在上方
                        # plain-wire 分支统一收明文。这里仅兼容迁移前的单 opcode
                        # ForcePlain 二进制；新功能不得再增加 opcode 白名单。
                        decode = None
                        op = int(header.opcode)
                        if op == 0x001A and codec_body:
                            try:
                                _dj = load_chain("opcode001a-disjoint_item.py")
                                if _dj.parse_disjoint_item_body(codec_body) is not None:
                                    decode = DecodeResult(
                                        True,
                                        12,
                                        codec_body,
                                        "001A legacy raw-plain compatibility body",
                                    )
                            except Exception:
                                decode = None
                        elif op == 0x0191 and codec_body:
                            try:
                                _us = load_chain("opcode0191-unseal_random_option.py")
                                if _us.parse_unseal_body(codec_body) is not None:
                                    decode = DecodeResult(
                                        True,
                                        9,
                                        codec_body,
                                        "0191 legacy raw-plain compatibility body",
                                    )
                            except Exception:
                                decode = None
                        elif op == 0x00BF and codec_body:
                            try:
                                _sp = load_chain(
                                    "opcode00bf-dungeon_event_story_pause.py"
                                )
                                _raw_bf = _sp.try_raw_plain_00bf(codec_body)
                                if _raw_bf is not None:
                                    decode = DecodeResult(
                                        True,
                                        9,
                                        _raw_bf,
                                        "00BF legacy raw-plain compatibility body",
                                    )
                            except Exception:
                                decode = None
                        elif op == 0x014B and codec_body:
                            try:
                                _sc = load_chain(
                                    "opcode014b-skill_command_customizing.py"
                                )
                                _raw_sc = _sc.try_raw_plain_014b(codec_body)
                                if _raw_sc is not None:
                                    decode = DecodeResult(
                                        True,
                                        9,
                                        _raw_sc,
                                        "014B legacy raw-plain compatibility body",
                                    )
                            except Exception:
                                decode = None
                        if decode is None:
                            decode = decode_payload_skeleton(header.opcode, codec_body)
                    if hotpath_request is not None:
                        hotpath_request.mark_decode_end()
                    recv_packet_for_sequence = packet
                    # The existing USERINFO actor producer uses native context0;
                    # selected channel23/32 is a separate server authorization.
                    raid_namespace = (0
                                      if header.opcode == 0 and (_env_bool('USDOF_RAID_LOCAL', False) or raid_lobby.enabled())
                                      and session_state.selected_channel_id in (23, 32) else None)
                    # Restore the prior player-tested raid entry notice.
                    # The positive clock/NAT initialization crashed the real
                    # client after entry; do not arm its timer receipt here.
                    decision = handle_decoded_packet(header.opcode, decode.plain, raid_namespace=raid_namespace)
                    if raid_namespace is not None:
                        session_state._raid_namespace_pending = (decision.response_packets[0], raid_namespace)
                    response_packets = list(decision.response_packets)
                    delayed_response_packets: list[tuple[bytes, str]] = []
                    decision_note = decision.note
                    if (
                        header.flag_or_magic == 1
                        and header.opcode in ENTER_GAME_FOLLOWUP_OPCODES
                        and session_state.enter_game_pending
                    ):
                        decision_note = self.enter_game_chain.classify_followup(
                            header.opcode,
                            len(decode.plain),
                            session_state,
                        )
                    close_after_packet = (
                        header.flag_or_magic == 1
                        and header.opcode == 0x0003
                    )
                    if close_after_packet and header.opcode == 0x0003:
                        # C->S flag1/0x0003 after charselect
                        # Exit Game and Channel-leave both use this opcode.
                        # Exit Game: SuccessAck flag1/0x0003 body=01 then close.
                        # Channel-leave: SuccessAck flag1/0x0003 then close. Opcode 7 is NOT channel-list return.
                        # Channel-leave may optionally send flag1/0x0007 (default OFF).
                        # IDA: opcode 7 (0x16080B0) is RETURN_SELECT_CHARACTER session teardown.
                        # Early settle 0007 causes teardown before Channel reentry and is wrong.
                        # Residual Select Channel: leave msg44444 when +1093; early op3 SuccessAck
                        # on 0x057F clears it (UI828). Keep 0x0242 OFF.
                        last_profile = getattr(session_state, "last_057f_profile", None)
                        # channel / none settle both mark Channel-button leave (not Exit Game).
                        channel_leave = (
                            last_profile in {"channel", "none"}
                            and bool(getattr(session_state, "channel_move_settled", False))
                        )
                        decision_note += (
                            "; CMDPACKET_EXIT/0x0003 observed"
                            f" channel_id={session_state.selected_channel_id}"
                            f" last_057f_profile={last_profile!r}"
                            f" channel_leave={int(channel_leave)}"
                        )
                        if channel_leave and _env_bool("USDOF_GAME_CHANNEL_LEAVE_SEND_0007", False):
                            # Residual-full A/B only.
                            # Opcode 7 is RETURN_SELECT_CHARACTER in enum, NOT "return to channel list".
                            # But IDA registers 7 on the CMD table (0x1AB3BC0) with EXIT(3)/SELECT(4).
                            # Success handler 0x16080B0 clears dword_4117878=-1 and teardown 1ABE3E0
                            # (UI42+UI175) without msg22362. ChannelInfo 0x15AF0F0 shows msg7000
                            # when gate != -1 on 7C03 reentry. Use flag1 SuccessAck body=01 (CMD),
                            # not flag0 (previous flag0 inject did not clear residual full).
                            leave_body = bytes([1])
                            leave_mode = os.environ.get(
                                "USDOF_GAME_CHANNEL_LEAVE_0007_MODE", "codec"
                            ).strip().lower()
                            leave_flag = max(0, _env_int("USDOF_GAME_CHANNEL_LEAVE_0007_FLAG", 1))
                            if leave_mode in {"codec", "encoded"}:
                                response_packets.append(
                                    self._codec_response(leave_flag, 0x0007, leave_body)
                                )
                                decision_note += (
                                    f"; CHANNEL-LEAVE gate-clear flag{leave_flag}/0x0007 "
                                    "codec body=01"
                                )
                            else:
                                response_packets.append(
                                    self._raw_response(leave_flag, 0x0007, leave_body)
                                )
                                decision_note += (
                                    f"; CHANNEL-LEAVE gate-clear flag{leave_flag}/0x0007 "
                                    "raw body=01"
                                )
                        send_exit_ack = _env_bool("USDOF_GAME_EXIT_SEND_SUCCESS_ACK", True)
                        if channel_leave and not _env_bool(
                            "USDOF_GAME_CHANNEL_LEAVE_SEND_SUCCESS_ACK", True
                        ):
                            send_exit_ack = False
                        # Early 0x0003 on 0x057F often runs before leave sets n5==5, so
                        # 1ABA3A0 may miss clear +1093. Late C->S 0x0003 SuccessAck is
                        # required for reentry. Only skip when A/B enables SKIP_DUP.
                        if (
                            channel_leave
                            and bool(getattr(session_state, "channel_leave_early_ack_sent", False))
                            and _env_bool("USDOF_GAME_CHANNEL_LEAVE_SKIP_DUP_EARLY_ACK", False)
                        ):
                            send_exit_ack = False
                            decision_note += "; CHANNEL-LEAVE skip duplicate SuccessAck (early 0003 already sent)"
                            session_state.channel_leave_early_ack_sent = False
                        elif channel_leave and bool(
                            getattr(session_state, "channel_leave_early_ack_sent", False)
                        ):
                            decision_note += "; CHANNEL-LEAVE keep late SuccessAck for n5 clear path"
                            session_state.channel_leave_early_ack_sent = False
                        if send_exit_ack:
                            ack_body = bytes([1])
                            ack_mode = os.environ.get("USDOF_GAME_EXIT_ACK_MODE", "codec").strip().lower()
                            if ack_mode in {"raw", "plain"}:
                                response_packets.append(self._raw_response(1, 0x0003, ack_body))
                                decision_note += "; EXIT success-ack flag1/0x0003 raw body=01"
                            else:
                                response_packets.append(self._codec_response(1, 0x0003, ack_body))
                                decision_note += "; EXIT success-ack flag1/0x0003 codec body=01"
                        if channel_leave:
                            decision_note += "; CHANNEL-LEAVE gate-clear 0007 (if enabled) then SuccessAck then close"
                            session_state.last_057f_profile = None
                            # Arm second-Game USERINFO fallback: short leave skips 00FA and
                            # often never sends C->S PIV after opcode1. Exit Game does not arm.
                            if _env_bool("USDOF_GAME_REENTRY_USERINFO_FALLBACK", True):
                                with self._channel_leave_reentry_lock:
                                    self._channel_leave_reentry_armed = True
                                    self._channel_leave_reentry_armed_at = time.time()
                                decision_note += "; arm second-Game USERINFO fallback after leave"
                                self.log(
                                    "[Game/Reentry] armed second-Game USERINFO fallback "
                                    "(no PIV after opcode1)"
                                )

                        if (not channel_leave) and _env_bool("USDOF_GAME_EXIT_INJECT_CHANNEL_MOVE", False):
                            try:
                                move_packets, move_note, _cid = (
                                    self.enter_game_chain.build_channel_move_success_packets(
                                        session=session_state
                                    )
                                )
                                if move_packets:
                                    response_packets.extend(move_packets)
                                    decision_note += f"; EXIT inject-channel-move {move_note}"
                            except Exception as exc:
                                decision_note += f"; EXIT channel-move inject failed: {exc!r}"
                    piv_plain = parse_piv_plain(decode.plain) if header.opcode == 0x013E else None
                    if (
                        header.opcode == 0x013E
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (1, 0x0004)
                    ):
                        charselect_0004_piv_seen = True
                        if charselect_0004_piv_deferred_packets:
                            # A live 2026-07-09 trace showed that bursting the
                            # deferred GET_USERINFO stream here as
                            # 0x0002 -> 0x0286 -> 0x01BA makes the client echo
                            # only 0x0286 and can leave the role list black.
                            # Release the roster now, then reuse the existing
                            # PIV queue for the remaining follow-ups.
                            first_packet, first_note = charselect_0004_piv_deferred_packets[0]
                            remaining_packets = list(charselect_0004_piv_deferred_packets[1:])
                            response_packets.append(first_packet)
                            try:
                                p1_waiting_for_piv_after = _packet_key(first_packet)
                            except Exception:
                                p1_waiting_for_piv_after = None
                            if remaining_packets:
                                p1_piv_followup_queue = remaining_packets + p1_piv_followup_queue
                            self.log(
                                "[Game/P1Gate] released first deferred USERINFO response after "
                                f"CHARSELECT 0x0004 PIV: {first_note}"
                            )
                            if remaining_packets:
                                self.log(
                                    "[Game/P1Gate] queued remaining deferred USERINFO followups "
                                    "for PIV-gated release: "
                                    + ", ".join(note for _packet, note in remaining_packets)
                                )
                            decision_note += (
                                "; released first deferred USERINFO response after "
                                "CHARSELECT 0x0004 PIV; "
                                f"queued_remaining={len(remaining_packets)}"
                            )
                            charselect_0004_piv_deferred_packets = []
                    if header.opcode == 0x013E and self.dynamic_userinfo and p1_waiting_for_piv_after is not None:
                        if piv_plain is None:
                            decision_note += "; P1 gated followups waiting but PIV body was not parseable"
                        else:
                            echoed_key = (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF)
                            waiting_key = p1_waiting_for_piv_after
                            strict_match = echoed_key == waiting_key
                            loose_match = p1_queue_step_mode in {"piv-any", "any-piv", "loose"}
                            # Stage3 enter-town (Probe K 2026-07-13): client PIV-echoes
                            # S->C 0x0131 even when we are waiting for a later delayed packet.
                            # If enter-seria delayed queue is pending and client echoes 0131,
                            # treat that as the release signal and burst the queue.
                            enter_seria_pending = any(
                                ("enter-seria-piv-burst-rest" in note) or ("enter-seria-after-0131" in note)
                                for _packet, note in p1_piv_followup_queue
                            )
                            enter_seria_0131_match = (
                                enter_seria_pending
                                and echoed_key[1] == 0x0131
                            )
                            if strict_match or loose_match or enter_seria_0131_match:
                                if p1_piv_followup_queue:
                                    release_all = p1_queue_step_mode in {
                                        "piv-all",
                                        "all",
                                        "burst",
                                        "burst-after-userinfo",
                                        "all-after-userinfo",
                                    }
                                    if not release_all and any(
                                        ("enter-seria-piv-burst-rest" in note)
                                        or ("enter-seria-after-0131" in note)
                                        for _packet, note in p1_piv_followup_queue
                                    ):
                                        release_all = True
                                    released: list[tuple[bytes, str]] = []
                                    if enter_seria_0131_match:
                                        # Only release enter-seria delayed packets.
                                        # Do NOT drain leftover charselect followups (0x0286/0x01BA).
                                        kept: list[tuple[bytes, str]] = []
                                        while p1_piv_followup_queue:
                                            item = p1_piv_followup_queue.pop(0)
                                            _packet, note = item
                                            if ("enter-seria-piv-burst-rest" in note) or ("enter-seria-after-0131" in note):
                                                released.append(item)
                                            else:
                                                kept.append(item)
                                        # Drop leftover charselect chrome packets rather than
                                        # re-injecting them mid-town (Expertise/0x0286 etc).
                                        if kept:
                                            self.log(
                                                "[Game/P1Gate] dropped non-enter-seria followups after 0131 PIV: "
                                                + ", ".join(note for _packet, note in kept)
                                            )
                                        p1_piv_followup_queue = []
                                        release_all = True
                                    elif release_all:
                                        while p1_piv_followup_queue:
                                            released.append(p1_piv_followup_queue.pop(0))
                                    else:
                                        released.append(p1_piv_followup_queue.pop(0))
                                    for queued_packet, queued_note in released:
                                        response_packets.append(queued_packet)
                                        try:
                                            p1_waiting_for_piv_after = _packet_key(queued_packet)
                                        except Exception:
                                            p1_waiting_for_piv_after = None
                                        self.log(
                                            "[Game/P1Gate] released queued followup after PIV "
                                            f"echo=flag={echoed_key[0]}/opcode=0x{echoed_key[1]:04x}: {queued_note}"
                                        )
                                    if release_all:
                                        p1_waiting_for_piv_after = None
                                    if p1_piv_followup_queue:
                                        self.log(
                                            "[Game/P1Gate] remaining queued followups: "
                                            + ", ".join(note for _packet, note in p1_piv_followup_queue)
                                        )
                                    decision_note += (
                                        "; P1 gated followup released after PIV "
                                        f"echo=flag={echoed_key[0]}/opcode=0x{echoed_key[1]:04x} "
                                        f"count={len(released)} step_mode={p1_queue_step_mode}"
                                    )
                                else:
                                    p1_waiting_for_piv_after = None
                                    decision_note += (
                                        "; P1 gated followup queue drained after final PIV "
                                        f"echo=flag={echoed_key[0]}/opcode=0x{echoed_key[1]:04x}"
                                    )
                            else:
                                decision_note += (
                                    "; P1 gated followup still waiting for "
                                    f"flag={waiting_key[0]}/opcode=0x{waiting_key[1]:04x}, "
                                    f"got flag={echoed_key[0]}/opcode=0x{echoed_key[1]:04x}"
                                )
                    if header.opcode == 0x013E and self.piv_response_probes and not piv_response_sent:
                        piv_response_sent = True
                        for flag, opcode, probe_payload in self.piv_response_probes:
                            response_packets.append(_build_server_game_packet(flag, opcode, probe_payload))
                        self.log(
                            "[Game] queued PIV-followup probes "
                            + ", ".join(f"flag={flag}/opcode=0x{opcode:04x}/payload={len(probe_payload)}" for flag, opcode, probe_payload in self.piv_response_probes)
                        )
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not db_userinfo_type0_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x0002)
                        and _env_bool("USDOF_GAME_USERINFO_TYPE0_EMPTY_AFTER_USERINFO_PIV", False)
                    ):
                        packet, note = self._encoded_userinfo_type0_empty_packet("piv-echo-0x0002")
                        response_packets.append(packet)
                        db_userinfo_type0_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued supplemental USERINFO {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_01ba_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (1, 0x02A8)
                        and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01BA_AFTER_PIV", True)
                    ):
                        packet, note = self._load_extend_characs_pkt2_packet()
                        response_packets.append(packet)
                        load_extend_01ba_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_piv_refresh_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (1, 0x02A8)
                        and _env_bool("USDOF_GAME_USERINFO_AFTER_LOAD_EXTEND_PIV", False)
                    ):
                        try:
                            packets, note = self._db_userinfo_packet_stream(
                                "piv-0x02a8-load-extend-refresh",
                                include_roster=True,
                                include_followups=False,
                            )
                            response_packets.extend(packets)
                            load_extend_piv_refresh_sent = True
                            db_userinfo_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued post-load-extend PIV USERINFO {note}")
                        except Exception as exc:
                            decision_note += f"; post-load-extend USERINFO failed: {exc}"
                            self.log(f"[Game/DB] post-load-extend USERINFO failed: {exc}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_0286_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) in {(0, 0x01BA), (1, 0x01BA)}
                        and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_0286_AFTER_01BA_PIV", False)
                    ):
                        packet, note = self._load_extend_characs_0286_packet()
                        response_packets.append(packet)
                        load_extend_0286_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_01f9_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x01BA)
                        and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01F9_AFTER_01BA_PIV", False)
                    ):
                        packet, note = self._load_extend_characs_01f9_packet()
                        response_packets.append(packet)
                        load_extend_01f9_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_01fa_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x01BA)
                        and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FA_AFTER_01BA_PIV", False)
                    ):
                        packet, note = self._load_extend_characs_01fa_packet()
                        response_packets.append(packet)
                        load_extend_01fa_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                        if (
                            not load_extend_01fb_sent
                            and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FB_AFTER_01FA", False)
                        ):
                            packet, note = self._load_extend_characs_01fb_packet()
                            response_packets.append(packet)
                            load_extend_01fb_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued {note}")
                        if (
                            not load_extend_01fc_sent
                            and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FC_AFTER_01FA", False)
                        ):
                            packet, note = self._load_extend_characs_01fc_packet()
                            response_packets.append(packet)
                            load_extend_01fc_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not charselect_0004_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x01BA)
                        and _env_bool("USDOF_GAME_SEND_CHARSELECT_0004_AFTER_01BA_PIV", False)
                    ):
                        if (
                            not login_state_init_0001_sent
                            and _env_bool("USDOF_GAME_SEND_STATE_INIT_0001_BEFORE_CHARSELECT_0004", True)
                        ):
                            packet, note = self._login_state_init_0001_packet(session=session_state)
                            response_packets.append(packet)
                            login_state_init_0001_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued {note}")
                        packet, note = self._charselect_open_0004_packet()
                        response_packets.append(packet)
                        charselect_0004_sent = True
                        reentry_charselect_pending = False
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_01ba_piv_refresh_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x01BA)
                        and _env_bool("USDOF_GAME_USERINFO_AFTER_LOAD_EXTEND_01BA_PIV", False)
                    ):
                        try:
                            packets, note = self._db_userinfo_packet_stream(
                                "piv-0x01ba-load-extend-refresh",
                                include_roster=True,
                                include_followups=False,
                            )
                            response_packets.extend(packets)
                            load_extend_01ba_piv_refresh_sent = True
                            db_userinfo_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued post-01BA PIV USERINFO refresh {note}")
                        except Exception as exc:
                            decision_note += f"; post-01BA PIV USERINFO failed: {exc}"
                            self.log(f"[Game/DB] post-01BA PIV USERINFO failed: {exc}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not load_extend_0080_sent
                        and piv_plain is not None
                        and (piv_plain.echoed_flag & 0xFF, piv_plain.echoed_opcode & 0xFFFF) == (0, 0x01BA)
                        and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_0080_AFTER_01BA_PIV", False)
                    ):
                        packet, note = self._load_extend_characs_0080_packet()
                        response_packets.append(packet)
                        load_extend_0080_sent = True
                        decision_note += "; " + note
                        self.log(f"[Game/DB] queued {note}")
                        if (
                            not load_extend_0080_refresh_sent
                            and _env_bool("USDOF_GAME_USERINFO_WITH_LOAD_EXTEND_0080", False)
                        ):
                            try:
                                packets, refresh_note = self._db_userinfo_packet_stream(
                                    "piv-0x01ba-load-extend-0080-refresh",
                                    include_roster=True,
                                    include_followups=False,
                                )
                                response_packets.extend(packets)
                                load_extend_0080_refresh_sent = True
                                db_userinfo_sent = True
                                decision_note += "; " + refresh_note
                                self.log(f"[Game/DB] queued post-0080 USERINFO refresh {refresh_note}")
                            except Exception as exc:
                                decision_note += f"; post-0080 USERINFO failed: {exc}"
                                self.log(f"[Game/DB] post-0080 USERINFO failed: {exc}")
                    if (
                        header.opcode == 0x013E
                        and self.dynamic_userinfo
                        and not db_userinfo_sent
                        and userinfo_trigger in PIV_USERINFO_TRIGGERS
                    ):
                        # Never USERINFO-before-open when charselect window is still closed.
                        # Applies to armed reentry AND cold first entry (2026-07-19 live black).
                        with self._channel_leave_reentry_lock:
                            piv_reentry_armed = bool(self._channel_leave_reentry_armed)
                            piv_armed_at = float(self._channel_leave_reentry_armed_at or 0.0)
                        piv_ttl_s = max(
                            1.0, float(_env_int("USDOF_GAME_REENTRY_USERINFO_ARM_TTL_S", 30))
                        )
                        piv_arm_valid = bool(piv_reentry_armed)
                        if (
                            piv_reentry_armed
                            and piv_armed_at > 0.0
                            and (time.time() - piv_armed_at) > piv_ttl_s
                        ):
                            with self._channel_leave_reentry_lock:
                                self._channel_leave_reentry_armed = False
                            piv_arm_valid = False
                        open_before_env = _env_bool(
                            "USDOF_GAME_REENTRY_OPEN_BEFORE_USERINFO", True
                        )
                        # Open-before if window not open yet (cold or armed). Pure USERINFO
                        # only when 0004 already sent (refresh path).
                        need_open_before = open_before_env and not charselect_0004_sent
                        if need_open_before:
                            piv_reason = (
                                "piv-armed-open-before"
                                if piv_arm_valid
                                else "piv-cold-open-before"
                            )
                            try:
                                (
                                    reentry_queue,
                                    login_state_init_0001_sent,
                                    charselect_0004_sent,
                                    synthetic_piv,
                                    userinfo_flag,
                                ) = self._build_reentry_open_before_userinfo_queue(
                                    session=session_state,
                                    reason=piv_reason,
                                    login_state_init_0001_sent=login_state_init_0001_sent,
                                    charselect_0004_sent=charselect_0004_sent,
                                )
                                for packet, note in reentry_queue:
                                    response_packets.append(packet)
                                    decision_note += "; " + note
                                if synthetic_piv:
                                    charselect_0004_piv_seen = True
                                    decision_note += "; reentry-synthetic-0004-piv-seen"
                                if userinfo_flag:
                                    db_userinfo_sent = True
                                reentry_charselect_pending = not charselect_0004_sent
                                session_state.reentry_forced_charselect = True
                                with self._channel_leave_reentry_lock:
                                    self._channel_leave_reentry_armed = False
                                decision_note += (
                                    f"; {piv_reason}"
                                    "; reentry_forced_charselect=1"
                                )
                                self.log(
                                    "[Game/Reentry] PIV path open-before-USERINFO "
                                    f"reason={piv_reason} "
                                    f"login={int(login_state_init_0001_sent)} "
                                    f"charselect={int(charselect_0004_sent)} "
                                    f"userinfo={int(db_userinfo_sent)} "
                                    f"queue={len(reentry_queue)}"
                                )
                            except Exception as exc:
                                decision_note += (
                                    f"; PIV open-before failed: {exc}"
                                )
                                self.log(
                                    f"[Game/Reentry] PIV open-before failed: {exc}"
                                )
                        else:
                            try:
                                packets, note = self._db_userinfo_packet_stream(
                                    "piv-0x013e",
                                    include_roster=True,
                                    include_followups=False,
                                )
                                response_packets.extend(packets)
                                db_userinfo_sent = True
                                with self._channel_leave_reentry_lock:
                                    self._channel_leave_reentry_armed = False
                                decision_note += "; " + note
                                self.log(f"[Game/DB] queued dynamic PIV USERINFO {note}")
                            except Exception as exc:
                                decision_note += f"; dynamic PIV DB USERINFO failed: {exc}"
                                self.log(f"[Game/DB] dynamic PIV USERINFO failed: {exc}")
                    if (
                        self.dynamic_userinfo
                        and header.flag_or_magic == 1
                        and header.opcode == 0x01F8
                        and charselect_0004_sent
                        and not post_charselect_01f8_userinfo_sent
                        and _env_bool("USDOF_GAME_USERINFO_AFTER_CHARSELECT_01F8", False)
                    ):
                        try:
                            packets, note = self._db_userinfo_packet_stream(
                                "post-charselect-01f8-refresh",
                                include_roster=True,
                                include_followups=False,
                            )
                            response_packets.extend(packets)
                            post_charselect_01f8_userinfo_sent = True
                            db_userinfo_sent = True
                            decision_note += "; post-charselect 0x01F8 USERINFO refresh: " + note
                            self.log(
                                "[Game/DB] queued post-charselect 0x01F8 USERINFO refresh "
                                f"{note}; reverse basis: 0x0004 tail builds C->S 0x01F8, "
                                "and USERINFO type2/type4 enters sub_2B8E540 while type0 does not"
                            )
                        except Exception as exc:
                            decision_note += f"; post-charselect 0x01F8 USERINFO failed: {exc}"
                            self.log(f"[Game/DB] post-charselect 0x01F8 USERINFO failed: {exc}")
                    if (
                        self.dynamic_userinfo
                        and header.flag_or_magic == 1
                        and header.opcode == 0x008C
                        and charselect_0004_sent
                        and not post_charselect_008c_userinfo_sent
                        and _env_bool("USDOF_GAME_USERINFO_AFTER_CHARSELECT_008C", False)
                    ):
                        try:
                            packets, note = self._db_userinfo_packet_stream(
                                "post-charselect-008c-refresh",
                                include_roster=True,
                                include_followups=False,
                            )
                            for packet in packets:
                                delayed_response_packets.append(
                                    (
                                        packet,
                                        "post-charselect 0x008C delayed USERINFO refresh: " + note,
                                    )
                                )
                            post_charselect_008c_userinfo_sent = True
                            db_userinfo_sent = True
                            decision_note += "; queued delayed post-charselect 0x008C USERINFO refresh: " + note
                            self.log(
                                "[Game/DB] queued delayed post-charselect 0x008C USERINFO refresh "
                                f"{note}; reverse basis: 0x01603250 sends C->S 0x008C after 0x01F8/0x01BA "
                                "near the charselect-open tail; keep disabled by default because x32dbg showed "
                                "no second 0x018D4140 dispatch for either post-0x01F8 or post-0x008C USERINFO"
                            )
                        except Exception as exc:
                            decision_note += f"; post-charselect 0x008C USERINFO failed: {exc}"
                            self.log(f"[Game/DB] post-charselect 0x008C USERINFO failed: {exc}")
                    # 进房后首个 C1/008C：客户端 UI settle 后再刷装备栏 000E（避免二次 U1 打乱槽）
                    if (
                        header.flag_or_magic == 1
                        and header.opcode == 0x008C
                        and session_state is not None
                        and bool(getattr(session_state, "enter_seria_settled", False))
                        and not bool(getattr(session_state, "enter_seria_equip_refreshed", False))
                    ):
                        try:
                            refresh_note = self._maybe_send_equip_refresh_on_008c(
                                client,
                                session_state,
                                connection_id=prefix,
                            )
                            if refresh_note and "already" not in refresh_note and "skip" not in refresh_note and "off" not in refresh_note:
                                decision_note += "; " + refresh_note
                        except Exception as equip_008c_exc:
                            self.log(
                                "[Game/EnterSeria] on-008c equip refresh failed: "
                                + repr(equip_008c_exc)
                            )
                    # The wish snapshot has its own once-per-bind gate.  It
                    # must still run when an equipment refresh already marked
                    # this 008C as handled (or when equipment refresh is off).
                    if (
                        header.flag_or_magic == 1
                        and header.opcode == 0x008C
                        and session_state is not None
                        and bool(getattr(session_state, "enter_seria_settled", False))
                    ):
                        try:
                            wish_008c_note = self._maybe_send_wish_snapshot_on_008c(
                                client,
                                session_state,
                                connection_id=prefix,
                            )
                            if wish_008c_note and "skip" not in wish_008c_note and "already" not in wish_008c_note:
                                decision_note += "; " + wish_008c_note
                        except Exception as wish_008c_exc:
                            self.log(
                                "[Game/EnterSeria] on-008c wish snapshot failed: "
                                + repr(wish_008c_exc)
                            )
                    if is_epic_book_make_item_overflow_report(
                        header.opcode, decode.plain
                    ):
                        decision_note += (
                            "; C1/00D9 Epic Book S1/0313 reader overflow; "
                            "no USERINFO followups"
                        )
                        self.log(
                            "[Game/EpicBook] C1/00D9 body=01 13 03 "
                            "reported S1/0313 reader overflow; suppressed unrelated "
                            "USERINFO followups"
                        )
                    elif should_suppress_dynamic_userinfo_for_dungeon_00d9(
                        header.opcode, session_state
                    ):
                        ack_response_sent = True
                        reported = bytes(decode.plain or b"")[:3].hex(" ")
                        decision_note += (
                            "; C1/00D9 active-dungeon reader report; "
                            "suppressed character-select ACK followups"
                        )
                        self.log(
                            "[Game/Dungeon] C1/00D9 active-dungeon reader report "
                            "body=%s; suppressed 0286/01BA character-select followups"
                            % (reported,)
                        )
                    elif header.opcode == 0x00D9 and self.dynamic_userinfo and not ack_response_sent:
                        ack_response_sent = True
                        if (
                            not load_extend_01fa_sent
                            and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FA_AFTER_00D9", False)
                        ):
                            packet, note = self._load_extend_characs_01fa_packet()
                            response_packets.append(packet)
                            load_extend_01fa_sent = True
                            decision_note += "; " + note
                            self.log(f"[Game/DB] queued {note}")
                            if (
                                not load_extend_01fb_sent
                                and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FB_AFTER_01FA", False)
                            ):
                                packet, note = self._load_extend_characs_01fb_packet()
                                response_packets.append(packet)
                                load_extend_01fb_sent = True
                                decision_note += "; " + note
                                self.log(f"[Game/DB] queued {note}")
                            if (
                                not load_extend_01fc_sent
                                and _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01FC_AFTER_01FA", False)
                            ):
                                packet, note = self._load_extend_characs_01fc_packet()
                                response_packets.append(packet)
                                load_extend_01fc_sent = True
                                decision_note += "; " + note
                                self.log(f"[Game/DB] queued {note}")
                        try:
                            include_followups = _env_bool("USDOF_GAME_USERINFO_FOLLOWUPS", True)
                            include_roster = not (
                                db_userinfo_sent
                                and userinfo_trigger in PIV_USERINFO_TRIGGERS
                            )
                            followups_then_roster = (
                                userinfo_trigger in ACK_FOLLOWUPS_THEN_ROSTER_TRIGGERS
                                or _env_bool("USDOF_GAME_ACK_RESEND_USERINFO_AFTER_FOLLOWUPS", False)
                            )
                            queue_aware_ack = userinfo_trigger in ACK_QUEUE_AWARE_TRIGGERS
                            if followups_then_roster:
                                ack_packets: list[bytes] = []
                                note_parts: list[str] = []
                                if include_followups:
                                    followup_packets, followup_note = self._db_userinfo_packet_stream(
                                        "ack-0x00d9-followups-before-roster",
                                        include_roster=False,
                                        include_followups=True,
                                    )
                                    ack_packets.extend(followup_packets)
                                    note_parts.append(followup_note)
                                else:
                                    note_parts.append("followups-disabled")

                                roster_packets, roster_note = self._db_userinfo_packet_stream(
                                    "ack-0x00d9-roster-after-followups",
                                    include_roster=True,
                                    include_followups=False,
                                )
                                ack_packets.extend(roster_packets)
                                response_packets.extend(ack_packets)
                                db_userinfo_sent = True
                                note = (
                                    "ACK followups-then-roster "
                                    f"trigger={userinfo_trigger}; "
                                    + "; ".join(note_parts + [roster_note])
                                )
                                decision_note += "; " + note
                                self.log(f"[Game/DB] queued dynamic {note}")
                            elif queue_aware_ack:
                                roster_packets: list[bytes] = []
                                if include_roster:
                                    roster_packets, roster_note = self._db_userinfo_packet_stream(
                                        "ack-0x00d9-roster",
                                        include_roster=True,
                                        include_followups=False,
                                    )
                                    response_packets.extend(roster_packets)
                                    db_userinfo_sent = True
                                else:
                                    roster_note = "DB USERINFO roster already sent before ACK; not re-sent"

                                queued_note = "followups-disabled"
                                if include_followups:
                                    followup_packets, followup_note = self._db_userinfo_packet_stream(
                                        "after-userinfo-piv",
                                        include_roster=False,
                                        include_followups=True,
                                    )
                                    p1_piv_followup_queue = self._filtered_p1_followups(followup_packets)
                                    queued_note = (
                                        f"{followup_note}; queued="
                                        + (
                                            ", ".join(note for _packet, note in p1_piv_followup_queue)
                                            if p1_piv_followup_queue
                                            else "<empty>"
                                        )
                                    )
                                    if p1_piv_followup_queue and p1_queue_step_mode in {
                                        "delay",
                                        "delayed",
                                        "timer",
                                        "delay-all",
                                        "piv-or-delay",
                                        "piv-delay",
                                    }:
                                        delayed_response_packets.extend(p1_piv_followup_queue)
                                        queued_note += (
                                            f"; delayed-release-ms={os.environ.get('USDOF_GAME_FOLLOWUP_DELAY_MS', '150')}"
                                        )
                                        p1_piv_followup_queue = []
                                        p1_waiting_for_piv_after = None
                                    elif p1_piv_followup_queue and p1_queue_step_mode in {"immediate", "none", "off"}:
                                        for queued_packet, queued_packet_note in p1_piv_followup_queue:
                                            response_packets.append(queued_packet)
                                            self.log(f"[Game/P1Gate] immediate followup release {queued_packet_note}")
                                        p1_piv_followup_queue = []
                                        p1_waiting_for_piv_after = None
                                    elif p1_piv_followup_queue and include_roster:
                                        p1_waiting_for_piv_after = (0, 0x0002)

                                decision_note += (
                                    "; P1 gated ACK roster/followups "
                                    f"trigger={userinfo_trigger} step_mode={p1_queue_step_mode}; "
                                    f"{roster_note}; {queued_note}"
                                )
                                self.log(
                                    "[Game/DB] queued dynamic ACK roster with PIV-gated followups "
                                    f"trigger={userinfo_trigger} step_mode={p1_queue_step_mode}; "
                                    f"{roster_note}; {queued_note}"
                                )
                            else:
                                packets, note = self._db_userinfo_packet_stream(
                                    "ack-0x00d9",
                                    include_roster=include_roster,
                                    include_followups=include_followups,
                                )
                                response_packets.extend(packets)
                                if include_roster:
                                    db_userinfo_sent = True
                                decision_note += "; " + note
                                self.log(f"[Game/DB] queued dynamic ACK-followup {note}")
                        except Exception as exc:
                            decision_note += f"; dynamic DB USERINFO failed: {exc}"
                            self.log(f"[Game/DB] dynamic USERINFO failed: {exc}")
                    elif header.opcode == 0x00D9 and self.ack_response_probes and not ack_response_sent:
                        ack_response_sent = True
                        for flag, opcode, probe_payload in self.ack_response_probes:
                            response_packets.append(_build_server_game_packet(flag, opcode, probe_payload))
                        self.log(
                            "[Game] queued ACK-followup probes "
                            + ", ".join(f"flag={flag}/opcode=0x{opcode:04x}/payload={len(probe_payload)}" for flag, opcode, probe_payload in self.ack_response_probes)
                        )
                    # Probe Z: release held enter-seria delayed packets after C->S 0x0162.
                    if (
                        header.flag_or_magic == 1
                        and header.opcode == 0x0162
                        and enter_seria_after_0162_queue
                    ):
                        released = []
                        for item in enter_seria_after_0162_queue:
                            packet = item[0]
                            note = item[1].replace('enter-seria-after-0131', 'enter-seria-after-0162-released')
                            released.append((packet, note))
                        enter_seria_after_0162_queue = []
                        delayed_response_packets.extend(released)
                        decision_note += (
                            '; released enter-seria after C->S 0x0162 packets='
                            + str(len(released))
                        )
                        self.log(
                            '[Game/EnterSeriaGate] released enter-seria delayed packets after C->S 0x0162 '
                            + 'count=' + str(len(released)) + ': '
                            + ', '.join(note for _packet, note in released)
                        )
                    if self.dynamic_userinfo:
                        cold_charselect_recovery.observe(
                            header.flag_or_magic, header.opcode, decode.plain
                        )
                        if hotpath_request is not None:
                            hotpath_request.mark_route_start()
                        try:
                            route = route_dynamic_game_packet(
                                self,
                                flag=header.flag_or_magic,
                                opcode=header.opcode,
                                plain=decode.plain,
                                session=session_state,
                                create_character_userinfo_refresh_pending=create_character_userinfo_refresh_pending,
                                charselect_0004_sent=charselect_0004_sent,
                            )
                        except Exception as exc:
                            # A failed handler must not leak its partial output
                            # into a later request on this connection.
                            discarded_delayed = len(self.pop_delayed_response_packets())
                            self.log(
                                f"[Game/DB] route_dynamic_game_packet failed opcode=0x{header.opcode:04x}: {exc}; discarded_delayed={discarded_delayed}"
                            )
                            decision_note += f"; route failed opcode=0x{header.opcode:04x}: {exc}"
                            route = None
                        finally:
                            if hotpath_request is not None:
                                hotpath_request.mark_route_end()
                        (
                            cold_charselect_dup_0008_guard_armed,
                            suppress_cold_charselect_dup_0008,
                        ) = _advance_cold_charselect_dup_0008_guard(
                            armed=cold_charselect_dup_0008_guard_armed,
                            flag=header.flag_or_magic,
                            opcode=header.opcode,
                            body=decode.plain,
                        )
                        if route is not None and route.handled:
                            response_packets.extend(route.response_packets)
                            queued_delayed_packets = self.pop_delayed_response_packets()
                            if queued_delayed_packets:
                                delayed_response_packets.extend(queued_delayed_packets)
                            create_character_userinfo_refresh_pending = route.create_character_userinfo_refresh_pending
                            decision_note += "; " + route.note
                            if self.diagnostic_trace:
                                self.log(f"[Game/DB] {route.note}")
                            # 游戏内 C1/0007 回角色页：
                            # IDA op7 已本地建 type=12；默认 **不** 再叠 S1/0001+0004（二次开窗
                            # = 黑屏可盲点）。S4A12 是 ACK+主动 USERINFO；Stage3 再加 PIV-0007
                            # 门闩，避免 ACK 与 roster 同 burst。清进图残留 P1/delayed。
                            if header.flag_or_magic == 1 and (
                                header.opcode == 0x0007 or
                                (header.opcode == 0x01FE and bool(getattr(
                                    session_state, "rename_return_select_requested", False)))
                            ):
                                session_state.rename_return_select_requested = False
                                dropped_p1 = len(p1_piv_followup_queue)
                                dropped_p1_notes = _format_p1_queue_notes(p1_piv_followup_queue)
                                p1_piv_followup_queue = []
                                p1_waiting_for_piv_after = None
                                charselect_0004_piv_deferred_packets = []
                                # 丢弃 0162 门闩里尚未释放的进房包（避免回页后再灌城镇）
                                if enter_seria_after_0162_queue:
                                    dropped_0162 = len(enter_seria_after_0162_queue)
                                    enter_seria_after_0162_queue = []
                                    decision_note += (
                                        f"; return-select-drop-0162-hold={dropped_0162}"
                                    )
                                    self.log(
                                        "[Game/ReturnSelect] cleared enter-seria 0162-hold queue "
                                        f"count={dropped_0162}"
                                    )
                                # 丢弃服务端全局 delayed 队列残留（跨包 pop 间隙）
                                stale_delayed = self.pop_delayed_response_packets()
                                if stale_delayed:
                                    decision_note += (
                                        f"; return-select-drop-stale-delayed={len(stale_delayed)}"
                                    )
                                    self.log(
                                        "[Game/ReturnSelect] cleared stale delayed queue "
                                        f"count={len(stale_delayed)}"
                                    )
                                # 丢弃本包刚 pop 的进图 delayed（enter-seria 等）
                                if delayed_response_packets:
                                    kept_delayed: list[tuple[bytes, str]] = []
                                    dropped_delayed = 0
                                    for item in delayed_response_packets:
                                        _pkt, dnote = item
                                        if (
                                            "enter-seria" in dnote
                                            or "enter-game" in dnote
                                            or "post-charselect" in dnote
                                        ):
                                            dropped_delayed += 1
                                            continue
                                        kept_delayed.append(item)
                                    delayed_response_packets = kept_delayed
                                    if dropped_delayed:
                                        decision_note += (
                                            f"; return-select-drop-delayed={dropped_delayed}"
                                        )
                                if dropped_p1:
                                    decision_note += (
                                        f"; return-select-drop-p1-queue={dropped_p1}"
                                    )
                                    self.log(
                                        "[Game/ReturnSelect] cleared leftover P1 followup queue "
                                        f"count={dropped_p1} notes=[{dropped_p1_notes}] "
                                        "(avoid mid-charselect re-inject)"
                                    )
                                # 会话：进房未完成标记复位，避免半城镇态污染角色页
                                try:
                                    session_state.enter_seria_settled = False
                                    session_state.enter_seria_in_progress = False
                                    session_state.enter_seria_equip_refreshed = False
                                    session_state.enter_seria_post_settle_000e_sent = False
                                    session_state.enter_seria_wish_snapshot_post_settle_sent = False
                                except Exception:
                                    pass
                                # op7 本身完成选角 UI：标记已在角色页，便于后续 0008。
                                session_state.reentry_forced_charselect = True
                                reentry_charselect_pending = False
                                decision_note += (
                                    "; return-select-op7-type12"
                                    "; reentry_forced_charselect=1"
                                    "; return-select-ui-reset"
                                )
                                # 拆包：immediate = ACK（+可选 A/B 开窗）；USERINFO 起全部
                                # 进 PIV(1,0007) 门闩，避免同 burst 抢在 teardown 前。
                                gate_userinfo = _env_bool(
                                    "USDOF_GAME_RETURN_SELECT_GATE_USERINFO_ON_0007_PIV",
                                    True,
                                )
                                immediate_packets: list[bytes] = []
                                gated_packets: list[tuple[bytes, str]] = []
                                saw_userinfo = False
                                for pkt in list(response_packets):
                                    key = _packet_key(pkt)
                                    if key == (1, 0x0001):
                                        login_state_init_0001_sent = True
                                    if key == (1, 0x0004):
                                        charselect_0004_sent = True
                                        if _env_bool(
                                            "USDOF_GAME_RETURN_SELECT_SYNTHETIC_0004_PIV_SEEN",
                                            False,
                                        ):
                                            charselect_0004_piv_seen = True
                                            decision_note += (
                                                "; return-select-synthetic-0004-piv-seen"
                                            )
                                        decision_note += "; return-select-charselect-open"
                                    if key == (0, 0x0002):
                                        saw_userinfo = True
                                    if gate_userinfo and (
                                        saw_userinfo or key == (0, 0x0002)
                                    ):
                                        gated_packets.append(
                                            (
                                                pkt,
                                                (
                                                    "return-select-after-0007-piv "
                                                    f"{_packet_key_text(pkt)}; {route.note}"
                                                ),
                                            )
                                        )
                                    else:
                                        immediate_packets.append(pkt)
                                if gate_userinfo and gated_packets:
                                    response_packets = immediate_packets
                                    p1_waiting_for_piv_after = (1, 0x0007)
                                    p1_piv_followup_queue = (
                                        gated_packets + p1_piv_followup_queue
                                    )
                                    decision_note += (
                                        f"; return-select-gate-userinfo-on-piv-0007 "
                                        f"immediate={len(immediate_packets)} "
                                        f"queued={len(gated_packets)}"
                                    )
                                    self.log(
                                        "[Game/ReturnSelect] gated USERINFO until C->S PIV "
                                        f"flag1/0x0007; immediate={len(immediate_packets)} "
                                        f"queued={len(gated_packets)}"
                                    )
                                elif any(
                                    _packet_key(pkt) == (0, 0x0002)
                                    for pkt in response_packets
                                ):
                                    db_userinfo_sent = True
                                # 无 0004 开窗时也视为「已在角色页」：后续 C1/0008 不 defer。
                                if not charselect_0004_sent and _env_bool(
                                    "USDOF_GAME_RETURN_SELECT_MARK_CHARSELECT_READY",
                                    True,
                                ):
                                    charselect_0004_sent = True
                                    charselect_0004_piv_seen = True
                                    decision_note += (
                                        "; return-select-mark-charselect-ready"
                                        " (op7 type=12; allow C1/0008)"
                                    )
                            # Reentry short-leave path: client often sends C->S 0x01BA after
                            # unsolicited USERINFO without ever PIV-echoing S->C 0x01BA.
                            # Cold path opens LOGIN/CHARSELECT on that PIV; reentry must open here.
                            if (
                                reentry_charselect_pending
                                and not charselect_0004_sent
                                and header.flag_or_magic == 1
                                and header.opcode == 0x01BA
                                and _env_bool("USDOF_GAME_REENTRY_OPEN_CHARSELECT_ON_01BA", True)
                            ):
                                try:
                                    if (
                                        not login_state_init_0001_sent
                                        and _env_bool(
                                            "USDOF_GAME_SEND_STATE_INIT_0001_BEFORE_CHARSELECT_0004",
                                            True,
                                        )
                                    ):
                                        packet, note = self._login_state_init_0001_packet(session=session_state)
                                        response_packets.append(packet)
                                        login_state_init_0001_sent = True
                                        decision_note += "; " + note
                                        self.log(f"[Game/Reentry] queued {note}")
                                    packet, note = self._charselect_open_0004_packet()
                                    response_packets.append(packet)
                                    charselect_0004_sent = True
                                    # Reentry never emits C->S PIV of flag1/0x0004. Cold path
                                    # defers GET_USERINFO (0x0008) until that PIV; without it
                                    # roster refresh is dropped forever and all buttons stay dead.
                                    # Treat reentry open as synthetic 0004-PIV-seen.
                                    if _env_bool("USDOF_GAME_REENTRY_SYNTHETIC_0004_PIV_SEEN", True):
                                        charselect_0004_piv_seen = True
                                        decision_note += "; reentry-synthetic-0004-piv-seen"
                                        self.log(
                                            "[Game/Reentry] synthetic CHARSELECT 0x0004 PIV-seen "
                                            "so GET_USERINFO is not deferred"
                                        )
                                    reentry_charselect_pending = False
                                    session_state.reentry_forced_charselect = True
                                    decision_note += (
                                        "; " + note
                                        + "; reentry-open-charselect-on-01ba"
                                        + "; reentry_forced_charselect=1"
                                    )
                                    self.log(
                                        f"[Game/Reentry] queued {note} (no 01BA PIV on reentry); "
                                        "reentry_forced_charselect=1"
                                    )
                                except Exception as exc:
                                    decision_note += f"; reentry open charselect failed: {exc}"
                                    self.log(f"[Game/Reentry] open charselect failed: {exc}")
                            # IMPORTANT: channel-leave close flag is set inside 0x057F handler
                            # (route_dynamic). Must evaluate AFTER route, not before.
                            if (
                                header.flag_or_magic == 1
                                and header.opcode == 0x057F
                                and bool(
                                    getattr(
                                        session_state,
                                        "channel_leave_close_after_settle",
                                        False,
                                    )
                                )
                            ):
                                close_after_packet = True
                                decision_note += "; CHANNEL-LEAVE close after 0x057F settle"
                            # Cold S1/0001 原生发送 ff ff 02 的 C1/0008；首份 roster 已由
                            # real 0004 PIV 放行时，不再用同一请求重灌第二份完整 USERINFO。
                            if (
                                header.opcode == 0x0008
                                and response_packets
                                and (suppress_cold_charselect_dup_0008 or (
                                    cold_charselect_recovery.requested
                                    and not cold_charselect_recovery.cancelled
                                    and not cold_charselect_recovery.released
                                    and charselect_0004_piv_deferred_packets
                                    and _is_get_userinfo_roster_request(decode.plain, session_state)
                                ))
                            ):
                                decision_note += "; cold-charselect-suppress-dup-0008"
                                self.log(
                                    "[Game/ColdCharselect] suppress startup C1/0008 duplicate "
                                    "after server-owned roster was scheduled"
                                )
                                response_packets = []
                            # return-select 门闩未完成时抑制重复 C1/0008：
                            # 否则 split GET_USERINFO 会改 wait-key 并把 0286 叠进残留 01BA，
                            # 再选角进房后城镇 UI 鼠标点不了。
                            elif (
                                header.opcode == 0x0008
                                and _is_get_userinfo_roster_request(decode.plain, session_state)
                                and response_packets
                                and _env_bool(
                                    "USDOF_GAME_RETURN_SELECT_SUPPRESS_DUP_0008",
                                    True,
                                )
                                and _is_return_select_userinfo_gate_active(
                                    p1_waiting_for_piv_after,
                                    p1_piv_followup_queue,
                                )
                            ):
                                decision_note += (
                                    "; return-select-suppress-dup-0008 "
                                    f"wait={p1_waiting_for_piv_after} "
                                    f"p1_queue={len(p1_piv_followup_queue)}"
                                )
                                self.log(
                                    "[Game/ReturnSelect] suppress duplicate C1/0008 while "
                                    "return-select USERINFO gate active "
                                    f"wait={p1_waiting_for_piv_after} "
                                    f"p1_queue={len(p1_piv_followup_queue)} "
                                    f"notes=[{_format_p1_queue_notes(p1_piv_followup_queue)}]"
                                )
                                response_packets = []
                            elif (
                                header.opcode == 0x0008
                                and _is_get_userinfo_roster_request(decode.plain, session_state)
                                and charselect_0004_sent
                                and not (charselect_0004_piv_seen or cold_charselect_recovery.released)
                                and response_packets
                            ):
                                for queued_packet in response_packets:
                                    charselect_0004_piv_deferred_packets.append(
                                        (
                                            queued_packet,
                                            (
                                                "deferred-after-charselect-0004-piv "
                                                f"{_packet_key_text(queued_packet)}; {route.note}"
                                            ),
                                        )
                                    )
                                response_packets = []
                                decision_note += (
                                    "; deferred GET_USERINFO responses until "
                                    "CHARSELECT 0x0004 PIV to preserve successful order"
                                )
                                self.log(
                                    "[Game/P1Gate] deferred GET_USERINFO responses until "
                                    "CHARSELECT 0x0004 PIV; observed early 0x0008 before PIV 0x0004"
                                )
                            elif (
                                header.opcode == 0x0008
                                and _is_get_userinfo_roster_request(decode.plain, session_state)
                                and charselect_0004_sent
                                and (charselect_0004_piv_seen or cold_charselect_recovery.released)
                                and len(response_packets) > 1
                            ):
                                # The same PIV gate applies when the client asks
                                # for GET_USERINFO after it has already echoed
                                # the charselect-open packet.  Do not burst
                                # USERINFO follow-ups in the same TCP response.
                                first_packet = response_packets[0]
                                remaining_packets = [
                                    (
                                        queued_packet,
                                        (
                                            "post-charselect-0008-piv-gated "
                                            f"{_packet_key_text(queued_packet)}; {route.note}"
                                        ),
                                    )
                                    for queued_packet in response_packets[1:]
                                ]
                                response_packets = [first_packet]
                                try:
                                    p1_waiting_for_piv_after = _packet_key(first_packet)
                                except Exception:
                                    p1_waiting_for_piv_after = None
                                if remaining_packets:
                                    p1_piv_followup_queue = remaining_packets + p1_piv_followup_queue
                                decision_note += (
                                    "; split GET_USERINFO response after CHARSELECT 0x0004 PIV; "
                                    f"sent_first={_packet_key_text(first_packet)} "
                                    f"queued_remaining={len(remaining_packets)}"
                                )
                                self.log(
                                    "[Game/P1Gate] split GET_USERINFO response after CHARSELECT 0x0004 PIV; "
                                    f"sent_first={_packet_key_text(first_packet)} queued_remaining="
                                    + ", ".join(note for _packet, note in remaining_packets)
                                )

                    if header.opcode == 0x013E and piv_plain is not None:
                        try:
                            native_loadout_note = note_native_loadout_piv(
                                session_state,
                                piv_plain.echoed_flag,
                                piv_plain.echoed_opcode,
                            )
                        except OSError as exc:
                            native_loadout_note = (
                                "native_loadout_ack_write_fail:%s"
                                % type(exc).__name__
                            )
                        if native_loadout_note:
                            decision_note += "; " + native_loadout_note
                        package_refresh_packets = release_cera_package_refresh_on_piv(
                            session_state,
                            piv_plain.echoed_flag,
                            piv_plain.echoed_opcode,
                        )
                        if package_refresh_packets:
                            response_packets.extend(
                                packet for packet, _note in package_refresh_packets
                            )
                            piv_flag = int(piv_plain.echoed_flag) & 0xFF
                            piv_opcode = int(piv_plain.echoed_opcode) & 0xFFFF
                            refresh_notes = ", ".join(
                                note for _packet, note in package_refresh_packets
                            )
                            decision_note += (
                                "; released Cera package refresh after PIV "
                                f"flag={piv_flag}/opcode=0x{piv_opcode:04x} "
                                f"count={len(package_refresh_packets)}"
                            )
                            self.log(
                                "[Game/CeraPackageGate] released refresh after "
                                f"PIV flag={piv_flag}/opcode=0x{piv_opcode:04x}: "
                                + refresh_notes
                            )

                    if self.diagnostic_trace and self.protocol_traffic_log:
                        self.log(
                            f"[Game] packet {describe_game_header(header)} "
                            f"decode={decode.note} decision={decision_note}"
                        )
                    self._log_game_sequence(
                        direction="C->S",
                        connection_id=prefix,
                        packet=recv_packet_for_sequence,
                        compact_prefix=True,
                        plain_body=decode.plain,
                        semantic=opcode_meaning(header.flag_or_magic, header.opcode),
                        decode_note=decode.note,
                        decision_note=decision_note,
                    )
                    if self.diagnostic_trace:
                        append_trace(
                            raw_hex=packet.hex(" "),
                            header=header,
                            decode=decode,
                            probable_handler=opcode_meaning(
                                header.flag_or_magic, header.opcode
                            ),
                            decision=decision_note,
                            capture_path=capture_path,
                        )
                    if (
                        header.opcode == 0x013E
                        and response_packets
                        and _env_bool("USDOF_GAME_PIV_USERINFO_DELAY_ENABLE", False)
                    ):
                        delay_ms = int(os.environ.get("USDOF_GAME_PIV_USERINFO_DELAY_MS", "0"), 0)
                        if delay_ms > 0:
                            self.log(
                                f"[Game/P1Gate] delaying PIV response packets by {delay_ms}ms "
                                "for USERINFO-ready timing experiment"
                            )
                            time.sleep(delay_ms / 1000.0)
                    previous_response_packet: bytes | None = None
                    for response in response_packets:
                        try:
                            charselect_delay_ms = _charselect_state_init_interpacket_delay_ms(
                                previous_response_packet,
                                response,
                            )
                            if charselect_delay_ms > 0:
                                self.log(
                                    "[Game/P1Gate] delaying CHARSELECT 0x0004 after "
                                    f"state-init 0x0001 by {charselect_delay_ms}ms "
                                    "to avoid PIV ACK/render-init race"
                                )
                                time.sleep(charselect_delay_ms / 1000.0)
                            # Reentry open order gaps (0001→0004→USERINFO); also covers
                            # PIV-path armed open-before queue in response_packets.
                            reentry_gap_ms = _reentry_open_userinfo_gap_ms(
                                previous_response_packet,
                                response,
                            )
                            # Avoid double-sleep when charselect delay already covered 0001→0004.
                            if reentry_gap_ms > charselect_delay_ms:
                                extra_gap = reentry_gap_ms - charselect_delay_ms
                                self.log(
                                    f"[Game/Reentry] gap {extra_gap}ms before "
                                    f"{_packet_key_text(response)} (open-before-USERINFO order)"
                                )
                                time.sleep(extra_gap / 1000.0)
                            # Small gap between S->C 0x0323 and 0x020C so the client
                            # runs 0x016633E0 before 0x0171F970 matches channel_id.
                            if previous_response_packet is not None:
                                prev_key = _packet_key(previous_response_packet)
                                cur_key = _packet_key(response)
                                if prev_key == (1, 0x0244) and cur_key in {(0, 0x0323), (0, 0x020C)}:
                                    gap_ms = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_0244_NEXT_GAP_MS", 40))
                                    if gap_ms > 0:
                                        time.sleep(gap_ms / 1000.0)
                                elif prev_key == (0, 0x0323) and cur_key == (0, 0x020C):
                                    gap_ms = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_0323_020C_GAP_MS", 40))
                                    if gap_ms > 0:
                                        time.sleep(gap_ms / 1000.0)
                                elif prev_key == (0, 0x020C) and cur_key == (1, 0x0244):
                                    # Trailing 0244 after list mutate: give client a beat to finish
                                    # 0x0171F970 before UI42 force-close (first cold leave residual).
                                    gap_ms = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_020C_0244_GAP_MS", 40))
                                    if gap_ms > 0:
                                        time.sleep(gap_ms / 1000.0)
                                elif prev_key == (0, 0x020C) and cur_key == (0, 0x0208):
                                    gap_ms = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_020C_0208_GAP_MS", 40))
                                    if gap_ms > 0:
                                        time.sleep(gap_ms / 1000.0)
                            self._log_game_sequence(
                                direction="S->C",
                                connection_id=prefix,
                                packet=response,
                                compact_prefix=(
                                    _s2c_plain_wire_enabled()
                                    and _plain_wire_header_mode() == "13"
                                ),
                                semantic=opcode_meaning(*_packet_key(response)),
                                note="immediate response",
                            )
                            if _s2c_plain_wire_enabled():
                                self._log_plain_wire_packet("send", response)
                            if hotpath_request is not None:
                                hotpath_request.mark_first_send_start()
                            send_raid_packet(self, session_state, client, response)
                            raid_lobby.note_send(self, session_state, response)
                            if hotpath_request is not None:
                                hotpath_request.mark_first_send_end()
                            if self.diagnostic_trace and self.protocol_traffic_log:
                                self.log(
                                    f"[Game] sent response bytes={len(response)} "
                                    f"hex={response.hex(' ')}"
                                )
                            previous_response_packet = response
                        except OSError as exc:
                            self.hotpath_metrics.finish_request()
                            self.log(f"[Game] send response failed: {exc}; closing client handler")
                            return
                    self.hotpath_metrics.finish_request()
                    # Channel page → character select (cold first entry OR Select Channel reentry).
                    #
                    # Hard order (black screen if USERINFO before open):
                    #   LOGIN 0x0001 -> CHARSELECT 0x0004 -> USERINFO 0x0002
                    #
                    # 2026-07-19 live (game_server_20260719_082352):
                    #   cold unarmed path deferred to PIV USERINFO-first → black UI,
                    #   roster still clickable (C1/0x02c1 blind clicks). Same symptom as
                    #   reentry USERINFO-before-open.
                    # Fix: whenever OPEN_BEFORE_USERINFO=1, always open 0001->0004
                    # immediately after opcode1, cold and armed alike. Cold entry then
                    # gates USERINFO on the real 0004 PIV; armed reentry retains the
                    # immediate USERINFO fallback because that path may omit the PIV.
                    # A/B only: OPEN_BEFORE_USERINFO=0 restores wait; early PIV = old cold.
                    if (
                        self.dynamic_userinfo
                        and not db_userinfo_sent
                        and header.flag_or_magic == 1
                        and header.opcode == 0x0000
                        and any(_packet_key(pkt) == (0, 0x0001) for pkt in response_packets)
                        and _env_bool("USDOF_GAME_REENTRY_USERINFO_FALLBACK", True)
                    ):
                        with self._channel_leave_reentry_lock:
                            reentry_armed = bool(self._channel_leave_reentry_armed)
                            armed_at = float(self._channel_leave_reentry_armed_at or 0.0)
                        wait_ms = max(0, _env_int("USDOF_GAME_REENTRY_USERINFO_WAIT_MS", 350))
                        require_arm = _env_bool("USDOF_GAME_REENTRY_USERINFO_REQUIRE_ARM", False)
                        ttl_s = max(1.0, float(_env_int("USDOF_GAME_REENTRY_USERINFO_ARM_TTL_S", 30)))
                        arm_valid = bool(reentry_armed)
                        if reentry_armed and armed_at > 0.0 and (time.time() - armed_at) > ttl_s:
                            with self._channel_leave_reentry_lock:
                                self._channel_leave_reentry_armed = False
                            arm_valid = False
                            self.log(
                                "[Game/Reentry] second-Game reentry arm expired; "
                                "treat as unarmed"
                            )
                        if require_arm and not arm_valid:
                            self.log(
                                "[Game/Reentry] fallback skipped (REQUIRE_ARM=1 and not armed)"
                            )
                        else:
                            open_before_env = _env_bool(
                                "USDOF_GAME_REENTRY_OPEN_BEFORE_USERINFO", True
                            )
                            # Default ON: cold first entry and armed reentry both open-before.
                            force_open_before = bool(open_before_env)
                            saw_early_client = False
                            if force_open_before:
                                self.log(
                                    "[Game/Reentry] after opcode1: immediate "
                                    "open-before-USERINFO "
                                    f"(skip PIV wait; armed={int(arm_valid)} "
                                    f"cold_or_reentry=1)"
                                )
                            else:
                                # A/B legacy: wait; early PIV defers to cold USERINFO-first.
                                self.log(
                                    f"[Game/Reentry] after opcode1 wait {wait_ms}ms for C->S PIV "
                                    f"before USERINFO fallback armed={int(reentry_armed)} "
                                    f"arm_valid={int(arm_valid)} open_before=0"
                                )
                                deadline = time.time() + (wait_ms / 1000.0)
                                while time.time() < deadline and not db_userinfo_sent:
                                    remaining = max(0.0, deadline - time.time())
                                    if remaining <= 0.0:
                                        break
                                    try:
                                        ready, _, _ = select.select(
                                            [client], [], [], min(0.05, remaining)
                                        )
                                    except (OSError, ValueError):
                                        break
                                    if not ready:
                                        continue
                                    try:
                                        peek = client.recv(4096)
                                    except OSError:
                                        break
                                    if not peek:
                                        break
                                    buffer.extend(peek)
                                    capture.write(peek)
                                    capture.flush()
                                    saw_early_client = True
                                    self.log(
                                        f"[Game/Reentry] early client data during wait "
                                        f"bytes={len(peek)}; defer to cold-path PIV loop "
                                        "(OPEN_BEFORE=0 A/B USERINFO-first)"
                                    )
                                    break
                            # Default: always open-before. A/B OPEN_BEFORE=0: only if no early PIV.
                            should_open_before = (not db_userinfo_sent) and (
                                force_open_before or not saw_early_client
                            )
                            if should_open_before:
                                try:
                                    open_reason = (
                                        "armed-immediate"
                                        if arm_valid
                                        else "cold-immediate"
                                    )
                                    if not force_open_before:
                                        open_reason = "after-opcode1-no-piv"
                                    (
                                        send_queue,
                                        login_state_init_0001_sent,
                                        charselect_0004_sent,
                                        synthetic_piv,
                                        userinfo_flag,
                                    ) = self._build_reentry_open_before_userinfo_queue(
                                        session=session_state,
                                        reason=open_reason,
                                        login_state_init_0001_sent=login_state_init_0001_sent,
                                        charselect_0004_sent=charselect_0004_sent,
                                    )
                                    cold_piv_gate = bool(
                                        not arm_valid
                                        and _env_bool(
                                            "USDOF_GAME_COLD_USERINFO_GATE_ON_0004_PIV",
                                            True,
                                        )
                                    )
                                    send_queue, cold_piv_deferred = (
                                        _partition_cold_charselect_queue_for_0004_piv(
                                            send_queue,
                                            gate_enabled=cold_piv_gate,
                                        )
                                    )
                                    if cold_piv_deferred:
                                        charselect_0004_piv_deferred_packets.extend(
                                            (
                                                packet,
                                                "cold-entry-after-real-0004-piv "
                                                f"{_packet_key_text(packet)}; {note}",
                                            )
                                            for packet, note in cold_piv_deferred
                                        )
                                        cold_charselect_dup_0008_guard_armed = _env_bool(
                                            "USDOF_GAME_COLD_SUPPRESS_DUP_0008_AFTER_USERINFO",
                                            True,
                                        )
                                        synthetic_piv = False
                                        self.log(
                                            "[Game/P1Gate] cold entry queued USERINFO until real "
                                            "CHARSELECT 0x0004 PIV; immediate="
                                            + ",".join(
                                                _packet_key_text(packet)
                                                for packet, _note in send_queue
                                            )
                                            + " deferred="
                                            + ",".join(
                                                _packet_key_text(packet)
                                                for packet, _note in cold_piv_deferred
                                            )
                                        )
                                    if synthetic_piv:
                                        charselect_0004_piv_seen = True
                                        self.log(
                                            "[Game/Reentry] synthetic CHARSELECT 0x0004 PIV-seen "
                                            "(open-before-USERINFO)"
                                        )
                                    if userinfo_flag:
                                        db_userinfo_sent = True
                                    prev_reentry_pkt: bytes | None = None
                                    send_groups = [
                                        [item] for item in send_queue
                                    ]
                                    if (
                                        len(send_queue) >= 2
                                        and _reentry_open_userinfo_gap_ms(
                                            send_queue[0][0], send_queue[1][0]
                                        )
                                        == 0
                                    ):
                                        send_groups = _coalesce_reentry_open_packets(send_queue)
                                    for send_group in send_groups:
                                        response, send_note = send_group[0]
                                        gap_ms = _reentry_open_userinfo_gap_ms(
                                            prev_reentry_pkt, response
                                        )
                                        if gap_ms > 0:
                                            self.log(
                                                f"[Game/Reentry] gap {gap_ms}ms before "
                                                f"{_packet_key_text(response)} "
                                                f"({send_note[:48]})"
                                            )
                                            time.sleep(gap_ms / 1000.0)
                                        for grouped_response, grouped_note in send_group:
                                            self._log_game_sequence(
                                                direction="S->C",
                                                connection_id=prefix,
                                                packet=grouped_response,
                                                compact_prefix=(
                                                    _s2c_plain_wire_enabled()
                                                    and _plain_wire_header_mode() == "13"
                                                ),
                                                semantic=opcode_meaning(*_packet_key(grouped_response)),
                                                note=grouped_note,
                                            )
                                            if _s2c_plain_wire_enabled():
                                                self._log_plain_wire_packet("send", grouped_response)
                                            if self.diagnostic_trace and self.protocol_traffic_log:
                                                self.log(
                                                    f"[Game] sent reentry open/USERINFO "
                                                    f"bytes={len(grouped_response)} "
                                                    f"hex={grouped_response[:48].hex(' ')}"
                                                )
                                            prev_reentry_pkt = grouped_response
                                        login_receipts = [capture_login_send(self, session_state, client, value)
                                                          for value, _note in send_group]
                                        client.sendall(
                                            b"".join(
                                                grouped_response
                                                for grouped_response, _note in send_group
                                            )
                                        )
                                        for login_receipt in login_receipts:
                                            confirm_login_send(self, login_receipt)
                                    # Window already opened: C->S 0x01BA only load-extends.
                                    if cold_piv_deferred:
                                        cold_charselect_recovery.open_sent(time.monotonic())
                                    reentry_charselect_pending = not charselect_0004_sent
                                    session_state.reentry_forced_charselect = True
                                    with self._channel_leave_reentry_lock:
                                        self._channel_leave_reentry_armed = False
                                    open_ok = charselect_0004_sent and open_before_env
                                    decision_note += (
                                        "; reentry-open-before-USERINFO"
                                        + f"; reason={open_reason}"
                                        + (
                                            "; open-before-USERINFO"
                                            if open_ok
                                            else "; USERINFO-only"
                                        )
                                        + "; reentry_forced_charselect=1"
                                    )
                                    self.log(
                                        "[Game/Reentry] open order "
                                        f"login={int(login_state_init_0001_sent)} "
                                        f"charselect={int(charselect_0004_sent)} "
                                        f"userinfo={int(db_userinfo_sent)} "
                                        f"queue={len(send_queue)} "
                                        f"piv_pending={len(cold_piv_deferred)} "
                                        f"reason={open_reason} "
                                        f"armed={int(arm_valid)}; "
                                        "reentry_forced_charselect=1"
                                    )
                                except Exception as exc:
                                    decision_note += (
                                        f"; reentry open/USERINFO fallback failed: {exc}"
                                    )
                                    self.log(
                                        f"[Game/Reentry] open/USERINFO fallback failed: {exc}"
                                    )

                    # After channel-move settle (0x0323/0x020C), give the client a beat to
                    # run 0x0171F970 before the same TCP burst hits CMDPACKET_EXIT close.
                    try:
                        if any(
                            _packet_key(pkt) in {(0, 0x0323), (0, 0x020C), (1, 0x0244), (1, 0x0242)}
                            for pkt in response_packets
                        ):
                            flush_ms = max(0, _env_int("USDOF_GAME_CHANNEL_MOVE_FLUSH_MS", 0))
                            if flush_ms > 0:
                                self.log(
                                    f"[Game] channel-move flush pause {flush_ms}ms after settle packets"
                                )
                                time.sleep(flush_ms / 1000.0)
                    except Exception:
                        pass
                    if delayed_response_packets:
                        enter_seria_after_0131 = any(
                            "enter-seria-after-0131" in note for _, note in delayed_response_packets
                        )
                        # Stage3: 0324 needs area+0x37C from 0029; do not burst after 0131.
                        # Gate remaining delayed enter packets on PIV of previous packet.
                        enter_piv_step = enter_seria_after_0131 and _env_bool(
                            "USDOF_GAME_ENTER_SERIA_PIV_STEP_DELAYED",
                            True,
                        )
                        if enter_seria_after_0131:
                            # 进房前丢掉角色页残留 P1（return-select 01BA / 0008-gated 0286 等）。
                            # 否则会在城镇里被错误 PIV 放行，或回页时再叠一层，最终鼠标点不了 UI。
                            if _env_bool(
                                "USDOF_GAME_ENTER_SERIA_DROP_CHARSELECT_P1_LEFTOVER",
                                True,
                            ):
                                leftover_n = len(p1_piv_followup_queue)
                                deferred_n = len(charselect_0004_piv_deferred_packets)
                                if leftover_n or deferred_n or p1_waiting_for_piv_after is not None:
                                    self.log(
                                        "[Game/EnterSeria] preflight drop charselect leftovers "
                                        f"p1_queue={leftover_n} "
                                        f"notes=[{_format_p1_queue_notes(p1_piv_followup_queue)}] "
                                        f"deferred_0004={deferred_n} "
                                        f"wait={p1_waiting_for_piv_after}"
                                    )
                                    decision_note += (
                                        f"; enter-seria-preflight-drop-p1={leftover_n}"
                                        f"; drop-deferred-0004={deferred_n}"
                                    )
                                p1_piv_followup_queue = []
                                p1_waiting_for_piv_after = None
                                charselect_0004_piv_deferred_packets = []
                            try:
                                session_state.enter_seria_in_progress = True
                                session_state.enter_seria_settled = False
                                session_state.enter_seria_equip_refreshed = False
                                session_state.enter_seria_post_settle_000e_sent = False
                                session_state.enter_seria_wish_snapshot_post_settle_sent = False
                            except Exception:
                                pass
                            # Probe Z optional gate: stash delayed packets until C->S 0x0162.
                            if _env_bool('USDOF_GAME_ENTER_SERIA_GATE_ON_CS_0162', False) and delayed_response_packets:
                                enter_seria_after_0162_queue.extend(delayed_response_packets)
                                self.log(
                                    '[Game/EnterSeriaGate] holding delayed enter-seria packets until C->S 0x0162 '
                                    + 'count=' + str(len(delayed_response_packets)) + ': '
                                    + ', '.join(note for _packet, note in delayed_response_packets)
                                )
                                delayed_response_packets = []
                            gate_note = self._maybe_wait_enter_seria_after_0131_gate(delayed_response_packets)
                            if gate_note:
                                self.log("[Game/EnterSeriaGate]" + gate_note)
                            delay_ms = int(
                                os.environ.get(
                                    "USDOF_GAME_ENTER_SERIA_AFTER_0131_DELAY_MS",
                                    os.environ.get("USDOF_GAME_FOLLOWUP_DELAY_MS", "150"),
                                ),
                                0,
                            )
                            inter_ms = int(
                                os.environ.get(
                                    "USDOF_GAME_ENTER_SERIA_AFTER_0131_INTERPACKET_DELAY_MS",
                                    os.environ.get("USDOF_GAME_FOLLOWUP_INTERPACKET_DELAY_MS", "60"),
                                ),
                                0,
                            )
                        else:
                            delay_ms = int(os.environ.get("USDOF_GAME_FOLLOWUP_DELAY_MS", "150"), 0)
                            inter_ms = int(os.environ.get("USDOF_GAME_FOLLOWUP_INTERPACKET_DELAY_MS", "60"), 0)
                        if enter_piv_step and delayed_response_packets:
                            # Stage3 live 2026-07-13 Probe K evidence:
                            # - client PIV-echoes 0x0131, not type0/0017/0004
                            # - waiting for non-0131 PIV leaves UI on role page
                            # - 0324 alone needs +0x37C from 0029 install; only then PIV-step
                            #   after 0029/0131 is required. When 0324 is OFF (town Probe K),
                            #   burst all delayed enter packets after 0131 with short delay.
                            # Detect real delayed 0x0324 wire only (not suppressed notes).
                            needs_0324_after_map = False
                            for packet, note in delayed_response_packets:
                                try:
                                    flag, opcode = _packet_key(packet)
                                except Exception:
                                    flag, opcode = (None, None)
                                if opcode == 0x0324:
                                    needs_0324_after_map = True
                                    break
                                if ("delayed 0x0324" in note) or ("delayed opcode=0x0324" in note):
                                    needs_0324_after_map = True
                                    break
                            if not needs_0324_after_map:
                                if delay_ms > 0:
                                    time.sleep(delay_ms / 1000.0)
                                # 主对象依赖包硬时序：
                                #   1) 可先发城镇 paint（0069/0016/0017/0018）
                                #   2) 等客户端 PIV 0x0131（主对象就绪）
                                #   3) 再发 U0 → 0013 → U1(166AED0 建槽) → 000E(更新) → 000D/list36
                                # 禁止在 0131 未就绪或角色子对象未绑定时灌这些包（会静默空栏）。
                                pre_equip, equip_rest = _split_enter_seria_delayed_for_0131_gate(
                                    delayed_response_packets
                                )
                                gate_0131 = _env_bool(
                                    "USDOF_GAME_ENTER_SERIA_GATE_EQUIP_ON_0131_PIV",
                                    True,
                                )
                                piv_timeout_ms = max(
                                    0,
                                    _env_int(
                                        "USDOF_GAME_ENTER_SERIA_0131_PIV_TIMEOUT_MS",
                                        2000,
                                    ),
                                )
                                try:
                                    if pre_equip:
                                        self._send_enter_seria_delayed_timed(
                                            client,
                                            pre_equip,
                                            connection_id=prefix,
                                            inter_ms=inter_ms,
                                            log_tag="enter-seria-pre-equip-after-0131",
                                            session_state=session_state,
                                        )
                                        previous_response_packet = pre_equip[-1][0]
                                    if equip_rest:
                                        piv_note = "piv_gate_off"
                                        if gate_0131:
                                            piv_ok, piv_note = self._wait_for_client_piv_echo(
                                                client,
                                                buffer,
                                                capture,
                                                want_flag=0,
                                                want_opcode=0x0131,
                                                timeout_ms=piv_timeout_ms,
                                                connection_id=prefix,
                                            )
                                            if not piv_ok and not _env_bool(
                                                "USDOF_GAME_ENTER_SERIA_0131_PIV_CONTINUE_ON_TIMEOUT",
                                                True,
                                            ):
                                                self.log(
                                                    "[Game/EnterSeria] abort equip delayed on 0131 PIV miss; "
                                                    + piv_note
                                                )
                                                equip_rest = []
                                            else:
                                                self.log(
                                                    "[Game/EnterSeria] equip-core after 0131 gate; "
                                                    + piv_note
                                                    + (
                                                        "; continue_on_timeout=1"
                                                        if not piv_ok
                                                        else ""
                                                    )
                                                )
                                                # 0131 刚确认后再留一拍，降低同帧 drain 竞态
                                                post_piv_ms = max(
                                                    0,
                                                    _env_int(
                                                        "USDOF_GAME_ENTER_SERIA_AFTER_0131_PIV_BEFORE_USERINFO_MS",
                                                        80,
                                                    ),
                                                )
                                                if post_piv_ms > 0:
                                                    time.sleep(post_piv_ms / 1000.0)
                                        if equip_rest:
                                            self._send_enter_seria_delayed_timed(
                                                client,
                                                equip_rest,
                                                connection_id=prefix,
                                                inter_ms=inter_ms,
                                                log_tag="enter-seria-equip-after-0131-piv",
                                                session_state=session_state,
                                            )
                                            previous_response_packet = equip_rest[-1][0]
                                except OSError:
                                    return
                                # timed burst 完成：标记进房 settled，保证回页前无残留 wait
                                try:
                                    session_state.enter_seria_in_progress = False
                                    session_state.enter_seria_settled = True
                                    session_state.enter_seria_post_settle_000e_sent = False
                                except Exception:
                                    pass
                                if p1_piv_followup_queue and _env_bool(
                                    "USDOF_GAME_ENTER_SERIA_DROP_CHARSELECT_P1_LEFTOVER",
                                    True,
                                ):
                                    self.log(
                                        "[Game/EnterSeria] post-burst drop leftover P1 "
                                        f"count={len(p1_piv_followup_queue)} "
                                        f"notes=[{_format_p1_queue_notes(p1_piv_followup_queue)}]"
                                    )
                                    p1_piv_followup_queue = []
                                    p1_waiting_for_piv_after = None
                                self.log(
                                    "[Game/EnterSeria] enter delayed burst complete "
                                    f"count={len(delayed_response_packets)} "
                                    f"pre={len(pre_equip)} equip_rest={len(equip_rest)} "
                                    f"gate_0131={int(gate_0131)} settled=1"
                                )
                                # 兜底 000E（短 delay、不 mark 终态）；008C 再做 U1+000E 完整补刷
                                try:
                                    self._maybe_send_post_settle_equip_refresh(
                                        client,
                                        session_state,
                                        connection_id=prefix,
                                    )
                                except Exception as refresh_exc:
                                    self.log(
                                        "[Game/EnterSeria] post-settle equip refresh failed: "
                                        + repr(refresh_exc)
                                    )
                            else:
                                try:
                                    if previous_response_packet is not None:
                                        p1_waiting_for_piv_after = _packet_key(previous_response_packet)
                                    else:
                                        p1_waiting_for_piv_after = (0, 0x0131)
                                except Exception:
                                    p1_waiting_for_piv_after = (0, 0x0131)
                                queued = [
                                    (
                                        packet,
                                        (
                                            note
                                            if "enter-seria-piv-burst-rest" in note
                                            else ("enter-seria-piv-burst-rest; " + note)
                                        ),
                                    )
                                    for packet, note in delayed_response_packets
                                ]
                                p1_piv_followup_queue = queued + p1_piv_followup_queue
                                wait_key = p1_waiting_for_piv_after
                                self.log(
                                    "[Game/P1Gate] enter-seria piv-step wait for 0131-or-previous PIV "
                                    f"wait=flag={wait_key[0]}/opcode=0x{wait_key[1]:04x}; "
                                    f"queued_delayed={len(queued)} (burst-on-0131-piv, has-0324): "
                                    + ", ".join(note for _packet, note in queued)
                                )
                        else:
                            if delay_ms > 0:
                                time.sleep(delay_ms / 1000.0)
                            for index, (response, delayed_note) in enumerate(delayed_response_packets):
                                try:
                                    self._log_game_sequence(
                                        direction="S->C delayed",
                                        connection_id=prefix,
                                        packet=response,
                                        compact_prefix=(
                                            _s2c_plain_wire_enabled()
                                            and _plain_wire_header_mode() == "13"
                                        ),
                                        semantic=opcode_meaning(*_packet_key(response)),
                                        note="delayed response index=" + str(index) + "; " + delayed_note,
                                    )
                                    if _s2c_plain_wire_enabled():
                                        self._log_plain_wire_packet("send-delayed", response)
                                    send_raid_packet(self, session_state, client, response)
                                    if self.diagnostic_trace and self.protocol_traffic_log:
                                        self.log(
                                            "[Game/P1Gate] delayed followup sent index="
                                            + str(index)
                                            + " "
                                            + delayed_note
                                            + " bytes="
                                            + str(len(response))
                                            + " hex="
                                            + response.hex(" ")
                                        )
                                except OSError as exc:
                                    self.log("[Game/P1Gate] delayed followup send failed: " + str(exc) + "; closing client handler")
                                    return
                                if index + 1 < len(delayed_response_packets):
                                    next_pkt, next_note = delayed_response_packets[index + 1]
                                    gap_ms = _enter_seria_followup_gap_ms(
                                        response,
                                        delayed_note,
                                        next_pkt,
                                        next_note,
                                        inter_ms,
                                    )
                                    if gap_ms > 0:
                                        if gap_ms > inter_ms and (
                                            "enter-seria" in (delayed_note or "")
                                            or "enter-seria" in (next_note or "")
                                            or "userinfo" in (delayed_note or "").lower()
                                            or "000e" in (next_note or "").lower()
                                        ):
                                            self.log(
                                                "[Game/P1Gate] delayed equip-order gap "
                                                f"{gap_ms}ms before next "
                                                f"({_packet_key_text(next_pkt)}; {next_note[:80]})"
                                            )
                                        time.sleep(gap_ms / 1000.0)
                            if enter_seria_after_0131:
                                try:
                                    session_state.enter_seria_in_progress = False
                                    session_state.enter_seria_settled = True
                                except Exception:
                                    pass

                    if close_after_packet:
                        # Close this Game TCP (Exit Game or Channel-leave after 0x057F).
                        delay_ms = max(0, _env_int("USDOF_GAME_EXIT_CLOSE_DELAY_MS", 0))
                        # Channel-leave: prefer short/zero delay so Channel TCP reentry wins.
                        is_channel_leave_close = (
                            "; CHANNEL-LEAVE" in decision_note
                            or bool(getattr(session_state, "channel_leave_close_after_settle", False))
                        )
                        if is_channel_leave_close:
                            delay_ms = max(0, _env_int("USDOF_GAME_CHANNEL_LEAVE_CLOSE_DELAY_MS", 0))
                            session_state.channel_leave_close_after_settle = False
                        if delay_ms > 0:
                            self.log(
                                f"[Game] CMDPACKET_EXIT/0x0003 complete; "
                                f"closing Game TCP after {delay_ms}ms "
                                f"(channel_id={session_state.selected_channel_id})"
                            )
                            time.sleep(delay_ms / 1000.0)
                        else:
                            self.log(
                                "[Game] CMDPACKET_EXIT/0x0003 complete; closing Game TCP immediately "
                                f"(channel_id={session_state.selected_channel_id})"
                            )
                        return
