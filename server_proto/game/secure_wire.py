"""Secure-wire v1 primitives and shared-memory layout.

This module is deliberately transport-neutral.  Importing it does not enable
secure-wire or alter the current Stage3 Game path.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = 1
MASTER_KEY_SIZE = 32
LAUNCH_ID_SIZE = 16
MAC_SIZE = 16
NONCE_PREFIX_SIZE = 4
NONCE_SIZE = 12

DIRECTION_C2S = 1
DIRECTION_S2C = 2

MAPPING_MAGIC = b"USDOFSW1"
MAPPING_HEADER_SIZE = 128
RING_HEADER_SIZE = 32
RING_ENTRY_SIZE = 64
DEFAULT_RING_CAPACITY = 256

_HEADER_AUTH_OFFSET = 96
_HEADER_AUTH_END = _HEADER_AUTH_OFFSET + MAC_SIZE
_U32_MAX = 0xFFFFFFFF
_U64_MAX = 0xFFFFFFFFFFFFFFFF


class SecureWireError(RuntimeError):
    """Stable secure-wire validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def _require_bytes(name: str, value: bytes, length: int) -> bytes:
    raw = bytes(value)
    if len(raw) != length:
        raise ValueError("%s must be exactly %s bytes" % (name, length))
    return raw


def hkdf_extract_sha256(salt: bytes, input_key_material: bytes) -> bytes:
    effective_salt = bytes(salt) or bytes(hashlib.sha256().digest_size)
    return hmac.new(effective_salt, bytes(input_key_material), hashlib.sha256).digest()


def hkdf_expand_sha256(pseudorandom_key: bytes, info: bytes, length: int) -> bytes:
    size = int(length)
    if size < 0 or size > 255 * hashlib.sha256().digest_size:
        raise ValueError("HKDF output length is out of range")
    output = bytearray()
    previous = b""
    block_index = 1
    while len(output) < size:
        previous = hmac.new(
            bytes(pseudorandom_key),
            previous + bytes(info) + bytes((block_index,)),
            hashlib.sha256,
        ).digest()
        output.extend(previous)
        block_index += 1
    return bytes(output[:size])


def hkdf_sha256(salt: bytes, input_key_material: bytes, info: bytes, length: int) -> bytes:
    return hkdf_expand_sha256(
        hkdf_extract_sha256(salt, input_key_material),
        info,
        length,
    )


def _rotate_left32(value: int, count: int) -> int:
    value &= _U32_MAX
    return ((value << count) | (value >> (32 - count))) & _U32_MAX


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & _U32_MAX
    state[d] = _rotate_left32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & _U32_MAX
    state[b] = _rotate_left32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & _U32_MAX
    state[d] = _rotate_left32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & _U32_MAX
    state[b] = _rotate_left32(state[b] ^ state[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    key = _require_bytes("key", key, 32)
    nonce = _require_bytes("nonce", nonce, NONCE_SIZE)
    counter = int(counter)
    if counter < 0 or counter > _U32_MAX:
        raise ValueError("ChaCha20 block counter is out of range")
    initial = list(struct.unpack("<4I", b"expand 32-byte k"))
    initial.extend(struct.unpack("<8I", key))
    initial.append(counter)
    initial.extend(struct.unpack("<3I", nonce))
    working = list(initial)
    for _round in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    return struct.pack("<16I", *((working[i] + initial[i]) & _U32_MAX for i in range(16)))


def chacha20_xor(key: bytes, nonce: bytes, data: bytes, *, initial_counter: int = 1) -> bytes:
    key = _require_bytes("key", key, 32)
    nonce = _require_bytes("nonce", nonce, NONCE_SIZE)
    source = bytes(data)
    counter = int(initial_counter)
    block_count = (len(source) + 63) // 64
    if counter < 0 or counter > _U32_MAX or block_count > (_U32_MAX - counter + 1):
        raise ValueError("ChaCha20 counter range is exhausted")
    output = bytearray(len(source))
    for offset in range(0, len(source), 64):
        stream = chacha20_block(key, counter, nonce)
        chunk = source[offset : offset + 64]
        for index, value in enumerate(chunk):
            output[offset + index] = value ^ stream[index]
        counter += 1
    return bytes(output)


@dataclass(frozen=True)
class SecureWireKeys:
    c2s_encryption: bytes
    s2c_encryption: bytes
    c2s_mac: bytes
    s2c_mac: bytes
    c2s_nonce_prefix: bytes
    s2c_nonce_prefix: bytes
    mapping_auth: bytes

    @classmethod
    def derive(cls, master_key: bytes, launch_id: bytes, client_epoch: int) -> "SecureWireKeys":
        master_key = _require_bytes("master_key", master_key, MASTER_KEY_SIZE)
        launch_id = _require_bytes("launch_id", launch_id, LAUNCH_ID_SIZE)
        epoch = int(client_epoch)
        if epoch < 0 or epoch > _U64_MAX:
            raise ValueError("client_epoch is out of range")
        context = b"USDOF secure-wire-v1\x00" + launch_id + struct.pack("<Q", epoch)
        salt = hashlib.sha256(b"USDOF secure-wire-v1 salt\x00" + launch_id + struct.pack("<Q", epoch)).digest()
        prk = hkdf_extract_sha256(salt, master_key)

        def expand(label: bytes, length: int) -> bytes:
            return hkdf_expand_sha256(prk, label + b"\x00" + context, length)

        return cls(
            c2s_encryption=expand(b"c2s encryption", 32),
            s2c_encryption=expand(b"s2c encryption", 32),
            c2s_mac=expand(b"c2s mac", 32),
            s2c_mac=expand(b"s2c mac", 32),
            c2s_nonce_prefix=expand(b"c2s nonce", NONCE_PREFIX_SIZE),
            s2c_nonce_prefix=expand(b"s2c nonce", NONCE_PREFIX_SIZE),
            mapping_auth=expand(b"mapping auth", 32),
        )

    def encryption_key(self, direction: int) -> bytes:
        if int(direction) == DIRECTION_C2S:
            return self.c2s_encryption
        if int(direction) == DIRECTION_S2C:
            return self.s2c_encryption
        raise ValueError("invalid secure-wire direction")

    def mac_key(self, direction: int) -> bytes:
        if int(direction) == DIRECTION_C2S:
            return self.c2s_mac
        if int(direction) == DIRECTION_S2C:
            return self.s2c_mac
        raise ValueError("invalid secure-wire direction")

    def nonce_prefix(self, direction: int) -> bytes:
        if int(direction) == DIRECTION_C2S:
            return self.c2s_nonce_prefix
        if int(direction) == DIRECTION_S2C:
            return self.s2c_nonce_prefix
        raise ValueError("invalid secure-wire direction")


def packet_nonce(keys: SecureWireKeys, direction: int, counter: int) -> bytes:
    counter = int(counter)
    if counter < 0 or counter > _U64_MAX:
        raise ValueError("packet counter is out of range")
    return keys.nonce_prefix(direction) + struct.pack("<Q", counter)


def packet_mac(
    keys: SecureWireKeys,
    *,
    direction: int,
    client_epoch: int,
    counter: int,
    opcode: int,
    ciphertext: bytes,
) -> bytes:
    direction = int(direction)
    if direction not in (DIRECTION_C2S, DIRECTION_S2C):
        raise ValueError("invalid secure-wire direction")
    epoch = int(client_epoch)
    counter = int(counter)
    opcode = int(opcode)
    body = bytes(ciphertext)
    if epoch < 0 or epoch > _U64_MAX or counter < 0 or counter > _U64_MAX:
        raise ValueError("secure-wire epoch or counter is out of range")
    if opcode < 0 or opcode > 0xFFFF or len(body) > _U32_MAX:
        raise ValueError("secure-wire opcode or body length is out of range")
    authenticated = (
        b"USDOF secure-wire-v1 packet\x00"
        + bytes((direction,))
        + struct.pack("<QQHI", epoch, counter, opcode, len(body))
        + body
    )
    return hmac.new(keys.mac_key(direction), authenticated, hashlib.sha256).digest()[:MAC_SIZE]


@dataclass(frozen=True)
class SecureWireRecord:
    direction: int
    client_epoch: int
    counter: int
    opcode: int
    body_length: int
    tag: bytes

    def __post_init__(self) -> None:
        _require_bytes("tag", self.tag, MAC_SIZE)


class SecureWireDirectionState:
    """Strict per-direction counter state for one endpoint."""

    def __init__(self, keys: SecureWireKeys, direction: int, client_epoch: int) -> None:
        if int(direction) not in (DIRECTION_C2S, DIRECTION_S2C):
            raise ValueError("invalid secure-wire direction")
        self.keys = keys
        self.direction = int(direction)
        self.client_epoch = int(client_epoch)
        self.next_send_counter = 0
        self.next_receive_counter = 0

    def seal(self, opcode: int, plaintext: bytes) -> tuple[bytes, SecureWireRecord]:
        counter = self.next_send_counter
        if counter > _U64_MAX:
            raise SecureWireError("COUNTER_EXHAUSTED", "secure-wire send counter is exhausted")
        ciphertext = chacha20_xor(
            self.keys.encryption_key(self.direction),
            packet_nonce(self.keys, self.direction, counter),
            bytes(plaintext),
        )
        tag = packet_mac(
            self.keys,
            direction=self.direction,
            client_epoch=self.client_epoch,
            counter=counter,
            opcode=opcode,
            ciphertext=ciphertext,
        )
        record = SecureWireRecord(
            direction=self.direction,
            client_epoch=self.client_epoch,
            counter=counter,
            opcode=int(opcode),
            body_length=len(ciphertext),
            tag=tag,
        )
        self.next_send_counter += 1
        return ciphertext, record

    def open(self, opcode: int, ciphertext: bytes, record: SecureWireRecord) -> bytes:
        body = bytes(ciphertext)
        checks = (
            (record.direction == self.direction, "WRONG_DIRECTION", "secure-wire direction mismatch"),
            (record.client_epoch == self.client_epoch, "OLD_EPOCH", "secure-wire epoch mismatch"),
            (record.counter == self.next_receive_counter, "REPLAY_OR_GAP", "secure-wire counter mismatch"),
            (record.opcode == int(opcode), "OPCODE_MISMATCH", "secure-wire opcode mismatch"),
            (record.body_length == len(body), "LENGTH_MISMATCH", "secure-wire body length mismatch"),
        )
        for valid, code, message in checks:
            if not valid:
                raise SecureWireError(code, message)
        expected = packet_mac(
            self.keys,
            direction=self.direction,
            client_epoch=self.client_epoch,
            counter=record.counter,
            opcode=opcode,
            ciphertext=body,
        )
        if not hmac.compare_digest(record.tag, expected):
            raise SecureWireError("MAC_MISMATCH", "secure-wire packet authentication failed")
        plaintext = chacha20_xor(
            self.keys.encryption_key(self.direction),
            packet_nonce(self.keys, self.direction, record.counter),
            body,
        )
        self.next_receive_counter += 1
        return plaintext


def required_mapping_size(capacity: int = DEFAULT_RING_CAPACITY) -> int:
    capacity = int(capacity)
    if capacity <= 1 or capacity > 0xFFFF:
        raise ValueError("ring capacity must be between 2 and 65535")
    return MAPPING_HEADER_SIZE + 2 * (RING_HEADER_SIZE + capacity * RING_ENTRY_SIZE)


class SecureWireMacRing:
    """Binary SPSC ring view used by Python tests and the future Game owner."""

    def __init__(self, buffer: Any, offset: int, capacity: int) -> None:
        self.buffer = memoryview(buffer).cast("B")
        self.offset = int(offset)
        self.capacity = int(capacity)
        end = self.offset + RING_HEADER_SIZE + self.capacity * RING_ENTRY_SIZE
        if self.offset < 0 or end > len(self.buffer):
            raise ValueError("ring view is outside the mapping")

    @classmethod
    def initialize(cls, buffer: Any, offset: int, capacity: int) -> "SecureWireMacRing":
        ring = cls(buffer, offset, capacity)
        ring.buffer[ring.offset : ring.offset + RING_HEADER_SIZE + capacity * RING_ENTRY_SIZE] = bytes(
            RING_HEADER_SIZE + capacity * RING_ENTRY_SIZE
        )
        struct.pack_into("<QQIIQ", ring.buffer, ring.offset, 0, 0, capacity, RING_ENTRY_SIZE, 0)
        return ring

    @property
    def write_sequence(self) -> int:
        return struct.unpack_from("<Q", self.buffer, self.offset)[0]

    @property
    def read_sequence(self) -> int:
        return struct.unpack_from("<Q", self.buffer, self.offset + 8)[0]

    def push(self, record: SecureWireRecord) -> None:
        write_sequence = self.write_sequence
        read_sequence = self.read_sequence
        if write_sequence - read_sequence >= self.capacity:
            raise SecureWireError("RING_FULL", "secure-wire MAC ring is full")
        slot = self.offset + RING_HEADER_SIZE + (write_sequence % self.capacity) * RING_ENTRY_SIZE
        published = struct.unpack_from("<Q", self.buffer, slot)[0]
        if published != 0:
            raise SecureWireError("RING_CORRUPT", "secure-wire MAC slot is still published")
        struct.pack_into(
            "<QQQHBBI16s16x",
            self.buffer,
            slot,
            0,
            int(record.client_epoch),
            int(record.counter),
            int(record.opcode),
            int(record.direction),
            0,
            int(record.body_length),
            bytes(record.tag),
        )
        struct.pack_into("<Q", self.buffer, slot, write_sequence + 1)
        struct.pack_into("<Q", self.buffer, self.offset, write_sequence + 1)

    def pop(self) -> SecureWireRecord:
        read_sequence = self.read_sequence
        write_sequence = self.write_sequence
        if read_sequence >= write_sequence:
            raise SecureWireError("RING_EMPTY", "secure-wire MAC ring is empty")
        slot = self.offset + RING_HEADER_SIZE + (read_sequence % self.capacity) * RING_ENTRY_SIZE
        published, epoch, counter, opcode, direction, _reserved, body_length, tag = struct.unpack_from(
            "<QQQHBBI16s", self.buffer, slot
        )
        if published != read_sequence + 1:
            raise SecureWireError("RING_CORRUPT", "secure-wire MAC slot publication is invalid")
        struct.pack_into("<Q", self.buffer, slot, 0)
        struct.pack_into("<Q", self.buffer, self.offset + 8, read_sequence + 1)
        return SecureWireRecord(
            direction=direction,
            client_epoch=epoch,
            counter=counter,
            opcode=opcode,
            body_length=body_length,
            tag=tag,
        )


@dataclass(frozen=True)
class SecureWireMapping:
    client_epoch: int
    launch_id: bytes
    master_key: bytes
    keys: SecureWireKeys
    c2s_ring: SecureWireMacRing
    s2c_ring: SecureWireMacRing


def initialize_mapping(
    buffer: Any,
    *,
    master_key: bytes,
    launch_id: bytes,
    client_epoch: int,
    capacity: int = DEFAULT_RING_CAPACITY,
) -> SecureWireMapping:
    view = memoryview(buffer).cast("B")
    master_key = _require_bytes("master_key", master_key, MASTER_KEY_SIZE)
    launch_id = _require_bytes("launch_id", launch_id, LAUNCH_ID_SIZE)
    mapping_size = required_mapping_size(capacity)
    if len(view) < mapping_size:
        raise ValueError("secure-wire mapping buffer is too small")
    view[:mapping_size] = bytes(mapping_size)
    c2s_offset = MAPPING_HEADER_SIZE
    s2c_offset = c2s_offset + RING_HEADER_SIZE + capacity * RING_ENTRY_SIZE
    struct.pack_into(
        "<8sIIIIQ16s32sIIII",
        view,
        0,
        MAPPING_MAGIC,
        PROTOCOL_VERSION,
        MAPPING_HEADER_SIZE,
        mapping_size,
        capacity,
        int(client_epoch),
        launch_id,
        master_key,
        c2s_offset,
        s2c_offset,
        RING_ENTRY_SIZE,
        RING_HEADER_SIZE,
    )
    keys = SecureWireKeys.derive(master_key, launch_id, client_epoch)
    auth = hmac.new(keys.mapping_auth, bytes(view[:_HEADER_AUTH_OFFSET]), hashlib.sha256).digest()[:MAC_SIZE]
    view[_HEADER_AUTH_OFFSET:_HEADER_AUTH_END] = auth
    c2s_ring = SecureWireMacRing.initialize(view, c2s_offset, capacity)
    s2c_ring = SecureWireMacRing.initialize(view, s2c_offset, capacity)
    return SecureWireMapping(client_epoch, launch_id, master_key, keys, c2s_ring, s2c_ring)


def open_mapping(buffer: Any) -> SecureWireMapping:
    view = memoryview(buffer).cast("B")
    if len(view) < MAPPING_HEADER_SIZE:
        raise SecureWireError("MAPPING_SIZE", "secure-wire mapping is shorter than its header")
    (
        magic,
        version,
        header_size,
        mapping_size,
        capacity,
        epoch,
        launch_id,
        master_key,
        c2s_offset,
        s2c_offset,
        entry_size,
        ring_header_size,
    ) = struct.unpack_from("<8sIIIIQ16s32sIIII", view, 0)
    if magic != MAPPING_MAGIC or version != PROTOCOL_VERSION:
        raise SecureWireError("MAPPING_PROTOCOL", "secure-wire mapping protocol mismatch")
    expected_size = required_mapping_size(capacity)
    expected_s2c = MAPPING_HEADER_SIZE + RING_HEADER_SIZE + capacity * RING_ENTRY_SIZE
    valid_layout = (
        header_size == MAPPING_HEADER_SIZE
        and mapping_size == expected_size
        and mapping_size <= len(view)
        and c2s_offset == MAPPING_HEADER_SIZE
        and s2c_offset == expected_s2c
        and entry_size == RING_ENTRY_SIZE
        and ring_header_size == RING_HEADER_SIZE
    )
    if not valid_layout:
        raise SecureWireError("MAPPING_LAYOUT", "secure-wire mapping layout mismatch")
    keys = SecureWireKeys.derive(master_key, launch_id, epoch)
    expected_auth = hmac.new(
        keys.mapping_auth,
        bytes(view[:_HEADER_AUTH_OFFSET]),
        hashlib.sha256,
    ).digest()[:MAC_SIZE]
    if not hmac.compare_digest(bytes(view[_HEADER_AUTH_OFFSET:_HEADER_AUTH_END]), expected_auth):
        raise SecureWireError("MAPPING_AUTH", "secure-wire mapping authentication failed")
    c2s_ring = SecureWireMacRing(view, c2s_offset, capacity)
    s2c_ring = SecureWireMacRing(view, s2c_offset, capacity)
    for ring in (c2s_ring, s2c_ring):
        stored_capacity, stored_entry_size = struct.unpack_from("<II", view, ring.offset + 16)
        if stored_capacity != capacity or stored_entry_size != RING_ENTRY_SIZE:
            raise SecureWireError("MAPPING_RING", "secure-wire ring header mismatch")
    return SecureWireMapping(epoch, launch_id, master_key, keys, c2s_ring, s2c_ring)


def zero_mutable_buffer(buffer: Any) -> None:
    view = memoryview(buffer).cast("B")
    view[:] = bytes(len(view))


__all__ = [
    "DEFAULT_RING_CAPACITY",
    "DIRECTION_C2S",
    "DIRECTION_S2C",
    "LAUNCH_ID_SIZE",
    "MAC_SIZE",
    "MAPPING_HEADER_SIZE",
    "MASTER_KEY_SIZE",
    "PROTOCOL_VERSION",
    "RING_ENTRY_SIZE",
    "RING_HEADER_SIZE",
    "SecureWireDirectionState",
    "SecureWireError",
    "SecureWireKeys",
    "SecureWireMacRing",
    "SecureWireMapping",
    "SecureWireRecord",
    "chacha20_block",
    "chacha20_xor",
    "hkdf_expand_sha256",
    "hkdf_extract_sha256",
    "hkdf_sha256",
    "initialize_mapping",
    "open_mapping",
    "packet_mac",
    "packet_nonce",
    "required_mapping_size",
    "zero_mutable_buffer",
]
