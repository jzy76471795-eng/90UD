"""C1/0233 Stage3 fatigue-burning activity state change."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain


_activity = load_chain("helper-activity_state.py")


def _selected_character_id(session: Any) -> int:
    if session is None:
        return 0
    for attr in ("selected_character_id", "character_id", "cid"):
        try:
            character_id = int(getattr(session, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return 0


class FatigueAccelerationStateChange0233Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _command_result(self, success: bool) -> bytes:
        return self.server._codec_response(
            1,
            _activity.FATIGUE_BURNING_COMMAND_OPCODE,
            b"\x01" if success else b"\x00",
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        raw = bytes(body or b"")
        character_id = _selected_character_id(session)
        if len(raw) != 1 or raw[0] not in (0, 1):
            return [self._command_result(False)], (
                "C1/0233 FATIGUE_BURNING reject malformed len=%d hex=%s"
                % (len(raw), raw[:8].hex(" "))
            )
        if character_id <= 0:
            return [self._command_result(False)], (
                "C1/0233 FATIGUE_BURNING reject selected_character_missing"
            )

        # The client sends its current state: 0 requests ON, 1 requests OFF.
        enabled = raw[0] == 0
        saved = self.server._repo().set_character_activity_state(
            character_id,
            _activity.FATIGUE_BURNING_ACTIVITY_KEY,
            enabled,
        )
        if not saved:
            return [self._command_result(False)], (
                "C1/0233 FATIGUE_BURNING save_failed cid=%d requested=%d"
                % (character_id, int(enabled))
            )

        _activity.cache_fatigue_burning_state(
            session,
            character_id=character_id,
            enabled=enabled,
        )
        state_body = _activity.build_fatigue_burning_state_body(enabled)
        return [
            self._command_result(True),
            self.server._codec_response(
                0,
                _activity.FATIGUE_BURNING_STATE_OPCODE,
                state_body,
            ),
        ], (
            "C1/0233 FATIGUE_BURNING OK cid=%d state=%d; "
            "S1/0233 success=1; S0/01EB value=0 state=%d"
            % (character_id, int(enabled), int(enabled))
        )


__all__ = ["FatigueAccelerationStateChange0233Service"]
