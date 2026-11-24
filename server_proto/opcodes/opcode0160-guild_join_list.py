"""Stage3 C1/S1 0x0160 pending guild-join application list."""
from __future__ import annotations

from typing import Any, Optional

from game.guild.guild_repository import GuildJoinApplicationList, GuildRepository


GUILD_JOIN_LIST_OPCODE = 0x0160
GUILD_JOIN_LIST_SUCCESS = 0x01
GUILD_JOIN_LIST_FAILURE = 0x00
GUILD_JOIN_LIST_STRING_CAPACITY = 512


def parse_guild_join_list_body(body: bytes) -> bool:
    """C1/0160 is empty; native codec padding is zero-only."""

    return not any(bytes(body or b""))


def _stream_bytes(value: bytes) -> bytes:
    raw = bytes(value)
    return len(raw).to_bytes(4, "little", signed=False) + raw


def _required_name_stream(value: bytes) -> bytes:
    raw = bytes(value)
    if not 0 < len(raw) < GUILD_JOIN_LIST_STRING_CAPACITY:
        raise ValueError("applicant name is outside S1/0160 reader capacity")
    return _stream_bytes(raw)


def _application_message_stream(value: bytes) -> bytes:
    raw = bytes(value)
    if len(raw) >= GUILD_JOIN_LIST_STRING_CAPACITY:
        raise ValueError("application message is outside S1/0160 reader capacity")
    return _stream_bytes(raw)


def build_guild_join_list_success_body(
    application_list: GuildJoinApplicationList,
) -> bytes:
    """Build the S1/0160 row order consumed by Stage3's join-list reader."""

    applications = tuple(application_list.applications)
    if len(applications) > 0xFFFFFFFF:
        raise ValueError("guild join application count exceeds u32")

    body = bytearray((GUILD_JOIN_LIST_SUCCESS,))
    body += len(applications).to_bytes(4, "little", signed=False)
    for application in applications:
        character_id = int(application.character_id)
        if character_id <= 0:
            raise ValueError("applicant character id is invalid")
        body += character_id.to_bytes(4, "little", signed=False)
        body += _required_name_stream(application.name_bytes)
        # Stage3 stores the next byte directly, splits the following byte into
        # low grow/high sub-grow fields, then consumes two more u8 values.
        body += bytes((int(application.job) & 0xFF,))
        body += bytes((int(application.grow_type) & 0x7F,))
        body += b"\x00\x00"
        body += _application_message_stream(application.application_message_bytes)
        body += (0).to_bytes(4, "little", signed=False)
        body += b"\x00"
    return bytes(body)


class GuildJoinList0160Service:
    """Project pending local applications for the selected guild member."""

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
        selected_character_id: int,
    ) -> tuple[list[bytes], str]:
        response = bytes((GUILD_JOIN_LIST_FAILURE,))
        packet = self.server._codec_response(1, GUILD_JOIN_LIST_OPCODE, response)
        return [packet], (
            "C1/0160 GUILD_JOIN_LIST reject=%s selected_character=%s S1/0160=%s"
            % (reason, selected_character_id, response.hex(" "))
        )

    def handle_guild_join_list_0160(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        selected_id = int(getattr(session, "selected_character_id", 0) or 0)
        if selected_id <= 0 or not parse_guild_join_list_body(body):
            return self._failure("invalid_body_or_selected_character", selected_id)

        application_list = self._repository().get_join_application_list_for_character(
            selected_id
        )
        if application_list is None:
            return self._failure("guild_missing", selected_id)
        try:
            response = build_guild_join_list_success_body(application_list)
        except ValueError as exc:
            return self._failure("invalid_snapshot:%s" % exc, selected_id)

        packet = self.server._codec_response(1, GUILD_JOIN_LIST_OPCODE, response)
        applicant_ids = ",".join(
            str(application.character_id)
            for application in application_list.applications
        )
        return [packet], (
            "C1/0160 GUILD_JOIN_LIST selected_character=%s guild=%s "
            "applications=%s applicant_ids=%s S1/0160=%s"
            % (
                selected_id,
                application_list.guild.guild_id,
                len(application_list.applications),
                applicant_ids or "-",
                response.hex(" "),
            )
        )
