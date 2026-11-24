"""Stage3 guild member-list snapshot (C1/S1 0x0043)."""
from __future__ import annotations

from typing import Any, Optional

from game.guild.guild_repository import GuildMemberList, GuildRepository


GUILD_MEMBER_LIST_OPCODE = 0x0043
GUILD_ALL_MEMBER_LIST_OPCODE = 0x008C
GUILD_INFO_OPCODE = 0x0046
GUILD_MEMBER_INFO_OPCODE = 0x007F
GUILD_MEMBER_LIST_SUCCESS = 0x01
GUILD_MEMBER_LIST_FAILURE = 0x00
GUILD_MEMBER_LIST_STRING_CAPACITY = 31
GUILD_MEMBER_LIST_HEADER = 0
GUILD_ALL_MEMBER_LIST_TRAILER = 0
GUILD_INFO_SERVER_KEY = 1
GUILD_INFO_DEFAULT_CHANNEL_ID = 1
GUILD_INFO_BODY_LENGTH = 241
GUILD_GRADE_TABLE_OFFSET = 23
GUILD_GRADE_RECORD_SIZE = 28
GUILD_GRADE_NAME_CAPACITY = 24
GUILD_MEMBER_OFFLINE = 0xFF
GUILD_PERMISSION_PURCHASE_CONTENT = 1 << 8
GUILD_PERMISSION_ALL = 0x0001FFFE

# Stage3 owns six grade slots. Slot zero is the invalid/empty fallback and the
# five formal grades are addressed directly by each member row's eighth u8.
GUILD_GRADE_ROWS = (
    (0, b""),
    (GUILD_PERMISSION_ALL, b"Guild Master"),
    (GUILD_PERMISSION_ALL, b"Vice Master"),
    (GUILD_PERMISSION_PURCHASE_CONTENT, b"Excellent Member"),
    (GUILD_PERMISSION_PURCHASE_CONTENT, b"Normal Member"),
    (GUILD_PERMISSION_PURCHASE_CONTENT, b"New Member"),
)


def parse_guild_member_list_body(body: bytes) -> bool:
    """C1/0043 is empty; native codec padding is zero-only."""

    return not any(bytes(body or b""))


def _stream_bytes(value: bytes) -> bytes:
    raw = bytes(value)
    return len(raw).to_bytes(4, "little", signed=False) + raw


def _required_name_stream(value: bytes, field_name: str) -> bytes:
    raw = bytes(value)
    if not 0 < len(raw) < GUILD_MEMBER_LIST_STRING_CAPACITY:
        raise ValueError(
            "%s byte_length=%s outside Stage3 reader capacity=%s"
            % (field_name, len(raw), GUILD_MEMBER_LIST_STRING_CAPACITY)
        )
    return _stream_bytes(raw)


def _fixed_c_string(value: bytes, capacity: int, field_name: str) -> bytes:
    raw = bytes(value)
    if b"\x00" in raw or len(raw) >= int(capacity):
        raise ValueError(
            "%s byte_length=%s outside fixed C-string capacity=%s"
            % (field_name, len(raw), capacity)
        )
    return raw + bytes(int(capacity) - len(raw))


def stage3_guild_grade_for_member_role(member_role: int) -> int:
    """Map the local leader/member roles to Stage3's formal grade indexes."""

    return 1 if int(member_role) == 0 else 5


def stage3_guild_member_roster_key(character_id: int) -> int:
    """Project one durable character into Stage3's member-row grouping key."""

    roster_key = int(character_id)
    if not 0 < roster_key <= 0xFFFFFFFF:
        raise ValueError("guild member roster key is outside u32")
    return roster_key


def current_member_guild_grade(
    selected_character_id: int,
    member_list: GuildMemberList,
) -> int:
    """Resolve the selected character's grade for S0/007F."""

    selected_id = int(selected_character_id)
    for member in member_list.members:
        if int(member.character_id) == selected_id:
            return stage3_guild_grade_for_member_role(member.member_role)
    raise ValueError("selected character is not a live guild member")


def _session_channel_id(session: Any) -> int:
    for field_name in ("selected_channel_id", "channel_id"):
        value = getattr(session, field_name, None)
        if value is None:
            continue
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < channel_id < GUILD_MEMBER_OFFLINE:
            return channel_id
    return GUILD_INFO_DEFAULT_CHANNEL_ID


def build_guild_info_body(
    *,
    channel_id: int,
    server_key: int = GUILD_INFO_SERVER_KEY,
) -> bytes:
    """Build the complete Stage3 S0/0046 guild-state initialization body."""

    current_server_key = int(server_key)
    current_channel_id = int(channel_id)
    if not 0 < current_server_key <= 0xFFFFFFFF:
        raise ValueError("guild server key is outside u32")
    if not 0 < current_channel_id <= 0xFFFFFFFF:
        raise ValueError("guild channel id is outside u32")

    body = bytearray()
    body += bytes((0, 0))
    body += (0).to_bytes(2, "little", signed=False)
    body += bytes(5)
    body += (0).to_bytes(4, "little", signed=False)
    body += _stream_bytes(b"")
    body += (0).to_bytes(2, "little", signed=False)
    body += _stream_bytes(b"")

    for grade_index, (permission_mask, grade_name) in enumerate(GUILD_GRADE_ROWS):
        body += int(permission_mask).to_bytes(4, "little", signed=False)
        body += _fixed_c_string(
            grade_name,
            GUILD_GRADE_NAME_CAPACITY,
            "guild_grade_name_%s" % grade_index,
        )

    body += bytes((0,))
    body += (0).to_bytes(4, "little", signed=False)
    body += bytes((0,))
    body += (0).to_bytes(4, "little", signed=False)
    body += bytes((0,))
    body += (0).to_bytes(4, "little", signed=False)
    body += bytes((0,))
    body += bytes(4 * 4)
    body += current_server_key.to_bytes(4, "little", signed=False)
    body += current_channel_id.to_bytes(4, "little", signed=False)
    body += bytes(2 * 4)
    body += (0).to_bytes(2, "little", signed=False)

    if len(body) != GUILD_INFO_BODY_LENGTH:
        raise AssertionError("unexpected Stage3 guild-info body length")
    return bytes(body)


def build_guild_member_list_success_body(
    selected_character_id: int,
    member_list: GuildMemberList,
    *,
    selected_channel_id: int = GUILD_INFO_DEFAULT_CHANNEL_ID,
    response_opcode: int = GUILD_MEMBER_LIST_OPCODE,
) -> bytes:
    """Build the exact Stage3 S1/0043 body consumed by the guild panel."""

    selected_id = int(selected_character_id)
    if selected_id <= 0:
        raise ValueError("selected character id is invalid")
    members = tuple(member_list.members)
    if not members:
        raise ValueError("guild has no live member rows")
    if not any(int(member.character_id) == selected_id for member in members):
        raise ValueError("selected character is not a live guild member")
    if len(members) > 0xFFFF:
        raise ValueError("guild member count exceeds u16")
    guild_id = int(member_list.guild.guild_id)
    if not 0 < guild_id <= 0xFFFFFFFF:
        raise ValueError("guild id is outside u32")
    channel_id = int(selected_channel_id)
    if not 0 < channel_id < GUILD_MEMBER_OFFLINE:
        raise ValueError("selected channel id is outside Stage3 member-state u8")

    guild_name = member_list.guild.display_name.encode("utf-8")
    body = bytearray((GUILD_MEMBER_LIST_SUCCESS,))
    body += guild_id.to_bytes(4, "little", signed=False)
    body += _required_name_stream(guild_name, "guild_name")
    body += GUILD_MEMBER_LIST_HEADER.to_bytes(2, "little", signed=False)
    body += len(members).to_bytes(2, "little", signed=False)

    for member in members:
        body += int(member.character_id).to_bytes(4, "little", signed=False)
        body += _required_name_stream(member.name_bytes, "member_name")
        body += _stream_bytes(b"")
        body += int(member.level).to_bytes(2, "little", signed=False)
        is_selected_member = int(member.character_id) == selected_id
        online_state = channel_id if is_selected_member else GUILD_MEMBER_OFFLINE
        guild_grade = stage3_guild_grade_for_member_role(member.member_role)
        body += bytes(
            (
                int(member.job) & 0xFF,
                int(member.grow_type) & 0xFF,
                0,
                0,
                online_state,
                0,
                0,
                guild_grade,
            )
        )
        body += (0).to_bytes(4, "little", signed=False)
        # The shared Stage3 reader stores this second trailing u32 at member
        # row +0x15C. The roster manager groups rows by that value, so a zero
        # for every member collapses the whole guild into one visible row.
        roster_key = stage3_guild_member_roster_key(member.character_id)
        body += roster_key.to_bytes(4, "little", signed=False)
        body += bytes((0,))
        body += _stream_bytes(b"")
        body += bytes((0,))

    if int(response_opcode) == GUILD_ALL_MEMBER_LIST_OPCODE:
        # Stage3's shared S1/0043/008C reader consumes this u32 only for
        # S1/008C (and its continuation alias). The local snapshot has no
        # continuation state, so the required field is zero.
        body += GUILD_ALL_MEMBER_LIST_TRAILER.to_bytes(4, "little", signed=False)
    return bytes(body)


class GuildMemberList0043Service:
    """Project durable local guild membership into Stage3's guild UI reader."""

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
        *,
        response_opcode: int,
        request_name: str,
    ) -> tuple[list[bytes], str]:
        response = bytes((GUILD_MEMBER_LIST_FAILURE,))
        packet = self.server._codec_response(1, response_opcode, response)
        return [packet], (
            "%s reject=%s selected_character=%s S1/%04X=%s"
            % (
                request_name,
                reason,
                selected_character_id,
                response_opcode,
                response.hex(" "),
            )
        )

    def handle_member_list_request(
        self,
        body: bytes,
        *,
        session: Any,
        response_opcode: int = GUILD_MEMBER_LIST_OPCODE,
        request_name: str = "C1/0043 GUILD_MEMBER_LIST",
        emit_current_member_grade: bool = True,
    ) -> tuple[list[bytes], str]:
        response_opcode = int(response_opcode) & 0xFFFF
        if response_opcode not in {
            GUILD_MEMBER_LIST_OPCODE,
            GUILD_ALL_MEMBER_LIST_OPCODE,
        }:
            raise ValueError("unsupported Stage3 guild member-list response opcode")
        selected_id = int(getattr(session, "selected_character_id", 0) or 0)
        if selected_id <= 0 or not parse_guild_member_list_body(body):
            return self._failure(
                "invalid_body_or_selected_character",
                selected_id,
                response_opcode=response_opcode,
                request_name=request_name,
            )

        member_list = self._repository().get_member_list_for_character(selected_id)
        if member_list is None:
            return self._failure(
                "guild_missing",
                selected_id,
                response_opcode=response_opcode,
                request_name=request_name,
            )

        try:
            channel_id = _session_channel_id(session)
            guild_info_response = build_guild_info_body(channel_id=channel_id)
            guild_member_info_response = None
            if emit_current_member_grade:
                current_grade = current_member_guild_grade(selected_id, member_list)
                guild_member_info_response = bytes((current_grade,))
            response = build_guild_member_list_success_body(
                selected_id,
                member_list,
                selected_channel_id=channel_id,
                response_opcode=response_opcode,
            )
        except ValueError as exc:
            return self._failure(
                "invalid_snapshot:%s" % exc,
                selected_id,
                response_opcode=response_opcode,
                request_name=request_name,
            )

        guild_info_packet = self.server._codec_response(
            0,
            GUILD_INFO_OPCODE,
            guild_info_response,
        )
        packets = [guild_info_packet]
        if guild_member_info_response is not None:
            packets.append(
                self.server._codec_response(
                    0,
                    GUILD_MEMBER_INFO_OPCODE,
                    guild_member_info_response,
                )
            )
        packets.append(self.server._codec_response(1, response_opcode, response))
        grades = ",".join(
            "%s:%s" % (
                member.character_id,
                stage3_guild_grade_for_member_role(member.member_role),
            )
            for member in member_list.members
        )
        roster_keys = ",".join(
            "%s:%s"
            % (
                member.character_id,
                stage3_guild_member_roster_key(member.character_id),
            )
            for member in member_list.members
        )
        current_grade_note = (
            guild_member_info_response.hex(" ")
            if guild_member_info_response is not None
            else "skipped_preselect"
        )
        return packets, (
            "%s selected_character=%s guild=%s name=%r members=%s grades=%s "
            "roster_keys=%s channel=%s S0/0046=%s S0/007F=%s S1/%04X=%s"
            % (
                request_name,
                selected_id,
                member_list.guild.guild_id,
                member_list.guild.display_name,
                len(member_list.members),
                grades,
                roster_keys,
                channel_id,
                guild_info_response.hex(" "),
                current_grade_note,
                response_opcode,
                response.hex(" "),
            )
        )

    def handle_guild_member_list_0043(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        return self.handle_member_list_request(
            body,
            session=session,
            response_opcode=GUILD_MEMBER_LIST_OPCODE,
            request_name="C1/0043 GUILD_MEMBER_LIST",
        )
