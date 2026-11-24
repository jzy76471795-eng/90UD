"""C1/0045 score-scroll timer signal for an ordinary free-card auto flip."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain


_auto_flip = load_chain("flow-card_reward_auto_flip.py")


def handle_score_scroll_state_0045(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    """Use the bodyless Stage3 timer signal to send the free-card result."""
    raw = bytes(body or b"")
    if raw:
        return [], "C1/0045 SCORE_SCROLL_STATE reject=len=%d" % len(raw)

    flip_packets, flip_note = (
        _auto_flip.collect_client_card_reward_auto_flip_packets(
            server,
            session=session,
        )
    )
    return list(flip_packets), (
        "C1/0045 SCORE_SCROLL_STATE trigger=direct_free_0047; %s" % flip_note
    )
