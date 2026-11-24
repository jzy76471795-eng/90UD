#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""00AD 79 项默认值表（从 Stage3 IDB 反编译 sub_1876770 得到）。

sub_1876770 = ``option_00ad_default_writer_sub_1876770``（IDA MCP 命名）。
它是客户端启动时调用的 GAME_OPTION 默认值 writer：对 79 项 option 调用
``sub_2F3F620(idx, value)`` 写入 storage（u16[79]）。

IDB 反编译 sub_2F3F620：
    if (n <= 0x4E) *(this + n) = a3;    // n 是 u16 索引，最大 0x4E = 78

服务端应当按这套默认值发 S->C 0x00AD 主块（158B），ext1/ext2 长度 0。

笔记《进赛丽亚.md》的"必要 00AD body"经验：
    primary = 79 项 u16 = 158B
    ext1_len = 0
    ext2_len = 0
    opt[0x4A] (index 74) 必须 = 1（写到本地 f4213 = pending-area enabled）
"""
from __future__ import annotations

# 79 项（index 0..78）的 u16 默认值。
# 显式出现的值来自 sub_1876770 反编译；未显式出现的 index
# 默认走 0（sub_1876280 init 阶段已经把 storage 清零）。
DEFAULT_GAME_OPTIONS: dict[int, int] = {
    0: 1, 1: 1, 2: 1, 3: 3, 4: 70, 5: 25,
    6: 0, 7: 0, 8: 0, 9: 0, 10: 0,
    11: 1, 12: 1, 13: 1, 14: 1, 15: 0, 16: 0, 17: 1,
    18: 23,   # First launch: windowed. Saved account options retain their value.
    19: 0,    # dword_41709C8 fallback
    20: 0, 21: 0,
    22: 1000, 23: 0, 24: 1, 25: 70, 26: 1,
    27: 0, 28: 0, 29: 0, 30: 0, 31: 0,
    32: 1, 33: 1, 34: 1, 35: 0, 36: 70,
    37: 0, 38: 0, 39: 0, 40: 0, 41: 0, 42: 1,
    43: 0, 44: 0, 45: 0, 46: 0, 47: 0, 48: 0, 49: 0,
    50: 1, 51: 0, 52: 0, 53: 0, 54: 1, 55: 1, 56: 1, 57: 0,
    58: 1, 59: 70, 60: 0, 61: 0, 62: 0, 63: 0, 64: 0,
    65: 1, 66: 1, 67: 1, 68: 100, 69: 0, 70: 1, 71: 100,
    72: 1, 73: 1,
    74: 1,    # <-- case 74 (0x4A): pending-area enabled -> f4213 (sub_1AB4F70)
    75: 0, 76: 0, 77: 0, 78: 0,
}

OPTION_COUNT = 79
PRIMARY_LEN = OPTION_COUNT * 2  # 158

assert len(DEFAULT_GAME_OPTIONS) == OPTION_COUNT, (
    f"need {OPTION_COUNT} options, got {len(DEFAULT_GAME_OPTIONS)}"
)


def build_default_options_body() -> bytes:
    """构造 ``sub_2F3F620`` 默认值对应的 158B 主块（79 × u16 LE）。"""
    body = bytearray()
    for idx in range(OPTION_COUNT):
        body.extend(DEFAULT_GAME_OPTIONS[idx].to_bytes(2, "little"))
    return bytes(body)


def build_game_option_packet_body(overrides: dict[int, int] | None = None) -> bytes:
    """构造 S->C 0x00AD packet body（含 ext1/ext2 length 前缀）。

    layout:
        u32 primary_len = 158
        79 × u16 options
        u32 ext1_len = 0
        u32 ext2_len = 0
    """
    primary = build_default_options_body()
    if overrides:
        primary_mut = bytearray(primary)
        for idx, val in overrides.items():
            if not (0 <= idx < OPTION_COUNT):
                continue
            primary_mut[idx * 2 : idx * 2 + 2] = int(val & 0xFFFF).to_bytes(2, "little")
        primary = bytes(primary_mut)
    body = bytearray()
    body.extend(len(primary).to_bytes(4, "little"))
    body.extend(primary)
    body.extend((0).to_bytes(4, "little"))  # ext1_len = 0
    body.extend((0).to_bytes(4, "little"))  # ext2_len = 0
    return bytes(body)


if __name__ == "__main__":
    body = build_game_option_packet_body()
    print(f"len(body) = {len(body)}  (expected 158 + 4 + 4 + 4 = 170)")
    print(f"primary[:32] = {body[4:36].hex(' ')}")
    print(f"ext1_len = {int.from_bytes(body[162:166], 'little')}")
    print(f"ext2_len = {int.from_bytes(body[166:170], 'little')}")
    print(f"opt[74] = 0x{int.from_bytes(body[4 + 74*2 : 4 + 74*2 + 2], 'little'):04x}")

def normalize_main_game_option_blob(main: bytes | None) -> bytes:
    """Normalize stored main blob to Stage3 158B and force opt[74]=1."""
    primary = bytearray(build_default_options_body())
    if main:
        raw = bytes(main)
        # tolerate accidental length-prefixed storage
        if len(raw) >= 4 + PRIMARY_LEN and int.from_bytes(raw[:4], "little") == PRIMARY_LEN:
            raw = raw[4 : 4 + PRIMARY_LEN]
        n = min(len(raw), PRIMARY_LEN)
        if n > 0:
            primary[:n] = raw[:n]
    # critical safety: pending-area flag
    primary[74 * 2 : 74 * 2 + 2] = (1).to_bytes(2, "little")
    return bytes(primary)


def build_game_option_packet_body_from_main(main: bytes | None = None) -> bytes:
    """S->C 0x00AD body from account stored main blob (or defaults)."""
    primary = normalize_main_game_option_blob(main)
    return (
        PRIMARY_LEN.to_bytes(4, "little")
        + primary
        + (0).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
    )
