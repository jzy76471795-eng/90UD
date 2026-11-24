"""Game-thread GM commands for account Cera and wallet gold grants."""
from __future__ import annotations

from typing import Any

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character.character_db import (
    ACCOUNT_CERA_STORAGE_MAX,
    CLIENT_GOLD_DISPLAY_CAP,
)


_DUNGEON_PAYLOADS = load_chain("helper-dungeon_payloads.py")
build_wallet_gold_update_000e = _DUNGEON_PAYLOADS.build_wallet_gold_update_000e

GM_CERA_ADD_MAX = ACCOUNT_CERA_STORAGE_MAX
GM_GOLD_ADD_MAX = CLIENT_GOLD_DISPLAY_CAP


class GmCurrencyControlCommands:
    def __init__(self, server: Any) -> None:
        self.server = server

    @staticmethod
    def _character_id(session: Any) -> int:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            raise ControlApiError(
                "SESSION_NOT_READY",
                "current client has no selected character",
                status=409,
            )
        return character_id

    @staticmethod
    def _amount(params: dict[str, Any], maximum: int) -> int:
        if not isinstance(params, dict) or set(params) != {"amount"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "currency command requires only amount",
                status=400,
            )
        if type(params['amount']) is not int:
            raise ControlApiError('INVALID_PARAMS','amount must be an integer',status=400)
        try:
            amount = int(params["amount"])
        except (KeyError, TypeError, ValueError):
            raise ControlApiError(
                "INVALID_PARAMS",
                "amount must be an integer",
                status=400,
            )
        if not 1 <= amount <= int(maximum):
            raise ControlApiError(
                "INVALID_PARAMS",
                "amount must be between 1 and %d" % int(maximum),
                status=400,
            )
        return amount

    def add_cera(self, params: dict[str, Any], session: Any):
        amount = self._amount(params, GM_CERA_ADD_MAX)
        character_id = self._character_id(session)
        repo = self.server._repo()
        account_id = repo.get_account_id_for_character(character_id)
        if account_id is None:
            raise ControlApiError("ACCOUNT_MISSING", "account_missing", status=409)
        ok, note, balance = repo.grant_account_cera(int(account_id), amount)
        if not ok:
            code = "CURRENCY_LIMIT_REACHED" if note == "cera_display_cap" else "CERA_ADD_FAILED"
            raise ControlApiError(code, note, status=409)
        return [], {
            "character_id": character_id,
            "account_id": int(account_id),
            "amount": amount,
            "balance": int(balance),
            "client_sync": "next_start",
            "packet_flow": [],
        }, "GM Cera add %s" % note

    def add_gold(self, params: dict[str, Any], session: Any):
        amount = self._amount(params, GM_GOLD_ADD_MAX)
        character_id = self._character_id(session)
        repo = self.server._repo()
        wallet_ok, wallet_note, wallet = repo.get_account_wallet(character_id)
        if not wallet_ok:
            raise ControlApiError("GOLD_ADD_FAILED", wallet_note, status=409)
        current = max(0, int(wallet.get("gold", 0) or 0))
        if current > CLIENT_GOLD_DISPLAY_CAP - amount:
            raise ControlApiError(
                "CURRENCY_LIMIT_REACHED",
                "wallet_gold_display_cap",
                status=409,
            )
        ok, note, balance = repo.grant_wallet_gold(character_id, amount)
        if not ok:
            raise ControlApiError("GOLD_ADD_FAILED", note, status=409)
        body = build_wallet_gold_update_000e(int(balance))
        packets = [self.server._codec_response(0, 0x000E, body)]
        return packets, {
            "character_id": character_id,
            "amount": amount,
            "balance": int(balance),
            "packet_flow": ["S0/000E wallet"],
        }, "GM gold add %s" % note


__all__ = [
    "GM_CERA_ADD_MAX",
    "GM_GOLD_ADD_MAX",
    "GmCurrencyControlCommands",
]
