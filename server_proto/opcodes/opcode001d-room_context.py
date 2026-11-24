"""进入赛利亚房间链路 03：0x001D room/context 初始化候选包。

维护边界：
- 本文件只负责 ``C->S SELECT_CHARACTER`` 后、``S->C 0x0324`` 前的
  ``S->C flag0/opcode=0x001D`` A/B 构造。
- 该包默认关闭；只有显式设置
  ``USDOF_GAME_ENTER_SERIA_SEND_001D_ROOM_CONTEXT=1`` 时才发送。
- 这里不把 0x001D 当作“已确认进房间包”。它只是根据当前 EXE handler
  读序和 x32dbg 发现的 0x0324 缺失 ``context+0x37C`` 问题，提供下一轮
  动态验证入口。

逆向依据：
- 当前 sentinelfix EXE 的 registry 映射：
  ``0x001D -> 0x016411A0``。
- ``0x016411A0`` 的最小读序已经由 IDA MCP 复核：
  ``u8 n2_3, u8 n2816, u8 n2_1, u32 v166, u8 byte_4189A01,
  u8 byte_4189A02, u32 v155, u8 n2``，当 ``n2`` 不为 1/2 时可继续走
  后半段并读取 ``u8 v176, u8 n7``。
- ``0x016411A0`` 可在分支中调用 ``sub_2BBE960``，该 setter 写入
  context ``+0x37C/+0x380/+0x384``。上一轮 x32dbg 证明 0x0324 卡在
  ``0x02BA1110`` 返回 0，也就是 ``context+0x37C`` 尚未初始化。

禁止踩坑：
- 0x001D 的 codec index 为 1；必须走已逆向到的 idx1 transport，
  不能用 raw/plain wire 偷跑。
- 字段语义尚未全部动态确认，默认 body 只用于显式 A/B；如果动态证明
  不命中 setter 或造成异常，保持默认关闭并继续逆 handler 分支。
"""
from __future__ import annotations

import os
from typing import Any


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


class EnterSeriaRoomContext001DService:
    """构造 0x001D room/context 初始化候选包。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any) -> tuple[bytes | None, str]:
        """构造 ``S->C flag0/opcode=0x001D``。

        默认体是 0x016411A0 的最小读序体，字段名沿用反编译局部变量，
        避免把尚未确认的语义写死。
        """

        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_001D_ROOM_CONTEXT", False):
            return None, (
                "S->C flag0/opcode=0x001D room-context suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_001D_ROOM_CONTEXT=1 only for explicit A/B"
            )

        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_001D_BODY")
        if body is None:
            n2_3 = _env_int("USDOF_GAME_ENTER_SERIA_001D_N2_3", 0) & 0xFF
            n2816 = _env_int("USDOF_GAME_ENTER_SERIA_001D_N2816", 0) & 0xFF
            n2_1 = _env_int("USDOF_GAME_ENTER_SERIA_001D_N2_1", 0) & 0xFF
            v166 = _env_int("USDOF_GAME_ENTER_SERIA_001D_V166", 0) & 0xFFFFFFFF
            byte_4189a01 = _env_int("USDOF_GAME_ENTER_SERIA_001D_BYTE_4189A01", 0) & 0xFF
            byte_4189a02 = _env_int("USDOF_GAME_ENTER_SERIA_001D_BYTE_4189A02", 0) & 0xFF
            v155 = _env_int("USDOF_GAME_ENTER_SERIA_001D_V155", 0) & 0xFFFFFFFF
            n2 = _env_int("USDOF_GAME_ENTER_SERIA_001D_N2", 0) & 0xFF
            v176 = _env_int("USDOF_GAME_ENTER_SERIA_001D_V176", 0) & 0xFF
            n7 = _env_int("USDOF_GAME_ENTER_SERIA_001D_N7", 0xFF) & 0xFF
            body = (
                bytes([n2_3, n2816, n2_1])
                + v166.to_bytes(4, "little")
                + bytes([byte_4189a01, byte_4189a02])
                + v155.to_bytes(4, "little")
                + bytes([n2, v176, n7])
            )
            body_note = (
                f"n2_3={n2_3} n2816={n2816} n2_1={n2_1} v166={v166} "
                f"byte_4189A01={byte_4189a01} byte_4189A02={byte_4189a02} "
                f"v155={v155} n2={n2} v176={v176} n7={n7}"
            )
        else:
            body_note = f"body_override={body.hex(' ')}"

        flag = _env_int("USDOF_GAME_ENTER_SERIA_001D_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_001D_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x001D, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x001D, body)

        return packet, (
            "queued ENTER_SERIA room-context candidate "
            f"S->C flag{flag}/opcode=0x001D mode={mode} {body_note} "
            f"plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/opcode001d-room_context.py; "
            "reverse basis: registry maps opcode0x001D to 0x016411A0; "
            "minimal reads u8,u8,u8,u32,u8,u8,u32,u8,u8,u8; "
            "handler can call sub_2BBE960 to initialize context+0x37C before 0x0324"
        )
