"""Publish a fresh Daily Challenge subscriber list for an online actor."""
from __future__ import annotations

from game.quest.daily_challenge import (
    ensure_daily_challenge_state,
    build_daily_challenge_body_0287,
)
from game.quest.quest_index import daily_quest_day_key


def pump(server, session, client, *, day_key=None):
    cid = getattr(session, 'selected_character_id', None)
    if type(cid) is not int or cid <= 0 or not (
            getattr(session, 'enter_seria_settled', False)
            or getattr(session, 'in_dungeon_run', False)):
        return
    previous = getattr(session, '_daily_challenge_identity', None)
    day = str(day_key or daily_quest_day_key())
    identity = (cid, day)
    # Login owns the first snapshot. A character switch must initialize its
    # own lists before this pump can participate; the common poll does no DB IO.
    if previous is None or previous == identity or previous[0] != cid:
        return
    repo = server._repo()
    actor = repo.get_character_level_job(cid)
    if actor is None:
        return
    state = ensure_daily_challenge_state(
        repo, character_id=cid, level=actor[0], day_key=day,
    )
    from game.quest.quest_session import pack_authoritative_finished_quest_snapshot
    packets, _note = pack_authoritative_finished_quest_snapshot(
        server, character_id=cid, allow_empty=True, context='daily_rollover',
    )
    packets.append(server._codec_response(
        0, 0x0287, build_daily_challenge_body_0287(state, include_claimed=False),
    ))
    for packet in packets:
        client.sendall(packet)
    # Failed/partial writes must not mark a snapshot as delivered.
    session._daily_challenge_identity = identity
    server.log('[DailyChallenge] online rollover cid=%d day=%s' % (cid, day))
