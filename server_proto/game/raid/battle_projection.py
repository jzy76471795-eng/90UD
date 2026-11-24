"""Current-US UI projection facts; unnamed business fields remain mandatory.

Pinned DFO.exe SHA256 14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634.
This module does not infer fees, reward ownership, phase transitions or rank.
"""
import struct

from .protocol import _integer, _u8, _u32
from .battle_protocol import (
    RewardItem, encode_dungeon_states, encode_dungeon_change,
    encode_raid_delay, encode_result,
)

C_SET_RAID_WAITING = 0x029C
C_RAID_REJOIN = 0x029D
C_RAID_MANAGER_WORK = 0x029E
N_RAID_SYMBOL = 0x0248


def dungeon_ui_capabilities(native_state: int) -> tuple[bool, bool, int | None]:
    """visible, enter enabled, optional timer key used by UI.

    0096370F permits only state0; 00FEBFE8/00FEC003 exclude -1/2;
    00FEC55D/574 route 1/3 to timer(2,dungeon); 00961CE9 uses timer1.
    The caller must preserve state1 and3 even though these UI effects match.
    """
    _integer(native_state, 0, 3)
    return ((True, True, 1), (True, False, 2),
            (False, False, None), (True, False, 2))[native_state]


def dungeon_states_body(states, *, tracked_dungeon_id: int) -> bytes:
    """24A prefix byte is unused in handler; tail is a real dungeon ID.

    0119F400 stores tail in +150 and symbol8; 00FEF2A0/2AC feeds it to
    dungeon-state getter. Zero means no tracked dungeon. Require its identity.
    """
    entries = tuple(states)
    _integer(tracked_dungeon_id, 0, 0xffffffff)
    seen = set()
    for dungeon_id, state in entries:
        _integer(dungeon_id, 1, 0xffffffff)
        dungeon_ui_capabilities(state)
        if dungeon_id in seen:
            raise ValueError('duplicate dungeon projection')
        seen.add(dungeon_id)
    if tracked_dungeon_id and tracked_dungeon_id not in seen:
        raise ValueError('tracked dungeon must be present in this snapshot')
    return encode_dungeon_states(0, entries, tracked_dungeon_id)


def dungeon_change_body(dungeon_id: int, state: int) -> bytes:
    """24B middle byte is read but never consumed by complete handler."""
    _integer(dungeon_id, 1, 0xffffffff)
    dungeon_ui_capabilities(state)
    return encode_dungeon_change(dungeon_id, 0, state)


def symbols_body(writes) -> bytes:
    """1633B1D count:u8; 1633B34/3D key/value:u32; key0/value0 resets.

    Preserve event order and repeated keys. This normal writer rejects key0
    so missing identities cannot accidentally reset every client symbol.
    """
    rows = tuple(writes)
    body = _u8(len(rows))
    for symbol_id, value in rows:
        _integer(symbol_id, 1, 0xffffffff)
        body += _u32(symbol_id) + _u32(value)
    return body


def delay_body(timer_id: int, dungeon_id: int, end_seconds: int,
               duration_seconds: int) -> bytes:
    """255 native timer key and explicit native-clock seconds; no unit guess."""
    _integer(timer_id, 1, 3)
    _integer(dungeon_id, 1, 0xffffffff)
    _integer(end_seconds, 0, 0xffffffff)
    _integer(duration_seconds, 1, 0xffffffff)
    return encode_raid_delay(timer_id, dungeon_id, end_seconds, duration_seconds)


def result_is_success(native_kind: int) -> bool:
    """16625AC: kind0/2 success animation; kind1/3 fail animation."""
    _integer(native_kind, 0, 3)
    return native_kind in (0, 2)


def result_body(*, native_kind: int, phase_index: int, elapsed_seconds: int,
                display_count: int, rank: int, environment_flag_byte: int) -> bytes:
    """25A fields proven through result record and UI consumers.

    phase0/1 selects first/second phase resource; elapsed enters TimeBorad,
    divided by60 at007BD512..540; rank selects reward_rank animation frames.
    display_count enters NumberBorad; death/coin identity is not yet proven.
    The last flag is inverted then sets environment+31C; require explicit byte.
    """
    result_is_success(native_kind)
    _integer(phase_index, 0, 1)
    _integer(environment_flag_byte, 0, 1)
    return encode_result(native_kind, phase_index, elapsed_seconds,
                         display_count, rank, environment_flag_byte)


def reward_item(item_id: int, quantity: int, *, premium: bool) -> RewardItem:
    """kind1=reward_open_premium_00; kind0=reward_card_open1_00.

    010AA900 tests RewardItem+44==1; 007C2432/36 selects premium asset.
    Equipment/stackable classification is derived from item ID elsewhere.
    """
    if not isinstance(premium, bool):
        raise ValueError('explicit normal/premium reward identity required')
    _integer(item_id, 1, 0xffffffff)
    _integer(quantity, 1, 0xffff)
    return RewardItem(int(premium), item_id, quantity)


def parse_group_request(body: bytes) -> tuple[int, int]:
    """29E action0/memberUID/partyNo are three u32, not B/H/B.

    014670FE/0146710E/01467129; 0146162C/163C/164A target party0.
    Action1 is the separate leader transfer from popup menu 145.
    """
    if not isinstance(body, bytes) or len(body) != 12:
        raise ValueError('RAID_MANAGER_WORK body must be exactly three u32')
    action, member_id, party_number = struct.unpack('<III', body)
    if action != 0:
        raise ValueError('unconfirmed raid manager action')
    _integer(member_id, 1, 0xffff)
    _integer(party_number, 0, 10)
    return member_id, party_number


def parse_waiting_request(body: bytes) -> tuple[int, int]:
    """29C observed literal0 and currentRaid.type:u8 at1465D33/5D4B."""
    if not isinstance(body, bytes) or len(body) != 2:
        raise ValueError('SET_RAID_WAITING body must be exactly two u8')
    action, raid_type = struct.unpack('<BB', body)
    if action != 0:
        raise ValueError('unconfirmed waiting action')
    _integer(raid_type, 0, 1)
    return action, raid_type


def parse_rejoin_request(body: bytes) -> int:
    """165AC21 reads S0 raid ID to local;165AD29 retransmits same u32."""
    if not isinstance(body, bytes) or len(body) != 4:
        raise ValueError('RAID_REJOIN body must be exactly one u32')
    raid_id, = struct.unpack('<I', body)
    _integer(raid_id, 1, 0xffffffff)
    return raid_id


def group_success_payload() -> bytes:
    """29E S1 handler 004239F0 is a stub with no additional body reader."""
    return b''
