"""Body-only raid codecs verified against the pinned current US DFO.exe.

EXE SHA256: 14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634.
Generic packet headers, ACK result/error bytes and transport encryption belong
to the existing server. This module never copies CN opcode numbers or solo tails.
Names remain bytes: native conversion uses codepage global 0x042D43D8, whose
runtime value has not been proven by this static analysis.
"""
from dataclasses import dataclass
import struct
from typing import Sequence

C_CREATE_RAID = 0x0299  # send 0146289C; S1 registration 01608B21.
C_LEAVE_RAID = 0x029A   # send 01199A04; S1 registration 01608B34.
C_START_RAID = 0x029B   # send 0119A0C3; S1 registration 01608B5A.
C_MODIFY_RAID = 0x029F  # save01462750; S1 registration016087B0 ->015FF750.
N_RAID_LIST = 0x024F    # S0 registration 01671461; reader 0166C8D0.
N_RAID_MODIFY = 0x0250  # S0 registration 01671474; reader 01666870.
# Actual local USERINFO0/1 rows and 01199AF0 lobby members use context0.
# CHANNELINFO field8 and raid-key high words must agree with that producer.
# This native namespace is separate from selected server channel23/32.
LOCAL_NATIVE_NAMESPACE = 0


@dataclass(frozen=True)
class RaidMember:
    """Exact 0119E570 reader; unproven semantic names use object offsets."""
    member_id: int
    field04: int
    name_bytes: bytes
    party_index: int
    field2c: int
    field2d: int
    field2e: int
    field30: int
    field34: int
    field35: int
    field38: int


@dataclass(frozen=True)
class RaidMetadata:
    """Exact 0119F550 reader including the nested 0119E570 leader."""
    raid_id: int
    name_bytes: bytes
    raid_type: int
    state: int
    state_index: int
    field34: int
    leader: RaidMember


def _integer(value: int, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'wire integer must be in [{low}, {high}]')
    return value


def _u8(value: int) -> bytes:
    return struct.pack('<B', _integer(value, 0, 0xFF))


def _i8(value: int) -> bytes:
    return struct.pack('<b', _integer(value, -128, 127))


def _u16(value: int) -> bytes:
    return struct.pack('<H', _integer(value, 0, 0xFFFF))


def _u32(value: int) -> bytes:
    return struct.pack('<I', _integer(value, 0, 0xFFFFFFFF))


def _text(value: bytes) -> bytes:
    # 02BF4BF0 reads u32 length, permits length < 0x100, then that many bytes.
    if not isinstance(value, bytes) or len(value) >= 0x100 or b'\0' in value:
        raise ValueError('wire name must be NUL-free bytes shorter than 256')
    return _u32(len(value)) + value


def parse_create_request(body: bytes) -> tuple[int, bytes]:
    """014628C0 writes u8 type; 014628DD writes u32 length + name bytes."""
    if not isinstance(body, bytes) or len(body) < 5:
        raise ValueError('truncated CREATE_RAID body')
    raid_type, length = struct.unpack_from('<BI', body)
    if raid_type not in (0, 1):
        raise ValueError('only verified Anton/Luke raid types are supported')
    name = body[5:]
    if length != len(name) or not name:
        raise ValueError('CREATE_RAID name length mismatch or empty name')
    _text(name)
    return raid_type, name


def parse_leave_request(body: bytes) -> int:
    """01199AA2 writes u16 local member ID; 02BF0580 copies two bytes."""
    if not isinstance(body, bytes) or len(body) != 2:
        raise ValueError('LEAVE_RAID body must contain exactly one u16')
    return struct.unpack('<H', body)[0]


def parse_modify_request(body: bytes) -> tuple[int, bytes]:
    """01462750 save uses the same u8 type + DSTR writers as CREATE."""
    return parse_create_request(body)


def parse_start_request(body: bytes) -> None:
    """0119A0C3 .. 0119A0D4 sends START_RAID with no body writer call."""
    if body != b'':
        raise ValueError('START_RAID body must be empty')


def create_success_payload(raid_id: int) -> bytes:
    """01605B38 reads u32 raid ID on generic ACK success."""
    return _u32(raid_id)


def leave_success_payload(reset_flag: int) -> bytes:
    """016061FE reads u8; nonzero triggers the full local leave reset."""
    return _u8(reset_flag)


def start_success_payload() -> bytes:
    """01605E3F success jumps to 01605FE6 without any extra reader."""
    return b''


def encode_member(member: RaidMember) -> bytes:
    if not isinstance(member, RaidMember):
        raise ValueError('complete verified RaidMember fields are required')
    return (_u16(member.member_id) + _u8(member.field04) +
            _text(member.name_bytes) + _i8(member.party_index) +
            _u8(member.field2c) + _u8(member.field2d) + _u8(member.field2e) +
            _u32(member.field30) + _u8(member.field34) +
            _u8(member.field35) + _u8(member.field38))


def encode_metadata(metadata: RaidMetadata) -> bytes:
    if not isinstance(metadata, RaidMetadata):
        raise ValueError('complete verified RaidMetadata fields are required')
    if metadata.raid_type not in (0, 1):
        raise ValueError('only verified Anton/Luke raid types are supported')
    return (_u32(metadata.raid_id) + _text(metadata.name_bytes) +
            _u8(metadata.raid_type) + _u8(metadata.state) +
            _i8(metadata.state_index) + _u32(metadata.field34) +
            encode_member(metadata.leader))


def _members(members: Sequence[RaidMember] | None) -> bytes:
    # 011A47CC reads u8 count; 011A49CD invokes 0119E570 for every member.
    if members is None:
        raise ValueError('explicit complete member roster is required')
    return _u8(len(members)) + b''.join(encode_member(m) for m in members)


def encode_raid_list(entries: Sequence[tuple[RaidMetadata, int]]) -> bytes:
    """0166C8FC count:u32; 0166C93C metadata; 0166C94A trailing u8."""
    return _u32(len(entries)) + b''.join(
        encode_metadata(metadata) + _u8(member_count)
        for metadata, member_count in entries)


def encode_raid_modify(raid_id: int, operation: int, *,
                       metadata: RaidMetadata | None = None,
                       members: Sequence[RaidMember] | None = None,
                       member_count: int | None = None) -> bytes:
    """0166689B/016668A4: raid_id:u32 + operation:u32.

    Jump table 01666D24: 0 -> 01666993 (metadata+roster),
    1 -> 01666AE9 (delete), 2 -> 01666B1E (metadata; 016FA170 is a no-op),
    3 -> 01666B48 (roster), 4 -> 01666CB8 (one u8 member count).
    """
    header = _u32(raid_id) + _u32(operation)
    if operation == 0:
        if metadata is None or metadata.raid_id != raid_id or member_count is not None:
            raise ValueError('add requires matching metadata and explicit roster')
        return header + encode_metadata(metadata) + _members(members)
    if operation == 1:
        if metadata is not None or members is not None or member_count is not None:
            raise ValueError('delete consumes no fields beyond the header')
        return header
    if operation == 2:
        if (metadata is None or metadata.raid_id != raid_id or
                members is not None or member_count is not None):
            raise ValueError('metadata action requires only matching metadata')
        return header + encode_metadata(metadata)
    if operation == 3:
        if metadata is not None or member_count is not None:
            raise ValueError('roster action consumes only its explicit roster')
        return header + _members(members)
    if operation == 4:
        if metadata is not None or members is not None or member_count is None:
            raise ValueError('count action requires only an explicit u8 count')
        return header + _u8(member_count)
    raise ValueError('unverified RAID_MODIFY operation; refusing to guess fields')
