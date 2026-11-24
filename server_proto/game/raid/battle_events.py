"""Narrow current-US event bodies; caller validates all authoritative events."""
from .protocol import _integer
from .battle_protocol import (
    DungeonParticipation, encode_participation, encode_raid_state,
    encode_raid_timer,
)


def active_phase_body(phase_index: int) -> bytes:
    """24C state2 selects00_start_back/_1 at01648848..01648918."""
    _integer(phase_index, 0, 1)
    return encode_raid_state(2, phase_index)


def reward_phase_body(phase_index: int) -> bytes:
    """24C4:01648CA2 -> 02788AB0 -> 010AE3F0 starts RaidRewardDrawer."""
    _integer(phase_index, 0, 1)
    return encode_raid_state(4, phase_index)


def main_timer_body(remaining_seconds: int) -> bytes:
    """256 key0:1467DB7→011A1250 renders main countdown."""
    return encode_raid_timer(0, remaining_seconds)


def reward_timer_body(remaining_seconds: int) -> bytes:
    """256 key1:00AC5554 state5→push1 at00AC5560→011A1250."""
    return encode_raid_timer(1, remaining_seconds)


def _participation(dungeon_id: int, action: int, member_ids) -> bytes:
    _integer(dungeon_id, 1, 0xffffffff)
    ids = tuple(member_ids)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('unique actual member IDs are required')
    for member_id in ids:
        _integer(member_id, 1, 0xffff)
    return encode_participation((DungeonParticipation(dungeon_id, action, ids),))


def participation_add_body(dungeon_id: int, member_ids) -> bytes:
    """249 action2:163D27A→011A6190 appends a whole-party UID vector.

    Action1 does the same append plus an announcement; use quiet action2.
    Actual party ownership and capacity must be checked before calling.
    """
    return _participation(dungeon_id, 2, member_ids)


def participation_remove_body(dungeon_id: int, member_ids) -> bytes:
    """249 action0:163D314→011A6210 removes supplied UID subset.

    Action3 removes with an announcement; action4 does not mutate membership.
    """
    return _participation(dungeon_id, 0, member_ids)


def empty_participation_body() -> bytes:
    """An empty249 contains count0 and enters no action branches."""
    return encode_participation(())
