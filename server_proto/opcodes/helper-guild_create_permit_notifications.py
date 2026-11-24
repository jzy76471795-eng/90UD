"""Durable S0 delivery for the local guild-create authorization flow."""
from __future__ import annotations

from typing import Any, Optional

from game.guild.guild_repository import GuildRepository


GUILD_CREATE_PERMIT_REQUEST_NOTIFICATION_OPCODE = 0x02B0
GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE = 0x0047


class GuildCreatePermitNotificationService:
    """Deliver a selected character's outstanding local guild-create events."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def build_for_selected_character(self, session: Any) -> tuple[list[bytes], str]:
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            return [], "guild-create-notifications=skip-no-selected-character"

        repository = self._repository()
        packets: list[bytes] = []
        has_invite = repository.has_pending_create_permit_for_target(character_id)
        if has_invite:
            packets.append(
                self.server._codec_response(
                    0,
                    GUILD_CREATE_PERMIT_REQUEST_NOTIFICATION_OPCODE,
                    b"",
                )
            )

        notices = repository.pending_create_notices(character_id)
        for notice in notices:
            packets.append(
                self.server._codec_response(
                    0,
                    GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE,
                    int(notice.guild_id).to_bytes(4, "little", signed=False),
                )
            )
            repository.mark_create_notice_delivered(notice.permit_id, character_id)

        if not packets:
            return [], "guild-create-notifications=none character=%s" % character_id
        return packets, (
            "guild-create-notifications character=%s invite=%s created=%s"
            % (character_id, int(has_invite), len(notices))
        )
