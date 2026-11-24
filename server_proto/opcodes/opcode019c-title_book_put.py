"""C->S 0x019C 称号簿放入 / 卸下（TITLE_BOOK PUT）。

Equipment(3) 源：装备栏称号卸回称号簿。
"""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_helper = load_chain("helper-titlebook_equip.py")
TitleBookEquipService = _helper.TitleBookEquipService


class TitleBookPut019CService:
    def __init__(self, server: Any) -> None:
        self.server = server
        self._inner = TitleBookEquipService(server)

    def handle_title_book_put_019c(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        return self._inner.handle_title_book(body, opcode=0x019C, session=session)
