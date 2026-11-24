"""Stage3 C1/S1 0x02E7 guild associations for the current account."""
from __future__ import annotations

from typing import Any, Optional

from game.guild.guild_repository import GuildAccountMembership, GuildRepository


GUILD_INFO_OF_MY_CHARS_OPCODE = 0x02E7
GUILD_INFO_OF_MY_CHARS_SUCCESS = 0x01
GUILD_INFO_OF_MY_CHARS_FAILURE = 0x00
GUILD_INFO_OF_MY_CHARS_STRING_CAPACITY = 256


def parse_guild_info_of_my_chars_body(body: bytes) -> bool:
    """C1/02E7 has no fields; native codec padding is zero-only."""

    return not any(bytes(body or b""))


def _stream_bytes(value: bytes) -> bytes:
    raw = bytes(value)
    return len(raw).to_bytes(4, "little", signed=False) + raw


def _required_name_stream(value: bytes, field_name: str) -> bytes:
    raw = bytes(value)
    if not 0 < len(raw) < GUILD_INFO_OF_MY_CHARS_STRING_CAPACITY:
        raise ValueError(
            "%s byte_length=%s outside Stage3 reader capacity=%s"
            % (field_name, len(raw), GUILD_INFO_OF_MY_CHARS_STRING_CAPACITY)
        )
    return _stream_bytes(raw)


def build_guild_info_of_my_chars_success_body(
    memberships: tuple[GuildAccountMembership, ...],
) -> bytes:
    """Build the complete list consumed by Stage3's S1/02E7 reader."""

    entries = tuple(memberships)
    if len(entries) > 0xFF:
        raise ValueError("guild association count exceeds u8")

    body = bytearray((GUILD_INFO_OF_MY_CHARS_SUCCESS, len(entries)))
    for entry in entries:
        guild_name = entry.guild.display_name.encode("utf-8")
        body += _required_name_stream(entry.name_bytes, "character_name")
        body += _required_name_stream(guild_name, "guild_name")
        # The list entry stores a boolean.  The durable leader identity is
        # authoritative when legacy membership grades disagree.
        body += bytes((1 if entry.is_leader else 0,))
    return bytes(body)


class GuildInfoOfMyChars02E7Service:
    """Project the current account's durable guild associations into S1/02E7."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def _failure(self, reason: str) -> tuple[list[bytes], str]:
        response = bytes((GUILD_INFO_OF_MY_CHARS_FAILURE,))
        packet = self.server._codec_response(
            1,
            GUILD_INFO_OF_MY_CHARS_OPCODE,
            response,
        )
        return [packet], (
            "C1/02E7 GUILD_INFO_OF_MY_CHARS reject=%s S1/02E7=%s"
            % (reason, response.hex(" "))
        )

    def handle_guild_info_of_my_chars_02e7(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        del session
        if not parse_guild_info_of_my_chars_body(body):
            return self._failure("invalid_body")

        account_id = int(getattr(self.server, "account_id", 0) or 0)
        memberships = self._repository().list_account_guild_memberships(account_id)
        try:
            response = build_guild_info_of_my_chars_success_body(memberships)
        except ValueError as exc:
            return self._failure("invalid_snapshot:%s" % exc)

        packet = self.server._codec_response(
            1,
            GUILD_INFO_OF_MY_CHARS_OPCODE,
            response,
        )
        entries = ",".join(
            "%s:%r:%r:leader=%s"
            % (
                entry.character_id,
                entry.name_bytes.decode("utf-8", errors="replace"),
                entry.guild.display_name,
                int(entry.is_leader),
            )
            for entry in memberships
        )
        return [packet], (
            "C1/02E7 GUILD_INFO_OF_MY_CHARS account=%s entries=%s [%s] "
            "S1/02E7=%s"
            % (account_id, len(memberships), entries or "none", response.hex(" "))
        )
