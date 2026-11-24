"""C->S 0x019D 称号簿取出 / 装备（TITLE_BOOK GET）。

Live：itemSpace=Equipment(3) slot=13 item_id=称号 从称号簿穿到装备栏。
"""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_helper = load_chain("helper-titlebook_equip.py")
TitleBookEquipService = _helper.TitleBookEquipService


class TitleBookGet019DService:
    def __init__(self, server: Any) -> None:
        self.server = server
        self._inner = TitleBookEquipService(server)

    def handle_title_book_get_019d(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        return self._inner.handle_title_book(body, opcode=0x019D, session=session)
