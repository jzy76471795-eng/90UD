"""Stage3 C1/S1 0x02E8 local guild-summary search."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRepository, GuildSearchDetail


GUILD_SEARCH_FOR_JOIN_OPCODE = 0x02E8
GUILD_SEARCH_FOR_JOIN_SUCCESS = 0x01
GUILD_SEARCH_FOR_JOIN_FAILURE = 0x00
GUILD_SEARCH_FOR_JOIN_STRING_CAPACITY = 256
# Stage3 reads this field as a u16.  Local guilds have no progression model,
# so every newly created guild projects the initial level.
GUILD_SEARCH_LOCAL_LEVEL = 1


@dataclass(frozen=True)
class ParsedGuildSearchForJoin:
    display_name: str
    name_bytes: bytes
    source: str


def parse_guild_search_for_join_body(
    body: bytes,
) -> Optional[ParsedGuildSearchForJoin]:
    """Parse the observed ``u32 UTF-8 byte_len + guild name`` C1 body."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 4:
            continue
        name_length = int.from_bytes(candidate[:4], "little", signed=False)
        if not 0 < name_length < GUILD_SEARCH_FOR_JOIN_STRING_CAPACITY:
            continue
        name_end = 4 + name_length
        if name_end > len(candidate) or any(candidate[name_end:]):
            continue
        name_bytes = candidate[4:name_end]
        try:
            display_name = name_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if display_name.strip():
            return ParsedGuildSearchForJoin(
                display_name=display_name,
                name_bytes=name_bytes,
                source=source,
            )
    return None


def _stream_bytes(value: bytes) -> bytes:
    raw = bytes(value)
    return len(raw).to_bytes(4, "little", signed=False) + raw


def _bounded_stream(value: bytes, field_name: str, *, allow_empty: bool = False) -> bytes:
    raw = bytes(value)
    if len(raw) >= GUILD_SEARCH_FOR_JOIN_STRING_CAPACITY or (
        not allow_empty and not raw
    ):
        raise ValueError(
            "%s byte_length=%s outside Stage3 reader capacity=%s"
            % (field_name, len(raw), GUILD_SEARCH_FOR_JOIN_STRING_CAPACITY)
        )
    return _stream_bytes(raw)


def build_guild_search_for_join_success_body(detail: GuildSearchDetail) -> bytes:
    """Build the exact fixed-prefix and zero-length optional table for S1/02E8."""

    guild_id = int(detail.guild.guild_id)
    if guild_id <= 0:
        raise ValueError("guild id is invalid")
    member_count = int(detail.member_count)
    if member_count < 0 or member_count > 0xFFFFFFFF:
        raise ValueError("member count exceeds u32")
    guild_name = detail.guild.display_name.encode("utf-8")
    leader_name = bytes(detail.leader_name_bytes)

    body = bytearray((GUILD_SEARCH_FOR_JOIN_SUCCESS,))
    body += guild_id.to_bytes(4, "little", signed=False)
    body += _bounded_stream(guild_name, "guild_name")
    body += GUILD_SEARCH_LOCAL_LEVEL.to_bytes(2, "little", signed=False)
    body += _bounded_stream(leader_name, "leader_name", allow_empty=True)
    body += member_count.to_bytes(4, "little", signed=False)
    # The Stage3 reader owns the final u8 vector.  The local guild model has
    # no optional per-grade search metadata, so it is deliberately empty.
    body += b"\x00"
    return bytes(body)


class GuildSearchForJoin02E8Service:
    """Return one durable local guild summary to the Stage3 search dialog."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def _failure(self, reason: str, *, requested_name: str = "") -> tuple[list[bytes], str]:
        response = bytes((GUILD_SEARCH_FOR_JOIN_FAILURE,))
        packet = self.server._codec_response(
            1,
            GUILD_SEARCH_FOR_JOIN_OPCODE,
            response,
        )
        return [packet], (
            "C1/02E8 GUILD_SEARCH_FOR_JOIN reject=%s name=%r S1/02E8=%s"
            % (reason, requested_name, response.hex(" "))
        )

    def handle_guild_search_for_join_02e8(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        del session
        parsed = parse_guild_search_for_join_body(body)
        if parsed is None:
            return self._failure("invalid_body")

        detail = self._repository().get_guild_search_detail(parsed.display_name)
        if detail is None:
            return self._failure("guild_missing", requested_name=parsed.display_name)
        try:
            response = build_guild_search_for_join_success_body(detail)
        except ValueError as exc:
            return self._failure(
                "invalid_snapshot:%s" % exc,
                requested_name=parsed.display_name,
            )

        packet = self.server._codec_response(
            1,
            GUILD_SEARCH_FOR_JOIN_OPCODE,
            response,
        )
        return [packet], (
            "C1/02E8 GUILD_SEARCH_FOR_JOIN name=%r source=%s guild=%s "
            "leader=%r members=%s S1/02E8=%s"
            % (
                parsed.display_name,
                parsed.source,
                detail.guild.guild_id,
                detail.leader_name_bytes.decode("utf-8", errors="replace"),
                detail.member_count,
                response.hex(" "),
            )
        )
