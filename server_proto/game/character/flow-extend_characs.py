"""扩展角色槽/选角 UI 握手 (0x02A8/0x01BA/0x0355 + S->C builders)

从 ``game_server.py`` 拆出的角色相关补包链。通过 ``self.server`` 复用
GameServer 的 DB/codec/日志等基础设施，避免循环依赖。
"""
from __future__ import annotations

import os
from typing import Any

from game.character.payloads import CLIENT_CERA_DISPLAY_CAP


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


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return int(str(raw).strip(), 0)


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


class CharExtendCharacsService:
    """扩展角色槽/选角 UI 握手 (0x02A8/0x01BA/0x0355 + S->C builders)"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_load_extend_characs(self, body: bytes) -> tuple[list[bytes], str]:
        start_slot = int.from_bytes(body[:2], "little") if len(body) >= 2 else None
        ack_plain = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_ACK_BODY")
        if ack_plain is None:
            ack_plain = b"\x00\x00"
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_ACK_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(1, 0x02A8, ack_plain)
        elif mode in {"empty", "raw-empty"}:
            ack_plain = b""
            packet = self.server._raw_response(1, 0x02A8, ack_plain)
        else:
            mode = "codec"
            packet = self.server._codec_response(1, 0x02A8, ack_plain)
        return [packet], (
            "LOAD_EXTEND_CHARACS/0x02A8 observed "
            f"start_slot={'none' if start_slot is None else f'0x{start_slot:04x}'}; "
            f"sent mode={mode} empty extend-character ACK/count=0 plain={ack_plain.hex(' ')}"
        )


    def handle_load_extend_characs_finish(
        self,
        body: bytes,
        *,
        suppress_direct_response: bool = False,
    ) -> tuple[list[bytes], str]:
        if suppress_direct_response and _env_bool("USDOF_GAME_IGNORE_01BA_AFTER_CHARSELECT_0004", True):
            return [], (
                "LOAD_EXTEND_CHARACS_FINISH/0x01BA observed "
                f"plain_len={len(body)}; post-charselect-0004 empty 0x01BA ignored; "
                "reverse: current EXE sub_7CE1F0 builds C->S opcode 0x01BA with no payload"
            )

        if not _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SEND_01BA_ON_DIRECT_FINISH", True):
            return [], (
                "LOAD_EXTEND_CHARACS_FINISH/0x01BA observed "
                f"plain_len={len(body)}; direct 0x01BA response disabled; "
                "waiting for PIV echo of S->C 0x02A8 before USERINFO refresh"
            )

        packet, note = self.build_01ba_packet()
        return [packet], (
            "LOAD_EXTEND_CHARACS_FINISH/0x01BA observed "
            f"plain_len={len(body)}; direct-finish response: {note}"
        )


    def handle_extend_ready_0355(self, body: bytes) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SEND_0355_RESPONSE_ON_0355", False):
            return [], (
                "EXTEND_READY/0x0355 observed "
                f"plain_len={len(body)}; direct 0x0355 response disabled"
            )

        flag = _env_optional_int("USDOF_GAME_0355_RESPONSE_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        response_body = _env_hex_bytes("USDOF_GAME_0355_RESPONSE_BODY")
        if response_body is None:
            response_body = b""
        mode = os.environ.get("USDOF_GAME_0355_RESPONSE_MODE", "raw").strip().lower()
        if mode in {"codec", "encoded"}:
            packet = self.server._codec_response(flag, 0x0355, response_body)
        else:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0355, response_body)
        return [packet], (
            "EXTEND_READY/0x0355 observed "
            f"plain_len={len(body)}; sent flag{flag}/opcode=0x0355 "
            f"mode={mode} plain={response_body.hex(' ')}"
        )


    def build_01ba_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_01BA_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_01BA_BODY")
        if body is None:
            # Current USDOF 90 active flag0/opcode 0x01BA receive path reads
            # u8 + u32 + u16 at 0x0190ABC0 and stores them into the
            # select-character extension state object (+4/+8/+12).  The old
            # local baseline used leading 1, which appears to retrigger the
            # expanded-slot animation on every fresh character-select entry.
            # Default to 0 so the 0x01BA handshake/PIV gate still exists
            # without asserting the extension animation state.  Set
            # USDOF_GAME_LOAD_EXTEND_CHARACS_SUPPRESS_ANIMATION=0 to restore
            # the historical leading 1 for A/B tests.
            status = (
                0
                if _env_bool("USDOF_GAME_LOAD_EXTEND_CHARACS_SUPPRESS_ANIMATION", True)
                else 1
            )
            body = bytes([status]) + b"\x00\x00\x00\x00\x00\x00"
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01BA_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x01BA, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x01BA, body)
        return packet, (
            "LOAD_EXTEND_CHARACS post-PIV sent "
            f"flag{flag}/opcode=0x01BA mode={mode} plain={body.hex(' ')}"
        )


    def build_01f9_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_01F9_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_01F9_BODY")
        if body is None:
            # Active USDOF90 handler 0x016FC2D0 reads:
            #   u8 list_type, u32 account/user context, u8 entry_count
            # With entry_count=0 it skips per-character reads and refreshes the
            # extension-list manager.  The launcher UserId is the proven DFO
            # launch identity; SQLite account_id is only our local DB key.
            account_context = int(
                os.environ.get(
                    "USDOF_GAME_LOAD_EXTEND_CHARACS_01F9_ACCOUNT_ID",
                    os.environ.get("USDOF_LAUNCHER_ACCOUNT_ID", str(self.server.launcher_account_id)),
                ),
                0,
            ) & 0xFFFFFFFF
            list_type = int(os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01F9_LIST_TYPE", "8"), 0) & 0xFF
            body = bytes([list_type]) + account_context.to_bytes(4, "little") + b"\x00"
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01F9_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x01F9, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x01F9, body)
        return packet, (
            "LOAD_EXTEND_CHARACS post-01BA-PIV sent "
            f"flag{flag}/opcode=0x01F9 mode={mode} plain={body.hex(' ')}"
        )


    def build_01fa_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_01FA_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_01FA_BODY")
        if body is None:
            # Active USDOF90 handler 0x016FB230 reads no packet body.  It clears
            # window/state 0x389, creates the select UI manager via 0x016FEC00,
            # then jumps to 0x016FB160 where multiple character-select UI
            # widgets are opened/refreshed.
            body = b""
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FA_MODE", "raw").strip().lower()
        if mode in {"codec", "encoded"}:
            packet = self.server._codec_response(flag, 0x01FA, body)
        else:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x01FA, body)
        return packet, (
            "LOAD_EXTEND_CHARACS UI-open sent "
            f"flag{flag}/opcode=0x01FA mode={mode} plain={body.hex(' ')}"
        )


    def build_01fb_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_01FB_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_01FB_BODY")
        if body is None:
            # Active USDOF90 handler 0x016FEC80 reads one u8, then calls
            # 0x016F9F80(status).  Reverse result: status 1 and 3 run visible
            # character-select UI paths; 2 is a narrower switch/close path.
            status = int(os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FB_STATUS", "1"), 0) & 0xFF
            body = bytes([status])
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FB_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x01FB, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x01FB, body)
        return packet, (
            "LOAD_EXTEND_CHARACS UI-status sent "
            f"flag{flag}/opcode=0x01FB mode={mode} plain={body.hex(' ')}"
        )


    def build_01fc_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_01FC_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_01FC_BODY")
        if body is None:
            # Active USDOF90 handler 0x016F9BB0 reads one u8 and one u16.
            # The u16 is then passed to the window-0x12 object update path.
            status = int(os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FC_U8", "0"), 0) & 0xFF
            value = int(os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FC_VALUE", "0"), 0) & 0xFFFF
            body = bytes([status]) + value.to_bytes(2, "little")
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_01FC_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x01FC, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x01FC, body)
        return packet, (
            "LOAD_EXTEND_CHARACS UI-progress sent "
            f"flag{flag}/opcode=0x01FC mode={mode} plain={body.hex(' ')}"
        )


    def build_charselect_open_0004_body(self, *, account_cera: int | None = None) -> bytes:
        success = int(os.environ.get("USDOF_GAME_CHARSELECT_0004_SUCCESS", "1"), 0) & 0xFF
        window_id = int(os.environ.get("USDOF_GAME_CHARSELECT_0004_WINDOW_ID", "12"), 0) & 0xFFFF
        window_param = int(os.environ.get("USDOF_GAME_CHARSELECT_0004_WINDOW_PARAM", "0"), 0) & 0xFFFFFFFF
        # IDA: sub_1603250 passes this byte to sub_2BF6980, which looks it up
        # in the client's town/town.lst table. USDOF Script.pvf has no key 0;
        # key 1 is SilverCrown.twn and is the first valid entry.
        n2 = int(os.environ.get("USDOF_GAME_CHARSELECT_0004_N2", "1"), 0) & 0xFF
        if account_cera is None:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
            account_cera = self.server._repo().get_account_cera(account_id)
        account_cera_u32 = min(
            max(0, int(account_cera)),
            CLIENT_CERA_DISPLAY_CAP,
        )

        body = bytearray()
        body.append(success)
        body += (0).to_bytes(4, "little")  # v141
        body += (0).to_bytes(4, "little")  # v153
        body += (0).to_bytes(2, "little")  # v152
        body += (0).to_bytes(2, "little")  # v149
        body += (0).to_bytes(2, "little")  # v148
        body += (0).to_bytes(2, "little")  # v147
        body.append(0)  # premium record count
        body += account_cera_u32.to_bytes(4, "little")

        body += window_id.to_bytes(2, "little")
        body += window_param.to_bytes(4, "little")
        for _ in range(29):
            body += (0xFFFF).to_bytes(2, "little")
            body += (0).to_bytes(4, "little")

        # sub_1603250 calls sub_2787610(dword_4189AC0) after the 30 window
        # pairs, and live x32dbg tracing showed it consumes 16 stream bytes
        # before the final scalar reads.
        body += b"\x00" * 16

        body.append(n2)
        body += (0).to_bytes(2, "little")  # v145
        body += (0).to_bytes(2, "little")  # v150
        body.append(0)  # v156
        body += (0).to_bytes(2, "little")  # v144
        extra_zeroes = int(os.environ.get("USDOF_GAME_CHARSELECT_0004_EXTRA_ZEROES", "16"), 0)
        if extra_zeroes > 0:
            body += b"\x00" * extra_zeroes
        return bytes(body)


    def build_charselect_open_0004_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_CHARSELECT_0004_FLAG")
        if flag is None:
            flag = 1
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_CHARSELECT_0004_BODY")
        if body is None:
            body = self.build_charselect_open_0004_body()
        mode = os.environ.get("USDOF_GAME_CHARSELECT_0004_MODE", "codec").strip().lower()
        if mode in {"codec", "encoded"}:
            packet = self.server._codec_response(flag, 0x0004, body)
        else:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0004, body)
        return packet, (
            "CHARSELECT window-open sent "
            f"flag{flag}/opcode=0x0004 mode={mode} plain_len={len(body)} "
            f"plain={body[:32].hex(' ')}{' ...' if len(body) > 32 else ''}"
        )


    def build_login_state_init_0001_body(self, *, session: Any | None = None) -> bytes:
        """Build the current-exe flag1/opcode0001 state-init body.

        Live-proven 152-byte layout (do NOT shrink):

        ``01 00 00 04 00`` + u32 + fixed 0x80 pad + 2xu32 + 7xu8

        2026-07-15 regression: a 27-byte "strict stream-order" rewrite
        (state at body[2], length-prefixed empty string) made the client stop
        after CHARSELECT open and surface as "channel full".  ``sub_1601B80``
        continues stream reads after the primary fields (v184/v175/...), so a
        short body underflows and breaks the GET_USERINFO / 0x0286 handshake.

        Keep this empirical body.  Goblin PIN (UI 396) is cancelled by Stage3
        EXE narrow patch ``0x2B9428F`` jz→jmp, not by reshaping this packet.
        Trailing ``USDOF_GAME_STATE_INIT_0001_U8_541`` (body last byte / v186
        under many zero-pad cases) still defaults 0.
        """

        success = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_SUCCESS", "1"), 0) & 0xFF
        state_raw = os.environ.get("USDOF_GAME_STATE_INIT_0001_STATE", "4")
        if state_raw == "bound-channel":
            if not (_env_bool("USDOF_LOCAL_RAID_CHANNELS")
                    and _env_bool("USDOF_CHANNELINFO_UNIQUE_PORTS")):
                raise ValueError("bound-channel LOGIN requires local raid channels and unique ports")
            channel_id = getattr(session, "selected_channel_id", None)
            # Reviewed local fixture IDs; no general row.kind == native type rule.
            local_types = {10: 1, 23: 23, 32: 32}
            if type(channel_id) is not int or channel_id not in local_types:
                raise ValueError("bound-channel LOGIN requires a known per-connection channel binding")
            state = local_types[channel_id]
        else:
            state = int(state_raw, 0) & 0xFF
        v53f = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_53F", "0"), 0) & 0xFF
        v540 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_540", "0"), 0) & 0xFF
        v547 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_547", "0"), 0) & 0xFF
        v54c = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U32_54C", "0"), 0) & 0xFFFFFFFF
        from game.raid.native_connection import key_for_login
        routing_key = key_for_login(self.server, session) if success else None
        if routing_key is not None:
            v54c = routing_key
        u32_a = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U32_AFTER_BLOCK_A", "0"), 0) & 0xFFFFFFFF
        u32_b = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U32_AFTER_BLOCK_B", "0"), 0) & 0xFFFFFFFF
        global_b0c = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_GLOBAL_B0C", "0"), 0) & 0xFF
        global_b0f = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_GLOBAL_B0F", "0"), 0) & 0xFF
        v542 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_542", "0"), 0) & 0xFF
        v53e = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_53E", "0"), 0) & 0xFF
        v545 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_545", "0"), 0) & 0xFF
        v548 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_548", "0"), 0) & 0xFF
        # 密保门闩相关；默认 0。角色页弹窗取消以 EXE 0x2B9428F 补丁为准。
        v541 = int(os.environ.get("USDOF_GAME_STATE_INIT_0001_U8_541", "0"), 0) & 0xFF

        block80 = _env_hex_bytes("USDOF_GAME_STATE_INIT_0001_BLOCK80")
        if block80 is None:
            block80 = b"\x00" * 0x80
        if len(block80) > 0x80:
            raise ValueError("USDOF_GAME_STATE_INIT_0001_BLOCK80 must be at most 0x80 bytes")
        block80 = block80.ljust(0x80, b"\x00")

        body = bytearray()
        body.append(success)
        body.append(v53f)
        body.append(v540)
        body.append(state)
        body.append(v547)
        body += v54c.to_bytes(4, "little")
        body += block80
        body += u32_a.to_bytes(4, "little")
        body += u32_b.to_bytes(4, "little")
        body.extend([global_b0c, global_b0f, v542, v53e, v545, v548, v541])
        result = bytes(body)
        if _env_bool("USDOF_RAID_LOCAL", False):
            from game.raid.channel_adapter import adapt_login_state_body
            result = adapt_login_state_body(result, getattr(session, "selected_channel_id", None))
        return result


    def build_login_state_init_0001_packet(self, *, session: Any | None = None) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_STATE_INIT_0001_FLAG")
        if flag is None:
            flag = 1
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_STATE_INIT_0001_BODY")
        if body is None:
            body = self.build_login_state_init_0001_body(session=session)
        if _env_bool("USDOF_RAID_LOCAL", False):
            from game.raid.channel_adapter import adapt_login_state_body
            body = adapt_login_state_body(body, getattr(session, "selected_channel_id", None))
        from game.raid.native_connection import prepare_login_body, track_login_packet
        body = prepare_login_body(self.server, session, body, flag)
        mode = os.environ.get("USDOF_GAME_STATE_INIT_0001_MODE", "raw").strip().lower()
        if mode in {"codec", "encoded"}:
            packet = self.server._codec_response(flag, 0x0001, body)
            mode_note = "codec"
        else:
            packet = self.server._raw_response(flag, 0x0001, body)
            mode_note = "raw"
        track_login_packet(self.server, session, packet, body, flag)
        # 日志字段沿用 live body 的 body[3] 位置（环境 STATE 写入位）
        state = body[3] if len(body) > 3 else None
        return packet, (
            "LOGIN state-init sent "
            f"flag{flag}/opcode=0x0001 mode={mode_note} plain_len={len(body)} "
            f"state={'none' if state is None else f'0x{state:02x}'} "
            f"plain={body[:32].hex(' ')}{' ...' if len(body) > 32 else ''}"
        )


    def build_0286_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_0286_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_0286_BODY")
        if body is None:
            # Current USDOF 90 active handler for flag0/opcode 0x0286 reads
            # one u8 and latches a ready/gate byte.  Live testing showed
            # 0x04 raises "The current Channel is full"; use 0 as the least
            # invasive success/clear value.  The older S4A12-style flag1 body
            # can still be tested by env override.
            body = b"\x00" if flag == 0 else b"\x00\x04"
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_0286_MODE", "auto").strip().lower()
        if mode in {"raw", "plain"} or (mode == "auto" and flag == 0):
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0286, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0286, body)
        return packet, (
            "LOAD_EXTEND_CHARACS post-01BA-PIV sent "
            f"flag{flag}/opcode=0x0286 mode={mode} plain={body.hex(' ')}"
        )


    def build_0080_packet(self) -> tuple[bytes, str]:
        flag = _env_optional_int("USDOF_GAME_LOAD_EXTEND_CHARACS_0080_FLAG")
        if flag is None:
            flag = 0
        flag &= 0xFF
        body = _env_hex_bytes("USDOF_GAME_LOAD_EXTEND_CHARACS_0080_BODY")
        if body is None:
            # USDOF90 flag0/opcode 0x0080 handler 0x01642600 consumes:
            # u32, u8, u8, u8, u16, u16, u8 countA, countA*(u8,u8,u16),
            # u8 countB, countB*u16. This 17-byte trace shape reaches the
            # handler tail / EnsureState8 path without using the 0x0286
            # full-channel notification.
            body = b"\x00" * 11 + b"\x01\x00\x00\x00\x00\x00"
        mode = os.environ.get("USDOF_GAME_LOAD_EXTEND_CHARACS_0080_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            packet = self.server._raw_response(flag, 0x0080, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0080, body)
        return packet, (
            "LOAD_EXTEND_CHARACS post-01BA-PIV sent "
            f"flag{flag}/opcode=0x0080 mode={mode} plain={body.hex(' ')}"
        )

