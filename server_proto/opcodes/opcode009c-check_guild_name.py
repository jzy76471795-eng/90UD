"""Stage3 guild-name availability check (C->S/S->C 0x009C)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRepository


CHECK_GUILD_NAME_OPCODE = 0x009C
MAX_GUILD_NAME_UTF16_UNITS = 14
MAX_GUILD_NAME_UTF8_BYTES = MAX_GUILD_NAME_UTF16_UNITS * 4

GUILD_NAME_AVAILABLE = 0x00
GUILD_NAME_TOO_LONG = 0x6A
GUILD_NAME_TAKEN = 0x6B
GUILD_NAME_ILLEGAL = 0x6C


@dataclass(frozen=True)
class ParsedGuildNameCheck:
    name: str
    name_bytes: bytes
    source: str


def utf16_code_unit_count(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse_check_guild_name_body(body: bytes) -> Optional[ParsedGuildNameCheck]:
    """Parse the Stage3 ``u32 byte_len + UTF-8 name`` request body."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 4:
            continue
        name_len = int.from_bytes(candidate[:4], "little", signed=False)
        if name_len <= 0 or name_len > MAX_GUILD_NAME_UTF8_BYTES:
            continue
        name_end = 4 + name_len
        if name_end > len(candidate):
            continue
        # Native codec buffers can retain only zero padding after the logical
        # request. Non-zero trailing data is not part of this command.
        if any(candidate[name_end:]):
            continue
        name_raw = candidate[4:name_end]
        try:
            name = name_raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return ParsedGuildNameCheck(
            name=name,
            name_bytes=name_raw,
            source=source,
        )
    return None


def validate_guild_name(name: str) -> int:
    """Return the Stage3 reader status for a parsed guild name."""

    if utf16_code_unit_count(name) > MAX_GUILD_NAME_UTF16_UNITS:
        return GUILD_NAME_TOO_LONG
    if not name.strip():
        return GUILD_NAME_ILLEGAL
    if any(not char.isprintable() or char in "\r\n\t" for char in name):
        return GUILD_NAME_ILLEGAL
    return GUILD_NAME_AVAILABLE


class GuildNameCheck009CService:
    """Handle the one guild-create-screen name availability request."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            # Initialize the main repository first, so a fresh DB can still
            # take the normal reference bootstrap path before this table exists.
            character_repository = self.server._repo()
            self._guild_repository = GuildRepository(character_repository.db_path)
        return self._guild_repository

    def _respond(self, success: bool, status: int) -> tuple[list[bytes], bytes]:
        body = bytes((1 if success else 0, int(status) & 0xFF))
        return [self.server._codec_response(1, CHECK_GUILD_NAME_OPCODE, body)], body

    def handle_check_guild_name_009c(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_check_guild_name_body(body)
        if parsed is None:
            packets, response = self._respond(False, GUILD_NAME_ILLEGAL)
            return packets, (
                "C1/009C CHECK_GUILD_NAME reject=invalid_body "
                "S1/009C=%s" % response.hex(" ")
            )

        status = validate_guild_name(parsed.name)
        if status != GUILD_NAME_AVAILABLE:
            packets, response = self._respond(False, status)
            return packets, (
                "C1/009C CHECK_GUILD_NAME reject=status=0x%02X name=%r source=%s "
                "S1/009C=%s"
                % (status, parsed.name, parsed.source, response.hex(" "))
            )

        if self._repository().is_name_taken(parsed.name):
            packets, response = self._respond(False, GUILD_NAME_TAKEN)
            return packets, (
                "C1/009C CHECK_GUILD_NAME duplicate name=%r source=%s S1/009C=%s"
                % (parsed.name, parsed.source, response.hex(" "))
            )

        creator_id = int(getattr(session, "selected_character_id", 0) or 0)
        if creator_id > 0:
            if not self._repository().save_create_draft_name(creator_id, parsed.name):
                packets, response = self._respond(False, GUILD_NAME_ILLEGAL)
                return packets, (
                    "C1/009C CHECK_GUILD_NAME reject=creator_missing creator=%s "
                    "name=%r source=%s S1/009C=%s"
                    % (creator_id, parsed.name, parsed.source, response.hex(" "))
                )

        packets, response = self._respond(True, GUILD_NAME_AVAILABLE)
        return packets, (
            "C1/009C CHECK_GUILD_NAME available name=%r source=%s S1/009C=%s"
            % (parsed.name, parsed.source, response.hex(" "))
        )
