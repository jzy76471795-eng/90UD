"""Current-US raid battle body codecs and exact initial lobby projection.

Pinned EXE: 14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634.
Widths/order are native reader facts. Required unnamed fields are deliberately
explicit; only the lobby helpers apply proven constructor values. ACK helpers
return extra payload after the existing generic result/error bytes.
"""
from dataclasses import dataclass
import struct
from typing import Sequence

from .protocol import (
    RaidMember, RaidMetadata, _integer, _i8, _u8, _u16, _u32, _members,
    encode_member, encode_metadata,
)

C_RAID_ENTRY_COST_INFO = 0x0293
C_SELECT_RAID_REWARD_CARD = 0x0295
C_RAID_INFO = 0x054E
N_RAID_PARTICIPATION = 0x0249
N_RAID_DUNGEON_STATE = 0x024A
N_RAID_CHANGE_DUNGEON_STATE = 0x024B
N_RAID_STATE = 0x024C
N_PREPARE_START_RAID = 0x0253
N_START_RAID = 0x0255
N_RAID_REMAIN_TIME = 0x0256
N_RAID_ENTRY_COST_INFO = 0x0257
N_RAID_MOVIE_SKIP = 0x0258
N_RAID_REWARD_LIST = 0x0259
N_RAID_RESULT = 0x025A
N_RAID_PHASE_COIN_INFO = 0x0308


def encode_phase_coin_info(used: int) -> bytes:
    """0164FEF0 reads u8; 01199610 stores the used count at manager+198.

    01879FA0 compares it >= the phase cap; 00ACCC30 renders cap-used.
    This is a used count, never a remaining-count field.
    """
    return _u8(used)


@dataclass(frozen=True)
class DungeonParticipation:
    dungeon_id: int
    field_u8: int
    member_ids: Sequence[int]


@dataclass(frozen=True)
class EntryCost:
    member_id: int
    owned_flag: int
    quantity: int


@dataclass(frozen=True)
class RewardItem:
    kind: int
    item_id: int
    quantity: int


@dataclass(frozen=True)
class MemberReward:
    member_id: int
    field_u8: int
    rewards: Sequence[RewardItem]


def lobby_member(*, member_id: int, name_bytes: bytes, base_job: int,
                 packed_grow: int, party_number: int, level: int) -> RaidMember:
    """Initial 01199AF0 defaults with four real actor fields substituted.

    The historical parameter ``party_index`` actually holds base job (+28).
    011A4B53 -> actor +04; +2C splits grow/subgrow to actor +08/+0C;
    +35 is written to encoded level slot +14 at 011A4AF4 .. 011A4B4E.
    True party number is +2D (0146160D UI buttons 1001..1010).
    01199B20 writes word 00FF: +34=FF, +35=00, not the reverse.
    """
    _integer(base_job, 0, 15)
    _integer(packed_grow, 0, 127)
    _integer(party_number, 0, 10)
    _integer(level, 1, 255)
    member = RaidMember(member_id, 0, name_bytes, base_job, packed_grow,
                        party_number, 0, 0, 255, level, 0)
    encode_member(member)
    return member


def lobby_metadata(*, raid_id: int, name_bytes: bytes, raid_type: int,
                   leader: RaidMember) -> RaidMetadata:
    """01606AA1/AA4/AAD defaults: state=0, field34=0, stage=FF."""
    metadata = RaidMetadata(raid_id, name_bytes, raid_type, 0, -1, 0, leader)
    encode_metadata(metadata)
    return metadata


def encode_raid_state(state: int, state_index: int) -> bytes:
    """01648704/713. State6's 02C37300 is only a vector constructor."""
    _integer(state, 0, 6)
    return _u8(state) + _i8(state_index)


def encode_participation(entries: Sequence[DungeonParticipation]) -> bytes:
    """0163D063 count; D087/D095/D0A3 row; D147 repeated u16 UID."""
    body = _u8(len(entries))
    for row in entries:
        if not isinstance(row, DungeonParticipation):
            raise ValueError('complete dungeon participation rows are required')
        body += (_u32(row.dungeon_id) + _u8(row.field_u8) +
                 _u8(len(row.member_ids)) +
                 b''.join(_u16(uid) for uid in row.member_ids))
    return body


def encode_dungeon_states(field_u8: int, entries: Sequence[tuple[int, int]],
                          tail_u32: int) -> bytes:
    """01633B8D/B98/BB4/BBF/BE8: u8,u8,(u32,u8)*count,u32."""
    return (_u8(field_u8) + _u8(len(entries)) +
            b''.join(_u32(dgn) + _u8(state) for dgn, state in entries) +
            _u32(tail_u32))


def encode_dungeon_change(dungeon_id: int, field_u8: int, state: int) -> bytes:
    """0163539A/3A5/3B0; the middle byte is consumed even when not used."""
    return _u32(dungeon_id) + _u8(field_u8) + _u8(state)


def encode_prepare_start() -> bytes:
    """0165DFD0..0165E252 contains no network body reader."""
    return b''


def encode_raid_delay(index: int, key_id: int, end_time: int,
                      duration: int) -> bytes:
    """01639D2C/35/3E/47: i8,u32,u32,u32 -> 011A02A0.

    Anton stores end_time-duration .. end_time. Luke stores current native
    seconds .. current native seconds+duration; do not silently convert units.
    """
    return _i8(index) + _u32(key_id) + _u32(end_time) + _u32(duration)


def encode_raid_timer(timer_id: int, remaining_seconds: int) -> bytes:
    """01633C1C/25 -> 011A11D0; seconds multiplied by 1000 for expiry."""
    return _u8(timer_id) + _u32(remaining_seconds)


def encode_entry_cost(entries: Sequence[EntryCost]) -> bytes:
    """0164C6E6 count:u32; 6F9/704/714 rows: u16,u8,u16."""
    body = _u32(len(entries))
    for row in entries:
        if not isinstance(row, EntryCost):
            raise ValueError('complete entry-cost rows are required')
        body += _u16(row.member_id) + _u8(row.owned_flag) + _u16(row.quantity)
    return body


def parse_entry_cost_request(body: bytes) -> int:
    """01199722/011A707E write one normalized u8 boolean for opcode0293."""
    if not isinstance(body, bytes) or len(body) != 1 or body[0] not in (0, 1):
        raise ValueError('RAID_ENTRY_COST_INFO body must be one boolean byte')
    return body[0]


def entry_cost_success_payload() -> bytes:
    """015FEA00 handler success consumes no extra body fields."""
    return b''


def encode_movie_skip() -> bytes:
    """01657190 movie/UI callback consumes no network body fields."""
    return b''


def encode_result(kind: int, result: int, field_u32: int, field_u16: int,
                   field_u8: int, flag: int) -> bytes:
    """01661E84/E96/EA6/EBA/EC5/EDA: B,B,I,H,B,B (10 bytes)."""
    _integer(kind, 0, 3)  # 01661F73 compare3 before jump table 016625AC.
    return (_u8(kind) + _u8(result) + _u32(field_u32) + _u16(field_u16) +
            _u8(field_u8) + _u8(flag))


def encode_reward_list(group: int, entries: Sequence[MemberReward]) -> bytes:
    """0166F619/F627; F657/F665/F670; F729/F735/F73E."""
    body = _u8(group) + _u8(len(entries))
    for member in entries:
        if not isinstance(member, MemberReward):
            raise ValueError('complete member reward rows are required')
        body += (_u16(member.member_id) + _u8(member.field_u8) +
                 _u8(len(member.rewards)))
        for reward in member.rewards:
            if not isinstance(reward, RewardItem):
                raise ValueError('complete item reward rows are required')
            body += _u8(reward.kind) + _u32(reward.item_id) + _u16(reward.quantity)
    return body


def parse_select_card_request(body: bytes) -> tuple[int, int]:
    """007C3F71/F81 etc. write two u8 values for opcode0295."""
    if not isinstance(body, bytes) or len(body) != 2:
        raise ValueError('SELECT_RAID_REWARD_CARD body must be exactly two u8')
    return struct.unpack('<BB', body)


def select_card_success_payload() -> bytes:
    """0160658D success jumps to 016066A4, without an extra reader."""
    return b''


def parse_raid_info_request(body: bytes) -> tuple[int, int]:
    """011997B5 writes u8 action; 011997C2 writes u32 raid ID (not u16)."""
    if not isinstance(body, bytes) or len(body) not in (5, 16):
        raise ValueError('RAID_INFO requires logical5 or one idx0-aligned16 body')
    action, raid_id = struct.unpack('<BI', body[:5])
    _integer(action, 0, 2)
    return action, raid_id


def raid_info_success_payload(action: int, raid_id: int, *,
                               metadata: RaidMetadata | None = None,
                               members: Sequence[RaidMember] | None = None) -> bytes:
    """01606B6B/6B7B; action0 metadata, 1 roster, 2 metadata+roster."""
    _integer(action, 0, 2)
    body = _u8(action) + _u32(raid_id)
    if action in (0, 2):
        if metadata is None or metadata.raid_id != raid_id:
            raise ValueError('matching complete metadata is required')
        body += encode_metadata(metadata)
    elif metadata is not None:
        raise ValueError('action1 has no metadata body')
    if action in (1, 2):
        body += _members(members)
    elif members is not None:
        raise ValueError('action0 has no roster body')
    return body


def raid_info_failure_payload(action: int, raid_id: int) -> bytes:
    """Even generic ACK failure consumes u8+u32 at 01606D2A/6D3A."""
    _integer(action, 0, 2)
    return _u8(action) + _u32(raid_id)
