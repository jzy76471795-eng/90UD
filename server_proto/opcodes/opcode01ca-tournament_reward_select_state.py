"""C1/01CA tournament final-card interaction state."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain


_tournament = load_chain("helper-dungeon_tournament.py")
# The generic Stage3 S1 dispatcher consumes the success byte before invoking
# reader 0x015F6A30.  The reader then consumes four party slots for each of
# the two reward groups.  These arenas are solo-only, so only slot zero is
# present for both groups.
_STAGE3_REWARD_SELECT_STATE_BODY = bytes(
    (1, 1, 0xFF, 0xFF, 0xFF, 1, 0xFF, 0xFF, 0xFF)
)


class TournamentRewardSelectState01CAService:
    """Open only the completed Shonan tournament card interaction."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        raw = bytes(body or b"")
        if raw:
            return [], "C1/01CA TOURNAMENT_REWARD_SELECT_STATE reject=len=%d" % len(raw)

        opened, note = _tournament.open_tournament_reward_selection(session)
        if not opened:
            return [], "C1/01CA TOURNAMENT_REWARD_SELECT_STATE %s" % note

        # Stage3 S1 dispatcher 0x01AB3810 consumes the leading success byte;
        # reader 0x015F6A30 then enters UI state 6 and consumes the remaining
        # eight bytes. State 6 is where card clicks emit C1/01CB.
        packet = build_tournament_reward_select_state_packet(self.server)
        return [packet], (
            "C1/01CA TOURNAMENT_REWARD_SELECT_STATE reply=S1/01CA "
            "codec len=%d body=%s %s"
            % (
                len(_STAGE3_REWARD_SELECT_STATE_BODY),
                _STAGE3_REWARD_SELECT_STATE_BODY.hex(),
                note,
            )
        )


def build_tournament_reward_select_state_packet(server: Any) -> bytes:
    """Build the confirmed Stage3 state-6 projection for initial or refresh use."""
    return server._codec_response(
        1,
        0x01CA,
        _STAGE3_REWARD_SELECT_STATE_BODY,
    )
