"""Stage3 C1/S1 0x015E approval of one pending guild-join application."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRepository


GUILD_APPROVE_JOIN_OPCODE = 0x015E
GUILD_APPROVE_JOIN_SUCCESS = 0x01
GUILD_APPROVE_JOIN_FAILURE = 0x00
GUILD_APPROVE_JOIN_GENERIC_FAILURE = 0x9F


@dataclass(frozen=True)
class ParsedGuildApproveJoin:
    applicant_character_id: int
    source: str


def parse_guild_approve_join_body(
    body: bytes,
) -> Optional[ParsedGuildApproveJoin]:
    """Parse the observed ``u32 applicant_character_id`` C1/015E body."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 4 or any(candidate[4:]):
            continue
        applicant_character_id = int.from_bytes(candidate[:4], "little")
        if applicant_character_id <= 0:
            continue
        return ParsedGuildApproveJoin(
            applicant_character_id=applicant_character_id,
            source=source,
        )
    return None


def build_guild_approve_join_success_body(applicant_character_id: int) -> bytes:
    """Build S1/015E's success form consumed by the native list callback."""

    applicant_id = int(applicant_character_id)
    if applicant_id <= 0:
        raise ValueError("applicant character id is invalid")
    return bytes((GUILD_APPROVE_JOIN_SUCCESS,)) + applicant_id.to_bytes(
        4,
        "little",
        signed=False,
    )


def build_guild_approve_join_failure_body(applicant_character_id: int) -> bytes:
    """Build the generic S1/015E failure form read after its applicant id."""

    applicant_id = int(applicant_character_id)
    if applicant_id <= 0:
        applicant_id = 0
    return (
        bytes((GUILD_APPROVE_JOIN_FAILURE,))
        + applicant_id.to_bytes(4, "little", signed=False)
        + bytes((GUILD_APPROVE_JOIN_GENERIC_FAILURE,))
    )


class GuildApproveJoin015EService:
    """Persist one accepted application and acknowledge the native UI callback."""

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
        applicant_character_id: int,
        approver_character_id: int,
    ) -> tuple[list[bytes], str]:
        response = build_guild_approve_join_failure_body(applicant_character_id)
        packet = self.server._codec_response(1, GUILD_APPROVE_JOIN_OPCODE, response)
        return [packet], (
            "C1/015E APPROVE_GUILD_JOIN reject=%s approver=%s applicant=%s "
            "S1/015E=%s"
            % (
                reason,
                approver_character_id,
                applicant_character_id,
                response.hex(" "),
            )
        )

    def handle_guild_approve_join_015e(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        approver_id = int(getattr(session, "selected_character_id", 0) or 0)
        parsed = parse_guild_approve_join_body(body)
        if approver_id <= 0 or parsed is None:
            applicant_id = 0 if parsed is None else parsed.applicant_character_id
            return self._failure(
                "invalid_body_or_selected_character",
                applicant_id,
                approver_id,
            )

        guild, reason = self._repository().approve_join_application(
            approver_id,
            parsed.applicant_character_id,
        )
        if guild is None:
            return self._failure(reason, parsed.applicant_character_id, approver_id)
        response = build_guild_approve_join_success_body(
            parsed.applicant_character_id
        )
        packet = self.server._codec_response(1, GUILD_APPROVE_JOIN_OPCODE, response)
        return [packet], (
            "C1/015E APPROVE_GUILD_JOIN approver=%s applicant=%s guild=%s "
            "source=%s S1/015E=%s"
            % (
                approver_id,
                parsed.applicant_character_id,
                guild.guild_id,
                parsed.source,
                response.hex(" "),
            )
        )
