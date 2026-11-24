"""C1/0x0026 monster hit/damage report (副本实现A.md §5.5.1).

Friend model: client-authoritative presentation; server ignores hit packets.
"""
from __future__ import annotations

from typing import Any


class MonsterHit0026Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        note = (
            "C1/0026 MONSTER_HIT plain_len=%s in_run=%s ignored_doc_client_auth; "
            "chain_file=game/chains/opcode0026-monster_hit.py"
            % (len(body or b""), in_run)
        )
        return [], note
