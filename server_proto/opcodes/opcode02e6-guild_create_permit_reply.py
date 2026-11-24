"""Stage3 local guild-create authentication acceptance (C1 0x02E6)."""
from __future__ import annotations

from typing import Any, Optional

from game.guild.guild_repository import GuildRepository


GUILD_CREATE_PERMIT_REPLY_OPCODE = 0x02E6
GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE = 0x0047


def parse_guild_create_permit_reply_body(body: bytes) -> bool:
    """C1/02E6 is empty; native codec padding is zero-only."""

    return not any(bytes(body or b""))


class GuildCreatePermitReply02E6Service:
    """Accept the helper character's invite and complete the local guild."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def handle_guild_create_permit_reply_02e6(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        target_id = int(getattr(session, "selected_character_id", 0) or 0)
        if target_id <= 0 or not parse_guild_create_permit_reply_body(body):
            return [], "C1/02E6 GUILD_CREATE_PERMIT_REPLY reject=invalid_body_or_target"

        guild, permit, reason = self._repository().accept_create_permit_and_create_guild(
            target_id
        )
        if guild is None or permit is None:
            return [], (
                "C1/02E6 GUILD_CREATE_PERMIT_REPLY reject=%s target=%s"
                % (reason, target_id)
            )

        packet = self.server._codec_response(
            0,
            GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE,
            int(guild.guild_id).to_bytes(4, "little", signed=False),
        )
        self._repository().mark_create_notice_delivered(permit.permit_id, target_id)
        return [packet], (
            "C1/02E6 GUILD_CREATE_PERMIT_REPLY accepted permit=%s target=%s "
            "guild=%s name=%r members=%s,%s S0/0047=u32:%s"
            % (
                permit.permit_id,
                target_id,
                guild.guild_id,
                guild.display_name,
                permit.creator_character_id,
                permit.target_character_id,
                guild.guild_id,
            )
        )
