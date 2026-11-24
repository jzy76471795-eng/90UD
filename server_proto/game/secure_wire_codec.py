"""Inactive secure-wire adapter for the 14 frozen Stage3 Game codecs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from game.game_codec import (
    codec0_seed0_decode,
    codec0_seed0_encode,
    codec1_seed0_decode,
    codec1_seed0_encode,
    codec10_xor_dword,
    codec11_seed0_decode,
    codec11_seed0_encode,
    codec12_seed0_decode,
    codec12_seed0_encode,
    codec13_seed0_decode,
    codec13_seed0_encode,
    codec2_seed0_decode,
    codec2_seed0_encode,
    codec3_seed0_decode,
    codec3_seed0_encode,
    codec4_seed0_decode,
    codec4_seed0_encode,
    codec5_seed0_decode,
    codec5_seed0_encode,
    codec6_seed0_decode,
    codec6_seed0_encode,
    codec7_seed0_decode,
    codec7_seed0_encode,
    codec8_seed0_decode,
    codec8_seed0_encode,
    codec9_seed0_decode,
    codec9_seed0_encode,
)
from game.game_handlers import zero_pad_game_payload
from game.game_wire import (
    COMPACT_GAME_HEADER_SIZE,
    build_compact_game_packet,
    build_game_packet,
    game_packet_body,
    parse_game_header,
)
from game.secure_wire import SecureWireDirectionState, SecureWireMacRing, SecureWireRecord


CodecTransform = Callable[[bytes], bytes]

_CODEC_UNITS = (16, 8, 16, 16, 16, 8, 8, 8, 8, 16, 4, 8, 16, 8)
_CODEC_ENCODE: tuple[CodecTransform, ...] = (
    codec0_seed0_encode,
    codec1_seed0_encode,
    codec2_seed0_encode,
    codec3_seed0_encode,
    codec4_seed0_encode,
    codec5_seed0_encode,
    codec6_seed0_encode,
    codec7_seed0_encode,
    codec8_seed0_encode,
    codec9_seed0_encode,
    codec10_xor_dword,
    codec11_seed0_encode,
    codec12_seed0_encode,
    codec13_seed0_encode,
)
_CODEC_DECODE: tuple[CodecTransform, ...] = (
    codec0_seed0_decode,
    codec1_seed0_decode,
    codec2_seed0_decode,
    codec3_seed0_decode,
    codec4_seed0_decode,
    codec5_seed0_decode,
    codec6_seed0_decode,
    codec7_seed0_decode,
    codec8_seed0_decode,
    codec9_seed0_decode,
    codec10_xor_dword,
    codec11_seed0_decode,
    codec12_seed0_decode,
    codec13_seed0_decode,
)


@dataclass(frozen=True)
class ProtectedGamePacket:
    wire: bytes
    record: Optional[SecureWireRecord]
    padded_plain_length: int


def codec_unit(opcode: int) -> int:
    return _CODEC_UNITS[int(opcode) % 14]


def native_seed0_encode(opcode: int, padded_plain: bytes) -> bytes:
    return _CODEC_ENCODE[int(opcode) % 14](bytes(padded_plain))


def native_seed0_decode(opcode: int, cipher: bytes) -> bytes:
    return _CODEC_DECODE[int(opcode) % 14](bytes(cipher))


def protect_s2c_codec_packet(
    *,
    flag: int,
    opcode: int,
    plain_payload: bytes,
    direction_state: SecureWireDirectionState,
    mac_ring: SecureWireMacRing,
    already_padded: bool = False,
    **packet_kwargs,
) -> ProtectedGamePacket:
    plain = bytes(plain_payload)
    if not plain:
        return ProtectedGamePacket(
            wire=build_game_packet(opcode, b"", flag_or_magic=flag, **packet_kwargs),
            record=None,
            padded_plain_length=0,
        )
    padded = plain if already_padded else zero_pad_game_payload(plain, codec_unit(opcode))
    if len(padded) % codec_unit(opcode):
        raise ValueError("pre-padded secure S2C body is not aligned for its native codec")
    secure_cipher, record = direction_state.seal(opcode, padded)
    native_cipher = native_seed0_encode(opcode, secure_cipher)
    mac_ring.push(record)
    return ProtectedGamePacket(
        wire=build_game_packet(opcode, native_cipher, flag_or_magic=flag, **packet_kwargs),
        record=record,
        padded_plain_length=len(padded),
    )


def open_s2c_codec_packet(
    packet: bytes,
    *,
    direction_state: SecureWireDirectionState,
    mac_ring: SecureWireMacRing,
) -> bytes:
    header = parse_game_header(packet[:16])
    native_cipher = game_packet_body(packet, header, compact_prefix=False)
    if not native_cipher:
        return b""
    secure_cipher = native_seed0_decode(header.opcode, native_cipher)
    record = mac_ring.pop()
    return direction_state.open(header.opcode, secure_cipher, record)


def protect_c2s_packet(
    *,
    flag: int,
    opcode: int,
    plain_payload: bytes,
    direction_state: SecureWireDirectionState,
    mac_ring: SecureWireMacRing,
    checksum_or_tag: int = 0,
    sub_or_seq: int = 0,
) -> ProtectedGamePacket:
    plain = bytes(plain_payload)
    if not plain:
        return ProtectedGamePacket(
            wire=build_compact_game_packet(
                opcode,
                b"",
                flag_or_magic=flag,
                checksum_or_tag=checksum_or_tag,
                sub_or_seq=sub_or_seq,
            ),
            record=None,
            padded_plain_length=0,
        )
    cipher, record = direction_state.seal(opcode, plain)
    mac_ring.push(record)
    return ProtectedGamePacket(
        wire=build_compact_game_packet(
            opcode,
            cipher,
            flag_or_magic=flag,
            checksum_or_tag=checksum_or_tag,
            sub_or_seq=sub_or_seq,
        ),
        record=record,
        padded_plain_length=len(plain),
    )


def open_c2s_packet(
    packet: bytes,
    *,
    direction_state: SecureWireDirectionState,
    mac_ring: SecureWireMacRing,
) -> bytes:
    header = parse_game_header(packet[:16])
    cipher = game_packet_body(packet, header, compact_prefix=True)
    if not cipher:
        return b""
    record = mac_ring.pop()
    return direction_state.open(header.opcode, cipher, record)


def pop_complete_compact_packets(buffer: bytearray) -> list[bytes]:
    """Remove complete 13-byte-header Game packets while retaining a partial tail."""
    packets = []
    while len(buffer) >= COMPACT_GAME_HEADER_SIZE:
        header = parse_game_header(buffer[:16])
        if len(buffer) < header.length:
            break
        packets.append(bytes(buffer[: header.length]))
        del buffer[: header.length]
    return packets


__all__ = [
    "ProtectedGamePacket",
    "codec_unit",
    "native_seed0_decode",
    "native_seed0_encode",
    "open_c2s_packet",
    "open_s2c_codec_packet",
    "pop_complete_compact_packets",
    "protect_c2s_packet",
    "protect_s2c_codec_packet",
]
