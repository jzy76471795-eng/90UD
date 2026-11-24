"""Stage3 C1/S1 0x008C complete guild member-list request."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from game.chain_loader import load_opcode
from game.guild.guild_repository import GuildRepository


GUILD_ALL_MEMBER_LIST_OPCODE = 0x008C
_member_list = load_opcode("opcode0043-guild_member_list.py")


class GuildAllMemberList008CService:
    """Use the native S1/008C alias of the S1/0043 member-list reader."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self._member_list = _member_list.GuildMemberList0043Service(server)
        self._guild_repository: GuildRepository | None = None

    def _repository(self) -> GuildRepository:
        if self._guild_repository is None:
            self._guild_repository = GuildRepository(self.server._repo().db_path)
        return self._guild_repository

    def _preselect_member_session(
        self,
        session: Any,
    ) -> tuple[Any, int | None]:
        """Resolve the pre-character-select 008C request from account membership."""

        account_id = int(getattr(self.server, "account_id", 0) or 0)
        character_id = self._repository().get_first_member_character_id_for_account(
            account_id
        )
        if character_id is None:
            return SimpleNamespace(
                selected_character_id=0,
                selected_channel_id=getattr(session, "selected_channel_id", None),
                channel_id=getattr(session, "channel_id", None),
            ), None
        return SimpleNamespace(
            selected_character_id=character_id,
            selected_channel_id=getattr(session, "selected_channel_id", None),
            channel_id=getattr(session, "channel_id", None),
        ), character_id

    def handle_guild_all_member_list_008c(
        self,
        body: bytes,
        *,
        session: Any,
    ) -> tuple[list[bytes], str]:
        selected_character_id = int(
            getattr(session, "selected_character_id", 0) or 0
        )
        request_session = session
        preselect_character_id: int | None = None
        if selected_character_id <= 0:
            request_session, preselect_character_id = self._preselect_member_session(
                session
            )

        packets, note = self._member_list.handle_member_list_request(
            body,
            session=request_session,
            response_opcode=GUILD_ALL_MEMBER_LIST_OPCODE,
            request_name="C1/008C GUILD_ALL_MEMBER_LIST",
            emit_current_member_grade=preselect_character_id is None,
        )
        if preselect_character_id is not None:
            note += " preselect_account_member=%s" % preselect_character_id
        return packets, note
