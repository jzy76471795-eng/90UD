"""Stage3 guild-create promotion-text check (C1/S1 0x02E2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates


CHECK_GUILD_CREATE_PROMOTE_OPCODE = 0x02E2
GUILD_CREATE_PROMOTE_AVAILABLE = 0x00
GUILD_CREATE_PROMOTE_ILLEGAL = 0x6C


@dataclass(frozen=True)
class ParsedGuildCreatePromoteCheck:
    text: str
    text_bytes: bytes
    source: str


def parse_guild_create_promote_check_body(
    body: bytes,
) -> Optional[ParsedGuildCreatePromoteCheck]:
    """Parse the observed ``u32 byte_len + UTF-8 text`` request body."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 4:
            continue
        text_len = int.from_bytes(candidate[:4], "little", signed=False)
        if text_len <= 0:
            continue
        text_end = 4 + text_len
        if text_end > len(candidate):
            continue
        # Native codec buffers can retain zero padding after the logical
        # request. Non-zero data is not part of this command.
        if any(candidate[text_end:]):
            continue
        text_raw = candidate[4:text_end]
        try:
            text = text_raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return ParsedGuildCreatePromoteCheck(
            text=text,
            text_bytes=text_raw,
            source=source,
        )
    return None


class GuildCreatePromoteCheck02E2Service:
    """Pass the guild-create invite-auth text check in the local flow."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def _respond(self, passed: bool, status: int) -> tuple[list[bytes], bytes]:
        response = bytes((1 if passed else 0, int(status) & 0xFF))
        packet = self.server._codec_response(
            1,
            CHECK_GUILD_CREATE_PROMOTE_OPCODE,
            response,
        )
        return [packet], response

    def handle_check_guild_create_promote_02e2(
        self,
        body: bytes,
    ) -> tuple[list[bytes], str]:
        parsed = parse_guild_create_promote_check_body(body)
        if parsed is None:
            packets, response = self._respond(False, GUILD_CREATE_PROMOTE_ILLEGAL)
            return packets, (
                "C1/02E2 GUILD_CREATE_PROMOTE_CHECK reject=invalid_body "
                "S1/02E2=%s" % response.hex(" ")
            )

        packets, response = self._respond(True, GUILD_CREATE_PROMOTE_AVAILABLE)
        return packets, (
            "C1/02E2 GUILD_CREATE_PROMOTE_CHECK default_pass text=%r source=%s "
            "S1/02E2=%s"
            % (parsed.text, parsed.source, response.hex(" "))
        )
