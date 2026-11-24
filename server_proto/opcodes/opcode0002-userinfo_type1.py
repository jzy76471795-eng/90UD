# Enter-seria chain 11: S->C 0x0002 USERINFO type1 bind-main (non-PVP n2500)
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE1=1
# Stage3: 0x018D4140 type1 -> 0x018D3A70 -> set_main_object_write_n2500_1E7A850
# Requires 0x0131 primary object first (1E7F0F0 key lookup). No 0x0029.
from __future__ import annotations

import os
from typing import Any


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


def _env_hex_bytes(name: str):
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    skip = set(" \t\r\n:_-")
    normalized = "".join(ch for ch in normalized if ch not in skip)
    if len(normalized) % 2:
        raise ValueError("%s bad hex: %r" % (name, raw))
    return bytes.fromhex(normalized)


def build_userinfo_type1_bind_main_plaintext(
    *,
    key: int = 0,
    entry_flag: int = 1,
    head18: bytes | None = None,
    tail_pad: int = 48,
) -> bytes:
    # Stage3 0x018D4140 case1 + 0x018D3A70: 18B + u16 key + trailing reads
    if head18 is None:
        head18 = bytes(18)
    if len(head18) != 18:
        raise ValueError("type1 head18 must be exactly 18 bytes, got %s" % len(head18))
    key_u16 = int(key) & 0xFFFF
    flag_u8 = int(entry_flag) & 0xFF
    pad = max(0, int(tail_pad))
    payload = head18 + key_u16.to_bytes(2, "little") + bytes(pad)
    return bytes([1]) + (1).to_bytes(2, "little") + bytes([flag_u8]) + payload


class EnterSeriaRoomUserinfoType1BindMain0002Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE1", False):
            return None, (
                "S->C flag0/opcode=0x0002 USERINFO type1 bind-main suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE1=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_BODY")
        if body is None:
            key = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            entry_flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_ENTRY_FLAG", 1) & 0xFF
            tail_pad = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_TAIL_PAD", 48)
            head = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_HEAD18")
            body = build_userinfo_type1_bind_main_plaintext(
                key=key,
                entry_flag=entry_flag,
                head18=head,
                tail_pad=tail_pad,
            )
            body_note = (
                "type=1 count=1 entry_flag=%s key=%s head18=%s tail_pad=%s plain_len=%s"
                % (
                    entry_flag,
                    key,
                    (head.hex(" ") if head is not None else "00*18"),
                    tail_pad,
                    len(body),
                )
            )
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE1_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0002, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0002, body)
        note = (
            "queued ENTER_SERIA USERINFO type1 bind-main n2500 path "
            "flag=%s opcode=0x0002 mode=%s %s; reverse 018D4140->18D3A70->1E7A850"
            % (flag, mode, body_note)
        )
        return packet, note
