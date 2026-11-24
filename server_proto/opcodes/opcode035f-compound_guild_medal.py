"""C1/S1 035F guild-medal compound flow."""
from __future__ import annotations

from typing import Any

from game.character.guild_item_compound_index import KIND_GUILD_MEDAL
from game.chain_loader import load_chain


_shared = load_chain("opcode0342-compound_guild_item.py")


def parse_compound_guild_medal_request(body: bytes):
    return _shared.parse_compound_guild_item_request(body)


class CompoundGuildMedal035FService(_shared.CompoundGuildItemService):
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        super().__init__(
            server,
            opcode=0x035F,
            expected_kind=KIND_GUILD_MEDAL,
            operation_name="COMPOUND_GUILD_MEDAL",
            rng=rng,
        )


def handle_compound_guild_medal_035f(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CompoundGuildMedal035FService(server).handle(body, session=session)


__all__ = [
    "CompoundGuildMedal035FService",
    "handle_compound_guild_medal_035f",
    "parse_compound_guild_medal_request",
]
