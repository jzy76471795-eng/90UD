"""进入赛利亚房间链路 01：SELECT_CHARACTER 后的最小 ACK。

文件命名规则说明：
- ``enter_seria_room`` 是同一条“选择角色 -> 进入赛利亚房间”链路的统一前缀。
- ``01`` 表示本文件只承接本链路第 1 个已确认动作：
  ``C->S flag1/opcode=0x0004 SELECT_CHARACTER`` 以及逆向确认后的
  ``S->C flag0/opcode=0x0004 status=0x64`` 最小 ACK。
- 后续若确认 ``S->C 0x000D`` item list、``S->C 0x0002`` 角色状态或
  town/room 入口包，必须新增同前缀递增编号文件，例如
  ``enter_seria_room_02_item_list.py``，不能继续堆进本文件。

逐包纪律：
- 本文件已发送的唯一 S->C 包是 ``flag0/opcode=0x0004`` ACK。
- 逆向依据是当前 sentinelfix EXE registry ``0x01681C1C | 0x0004 -> 0x01672570``；
  handler 只读取 1 个 ``u8 status``，且 ``status == 0x64`` 进入
  ``sub_1ABF8B0(...)`` 状态迁移路径。
- 不在这里猜测完整赛利亚房间初始化流。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import LOG_DIR
from game.chain_loader import load_chain
from game.character.exp_bonus_consumable import (
    build_exp_bonus_item_state_restore_packets,
)
from native_core_loader import plan_select_character
EnterSeriaRoomCreatePrimaryObject0131Service = load_chain('opcode0131-create_primary_object.py').EnterSeriaRoomCreatePrimaryObject0131Service
EnterSeriaRoomContext001DService = load_chain('opcode001d-room_context.py').EnterSeriaRoomContext001DService
EnterSeriaRoomBindTownObject0324Service = load_chain('opcode0324-bind_town_object.py').EnterSeriaRoomBindTownObject0324Service
EnterSeriaRoomContext00D3Service = load_chain('opcode00d3-room_context.py').EnterSeriaRoomContext00D3Service
EnterSeriaRoomMapTable0029Service = load_chain('opcode0029-map_table.py').EnterSeriaRoomMapTable0029Service
EnterSeriaRoomUserArea0017Service = load_chain('opcode0017-user_area.py').EnterSeriaRoomUserArea0017Service
EnterSeriaRoomAreaUsers0018Service = load_chain('opcode0018-area_users.py').EnterSeriaRoomAreaUsers0018Service
EnterSeriaRoomUserPosition0016Service = load_chain('opcode0016-user_position.py').EnterSeriaRoomUserPosition0016Service
EnterSeriaRoomUserinfoType1BindMain0002Service = load_chain('opcode0002-userinfo_type1.py').EnterSeriaRoomUserinfoType1BindMain0002Service
EnterSeriaRoomUserinfoType3BindMain0002Service = load_chain('opcode0002-userinfo_type3.py').EnterSeriaRoomUserinfoType3BindMain0002Service
EnterSeriaRoomUserinfoType0BindMain0002Service = load_chain('opcode0002-userinfo_type0.py').EnterSeriaRoomUserinfoType0BindMain0002Service
EnterSeriaRoomMapUpdate002BService = load_chain('opcode002b-map_update.py').EnterSeriaRoomMapUpdate002BService
EnterSeriaRoomGameOption00ADService = load_chain('opcode00ad-game_option.py').EnterSeriaRoomGameOption00ADService
EnterSeriaStartMapService = load_chain('flow-start_map_enter.py').EnterSeriaStartMapService
_tower_item_cleanup = load_chain("helper-death_tower_item_cleanup.py")


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


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw.strip())


class EnterSeriaRoomSelectCharacterAckService:
    """处理进入赛利亚房间链路第 1 步：选角绑定 + 最小 ACK。"""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.create_primary_object_chain = EnterSeriaRoomCreatePrimaryObject0131Service(server)
        self.room_context_chain = EnterSeriaRoomContext001DService(server)
        self.bind_town_object_chain = EnterSeriaRoomBindTownObject0324Service(server)
        self.context_00d3_chain = EnterSeriaRoomContext00D3Service(server)
        self.map_table_0029_chain = EnterSeriaRoomMapTable0029Service(server)
        self.user_area_0017_chain = EnterSeriaRoomUserArea0017Service(server)
        self.area_users_0018_chain = EnterSeriaRoomAreaUsers0018Service(server)
        self.user_position_0016_chain = EnterSeriaRoomUserPosition0016Service(server)
        self.userinfo_type1_0002_chain = EnterSeriaRoomUserinfoType1BindMain0002Service(server)
        self.userinfo_type3_0002_chain = EnterSeriaRoomUserinfoType3BindMain0002Service(server)
        self.userinfo_type0_0002_chain = EnterSeriaRoomUserinfoType0BindMain0002Service(server)
        self.map_update_002b_chain = EnterSeriaRoomMapUpdate002BService(server)
        self.game_option_00ad_chain = EnterSeriaRoomGameOption00ADService(server)
        self.start_map_chain = EnterSeriaStartMapService(server)

    def _queue_auction_service_notice(self) -> None:
        # Frozen client S0/00B7 reader 01672C90 consumes exactly one u8;
        # 027BAFB0 sets 042BB839. The auction click handler at 027CA0DA
        # silently returns while that byte is zero. Queue after actor setup
        # on this connection; do not bypass the existing entry delay/owner gate.
        self.server.queue_delayed_response_packets(
            [
                (
                    self.server._codec_response(0, 0x00B7, b"\x01"),
                    "enter-seria-after-0131; auction service notice S0/00B7 enabled=1",
                )
            ]
        )

    def _maybe_wait_pre_reply_debug_gate(
        self,
        *,
        requested_slot: int,
        resolved_slot: int,
        selected: Any,
    ) -> str:
        """收到 SELECT_CHARACTER 后、发送 S->C 回包前的本地调试闸门。

        维护说明：
        - 这个闸门只为 x32dbg 动态验证使用，默认关闭。
        - 打开后，服务端先写 ``ready.json``，再等待 ``release.flag`` 出现。
          这样可以等客户端真实发出 ``C->S 0x0004`` 后再 attach x32dbg，
          种好 ``0x0131/0x0324/0x0004`` 接收链断点，再放行服务端回包。
        - 不要用它抓按钮点击到 C->S 的 UI/send 路径；那条路径在服务端收到
          请求前已经执行完。本闸门只保证不会漏服务端回包后的 S->C handler。
        """

        if not _env_bool("USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_WAIT", False):
            return ""

        ready_path = _path_from_env(
            "USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_READY_FILE",
            LOG_DIR / "select_character_pre_reply_ready.json",
        )
        release_path = _path_from_env(
            "USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_RELEASE_FILE",
            LOG_DIR / "select_character_pre_reply_release.flag",
        )
        timeout_ms = max(0, _env_int("USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_TIMEOUT_MS", 120000))
        poll_seconds = max(0.05, _env_int("USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_POLL_MS", 100) / 1000.0)
        clear_release = _env_bool("USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_CLEAR_RELEASE", True)
        continue_on_timeout = _env_bool("USDOF_GAME_SELECT_CHARACTER_PRE_REPLY_CONTINUE_ON_TIMEOUT", True)
        nonce = f"{int(time.time() * 1000)}-{os.getpid()}"
        payload = {
            "nonce": nonce,
            "stage": "select-character-pre-reply",
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "requested_slot": requested_slot,
            "resolved_slot": resolved_slot,
            "character_id": int(getattr(selected, "character_id", 0)),
            "name": str(getattr(selected, "display_name", "")),
            "job": int(getattr(selected, "job", 0)),
            "level": int(getattr(selected, "level", 0)),
            "town_id": int(getattr(selected, "town_id", 0)),
            "area_id": int(getattr(selected, "area_id", 0)),
            "pos_x": int(getattr(selected, "pos_x", 0)),
            "pos_y": int(getattr(selected, "pos_y", 0)),
            "release_file": str(release_path),
            "timeout_ms": timeout_ms,
            "note": (
                "service has received C->S flag1/opcode0x0004 and is waiting before "
                "S->C 0x0131/0x0324/0x0004 replies"
            ),
        }

        try:
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            release_path.parent.mkdir(parents=True, exist_ok=True)
            if clear_release and release_path.exists():
                release_path.unlink()
            ready_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            return (
                " pre_reply_debug_gate write_failed="
                f"{type(exc).__name__}:{exc} ready={ready_path} release={release_path}"
            )

        self.server.log(
            "[Game/EnterSeriaGate] SELECT_CHARACTER pre-reply wait "
            f"nonce={nonce} ready={ready_path} release={release_path} timeout_ms={timeout_ms}"
        )
        deadline = time.time() + timeout_ms / 1000.0
        while True:
            if release_path.exists():
                try:
                    release_text = release_path.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    release_text = ""
                self.server.log(
                    "[Game/EnterSeriaGate] SELECT_CHARACTER pre-reply released "
                    f"nonce={nonce} release={release_path}"
                )
                return (
                    " pre_reply_debug_gate released "
                    f"nonce={nonce} ready={ready_path} release={release_path}"
                    f"{' release_note=' + release_text[:120] if release_text else ''}"
                )
            if timeout_ms == 0 or time.time() >= deadline:
                note = (
                    " pre_reply_debug_gate timeout "
                    f"nonce={nonce} timeout_ms={timeout_ms} ready={ready_path} release={release_path}"
                )
                self.server.log("[Game/EnterSeriaGate] SELECT_CHARACTER" + note)
                if continue_on_timeout:
                    return note + " continue_on_timeout=1"
                while not release_path.exists():
                    time.sleep(poll_seconds)
                return note + " late_release_after_timeout"
            time.sleep(poll_seconds)

    @staticmethod
    def select_character_by_slot(characters: list[Any], requested_slot: int) -> Any | None:
        """C1/0004 carries the index in the emitted roster, not its saved slot.

        Native 02B8BC30 sends the same vector index consumed by 02B76280.
        Saved slots can contain gaps after deleting or moving characters.
        """
        if 0 <= int(requested_slot) < len(characters):
            return characters[int(requested_slot)]
        return None

    def build_select_character_ack_0004_packet(self) -> tuple[bytes | None, str]:
        """构造选择角色后的最小进入游戏 ACK。

        中文维护说明：
        - 不要把这里的 ``S->C flag0/opcode=0x0004`` 和角色选择页打开用的
          ``S->C flag1/opcode=0x0004`` 混在一起。后者由 charselect-open 链路发送，
          目标 handler 是 ``0x01603250``。
        - 当前 sentinelfix EXE 静态 registry 显示另一套 flag0/active registry：
          ``0x01681C1C | 0x0004 -> 0x01672570``。该 handler 只读取 1 个 u8
          状态字节；当状态为 ``0x64`` 时会进入 ``sub_1ABF8B0(...)`` 的状态迁移
          路径。因此这里默认只发 1 字节 ``0x64``，后续房间/城镇初始化包必须另起
          ``enter_seria_room_02_*`` 等编号文件追加。
        - 所有字段均保留环境变量 A/B 开关；动态验证若证明状态或编码模式不同，
          只改默认值或外部覆盖，不要删除本注释里的逆向边界。
        """

        if not _env_bool("USDOF_GAME_SELECT_CHARACTER_SEND_ACK_0004", True):
            return None, (
                "SELECT_CHARACTER enter-game ACK S->C flag0/opcode=0x0004 suppressed by "
                "USDOF_GAME_SELECT_CHARACTER_SEND_ACK_0004=0"
            )

        flag = _env_int("USDOF_GAME_SELECT_CHARACTER_ACK_0004_FLAG", 0) & 0xFF
        body = _env_hex_bytes("USDOF_GAME_SELECT_CHARACTER_ACK_0004_BODY")
        if body is None:
            status = _env_int("USDOF_GAME_SELECT_CHARACTER_ACK_0004_STATUS", 0x64) & 0xFF
            body = bytes([status])
            body_note = f"status=0x{status:02x}"
        else:
            body_note = f"body_override={body.hex(' ')}"

        mode = os.environ.get("USDOF_GAME_SELECT_CHARACTER_ACK_0004_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0004, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0004, body)

        return packet, (
            "queued SELECT_CHARACTER enter-game ACK "
            f"S->C flag{flag}/opcode=0x0004 mode={mode} {body_note} "
            f"plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/flow-select_character.py; "
            "reverse basis: current sentinelfix registry 0x01681C1C maps flag0/opcode0x0004 "
            "to 0x01672570, which reads one u8 and status 0x64 enters sub_1ABF8B0 path"
        )

    def handle_select_character(
        self,
        body: bytes,
        *,
        session: Any,
        native_branch: bool = False,
    ) -> tuple[list[bytes], str]:
        """处理 C->S flag1/opcode 0x0004 SELECT_CHARACTER。"""
        # Path A (friend task5-fix): START map 262B enter-Seria.
        # Enabled by USDOF_GAME_ENTER_SERIA_START_MAP=1; default off for rolepage baseline.
        start_map_result = self.start_map_chain.handle_select_character(
            body,
            session=session,
            select_character_by_slot=self.select_character_by_slot,
            native_branch=native_branch,
        )
        if start_map_result is not None:
            packets, note = start_map_result
            if packets and int(getattr(session, "selected_character_id", 0) or 0) > 0:
                self._queue_auction_service_notice()
                note += "; auction_service_notice=queued"
            return packets, note

        requested_slot = 0
        parse_source = "default-empty-body"
        native_selected_index = -1
        native_selection_kind = 0
        native_plan_note = ""
        if not native_branch and len(body) >= 2:
            # 当前加密链路下 decode.plain 已是命令体；保留 reserved3 兼容旧 fixture。
            candidates = [body]
            if len(body) > 3:
                candidates.append(body[3:])
            for index, candidate in enumerate(candidates):
                if len(candidate) >= 2:
                    requested_slot = int.from_bytes(candidate[:2], "little") & 0xFFFF
                    parse_source = "body" if index == 0 else "body_minus_reserved3"
                    break

        repo = self.server._repo()
        # Boot normally backfills all records. Repeat on explicit selection so
        # an imported/created record cannot reach the START_MAP snapshot before
        # its PVF-derived default finished flags are persisted.
        try:
            from game.quest.default_unlocks import apply_default_unlocks

            for record in repo.list_by_account(self.server.account_id):
                apply_default_unlocks(repo, int(record.character_id))
        except Exception:
            pass
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
        if characters:
            selected = (
                characters[native_selected_index]
                if native_branch and 0 <= native_selected_index < len(characters)
                else self.select_character_by_slot(characters, requested_slot)
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
                # Drop paths still apply the prewarmed identity gate; keep
                # character entry available if a local cache is rebuilding.
                wish_note = "wish_hydrate_error=%s" % type(wish_exc).__name__
            equipment_count = len(selected.equipped_entries or ())
            gate_note = self._maybe_wait_pre_reply_debug_gate(
                requested_slot=requested_slot,
                resolved_slot=resolved_slot,
                selected=selected,
            )
            packets: list[bytes] = []
            delayed_packets: list[tuple[bytes, str]] = []
            split_after_0131 = _env_bool("USDOF_GAME_ENTER_SERIA_SPLIT_AFTER_0131", False)
            primary_packet, primary_note = self.create_primary_object_chain.build_packet(selected)
            if primary_packet is not None:
                packets.append(primary_packet)
            room_context_packet, room_context_note = self.room_context_chain.build_packet(selected)
            if room_context_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            room_context_packet,
                            (
                                "enter-seria-after-0131 delayed 0x001D room-context; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + room_context_note
                            ),
                        )
                    )
                else:
                    packets.append(room_context_packet)
            context_00d3_packet, context_00d3_note = self.context_00d3_chain.build_packet(selected)
            if context_00d3_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            context_00d3_packet,
                            (
                                "enter-seria-after-0131 delayed 0x00D3 context; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + context_00d3_note
                            ),
                        )
                    )
                else:
                    packets.append(context_00d3_packet)
            map_table_packet, map_table_note = self.map_table_0029_chain.build_packet(selected)
            if map_table_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            map_table_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0029 map-table; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + map_table_note
                            ),
                        )
                    )
                else:
                    packets.append(map_table_packet)
            map_update_002b_packet, map_update_002b_note = self.map_update_002b_chain.build_packet(selected)
            if map_update_002b_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            map_update_002b_packet,
                            (
                                'enter-seria-after-0131 delayed 0x002B map-update; '
                                '0x0131 already sent before x32dbg attach/release gate; '
                                + map_update_002b_note
                            ),
                        )
                    )
                else:
                    packets.append(map_update_002b_packet)


            userinfo_type0_packet, userinfo_type0_note = self.userinfo_type0_0002_chain.build_packet(selected)
            if userinfo_type0_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            userinfo_type0_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0002 USERINFO type0 bind-main; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + userinfo_type0_note
                            ),
                        )
                    )
                else:
                    packets.append(userinfo_type0_packet)
            userinfo_type3_packet, userinfo_type3_note = self.userinfo_type3_0002_chain.build_packet(selected)
            if userinfo_type3_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            userinfo_type3_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0002 USERINFO type3 bind-main; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + userinfo_type3_note
                            ),
                        )
                    )
                else:
                    packets.append(userinfo_type3_packet)
            userinfo_type1_packet, userinfo_type1_note = self.userinfo_type1_0002_chain.build_packet(selected)
            if userinfo_type1_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            userinfo_type1_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0002 USERINFO type1 bind-main; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + userinfo_type1_note
                            ),
                        )
                    )
                else:
                    packets.append(userinfo_type1_packet)

            exp_item_state_note = "EXP item state restore skip"
            try:
                exp_item_state_packets, exp_item_state_note = (
                    build_exp_bonus_item_state_restore_packets(
                        self.server,
                        repo,
                        int(selected.character_id),
                    )
                )
                for exp_item_state_packet in exp_item_state_packets:
                    if split_after_0131 and primary_packet is not None:
                        delayed_packets.append(
                            (
                                exp_item_state_packet,
                                "enter-seria-after-0131 delayed %s after_USERINFO1"
                                % exp_item_state_note,
                            )
                        )
                    else:
                        packets.append(exp_item_state_packet)
            except Exception as exp_item_state_exc:
                exp_item_state_note = "EXP item state restore fail=%s:%s" % (
                    type(exp_item_state_exc).__name__,
                    exp_item_state_exc,
                )

            wish_snapshot_note = "S0/050A wish_items_snapshot skip=hydrate_failed"
            if wish_hydrated:
                try:
                    from game.drop.wish_service import build_wish_items_snapshot_packet

                    wish_snapshot_packet, wish_snapshot_note = (
                        build_wish_items_snapshot_packet(self.server, wish_ids)
                    )
                    if split_after_0131 and primary_packet is not None:
                        delayed_packets.append(
                            (
                                wish_snapshot_packet,
                                "enter-seria-after-0131 delayed %s after_USERINFO1"
                                % wish_snapshot_note,
                            )
                        )
                    else:
                        packets.append(wish_snapshot_packet)
                except Exception as wish_snapshot_exc:
                    wish_snapshot_note = "S0/050A wish_items_snapshot fail=%s:%s" % (
                        type(wish_snapshot_exc).__name__,
                        wish_snapshot_exc,
                    )

            user_area_packet, user_area_note = self.user_area_0017_chain.build_packet(selected)
            if user_area_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            user_area_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0017 USER_AREA; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + user_area_note
                            ),
                        )
                    )
                else:
                    packets.append(user_area_packet)
            user_position_packet, user_position_note = self.user_position_0016_chain.build_packet(selected)
            if user_position_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            user_position_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0016 USER_POSITION; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + user_position_note
                            ),
                        )
                    )
                else:
                    packets.append(user_position_packet)
            area_users_packet, area_users_note = self.area_users_0018_chain.build_packet(selected)
            if area_users_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            area_users_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0018 AREA_USERS; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + area_users_note
                            ),
                        )
                    )
                else:
                    packets.append(area_users_packet)

            bind_packet, bind_note = self.bind_town_object_chain.build_packet(selected)
            if bind_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            bind_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0324 bind; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + bind_note
                            ),
                        )
                    )
                else:
                    packets.append(bind_packet)

            # 00AD GAME_OPTION：笔记《进赛丽亚.md》明示的"必要链路"项。
            # 客户端 0x00AD handler option_00ad_dispatch_loop_sub_1877260 遍历 79 项 u16，
            # case 74 -> option_setter_case_74_sub_1AB4F70_f4213 写 f4213 = pending-area enabled。
            # 全 0 00AD 会把 f4213 清成 0，损坏 pending-area 状态，因此默认关闭。
            # 开启：USDOF_GAME_ENTER_SERIA_SEND_00AD_GAME_OPTION=1
            game_option_packet, game_option_note = self.game_option_00ad_chain.build_packet(selected)
            if game_option_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            game_option_packet,
                            (
                                "enter-seria-after-0131 delayed 0x00AD GAME_OPTION; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + game_option_note
                            ),
                        )
                    )
                else:
                    packets.append(game_option_packet)

            ack_packet, ack_note = self.build_select_character_ack_0004_packet()
            if ack_packet is not None:
                if split_after_0131 and primary_packet is not None:
                    delayed_packets.append(
                        (
                            ack_packet,
                            (
                                "enter-seria-after-0131 delayed 0x0004 ACK; "
                                "0x0131 already sent before x32dbg attach/release gate; "
                                + ack_note
                            ),
                        )
                    )
                else:
                    packets.append(ack_packet)
            split_note = ""
            if split_after_0131:
                if primary_packet is not None and delayed_packets:
                    self.server.queue_delayed_response_packets(delayed_packets)
                    split_note = (
                        f"; split_after_0131=1 immediate_packets={len(packets)} "
                        f"delayed_packets={len(delayed_packets)} "
                        "delayed_marker=enter-seria-after-0131"
                    )
                elif primary_packet is None:
                    split_note = "; split_after_0131 requested but 0x0131 primary packet is suppressed"
            self._queue_auction_service_notice()
            split_note += "; auction_service_notice=queued"
            return packets, (
                "SELECT_CHARACTER bound session "
                f"requested_slot={requested_slot} resolved_slot={resolved_slot} source={parse_source} "
                f"-> character_id={selected.character_id} name={selected.display_name!r} "
                f"job={selected.job} level={selected.level} town_id={selected.town_id} area_id={selected.area_id} "
                f"pos=({selected.pos_x},{selected.pos_y}) direction={selected.direction} "
                f"area_state={selected.area_state} equipment_count={equipment_count}; "
                f"{fallback_note}{native_plan_note}; {tower_item_cleanup_note}; {wish_note}; enter_game_pending=1; enter-town packet stream not implemented yet"
                f"{';' + gate_note if gate_note else ''}{split_note}; "
                f"{primary_note}; {room_context_note}; {context_00d3_note}; {map_table_note}; {map_update_002b_note}; {userinfo_type0_note}; {userinfo_type3_note}; {userinfo_type1_note}; {exp_item_state_note}; {wish_snapshot_note}; {user_area_note}; {area_users_note}; {bind_note}; {game_option_note}; {ack_note}"
            )

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
            "SELECT_CHARACTER observed but account has no DB characters; "
            f"requested_slot={requested_slot} source={parse_source}; enter_game_pending=1; "
            "enter-town packet stream not implemented yet; "
            "chain_file=game/chains/flow-select_character.py"
            f"{native_plan_note}"
        )
