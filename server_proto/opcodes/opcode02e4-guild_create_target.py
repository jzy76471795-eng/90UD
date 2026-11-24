"""Stage3 guild-create authentication target selection (C1/S1 0x02E4)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from game.chain_loader import load_chain
from game.character.character_db import iter_plain_body_candidates
from game.guild.guild_repository import GuildRepository


GUILD_CREATE_TARGET_OPCODE = 0x02E4
GUILD_CREATE_TARGET_SUCCESS = 0x00
GUILD_CREATE_TARGET_INVALID = 0x03
GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE = 0x0047
MAX_TARGET_NAME_UTF8_BYTES = 56


@dataclass(frozen=True)
class ParsedGuildCreateTarget:
    name: str
    source: str


def parse_guild_create_target_body(body: bytes) -> Optional[ParsedGuildCreateTarget]:
    """Parse the observed ``u32 byte_len + UTF-8 character name`` body."""

    for source, candidate in iter_plain_body_candidates(bytes(body or b"")):
        if len(candidate) < 4:
            continue
        name_len = int.from_bytes(candidate[:4], "little", signed=False)
        if name_len <= 0 or name_len > MAX_TARGET_NAME_UTF8_BYTES:
            continue
        name_end = 4 + name_len
        if name_end > len(candidate) or any(candidate[name_end:]):
            continue
        try:
            name = candidate[4:name_end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if name.strip():
            return ParsedGuildCreateTarget(name=name, source=source)
    return None


class GuildCreateTarget02E4Service:
    """Bind the local helper character selected on the guild-create screen."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._guild_repository: Optional[GuildRepository] = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def _respond(self, passed: bool, status: int) -> tuple[list[bytes], bytes]:
        response = bytes((1 if passed else 0, int(status) & 0xFF))
        return [self.server._codec_response(1, GUILD_CREATE_TARGET_OPCODE, response)], response

    def _character_record(self, character_id: int) -> Any:
        """Find the selected creator without adding a second character DB API."""

        cid = int(character_id)
        if cid <= 0:
            return None
        try:
            records = self.server._repo().list_all_alive()
        except Exception:
            return None
        for record in records:
            if int(getattr(record, "character_id", 0) or 0) == cid:
                return record
        return None

    def handle_guild_create_target_02e4(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        parsed = parse_guild_create_target_body(body)
        creator_id = int(getattr(session, "selected_character_id", 0) or 0)
        if parsed is None or creator_id <= 0:
            packets, response = self._respond(False, GUILD_CREATE_TARGET_INVALID)
            return packets, (
                "C1/02E4 GUILD_CREATE_TARGET reject=invalid_body_or_creator "
                "S1/02E4=%s" % response.hex(" ")
            )

        target = self.server._repo().get_by_name(parsed.name)
        if target is None:
            packets, response = self._respond(False, GUILD_CREATE_TARGET_INVALID)
            return packets, (
                "C1/02E4 GUILD_CREATE_TARGET reject=target_missing name=%r source=%s "
                "S1/02E4=%s" % (parsed.name, parsed.source, response.hex(" "))
            )

        reason = self._repository().set_create_target(
            creator_id,
            int(target.character_id),
        )
        if reason != "ok":
            packets, response = self._respond(False, GUILD_CREATE_TARGET_INVALID)
            return packets, (
                "C1/02E4 GUILD_CREATE_TARGET reject=%s creator=%s target=%s "
                "S1/02E4=%s"
                % (reason, creator_id, target.character_id, response.hex(" "))
            )

        # The frozen local client tears down the creator's wait state when a
        # player switches to the helper. Complete the helper's approval here
        # instead of sending S0/02B0 to a role that cannot be selected safely.
        permit, permit_reason = self._repository().request_create_permit(creator_id)
        if permit is None:
            packets, response = self._respond(False, GUILD_CREATE_TARGET_INVALID)
            return packets, (
                "C1/02E4 GUILD_CREATE_TARGET reject=%s creator=%s target=%s "
                "S1/02E4=%s"
                % (permit_reason, creator_id, target.character_id, response.hex(" "))
            )

        guild, created_permit, create_reason = (
            self._repository().accept_create_permit_and_create_guild(
                int(target.character_id)
            )
        )
        if guild is None or created_permit is None:
            self._repository().cancel_create_permit(permit.permit_id)
            packets, response = self._respond(False, GUILD_CREATE_TARGET_INVALID)
            return packets, (
                "C1/02E4 GUILD_CREATE_TARGET auto_accept_reject=%s creator=%s "
                "target=%s S1/02E4=%s"
                % (
                    create_reason,
                    creator_id,
                    target.character_id,
                    response.hex(" "),
                )
            )

        packets, response = self._respond(True, GUILD_CREATE_TARGET_SUCCESS)
        refresh_packets: list[bytes] = []
        refresh_note = "USERINFO0_skip=no_creator_record"
        creator = self._character_record(creator_id)
        if creator is not None:
            try:
                userinfo_helper = load_chain("helper-userinfo_equipment.py")
                refresh_packets, refresh_note = (
                    userinfo_helper.build_userinfo0_appearance_packets(
                        self.server,
                        character_id=creator_id,
                        record=creator,
                        repo=self.server._repo(),
                        session=session,
                    )
                )
            except Exception as refresh_exc:
                refresh_note = "USERINFO0_fail:%s" % type(refresh_exc).__name__
        packets.extend(refresh_packets)
        packets.append(
            self.server._codec_response(
                0,
                GUILD_CREATE_COMPLETE_NOTIFICATION_OPCODE,
                int(guild.guild_id).to_bytes(4, "little", signed=False),
            )
        )
        self._repository().mark_create_notice_delivered(
            created_permit.permit_id,
            creator_id,
        )
        return packets, (
            "C1/02E4 GUILD_CREATE_TARGET auto_accepted permit=%s creator=%s "
            "target=%s guild=%s name=%r source=%s %s S0/0047=u32:%s S1/02E4=%s"
            % (
                created_permit.permit_id,
                creator_id,
                target.character_id,
                guild.guild_id,
                parsed.name,
                parsed.source,
                refresh_note,
                guild.guild_id,
                response.hex(" "),
            )
        )
