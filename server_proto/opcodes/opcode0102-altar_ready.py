"""C1/0102 altar UI-ready signal and first-wave trigger."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

AltarRandomFlow = load_chain("flow-altar-random.py").AltarRandomFlow


def handle_altar_ready_0102(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return AltarRandomFlow(server).handle_ready(body, session=session)
