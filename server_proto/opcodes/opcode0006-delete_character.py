"""删除角色 (C->S 0x0006)

从 ``game_server.py`` 拆出的角色相关补包链。通过 ``self.server`` 复用
GameServer 的 DB/codec/日志等基础设施，避免循环依赖。
"""
from __future__ import annotations

import os
from typing import Any

from game.character.character_db import parse_delete_character_body

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


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return int(str(raw).strip(), 0)


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


class CharDeleteCharacterService:
    """删除角色 (C->S 0x0006)"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_delete_character(self, body: bytes) -> tuple[list[bytes], str]:
        parsed = parse_delete_character_body(body)
        if parsed is None:
            packet = self.server._codec_response(1, 0x0006, b"\x00\x02")
            return [packet], "DELETE_CHARACTER body not plaintext/plausible; sent codec6 failure status=0x02"
        slot_index, name, name_raw, source = parsed
        repo = self.server._repo()
        characters = repo.list_by_account(self.server.account_id)

        def _name_eq(record: Any) -> bool:
            return record.name_bytes == name_raw or record.display_name == name

        # 名字是包内权威字段（characters.name UNIQUE）。slot 在空洞场景下可能是
        # 客户端稠密 UI 序号，不能单独当真源：曾出现 slot=1/name=EEEE 实际命中 CCC。
        by_name = next((c for c in characters if _name_eq(c)), None)
        by_slot = next((c for c in characters if int(c.slot_index) == int(slot_index)), None)
        if by_slot is None and 0 <= int(slot_index) < len(characters):
            # legacy fallback: dense 0..n-1 list index（list 已按 slot 升序）
            by_slot = characters[int(slot_index)]

        resolve_note = ""
        if by_name is not None:
            target = by_name
            if by_slot is not None and int(by_slot.character_id) != int(by_name.character_id):
                resolve_note = (
                    f" slot-mismatch-resolved-by-name client_slot={slot_index}"
                    f" slot_owner={by_slot.display_name!r}"
                    f" real_slot={by_name.slot_index}"
                )
            elif int(by_name.slot_index) != int(slot_index):
                resolve_note = (
                    f" slot-gap-resolved-by-name client_slot={slot_index}"
                    f" real_slot={by_name.slot_index}"
                )
        elif by_slot is not None:
            # 无同名角色：仅当 slot 命中且名字也对得上才删，否则仍报 mismatch
            if not _name_eq(by_slot):
                packet = self.server._codec_response(1, 0x0006, b"\x00\x15")
                return [packet], (
                    f"DELETE_CHARACTER name mismatch slot={slot_index} "
                    f"expected={by_slot.display_name!r} got={name!r}; "
                    "sent codec6 failure status=0x15"
                )
            target = by_slot
        else:
            packet = self.server._codec_response(1, 0x0006, b"\x00\x02")
            return [packet], (
                f"DELETE_CHARACTER slot={slot_index} name={name!r} not found "
                f"count={len(characters)} source={source}; "
                "sent codec6 failure status=0x02"
            )

        # 玩家明确要求:删除角色必须物理删除,禁止逻辑删除。物理删除后角色名彻底释放,
        # 可被重新创建,不再触发 characters.name UNIQUE 冲突导致创建卡住。
        # physical_delete 内会把剩余槽位收口为 0..n-1。
        deleted_slot = int(target.slot_index)
        repo.physical_delete(target.character_id)
        # 回显客户端请求的 slot（客户端本地选中索引），不是 densify 后的新映射。
        success_body = b"\x01\x00" + (slot_index & 0xFFFF).to_bytes(2, "little")
        packets = [self.server._codec_response(1, 0x0006, success_body)]
        # 玩家要求:删除后必须刷新角色列表。默认发送 USERINFO 刷新(可用
        # USDOF_DELETE_CHARACTER_SEND_USERINFO_REFRESH=0 关闭做 A/B 对比)。
        refresh_note = "delete USERINFO refresh suppressed by USDOF_DELETE_CHARACTER_SEND_USERINFO_REFRESH=0"
        if _env_bool("USDOF_DELETE_CHARACTER_SEND_USERINFO_REFRESH", True):
            userinfo_packets, userinfo_note = self.server._db_userinfo_packet_stream(
                "delete-character-refresh",
                include_followups=False,
            )
            packets.extend(userinfo_packets)
            refresh_note = userinfo_note
        return packets, (
            f"DELETE_CHARACTER physically-deleted character_id={target.character_id} "
            f"name={target.display_name!r} db_slot={deleted_slot}{resolve_note}; "
            f"sent codec6 success body={success_body.hex(' ')} + {refresh_note}"
        )

