"""进入赛利亚房间链路 14：S->C 0x00AD GAME_OPTION。

维护边界：
- 本文件只负责 ``C->S SELECT_CHARACTER`` 之后、``S->C 0x0004 ACK`` 之前的
  ``S->C flag0/opcode=0x00AD`` A/B 构造。
- 默认关闭。开启方式：``USDOF_GAME_ENTER_SERIA_SEND_00AD_GAME_OPTION=1``。
- 这是笔记《进赛丽亚.md》明示的"必要链路"项：客户端 0x00AD handler
  ``option_00ad_dispatch_loop_sub_1877260`` 读 79 项 u16；case 74 通过
  ``option_setter_case_74_sub_1AB4F70_f4213`` 写本地 f4213 = pending-area enabled flag。
  全 0 的 00AD body 会把 f4213 清成 0，损坏 pending-area 状态，故零表 00AD 有害。

逆向依据（2026-07-13 IDA MCP SSE @127.0.0.1:13337, Stage3 权威 IDB）：
- ``sub_2F3F600(this, n)`` 读 option storage；``n > 0x4E (78)`` 返回 -1。
- ``sub_2F3F620(this, n, v)`` 写 option storage；``n <= 0x4E (78)`` 才落地。
- ``option_00ad_default_writer_sub_1876770`` 客户端启动默认值（79 项 u16）。
- ``option_00ad_dispatch_loop_sub_1877260`` S->C 0x00AD 入口；遍历 79 项后
  switch case（0/3/4/5/11/14/20/22/25/36/37/38/39/40/41/56/58/59/60/66/68/...）
  调到各 setter；其余 case 默认 LABEL_47。
- case 74 通过 ``option_setter_case_74_sub_1AB4F70_f4213`` 写 *(this + 4213)。
- body 真实 layout：
    u32 primary_len = 158
    79 × u16 options
    u32 ext1_len = 0
    u32 ext2_len = 0
  总 plain_len = 170B。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
_mod = load_chain('helper-00ad_defaults.py')
DEFAULT_GAME_OPTIONS = _mod.DEFAULT_GAME_OPTIONS
OPTION_COUNT = _mod.OPTION_COUNT
build_default_options_body = _mod.build_default_options_body
build_game_option_packet_body = _mod.build_game_option_packet_body


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def _env_hex_bytes(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    normalized = "".join(ch for ch in normalized if ch not in " \t\r\n:_-")
    if len(normalized) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits: {raw!r}")
    return bytes.fromhex(normalized)


def _parse_overrides_env(name: str) -> dict[int, int] | None:
    """解析 ``index=value,index=value`` 形式的 overrides。

    例：``USDOF_GAME_ENTER_SERIA_00AD_OVERRIDES=74=1,40=0``
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    out: dict[int, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            idx = int(k.strip(), 0)
            val = int(v.strip(), 0) & 0xFFFF
        except ValueError:
            continue
        if 0 <= idx < OPTION_COUNT:
            out[idx] = val
    return out


class EnterSeriaRoomGameOption00ADService:
    """构造进入赛利亚房间前 ``S->C flag0/opcode=0x00AD`` 候选包。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any) -> tuple[bytes | None, str]:
        """构造 ``S->C flag0/opcode=0x00AD``。

        默认 body 由 ``sub_1876770`` 反编译出的 79 项默认值；
        可通过 ``USDOF_GAME_ENTER_SERIA_00AD_OVERRIDES`` 覆盖特定 index。
        """
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_00AD_GAME_OPTION", False):
            return None, (
                "S->C flag0/opcode=0x00AD GAME_OPTION suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_00AD_GAME_OPTION=1 only for explicit A/B"
            )

        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_00AD_BODY")
        if body is None:
            overrides = _parse_overrides_env("USDOF_GAME_ENTER_SERIA_00AD_OVERRIDES")
            body = build_game_option_packet_body(overrides)
            body_note = (
                f"primary_len=158 option_count={OPTION_COUNT} "
                f"overrides={overrides if overrides else 'none'}"
            )
        else:
            body_note = f"body_override={body.hex(' ')}"

        flag = _env_int("USDOF_GAME_ENTER_SERIA_00AD_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_00AD_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x00AD, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x00AD, body)

        # 关键 safety check：opt[74] 必须 = 1（避免零表 00AD 把 f4213 清掉）。
        opt_74 = int.from_bytes(body[4 + 74 * 2 : 4 + 74 * 2 + 2], "little") & 0xFFFF
        safety_warn = "" if opt_74 == 1 else (
            f" WARNING opt[74]=0x{opt_74:04x} != 1; "
            "case 74 -> sub_1AB4F70 will NOT set pending-area f4213"
        )

        return packet, (
            "queued ENTER_SERIA GAME_OPTION "
            f"S->C flag{flag}/opcode=0x00AD mode={mode} {body_note} "
            f"opt[74]=0x{opt_74:04x} plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/opcode00ad-game_option.py; "
            "reverse basis: option_00ad_dispatch_loop_sub_1877260 reads 79 × u16; "
            "case 74 -> option_setter_case_74_sub_1AB4F70_f4213"
            f"{safety_warn}"
        )