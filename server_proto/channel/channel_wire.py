from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from config import CHANNEL_HEADER_SIZE, MAX_CHANNEL_PAYLOAD


@dataclass(frozen=True)
class ChannelHeader:
    opcode: int
    length: int
    raw: bytes
    flag: int = 0
    tail_or_version: int = 1

    @property
    def payload_len(self) -> int:
        return self.length - CHANNEL_HEADER_SIZE


class ChannelWireError(RuntimeError):
    pass


def read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise ChannelWireError."""
    if n < 0:
        raise ChannelWireError(f"negative read length: {n}")
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ChannelWireError(f"socket closed after {got}/{n} bytes")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def parse_channel_header(raw: bytes) -> ChannelHeader:
    if len(raw) != CHANNEL_HEADER_SIZE:
        raise ChannelWireError(f"channel header must be 0x0B bytes, got {len(raw)}")
    flag = raw[0]
    opcode = raw[1]
    length = struct.unpack_from("<I", raw, 2)[0]
    tail = raw[0x0A]
    if length < CHANNEL_HEADER_SIZE:
        raise ChannelWireError(f"invalid channel total length 0x{length:x}")
    payload_len = length - CHANNEL_HEADER_SIZE
    if payload_len > MAX_CHANNEL_PAYLOAD:
        raise ChannelWireError(f"channel payload too large: 0x{payload_len:x}")
    return ChannelHeader(opcode=opcode, length=length, raw=raw, flag=flag, tail_or_version=tail)


def read_channel_header(sock: socket.socket) -> ChannelHeader:
    return parse_channel_header(read_exact(sock, CHANNEL_HEADER_SIZE))


def build_channel_header(opcode: int, body_len: int, *, flag: int = 0, tail_or_version: int = 1) -> bytes:
    if not 0 <= opcode <= 0xFF:
        raise ValueError(f"opcode out of byte range: {opcode!r}")
    if body_len < 0:
        raise ValueError("body_len cannot be negative")
    total = CHANNEL_HEADER_SIZE + body_len
    # +6..+9 are still unknown; +0x0A must be >= 1 on the recv path.
    return struct.pack("<BBI4sB", flag & 0xFF, opcode & 0xFF, total, b"\x00" * 4, tail_or_version & 0xFF)


def build_channel_packet(opcode: int, body: bytes, *, flag: int = 0, tail_or_version: int = 1) -> bytes:
    return build_channel_header(opcode, len(body), flag=flag, tail_or_version=tail_or_version) + body


def read_channel_packet(sock: socket.socket) -> tuple[ChannelHeader, bytes]:
    header = read_channel_header(sock)
    return header, read_exact(sock, header.payload_len)


def describe_header(header: ChannelHeader) -> str:
    return (
        f"flag=0x{header.flag:02x} opcode=0x{header.opcode:02x} "
        f"length=0x{header.length:x} payload=0x{header.payload_len:x} "
        f"tail=0x{header.tail_or_version:02x}"
    )

