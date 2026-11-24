"""进入赛利亚房间链路 05：S->C 0x00D3 上下文 setter 候选。

维护边界：
- 本文件只负责 SELECT_CHARACTER 之后、0x0324 之前可选发送的
  ``S->C flag0/opcode=0x00D3``（registry ``sub_1AB3F40(211, sub_1642F20)``）。
- 默认关闭。只有显式
  ``USDOF_GAME_ENTER_SERIA_SEND_00D3_CONTEXT=1`` 时才发送。
- 目标：尝试走到 ``sub_2BBE960``，为 0324 依赖的 ``object+0x37C`` 写入候选值。

逆向依据（2026-07-12 IDA worker）：
- registry builder ``0x01649540`` 注册 ``opcode=211 (0x00D3) -> 0x01642F20``。
- ``0x01642F20`` 最小读序：
  ``u8 a, u8 b, u32 c, u8 byte_4189A01, u8 n2``。
- 当 ``n2`` 不是 1/2 时跳过重型循环；若 ``sub_1682BD0(10)`` 返回非空且
  ``sub_1AB6740(...)==2``，则调用 ``sub_2BBE960`` 写 context ``+0x37C/+0x380/+0x384``。
- 0324 崩溃点 ``0x0165584E`` 是 ``vtable+0x16C (0x02BA1110)`` 读 ``+0x37C`` 为空。

禁止：
- 不得把本包设为默认开启。
- 不得把 90 起源 enum 中文名当作字段真相。
- 001D 已多次动态证伪为“随便开就能填 37C”，本文件不替代 001D 结论。
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


class EnterSeriaRoomContext00D3Service:
    """构造 0x00D3 上下文 setter 候选包。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any) -> tuple[bytes | None, str]:
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_00D3_CONTEXT", False):
            return None, (
                "S->C flag0/opcode=0x00D3 context suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_00D3_CONTEXT=1 only for explicit A/B"
            )

        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_00D3_BODY")
        if body is None:
            # Minimal read order of sub_1642F20 before n2 branch.
            # n2 default 0 skips heavy loops; setter still attempted if area object exists
            # and world-type helper returns 2.
            a = _env_int("USDOF_GAME_ENTER_SERIA_00D3_A", 0) & 0xFF
            b = _env_int("USDOF_GAME_ENTER_SERIA_00D3_B", 0) & 0xFF
            c = _env_int("USDOF_GAME_ENTER_SERIA_00D3_C", 0) & 0xFFFFFFFF
            byte_4189a01 = _env_int("USDOF_GAME_ENTER_SERIA_00D3_BYTE_4189A01", 0) & 0xFF
            n2 = _env_int("USDOF_GAME_ENTER_SERIA_00D3_N2", 0) & 0xFF
            body = (
                bytes([a, b])
                + c.to_bytes(4, "little")
                + bytes([byte_4189a01, n2])
            )
            body_note = (
                f"a={a} b={b} c={c} byte_4189a01={byte_4189a01} n2={n2} "
                f"(n2 not in {{1,2}} skips heavy loops)"
            )
        else:
            body_note = f"body_override={body.hex(' ')}"

        flag = _env_int("USDOF_GAME_ENTER_SERIA_00D3_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_00D3_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x00D3, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x00D3, body)

        return packet, (
            "queued ENTER_SERIA context-00D3 candidate "
            f"S->C flag{flag}/opcode=0x00D3 mode={mode} {body_note} "
            f"plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/opcode00d3-room_context.py; "
            "reverse: registry 0x01649540 maps 211/0x00D3 -> 0x01642F20; "
            "when area object via sub_1682BD0(10) exists and sub_1AB6740==2, "
            "calls sub_2BBE960 to write context +0x37C before 0x0324"
        )
