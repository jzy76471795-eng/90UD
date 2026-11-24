"""C1/0134 withdraw a four-byte amount from account cargo."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain

_cargo_gold = load_chain("helper-account_cargo_gold.py")


def handle_withdraw_money_0134(server: Any, body: bytes, *, session: Any = None):
    return _cargo_gold.handle_account_cargo_gold(
        server, body, opcode=0x0134, direction="withdraw", session=session,
    )
