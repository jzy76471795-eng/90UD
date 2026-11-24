# Enter-seria chain 07: S->C 0x0017 USER_AREA candidate
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0017_USER_AREA=1
# Stage3: registry 0x016713FC maps 0x0017 -> 0x0167A160
# Read: u16 key, u8 town, u8 area, u16 x, u16 y, u8 dir, u8 active
# Depends on 0x0131 primary object. Enum NOTIPACKET_USER_AREA.
# 0x0029 is PVP_ROOM_INFO - do not use as town map-table.
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


class EnterSeriaRoomUserArea0017Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0017_USER_AREA", False):
            return None, (
                "S->C flag0/opcode=0x0017 USER_AREA suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0017_USER_AREA=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0017_BODY")
        if body is None:
            key = _env_int("USDOF_GAME_ENTER_SERIA_0017_KEY", _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0)) & 0xFFFF
            town = _env_int("USDOF_GAME_ENTER_SERIA_0017_TOWN_ID", int(getattr(selected, "town_id", 1) or 1)) & 0xFF
            area = _env_int("USDOF_GAME_ENTER_SERIA_0017_AREA_ID", int(getattr(selected, "area_id", 0) or 0)) & 0xFF
            x = _env_int("USDOF_GAME_ENTER_SERIA_0017_X", int(getattr(selected, "pos_x", 0) or 0)) & 0xFFFF
            y = _env_int("USDOF_GAME_ENTER_SERIA_0017_Y", int(getattr(selected, "pos_y", 0) or 0)) & 0xFFFF
            direction = _env_int("USDOF_GAME_ENTER_SERIA_0017_DIR", int(getattr(selected, "direction", 5) or 5)) & 0xFF
            active = _env_int("USDOF_GAME_ENTER_SERIA_0017_ACTIVE", int(getattr(selected, "area_state", 3) or 3)) & 0xFF
            body = (
                key.to_bytes(2, "little")
                + bytes([town, area])
                + x.to_bytes(2, "little")
                + y.to_bytes(2, "little")
                + bytes([direction, active])
            )
            body_note = "key=%s town=%s area=%s x=%s y=%s dir=%s active=%s" % (key, town, area, x, y, direction, active)
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0017_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0017_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0017, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0017, body)
        note = (
            "queued ENTER_SERIA USER_AREA candidate "
            "S->C flag%s/opcode=0x0017 mode=%s %s plain_len=%s wire=%s; "
            "chain_file=game/chains/opcode0017-user_area.py; "
            "reverse 0x0017 handler 0x0167A160 depends on 0x0131 primary object"
        ) % (flag, mode, body_note, len(body), len(packet))
        return packet, note

