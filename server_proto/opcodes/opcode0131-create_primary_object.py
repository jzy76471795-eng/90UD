"""进入赛利亚房间链路 02：创建客户端主 town/object 对象的 S->C 0x0131。

维护边界：
- 本文件只负责 ``C->S SELECT_CHARACTER`` 之后、``S->C 0x0324`` 绑定包和
  ``S->C 0x0004`` ACK 之前的 ``S->C flag0/opcode=0x0131`` A/B 构造。
- 该包当前默认关闭。只有显式设置
  ``USDOF_GAME_ENTER_SERIA_SEND_0131_PRIMARY_OBJECT=1`` 时才发送。
- 若 x32dbg 证明本链路是必需且字段稳定，才允许把默认值改成开启；在此之前
  不得把它当成常规进入房间流。

逆向依据：
- IDA MCP 已确认当前 sentinelfix IDB/EXE 匹配。
- active registry ``0x016713FC | 0x0131`` 映射到 ``0x01654C80``。
- ``0x01654C80`` 读序为：
  ``u16 key, u32 x, u32 y, u16 z_or_state, u32 value_a, u8 area_or_state,
  u32 value_b, u8 tail_flag``。
- 该 handler 先用 ``sub_1E7F0F0(key)`` 查 ``dword_4189A88+0x04`` 主对象表；
  未命中时会调用 ``0x01E80E00``，后者分配 ``0x820`` 对象并通过
  ``0x01869000`` 插入 ``manager+0x04``。
- 后续 ``S->C 0x0324`` 依赖同一个主对象表；此前动态 A/B 已证明仅发送
  ``0x0324`` 查不到对象，无法写 ``n2500``。
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


class EnterSeriaRoomCreatePrimaryObject0131Service:
    """构造进入赛利亚房间前的主对象创建候选包。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any) -> tuple[bytes | None, str]:
        """构造 ``S->C flag0/opcode=0x0131``。

        默认字段只用于 A/B：
        - key 默认取 ``0``，因为当前 x32dbg 对全局 town key codec 的动态解码
          结果是 ``decoded_key_u16=0``；可用 ``*_KEY`` 覆盖。
        - x/y 默认取 DB 中角色坐标。
        - 其余字段先保守发 0 或当前 DB town/area，实际语义等 x32dbg 命中
          ``0x01654C80 -> 0x01E80E00 -> 0x01E80E78`` 后再收敛。
        """

        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0131_PRIMARY_OBJECT", False):
            return None, (
                "S->C flag0/opcode=0x0131 primary-object create suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0131_PRIMARY_OBJECT=1 only for explicit A/B"
            )

        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0131_BODY")
        if body is None:
            key = _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0) & 0xFFFF
            x = _env_int("USDOF_GAME_ENTER_SERIA_0131_X", int(getattr(selected, "pos_x", 0))) & 0xFFFFFFFF
            y = _env_int("USDOF_GAME_ENTER_SERIA_0131_Y", int(getattr(selected, "pos_y", 0))) & 0xFFFFFFFF
            z_or_state = _env_int("USDOF_GAME_ENTER_SERIA_0131_Z_OR_STATE", 0) & 0xFFFF
            value_a = _env_int("USDOF_GAME_ENTER_SERIA_0131_VALUE_A", int(getattr(selected, "town_id", 1))) & 0xFFFFFFFF
            area_or_state = _env_int("USDOF_GAME_ENTER_SERIA_0131_AREA_OR_STATE", int(getattr(selected, "area_id", 0))) & 0xFF
            value_b = _env_int("USDOF_GAME_ENTER_SERIA_0131_VALUE_B", 0) & 0xFFFFFFFF
            tail_flag = _env_int("USDOF_GAME_ENTER_SERIA_0131_TAIL_FLAG", 0) & 0xFF
            body = (
                key.to_bytes(2, "little")
                + x.to_bytes(4, "little")
                + y.to_bytes(4, "little")
                + z_or_state.to_bytes(2, "little")
                + value_a.to_bytes(4, "little")
                + bytes([area_or_state])
                + value_b.to_bytes(4, "little")
                + bytes([tail_flag])
            )
            body_note = (
                f"key={key} x={x} y={y} z_or_state={z_or_state} value_a={value_a} "
                f"area_or_state={area_or_state} value_b={value_b} tail_flag={tail_flag}"
            )
        else:
            body_note = f"body_override={body.hex(' ')}"

        flag = _env_int("USDOF_GAME_ENTER_SERIA_0131_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0131_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0131, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0131, body)

        return packet, (
            "queued ENTER_SERIA primary-object create "
            f"S->C flag{flag}/opcode=0x0131 mode={mode} {body_note} "
            f"plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/opcode0131-create_primary_object.py; "
            "reverse basis: registry 0x016713FC maps opcode0x0131 to 0x01654C80; "
            "handler can call 0x01E80E00 and insert manager+0x04 at 0x01E80E78"
        )
