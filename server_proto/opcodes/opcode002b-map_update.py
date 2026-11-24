# S->C 0x002B ENUM_NOTIPACKET_PVP_SEAT_STATE (PacketEnum name).
# Stage3 handler 0x01652F50: lookup existing map by map_id; missing => no-op.
# Body: u16 map_id, u8 type (map+0x8C), u8 slot_count, then N*(u8 idx,u8 flag,u16 key).
# Slot key match installs map table (area+0x37C). No type 9/10 PVP tail.
# Default OFF. Prefer 0029 VFUNC16=0 to CREATE map first.
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
    skip = set(" :_\t\n\r-")
    normalized = "".join(ch for ch in normalized if ch not in skip)
    if len(normalized) % 2:
        raise ValueError("%s bad hex: %r" % (name, raw))
    return bytes.fromhex(normalized)

class EnterSeriaRoomMapUpdate002BService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_002B_MAP_UPDATE", False):
            return None, (
                "S->C flag0/opcode=0x002B map-update suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_002B_MAP_UPDATE=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_002B_BODY")
        if body is None:
            map_id = _env_int(
                "USDOF_GAME_ENTER_SERIA_002B_MAP_ID",
                int(getattr(selected, "town_id", 1) or 1),
            ) & 0xFFFF
            map_type = _env_int("USDOF_GAME_ENTER_SERIA_002B_TYPE", 0) & 0xFF
            slot_index = _env_int("USDOF_GAME_ENTER_SERIA_002B_SLOT_INDEX", 0) & 0xFF
            slot_flag = _env_int("USDOF_GAME_ENTER_SERIA_002B_SLOT_FLAG", 0) & 0xFF
            slot_key = _env_int(
                "USDOF_GAME_ENTER_SERIA_002B_SLOT_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            slot_count = _env_int("USDOF_GAME_ENTER_SERIA_002B_SLOT_COUNT", 1) & 0xFF
            if slot_count < 1:
                slot_count = 1
            if slot_count > 8:
                slot_count = 8
            body = map_id.to_bytes(2, "little")
            body += bytes([map_type, slot_count])
            for index in range(slot_count):
                body += bytes([slot_index if index == 0 else index, slot_flag])
                key = slot_key if index == 0 else 0xFFFF
                body += key.to_bytes(2, "little")
            body_note = (
                "map_id=%s type=%s slot_count=%s slot0_index=%s slot0_flag=%s slot0_key=%s"
                % (map_id, map_type, slot_count, slot_index, slot_flag, slot_key)
            )
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_002B_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_002B_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x002B, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x002B, body)
        note = (
            "queued ENTER_SERIA map-update-002B candidate "
            "S->C flag%s/opcode=0x002B mode=%s %s plain_len=%s wire=%s; "
            "chain_file=game/chains/opcode002b-map_update.py; "
            "reverse: registry 0x2B -> 0x01652F50; needs existing map object; "
            "slot_key match installs map table and fills area+0x37C"
        ) % (flag, mode, body_note, len(body), len(packet))
        return packet, note

