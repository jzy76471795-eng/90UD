# Enter-seria chain 09: S->C 0x0016 USER_POSITION candidate
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0016_USER_POSITION=1
# Stage3: registry maps 0x0016 -> 0x01675F30
# Read: u16 key, u16 x, u16 y, u8 dir, u16 speed_or_state
# First u16 is object key for sub_1E7F0F0 (same family as 0x0131/0x0017).
# Enum NOTIPACKET_USER_POSITION. codec index 0x0016%14=8 (codec8).
# S4A12 ends select-init with 0x0016; reference only - field order from Stage3.
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


class EnterSeriaRoomUserPosition0016Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0016_USER_POSITION", False):
            return None, (
                "S->C flag0/opcode=0x0016 USER_POSITION suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0016_USER_POSITION=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0016_BODY")
        if body is None:
            key = _env_int(
                "USDOF_GAME_ENTER_SERIA_0016_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            x = _env_int(
                "USDOF_GAME_ENTER_SERIA_0016_X",
                int(getattr(selected, "pos_x", 0) or 0),
            ) & 0xFFFF
            y = _env_int(
                "USDOF_GAME_ENTER_SERIA_0016_Y",
                int(getattr(selected, "pos_y", 0) or 0),
            ) & 0xFFFF
            direction = _env_int(
                "USDOF_GAME_ENTER_SERIA_0016_DIR",
                int(getattr(selected, "direction", 5) or 5),
            ) & 0xFF
            speed = _env_int("USDOF_GAME_ENTER_SERIA_0016_SPEED", 100) & 0xFFFF
            body = (
                key.to_bytes(2, "little")
                + x.to_bytes(2, "little")
                + y.to_bytes(2, "little")
                + bytes([direction])
                + speed.to_bytes(2, "little")
            )
            body_note = "key=%s x=%s y=%s dir=%s speed=%s" % (key, x, y, direction, speed)
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0016_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0016_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0016, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0016, body)
        note = (
            "queued ENTER_SERIA USER_POSITION candidate "
            "S->C flag%s/opcode=0x0016 mode=%s %s plain_len=%s wire=%s; "
            "chain_file=game/chains/opcode0016-user_position.py; "
            "reverse 0x0016 handler 0x01675F30 needs 0x0131 primary object for sub_1E7F0F0(key)"
        ) % (flag, mode, body_note, len(body), len(packet))
        return packet, note

