# -*- coding: utf-8 -*-
"""Default-off bounded ACK acceptance probe; no raid object or business success.

The five-byte reply is only the direct prefix hypothesis from409. Experimental
opaque value1 and all tail/completebody/clientacceptance semantics remain unproved.
"""
from __future__ import annotations

import os
import struct
from typing import Any


"""Pure bounded mode0 hypothesis fixture; no transport or business creation.

Main names use request bytes within the printable ASCII server policy. The
default remains name1111 for existing fixture callers. Empty subname is policy.
Main scalar values mirror observed direct ctor writes; subrecord zeros and the
extra-record count0 are experiment choices, not native defaults or acceptance.
This expands only title input; it does not implement complete raid creation.
"""
import struct


def _encode_policy_name(raw: bytes) -> bytes:
    """Intentional ASCII/NUL-free 0..255 subset, not a native charset contract."""
    if type(raw) is not bytes:
        raise TypeError("name policy requires exact bytes")
    if len(raw) > 255 or b"\x00" in raw or any(value > 0x7F for value in raw):
        raise ValueError("name policy requires NUL-free ASCII of at most255 bytes")
    return struct.pack("<I", len(raw)) + raw


def encode_mode0_fixture(identifier: int, raw_raid_name: bytes = b"1111") -> bytes:
    """Build a fixed experimental sequence; this does not prove a legal body."""
    if type(identifier) is not int:
        raise TypeError("identifier requires exact int")
    if not 0 <= identifier <= 0xFFFFFFFF:
        raise ValueError("identifier must fit u32")
    if type(raw_raid_name) is not bytes:
        raise TypeError("raid name policy requires exact bytes")
    # The known 0250 reader consumes 0<L<256 with cap256. Printable ASCII is
    # a server policy, not proof of the client's charset or input-length limit.
    if not 1 <= len(raw_raid_name) <= 255 or any(
        not 0x20 <= value <= 0x7E for value in raw_raid_name
    ):
        raise ValueError("raid name requires one to 255 printable ASCII bytes")
    return (
        struct.pack("<II", identifier, 0)
        + struct.pack("<I", identifier)
        + _encode_policy_name(raw_raid_name)
        + struct.pack("<BBBI", 2, 0, 0xFF, 0)
        + struct.pack("<HB", 0, 0)
        + _encode_policy_name(b"")
        + struct.pack("<bBBBIBBB", 0, 0, 0, 0, 0, 0, 0, 0)
        + b"\x00"
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def encode_mode0_creator_row_fixture(
    identifier: int, actor_key: int, raw_name: bytes, *, raw_raid_name: bytes = b"1111"
) -> bytes:
    """One experimental row; key projection and zero fields are explicit policy.

    The u16 comes from the existing town source only for exact 1..65535 IDs.
    The adjacent u8 is fixed zero, never a truncated selected character ID.
    This does not establish realm, job, member identity or complete N1 legality.
    """
    if type(actor_key) is not int or not 1 <= actor_key <= 0xFFFF:
        raise ValueError("creator row requires an exact positive u16 source key")
    if type(raw_name) is not bytes or not 1 <= len(raw_name) <= 7:
        raise ValueError("creator row requires one to seven raw ASCII bytes")
    name = _encode_policy_name(raw_name)
    return (
        encode_mode0_fixture(identifier, raw_raid_name)[:-1]
        + b"\x01"
        + struct.pack("<HB", actor_key, 0)
        + name
        + struct.pack("<bBBBIBBB", 0, 0, 0, 0, 0, 0, 0, 0)
    )


def parse_raid_create_request_0299(
    plain: bytes | bytearray,
    *,
    max_name_bytes: int | None = None,
) -> tuple[int, bytes] | None:
    """Parse mode and raw name bytes from the normal sender's scan domain.

    ``max_name_bytes`` is optional server policy, not a native client limit.
    Empty names can parse structurally; this does not authorize creation.
    Encoding and business validation remain the caller's unresolved concerns.
    """
    if max_name_bytes is not None and (
        type(max_name_bytes) is not int or max_name_bytes < 0
    ):
        raise ValueError("max_name_bytes must be a nonnegative server-policy int or None")
    if type(plain) not in (bytes, bytearray) or len(plain) < 5:
        return None
    data = bytes(plain)
    mode = data[0]
    if mode not in (0, 1, 2):
        return None
    length = int.from_bytes(data[1:5], "little")
    if length != len(data) - 5:
        return None
    if max_name_bytes is not None and length > max_name_bytes:
        return None
    raw_name = data[5:]
    # 412 proves byte-NUL terminates the normal sender's length scan.
    # This is a sender-domain boundary, not an assumed charset/input rule.
    if b"\x00" in raw_name:
        return None
    return mode, raw_name


def try_handle_raid_create_ack_probe_0299(
    server: Any,
    *,
    flag: int,
    opcode: int,
    plain: bytes,
    session: Any = None,
) -> tuple[list[bytes], str] | None:
    """Build one experimental prefix per session; no transmission claim."""
    if type(flag) is not int or flag != 1 or type(opcode) is not int or opcode != 0x0299:
        return None
    if not (
        _env_bool("USDOF_EXPERIMENT_RAID_CREATE_ACK_0299", False)
        and _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
        and _env_bool("USDOF_RESTORE_ANTON_WAITING_SPAWN", False)
        and os.environ.get("USDOF_EXPERIMENT_RAID_CREATE_ACK_0299_VALUE") == "1"
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
        and getattr(session, "raid_create_ack_0299_probe_sent", False) is False
    ):
        return None
    parsed = parse_raid_create_request_0299(plain, max_name_bytes=255)
    if parsed is None or parsed[0] != 0:
        return None
    raw_raid_name = parsed[1]
    # Preserve exact title bytes; no decode/normalization or Chinese assumption.
    # Exclude control bytes from this narrowly expanded display-only probe.
    if not raw_raid_name or any(not 0x20 <= value <= 0x7E for value in raw_raid_name):
        return None
    # V=1 is an explicit opt-in experiment, not an allocated object identifier.
    object_probe = _env_bool("USDOF_EXPERIMENT_RAID_CREATE_OBJECT_0250", False)
    creator_row = _env_bool("USDOF_EXPERIMENT_RAID_CREATOR_ROW_0250", False)
    if creator_row:
        name = getattr(session, "selected_character_name", None)
        if not (
            object_probe
            and selected <= 0xFFFF
            and type(name) is str
            and 1 <= len(name) <= 7
            and all(0 < ord(character) <= 0x7F for character in name)
        ):
            return None
    if object_probe:
        body = (
            encode_mode0_creator_row_fixture(
                1, selected, name.encode("ascii"), raw_raid_name=raw_raid_name
            )
            if creator_row
            else encode_mode0_fixture(1, raw_raid_name)
        )
        notification = server._codec_response(0, 0x0250, body)
    packet = server._codec_response(1, 0x0299, struct.pack("<BI", 1, 1))
    session.raid_create_ack_0299_probe_sent = True
    if creator_row:
        return [notification, packet], "C1/0299 bounded_creator_name_row experiment; member business unproved"
    if object_probe:
        return [notification, packet], "C1/0299 bounded_notification_then_ack experiment; no business creation claimed"
    return [packet], "C1/0299 bounded_ack_probe experimental_prefix S=1 V=1; no object created"


def try_handle_raid_member_refresh_probe_054e(
    server: Any,
    *,
    flag: int,
    opcode: int,
    plain: bytes,
    session: Any = None,
) -> tuple[list[bytes], str] | None:
    """Default-off S1/D1/K1/N1 hypothesis; no complete serializer or business proof."""
    if type(flag) is not int or flag != 1 or type(opcode) is not int or opcode != 0x054E:
        return None
    if not (
        _env_bool("USDOF_EXPERIMENT_RAID_MEMBER_REFRESH_054E", False)
        and _env_bool("USDOF_EXPERIMENT_RAID_CREATE_ACK_0299", False)
        and _env_bool("USDOF_EXPERIMENT_RAID_CREATE_OBJECT_0250", False)
        and _env_bool("USDOF_EXPERIMENT_RAID_CREATOR_ROW_0250", False)
        and _env_bool("USDOF_LOCAL_RAID_CHANNELS", False)
        and _env_bool("USDOF_RESTORE_ANTON_WAITING_SPAWN", False)
        and os.environ.get("USDOF_EXPERIMENT_RAID_CREATE_ACK_0299_VALUE") == "1"
    ):
        return None
    if type(plain) not in (bytes, bytearray) or bytes(plain) != b"\x01\x01\x00\x00\x00":
        return None
    channel = getattr(session, "selected_channel_id", None)
    selected = getattr(session, "selected_character_id", None)
    marker = getattr(session, "anton_waiting_position_character_id", None)
    name = getattr(session, "selected_character_name", None)
    if not (
        type(channel) is int
        and channel == 23
        and type(selected) is int
        and 1 <= selected <= 0xFFFF
        and type(marker) is int
        and marker == selected
        and getattr(session, "enter_seria_settled", False) is True
        and getattr(session, "raid_create_ack_0299_probe_sent", False) is True
        and getattr(session, "raid_member_refresh_054e_probe_sent", False) is False
        and type(name) is str
        and 1 <= len(name) <= 7
        and all(0 < ord(character) <= 0x7F for character in name)
    ):
        return None
    # Reuse only N1+row from W's fixture. Fixed K1 and all zero fields are policy;
    # neither adjacent u8 nor native identity nor complete consumption is proved.
    base = encode_mode0_fixture(1)
    fixture = encode_mode0_creator_row_fixture(1, selected, name.encode("ascii"))
    body = struct.pack("<BBI", 1, 1, 1) + fixture[len(base) - 1 :]
    packet = server._codec_response(1, 0x054E, body)
    session.raid_member_refresh_054e_probe_sent = True
    return [packet], "C1/054E bounded_refresh experiment S1 D1 K1 N1; serializer/member business unproved"
