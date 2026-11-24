"""返回选择角色 (C->S 0x0007 RETURN_SELECT_CHARACTER)

从城镇/世界回角色选择页。

IDA Stage3:
- S2C success handler ``op7_return_select_character_teardown`` @ ``0x16080B0``
  在 body 成功字节非 0 时执行 ``session_teardown_1ABE3E0``，本地拆城镇会话并
  **自己创建 type=12 选角 UI**；**不会**自己拉角色列表。
- 仅 ACK 会留下半拆状态；体感常被误报为「人数已满」（尤其 raw ACK 解不出 a3）。

服务端默认回包（对齐 IDA + S4A12 语义，**禁止**再叠冷启动开窗包）::

    S1/0007 body=01 (codec) → teardown + 客户端本地建 type=12
    （停）等客户端 PIV 0007
    → S0/0002 USERINFO roster（+ followups 分片）
    可选：客户端再发 C1/0008 时走既有 GET_USERINFO 路径

硬规则：
- 0007 success ACK **必须 codec**（opcode%14==7 → Blowfish codec7）。
  raw 单字节 body=01 客户端解不出 a3 成功字节 → teardown 不完整，
  体感常被误报为「频道人数已满」。
- **禁止** 再发 S1/0001 + S1/0004 开窗：
  op7 已建 type=12；再叠 0001/0004 = 二次开窗，日志可见 PIV 0007→0001→0004
  后 USERINFO 仍到，但体感=**黑屏可盲点**（roster 在黑层下可点进赛丽亚）。
- **禁止** 0007 ACK 与 USERINFO 同 TCP burst：
  等 C->S PIV 0007 后再发 roster（game_server 门闩），避免 teardown 未完成就灌表。
- 对照 ``servers4a12``：ACK + 主动 character list；Stage3 多一道 PIV 门闩。
- 旧 A/B ``OPEN_BEFORE_USERINFO=1`` 仅调试用，日常默认 OFF。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.character.native_loadout_bridge import abort_native_loadout_plan

_room_state = load_chain("helper-dungeon_room_state.py")
reset_run_room_state = _room_state.reset_run_room_state


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_hex_bytes(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    normalized = "".join(ch for ch in normalized if ch not in " \t\r\n:_-")
    if len(normalized) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits: {raw!r}")
    return bytes.fromhex(normalized)


class ReturnSelectCharacterService:
    """C->S 0x0007：回角色选择页完整链。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_return_select_character(
        self,
        body: bytes = b"",
        *,
        session: Any | None = None,
    ) -> tuple[list[bytes], str]:
        """Build S->C: 默认仅 0007 ACK + USERINFO；开窗包默认关闭。"""
        login_session = session
        session = self.server._session_state(session)
        prev_cid = getattr(session, "selected_character_id", None)
        prev_name = getattr(session, "selected_character_name", None)
        was_in_run = bool(getattr(session, "in_dungeon_run", False))
        native_loadout_note = ""
        try:
            native_loadout_note = abort_native_loadout_plan(
                session,
                reason="return_select_character",
            )
        except OSError as exc:
            native_loadout_note = (
                "native_loadout_abort_ack_write_fail:%s" % type(exc).__name__
            )

        self._clear_session_to_charselect(session)

        packets: list[bytes] = []
        notes: list[str] = []
        if native_loadout_note:
            notes.append(native_loadout_note)

        ack_packet, ack_note = self._build_0007_ack()
        packets.append(ack_packet)
        notes.append(ack_note)

        # 默认 OFF：op7 已建 type=12；再发 0001/0004 = 二次开窗黑屏可盲点。
        # A/B 调试才打开 OPEN_BEFORE。
        open_before = _env_bool("USDOF_GAME_RETURN_SELECT_OPEN_BEFORE_USERINFO", False)
        if open_before:
            if _env_bool("USDOF_GAME_RETURN_SELECT_SEND_0001", True):
                packet, note = self.server._login_state_init_0001_packet(session=login_session)
                packets.append(packet)
                notes.append(note)
            if _env_bool("USDOF_GAME_RETURN_SELECT_SEND_0004", True):
                packet, note = self.server._charselect_open_0004_packet()
                packets.append(packet)
                notes.append(note)
            notes.append("open_before A/B ON (may black-screen; op7 already builds type=12)")
        else:
            notes.append("no S1/0001+0004 (op7 builds type=12 UI)")

        # 默认 ON：对齐 S4A12 主动 character list；game_server 在 PIV 0007 后放行，
        # 避免 ACK+USERINFO 同 burst 抢在 teardown 完成前。
        send_userinfo = _env_bool("USDOF_GAME_RETURN_SELECT_SEND_USERINFO", True)
        if send_userinfo:
            include_followups = _env_bool(
                "USDOF_GAME_RETURN_SELECT_USERINFO_FOLLOWUPS",
                True,
            )
            ui_packets, ui_note = self.server._db_userinfo_packet_stream(
                "return-select-character",
                include_roster=True,
                include_followups=include_followups,
            )
            packets.extend(ui_packets)
            notes.append(ui_note)
            notes.append("USERINFO queued for PIV-0007 gate (game_server)")
        else:
            notes.append("USERINFO not in chain; expect C1/0008 or empty roster")

        # 标记：本 TCP 上角色页由 return-select 重开（Exit 判别/01BA 路径用）
        session.reentry_forced_charselect = True

        note = (
            "RETURN_SELECT_CHARACTER "
            f"prev_cid={prev_cid} prev_name={prev_name!r} was_in_run={int(was_in_run)} "
            f"open_before_userinfo={int(open_before)} "
            f"send_userinfo={int(send_userinfo)} "
            f"packets={len(packets)} "
            f"chain=[{'; '.join(notes)}]; "
            "chain_file=game/chains/opcode0007-return_select_character.py"
        )
        return packets, note

    def _build_0007_ack(self) -> tuple[bytes, str]:
        body = _env_hex_bytes("USDOF_GAME_RETURN_SELECT_0007_BODY")
        if body is None:
            # IDA op7: a3=success byte；01=成功 teardown
            body = b"\x01"
        flag = 1
        raw_flag = os.environ.get("USDOF_GAME_RETURN_SELECT_0007_FLAG")
        if raw_flag is not None and str(raw_flag).strip():
            flag = int(str(raw_flag).strip(), 0) & 0xFF
        # 默认 codec：与 0005/0006/0003 SuccessAck 一致。
        # raw 会让 codec_index=7 的客户端解不出 a3=1 → 半拆 + 人数已满误报。
        mode = os.environ.get("USDOF_GAME_RETURN_SELECT_0007_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x0007, body)
            mode_note = "raw"
        else:
            packet = self.server._codec_response(flag, 0x0007, body)
            mode_note = "codec"
        return packet, (
            f"S1/0007 success ACK mode={mode_note} body={body.hex(' ')}"
        )

    @staticmethod
    def _clear_session_to_charselect(session: Any) -> None:
        """把会话从 in-game 收口到角色页，避免残留进图/副本态。"""
        if session is None:
            return
        clear_waiting_position = getattr(session, "clear_anton_waiting_position", None)
        if callable(clear_waiting_position):
            clear_waiting_position()
        reset_run_room_state(session)
        session.selected_character_id = None
        session.selected_slot = None
        session.selected_character_name = None
        session.dungeon_wish_item_ids = ()
        session.enter_game_pending = False
        session.enter_seria_in_progress = False
        session.enter_seria_settled = False
        session.enter_seria_equip_refreshed = False
        session.enter_seria_post_settle_000e_sent = False
        session.enter_seria_wish_snapshot_post_settle_sent = False
        session.quest_hud_blink_pending = False
        session.quest_hud_blink_armed = False
        session.last_select_character_at = None
        session.cur_town_id = None
        session.cur_area_id = None
        session.cur_pos_x = None
        session.cur_pos_y = None
        session.cur_direction = None
        session.cur_area_state = None
        session.active_quest_ids = []
        session.active_quest_pairs = []
        session.finished_quest_ids = []
        session.last_quest_id = None
        session.quest_connected = False
        session.quest_hydrated = False
        session.cur_dungeon_id = None
        session.cur_dungeon_difficulty = None
        session.last_quest_drop = None
        session.in_dungeon_select = False
        session.dungeon_select_active = False
        session.in_dungeon_run = False
        session.dungeon_weapon_avatar_render_pending = None
        session.dungeon_weapon_avatar_render_character_id = None
        session.dungeon_weapon_avatar_render_dungeon_id = None
        session.dungeon_weapon_avatar_render_map_index = None
        session.dungeon_weapon_avatar_render_run_generation = 0
        session.dungeon_weapon_avatar_render_sent = False
        session.selected_dungeon_id = None
        session.selected_difficulty = None
        session.special_dungeon_selector_player_uid = 0
        session.dungeon_special_selector_dungeon_id = 0
        session.dungeon_special_selector_progress_key = 0
        session.dungeon_special_selector_entry_option = 0
        session.dungeon_special_selector_highest_unlocked = 0
        session.dungeon_start_x = None
        session.dungeon_start_y = None
        session.dungeon_cell_x = None
        session.dungeon_cell_y = None
        session.dungeon_boss_x = None
        session.dungeon_boss_y = None
        session.dungeon_map_index = None
        session.dungeon_maze_index = None
        session.dungeon_seed = None
        session.dungeon_clear_phase = 0
        session.dungeon_run_kill_exp = 0
        session.dungeon_run_premium_exp = 0
        session.dungeon_run_growth_exp = 0
        session.dungeon_run_neo_exp = 0
        session.dungeon_run_fatigue_burn_exp = 0
        session.dungeon_run_started_monotonic = None
        session.dungeon_run_play_time_ms = None
        session.dungeon_run_best_time_ms = None
        session.dungeon_room_mons = []
        session.dungeon_room_first_seq = 0
        # 频道 leave 相关不应被 return-select 误 arm
        session.channel_move_settled = False
        session.channel_leave_early_ack_sent = False
        session.channel_leave_close_after_settle = False
        session.last_057f_profile = None
        session.last_057f_at = None
