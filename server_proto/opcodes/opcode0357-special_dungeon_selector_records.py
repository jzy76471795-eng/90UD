"""C1/0357 special-dungeon selector record request."""
from __future__ import annotations

from typing import Any, List, Tuple

from game.chain_loader import load_chain


_selector = load_chain("helper-special_dungeon_selector.py")


class SpecialDungeonSelectorRecords0357Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle(self, body: bytes, *, session: Any = None) -> Tuple[List[bytes], str]:
        context, note = _selector.selector_request_context(
            self.server._repo(),
            session,
            body,
        )
        if context is None:
            return [], "C1/0357 %s" % note
        player_uid, character_id, account_id, progress = context
        response_body = _selector.build_selector_records_0318_body(
            player_uid,
            progress,
        )
        return [self.server._codec_response(0, 0x0318, response_body)], (
            "C1/0357 player_uid=%d account=%d cid=%d progress_keys=%d max=%d; "
            "S0/0318 blob=0x%X"
            % (
                player_uid,
                account_id,
                character_id,
                len(progress),
                max(progress.values()) if progress else 0,
                len(response_body) - 4,
            )
        )


__all__ = ["SpecialDungeonSelectorRecords0357Service"]
