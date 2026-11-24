"""C1/0356 special-dungeon selector state request."""
from __future__ import annotations

from typing import Any, List, Tuple

from game.chain_loader import load_chain


_selector = load_chain("helper-special_dungeon_selector.py")


class SpecialDungeonSelectorState0356Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle(self, body: bytes, *, session: Any = None) -> Tuple[List[bytes], str]:
        context, note = _selector.selector_request_context(
            self.server._repo(),
            session,
            body,
        )
        if context is None:
            return [], "C1/0356 %s" % note
        player_uid, character_id, account_id, progress = context
        response_body = _selector.build_selector_state_0317_body(
            player_uid,
        )
        return [self.server._codec_response(0, 0x0317, response_body)], (
            "C1/0356 player_uid=%d account=%d cid=%d progress_keys=%d; "
            "S0/0317 blob=0x%X"
            % (
                player_uid,
                account_id,
                character_id,
                len(progress),
                len(response_body) - 4,
            )
        )


__all__ = ["SpecialDungeonSelectorState0356Service"]
