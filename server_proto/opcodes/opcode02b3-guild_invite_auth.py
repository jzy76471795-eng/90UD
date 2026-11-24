"""Observed Stage3 C1/02B3 request; its S2C state vector is not confirmed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates


GUILD_INVITE_AUTH_OPCODE = 0x02B3


@dataclass(frozen=True)
class ParsedGuildInviteAuthRequest:
    request_flag: int
    source: str


def parse_guild_invite_auth_body(
    body: bytes,
) -> Optional[ParsedGuildInviteAuthRequest]:
    """Parse the Stage3 ``u8`` request flag, allowing codec zero padding."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if not candidate or candidate[0] not in (0, 1):
            continue
        if any(candidate[1:]):
            continue
        return ParsedGuildInviteAuthRequest(
            request_flag=int(candidate[0]),
            source=source,
        )
    return None


class GuildInviteAuth02B3Service:
    """Record the observed request without inventing an S2C state snapshot."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_guild_invite_auth_02b3(self, body: bytes) -> tuple[list[bytes], str]:
        parsed = parse_guild_invite_auth_body(body)
        if parsed is None:
            return [], "C1/02B3 GUILD_INVITE_AUTH reject=invalid_body"

        # The Stage3 reader accepts mode=1/count=0 syntactically, but no
        # native S2C vector establishes that it represents this request's
        # required state.  Do not mutate the client state with a guessed
        # snapshot while that producer-side contract remains unresolved.
        return [], (
            "C1/02B3 GUILD_INVITE_AUTH request_flag=%s source=%s "
            "s2c_snapshot=withheld_unverified_vector attempt=2/3"
            % (parsed.request_flag, parsed.source)
        )
