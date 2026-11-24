"""Stage3 local guild-create authentication request (C1 0x02E5)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRepository


GUILD_CREATE_PERMIT_OPCODE = 0x02E5


@dataclass(frozen=True)
class ParsedGuildCreatePermitRequest:
    request_flag: int
    source: str


def parse_guild_create_permit_body(
    body: bytes,
) -> Optional[ParsedGuildCreatePermitRequest]:
    """Parse the observed one-byte request flag with zero codec padding."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if not candidate or candidate[0] not in (0, 1) or any(candidate[1:]):
            continue
        return ParsedGuildCreatePermitRequest(
            request_flag=int(candidate[0]),
            source=source,
        )
    return None


class GuildCreatePermit02E5Service:
    """Persist the invite and let the target's next selected role receive it."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def handle_guild_create_permit_02e5(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        parsed = parse_guild_create_permit_body(body)
        creator_id = int(getattr(session, "selected_character_id", 0) or 0)
        if parsed is None or creator_id <= 0:
            return [], "C1/02E5 GUILD_CREATE_PERMIT reject=invalid_body_or_creator"

        permit, reason = self._repository().request_create_permit(creator_id)
        if permit is None:
            return [], (
                "C1/02E5 GUILD_CREATE_PERMIT reject=%s creator=%s flag=%s source=%s"
                % (reason, creator_id, parsed.request_flag, parsed.source)
            )

        # Stage3's create screen enters its waiting state after this request.
        # The successful server-side action is the S0/02B0 notification sent
        # when the selected helper character next enters town.
        return [], (
            "C1/02E5 GUILD_CREATE_PERMIT pending permit=%s creator=%s target=%s "
            "flag=%s source=%s delivery=S0/02B0-on-target-select"
            % (
                permit.permit_id,
                permit.creator_character_id,
                permit.target_character_id,
                parsed.request_flag,
                parsed.source,
            )
        )
