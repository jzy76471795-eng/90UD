"""C1/0x0028 character death report and Stage3 revive-state arm.

Stage3 live logs show the client sends C1/0028 when the local character dies.
Its six-byte report is retained as observational input; the response does not
depend on its unresolved fields.  The frozen Stage3 S0/0020 reader at
``sub_163A1A0`` reads ``u16 actor_id``, ``u8 state`` and one additional u8.
The extra byte's semantic name is not established; send its existing zero
explicitly rather than relying on cipher padding. State 0
opens the death/revive state and state 1 is sent after the wallet spend.
"""
from __future__ import annotations

import struct
from typing import Any


class DieCharacter0028Service:
    """Arm a single revive action after the local dungeon character dies."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_die_character_0028(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        raw = bytes(body or b"")
        if session is None or not bool(getattr(session, "in_dungeon_run", False)):
            return [], (
                "C1/0028 DIE_CHARACTER rejected=in_dungeon_run_false "
                "plain_len=%d raw=%s" % (len(raw), raw.hex(" "))
            )

        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            return [], (
                "C1/0028 DIE_CHARACTER rejected=selected_character_missing "
                "plain_len=%d raw=%s" % (len(raw), raw.hex(" "))
            )

        actor_id = character_id & 0xFFFF
        session.dungeon_death_pending = True
        session.dungeon_death_actor_id = actor_id
        state_body = struct.pack("<HBB", actor_id, 0, 0)
        packet = self.server._codec_response(0, 0x0020, state_body)
        return [packet], (
            "C1/0028 DIE_CHARACTER plain_len=%d raw=%s actor=%d "
            "armed=1 reply=S0/0020 state=0 len=%d "
            "native=sub_163A1A0_u16_actor_u8_state_u8_aux"
            % (len(raw), raw.hex(" "), actor_id, len(state_body))
        )
