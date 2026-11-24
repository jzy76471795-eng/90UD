"""Account-cargo money transfer; pinned Stage3 0133/0134 native contract.

C1 body is exactly one 4-byte amount. S1 success is 01 + new cargo u32;
failure is 00 + error byte. Native callback applies the pending cash delta,
so an authoritative S0 wallet snapshot must follow the success callback.
"""
from __future__ import annotations

import struct
from typing import Any

from game.chain_loader import load_chain

_wallet_payloads = load_chain("helper-dungeon_payloads.py")
_REJECT_REASONS = frozenset({
    "amount_body_width", "amount_zero", "session_binding",
    "character_account_mismatch", "repository_error", "character_missing",
    "account_missing", "wallet_transfer_direction_invalid", "wallet_transfer_amount_invalid",
    "wallet_gold_insufficient", "cargo_gold_insufficient", "cargo_gold_overflow",
    "wallet_gold_overflow", "account_cargo_closed", "cargo_gold_invalid",
    "cargo_gold_business_cap_exceeded",
})


def handle_account_cargo_gold(
    server: Any, body: bytes, *, opcode: int, direction: str, session: Any = None,
) -> tuple[list[bytes], str]:
    prefix = "account_cargo_gold opcode=%04X direction=%s" % (opcode, direction)
    cid = aid = amount = 0

    def business_log(message: str) -> None:
        logger = getattr(server, "log", None)
        if callable(logger):
            try:
                logger(message)
            except Exception:
                pass

    def reject(reason: str, error_code: int = 0x7B) -> tuple[list[bytes], str]:
        # Current native common7B consumes a two-byte failure without text.
        # Dedicated messages are used only for verified DSTR meanings.
        if reason not in _REJECT_REASONS:
            reason = "transfer_rejected"
        business_log(
            "[Game/CargoGold] opcode=%04X action=%s actor=%d account_id=%d amount=%d status=reject reason=%s"
            % (opcode, direction, cid, aid, amount, reason)
        )
        return [server._codec_response(1, opcode, bytes((0, error_code)))], prefix + " reject=" + reason

    if len(body) != 4:
        return reject("amount_body_width")
    amount = struct.unpack("<I", body)[0]
    prefix += " amount=%d" % amount
    if amount == 0:
        return reject("amount_zero")
    try:
        cid = int(getattr(session, "selected_character_id", None) or 0)
        aid = int(getattr(session, "account_id", None) or 0)
    except (TypeError, ValueError):
        return reject("session_binding")
    if cid <= 0 or aid <= 0:
        return reject("session_binding")
    try:
        repo = server._repo()
        if repo.get_account_id_for_character(cid) != aid:
            return reject("character_account_mismatch")
        ok, note, result = repo.transfer_wallet_gold(cid, direction, amount)
    except Exception:
        # Repository owns rollback; expose no SQL text, path or raw request.
        return reject("repository_error")
    if not ok:
        error_code = 0x7B
        if direction == "withdraw" and note == "cargo_gold_insufficient":
            error_code = 0x0A
        elif direction == "deposit" and note in {"cargo_gold_overflow", "cargo_gold_business_cap_exceeded"}:
            error_code = 0x5F
        return reject(str(note), error_code)
    cargo_gold = int(result["cargo_gold"])
    gold = int(result["gold"])
    business_log(
        "[Game/CargoGold] opcode=%04X action=%s actor=%d account_id=%d amount=%d gold=%d cargo_gold=%d status=ok"
        % (opcode, direction, cid, aid, amount, gold, cargo_gold)
    )
    ack = server._codec_response(1, opcode, b"\x01" + struct.pack("<I", cargo_gold))
    wallet = server._codec_response(
        0, 0x000E, _wallet_payloads.build_wallet_gold_update_000e(gold),
    )
    return [ack, wallet], (
        prefix + " ok character_id=%d account_id=%d gold=%d cargo_gold=%d"
        % (cid, aid, gold, cargo_gold)
    )
