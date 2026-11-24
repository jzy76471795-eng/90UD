"""Keep Shonan tournament reward cards in the native manual-click state."""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from game.chain_loader import load_chain


_tournament = load_chain("helper-dungeon_tournament.py")
_tournament_state = load_chain("opcode01ca-tournament_reward_select_state.py")


def collect_due_tournament_reward_manual_refresh_packets(
    server: Any,
    *,
    session: Any,
    now_monotonic: Optional[float] = None,
) -> Tuple[List[bytes], str]:
    """Return one S1/01CA refresh while leaving every card unselected."""
    due, note = _tournament.take_due_tournament_reward_manual_refresh(
        session,
        now_monotonic=now_monotonic,
    )
    if not due:
        return [], note

    packet = _tournament_state.build_tournament_reward_select_state_packet(
        server
    )
    return [packet], "tournament_manual_refresh=sent %s" % note
