from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import struct

from channel.channel_crypto import aes_ecb_decrypt, aes_ecb_encrypt
from game.twofish_codec import (
    TwofishState,
    twofish_decrypt_block,
    twofish_encrypt_block,
    twofish_prepare_key,
)
from native_core_loader import get_native_core_runtime


CODEC_UNITS = {
    0: 0x10,
    1: 0x08,
    2: 0x10,
    # Codec index 3 is Twofish-based.  Its vtable contains a 0x20-returning
    # helper at +0x1C, but the encode/decode wrappers call vtable+0x20, which
    # points to 0x02C105F0 and returns the actual transform unit 0x10.
    # USER_STATE (opcode 0x0003) selects this codec.
    3: 0x10,
    4: 0x10,
    5: 0x08,
    6: 0x08,
    7: 0x08,
    8: 0x08,
    9: 0x10,
    10: 0x04,
    11: 0x08,
    12: 0x10,
    13: 0x08,
}


# Codec index 0 is selected by opcode % 14 == 0, including S->C 0x001C
# DUNGEON_INFO. Stage3 factory ctor 0x02C10680 wraps an XTEA object
# (inner 0x02C5B6D0 / encrypt 0x02C5BA80). seed0 material is the 127-char
# alnum string from codec_default_material_seed0.json index 0; init copies
# the first 16 key bytes (block size 8, outer pad unit 0x10, ECB mode 0).
CODEC0_SEED0_KEY_MATERIAL = (
    b"9Ffl3ll1mJH5504C61VeSqfcmSS01jn7C66Fa4UIz95zvy3dW6JtB6nA2AKgqYYpfvawVfObA55H6F7d"
    b"88469g145JlSBHV0AY2719m9e1d0Mzm5hF9GmR3knkrmamc"
)
CODEC0_SEED0_KEY16 = CODEC0_SEED0_KEY_MATERIAL[:16]


# Deterministic codec-index-2 constructor bytes observed in the current
# localtest executable after the codec-seed0 patch:
#
#   DFO_STAGE3_local_127001_obfjump_invalidret_autogame_recv_codecseed0_noti_gate_noskip.exe
#
# Trace evidence:
#   docs/stage3_breakpoint_trace_20260703_080710.md
#   0x02C11060(len=0x3C) -> bytes below -> codec2 state.
#
# This is not a universal USDOF key; it is a convenience vector for the
# controlled localtest build so the prototype can send plaintext probes through
# the same codec2 transform the client uses for flag0/opcode2 USERINFO.
CODEC2_SEED0_KEY_MATERIAL = bytes.fromhex(
    "70 66 b7 58 26 57 e8 fb e0 81 9f 77 c7 fb e6 c1 "
    "cd 7c eb 5e 36 cb a6 75 56 76 bc 28 9c c7 a3 24 "
    "cf f4 07 77 98 96 6d a3 41 5c 17 f0 bc 01 27 06 "
    "e7 7b 07 ba 0e 76 07 29 b1 00 48 03"
)

# Deterministic codec-index-8 constructor bytes for the same seed0 localtest
# build.  Static context:
#
#   factory index 8 -> constructor 0x02C10E40
#   0x02C11060(len=0x10) -> bytes below
#   0x02C10B50 -> 0x02C19420 expands the 16-byte key to a 0x100-byte
#   precomputed XTEA-style key table.
#
# This codec is selected by opcodes where ``opcode % 14 == 8``.  In the
# current Game P1 flow that includes S->C ``0x01BA`` from the ServerS4A12
# get-userinfo follow-up set.  Sending that body raw leaves the client waiting
# after a brief loading flash; encoding it with this seed0 table matches the
# deterministic localtest client used by the prototype.
CODEC8_SEED0_KEY_MATERIAL = bytes.fromhex("81 3b 0c 07 7e d3 d8 29 c8 25 92 29 7f 21 7e 0c")

# Codec index 9 is selected by opcode % 14 == 9, including S->C 0x0017 USER_AREA
# and 0x0025 EXP_NOTIFICATION.  Current Stage3 reverse:
#
# - constructor 0x02C0E000 obtains 0x10 bytes and calls setup 0x02C0DD00;
# - setup 0x02C11E70 accepts 0x10..0x28-byte keys in four-byte steps and uses
#   rounds = key_dwords + 8 (12 rounds for the seed0 16-byte key);
# - block transforms 0x02C12880/0x02C128B0 use the original Anubis tables at
#   0x03CBCFF0..0x03CBE83C, not AES/Rijndael despite the similar T-table shape;
# - vtable +0x10/+0x18 select the forward/inverse 16-byte block schedules.
#
# The old AES-ECB implementation only round-tripped against itself.  Native
# Stage3 decode proved it wrong for every opcode using codec index 9.
CODEC9_SEED0_KEY_MATERIAL = bytes.fromhex("1e a5 0b 57 ae e9 a8 8a 39 1a d8 ea 82 45 89 83")
CODEC9_TABLE_BASE_VA = 0x03CBCFF0
CODEC9_TABLE_COUNT = 6
CODEC9_TABLE_SIZE = 0x400
CODEC9_ROUND_CONSTANT_COUNT = 19

# Codec index 1 is selected by opcode % 14 == 1, including S->C 0x001D.
#
# Current sentinelfix EXE reverse:
# - constructor ``0x02C0E860`` calls ``0x02C11060(len=0x10)``;
# - setup ``0x02C0E570`` calls ``0x02C14190`` -> ``0x02C13750``;
# - encode ``0x02C0E710`` calls block helper ``0x02C146C0``;
# - decode ``0x02C0E7C0`` calls block helper ``0x02C14BE0``;
# - block unit ``0x02C0F3D0`` returns 8.
#
# Protocol notes classify this as a CAST-128-like codec.  For now this module
# only exposes the deterministic seed0 material from the current local
# codecseed0/sentinelfix build; it does not authorize any particular 0x001D
# packet semantics by itself.
CODEC1_SEED0_KEY_MATERIAL = bytes.fromhex("86 45 b3 93 e8 be aa d0 0f ef 66 e8 1c 00 c5 57")

# Deterministic codec-index-3 constructor bytes for the same seed0 localtest
# build.  Dynamic trace:
#
#   logs/stage3_breakpoint_trace_20260703_080710.json
#   hit=3 ret=0x02C10AD8 buf_arg=0x001AF1C4 len_arg=0x20
#
# Static context:
#
#   factory index 3 -> constructor 0x02C10A70
#   constructor calls 0x02C11060(len=0x20), then key setup 0x02C10760
#   vtable +0x20 -> 0x02C105F0 returns transform unit size 0x10
#   vtable +0x1c -> 0x02C10A60 returns codec/key material size 0x20
#   vtable +0x0c/+0x14 wrappers call 0x02C19160 -> 0x02C18EE0 (Twofish enc)
#   vtable +0x18/+0x1c wrappers call 0x02C193F0 -> 0x02C19190 (Twofish dec)
CODEC3_SEED0_KEY_MATERIAL = bytes.fromhex(
    "88 86 da 2a c5 ff 21 7c 99 67 7d ec f9 6f 29 d8 "
    "a2 73 64 97 5b ac cf 51 7a a7 17 13 a9 f5 d3 22"
)

# Codec index 4 is the Rijndael/AES wrapper selected by opcode % 14 == 4.
# In the current codecseed0 executable its constructor obtains a 0x7f-byte
# alnum string, then initializes the inner AES object with a 16-byte key.
CODEC4_SEED0_KEY_MATERIAL = (
    "0fek6dyfU836ay5uEMZH4tBGS73fp15NCstl5h13gto8504FK4894e5m00501YR85UQF1VYQE3XOba81VwcDNFLbqPCikZJz8a0rO1l3X8ZjRM6PCs5q2aC4037rTeV"
).encode("ascii")
CODEC4_SEED0_AES_KEY = CODEC4_SEED0_KEY_MATERIAL[:16]

# Codec index 5 is selected by CREATE_CHARACTER (opcode 0x0005).  Current EXE
# reverse:
#
#   factory index 5 -> constructor 0x02C10330
#   0x02C11060(len=0x0A) -> bytes below
#   vtable +0x10/+0x18 -> 0x02C100C0/0x02C10150
#   inner block routines call 0x02C17CF0/0x02C181F0 with the standard Skipjack
#   F-table at 0x03CC86C8.
CODEC5_SEED0_KEY_MATERIAL = bytes.fromhex("61 a7 80 e6 0e f9 c2 bc fd 1d")
CODEC5_FTABLE_VA = 0x03CC86C8

# Codec index 6 is selected by DELETE_CHARACTER (opcode 0x0006). Current EXE:
#
#   constructor 0x02C0EC30 -> 0x02C11060(len=0x10) -> 0x02C14FF0
#   encode      0x02C0EAE0 -> 0x02C14D50
#   decode      0x02C0EB90 -> 0x02C14EA0
#
# This is a 64-bit MISTY-like transform, but the key schedule and round order
# are the client-specific routines above rather than drop-in standard MISTY1.
CODEC6_SEED0_KEY_MATERIAL = bytes.fromhex("c3 b6 5d 59 3e f2 e2 5d b8 2f 5f 81 b7 33 97 4f")
CODEC6_S7_VA = 0x03CC3938
CODEC6_S9_VA = 0x03CC3B38

# Codec index 7 is selected by opcode % 14 == 7; CHECK_DOUBLE_CHARACTER_NAME
# (0x02B5) uses it.  Current EXE reverse:
#
#   constructor 0x02C0E360 calls 0x02C11060(len=0x38)
#   encode      0x02C0E220 -> 0x02C12FE0(mode 0)
#   decode      0x02C0E2B0 -> 0x02C132C0(mode 0)
#   block enc   0x02C128E0
#   block dec   0x02C12C60
#
# It is Blowfish-like, but the key schedule in this EXE is not the standard
# library Blowfish schedule: it only refreshes P entries while n < 9 and only
# 128 entries per S-box.  Do not replace this with Crypto.Cipher.Blowfish.
CODEC7_SEED0_KEY_MATERIAL = bytes.fromhex(
    "2f 6b 51 41 3d d2 aa cb 17 13 af 9c 44 f2 2b 22 "
    "08 e8 be 4c 23 0d 2f 93 bb 3c 03 3b 96 71 4f d6 "
    "8c 75 ca 0b 3c be 26 95 ff 90 a1 8e 1a 69 d7 ee "
    "38 9c da 4f 17 3c 84 f3"
)
CODEC7_PARRAY_VA = 0x03CBF870
CODEC7_SBOX_VA = 0x03CBF8B8

# Codec index 11 is selected by opcode % 14 == 11, including the current
# S->C 0x0131 primary-object creation candidate for entering Seria room.
#
# Current sentinelfix EXE reverse:
# - constructor ``0x02C0F000`` calls ``0x02C11060(len=0x10)``;
# - key setup ``0x02C0ED10`` validates 16 input bytes and 8-byte unit, then
#   calls ``0x02C153D0`` to build a 0x90-byte inner state;
# - encode ``0x02C0EEB0`` applies ``0x02C15CA0`` for each 8-byte block;
# - decode ``0x02C0EF60`` applies ``0x02C15CC0`` for each 8-byte block;
# - ``0x02C15CA0`` calls table transform ``0x02C159E0(input, output, state+0)``;
# - ``0x02C15CC0`` calls table transform ``0x02C159E0(input, output, state+0x48)``.
#
# This is not DES3/EDE.  2026-07-08 IDA/static + live ReadProcessMemory
# confirmed the current sentinelfix idx11 state below.  Decoding the old DES3
# service wire for S->C 0x0131 with this state yields first bytes ``3e e5``,
# exactly matching x32dbg at ``0x01654D0C`` (u16 object id 0xE53E).
#
# The seed0 material below comes from
# ``analysis_output/us_dfo_unpack/protocol/codec_default_material_seed0.json``
# for the current codecseed0/sentinelfix build. This is codec transport only;
# it does not authorize any 0x0131 semantic body by itself.
CODEC11_SEED0_KEY_MATERIAL = bytes.fromhex("f3 77 a2 6e d7 23 db 50 c9 c3 d1 23 6c c2 5a 39")
CODEC11_TTABLE_VAS = (
    0x03CC4338,
    0x03CC4B38,
    0x03CC5338,
    0x03CC5B38,
    0x03CC6338,
    0x03CC6B38,
    0x03CC7338,
    0x03CC7B38,
)
CODEC11_SEED0_STATE = bytes.fromhex(
    "bb 60 1f d6 fd 7c 25 0a 72 ba e5 7b ea e7 02 6a "
    "26 f5 00 f5 d9 74 fc b8 02 0e 63 54 4f f8 18 b8 "
    "6e 47 97 ae aa 0b 72 e3 d1 5a d0 d7 27 e6 9c b9 "
    "05 4d bb d5 f9 a1 24 90 54 aa 1d e8 0d 6b b3 a9 "
    "50 48 21 2b bd ad c8 cd 50 48 21 2b bd ad c8 cd "
    "bc 4e 92 85 c0 83 f1 20 69 83 48 0d 41 60 d7 93 "
    "60 37 f2 f9 b7 18 90 0b dc b5 0a 33 54 08 9e b2 "
    "53 eb 19 c2 0f ed 22 8f ec 7d a6 92 2e 23 9e f9 "
    "a8 27 01 be b4 84 d4 e7 bb 60 1f d6 fd 7c 25 0a"
)


# Codec index 12 is the Stage3 Noekeon-derived word0-Pi variant
# (opcode % 14 == 12). Frozen Stage3 reverse and native vectors:
# - constructor 0x02C0F7E0 -> GenerateRandomBytes(0x10) then key setup 0x02C0F4F0
# - key setup 0x02C170F0: big-endian 4 dwords; encrypt key = material as-is;
#   decrypt key half = Theta(0, K) written at context +0x10
# - encode 0x02C0F690 -> 0x02C17600 -> 0x02C17270 (Noekeon encrypt, unit 0x10)
# - decode 0x02C0F740 -> 0x02C179E0 -> 0x02C17630 (Noekeon decrypt)
# - RC table 0x03CC8680 matches NESSIE Noekeon RC[0..16], but Pi1/Pi2 rotate
#   state word 0 by 1 rather than reference Noekeon's state word 1
# - 2026-08-01 native S2C decode vectors at 0x02BF2496 verify that distinction
# seed0 material: analysis_output/.../codec_default_material_seed0.json index 12
CODEC12_SEED0_KEY_MATERIAL = bytes.fromhex("5d9a157d663a46fdf5aae1a5ccf1e54d")
CODEC12_RC = (
    0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D, 0x9A,
    0x2F, 0x5E, 0xBC, 0x63, 0xC6, 0x97, 0x35, 0x6A, 0xD4,
)


# Codec index 13 是进入游戏阶段必须补齐的编码之一：
# - ``S->C flag0/opcode=0x000D``（候选 ITEM_LIST）满足 ``0x000D % 14 == 13``；
# - 旧日志中的 ``C->S 0x0117`` 也满足 idx13，补齐后能把后续请求链日志从
#   “raw preserved”推进到可读明文。
#
# 逆向依据（当前 sentinelfix EXE）：
# - constructor ``0x02C0F3F0`` 调 ``0x02C11060(len=0x28)`` 取得 40 字节材料；
# - key setup ``0x02C16DC0``：按大端拆 10 个 dword，后 2 个 dword + 前 8 个
#   dword 进入 ``0x02C16B70``，生成 8 个 schedule dword，并设置默认 rounds=0x80；
# - encode ``0x02C0F280`` -> block helper ``0x02C16F90`` -> ``0x02C16C40``；
# - decode ``0x02C0F330`` -> block helper ``0x02C17040`` -> ``0x02C16D00``；
# - block unit ``0x02C0F3D0`` 返回 8。
#
# 注意：这只解决“当前 codecseed0/sentinelfix 本地客户端”的 idx13 编解码。
# 它不是 ITEM_LIST 包语义的授权；进入赛利亚房间的 S->C 0x000D 是否发送，
# 仍必须先确认当前 active registry、handler 字段读取和状态分支。
CODEC13_SEED0_KEY_MATERIAL = bytes.fromhex(
    "f9 37 eb f7 09 27 ff 51 79 97 ae 82 6d 6b 60 22 "
    "a4 b6 e9 8a ac 23 5a 4f 52 94 1d 09 5d 08 41 59 "
    "7c c2 12 2a 7c a8 ed 36"
)

_STAGE3_CLIENT_EXE_NAME = "DFO.exe"

# 开发树默认：<repo>/USDOF/DFO.exe。分享包 runtime/server 下 parents[2]=runtime，
# 不可靠；实际解析一律走 _codec7_client_exe_path()。
CODEC7_DEFAULT_CLIENT_EXE = (
    Path(__file__).resolve().parents[2] / "USDOF" / _STAGE3_CLIENT_EXE_NAME
)


def _rol32(value: int, count: int) -> int:
    count &= 31
    value &= 0xFFFFFFFF
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF if count else value


def _ror32(value: int, count: int) -> int:
    count &= 31
    value &= 0xFFFFFFFF
    return ((value >> count) | (value << (32 - count))) & 0xFFFFFFFF if count else value


def _rol16(value: int, count: int) -> int:
    count &= 15
    value &= 0xFFFF
    return ((value << count) | (value >> (16 - count))) & 0xFFFF if count else value


def _u32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _p32(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, "little")


def _read_pe_va(path: Path, va: int, size: int) -> bytes:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"not a PE file: {path}")
    pe_offset = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe_offset : pe_offset + 4] != b"PE\x00\x00":
        raise ValueError(f"missing PE signature: {path}")
    file_header = pe_offset + 4
    section_count = int.from_bytes(data[file_header + 2 : file_header + 4], "little")
    optional_size = int.from_bytes(data[file_header + 16 : file_header + 18], "little")
    optional = file_header + 20
    magic = int.from_bytes(data[optional : optional + 2], "little")
    if magic == 0x10B:
        image_base = int.from_bytes(data[optional + 28 : optional + 32], "little")
    elif magic == 0x20B:
        image_base = int.from_bytes(data[optional + 24 : optional + 32], "little")
    else:
        raise ValueError(f"unsupported PE optional header magic 0x{magic:x}: {path}")

    rva = va - image_base if va >= image_base else va
    section_table = optional + optional_size
    for index in range(section_count):
        section = section_table + index * 40
        virtual_size = int.from_bytes(data[section + 8 : section + 12], "little")
        virtual_address = int.from_bytes(data[section + 12 : section + 16], "little")
        raw_size = int.from_bytes(data[section + 16 : section + 20], "little")
        raw_pointer = int.from_bytes(data[section + 20 : section + 24], "little")
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            raw_offset = raw_pointer + (rva - virtual_address)
            return data[raw_offset : raw_offset + size]
    raise ValueError(f"VA 0x{va:08x} not mapped by PE sections: {path}")


def _resolve_env_path(raw: str) -> Path:
    """环境变量路径：相对路径相对 USDOF_PATH_BASE / cwd；禁止依赖写死盘符。"""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    base_raw = (os.environ.get("USDOF_PATH_BASE") or "").strip()
    if base_raw:
        base = Path(base_raw).expanduser()
        if not base.is_absolute():
            base = Path.cwd() / base
    else:
        base = Path.cwd()
    try:
        return (base / p).resolve()
    except OSError:
        return base / p


def _codec7_client_exe_path() -> Path:
    """解析 Stage3 客户端 EXE（codec 表从 PE 读）。

    优先级（均为相对布局拼接，不写死盘符）：
      1. USDOF_CLIENT_EXE / USDOF_CODEC7_CLIENT_EXE（可相对分享根）
      2. data.CLIENT_DIR / <exe>
      3. USDOF_REPO_ROOT/USDOF/<exe>
      4. 分享包：runtime/server/game → 上三级为分享根 /USDOF/<exe>
      5. 开发树：server_proto/game → 上两级为仓库根 /USDOF/<exe>
    """
    override = (
        os.environ.get("USDOF_CLIENT_EXE")
        or os.environ.get("USDOF_CODEC7_CLIENT_EXE")
        or ""
    ).strip()
    if override:
        return _resolve_env_path(override)

    candidates: list[Path] = []
    try:
        from data import CLIENT_DIR  # 延迟导入，避免循环

        candidates.append(Path(CLIENT_DIR) / _STAGE3_CLIENT_EXE_NAME)
    except Exception:
        pass

    repo = (os.environ.get("USDOF_REPO_ROOT") or os.environ.get("USDOF_SHARE_ROOT") or "").strip()
    if repo:
        candidates.append(_resolve_env_path(repo) / "USDOF" / _STAGE3_CLIENT_EXE_NAME)

    here = Path(__file__).resolve()
    # 分享包：.../runtime/server/game/game_codec.*
    #   parents[0]=game, [1]=server, [2]=runtime, [3]=share
    if (
        len(here.parents) >= 4
        and here.parents[1].name.lower() == "server"
        and here.parents[2].name.lower() == "runtime"
    ):
        candidates.append(here.parents[3] / "USDOF" / _STAGE3_CLIENT_EXE_NAME)
    # 开发树：.../server_proto/game/game_codec.* → parents[2]=repo
    candidates.append(here.parents[2] / "USDOF" / _STAGE3_CLIENT_EXE_NAME)
    candidates.append(CODEC7_DEFAULT_CLIENT_EXE)

    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    # 全部不存在时仍返回首选，让上层抛出可读 FileNotFoundError
    return candidates[0] if candidates else CODEC7_DEFAULT_CLIENT_EXE


@dataclass(frozen=True)
class DecodeResult:
    ok: bool
    codec_index: int
    plain: bytes
    note: str


@dataclass(frozen=True)
class PivPlain:
    """Decoded plaintext shape for ``ENUM_CMDPACKET_PIV``.

    The live Stage3 client builds this command from the last accepted
    server-to-client notipacket.  Static path:

    ``0x019264F0 -> 0x02BF08A0(0x013E) -> write_u8(flag) ->
    write_u16(opcode) -> send``.

    The final dword is still under reverse analysis.  It is observed as
    ``59 a7 2c cf`` for the current minimal ``flag0/opcode1`` special
    ChannelInfo packet and is treated as an opaque PIV/tag until the exact
    producer is proven.
    """

    echoed_flag: int
    echoed_opcode: int
    tag: int | None
    raw: bytes

    @property
    def tag_hex(self) -> str:
        return "<none>" if self.tag is None else f"0x{self.tag:08x}"


def piv_md5_fold_tag(data: bytes) -> int:
    """Return the 32-bit PIV payload tag used by selected Game commands.

    Dynamic trace of client ``ENUM_CMDPACKET_PIV`` proved the following send
    hook before ``GamePacket_WrapEncodeChecksum``:

    ``0x01924610 GamePacket_AppendPivPayloadTag_BeforeSend``
    ``-> 0x004F7D60 GamePIV_MD5FoldPayloadTag``
    ``-> 0x02BF05A0 GamePacketBuilder_WriteU32``

    ``0x004F7D60`` hashes the current payload bytes with the MD5-like helper
    at ``0x02C168E0`` and folds the 16 digest bytes into four bytes:

    ``b0^b13^b8^b10^0x81``
    ``b1^b9^b5^b12^0x78``
    ``b2^b6^b14^b4^0x1A``
    ``b3^b15^b11^b7^0xBF``

    For the live PIV payload ``00 01 00`` this yields bytes
    ``59 a7 2c cf`` (little-endian integer ``0xCF2CA759``), exactly matching
    the decoded client packet body ``00 01 00 59 a7 2c cf 00``.
    """

    digest = hashlib.md5(data).digest()
    folded = bytes(
        (
            digest[0] ^ digest[13] ^ digest[8] ^ digest[10] ^ 0x81,
            digest[1] ^ digest[9] ^ digest[5] ^ digest[12] ^ 0x78,
            digest[2] ^ digest[6] ^ digest[14] ^ digest[4] ^ 0x1A,
            digest[3] ^ digest[15] ^ digest[11] ^ digest[7] ^ 0xBF,
        )
    )
    return int.from_bytes(folded, "little")


def append_piv_md5_fold_tag(data: bytes) -> bytes:
    """Append the Stage3 PIV MD5-fold tag as a little-endian dword."""

    return data + piv_md5_fold_tag(data).to_bytes(4, "little")


def _codec2_build_state_python(key_material: bytes) -> bytes:
    """Build codec-index-2's 0x4C-byte RC6-like state from constructor bytes.

    Static/dynamic context:

    - factory index 2 constructs object ``0x02C0FAC0``;
    - constructor calls ``0x02C11060`` for ``0x3C`` random bytes;
    - method ``0x02C0F8C0`` copies the first ``0x4C`` bytes into the inner
      state and then calls ``0x02C17A10(inner_state, 0x20)``;
    - ``0x02C17A10`` uses only the first 0x20 bytes as key material and fills
      the byte table at ``state[0x20:0x4C]``.

    The implementation below is a direct translation of ``0x02C17A10``.  It is
    intentionally byte-for-byte oriented because the live routine stores only
    the low byte of the mixed S-table values.  A trace vector from
    ``stage3_breakpoint_trace_20260703_052908`` is covered by unit tests.
    """

    if len(key_material) < 0x20:
        raise ValueError("codec2 key material must contain at least 0x20 bytes")

    key_len = 0x20
    state = bytearray(0x4C)
    state[: min(len(key_material), 0x4C)] = key_material[:0x4C]

    # Pack key bytes into little-endian dwords exactly as the routine's reverse
    # byte loop does:
    #
    #   for edx = key_len-1 .. 0:
    #       L[edx>>2] = key[edx] + (L[edx>>2] << 8)
    c = (key_len + 3) // 4
    l_words = [0] * c
    for pos in range(key_len - 1, -1, -1):
        idx = pos >> 2
        l_words[idx] = (state[pos] + ((l_words[idx] << 8) & 0xFFFFFFFF)) & 0xFFFFFFFF

    # Byte S-table seed: state[0x20] = 0x63; each following byte subtracts
    # 0x47 from the previous byte, modulo 256.
    state[0x20] = 0x63
    for i in range(1, 0x2C):
        state[0x20 + i] = (state[0x1F + i] - 0x47) & 0xFF

    a = 0  # live ESI, then low byte of mixed S value
    b = 0  # live EDI / mixed L dword
    i = 0
    j = 0
    for _ in range(max(c, 0x2C) * 3):
        s_val = state[0x20 + j]
        mixed_s = _rol32((s_val + a + b) & 0xFFFFFFFF, 3)
        state[0x20 + j] = mixed_s & 0xFF
        a = state[0x20 + j]

        mixed_l = _rol32((l_words[i] + a + b) & 0xFFFFFFFF, (a + b) & 0x1F)
        l_words[i] = mixed_l
        b = mixed_l

        j = (j + 1) % 0x2C
        i = (i + 1) % c

    return bytes(state)


def codec2_build_state_from_key(key_material: bytes) -> bytes:
    """Build codec-index-2 state while preserving the traced Python result."""

    key_bytes = bytes(key_material)
    if len(key_bytes) < 0x20:
        raise ValueError("codec2 key material must contain at least 0x20 bytes")
    runtime = get_native_core_runtime()
    return runtime.execute(
        "codec2_build_state",
        lambda: _codec2_build_state_python(key_bytes),
        lambda: runtime.module.codec2_build_state(key_bytes),
        input_bytes=len(key_bytes),
        output_bytes=0x4C,
    )


def _codec2_encode_block(state: bytes, block16: bytes) -> bytes:
    """Encode one 16-byte block using codec-index-2 method ``0x02C17B10``."""

    if len(state) < 0x4C:
        raise ValueError("codec2 state must be at least 0x4C bytes")
    if len(block16) != 0x10:
        raise ValueError("codec2 block size is 0x10")

    src0 = _u32(block16, 0)
    src1 = _u32(block16, 4)
    src2 = _u32(block16, 8)
    src3 = _u32(block16, 12)

    local_m8 = src0
    local_m4 = src2
    ebx = (src3 + state[0x21]) & 0xFFFFFFFF
    edi = (src1 + state[0x20]) & 0xFFFFFFFF
    s_index = 0x23

    for _ in range(0x14):
        edx = _rol32((edi * ((edi + edi + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
        eax = _rol32((ebx * ((ebx + ebx + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)

        esi = _rol32((local_m8 ^ edx) & 0xFFFFFFFF, eax & 0x1F)
        esi = (esi + state[s_index - 1]) & 0xFFFFFFFF

        new_edi = _rol32((local_m4 ^ eax) & 0xFFFFFFFF, edx & 0x1F)
        new_edi = (new_edi + state[s_index]) & 0xFFFFFFFF

        local_m8 = edi
        local_m4 = ebx
        ebx = esi
        edi = new_edi
        s_index += 2

    out0 = (local_m8 + state[0x4A]) & 0xFFFFFFFF
    out1 = edi
    out2 = (local_m4 + state[0x4B]) & 0xFFFFFFFF
    out3 = ebx
    return b"".join((_p32(out0), _p32(out1), _p32(out2), _p32(out3)))


def _codec2_decode_block(state: bytes, block16: bytes) -> bytes:
    """Decode one 16-byte block using codec-index-2 method ``0x02C17BC0``."""

    if len(state) < 0x4C:
        raise ValueError("codec2 state must be at least 0x4C bytes")
    if len(block16) != 0x10:
        raise ValueError("codec2 block size is 0x10")

    edx = (_u32(block16, 8) - state[0x4B]) & 0xFFFFFFFF
    eax = _u32(block16, 4)
    esi = _u32(block16, 12)
    ecx = (_u32(block16, 0) - state[0x4A]) & 0xFFFFFFFF
    ebx = 0x28

    local_m4 = ebx
    local_m8 = 0
    local_m12 = 0
    local_arg_c = eax

    while True:
        s_byte = state[ebx + 0x21]
        edi = local_arg_c

        t = _rol32((edx * ((edx + edx + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
        local_m8 = edx
        u = _rol32((ecx * ((ecx + ecx + 1) & 0xFFFFFFFF)) & 0xFFFFFFFF, 5)
        local_m12 = ecx

        edi = (edi - s_byte) & 0xFFFFFFFF
        ebx = (local_m4 - 2) & 0xFFFFFFFF
        edi = _ror32(edi, u & 0x1F)
        edi ^= t
        local_arg_c = edi

        esi = (esi - state[ebx + 0x22]) & 0xFFFFFFFF
        esi = _ror32(esi, t & 0x1F)
        local_m4 = ebx
        esi ^= u
        ecx = esi

        if ebx < 2:
            break

        eax = local_m12
        esi = local_m8
        edx = local_arg_c
        local_arg_c = eax

    out0 = ecx
    out1 = (local_m12 - state[0x20]) & 0xFFFFFFFF
    out2 = local_arg_c
    out3 = (local_m8 - state[0x21]) & 0xFFFFFFFF
    return b"".join((_p32(out0), _p32(out1), _p32(out2), _p32(out3)))


def _codec2_encode_python(state: bytes, data: bytes) -> bytes:
    return b"".join(
        _codec2_encode_block(state, data[i : i + 0x10])
        for i in range(0, len(data), 0x10)
    )


def _codec2_decode_python(state: bytes, data: bytes) -> bytes:
    return b"".join(
        _codec2_decode_block(state, data[i : i + 0x10])
        for i in range(0, len(data), 0x10)
    )


def codec2_encode(state: bytes, data: bytes) -> bytes:
    """Encode a 0x10-aligned buffer with codec index 2."""

    if len(data) % 0x10:
        raise ValueError("codec2 input must be 0x10-byte aligned")
    state_bytes = bytes(state)
    data_bytes = bytes(data)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "codec2_encode",
        lambda: _codec2_encode_python(state_bytes, data_bytes),
        lambda: runtime.module.codec2_encode(state_bytes, data_bytes),
        input_bytes=len(state_bytes) + len(data_bytes),
        output_bytes=len(data_bytes),
    )


def codec2_decode(state: bytes, data: bytes) -> bytes:
    """Decode a 0x10-aligned buffer with codec index 2."""

    if len(data) % 0x10:
        raise ValueError("codec2 input must be 0x10-byte aligned")
    state_bytes = bytes(state)
    data_bytes = bytes(data)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "codec2_decode",
        lambda: _codec2_decode_python(state_bytes, data_bytes),
        lambda: runtime.module.codec2_decode(state_bytes, data_bytes),
        input_bytes=len(state_bytes) + len(data_bytes),
        output_bytes=len(data_bytes),
    )


def codec2_seed0_state() -> bytes:
    """Return the deterministic codec2 state for the current seed0 localtest."""

    return codec2_build_state_from_key(CODEC2_SEED0_KEY_MATERIAL)


def codec2_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x10-aligned plaintext buffer for flag0/opcode2 probes."""

    return codec2_encode(codec2_seed0_state(), plain)


def codec2_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x10-aligned ciphertext buffer with the seed0 localtest state."""

    return codec2_decode(codec2_seed0_state(), encoded)


@lru_cache(maxsize=4)
def codec7_client_constants(client_exe: str | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Read codec7's initial P/S constants from the current sentinelfix EXE."""

    path = Path(client_exe) if client_exe else _codec7_client_exe_path()
    p_bytes = _read_pe_va(path, CODEC7_PARRAY_VA, 18 * 4)
    s_bytes = _read_pe_va(path, CODEC7_SBOX_VA, 1024 * 4)
    if len(p_bytes) != 18 * 4 or len(s_bytes) != 1024 * 4:
        raise ValueError(f"short codec7 constants read from {path}")
    p_words = struct.unpack("<18I", p_bytes)
    s_words = struct.unpack("<1024I", s_bytes)
    return p_words, s_words


def _codec7_f(state: tuple[int, ...] | list[int], value: int) -> int:
    value &= 0xFFFFFFFF
    return (
        state[790 + (value & 0xFF)]
        + (
            state[534 + ((value >> 8) & 0xFF)]
            ^ ((state[22 + ((value >> 24) & 0xFF)] + state[278 + ((value >> 16) & 0xFF)]) & 0xFFFFFFFF)
        )
    ) & 0xFFFFFFFF


def _codec7_encrypt_words(state: tuple[int, ...] | list[int], left: int, right: int) -> tuple[int, int]:
    x = (left ^ state[4]) & 0xFFFFFFFF
    y = (right ^ state[5] ^ _codec7_f(state, x)) & 0xFFFFFFFF
    for p_index in range(6, 20, 2):
        x = (state[p_index] ^ _codec7_f(state, y) ^ x) & 0xFFFFFFFF
        y = (state[p_index + 1] ^ _codec7_f(state, x) ^ y) & 0xFFFFFFFF
    return (y ^ state[21]) & 0xFFFFFFFF, (state[20] ^ _codec7_f(state, y) ^ x) & 0xFFFFFFFF


def _codec7_decrypt_words(state: tuple[int, ...] | list[int], left: int, right: int) -> tuple[int, int]:
    x = (left ^ state[21]) & 0xFFFFFFFF
    y = (right ^ state[20] ^ _codec7_f(state, x)) & 0xFFFFFFFF
    for p_index in range(19, 5, -2):
        x = (state[p_index] ^ _codec7_f(state, y) ^ x) & 0xFFFFFFFF
        y = (state[p_index - 1] ^ _codec7_f(state, x) ^ y) & 0xFFFFFFFF
    return (y ^ state[4]) & 0xFFFFFFFF, (state[5] ^ _codec7_f(state, y) ^ x) & 0xFFFFFFFF


def codec7_build_state_from_key(key_material: bytes, client_exe: str | None = None) -> tuple[int, ...]:
    """Build codec-index-7's seed0 state exactly like current EXE 0x02C135A0."""

    if not key_material:
        raise ValueError("codec7 key material must not be empty")
    key = bytes(key_material[:0x38])
    p_words, s_words = codec7_client_constants(client_exe)
    state = [0, 0, 0, 0, *p_words, *s_words]
    key_offset = 0
    for p_index in range(18):
        material = 0
        for _ in range(4):
            material = ((material << 8) | key[key_offset]) & 0xFFFFFFFF
            key_offset += 1
            if key_offset == len(key):
                key_offset = 0
        state[4 + p_index] ^= material

    left = 0
    right = 0
    p_written = 0
    out_index = 4
    while True:
        left, right = _codec7_encrypt_words(state, left, right)
        state[out_index] = left
        state[out_index + 1] = right
        p_written += 2
        out_index += 2
        if not (p_written < 9):
            break

    for box_index in range(4):
        base = 22 + box_index * 256
        for entry_index in range(0, 128, 2):
            left, right = _codec7_encrypt_words(state, left, right)
            state[base + entry_index] = left
            state[base + entry_index + 1] = right
    return tuple(value & 0xFFFFFFFF for value in state)


def _codec7_encode_block(state: tuple[int, ...], block8: bytes) -> bytes:
    if len(state) != 1046:
        raise ValueError("codec7 state must contain 1046 dwords")
    if len(block8) != 0x08:
        raise ValueError("codec7 block size is 0x08")
    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    left, right = _codec7_encrypt_words(state, left, right)
    return left.to_bytes(4, "big") + right.to_bytes(4, "big")


def _codec7_decode_block(state: tuple[int, ...], block8: bytes) -> bytes:
    if len(state) != 1046:
        raise ValueError("codec7 state must contain 1046 dwords")
    if len(block8) != 0x08:
        raise ValueError("codec7 block size is 0x08")
    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    left, right = _codec7_decrypt_words(state, left, right)
    return left.to_bytes(4, "big") + right.to_bytes(4, "big")


def codec7_encode(state: tuple[int, ...], data: bytes) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 7."""

    if len(data) % 0x08:
        raise ValueError("codec7 input must be 0x08-byte aligned")
    return b"".join(_codec7_encode_block(state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec7_decode(state: tuple[int, ...], data: bytes) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 7."""

    if len(data) % 0x08:
        raise ValueError("codec7 input must be 0x08-byte aligned")
    return b"".join(_codec7_decode_block(state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


@lru_cache(maxsize=1)
def codec7_seed0_state() -> tuple[int, ...]:
    """Return the deterministic codec7 state for the current sentinelfix seed0 EXE."""

    return codec7_build_state_from_key(CODEC7_SEED0_KEY_MATERIAL)


def codec7_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 7`` probes."""

    return codec7_encode(codec7_seed0_state(), plain)


def codec7_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with the seed0 localtest state."""

    return codec7_decode(codec7_seed0_state(), encoded)


@lru_cache(maxsize=1)
def codec5_ftable(client_exe: Path | None = None) -> bytes:
    """Read codec-index-5's Skipjack F-table from the current sentinelfix EXE."""

    path = Path(client_exe) if client_exe else _codec7_client_exe_path()
    table = _read_pe_va(path, CODEC5_FTABLE_VA, 0x100)
    if len(table) != 0x100:
        raise ValueError("codec5 F-table must contain 0x100 bytes")
    return table


def _codec5_g(ftable: bytes, key_material: bytes, round_index: int, value: int) -> int:
    g1 = (value >> 8) & 0xFF
    g2 = value & 0xFF
    base = (4 * (round_index - 1)) % 10
    for step in range(4):
        g1, g2 = g2, ftable[g2 ^ key_material[(base + step) % 10]] ^ g1
    return ((g1 & 0xFF) << 8) | (g2 & 0xFF)


def _codec5_g_inv(ftable: bytes, key_material: bytes, round_index: int, value: int) -> int:
    g1 = (value >> 8) & 0xFF
    g2 = value & 0xFF
    base = (4 * (round_index - 1)) % 10
    for step in range(3, -1, -1):
        old_g2 = g1
        old_g1 = ftable[g1 ^ key_material[(base + step) % 10]] ^ g2
        g1, g2 = old_g1 & 0xFF, old_g2 & 0xFF
    return ((g1 & 0xFF) << 8) | (g2 & 0xFF)


def _codec5_encrypt_block(ftable: bytes, key_material: bytes, block8: bytes) -> bytes:
    if len(block8) != 0x08:
        raise ValueError("codec5 block size is 0x08")
    w1 = int.from_bytes(block8[0:2], "big")
    w2 = int.from_bytes(block8[2:4], "big")
    w3 = int.from_bytes(block8[4:6], "big")
    w4 = int.from_bytes(block8[6:8], "big")
    for round_index in range(1, 33):
        if round_index <= 8 or 17 <= round_index <= 24:
            g = _codec5_g(ftable, key_material, round_index, w1)
            w1, w2, w3, w4 = (g ^ w4 ^ round_index) & 0xFFFF, g, w2, w3
        else:
            g = _codec5_g(ftable, key_material, round_index, w1)
            w1, w2, w3, w4 = w4, g, (w1 ^ w2 ^ round_index) & 0xFFFF, w3
    return b"".join(word.to_bytes(2, "big") for word in (w1, w2, w3, w4))


def _codec5_decrypt_block(ftable: bytes, key_material: bytes, block8: bytes) -> bytes:
    if len(block8) != 0x08:
        raise ValueError("codec5 block size is 0x08")
    w1 = int.from_bytes(block8[0:2], "big")
    w2 = int.from_bytes(block8[2:4], "big")
    w3 = int.from_bytes(block8[4:6], "big")
    w4 = int.from_bytes(block8[6:8], "big")
    for round_index in range(32, 0, -1):
        old_w1 = _codec5_g_inv(ftable, key_material, round_index, w2)
        if round_index <= 8 or 17 <= round_index <= 24:
            old_w2 = w3
            old_w3 = w4
            old_w4 = (w1 ^ w2 ^ round_index) & 0xFFFF
        else:
            old_w2 = (w3 ^ old_w1 ^ round_index) & 0xFFFF
            old_w3 = w4
            old_w4 = w1
        w1, w2, w3, w4 = old_w1, old_w2, old_w3, old_w4
    return b"".join(word.to_bytes(2, "big") for word in (w1, w2, w3, w4))


def codec5_encode(key_material: bytes, data: bytes, *, client_exe: Path | None = None) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 5."""

    if len(key_material) != 0x0A:
        raise ValueError("codec5 key material must be exactly 0x0a bytes")
    if len(data) % 0x08:
        raise ValueError("codec5 input must be 0x08-byte aligned")
    ftable = codec5_ftable(client_exe)
    return b"".join(_codec5_encrypt_block(ftable, key_material, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec5_decode(key_material: bytes, data: bytes, *, client_exe: Path | None = None) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 5."""

    if len(key_material) != 0x0A:
        raise ValueError("codec5 key material must be exactly 0x0a bytes")
    if len(data) % 0x08:
        raise ValueError("codec5 input must be 0x08-byte aligned")
    ftable = codec5_ftable(client_exe)
    return b"".join(_codec5_decrypt_block(ftable, key_material, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec5_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for CREATE_CHARACTER probes."""

    return codec5_encode(CODEC5_SEED0_KEY_MATERIAL, plain)


def codec5_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned CREATE_CHARACTER ciphertext buffer."""

    return codec5_decode(CODEC5_SEED0_KEY_MATERIAL, encoded)


@lru_cache(maxsize=4)
def codec6_sboxes(client_exe: str | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Read codec6 S7/S9 tables from the current sentinelfix EXE."""

    path = Path(client_exe) if client_exe else _codec7_client_exe_path()
    s7_bytes = _read_pe_va(path, CODEC6_S7_VA, 128 * 4)
    s9_bytes = _read_pe_va(path, CODEC6_S9_VA, 512 * 4)
    if len(s7_bytes) != 128 * 4 or len(s9_bytes) != 512 * 4:
        raise ValueError(f"short codec6 constants read from {path}")
    return (
        tuple(value & 0xFFFF for value in struct.unpack("<128I", s7_bytes)),
        tuple(value & 0xFFFF for value in struct.unpack("<512I", s9_bytes)),
    )


def codec6_build_state_from_key(key_material: bytes) -> tuple[int, ...]:
    """Build codec-index-6 state exactly like current EXE 0x02C14FF0."""

    if len(key_material) != 0x10:
        raise ValueError("codec6 key material must be exactly 0x10 bytes")
    key_words = [(key_material[index * 2] << 8) | key_material[index * 2 + 1] for index in range(8)]
    masks = (0x0123, 0x4567, 0x89AB, 0xCDEF, 0xFEDC, 0xBA98, 0x7654, 0x3210)
    mixed_words = [(word ^ mask) & 0xFFFF for word, mask in zip(key_words, masks)]
    state = [0] * 64
    for index in range(8):
        cursor = index - 3
        state[index] = _rol16(key_words[index], 1)
        state[8 + index] = mixed_words[(cursor - 3) & 7]
        state[16 + index] = _rol16(key_words[(cursor + 4) & 7], 5)
        state[24 + index] = _rol16(key_words[cursor & 7], 8)
        state[32 + index] = _rol16(key_words[(cursor + 1) & 7], 13)
        state[40 + index] = mixed_words[(cursor - 1) & 7]
        state[48 + index] = mixed_words[(cursor - 2) & 7]
        state[56 + index] = mixed_words[(cursor + 2) & 7]
    return tuple(state)


def _codec6_fi(s7: tuple[int, ...], s9: tuple[int, ...], value: int, key: int) -> int:
    value &= 0xFFFF
    key &= 0xFFFF
    d7 = value & 0x7F
    d9 = s9[(value >> 7) & 0x1FF] ^ d7
    d7 = (s7[d7] ^ (d9 & 0x7F) ^ (key >> 9)) & 0x7F
    d9 = s9[(key & 0x1FF) ^ d9] ^ d7
    d7 = (s7[d7] ^ (d9 & 0x7F)) & 0x7F
    return ((d7 << 9) | (d9 & 0x1FF)) & 0xFFFF


def _codec6_f(s7: tuple[int, ...], s9: tuple[int, ...], state: tuple[int, ...], value: int, index: int) -> int:
    low = value & 0xFFFF
    high = (value >> 16) & 0xFFFF
    first = _codec6_fi(s7, s9, high ^ state[16 + index], state[40 + index]) ^ low
    second = _codec6_fi(s7, s9, low ^ state[24 + index], state[48 + index]) ^ first
    third = _codec6_fi(s7, s9, first ^ state[32 + index], state[56 + index]) ^ second
    return (((second & 0xFFFF) << 16) | (third & 0xFFFF)) & 0xFFFFFFFF


def _codec6_fl(value: int, mask: int, or_word: int) -> int:
    high = (value >> 16) & 0xFFFF
    low = (_rol16(mask & high, 1) ^ value) & 0xFFFF
    word = (or_word | low) & 0xFFFF
    return ((_rol16(word, 1) ^ high) << 16 | low) & 0xFFFFFFFF


def _codec6_encode_block(s7: tuple[int, ...], s9: tuple[int, ...], state: tuple[int, ...], block8: bytes) -> bytes:
    if len(block8) != 0x08:
        raise ValueError("codec6 block size is 0x08")
    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    ptr = 8
    index = 0
    while index <= 7:
        transformed = _codec6_fl(left, state[index], state[ptr])
        ptr += 1
        right ^= _codec6_f(s7, s9, state, transformed, index)
        index += 1
        transformed = _codec6_fl(_codec6_f(s7, s9, state, right, index), state[index], state[ptr])
        ptr += 1
        left ^= transformed
        index += 1
    return (left & 0xFFFFFFFF).to_bytes(4, "big") + (right & 0xFFFFFFFF).to_bytes(4, "big")


def _codec6_decode_block(s7: tuple[int, ...], s9: tuple[int, ...], state: tuple[int, ...], block8: bytes) -> bytes:
    if len(block8) != 0x08:
        raise ValueError("codec6 block size is 0x08")
    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    ptr = 15
    index = 7
    while index >= 0:
        transformed = _codec6_fl(_codec6_f(s7, s9, state, right, index), state[index], state[ptr])
        ptr -= 1
        left ^= transformed
        index -= 1
        transformed = _codec6_fl(left, state[index], state[ptr])
        ptr -= 1
        right ^= _codec6_f(s7, s9, state, transformed, index)
        index -= 1
    return (left & 0xFFFFFFFF).to_bytes(4, "big") + (right & 0xFFFFFFFF).to_bytes(4, "big")


def codec6_encode(key_material: bytes, data: bytes, *, client_exe: str | None = None) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 6."""

    if len(data) % 0x08:
        raise ValueError("codec6 input must be 0x08-byte aligned")
    s7, s9 = codec6_sboxes(client_exe)
    state = codec6_build_state_from_key(key_material)
    return b"".join(_codec6_encode_block(s7, s9, state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec6_decode(key_material: bytes, data: bytes, *, client_exe: str | None = None) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 6."""

    if len(data) % 0x08:
        raise ValueError("codec6 input must be 0x08-byte aligned")
    s7, s9 = codec6_sboxes(client_exe)
    state = codec6_build_state_from_key(key_material)
    return b"".join(_codec6_decode_block(s7, s9, state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec6_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 6``."""

    return codec6_encode(CODEC6_SEED0_KEY_MATERIAL, plain)


def codec6_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with the seed0 codec6 key."""

    return codec6_decode(CODEC6_SEED0_KEY_MATERIAL, encoded)


def _codec0_key_words(key16: bytes) -> tuple[int, int, int, int]:
    """Big-endian XTEA key dwords (sub_2C16AD0 load order)."""
    if len(key16) < 16:
        raise ValueError("codec0 key must be at least 16 bytes")
    return (
        int.from_bytes(key16[0:4], "big"),
        int.from_bytes(key16[4:8], "big"),
        int.from_bytes(key16[8:12], "big"),
        int.from_bytes(key16[12:16], "big"),
    )


def _codec0_encode_block(key: tuple[int, int, int, int], block8: bytes) -> bytes:
    """One 8-byte XTEA block encrypt (sub_2C5BA80), big-endian words.

    Matches C: v += (sum + key) ^ (other + ((other<<4) ^ (other>>5)))
    which is standard XTEA with XOR/add associativity.
    """
    if len(block8) != 8:
        raise ValueError("codec0 block size is 8")
    v0 = int.from_bytes(block8[0:4], "big")
    v1 = int.from_bytes(block8[4:8], "big")
    acc = 0
    delta = 0x9E3779B9
    for _ in range(32):
        v0 = (
            v0
            + (
                (acc + key[acc & 3])
                ^ (v1 + (((v1 << 4) & 0xFFFFFFFF) ^ (v1 >> 5)))
            )
        ) & 0xFFFFFFFF
        acc = (acc + delta) & 0xFFFFFFFF
        v1 = (
            v1
            + (
                (acc + key[(acc >> 11) & 3])
                ^ (v0 + (((v0 << 4) & 0xFFFFFFFF) ^ (v0 >> 5)))
            )
        ) & 0xFFFFFFFF
    return v0.to_bytes(4, "big") + v1.to_bytes(4, "big")


def _codec0_decode_block(key: tuple[int, int, int, int], block8: bytes) -> bytes:
    """One 8-byte XTEA block decrypt (inverse of sub_2C5BA80)."""
    if len(block8) != 8:
        raise ValueError("codec0 block size is 8")
    v0 = int.from_bytes(block8[0:4], "big")
    v1 = int.from_bytes(block8[4:8], "big")
    delta = 0x9E3779B9
    acc = (delta * 32) & 0xFFFFFFFF
    for _ in range(32):
        v1 = (
            v1
            - (
                (acc + key[(acc >> 11) & 3])
                ^ (v0 + (((v0 << 4) & 0xFFFFFFFF) ^ (v0 >> 5)))
            )
        ) & 0xFFFFFFFF
        acc = (acc - delta) & 0xFFFFFFFF
        v0 = (
            v0
            - (
                (acc + key[acc & 3])
                ^ (v1 + (((v1 << 4) & 0xFFFFFFFF) ^ (v1 >> 5)))
            )
        ) & 0xFFFFFFFF
    return v0.to_bytes(4, "big") + v1.to_bytes(4, "big")


def codec0_encode(key_material: bytes, data: bytes) -> bytes:
    """Encode buffer with codec index 0 XTEA-ECB (8B blocks).

    Outer pad unit is 0x10 (wrapper unit_20); inner block is 8.
    """
    if len(data) % 8:
        raise ValueError("codec0 input must be 8-byte aligned")
    key = _codec0_key_words(key_material[:16])
    return b"".join(_codec0_encode_block(key, data[i : i + 8]) for i in range(0, len(data), 8))


def codec0_decode(key_material: bytes, data: bytes) -> bytes:
    """Decode buffer with codec index 0 XTEA-ECB (8B blocks)."""
    if len(data) % 8:
        raise ValueError("codec0 input must be 8-byte aligned")
    key = _codec0_key_words(key_material[:16])
    return b"".join(_codec0_decode_block(key, data[i : i + 8]) for i in range(0, len(data), 8))


def codec0_seed0_encode(plain: bytes) -> bytes:
    """Encode for opcode % 14 == 0 (e.g. S0/0x001C DUNGEON_INFO)."""
    return codec0_encode(CODEC0_SEED0_KEY16, plain)


def codec0_seed0_decode(encoded: bytes) -> bytes:
    """Decode for opcode % 14 == 0."""
    return codec0_decode(CODEC0_SEED0_KEY16, encoded)


def codec8_build_state_from_key(key_material: bytes) -> tuple[int, ...]:
    """Build codec-index-8's 64-dword XTEA-style precomputed key table.

    This is a direct Python translation of the USDOF Stage3 routines:

    - ``0x02C10E40`` obtains 16 constructor bytes for codec index 8;
    - ``0x02C10B50`` validates the key length and calls ``0x02C19420``;
    - ``0x02C19420`` expands four little-endian key dwords into 64 precomputed
      round words stored in a 0x100-byte state buffer.

    The expansion mirrors the classic XTEA schedule but stores both half-round
    keys explicitly:

    ``state[round]      = sum + key[sum & 3]``
    ``state[32 + round] = (sum + delta) + key[((sum + delta) >> 11) & 3]``

    where ``delta`` is ``0x9E3779B9`` (implemented in the binary as
    subtracting ``0x61C88647``).
    """

    if len(key_material) != 0x10:
        raise ValueError("codec8 key material must be exactly 0x10 bytes")

    key_words = [_u32(key_material, offset) for offset in range(0, 0x10, 4)]
    state = [0] * 64
    acc = 0
    for index in range(0x20):
        state[index] = (key_words[acc & 3] + acc) & 0xFFFFFFFF
        acc = (acc + 0x9E3779B9) & 0xFFFFFFFF
        state[0x20 + index] = (key_words[(acc >> 11) & 3] + acc) & 0xFFFFFFFF
    return tuple(state)


def _codec8_mix(value: int) -> int:
    value &= 0xFFFFFFFF
    return ((((value >> 5) ^ ((value << 4) & 0xFFFFFFFF)) + value) & 0xFFFFFFFF)


def _codec8_encode_block(state: tuple[int, ...], block8: bytes) -> bytes:
    """Encode one 8-byte block using codec-index-8 method ``0x02C19550``."""

    if len(state) != 64:
        raise ValueError("codec8 state must contain 64 dwords")
    if len(block8) != 0x08:
        raise ValueError("codec8 block size is 0x08")

    v0 = _u32(block8, 0)
    v1 = _u32(block8, 4)
    for round_index in range(0x20):
        v0 = (v0 + (_codec8_mix(v1) ^ state[round_index])) & 0xFFFFFFFF
        v1 = (v1 + (_codec8_mix(v0) ^ state[0x20 + round_index])) & 0xFFFFFFFF
    return _p32(v0) + _p32(v1)


def _codec8_decode_block(state: tuple[int, ...], block8: bytes) -> bytes:
    """Decode one 8-byte block using codec-index-8 method ``0x02C19690``."""

    if len(state) != 64:
        raise ValueError("codec8 state must contain 64 dwords")
    if len(block8) != 0x08:
        raise ValueError("codec8 block size is 0x08")

    v0 = _u32(block8, 0)
    v1 = _u32(block8, 4)
    for round_index in range(0x1F, -1, -1):
        v1 = (v1 - (_codec8_mix(v0) ^ state[0x20 + round_index])) & 0xFFFFFFFF
        v0 = (v0 - (_codec8_mix(v1) ^ state[round_index])) & 0xFFFFFFFF
    return _p32(v0) + _p32(v1)


def codec8_encode(state: tuple[int, ...], data: bytes) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 8."""

    if len(data) % 0x08:
        raise ValueError("codec8 input must be 0x08-byte aligned")
    return b"".join(_codec8_encode_block(state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec8_decode(state: tuple[int, ...], data: bytes) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 8."""

    if len(data) % 0x08:
        raise ValueError("codec8 input must be 0x08-byte aligned")
    return b"".join(_codec8_decode_block(state, data[i : i + 0x08]) for i in range(0, len(data), 0x08))


def codec8_seed0_state() -> tuple[int, ...]:
    """Return the deterministic codec8 state for the current seed0 localtest."""

    return codec8_build_state_from_key(CODEC8_SEED0_KEY_MATERIAL)


def codec8_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 8`` probes."""

    return codec8_encode(codec8_seed0_state(), plain)


def codec8_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with the seed0 localtest state."""

    return codec8_decode(codec8_seed0_state(), encoded)


@lru_cache(maxsize=4)
def codec11_ttables(client_exe: str | None = None) -> tuple[tuple[tuple[int, ...], tuple[int, ...]], ...]:
    """Read codec-index-11's eight 256-entry table pairs from sentinelfix EXE."""

    path = Path(client_exe) if client_exe else _codec7_client_exe_path()
    tables: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for va in CODEC11_TTABLE_VAS:
        raw = _read_pe_va(path, va, 0x800)
        if len(raw) != 0x800:
            raise ValueError(f"short codec11 T-table read at 0x{va:08x} from {path}")
        words = struct.unpack("<512I", raw)
        tables.append((tuple(words[0::2]), tuple(words[1::2])))
    return tuple(tables)


def _codec11_transform_block(
    tables: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...],
    schedule: bytes,
    block8: bytes,
) -> bytes:
    """Translate current EXE ``0x02C159E0`` for one 8-byte block."""

    if len(schedule) != 0x48:
        raise ValueError("codec11 schedule must be exactly 0x48 bytes")
    if len(block8) != 0x08:
        raise ValueError("codec11 block size is 0x08")

    round_words = struct.unpack("<18I", schedule)
    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    esi = (round_words[0] ^ right) & 0xFFFFFFFF
    ecx = (round_words[1] ^ left) & 0xFFFFFFFF

    def byte(value: int, index: int) -> int:
        return (value >> (8 * index)) & 0xFF

    for round_index in range(1, 8):
        out_left = (
            tables[1][0][byte(ecx, 2)]
            ^ tables[2][0][byte(ecx, 1)]
            ^ tables[3][0][byte(ecx, 0)]
            ^ tables[4][0][byte(esi, 3)]
            ^ tables[5][0][byte(esi, 2)]
            ^ tables[6][0][byte(esi, 1)]
            ^ tables[0][0][byte(ecx, 3)]
            ^ tables[7][0][byte(esi, 0)]
            ^ round_words[2 * round_index]
        )
        out_right = (
            tables[1][1][byte(ecx, 2)]
            ^ tables[2][1][byte(ecx, 1)]
            ^ tables[3][1][byte(ecx, 0)]
            ^ tables[4][1][byte(esi, 3)]
            ^ tables[5][1][byte(esi, 2)]
            ^ tables[6][1][byte(esi, 1)]
            ^ tables[0][1][byte(ecx, 3)]
            ^ tables[7][1][byte(esi, 0)]
            ^ round_words[(2 * round_index) + 1]
        )
        esi = out_left & 0xFFFFFFFF
        ecx = out_right & 0xFFFFFFFF

    final_left = (
        round_words[16]
        ^ (tables[5][0][byte(esi, 2)] & 0x00FF0000)
        ^ (tables[4][0][byte(esi, 3)] & 0xFF000000)
        ^ (tables[6][0][byte(esi, 1)] & 0x0000FF00)
        ^ (tables[7][0][byte(esi, 0)] & 0x000000FF)
    ) & 0xFFFFFFFF
    final_right = (
        round_words[17]
        ^ (tables[1][1][byte(ecx, 2)] & 0x00FF0000)
        ^ (tables[0][1][byte(ecx, 3)] & 0xFF000000)
        ^ (tables[2][1][byte(ecx, 1)] & 0x0000FF00)
        ^ (tables[3][1][byte(ecx, 0)] & 0x000000FF)
    ) & 0xFFFFFFFF
    return final_right.to_bytes(4, "big") + final_left.to_bytes(4, "big")


def codec11_seed0_state() -> bytes:
    """Return current sentinelfix seed0 idx11 0x90-byte state.

    当前只把运行态已验证 state 固定下来；``0x02C153D0`` 的通用 key
    schedule 还没有完整翻译，因此不要把本函数外推为任意客户端/任意 seed。
    """

    return CODEC11_SEED0_STATE


def codec11_transform(schedule: bytes, data: bytes, *, client_exe: str | None = None) -> bytes:
    """Apply idx11 ``0x02C159E0`` to an aligned buffer with one 0x48-byte schedule."""

    if len(data) % 0x08:
        raise ValueError("codec11 input must be 0x08-byte aligned")
    tables = codec11_ttables(client_exe)
    return b"".join(
        _codec11_transform_block(tables, schedule, data[offset : offset + 0x08])
        for offset in range(0, len(data), 0x08)
    )


def _codec1_cast_cipher(key_material: bytes):
    if len(key_material) != 0x10:
        raise ValueError("codec1 key material must be exactly 0x10 bytes")
    try:
        from Crypto.Cipher import CAST
    except ImportError as exc:  # pragma: no cover - local runtime ships pycryptodome.
        raise RuntimeError("codec1 requires pycryptodome Crypto.Cipher.CAST") from exc
    return CAST.new(key_material, CAST.MODE_ECB)


def codec1_encode(key_material: bytes, data: bytes) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 1."""

    if len(data) % 0x08:
        raise ValueError("codec1 input must be 0x08-byte aligned")
    return _codec1_cast_cipher(key_material).encrypt(data)


def codec1_decode(key_material: bytes, data: bytes) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 1."""

    if len(data) % 0x08:
        raise ValueError("codec1 input must be 0x08-byte aligned")
    return _codec1_cast_cipher(key_material).decrypt(data)


def codec1_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 1``."""

    return codec1_encode(CODEC1_SEED0_KEY_MATERIAL, plain)


def codec1_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with seed0 codec1 material."""

    return codec1_decode(CODEC1_SEED0_KEY_MATERIAL, encoded)


def codec11_encode(key_material: bytes, data: bytes) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 11.

    Current implementation is deliberately scoped to the sentinelfix seed0
    state verified on 2026-07-08.  The client key setup routine ``0x02C153D0``
    is not yet exposed as a generic key schedule builder.
    """

    if len(key_material) != 0x10:
        raise ValueError("codec11 key material must be exactly 0x10 bytes")
    if key_material != CODEC11_SEED0_KEY_MATERIAL:
        raise NotImplementedError("codec11 generic key schedule is not implemented; only current seed0 is supported")
    if len(data) % 0x08:
        raise ValueError("codec11 input must be 0x08-byte aligned")
    return codec11_transform(codec11_seed0_state()[:0x48], data)


def codec11_decode(key_material: bytes, data: bytes) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 11."""

    if len(key_material) != 0x10:
        raise ValueError("codec11 key material must be exactly 0x10 bytes")
    if key_material != CODEC11_SEED0_KEY_MATERIAL:
        raise NotImplementedError("codec11 generic key schedule is not implemented; only current seed0 is supported")
    if len(data) % 0x08:
        raise ValueError("codec11 input must be 0x08-byte aligned")
    return codec11_transform(codec11_seed0_state()[0x48:], data)


def codec11_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 11``."""

    return codec11_encode(CODEC11_SEED0_KEY_MATERIAL, plain)


def codec11_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with seed0 codec11 state."""

    return codec11_decode(CODEC11_SEED0_KEY_MATERIAL, encoded)



def _noekeon_theta(key_words: list[int], state: list[int]) -> None:
    """Noekeon Theta; mutates state in place. key_words length 4."""
    t = state[0] ^ state[2]
    t ^= _rol32(t, 8) ^ _ror32(t, 8)
    state[1] = (state[1] ^ t) & 0xFFFFFFFF
    state[3] = (state[3] ^ t) & 0xFFFFFFFF
    for index in range(4):
        state[index] = (state[index] ^ int(key_words[index])) & 0xFFFFFFFF
    t = state[1] ^ state[3]
    t ^= _rol32(t, 8) ^ _ror32(t, 8)
    state[0] = (state[0] ^ t) & 0xFFFFFFFF
    state[2] = (state[2] ^ t) & 0xFFFFFFFF


def _noekeon_gamma(state: list[int]) -> None:
    state[1] = (state[1] ^ (~(state[3] | state[2]) & 0xFFFFFFFF)) & 0xFFFFFFFF
    state[0] = (state[0] ^ (state[2] & state[1])) & 0xFFFFFFFF
    state[0], state[3] = state[3], state[0]
    state[2] = (state[2] ^ state[0] ^ state[1] ^ state[3]) & 0xFFFFFFFF
    state[1] = (state[1] ^ (~(state[3] | state[2]) & 0xFFFFFFFF)) & 0xFFFFFFFF
    state[0] = (state[0] ^ (state[2] & state[1])) & 0xFFFFFFFF


def _noekeon_pi1(state: list[int]) -> None:
    # Frozen Stage3 0x02C17270 rotates word 0 here. This differs from the
    # reference Noekeon Pi1 word-1 rotation and is transport-significant.
    state[0] = _rol32(state[0], 1)
    state[2] = _rol32(state[2], 5)
    state[3] = _rol32(state[3], 2)


def _noekeon_pi2(state: list[int]) -> None:
    state[0] = _ror32(state[0], 1)
    state[2] = _ror32(state[2], 5)
    state[3] = _ror32(state[3], 2)


def codec12_build_keys(key_material: bytes) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Build encrypt/decrypt Noekeon keys like EXE 0x02C170F0."""
    if len(key_material) != 0x10:
        raise ValueError("codec12 key material must be exactly 0x10 bytes")
    enc = tuple(int.from_bytes(key_material[offset : offset + 4], "big") for offset in range(0, 0x10, 4))
    dec = list(enc)
    _noekeon_theta([0, 0, 0, 0], dec)
    return enc, tuple(dec)


def _codec12_encrypt_block(enc_key: tuple[int, ...], block16: bytes) -> bytes:
    if len(block16) != 0x10:
        raise ValueError("codec12 block size is 0x10")
    state = [int.from_bytes(block16[offset : offset + 4], "big") for offset in range(0, 0x10, 4)]
    key = list(enc_key)
    for round_index in range(16):
        state[0] = (state[0] ^ CODEC12_RC[round_index]) & 0xFFFFFFFF
        _noekeon_theta(key, state)
        _noekeon_pi1(state)
        _noekeon_gamma(state)
        _noekeon_pi2(state)
    state[0] = (state[0] ^ CODEC12_RC[16]) & 0xFFFFFFFF
    _noekeon_theta(key, state)
    return b"".join(word.to_bytes(4, "big") for word in state)


def _codec12_decrypt_block(dec_key: tuple[int, ...], block16: bytes) -> bytes:
    if len(block16) != 0x10:
        raise ValueError("codec12 block size is 0x10")
    state = [int.from_bytes(block16[offset : offset + 4], "big") for offset in range(0, 0x10, 4)]
    key = list(dec_key)
    for round_index in range(16, 0, -1):
        _noekeon_theta(key, state)
        state[0] = (state[0] ^ CODEC12_RC[round_index]) & 0xFFFFFFFF
        _noekeon_pi1(state)
        _noekeon_gamma(state)
        _noekeon_pi2(state)
    _noekeon_theta(key, state)
    state[0] = (state[0] ^ CODEC12_RC[0]) & 0xFFFFFFFF
    return b"".join(word.to_bytes(4, "big") for word in state)


def codec12_encode(key_material: bytes, data: bytes) -> bytes:
    """Encode a 0x10-aligned buffer with codec index 12 (Noekeon)."""
    if len(data) % 0x10:
        raise ValueError("codec12 input must be 0x10-byte aligned")
    enc_key, _dec_key = codec12_build_keys(key_material)
    out = bytearray()
    for offset in range(0, len(data), 0x10):
        out.extend(_codec12_encrypt_block(enc_key, data[offset : offset + 0x10]))
    return bytes(out)


def codec12_decode(key_material: bytes, data: bytes) -> bytes:
    """Decode a 0x10-aligned buffer with codec index 12 (Noekeon)."""
    if len(data) % 0x10:
        raise ValueError("codec12 input must be 0x10-byte aligned")
    _enc_key, dec_key = codec12_build_keys(key_material)
    out = bytearray()
    for offset in range(0, len(data), 0x10):
        out.extend(_codec12_decrypt_block(dec_key, data[offset : offset + 0x10]))
    return bytes(out)


def codec12_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x10-aligned plaintext buffer for opcode % 14 == 12."""
    return codec12_encode(CODEC12_SEED0_KEY_MATERIAL, plain)


def codec12_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x10-aligned ciphertext buffer with seed0 codec12 key."""
    return codec12_decode(CODEC12_SEED0_KEY_MATERIAL, encoded)


def codec13_build_state_from_key(key_material: bytes, rounds: int = 0x80) -> tuple[int, tuple[int, ...]]:
    """Build codec-index-13 state exactly like current EXE ``0x02C16DC0``.

    中文维护说明：
    - idx13 的 40 字节 key 不是标准库算法的直接 key；客户端先把它按大端拆成
      10 个 dword，再由 ``0x02C16B70`` 把前 8 个 dword 和后 2 个 seed dword
      混成 8 个 schedule dword。
    - ``rounds`` 对应 inner state ``+0``，constructor 传 0 时 setup 函数默认
      写入 ``0x80``。这里保留显式参数只为动态 dump 后对齐，正常 seed0 走默认值。
    """

    if len(key_material) != 0x28:
        raise ValueError("codec13 key material must be exactly 0x28 bytes")
    if rounds <= 0:
        rounds = 0x80

    words = tuple(int.from_bytes(key_material[offset : offset + 4], "big") for offset in range(0, 0x28, 4))
    src = words[:8]
    left = words[8]
    right = (words[9] ^ left) & 0xFFFFFFFF
    schedule: list[int] = []

    value = (src[0] + right) & 0xFFFFFFFF
    value = (value + _rol32(value, 1) - 1) & 0xFFFFFFFF
    left = (left ^ (_rol32(value, 4) ^ value)) & 0xFFFFFFFF
    schedule.append(left)

    value = (src[1] + left) & 0xFFFFFFFF
    value = (value + _rol32(value, 2) + 1) & 0xFFFFFFFF
    mixed = (_rol32(value, 8) ^ value) & 0xFFFFFFFF
    mixed = (mixed + src[2]) & 0xFFFFFFFF
    value = (_rol32(mixed, 1) - mixed) & 0xFFFFFFFF
    mixed = (_rol32(value, 16) ^ (value | left)) & 0xFFFFFFFF
    right = (right ^ mixed) & 0xFFFFFFFF
    schedule.append(right)

    value = (src[3] + right) & 0xFFFFFFFF
    value = (_rol32(value, 2) + value + 1) & 0xFFFFFFFF
    left = (left ^ value) & 0xFFFFFFFF
    right = (right ^ left) & 0xFFFFFFFF
    schedule.extend((left, right))

    value = (src[4] + right) & 0xFFFFFFFF
    value = (value + _rol32(value, 1) - 1) & 0xFFFFFFFF
    left = (left ^ (_rol32(value, 4) ^ value)) & 0xFFFFFFFF
    schedule.append(left)

    value = (src[5] + left) & 0xFFFFFFFF
    value = (value + _rol32(value, 2) + 1) & 0xFFFFFFFF
    mixed = (_rol32(value, 8) ^ value) & 0xFFFFFFFF
    mixed = (mixed + src[6]) & 0xFFFFFFFF
    value = (_rol32(mixed, 1) - mixed) & 0xFFFFFFFF
    mixed = (_rol32(value, 16) ^ (value | left)) & 0xFFFFFFFF
    right = (right ^ mixed) & 0xFFFFFFFF
    schedule.append(right)

    value = (src[7] + right) & 0xFFFFFFFF
    value = (_rol32(value, 2) + value + 1) & 0xFFFFFFFF
    left = (left ^ value) & 0xFFFFFFFF
    right = (right ^ left) & 0xFFFFFFFF
    schedule.extend((left, right))

    if len(schedule) != 8:
        raise AssertionError("codec13 schedule must contain 8 dwords")
    python_state = (int(rounds), tuple(schedule))
    runtime = get_native_core_runtime()
    native_state = runtime.execute(
        "codec13_build_state",
        lambda: python_state,
        lambda: (
            lambda packed: (
                int.from_bytes(packed[:4], "little"),
                tuple(
                    int.from_bytes(packed[offset : offset + 4], "little")
                    for offset in range(4, 0x24, 4)
                ),
            )
        )(runtime.module.codec13_build_state(bytes(key_material))),
        input_bytes=len(key_material),
        output_bytes=0x24,
    )
    return int(native_state[0]), tuple(native_state[1])


def _codec13_encode_block(state: tuple[int, tuple[int, ...]], block8: bytes) -> bytes:
    """Encode one 8-byte block using current EXE helper ``0x02C16C40``."""

    if len(block8) != 0x08:
        raise ValueError("codec13 block size is 0x08")
    rounds, schedule = state
    if len(schedule) != 8:
        raise ValueError("codec13 schedule must contain 8 dwords")

    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")

    right = (right ^ left) & 0xFFFFFFFF
    index = 0
    step = 1
    index_shadow = index
    if rounds != step:
        while True:
            value = (schedule[index] + right) & 0xFFFFFFFF
            step += 1
            mixed = (value + _rol32(value, 1) - 1) & 0xFFFFFFFF
            left = (_rol32(mixed, 4) ^ left ^ mixed) & 0xFFFFFFFF
            if step == rounds:
                break

            value = (schedule[index + 1] + left) & 0xFFFFFFFF
            mixed = (value + _rol32(value, 2) + 1) & 0xFFFFFFFF
            value = (_rol32(mixed, 8) ^ mixed) & 0xFFFFFFFF
            value = (value + schedule[index + 2]) & 0xFFFFFFFF
            mixed = (_rol32(value, 1) - value) & 0xFFFFFFFF
            value = (_rol32(mixed, 16) ^ (left | mixed)) & 0xFFFFFFFF
            right = (right ^ value) & 0xFFFFFFFF
            step += 1
            if step == rounds:
                break

            index = index_shadow
            value = (schedule[index + 3] + right) & 0xFFFFFFFF
            value = (_rol32(value, 2) + value + 1) & 0xFFFFFFFF
            left = (value ^ left) & 0xFFFFFFFF
            step += 1
            if step == rounds:
                break

            right = (right ^ left) & 0xFFFFFFFF
            index ^= 4
            step += 1
            index_shadow = index
            if step == rounds:
                break

    return left.to_bytes(4, "big") + right.to_bytes(4, "big")


def _codec13_decode_block(state: tuple[int, tuple[int, ...]], block8: bytes) -> bytes:
    """Decode one 8-byte block using current EXE helper ``0x02C16D00``."""

    if len(block8) != 0x08:
        raise ValueError("codec13 block size is 0x08")
    rounds, schedule = state
    if len(schedule) != 8:
        raise ValueError("codec13 schedule must contain 8 dwords")

    left = int.from_bytes(block8[:4], "big")
    right = int.from_bytes(block8[4:], "big")
    remaining = int(rounds)
    index = ((~remaining) & 1) * 4

    # constructor 默认 rounds=0x80，是 4 的倍数；反编译的 jump table 对
    # 小于 4 的尾数直接返回。这里保留同样行为，避免“修正”客户端语义。
    while remaining >= 4:
        value = (schedule[index + 3] + right) & 0xFFFFFFFF
        value = (_rol32(value, 2) + value + 1) & 0xFFFFFFFF
        left = (left ^ value) & 0xFFFFFFFF
        remaining -= 1

        value = (schedule[index + 1] + left) & 0xFFFFFFFF
        mixed = (value + _rol32(value, 2) + 1) & 0xFFFFFFFF
        value = (_rol32(mixed, 8) ^ mixed) & 0xFFFFFFFF
        value = (value + schedule[index + 2]) & 0xFFFFFFFF
        mixed = (_rol32(value, 1) - value) & 0xFFFFFFFF
        value = (_rol32(mixed, 16) ^ (left | mixed)) & 0xFFFFFFFF
        right = (right ^ value) & 0xFFFFFFFF
        remaining -= 1

        value = (right + schedule[index]) & 0xFFFFFFFF
        value = (value + _rol32(value, 1) - 1) & 0xFFFFFFFF
        left = (_rol32(value, 4) ^ left ^ value) & 0xFFFFFFFF
        remaining -= 1

        right = (right ^ left) & 0xFFFFFFFF
        remaining -= 1
        index ^= 4

    return left.to_bytes(4, "big") + right.to_bytes(4, "big")


def codec13_encode(state: tuple[int, tuple[int, ...]], data: bytes) -> bytes:
    """Encode a 0x08-aligned buffer with codec index 13."""

    if len(data) % 0x08:
        raise ValueError("codec13 input must be 0x08-byte aligned")
    state_bytes = struct.pack("<I8I", int(state[0]), *state[1])
    data_bytes = bytes(data)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "codec13_encode",
        lambda: b"".join(
            _codec13_encode_block(state, data_bytes[offset : offset + 0x08])
            for offset in range(0, len(data_bytes), 0x08)
        ),
        lambda: runtime.module.codec13_encode(state_bytes, data_bytes),
        input_bytes=len(state_bytes) + len(data_bytes),
        output_bytes=len(data_bytes),
    )


def codec13_decode(state: tuple[int, tuple[int, ...]], data: bytes) -> bytes:
    """Decode a 0x08-aligned buffer with codec index 13."""

    if len(data) % 0x08:
        raise ValueError("codec13 input must be 0x08-byte aligned")
    state_bytes = struct.pack("<I8I", int(state[0]), *state[1])
    data_bytes = bytes(data)
    runtime = get_native_core_runtime()
    return runtime.execute(
        "codec13_decode",
        lambda: b"".join(
            _codec13_decode_block(state, data_bytes[offset : offset + 0x08])
            for offset in range(0, len(data_bytes), 0x08)
        ),
        lambda: runtime.module.codec13_decode(state_bytes, data_bytes),
        input_bytes=len(state_bytes) + len(data_bytes),
        output_bytes=len(data_bytes),
    )


@lru_cache(maxsize=1)
def codec13_seed0_state() -> tuple[int, tuple[int, ...]]:
    """Return deterministic idx13 state for the current sentinelfix seed0 EXE."""

    return codec13_build_state_from_key(CODEC13_SEED0_KEY_MATERIAL)


def codec13_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x08-aligned plaintext buffer for opcode ``% 14 == 13``."""

    return codec13_encode(codec13_seed0_state(), plain)


def codec13_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x08-aligned ciphertext buffer with seed0 codec13 state."""

    return codec13_decode(codec13_seed0_state(), encoded)


def codec3_encode(state: TwofishState, data: bytes) -> bytes:
    """Encode a 0x10-aligned buffer with codec index 3."""

    if len(data) % 0x10:
        raise ValueError("codec3 input must be 0x10-byte aligned")
    out = bytearray(data)
    for offset in range(0, len(data), 0x10):
        out[offset : offset + 0x10] = twofish_encrypt_block(state, data[offset : offset + 0x10])
    return bytes(out)


def codec3_decode(state: TwofishState, data: bytes) -> bytes:
    """Decode a 0x10-aligned buffer with codec index 3."""

    if len(data) % 0x10:
        raise ValueError("codec3 input must be 0x10-byte aligned")
    out = bytearray(data)
    for offset in range(0, len(data), 0x10):
        out[offset : offset + 0x10] = twofish_decrypt_block(state, data[offset : offset + 0x10])
    return bytes(out)


@lru_cache(maxsize=1)
def codec3_seed0_state() -> TwofishState:
    """Return the deterministic codec3 Twofish state for the seed0 localtest."""

    return twofish_prepare_key(CODEC3_SEED0_KEY_MATERIAL)


def codec3_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x10-aligned plaintext buffer for opcode ``% 14 == 3`` probes."""

    return codec3_encode(codec3_seed0_state(), plain)


def codec3_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x20-aligned ciphertext buffer with the seed0 localtest state."""

    return codec3_decode(codec3_seed0_state(), encoded)


def codec4_seed0_encode(plain: bytes) -> bytes:
    """Encode a 0x10-aligned plaintext buffer with seed0 codec index 4."""

    if len(plain) % 0x10:
        raise ValueError("codec4 input must be 0x10-byte aligned")
    return aes_ecb_encrypt(plain, key=CODEC4_SEED0_AES_KEY)


def codec4_seed0_decode(encoded: bytes) -> bytes:
    """Decode a 0x10-aligned ciphertext buffer with seed0 codec index 4."""

    if len(encoded) % 0x10:
        raise ValueError("codec4 input must be 0x10-byte aligned")
    return aes_ecb_decrypt(encoded, key=CODEC4_SEED0_AES_KEY)


@lru_cache(maxsize=4)
def codec9_anubis_tables(
    client_exe: str | None = None,
) -> tuple[tuple[tuple[int, ...], ...], tuple[int, ...]]:
    """Read original-Anubis T0..T5 and round constants from Stage3."""

    path = Path(client_exe) if client_exe else _codec7_client_exe_path()
    round_constant_size = CODEC9_ROUND_CONSTANT_COUNT * 4
    raw = _read_pe_va(
        path,
        CODEC9_TABLE_BASE_VA,
        CODEC9_TABLE_COUNT * CODEC9_TABLE_SIZE + round_constant_size,
    )
    expected_size = CODEC9_TABLE_COUNT * CODEC9_TABLE_SIZE + round_constant_size
    if len(raw) != expected_size:
        raise ValueError(
            f"short codec9 Anubis table read at 0x{CODEC9_TABLE_BASE_VA:08x} "
            f"from {path}: got={len(raw)} expected={expected_size}"
        )

    tables = []
    for index in range(CODEC9_TABLE_COUNT):
        start = index * CODEC9_TABLE_SIZE
        tables.append(struct.unpack("<256I", raw[start : start + CODEC9_TABLE_SIZE]))
    rc_start = CODEC9_TABLE_COUNT * CODEC9_TABLE_SIZE
    round_constants = struct.unpack(
        f"<{CODEC9_ROUND_CONSTANT_COUNT}I",
        raw[rc_start : rc_start + round_constant_size],
    )
    return tuple(tables), tuple(round_constants)


@lru_cache(maxsize=8)
def codec9_build_round_keys(
    key_material: bytes,
    client_exe: str | None = None,
) -> tuple[tuple[tuple[int, int, int, int], ...], tuple[tuple[int, int, int, int], ...], int]:
    """Build Stage3 codec9 forward/inverse schedules (IDA 0x02C11E70)."""

    key = bytes(key_material)
    if len(key) < 0x10 or len(key) > 0x28 or len(key) % 4:
        raise ValueError("codec9 Anubis key must be 0x10..0x28 bytes in four-byte steps")

    tables, round_constants = codec9_anubis_tables(client_exe)
    t0, t1, t2, t3, t4, t5 = tables
    word_count = len(key) // 4
    rounds = word_count + 8
    kappa = list(struct.unpack(f">{word_count}I", key))
    forward: list[tuple[int, int, int, int]] = []

    for round_index in range(rounds + 1):
        last = kappa[word_count - 1]
        round_key = [
            t4[(last >> 24) & 0xFF],
            t4[(last >> 16) & 0xFF],
            t4[(last >> 8) & 0xFF],
            t4[last & 0xFF],
        ]
        for key_index in range(word_count - 2, -1, -1):
            word = kappa[key_index]
            key_bytes = (
                (word >> 24) & 0xFF,
                (word >> 16) & 0xFF,
                (word >> 8) & 0xFF,
                word & 0xFF,
            )
            for column in range(4):
                value = round_key[column]
                round_key[column] = (
                    t4[key_bytes[column]]
                    ^ (t5[(value >> 24) & 0xFF] & 0xFF000000)
                    ^ (t5[(value >> 16) & 0xFF] & 0x00FF0000)
                    ^ (t5[(value >> 8) & 0xFF] & 0x0000FF00)
                    ^ (t5[value & 0xFF] & 0x000000FF)
                ) & 0xFFFFFFFF
        forward.append(tuple(round_key))

        if round_index == rounds:
            break
        inter = []
        for index in range(word_count):
            word0 = kappa[index]
            word1 = kappa[(index - 1) % word_count]
            word2 = kappa[(index - 2) % word_count]
            word3 = kappa[(index - 3) % word_count]
            inter.append(
                (
                    t0[(word0 >> 24) & 0xFF]
                    ^ t1[(word1 >> 16) & 0xFF]
                    ^ t2[(word2 >> 8) & 0xFF]
                    ^ t3[word3 & 0xFF]
                )
                & 0xFFFFFFFF
            )
        inter[0] ^= round_constants[round_index]
        kappa = inter

    inverse: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * (rounds + 1)
    inverse[0] = forward[rounds]
    inverse[rounds] = forward[0]
    for round_index in range(1, rounds):
        row = []
        for value in forward[rounds - round_index]:
            row.append(
                (
                    t0[t4[(value >> 24) & 0xFF] & 0xFF]
                    ^ t1[t4[(value >> 16) & 0xFF] & 0xFF]
                    ^ t2[t4[(value >> 8) & 0xFF] & 0xFF]
                    ^ t3[t4[value & 0xFF] & 0xFF]
                )
                & 0xFFFFFFFF
            )
        inverse[round_index] = tuple(row)

    return tuple(forward), tuple(inverse), rounds


def _codec9_transform_block(
    tables: tuple[tuple[int, ...], ...],
    round_keys: tuple[tuple[int, int, int, int], ...],
    rounds: int,
    block16: bytes,
) -> bytes:
    """Apply one original-Anubis block transform (IDA 0x02C12480)."""

    if len(block16) != 0x10:
        raise ValueError("codec9 Anubis block size is 0x10")
    if len(round_keys) != rounds + 1:
        raise ValueError("codec9 Anubis round-key count mismatch")

    t0, t1, t2, t3 = tables[:4]
    input_words = struct.unpack(">4I", block16)
    state = [
        (input_words[index] ^ round_keys[0][index]) & 0xFFFFFFFF
        for index in range(4)
    ]

    for round_index in range(1, rounds):
        state = [
            (
                t0[(state[0] >> shift) & 0xFF]
                ^ t1[(state[1] >> shift) & 0xFF]
                ^ t2[(state[2] >> shift) & 0xFF]
                ^ t3[(state[3] >> shift) & 0xFF]
                ^ round_keys[round_index][column]
            )
            & 0xFFFFFFFF
            for column, shift in enumerate((24, 16, 8, 0))
        ]

    output = []
    for column, shift in enumerate((24, 16, 8, 0)):
        output.append(
            (
                (t0[(state[0] >> shift) & 0xFF] & 0xFF000000)
                ^ (t1[(state[1] >> shift) & 0xFF] & 0x00FF0000)
                ^ (t2[(state[2] >> shift) & 0xFF] & 0x0000FF00)
                ^ (t3[(state[3] >> shift) & 0xFF] & 0x000000FF)
                ^ round_keys[rounds][column]
            )
            & 0xFFFFFFFF
        )
    return struct.pack(">4I", *output)


def codec9_encode(
    key_material: bytes,
    data: bytes,
    *,
    client_exe: str | None = None,
) -> bytes:
    """Encode a 0x10-aligned buffer with Stage3 codec index 9."""

    if len(data) % 0x10:
        raise ValueError("codec9 input must be 0x10-byte aligned")
    tables, _ = codec9_anubis_tables(client_exe)
    forward, _inverse, rounds = codec9_build_round_keys(key_material, client_exe)
    return b"".join(
        _codec9_transform_block(tables, forward, rounds, data[offset : offset + 0x10])
        for offset in range(0, len(data), 0x10)
    )


def codec9_decode(
    key_material: bytes,
    data: bytes,
    *,
    client_exe: str | None = None,
) -> bytes:
    """Decode a 0x10-aligned buffer with Stage3 codec index 9."""

    if len(data) % 0x10:
        raise ValueError("codec9 input must be 0x10-byte aligned")
    tables, _ = codec9_anubis_tables(client_exe)
    _forward, inverse, rounds = codec9_build_round_keys(key_material, client_exe)
    return b"".join(
        _codec9_transform_block(tables, inverse, rounds, data[offset : offset + 0x10])
        for offset in range(0, len(data), 0x10)
    )


def codec9_seed0_encode(plain: bytes) -> bytes:
    """Encode with Stage3 codec9's seed0 original-Anubis key."""

    return codec9_encode(CODEC9_SEED0_KEY_MATERIAL, plain)


def codec9_seed0_decode(encoded: bytes) -> bytes:
    """Decode with Stage3 codec9's seed0 original-Anubis key."""

    return codec9_decode(CODEC9_SEED0_KEY_MATERIAL, encoded)


def codec_index_for_opcode(opcode: int) -> int:
    return opcode % 14


def unit_for_opcode(opcode: int) -> int:
    return CODEC_UNITS[codec_index_for_opcode(opcode)]


def codec10_xor_dword(data: bytes) -> bytes:
    """Apply the fixed dword-XOR transform used by codec index 10.

    Codec index 10 is selected by ``opcode % 14 == 10``.  Dynamic traces show
    ``ENUM_CMDPACKET_PIV`` (0x013E) using this transform with mask
    ``0x1D3B4E9C``.  The transform is symmetric for encode/decode.
    """

    if len(data) % 4:
        raise ValueError("codec10 input must be 4-byte aligned")
    mask = 0x1D3B4E9C
    out = bytearray()
    for offset in range(0, len(data), 4):
        value = int.from_bytes(data[offset : offset + 4], "little") ^ mask
        out.extend(value.to_bytes(4, "little"))
    return bytes(out)


def parse_piv_plain(plain: bytes) -> PivPlain | None:
    """Parse decoded ``ENUM_CMDPACKET_PIV`` plaintext if it has the live shape.

    Current live plaintext after trimming codec padding:

    ``00 01 00 59 a7 2c cf``

    meaning:

    - ``00``: echoed server notipacket flag;
    - ``01 00``: echoed server notipacket opcode;
    - ``59 a7 2c cf``: opaque PIV/tag dword, little-endian ``0xCF2CA759``.
    """

    trimmed = plain[:-1] if plain.endswith(b"\x00") else plain
    if len(trimmed) < 3:
        return None
    tag = int.from_bytes(trimmed[3:7], "little") if len(trimmed) >= 7 else None
    return PivPlain(
        echoed_flag=trimmed[0],
        echoed_opcode=int.from_bytes(trimmed[1:3], "little"),
        tag=tag,
        raw=trimmed,
    )


def decode_payload_skeleton(opcode: int, payload: bytes) -> DecodeResult:
    """Stage P1 skeleton.

    The Stage3 client uses 14 opcode-selected codecs and mostly time/PRNG-
    derived key material.  Until the live key material is dumped, the server
    should preserve raw bytes and mark decode as tentative.  idx10 is a fixed
    XOR-dword codec candidate, so we expose that transform for correlation.

    ``payload`` is intentionally the codec body, not always just
    ``packet[0x10:]``.  For regular client->server packets with
    ``total_length > 0x10``, live traces show that the three header bytes at
    ``+0x0D..+0x0F`` participate as a codec prefix.  The caller therefore
    passes ``header.reserved + packet[0x10:]``.  Header-only packets still pass
    an empty payload.
    """
    idx = codec_index_for_opcode(opcode)
    if not payload:
        return DecodeResult(True, idx, b"", "empty payload")
    if idx == 0 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec0_seed0_decode(payload), "idx0 XTEA-ECB seed0 candidate")
    if idx == 1 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec1_seed0_decode(payload), "idx1 CAST-128 seed0 candidate")
    if idx == 2 and len(payload) % 0x10 == 0:
        return DecodeResult(True, idx, codec2_seed0_decode(payload), "idx2 seed0 candidate")
    if idx == 7 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec7_seed0_decode(payload), "idx7 Blowfish-like seed0 candidate")
    if idx == 8 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec8_seed0_decode(payload), "idx8 XTEA seed0 candidate")
    if idx == 9 and len(payload) % 0x10 == 0:
        return DecodeResult(True, idx, codec9_seed0_decode(payload), "idx9 Anubis seed0 candidate")
    if idx == 3 and len(payload) % 0x10 == 0:
        return DecodeResult(True, idx, codec3_seed0_decode(payload), "idx3 Twofish seed0 candidate")
    if idx == 4 and len(payload) % 0x10 == 0:
        return DecodeResult(True, idx, codec4_seed0_decode(payload), "idx4 AES seed0 candidate")
    if idx == 5 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec5_seed0_decode(payload), "idx5 Skipjack seed0 candidate")
    if idx == 6 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec6_seed0_decode(payload), "idx6 MISTY-like seed0 candidate")
    if idx == 10 and len(payload) % 4 == 0:
        return DecodeResult(True, idx, codec10_xor_dword(payload), "idx10 fixed XOR-dword candidate")
    if idx == 11 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec11_seed0_decode(payload), "idx11 table-transform seed0 candidate")
    if idx == 12 and len(payload) % 0x10 == 0:
        return DecodeResult(True, idx, codec12_seed0_decode(payload), "idx12 Noekeon seed0 candidate")
    if idx == 13 and len(payload) % 8 == 0:
        return DecodeResult(True, idx, codec13_seed0_decode(payload), "idx13 ARX64 seed0 candidate")
    return DecodeResult(False, idx, payload, "codec key/material not yet synchronized; raw payload preserved")
