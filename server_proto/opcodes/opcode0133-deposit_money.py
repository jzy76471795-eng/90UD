"""C1/0133 deposit a four-byte amount into account cargo."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_cargo_gold = load_chain("helper-account_cargo_gold.py")


def handle_deposit_money_0133(server: Any, body: bytes, *, session: Any = None):
    return _cargo_gold.handle_account_cargo_gold(
        server, body, opcode=0x0133, direction="deposit", session=session,
    )
