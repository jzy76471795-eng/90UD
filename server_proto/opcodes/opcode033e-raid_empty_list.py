# -*- coding: utf-8 -*-
"""Opt-in experiment for one observed C1/033E first-screen query.

The 12B reply uses experimental A/B/count values, not business defaults or a
claim that the complete native response contract has been established.
"""
from __future__ import annotations

import os
import struct
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def try_handle_raid_empty_list_033e(
    server: Any,
    *,
    flag: int,
    plain: bytes,
    session: Any = None,
) -> tuple[list[bytes], str] | None:
    """Return None to retain the existing unknown-opcode path when ineligible."""
    if type(flag) is not int or flag != 1:
        return None
    if not (
        _env_bool("USDOF_RESTORE_RAID_EMPTY_LIST_02F2", False)
        and _env_bool("USDOF_RESTORE_ANTON_WAITING_SPAWN", False)
        and _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
    ):
        return None
    channel = getattr(session, "selected_channel_id", None)
    selected = getattr(session, "selected_character_id", None)
    marker = getattr(session, "anton_waiting_position_character_id", None)
    if not (
        type(channel) is int
        and channel == 23
        and type(selected) is int
        and selected > 0
        and type(marker) is int
        and marker > 0
        and marker == selected
        and getattr(session, "enter_seria_settled", False) is True
    ):
        return None
    if type(plain) not in (bytes, bytearray) or len(plain) != 13:
        return None
    if not (
        plain[0] == 0
        and int.from_bytes(plain[1:5], "little") == 0
        and plain[5:9] == bytes((5, 0, 0, 1))
        and int.from_bytes(plain[9:13], "little") == 0
    ):
        return None
    # Explicit experiment values only; no row serializer or shared state writes.
    body = struct.pack("<III", 0, 0, 0)
    packet = server._codec_response(0, 0x02F2, body)
    return [packet], (
        "C1/033E raid_empty_list_02f2 experimental_firstscreen "
        "A=0 B=0 count=0 plain_len=12; wire=%s" % len(packet)
    )
