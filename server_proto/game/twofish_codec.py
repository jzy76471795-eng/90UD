from __future__ import annotations

from dataclasses import dataclass


U32 = 0xFFFFFFFF


def _u32(value: int) -> int:
    return value & U32


def _rol32(value: int, count: int) -> int:
    value &= U32
    count &= 31
    return ((value << count) | (value >> (32 - count))) & U32 if count else value


def _ror32(value: int, count: int) -> int:
    value &= U32
    count &= 31
    return ((value >> count) | (value << (32 - count))) & U32 if count else value


def _u32le(data: bytes | bytearray | memoryview, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _p32le(value: int) -> bytes:
    return (value & U32).to_bytes(4, "little")


_T_TABLE = (
    (
        (0x8, 0x1, 0x7, 0xD, 0x6, 0xF, 0x3, 0x2, 0x0, 0xB, 0x5, 0x9, 0xE, 0xC, 0xA, 0x4),
        (0xE, 0xC, 0xB, 0x8, 0x1, 0x2, 0x3, 0x5, 0xF, 0x4, 0xA, 0x6, 0x7, 0x0, 0x9, 0xD),
        (0xB, 0xA, 0x5, 0xE, 0x6, 0xD, 0x9, 0x0, 0xC, 0x8, 0xF, 0x3, 0x2, 0x4, 0x7, 0x1),
        (0xD, 0x7, 0xF, 0x4, 0x1, 0x2, 0x6, 0xE, 0x9, 0xB, 0x3, 0x0, 0x8, 0x5, 0xC, 0xA),
    ),
    (
        (0x2, 0x8, 0xB, 0xD, 0xF, 0x7, 0x6, 0xE, 0x3, 0x1, 0x9, 0x4, 0x0, 0xA, 0xC, 0x5),
        (0x1, 0xE, 0x2, 0xB, 0x4, 0xC, 0x3, 0x7, 0x6, 0xD, 0xA, 0x5, 0xF, 0x9, 0x0, 0x8),
        (0x4, 0xC, 0x7, 0x5, 0x1, 0x6, 0x9, 0xA, 0x0, 0xE, 0xD, 0x8, 0x2, 0xB, 0x3, 0xF),
        (0xB, 0x9, 0x5, 0x1, 0xC, 0x3, 0xD, 0xE, 0x6, 0x4, 0x7, 0xF, 0x2, 0x0, 0x8, 0xA),
    ),
)


def _ror4_by_1(value: int) -> int:
    return ((value >> 1) | ((value << 3) & 0x08)) & 0x0F


def _make_q_table(t: tuple[tuple[int, ...], ...]) -> tuple[int, ...]:
    q: list[int] = []
    for i in range(0x100):
        ae = i >> 4
        be = i & 0x0F
        ao = ae ^ be
        bo = ae ^ _ror4_by_1(be) ^ ((ae << 3) & 0x08)
        ae = t[0][ao]
        be = t[1][bo]
        ao = ae ^ be
        bo = ae ^ _ror4_by_1(be) ^ ((ae << 3) & 0x08)
        ae = t[2][ao]
        be = t[3][bo]
        q.append(((be << 4) | ae) & 0xFF)
    return tuple(q)


Q0 = _make_q_table(_T_TABLE[0])
Q1 = _make_q_table(_T_TABLE[1])


def _build_mds_tables() -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    tables = [[0] * 0x100 for _ in range(4)]
    for i in range(0x100):
        q = Q0[i]
        qef = (q >> 1) ^ (0xB4 if q & 1 else 0)
        q5b = ((qef >> 1) ^ (0xB4 if qef & 1 else 0) ^ q) & 0xFF
        qef = (qef ^ q5b) & 0xFF
        tables[1][i] = _u32((q << 24) | (q5b << 16) | (qef << 8) | qef)
        tables[3][i] = _u32((q5b << 24) | (qef << 16) | (q << 8) | q5b)

        q = Q1[i]
        qef = (q >> 1) ^ (0xB4 if q & 1 else 0)
        q5b = ((qef >> 1) ^ (0xB4 if qef & 1 else 0) ^ q) & 0xFF
        qef = (qef ^ q5b) & 0xFF
        tables[0][i] = _u32((qef << 24) | (qef << 16) | (q5b << 8) | q)
        tables[2][i] = _u32((qef << 24) | (q << 16) | (qef << 8) | q5b)
    return tuple(tuple(row) for row in tables)  # type: ignore[return-value]


MDS0, MDS1, MDS2, MDS3 = _build_mds_tables()


def _h02(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return MDS0[Q0[Q0[y] ^ key_bytes[8]] ^ key_bytes[0]]


def _h12(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return MDS1[Q0[Q1[y] ^ key_bytes[9]] ^ key_bytes[1]]


def _h22(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return MDS2[Q1[Q0[y] ^ key_bytes[10]] ^ key_bytes[2]]


def _h32(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return MDS3[Q1[Q1[y] ^ key_bytes[11]] ^ key_bytes[3]]


def _h03(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h02(Q1[y] ^ key_bytes[16], key_bytes)


def _h13(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h12(Q1[y] ^ key_bytes[17], key_bytes)


def _h23(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h22(Q0[y] ^ key_bytes[18], key_bytes)


def _h33(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h32(Q0[y] ^ key_bytes[19], key_bytes)


def _h04(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h03(Q1[y] ^ key_bytes[24], key_bytes)


def _h14(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h13(Q0[y] ^ key_bytes[25], key_bytes)


def _h24(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h23(Q0[y] ^ key_bytes[26], key_bytes)


def _h34(y: int, key_bytes: bytes | bytearray | memoryview) -> int:
    return _h33(Q1[y] ^ key_bytes[27], key_bytes)


def _h(y: int, key_bytes: bytes | bytearray | memoryview, key_cycles: int) -> int:
    y &= 0xFF
    if key_cycles == 2:
        return _h02(y, key_bytes) ^ _h12(y, key_bytes) ^ _h22(y, key_bytes) ^ _h32(y, key_bytes)
    if key_cycles == 3:
        return _h03(y, key_bytes) ^ _h13(y, key_bytes) ^ _h23(y, key_bytes) ^ _h33(y, key_bytes)
    if key_cycles == 4:
        return _h04(y, key_bytes) ^ _h14(y, key_bytes) ^ _h24(y, key_bytes) ^ _h34(y, key_bytes)
    raise ValueError(f"unsupported Twofish key cycle count: {key_cycles}")


def _fill_keyed_sboxes(s_vector: bytes | bytearray | memoryview, key_cycles: int) -> tuple[tuple[int, ...], ...]:
    rows = [[0] * 0x100 for _ in range(4)]
    for i in range(0x100):
        if key_cycles == 2:
            rows[0][i] = _h02(i, s_vector)
            rows[1][i] = _h12(i, s_vector)
            rows[2][i] = _h22(i, s_vector)
            rows[3][i] = _h32(i, s_vector)
        elif key_cycles == 3:
            rows[0][i] = _h03(i, s_vector)
            rows[1][i] = _h13(i, s_vector)
            rows[2][i] = _h23(i, s_vector)
            rows[3][i] = _h33(i, s_vector)
        elif key_cycles == 4:
            rows[0][i] = _h04(i, s_vector)
            rows[1][i] = _h14(i, s_vector)
            rows[2][i] = _h24(i, s_vector)
            rows[3][i] = _h34(i, s_vector)
        else:
            raise ValueError(f"unsupported Twofish key cycle count: {key_cycles}")
    return tuple(tuple(row) for row in rows)


@dataclass(frozen=True)
class TwofishState:
    sboxes: tuple[tuple[int, ...], ...]
    round_keys: tuple[int, ...]


def twofish_prepare_key(key: bytes | bytearray | memoryview) -> TwofishState:
    """Prepare a standard Twofish key in the same layout used by USDOF codec3.

    The USDOF codec-index-3 constructor at ``0x02C10A70`` allocates a 0x10A0
    byte state, then calls the key setup path reached through ``0x02C10760`` /
    ``0x02C18EB0``.  The state layout is the standard table-heavy Twofish
    layout: 4 keyed S-box tables (0x1000 bytes) followed by 40 round keys.
    """

    key = bytes(key)
    key_len = len(key)
    if key_len < 0 or key_len > 32:
        raise ValueError("Twofish key length must be in 0..32 bytes")
    work = bytearray(32 + 32 + 4)
    work[:key_len] = key
    key_cycles = (key_len + 7) >> 3
    if key_cycles < 2:
        key_cycles = 2

    round_keys = [0] * 40
    for i in range(0, 40, 2):
        a = _h(i, work, key_cycles)
        b = _rol32(_h(i + 1, memoryview(work)[4:], key_cycles), 8)
        a = _u32(a + b)
        b = _u32(b + a)
        round_keys[i] = a
        round_keys[i + 1] = _rol32(b, 9)

    kptr = 8 * key_cycles
    sptr = 32
    while kptr > 0:
        kptr -= 8
        work[sptr : sptr + 4] = b"\x00" * 4
        work[sptr + 4 : sptr + 12] = work[kptr : kptr + 8]
        t = sptr + 11
        while t > sptr + 3:
            b = work[t]
            bx = ((b << 1) ^ (0x14D if b >> 7 else 0)) & 0xFF
            bxx = ((b >> 1) ^ (0xA6 if b & 1 else 0) ^ bx) & 0xFF
            work[t - 1] ^= bxx
            work[t - 2] ^= bx
            work[t - 3] ^= bxx
            work[t - 4] ^= b
            t -= 1
        sptr += 8

    return TwofishState(
        sboxes=_fill_keyed_sboxes(memoryview(work)[32:], key_cycles),
        round_keys=tuple(round_keys),
    )


def _g0(value: int, state: TwofishState) -> int:
    s = state.sboxes
    return _u32(s[0][value & 0xFF] ^ s[1][(value >> 8) & 0xFF] ^ s[2][(value >> 16) & 0xFF] ^ s[3][(value >> 24) & 0xFF])


def _g1(value: int, state: TwofishState) -> int:
    s = state.sboxes
    return _u32(s[0][(value >> 24) & 0xFF] ^ s[1][value & 0xFF] ^ s[2][(value >> 8) & 0xFF] ^ s[3][(value >> 16) & 0xFF])


def twofish_encrypt_block(state: TwofishState, plain16: bytes | bytearray | memoryview) -> bytes:
    if len(plain16) != 0x10:
        raise ValueError("Twofish block size is 16 bytes")
    k = state.round_keys
    a = _u32le(plain16, 0) ^ k[0]
    b = _u32le(plain16, 4) ^ k[1]
    c = _u32le(plain16, 8) ^ k[2]
    d = _u32le(plain16, 12) ^ k[3]
    for cycle in range(8):
        r = 2 * cycle
        t0 = _g0(a, state)
        t1 = _g1(b, state)
        c = _ror32(c ^ _u32(t0 + t1 + k[8 + 2 * r]), 1)
        d = _u32(_rol32(d, 1) ^ _u32(t0 + 2 * t1 + k[8 + 2 * r + 1]))

        r = 2 * cycle + 1
        t0 = _g0(c, state)
        t1 = _g1(d, state)
        a = _ror32(a ^ _u32(t0 + t1 + k[8 + 2 * r]), 1)
        b = _u32(_rol32(b, 1) ^ _u32(t0 + 2 * t1 + k[8 + 2 * r + 1]))
    return _p32le(c ^ k[4]) + _p32le(d ^ k[5]) + _p32le(a ^ k[6]) + _p32le(b ^ k[7])


def twofish_decrypt_block(state: TwofishState, cipher16: bytes | bytearray | memoryview) -> bytes:
    if len(cipher16) != 0x10:
        raise ValueError("Twofish block size is 16 bytes")
    k = state.round_keys
    a = _u32le(cipher16, 0) ^ k[4]
    b = _u32le(cipher16, 4) ^ k[5]
    c = _u32le(cipher16, 8) ^ k[6]
    d = _u32le(cipher16, 12) ^ k[7]
    for cycle in range(7, -1, -1):
        r = 2 * cycle + 1
        t0 = _g0(a, state)
        t1 = _g1(b, state)
        c = _u32(_rol32(c, 1) ^ _u32(t0 + t1 + k[8 + 2 * r]))
        d = _ror32(d ^ _u32(t0 + 2 * t1 + k[8 + 2 * r + 1]), 1)

        r = 2 * cycle
        t0 = _g0(c, state)
        t1 = _g1(d, state)
        a = _u32(_rol32(a, 1) ^ _u32(t0 + t1 + k[8 + 2 * r]))
        b = _ror32(b ^ _u32(t0 + 2 * t1 + k[8 + 2 * r + 1]), 1)
    return _p32le(c ^ k[0]) + _p32le(d ^ k[1]) + _p32le(a ^ k[2]) + _p32le(b ^ k[3])


def twofish_encrypt(state: TwofishState, data: bytes | bytearray | memoryview) -> bytes:
    data = bytes(data)
    if len(data) % 0x10:
        raise ValueError("Twofish input must be 16-byte aligned")
    return b"".join(twofish_encrypt_block(state, data[i : i + 0x10]) for i in range(0, len(data), 0x10))


def twofish_decrypt(state: TwofishState, data: bytes | bytearray | memoryview) -> bytes:
    data = bytes(data)
    if len(data) % 0x10:
        raise ValueError("Twofish input must be 16-byte aligned")
    return b"".join(twofish_decrypt_block(state, data[i : i + 0x10]) for i in range(0, len(data), 0x10))
