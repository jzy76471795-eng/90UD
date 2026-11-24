"""C1/0218 Stage3 cube-contract selection update."""
from __future__ import annotations

import time
from typing import Any

from game.shop.cube_contract import (
    CUBE_CONTRACT_UPDATE_OPCODE,
    parse_cube_contract_update,
)
from game.shop.premium_contract_index import PREMIUM_TYPE_CUBE


def build_cube_contract_update_success() -> bytes:
    """The Stage3 S1/0218 success branch consumes only the common marker."""
    return b"\x01"


def build_cube_contract_update_failure(error_code: int = 4) -> bytes:
    """Failure marker plus the two bytes consumed by ``sub_17596F0``."""
    return bytes((0, int(error_code) & 0xFF, 0))


def _selected_character_id(session: Any) -> int | None:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        try:
            character_id = int(getattr(session, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return None


class UpdateCubeContract0218Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, body: bytes) -> bytes:
        return self.server._codec_response(1, CUBE_CONTRACT_UPDATE_OPCODE, body)

    def _failure(self, note: str) -> tuple[list[bytes], str]:
        return [self._emit(build_cube_contract_update_failure())], (
            "C1/0218 CUBE_CONTRACT FAIL %s" % note
        )

    def handle_update_cube_contract_0218(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_cube_contract_update(body)
        if request is None:
            return self._failure(
                "malformed plain_len=%d hex=%s"
                % (len(bytes(body or b"")), bytes(body or b"")[:8].hex(" "))
            )

        character_id = _selected_character_id(session)
        if character_id is None:
            return self._failure("selected_character_missing")

        now_unix = int(time.time())
        ok, save_note, detail = self.server._repo().save_cube_contract_selection(
            character_id,
            request.selection,
            premium_type=PREMIUM_TYPE_CUBE,
            now_unix=now_unix,
        )
        if not ok:
            return self._failure(
                "%s cid=%d selection=%02x"
                % (save_note, character_id, request.selection)
            )

        packet = self._emit(build_cube_contract_update_success())
        return [packet], (
            "C1/0218 CUBE_CONTRACT OK cid=%d account=%d mode=%d selection=%02x "
            "remaining=%d; S1/0218 success=1"
            % (
                character_id,
                int(detail.get("account_id", 0) or 0),
                request.mode,
                request.selection,
                int(detail.get("remaining_seconds", 0) or 0),
            )
        )


__all__ = [
    "UpdateCubeContract0218Service",
    "build_cube_contract_update_failure",
    "build_cube_contract_update_success",
]
