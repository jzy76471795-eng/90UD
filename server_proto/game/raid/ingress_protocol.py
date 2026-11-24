"""Current-US raid search/join body codecs; existing frozen codecs unchanged.

Only widths, observed sender variants, and visible consumer identities are
encoded. Root owns channel/session authorization; these values are not CIDs.
The four search-filter bytes remain opaque: their full business mapping has
not been proved. Search returns S0/02F2, not a fabricated S1/033E ACK.
"""
from dataclasses import dataclass, field
import struct
from typing import Sequence

from .protocol import RaidMetadata, _integer, _text, _u16, _u32, encode_raid_list


C_RAID_SEARCH = 0x033E
N_RAID_SEARCH_RESULT = 0x02F2
C_JOIN_RAID_VIA_PARTY = 0x000A
C_JOIN_RAID_APPROVAL = 0x000B
C_JOIN_RAID_OTHER_NAMESPACE = 0x0334
C_RAID_MEMBER_LOOKUP = 0x0378
C_SET_RAID_WAITING = 0x029C
C_RAID_MANAGER_WORK = 0x029E
C_RAID_ACTION_294 = 0x0294
N_RAID_WAITING_LIST = 0x0251
RAID_JOIN_KIND = 10
N_JOIN_REQUEST = 0x0007


@dataclass(frozen=True)
class RaidSearchRequest:
    action: int
    cursor: int | None
    filters: bytes | None
    search_bytes: bytes | None


@dataclass(frozen=True)
class RaidJoinRequest:
    target_key_u16: int
    actor_value_u32: int
    derived_value_u16: int
    frame: bytes = field(default=b'', repr=False, compare=False)


@dataclass(frozen=True)
class RaidJoinApprovalRequest:
    target_key_u16: int
    actor_value_u32: int
    frame: bytes = field(default=b'', repr=False, compare=False)


@dataclass(frozen=True)
class RaidMemberLookupRequest:
    namespace_u8: int
    target_key_u16: int
    frame: bytes = field(default=b'', repr=False, compare=False)


def parse_search_request(body: bytes) -> RaidSearchRequest:
    """0119A7FF/849 sends033E: action1 only, or action0+raw8+text.

    01464E55 page multiplier33 fills raw8[0:4]. raw8[4:8] is four
    UI-derived filter bytes with unconfirmed names. 0119A876 writes raw8;
    0119A88F writes u32 byte length + codepage-encoded search text.
    """
    if not isinstance(body, bytes) or not body:
        raise ValueError('missing raid search action')
    if body == b'\x01':
        return RaidSearchRequest(1, None, None, None)
    if body[0] != 0 or len(body) < 13:
        raise ValueError('unconfirmed raid search body variant')
    cursor = struct.unpack_from('<I', body, 1)[0]
    filters = body[5:9]
    length = struct.unpack_from('<I', body, 9)[0]
    search = body[13:]
    if length != len(search):
        raise ValueError('raid search text length mismatch')
    _text(search)
    return RaidSearchRequest(0, cursor, filters, search)


def encode_search_result(total_count: int, cursor: int,
                         entries: Sequence[tuple[RaidMetadata, int]]) -> bytes:
    """S0/02F2 0166C9A0: total:u32,cursor:u32,rowcount:u32,rows.

    0166C9D1 -> manager+1B0 total; 014635D7 uses it for page count.
    0166C9EF -> manager+1B4 cursor (native clamps to total).
    0166CA15/5C/6A read count and metadata/membercount rows.
    0166CB58 clears search pending+1B8 even for an empty result.
    """
    _integer(total_count, 0, 0x7FFFFFFF)
    _integer(cursor, 0, total_count)
    if len(entries) > total_count:
        raise ValueError('visible rows exceed search total')
    return _u32(total_count) + _u32(cursor) + encode_raid_list(entries)


def parse_join_request(body: bytes) -> RaidJoinRequest:
    """011991E0 logical candidate: u16key,u8kind10,u32value,u16derived,u32zero.

    678 proves the fourth value comes from 015BB170 numeric computation,
    including a legitimate zero path. Neither u16 establishes a selected CID
    or authenticated leader. 680 establishes 13 bytes only under its recorded
    transport conditions; callers must retain the original frame/padding.
    """
    # Plain-wire hooks expose the logical13. The existing idx10 decoder
    # retains the 4-byte-unit padding (16 total). Preserve that opaque suffix;
    # it is not another business field or a source of join authority.
    if not isinstance(body, bytes) or len(body) not in (13, 16):
        raise ValueError('raid join requires logical13 or one idx10-aligned16 body')
    target_key, kind, actor_value, derived, tail = struct.unpack('<HBIHI', body[:13])
    if kind != RAID_JOIN_KIND or tail != 0:
        raise ValueError('unconfirmed party-join variant or raid join tail')
    _integer(target_key, 0, 0xFFFF)
    _integer(derived, 0, 0xFFFF)
    return RaidJoinRequest(target_key, actor_value, derived, body)


def parse_join_approval_request(body: bytes) -> RaidJoinApprovalRequest:
    """029A92BD..92FF selected000B logical body: word, mode0A, dword.

    The actor/namespace mapping and approval correlation belong to server
    authority, not either number in this body.
    """
    if not isinstance(body, bytes) or len(body) not in (7, 8):
        raise ValueError('raid approval requires logical7 or one idx11-aligned8 body')
    key, kind, value = struct.unpack('<HBI', body[:7])
    if kind != RAID_JOIN_KIND:
        raise ValueError('unconfirmed raid approval kind')
    return RaidJoinApprovalRequest(key, value, body)


def parse_member_raid_lookup_request(body: bytes) -> RaidMemberLookupRequest:
    """01199940 requests0378: byte namespace, word actor key.

    029A9202's separate0334 branch requests the same widths. This does not
    establish the same authorization or successful business outcome.
    """
    if not isinstance(body, bytes) or len(body) not in (3, 8):
        raise ValueError('member lookup requires logical3 or one aligned8 body')
    return RaidMemberLookupRequest(*struct.unpack('<BH', body[:3]), body)


def member_raid_lookup_success(raid_key: int, leader_actor_key: int, member_count: int) -> bytes:
    """015FF6A0 extra: raid:u32, leader actor:u16, member count:u8.

    01199650 stores the last fields at manager+1A4/+1A6. 011A7F80
    substitutes them for raid+38 (nested leader UID, 0119F550/0119E570)
    and raid+74 (actual roster count, 011A4790). Its leader word reaches
    0119C300 closure+34, then 0119C390 -> C000A. These are not an extra
    applicant UID or server/channel pair. Generic ACK prefix is separate.
    """
    _integer(raid_key, 1, 0xffffffff)
    _integer(leader_actor_key, 1, 0xfffe)
    _integer(member_count, 1, 20)
    return _u32(raid_key) + _u16(leader_actor_key) + bytes((member_count,))


def join_ack_extra(success: bool) -> bytes:
    """S1/000A 01610F80: success no extra; failure always reads kind:u8.

    Extra kind10 takes no further network reads. The additional u32 branch
    at016115EF is only for kinds4/13, which this narrow raid codec rejects.
    Caller supplies existing generic success/error bytes before this extra.
    """
    if type(success) is not bool:
        raise ValueError('explicit ACK success required')
    return b'' if success else b'\x0a'


def encode_join_request_notice(*, actor_key: int, revive_coins: int,
                               fatigue: int, weakness_percent: int, exorcism: int) -> bytes:
    """S0/0007 mode0A: actor word, kind, revive dword, three profile words.

    01675300 first requires the actor to exist in01E7F0F0. The mode0A
    branch stores +10/+1C/+20/+38. 029AB2D0 formats these with current
    DSTR79CC..79CF: revive coins, fatigue, weakness percent, exorcism.
    Caller must provide actual server values and prove actor visibility;
    encoding this body does not authorize or commit raid membership.
    """
    _integer(actor_key, 1, 0xffff)
    return (_u16(actor_key) + bytes((RAID_JOIN_KIND,)) + _u32(revive_coins)
            + _u16(fatigue) + _u16(weakness_percent) + _u16(exorcism))


def join_approval_ack_extra(success: bool, *, actor_key: int | None = None) -> bytes:
    """01611700 success reads target word+kind; mode0A reads no more.

    A failed ACK always reads one kind byte. The generic ACK result/error
    prefix remains the caller's responsibility. This is not join authority.
    """
    if type(success) is not bool or (not success and actor_key is not None):
        raise ValueError('explicit approval ACK outcome and matching target required')
    if success:
        _integer(actor_key, 1, 0xffff)
        return _u16(actor_key) + bytes((RAID_JOIN_KIND,))
    return bytes((RAID_JOIN_KIND,))


def waiting_ack_extra(success: bool) -> bytes:
    """S1/029C full01606440..55F has no extra reader on either branch."""
    if type(success) is not bool:
        raise ValueError('explicit ACK success required')
    return b''


def empty_waiting_list_payload() -> bytes:
    """S0/0251 0165200C reads a u16 count; zero consumes no row fields.

    C033E action1 caller reads the same waiting vector as N0251 fills.
    Their request/response timing is inferred and still needs local capture.
    No positive waiting-row business values are invented by this helper.
    """
    return b'\x00\x00'


def parse_action294_request(body: bytes) -> int:
    """0107DCB0 writes literal1, 0107DCF3 writes literal0; u8 only.

    Callers are confirmed; business labels for 0/1 remain unproved.
    No waiting-state or admission mutation is implied by parsing this byte.
    """
    if not isinstance(body, bytes) or len(body) != 1 or body[0] not in (0, 1):
        raise ValueError('unconfirmed0294 request body')
    return body[0]


def action294_ack_extra(success: bool) -> bytes:
    """S1/0294 registers no-op004239F0, consuming no extra body."""
    if type(success) is not bool:
        raise ValueError('explicit ACK success required')
    return b''


def unsupported_join_failure_body() -> bytes:
    """Root's local unsupported-join policy: generic00/13 + required kind0A.

    Generic error13 is a chosen rejection, not a newly inferred business enum.
    01610FAE requires the extra byte for all failures; kind10 has no u32 tail.
    Return a complete ACK body, without packet header or encryption.
    """
    return b'\x00\x13\x0a'
