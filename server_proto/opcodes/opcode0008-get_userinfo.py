"""拉取 USERINFO (C->S 0x0008)

从 ``game_server.py`` 拆出的角色相关补包链。通过 ``self.server`` 复用
GameServer 的 DB/codec/日志等基础设施，避免循环依赖。
"""
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


class CharGetUserinfoService:
    """拉取 USERINFO (C->S 0x0008)"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_get_userinfo(self, body=None, *, session=None) -> tuple[list[bytes], str]:
        # Native 029AD019 / 017E6070 send <H target><B kind=3> for
        # inspection. Only FFFF/2 before character selection requests roster.
        if body is not None:
            raw = bytes(body)
            if len(raw) not in (3, 8, 16) or any(raw[3:]):
                return [], 'GET_USERINFO rejected malformed target request'
            target, kind = int.from_bytes(raw[:2], 'little'), raw[2]
            if kind == 3 and 1 <= target <= 65534:
                from game.character.other_profile import handle
                return handle(self.server, session, target)
            if (target, kind) != (65535, 2) or getattr(session, 'selected_character_id', None) is not None:
                return [], 'GET_USERINFO rejected unsupported selected-character request'
        if not _env_bool("USDOF_GAME_GET_USERINFO_SEND_ROSTER", True):
            return [], "GET_USERINFO observed; roster response disabled by USDOF_GAME_GET_USERINFO_SEND_ROSTER=0"
        packets, note = self.server._db_userinfo_packet_stream(
            "cmd-get-userinfo",
            include_followups=_env_bool("USDOF_GAME_GET_USERINFO_FOLLOWUPS", True),
        )
        return packets, f"GET_USERINFO sent {note}"

