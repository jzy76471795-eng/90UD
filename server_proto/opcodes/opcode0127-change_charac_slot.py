"""变更角色槽位 (C->S 0x0127 / ENUM_CMDPACKET_CHANGE_CHARAC_SLOT)

Live Stage3 evidence (game_server_20260714_175933.log):
  - flag=1, opcode=0x0127, codec_index=1 (CAST-128 seed0, 8-byte align)
  - plain body = u32 from_slot + u32 to_slot (LE)

S->C 0x0127: minimal success flag byte (1=ok, 0=fail). Default follow-up:
USERINFO roster refresh so UI roster matches DB slot_index.
"""
from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


class CharChangeCharacSlotService:
    """变更角色槽位 (C->S 0x0127)"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_change_charac_slot(self, body: bytes) -> tuple[list[bytes], str]:
        if body is None or len(body) < 8:
            packet = self.server._codec_response(1, 0x0127, b"\x00")
            blen = 0 if body is None else len(body)
            return [packet], (
                "CHANGE_CHARAC_SLOT invalid body_len=%s; sent fail status=00 wire=%s"
                % (blen, len(packet))
            )

        from_slot = int.from_bytes(body[0:4], "little")
        to_slot = int.from_bytes(body[4:8], "little")
        repo = self.server._repo()
        ok, note, updated = repo.change_character_slots(
            account_id=self.server.account_id,
            from_slot=from_slot,
            to_slot=to_slot,
        )
        if not ok:
            packet = self.server._codec_response(1, 0x0127, b"\x00")
            return [packet], (
                "CHANGE_CHARAC_SLOT fail from=%s to=%s; %s; sent status=00 wire=%s"
                % (from_slot, to_slot, note, len(packet))
            )

        packets = [self.server._codec_response(1, 0x0127, b"\x01")]
        roster = ",".join(
            "%s:%s:%s" % (r.slot_index, r.character_id, r.display_name) for r in updated
        )
        refresh_note = (
            "USERINFO refresh suppressed by USDOF_CHANGE_CHARAC_SLOT_SEND_USERINFO_REFRESH=0"
        )
        if _env_bool("USDOF_CHANGE_CHARAC_SLOT_SEND_USERINFO_REFRESH", True):
            userinfo_packets, userinfo_note = self.server._db_userinfo_packet_stream(
                "change-charac-slot-refresh",
                include_followups=_env_bool(
                    "USDOF_CHANGE_CHARAC_SLOT_USERINFO_FOLLOWUPS", False
                ),
            )
            packets.extend(userinfo_packets)
            refresh_note = userinfo_note
        return packets, (
            "CHANGE_CHARAC_SLOT ok from=%s to=%s; %s; roster=[%s]; sent status=01 wire=%s + %s"
            % (from_slot, to_slot, note, roster, len(packets[0]), refresh_note)
        )
