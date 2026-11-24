from __future__ import annotations

import struct
from dataclasses import dataclass

from config import GAME_HEADER_SIZE, MAX_GAME_PACKET
from native_core_loader import (
    build_game_header as build_native_game_header,
    parse_game_header as parse_native_game_header,
)


# Client-to-server encoded packets use a 13-byte physical header:
#
#   u8 flag, u16 opcode, u32 total_len, u32 checksum/tag, u16 seq
#
# For packets with an encoded body, the first three body bytes historically
# looked like a "reserved" header suffix in our early tracer.  We keep the
# existing 16-byte view for non-empty encoded packets because downstream trace
# code intentionally reconstructs ``reserved + payload`` as the codec body.
# But zero-body client packets are genuinely ``length == 13`` and must not
# block the stream parser.
COMPACT_GAME_HEADER_SIZE = 13


@dataclass(frozen=True)
class GamePacketHeader:
    flag_or_magic: int
    opcode: int
    length: int
    checksum_or_tag: int
    sub_or_seq: int
    reserved: bytes
    raw: bytes

    @property
    def payload_len(self) -> int:
        return self.length - len(self.raw)

    @property
    def codec_index(self) -> int:
        return self.opcode % 14


class GameWireError(RuntimeError):
    pass


def parse_game_header(raw: bytes) -> GamePacketHeader:
    if len(raw) < COMPACT_GAME_HEADER_SIZE:
        raise GameWireError(f"need 0x0d bytes for compact game header, got {len(raw)}")
    flag, opcode, length, checksum, sub_or_seq, header_view_size = (
        parse_native_game_header(raw)
    )
    if length < COMPACT_GAME_HEADER_SIZE:
        raise GameWireError(f"invalid game total length 0x{length:x}")
    if length > MAX_GAME_PACKET:
        raise GameWireError(f"game packet too large 0x{length:x}")
    # The native fixed-width parser makes the same view-size decision as the
    # historical Python path; keep slicing and reserved/body ownership here.
    raw = raw[:header_view_size]
    reserved = raw[0x0D:0x10] if header_view_size >= GAME_HEADER_SIZE else b""
    return GamePacketHeader(
        flag_or_magic=flag,
        opcode=opcode,
        length=length,
        checksum_or_tag=checksum,
        sub_or_seq=sub_or_seq,
        reserved=reserved,
        raw=raw,
    )


def build_game_header(
    opcode: int,
    payload_len: int,
    *,
    flag_or_magic: int = 1,
    checksum_or_tag: int = 0,
    sub_or_seq: int = 0,
    reserved: bytes = b"\x00\x00\x00",
) -> bytes:
    return build_native_game_header(
        opcode,
        payload_len,
        flag_or_magic=flag_or_magic,
        checksum_or_tag=checksum_or_tag,
        sub_or_seq=sub_or_seq,
        reserved=reserved,
    )


def build_game_packet(opcode: int, payload: bytes, **kwargs) -> bytes:
    return build_game_header(opcode, len(payload), **kwargs) + payload


def build_compact_game_header(
    opcode: int,
    payload_len: int,
    *,
    flag_or_magic: int = 1,
    checksum_or_tag: int = 0,
    sub_or_seq: int = 0,
) -> bytes:
    return build_native_game_header(
        opcode,
        payload_len,
        compact=True,
        flag_or_magic=flag_or_magic,
        checksum_or_tag=checksum_or_tag,
        sub_or_seq=sub_or_seq,
    )


def build_compact_game_packet(opcode: int, payload: bytes, **kwargs) -> bytes:
    return build_compact_game_header(opcode, len(payload), **kwargs) + payload


def game_packet_body(packet: bytes, header: GamePacketHeader, *, compact_prefix: bool = True) -> bytes:
    """Return the logical Game body for both 13-byte and 16-byte views.

    Current USDOF client packets use a 13-byte physical header.  When the
    total length is at least 0x10, the existing parser exposes the first three
    body bytes as ``header.reserved`` to preserve the older 16-byte diagnostic
    view.  C->S plain-wire mode needs those bytes included in the body.
    Server-built 16-byte packets should pass ``compact_prefix=False`` because
    their reserved bytes are an actual header suffix.
    """

    packet = packet[: header.length]
    if header.length <= COMPACT_GAME_HEADER_SIZE:
        return b""
    if len(header.raw) >= GAME_HEADER_SIZE:
        if compact_prefix:
            return header.reserved + packet[GAME_HEADER_SIZE:]
        return packet[GAME_HEADER_SIZE:]
    return packet[COMPACT_GAME_HEADER_SIZE:]


def describe_game_header(header: GamePacketHeader) -> str:
    return (
        f"flag=0x{header.flag_or_magic:02x} opcode=0x{header.opcode:04x} "
        f"codec_index={header.codec_index} length=0x{header.length:x} "
        f"payload=0x{header.payload_len:x} checksum=0x{header.checksum_or_tag:08x} "
        f"sub_or_seq=0x{header.sub_or_seq:04x} reserved={header.reserved.hex()}"
    )
