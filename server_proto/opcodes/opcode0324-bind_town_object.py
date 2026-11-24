"""进入赛利亚房间链路 02：绑定客户端城镇/频道对象的 S->C 0x0324。

维护边界：
- 本文件只负责 ``C->S SELECT_CHARACTER`` 之后、``S->C 0x0004 ACK`` 之前的
  ``S->C flag0/opcode=0x0324`` 构造。
- 不在这里追加 item list、角色状态、房间对象列表等后续包；后续每确认一个
  发/收包动作产生的新链路，继续新建 ``enter_seria_room_03_*``、
  ``enter_seria_room_04_*``。

逆向依据：
- 当前 sentinelfix EXE 的 active registry ``0x016713FC | 0x0324`` 映射到
  ``0x01655800``。
- ``0x01655800`` 的读取顺序已经由反汇编确认：
  1. ``PacketReader_read_u32`` -> 本地变量 ``edi``，随后传入
     ``sub_1E7F0F0(edi)`` 并用于匹配客户端已有的城镇/频道对象 id。
  2. ``PacketReader_read_u8`` -> 本地变量 ``[ebp-1]``，后续作为对象子索引/
     区域索引传入 ``a93d50/a93cd0/a93d70/a93cf0`` 等路径。
- 当 ``edi`` 匹配当前客户端对象且子索引有效时，``0x01655A05`` 会写入
  ``dword_04294CE8``（n2500）。此前动态 trace 已证明 ``S->C 0x0004`` ACK
  到达时 n2500 为 0，导致 ACK handler 在 ``0x0167258E`` 直接返回。

2026-07-08 动态 A/B 已确认：``town_id=1`` 与 ``town_id=0x04F8`` 都能命中
``0x01655800`` 并按上述顺序读取，但 ``sub_1E7F0F0`` 返回 0，未命中
``0x01655A05``。这说明 0x0324 依赖一个更早已插入客户端对象管理器的对象，
不能再作为默认回包；本文件保留为显式环境变量 A/B 工具。
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


class EnterSeriaRoomBindTownObject0324Service:
    """构造进入赛利亚房间前的城镇对象绑定包。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any) -> tuple[bytes | None, str]:
        """构造 ``S->C flag0/opcode=0x0324``。

        中文说明：
        - 默认禁用：动态 A/B 已证明当前缺少先导对象时不会写 n2500。
        - 仅当需要复现实验时，通过
          ``USDOF_GAME_ENTER_SERIA_SEND_0324_BIND=1`` 显式启用。
        """

        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0324_BIND", False):
            return None, (
                "S->C flag0/opcode=0x0324 town-object bind suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0324_BIND=1 only for explicit A/B"
            )

        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0324_BODY")
        if body is None:
            # Stage3 0x01655800 loops twice: do { read u32 key; read u8 area; ... } while (n2 < 2)
            # Emit two (u32,u8) pairs so both iterations share the same object key.
            # town_id/area_id come from the bound Character row (DB), never hard-coded:
            # the DB is the source of truth for where this character "should" enter.
            # Override only via USDOF_GAME_ENTER_SERIA_0324_TOWN_ID=...
            # and USDOF_GAME_ENTER_SERIA_0324_AREA_ID=... when running A/B against
            # a different PVF town key.
            town_id = _env_int(
                "USDOF_GAME_ENTER_SERIA_0324_TOWN_ID",
                int(getattr(selected, "town_id", 0)),
            ) & 0xFFFFFFFF
            area_id = _env_int(
                "USDOF_GAME_ENTER_SERIA_0324_AREA_ID",
                int(getattr(selected, "area_id", 0)),
            ) & 0xFF
            pair_count = max(1, _env_int("USDOF_GAME_ENTER_SERIA_0324_PAIR_COUNT", 2))
            pair = town_id.to_bytes(4, "little") + bytes([area_id])
            body = pair * pair_count
            body_note = f"town_id={town_id} area_id={area_id} pair_count={pair_count} pairs={body.hex(' ')}"
        else:
            body_note = f"body_override={body.hex(' ')}"

        flag = _env_int("USDOF_GAME_ENTER_SERIA_0324_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0324_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0324, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0324, body)

        return packet, (
            "queued ENTER_SERIA town-object bind "
            f"S->C flag{flag}/opcode=0x0324 mode={mode} {body_note} "
            f"plain_len={len(body)} wire={len(packet)}; "
            "chain_file=game/chains/opcode0324-bind_town_object.py; "
            "reverse basis: registry 0x016713FC maps opcode0x0324 to 0x01655800; "
            "handler loops twice reading u32+u8; n2500 write at 0x01655A05 when key matches current object"
        )
