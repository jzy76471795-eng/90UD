# Enter-seria chain 08: S->C 0x0018 AREA_USERS candidate
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0018_AREA_USERS=1
# Stage3: registry maps 0x0018 -> 0x016800B0
# Read: u8 town, u8 area, u16 count, then (u16 key,u16 x,u16 y,u8 dir,u8 active)*count
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


class EnterSeriaRoomAreaUsers0018Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0018_AREA_USERS", False):
            return None, (
                "S->C flag0/opcode=0x0018 AREA_USERS suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0018_AREA_USERS=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0018_BODY")
        if body is None:
            town = _env_int("USDOF_GAME_ENTER_SERIA_0018_TOWN_ID", int(getattr(selected, "town_id", 1) or 1)) & 0xFF
            area = _env_int("USDOF_GAME_ENTER_SERIA_0018_AREA_ID", int(getattr(selected, "area_id", 0) or 0)) & 0xFF
            key = _env_int("USDOF_GAME_ENTER_SERIA_0018_KEY", _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0)) & 0xFFFF
            x = _env_int("USDOF_GAME_ENTER_SERIA_0018_X", int(getattr(selected, "pos_x", 0) or 0)) & 0xFFFF
            y = _env_int("USDOF_GAME_ENTER_SERIA_0018_Y", int(getattr(selected, "pos_y", 0) or 0)) & 0xFFFF
            direction = _env_int("USDOF_GAME_ENTER_SERIA_0018_DIR", int(getattr(selected, "direction", 5) or 5)) & 0xFF
            active = _env_int("USDOF_GAME_ENTER_SERIA_0018_ACTIVE", int(getattr(selected, "area_state", 3) or 3)) & 0xFF
            count = _env_int("USDOF_GAME_ENTER_SERIA_0018_COUNT", 1) & 0xFFFF
            if count < 1:
                count = 1
            body = bytes([town, area]) + count.to_bytes(2, "little")
            entry = (
                key.to_bytes(2, "little")
                + x.to_bytes(2, "little")
                + y.to_bytes(2, "little")
                + bytes([direction, active])
            )
            body += entry * count
            body_note = "town=%s area=%s count=%s key=%s x=%s y=%s dir=%s active=%s" % (town, area, count, key, x, y, direction, active)
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0018_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0018_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0018, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0018, body)
        note = (
            "queued ENTER_SERIA AREA_USERS candidate "
            "S->C flag%s/opcode=0x0018 mode=%s %s plain_len=%s wire=%s; "
            "chain_file=game/chains/opcode0018-area_users.py"
        ) % (flag, mode, body_note, len(body), len(packet))
        return packet, note

