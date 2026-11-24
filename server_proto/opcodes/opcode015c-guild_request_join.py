"""Stage3 C1/S1 0x015C guild-join application request."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRecord, GuildRepository


GUILD_REQUEST_JOIN_OPCODE = 0x015C
GUILD_REQUEST_JOIN_SUCCESS = 0x01
GUILD_REQUEST_JOIN_FAILURE = 0x00
GUILD_REQUEST_JOIN_STRING_CAPACITY = 512


@dataclass(frozen=True)
class ParsedGuildRequestJoin:
    display_name: str
    application_message: str
    application_message_bytes: bytes
    source: str


def parse_guild_request_join_body(
    body: bytes,
) -> Optional[ParsedGuildRequestJoin]:
    """Parse the observed ``DSTR guild_name + DSTR application_message`` request."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 8:
            continue
        name_length = int.from_bytes(candidate[:4], "little", signed=False)
        if not 0 < name_length < GUILD_REQUEST_JOIN_STRING_CAPACITY:
            continue
        name_end = 4 + name_length
        if name_end + 4 > len(candidate):
            continue
        message_length = int.from_bytes(
            candidate[name_end : name_end + 4],
            "little",
            signed=False,
        )
        if message_length >= GUILD_REQUEST_JOIN_STRING_CAPACITY:
            continue
        message_start = name_end + 4
        message_end = message_start + message_length
        if message_end > len(candidate) or any(candidate[message_end:]):
            continue
        name_bytes = candidate[4:name_end]
        message_bytes = candidate[message_start:message_end]
        try:
            display_name = name_bytes.decode("utf-8")
            application_message = message_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not display_name.strip():
            continue
        return ParsedGuildRequestJoin(
            display_name=display_name,
            application_message=application_message,
            application_message_bytes=message_bytes,
            source=source,
        )
    return None


def _stream_bytes(value: bytes) -> bytes:
    raw = bytes(value)
    return len(raw).to_bytes(4, "little", signed=False) + raw


def build_guild_request_join_success_body(guild: GuildRecord) -> bytes:
    """Build the exact successful S1/015C reader prefix and trailing fields."""

    guild_id = int(guild.guild_id)
    guild_name = str(guild.display_name).encode("utf-8")
    if guild_id <= 0:
        raise ValueError("guild id is invalid")
    if not 0 < len(guild_name) < GUILD_REQUEST_JOIN_STRING_CAPACITY:
        raise ValueError("guild name is outside S1/015C reader capacity")

    return (
        bytes((GUILD_REQUEST_JOIN_SUCCESS,))
        + guild_id.to_bytes(4, "little", signed=False)
        + _stream_bytes(guild_name)
        + _stream_bytes(b"")
        + (0).to_bytes(4, "little", signed=False)
    )


class GuildRequestJoin015CService:
    """Store Stage3 guild applications before the member opens the join list."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def _failure(
        self,
        reason: str,
        requester_character_id: int,
    ) -> tuple[list[bytes], str]:
        # The failure branch takes a second status byte.  No producer-side
        # evidence maps local validation errors to a nonzero native status.
        response = bytes((GUILD_REQUEST_JOIN_FAILURE, 0))
        packet = self.server._codec_response(1, GUILD_REQUEST_JOIN_OPCODE, response)
        return [packet], (
            "C1/015C REQUEST_JOIN_GUILD reject=%s requester=%s S1/015C=%s"
            % (reason, requester_character_id, response.hex(" "))
        )

    def handle_guild_request_join_015c(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        requester_id = int(getattr(session, "selected_character_id", 0) or 0)
        parsed = parse_guild_request_join_body(body)
        if requester_id <= 0 or parsed is None:
            return self._failure("invalid_body_or_selected_character", requester_id)

        repository = self._repository()
        detail = repository.get_guild_search_detail(parsed.display_name)
        if detail is None:
            return self._failure("guild_missing", requester_id)

        guild, reason = repository.request_join_guild(
            requester_id,
            detail.guild.guild_id,
            parsed.display_name,
            parsed.application_message_bytes,
        )
        if guild is None:
            return self._failure(reason, requester_id)
        try:
            response = build_guild_request_join_success_body(guild)
        except ValueError as exc:
            return self._failure("invalid_guild:%s" % exc, requester_id)

        packet = self.server._codec_response(1, GUILD_REQUEST_JOIN_OPCODE, response)
        return [packet], (
            "C1/015C REQUEST_JOIN_GUILD requester=%s guild=%s name=%r "
            "source=%s application_message=%r message_bytes=%s S1/015C=%s"
            % (
                requester_id,
                guild.guild_id,
                guild.display_name,
                parsed.source,
                parsed.application_message,
                len(parsed.application_message_bytes),
                response.hex(" "),
            )
        )
