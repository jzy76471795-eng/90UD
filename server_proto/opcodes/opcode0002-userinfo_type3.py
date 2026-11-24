# Enter-seria chain 12: S->C 0x0002 USERINFO type3 bind-main (non-PVP n2500)
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE3=1
#
# Stage3 reverse (idalib, authority IDB):
# - 0x018D4140 case 3 -> userinfo_type3_bind_main_18D27C0
# - type3 can create town object via sub_18CD220 when channel matches (no 0029)
# - then userinfo_get_or_make_char_obj_18CCBE0 -> set_main_object_write_n2500_1E7A850
# - n2500 write @ 0x01E7A994 only if:
#     a2(char_obj)!=0, *(obj+8)==local_key(4294EF8), !sub_1E79960
# - type1(18D3A70) needs existing 1E7F0F0(key); type3 can create first
# - packet read order before n2500 write:
#     blob6 (2C74B80 6) + u16 key (2C74A80)
# - after write more u8/u16 reads; tail pad keeps stream from underrun
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


def build_userinfo_type3_bind_main_plaintext(
    *,
    key: int = 0,
    entry_flag: int = 1,
    head6: bytes | None = None,
    tail_pad: int = 64,
) -> bytes:
    # Outer: u8 type=3, u16 count=1, u8 entry_flag
    # Entry: 6B blob + u16 key + trailing fields (zeros for A/B)
    if head6 is None:
        head6 = bytes(6)
    if len(head6) != 6:
        raise ValueError("type3 head6 must be exactly 6 bytes, got %s" % len(head6))
    key_u16 = int(key) & 0xFFFF
    flag_u8 = int(entry_flag) & 0xFF
    pad = max(0, int(tail_pad))
    payload = head6 + key_u16.to_bytes(2, "little") + bytes(pad)
    return bytes([3]) + (1).to_bytes(2, "little") + bytes([flag_u8]) + payload


class EnterSeriaRoomUserinfoType3BindMain0002Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE3", False):
            return None, (
                "S->C flag0/opcode=0x0002 USERINFO type3 bind-main suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE3=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_BODY")
        if body is None:
            key = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            entry_flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_ENTRY_FLAG", 1) & 0xFF
            tail_pad = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_TAIL_PAD", 64)
            head = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_HEAD6")
            body = build_userinfo_type3_bind_main_plaintext(
                key=key,
                entry_flag=entry_flag,
                head6=head,
                tail_pad=tail_pad,
            )
            body_note = (
                "type=3 count=1 entry_flag=%s key=%s head6=%s tail_pad=%s plain_len=%s"
                % (
                    entry_flag,
                    key,
                    (head.hex(" ") if head is not None else "00*6"),
                    tail_pad,
                    len(body),
                )
            )
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE3_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0002, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0002, body)
        note = (
            "queued ENTER_SERIA USERINFO type3 bind-main n2500 path "
            "flag=%s opcode=0x0002 mode=%s %s; reverse 018D4140->18D27C0->18CCBE0->1E7A850"
            % (flag, mode, body_note)
        )
        return packet, note
