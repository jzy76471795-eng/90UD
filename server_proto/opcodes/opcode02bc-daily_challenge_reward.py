"""C1/02BC Daily Challenge group reward claim."""
from __future__ import annotations

from typing import Any

from game.quest.daily_challenge import (
    build_daily_challenge_group_reward_ack,
    handle_daily_challenge_group_reward,
)
from game.quest.quest_session import resolve_selected_character_detail


def handle_daily_challenge_reward_02bc(
    server: Any,
    plain: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    body = bytes(plain or b"")
    if len(body) != 4:
        return [
            server._codec_response(
                1,
                0x02BC,
                build_daily_challenge_group_reward_ack(
                    success=False, error_code=0x16
                ),
            )
        ], "C1/02BC DAILY_CHALLENGE_REWARD reject=body_len_%d expect=4" % len(body)

    group_index = int.from_bytes(body, "little")
    detail = resolve_selected_character_detail(server, session)
    character_id = detail.get("character_id")
    level = detail.get("level")
    if character_id is None:
        return [
            server._codec_response(
                1,
                0x02BC,
                build_daily_challenge_group_reward_ack(
                    success=False, error_code=0x16
                ),
            )
        ], "C1/02BC DAILY_CHALLENGE_REWARD group=%d reject=no_character" % group_index

    packets, note = handle_daily_challenge_group_reward(
        server,
        character_repo=server._repo(),
        character_id=int(character_id),
        level=int(level or 1),
        group_index=group_index,
        session=session,
    )
    return packets, "C1/02BC DAILY_CHALLENGE_REWARD group=%d; %s" % (
        group_index,
        note,
    )
