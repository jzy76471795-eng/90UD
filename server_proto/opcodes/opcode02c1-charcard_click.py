"""角色选择页角色卡确认 (C->S 0x02C1)。

从 ``game_server.py`` 拆出的角色相关补包链。通过 ``self.server`` 复用
GameServer 的 DB/codec/日志等基础设施，避免循环依赖。
"""
from __future__ import annotations

from typing import Any


def parse_charcard_slot_02c1(body: bytes) -> int | None:
    """Stage3 writer ``0x02B89225`` emits exactly one ``u8 slot``."""
    if not body:
        return None
    return body[0]


class CharCharcardClickService:
    """角色选择页角色卡确认 (C->S 0x02C1)。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_charcard_click_02c1(self, body: bytes) -> tuple[list[bytes], str]:
        """Clear the Stage3 character-card wait state with the native failure ACK.

        Current authoritative IDB evidence (2026-07-24):
          - ``0x02B89225`` writes the requested slot with ``WriteU8`` and then
            sets the wait byte at ``this+0xA34``.
          - S2C reader ``0x015FED10`` treats the first body byte as success.
            Failure only calls ``0x02B76190`` to clear the wait and terminates
            the selection path.
          - Success consumes ``u8 slot + u8 mode + 8*u32`` and updates card
            state.  Live A/B showed that this branch does not initiate the
            normal ``C1/0004`` character-entry path.

        Keep the pre-regression one-byte failure response for every slot.  It
        only clears the wait byte and leaves normal character selection to the
        client's existing state machine.
        """
        slot = parse_charcard_slot_02c1(body)
        packet = self.server._codec_response(1, 0x02C1, b"\x00")
        return [packet], (
            f"CHARCARD_CLICK/0x02c1 slot={slot}; sent native FAIL ack -> "
            f"client clears wait this+0xA34 without card-state mutation; wire={len(packet)}"
        )
