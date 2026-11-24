from __future__ import annotations

import zlib

from config import CHANNEL_AES_IV, CHANNEL_SESSION_KEY
from native_core_loader import get_native_core_runtime


BLOCK_SIZE = 16


def _gf_mul(a: int, b: int) -> int:
    out = 0
    a &= 0xFF
    b &= 0xFF
    for _ in range(8):
        if b & 1:
            out ^= a
        carry = a & 0x80
        a = (a << 1) & 0xFF
        if carry:
            a ^= 0x1B
        b >>= 1
    return out


def _gf_pow(a: int, power: int) -> int:
    result = 1
    base = a & 0xFF
    while power:
        if power & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        power >>= 1
    return result


def _rotl8(value: int, shift: int) -> int:
    value &= 0xFF
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _generate_sboxes() -> tuple[list[int], list[int]]:
    sbox = [0] * 256
    inv = [0] * 256
    for x in range(256):
        y = 0 if x == 0 else _gf_pow(x, 254)
        s = y ^ _rotl8(y, 1) ^ _rotl8(y, 2) ^ _rotl8(y, 3) ^ _rotl8(y, 4) ^ 0x63
        s &= 0xFF
        sbox[x] = s
        inv[s] = x
    return sbox, inv


SBOX, INV_SBOX = _generate_sboxes()


def _sub_word(word: list[int]) -> list[int]:
    return [SBOX[b] for b in word]


def _rot_word(word: list[int]) -> list[int]:
    return word[1:] + word[:1]


def _xor_words(a: list[int], b: list[int]) -> list[int]:
    return [(x ^ y) & 0xFF for x, y in zip(a, b)]


class AES:
    """Small AES block implementation used to avoid external dependencies."""

    Nb = 4
    Nk = 0
    Nr = 0
    key_size = 0

    def __init__(self, key: bytes):
        if len(key) != self.key_size:
            raise ValueError(f"{type(self).__name__} key must be {self.key_size} bytes, got {len(key)}")
        self.round_keys = self._expand_key(key)

    @classmethod
    def _expand_key(cls, key: bytes) -> list[bytes]:
        words: list[list[int]] = [list(key[i : i + 4]) for i in range(0, len(key), 4)]
        rcon = 1
        total_words = cls.Nb * (cls.Nr + 1)
        i = cls.Nk
        while len(words) < total_words:
            temp = words[-1][:]
            if i % cls.Nk == 0:
                temp = _sub_word(_rot_word(temp))
                temp[0] ^= rcon
                rcon = _gf_mul(rcon, 2)
            elif i % cls.Nk == 4:
                temp = _sub_word(temp)
            words.append(_xor_words(words[i - cls.Nk], temp))
            i += 1
        round_keys: list[bytes] = []
        for r in range(cls.Nr + 1):
            chunk = b"".join(bytes(words[r * 4 + c]) for c in range(4))
            round_keys.append(chunk)
        return round_keys

    @staticmethod
    def _add_round_key(state: bytearray, round_key: bytes) -> None:
        for i in range(16):
            state[i] ^= round_key[i]

    @staticmethod
    def _sub_bytes(state: bytearray) -> None:
        for i, b in enumerate(state):
            state[i] = SBOX[b]

    @staticmethod
    def _inv_sub_bytes(state: bytearray) -> None:
        for i, b in enumerate(state):
            state[i] = INV_SBOX[b]

    @staticmethod
    def _shift_rows(state: bytearray) -> None:
        original = state[:]
        for r in range(4):
            for c in range(4):
                state[r + 4 * c] = original[r + 4 * ((c + r) % 4)]

    @staticmethod
    def _inv_shift_rows(state: bytearray) -> None:
        original = state[:]
        for r in range(4):
            for c in range(4):
                state[r + 4 * c] = original[r + 4 * ((c - r) % 4)]

    @staticmethod
    def _mix_columns(state: bytearray) -> None:
        for c in range(4):
            i = 4 * c
            a0, a1, a2, a3 = state[i : i + 4]
            state[i + 0] = _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3
            state[i + 1] = a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3
            state[i + 2] = a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3)
            state[i + 3] = _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2)

    @staticmethod
    def _inv_mix_columns(state: bytearray) -> None:
        for c in range(4):
            i = 4 * c
            a0, a1, a2, a3 = state[i : i + 4]
            state[i + 0] = _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9)
            state[i + 1] = _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13)
            state[i + 2] = _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11)
            state[i + 3] = _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14)

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("AES block must be 16 bytes")
        state = bytearray(block)
        self._add_round_key(state, self.round_keys[0])
        for round_index in range(1, self.Nr):
            self._sub_bytes(state)
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, self.round_keys[round_index])
        self._sub_bytes(state)
        self._shift_rows(state)
        self._add_round_key(state, self.round_keys[self.Nr])
        return bytes(state)

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("AES block must be 16 bytes")
        state = bytearray(block)
        self._add_round_key(state, self.round_keys[self.Nr])
        for round_index in range(self.Nr - 1, 0, -1):
            self._inv_shift_rows(state)
            self._inv_sub_bytes(state)
            self._add_round_key(state, self.round_keys[round_index])
            self._inv_mix_columns(state)
        self._inv_shift_rows(state)
        self._inv_sub_bytes(state)
        self._add_round_key(state, self.round_keys[0])
        return bytes(state)


class AES128(AES):
    Nk = 4
    Nr = 10
    key_size = 16


class AES256(AES):
    Nk = 8
    Nr = 14
    key_size = 32


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes((x ^ y) & 0xFF for x, y in zip(a, b))


def zero_pad(data: bytes, block_size: int = BLOCK_SIZE, *, min_one_block: bool = False) -> bytes:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    if not data and min_one_block:
        return b"\x00" * block_size
    remainder = len(data) % block_size
    if remainder == 0:
        return data
    return data + (b"\x00" * (block_size - remainder))


def _aes_for_key(key: bytes) -> AES:
    if len(key) == 16:
        return AES128(key)
    if len(key) == 32:
        return AES256(key)
    raise ValueError(f"AES key must be 16 or 32 bytes, got {len(key)}")


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes = CHANNEL_AES_IV) -> bytes:
    if len(iv) != BLOCK_SIZE:
        raise ValueError("CBC IV must be 16 bytes")
    if len(data) % BLOCK_SIZE:
        raise ValueError("CBC input must be block aligned")
    aes = _aes_for_key(key)
    prev = iv
    out = bytearray()
    for offset in range(0, len(data), BLOCK_SIZE):
        block = xor_bytes(data[offset : offset + BLOCK_SIZE], prev)
        enc = aes.encrypt_block(block)
        out.extend(enc)
        prev = enc
    return bytes(out)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes = CHANNEL_AES_IV) -> bytes:
    if len(iv) != BLOCK_SIZE:
        raise ValueError("CBC IV must be 16 bytes")
    if len(data) % BLOCK_SIZE:
        raise ValueError("CBC input must be block aligned")
    aes = _aes_for_key(key)
    prev = iv
    out = bytearray()
    for offset in range(0, len(data), BLOCK_SIZE):
        block = data[offset : offset + BLOCK_SIZE]
        dec = aes.decrypt_block(block)
        out.extend(xor_bytes(dec, prev))
        prev = block
    return bytes(out)


def _aes_ecb_encrypt_python(data: bytes, key: bytes) -> bytes:
    aes = _aes_for_key(key)
    return b"".join(
        aes.encrypt_block(data[offset : offset + BLOCK_SIZE])
        for offset in range(0, len(data), BLOCK_SIZE)
    )


def _aes_ecb_decrypt_python(data: bytes, key: bytes) -> bytes:
    aes = _aes_for_key(key)
    return b"".join(
        aes.decrypt_block(data[offset : offset + BLOCK_SIZE])
        for offset in range(0, len(data), BLOCK_SIZE)
    )


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % BLOCK_SIZE:
        raise ValueError("ECB input must be block aligned")
    data_bytes = bytes(data)
    key_bytes = bytes(key)
    if len(key_bytes) != 16:
        return _aes_ecb_encrypt_python(data_bytes, key_bytes)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "aes128_ecb_encrypt",
        lambda: _aes_ecb_encrypt_python(data_bytes, key_bytes),
        lambda: runtime.module.aes128_ecb_encrypt(data_bytes, key_bytes),
        input_bytes=len(data_bytes) + len(key_bytes),
        output_bytes=len(data_bytes),
    )


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % BLOCK_SIZE:
        raise ValueError("ECB input must be block aligned")
    data_bytes = bytes(data)
    key_bytes = bytes(key)
    if len(key_bytes) != 16:
        return _aes_ecb_decrypt_python(data_bytes, key_bytes)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "aes128_ecb_decrypt",
        lambda: _aes_ecb_decrypt_python(data_bytes, key_bytes),
        lambda: runtime.module.aes128_ecb_decrypt(data_bytes, key_bytes),
        input_bytes=len(data_bytes) + len(key_bytes),
        output_bytes=len(data_bytes),
    )


def aes256_cbc_encrypt(data: bytes, key: bytes = CHANNEL_SESSION_KEY, iv: bytes = CHANNEL_AES_IV) -> bytes:
    return aes_cbc_encrypt(data, key, iv)


def aes256_cbc_decrypt(data: bytes, key: bytes = CHANNEL_SESSION_KEY, iv: bytes = CHANNEL_AES_IV) -> bytes:
    return aes_cbc_decrypt(data, key, iv)


def channel_cipher_key(key: bytes = CHANNEL_SESSION_KEY) -> bytes:
    """Return the effective Channel Rijndael key used by the Stage3 client.

    Dynamic dumps of the client's ``0x0416A610`` Rijndael object after the
    ``00 0C`` response show only the first 16 bytes of our 32-byte material in
    the round-key seed (``USDOF_LOCAL_STAG``), followed by AES-128-style round
    material.  A CBC probe decrypted only the first block correctly; subsequent
    blocks were not XORed with the previous ciphertext, so Channel bodies use
    raw AES-128/Rijndael block mode (ECB-like, no chaining).  The server still
    returns 32 bytes for protocol compatibility, but encrypted Channel bodies
    must use the first 16 bytes.
    """

    if len(key) < 16:
        raise ValueError("Channel session key material must contain at least 16 bytes")
    return key[:16]


def build_encrypted_packed_payload(
    plaintext: bytes,
    *,
    key: bytes = CHANNEL_SESSION_KEY,
    iv: bytes = CHANNEL_AES_IV,
    min_one_block: bool = True,
    zlib_level: int = 6,
) -> bytes:
    """Channel send direction: plaintext -> AES-128/Rijndael block encrypt -> zlib pack."""
    padded = zero_pad(plaintext, BLOCK_SIZE, min_one_block=min_one_block)
    encrypted = aes_ecb_encrypt(padded, key=channel_cipher_key(key))
    return zlib.compress(encrypted, level=zlib_level)


def unpack_decrypt_payload(
    payload: bytes,
    *,
    key: bytes = CHANNEL_SESSION_KEY,
    iv: bytes = CHANNEL_AES_IV,
) -> bytes:
    encrypted = zlib.decompress(payload)
    return aes_ecb_decrypt(encrypted, key=channel_cipher_key(key))


def try_zlib_decompress_modes(data: bytes) -> dict[str, bytes | str]:
    """Useful when 0x03195D20 turns out not to be plain zlib wrapper."""
    result: dict[str, bytes | str] = {}
    modes = {
        "zlib": 15,
        "raw_deflate": -15,
        "gzip": 31,
    }
    for name, wbits in modes.items():
        try:
            result[name] = zlib.decompress(data, wbits=wbits)
        except Exception as exc:  # keep diagnostic text for the compare tool
            result[name] = f"{type(exc).__name__}: {exc}"
    return result
