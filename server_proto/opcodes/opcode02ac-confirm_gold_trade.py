"""Authenticated compatibility reply for the Stage3 high-value spend gate.

015F4D30 requests C1/02AC after accumulated spend exceeds 10,000,000.
The registered S1/008E callback 015FE250 -> 015F4C90 resolves its pending
state. This acknowledgement moves no currency or items; each subsequent
purchase still passes through its authoritative transaction.
"""
from __future__ import annotations

from typing import Any


def handle_confirm_gold_trade_02ac(
    server: Any, plain: bytes, *, session: Any = None,
) -> tuple[list[bytes], str]:
    cid = getattr(session, "selected_character_id", None)
    aid = getattr(session, "account_id", None)
    accepted = False
    reason = "invalid_session"
    if bytes(plain):
        reason = "invalid_body"
    elif type(cid) is int and cid > 0 and type(aid) is int and aid > 0:
        with server._repo().open() as conn:
            row = conn.execute(
                "SELECT account_id FROM characters WHERE character_id=? AND delete_flag=0",
                (cid,),
            ).fetchone()
        accepted = row is not None and int(row[0]) == aid
        reason = "owned_character" if accepted else "ownership_mismatch"
    body = b"\x01" if accepted else b"\x00\x01"
    packet = server._codec_response(1, 0x008E, body)
    return [packet], "C1/02AC money_confirmation cid=%s accepted=%d reason=%s reply=S1/008E" % (
        cid, accepted, reason,
    )
