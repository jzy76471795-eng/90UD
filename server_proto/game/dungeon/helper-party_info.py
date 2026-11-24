"""Stage3 PARTY_INFO (S0/0x0009) builders.

Authority: Stage3 sub_165C310 (IDA session).
Its roster supplies the party actor table used by 01E83AA0 and 01E83690.
Publication timing and peer creation must be established independently;
001D does not unconditionally require its final byte to equal the own slot.
"""
from __future__ import annotations

import struct
from typing import Sequence


def build_party_info_type0(
    *,
    user_ids: Sequence[int],
    party_id: int = 1,
    party_index: int = 0,
    top_field: int = 0,
    unk: int = 0,
    name_flag: int = 0,
    title_present: int = 0,
    is_return: int = 0,
    user_max: int = 4,
    is_event: int = 0,
    info_field: int = 0,
    difficulty: int = 0,
    dungeon_id: int = 0,
    more: int = 0,
    aux: int = 0,
    aux2: int = 0,
    member_extras: Sequence[tuple[int, int, int]] | None = None,
    tail: Sequence[int] | None = None,
    has_extra: int = 0,
) -> bytes:
    """Build pinned Stage3 PARTY_INFO type0 (default-name variant).

    Wire (sub_165C310 read order):
      u16 blockCount
      u16 topField          # var_294; 0 skips special 0x270F path
      # block:
      u16 partyId
      u8  partyIndex
      u8  unk
      u8  type (=0)
      # Historical keyword names are retained for existing callers.
      u8 nameFlag, titlePresent
      u32 nameLength (=0, selects the native default name)
      u8 isReturn, isEvent, userMax
      u32 infoField
      u8  difficulty
      u16 dungeonId
      u8  more, aux
      u16 aux2
      # type0/2/6 roster:
      u8 memberCount
      memberCount x (u8 slot + u16 uid + 3x u8)
      4x u8 tail
      u8 hasExtra
    """
    # 0165C659 -> 02BF4C90 -> 02BF4BF0 reads a u32 length even
    # for the default name. 0165C6B1/C78D reads capacity AFTER the
    # two preceding opaque bytes; C000C's field order is different.
    if int(name_flag) != 0 or int(title_present) != 0:
        raise ValueError("only the verified default native party name is supported")
    members: list[tuple[int, int]] = []
    for idx, raw_uid in enumerate(user_ids or ()):
        uid = int(raw_uid) & 0xFFFF
        if uid == 0 or uid == 0xFFFF:
            continue
        members.append((idx & 0xFF, uid))
        if len(members) >= 8:
            break
    if not members:
        raise ValueError("PARTY_INFO type0 requires at least one non-empty uid")

    extras: list[tuple[int, int, int]] = []
    if member_extras is None:
        extras = [(0, 0, 0)] * len(members)
    else:
        for i in range(len(members)):
            if i < len(member_extras):
                a, b, c = member_extras[i]
                extras.append((int(a) & 0xFF, int(b) & 0xFF, int(c) & 0xFF))
            else:
                extras.append((0, 0, 0))

    tail_bytes = [0, 0, 0, 0]
    if tail is not None:
        vals = [int(x) & 0xFF for x in list(tail)[:4]]
        tail_bytes = (vals + [0, 0, 0, 0])[:4]

    body = bytearray()
    body += struct.pack("<H", 1)  # blockCount
    body += struct.pack("<H", int(top_field) & 0xFFFF)
    body += struct.pack("<H", int(party_id) & 0xFFFF)
    body += bytes([
        int(party_index) & 0xFF,
        int(unk) & 0xFF,
        0,  # type0
    ])
    body += bytes([
        int(name_flag) & 0xFF,
        int(title_present) & 0xFF,
    ])
    body += struct.pack("<I", 0)
    body += bytes([
        int(is_return) & 0xFF,
        int(is_event) & 0xFF,
        int(user_max) & 0xFF,
    ])
    body += struct.pack("<I", int(info_field) & 0xFFFFFFFF)
    body += bytes([int(difficulty) & 0xFF])
    body += struct.pack("<H", int(dungeon_id) & 0xFFFF)
    body += bytes([int(more) & 0xFF, int(aux) & 0xFF])
    body += struct.pack("<H", int(aux2) & 0xFFFF)

    body += bytes([len(members) & 0xFF])
    for (slot, uid), (e0, e1, e2) in zip(members, extras):
        body += bytes([slot & 0xFF])
        body += struct.pack("<H", uid & 0xFFFF)
        body += bytes([e0 & 0xFF, e1 & 0xFF, e2 & 0xFF])
    body += bytes(tail_bytes)
    body += bytes([int(has_extra) & 0xFF])
    return bytes(body)


def build_current_namespace_party(
    *, party_id: int, members: Sequence[tuple[int, int, tuple[int, int, int]]],
    user_max: int, dungeon_id: int, difficulty: int, tail: Sequence[int], info_field: int = 0,
    aux: int = 0, more: int = 0, aux2: int = 0,
) -> bytes:
    """Strict type0 for confirmed local namespace 0, using the default name.

    Native 0165C310 reads a different, longer member layout when namespace
    differs from the current CHANNELINFO context. Do not use this codec for
    cross-namespace parties. Three per-member bytes and four tail bytes are
    explicit opaque producer inputs; their complete business enums remain
    unproved. Slots are supplied, not renumbered after a member leaves.
    """
    def integer(value, lower, upper, name):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError('invalid native party ' + name)
        return value
    integer(party_id, 1, 65534, 'id')
    integer(user_max, 1, 8, 'capacity')
    integer(dungeon_id, 0, 65535, 'dungeon')
    integer(difficulty, 0, 255, 'difficulty')
    integer(info_field, 0, 0xffffffff, 'info field')
    integer(aux, 0, 255, 'auxiliary byte')
    integer(more, 0, 255, 'more byte')
    integer(aux2, 0, 65535, 'auxiliary word')
    rows, suffix = tuple(members), tuple(tail)
    if not 1 <= len(rows) <= user_max or len(suffix) != 4:
        raise ValueError('invalid native party member count or tail')
    slots, uids, extras = [], [], []
    for row in rows:
        if len(row) != 3:
            raise ValueError('invalid native party member shape')
        slot, uid, flags = row
        integer(slot, 0, 7, 'slot')
        integer(uid, 1, 65534, 'UID')
        flags = tuple(flags)
        if len(flags) != 3:
            raise ValueError('native party requires all three member bytes')
        for flag in flags:
            integer(flag, 0, 255, 'member byte')
        slots.append(slot)
        uids.append(uid)
        extras.append(flags)
    if len(set(slots)) != len(slots) or len(set(uids)) != len(uids):
        raise ValueError('ambiguous native party slot or UID')
    for flag in suffix:
        integer(flag, 0, 255, 'tail byte')
    body = bytearray(build_party_info_type0(user_ids=uids, party_id=party_id,
        party_index=0, user_max=user_max, dungeon_id=dungeon_id,
        difficulty=difficulty, member_extras=extras, tail=suffix, info_field=info_field, aux=aux, more=more, aux2=aux2))
    # The default name includes its four-byte length: first row at byte30.
    for index, slot in enumerate(slots):
        body[30 + index * 6] = slot
    return bytes(body)


def build_current_namespace_party_clear(*, party_id: int) -> bytes:
    """0165C310 type3 clears all eight UID slots; it has no info/roster tail."""
    if type(party_id) is not int or not 1 <= party_id <= 65534:
        raise ValueError('invalid native party id')
    return struct.pack('<HHHBBB', 1, 0, party_id, 0, 0, 3)


def build_current_namespace_party_slots(**kwargs) -> bytes:
    """Publish every slot, including the native 0xFFFF empty-slot sentinel.

    0165C310 indexes its UID and second member byte by explicit slot, but
    stores the third byte by row ordinal before 02765750 consumes slot0..7.
    Dense slot rows preserve that association after leaves/group changes.
    The caller still supplies only actual members; empty rows carry zero flags.
    """
    rows = tuple(kwargs['members'])
    kwargs = dict(kwargs, members=rows)
    body = build_current_namespace_party(**kwargs)
    capacity = kwargs['user_max']
    if any(slot >= capacity for slot, _uid, _flags in rows):
        raise ValueError('native party slot exceeds its actual capacity')
    by_slot = {slot: (uid, tuple(flags)) for slot, uid, flags in rows}
    result = bytearray(body[:29])
    result.append(capacity)
    for slot in range(capacity):
        uid, flags = by_slot.get(slot, (65535, (0, 0, 0)))
        result += struct.pack('<BHBBB', slot, uid, *flags)
    result += body[-5:]
    return bytes(result)


def build_party_info_type0_solo(
    user_id: int,
    *,
    dungeon_id: int = 0,
    difficulty: int = 0,
    party_id: int = 1,
    user_max: int = 4,
) -> bytes:
    """Solo type0 roster: slot0 uid = character key (cid & 0xFFFF)."""
    return build_party_info_type0(
        user_ids=[int(user_id) & 0xFFFF],
        party_id=party_id,
        dungeon_id=int(dungeon_id) & 0xFFFF,
        difficulty=int(difficulty) & 0xFF,
        user_max=int(user_max) & 0xFF,
        # 0165C310 -> party+62. Mode 0/1 makes 02BDB0EF reject ordinary
        # dungeon entries; 02BC4C00 displays DSTR48612 before sending C0010.
        aux=2,
    )


def parse_party_info_type0_header(body: bytes) -> dict:
    """Light parser for type0's verified default-name variant."""
    data = bytes(body or b"")
    if len(data) < 32:
        raise ValueError("PARTY_INFO body too short: %d" % len(data))
    off = 0
    block_count = int.from_bytes(data[off:off + 2], "little"); off += 2
    top_field = int.from_bytes(data[off:off + 2], "little"); off += 2
    party_id = int.from_bytes(data[off:off + 2], "little"); off += 2
    party_index = data[off]; off += 1
    unk = data[off]; off += 1
    ptype = data[off]; off += 1
    name_flag = data[off]; off += 1
    title_present = data[off]; off += 1
    if name_flag != 0 or title_present != 0:
        raise ValueError("unsupported native party name variant")
    name_length = int.from_bytes(data[off:off + 4], "little"); off += 4
    if name_length >= 64 or off + name_length > len(data):
        raise ValueError("invalid native party name length")
    party_name = data[off:off + name_length]; off += name_length
    is_return = data[off]; off += 1
    is_event = data[off]; off += 1
    user_max = data[off]; off += 1
    info_field = int.from_bytes(data[off:off + 4], "little"); off += 4
    difficulty = data[off]; off += 1
    dungeon_id = int.from_bytes(data[off:off + 2], "little"); off += 2
    more = data[off]; off += 1
    aux = data[off]; off += 1
    aux2 = int.from_bytes(data[off:off + 2], "little"); off += 2
    member_count = data[off]; off += 1
    members = []
    for _ in range(member_count):
        if off + 6 > len(data):
            raise ValueError("member row truncated")
        slot = data[off]; off += 1
        uid = int.from_bytes(data[off:off + 2], "little"); off += 2
        extras = (data[off], data[off + 1], data[off + 2]); off += 3
        members.append({"slot": slot, "uid": uid, "extras": extras})
    if off + 5 > len(data):
        raise ValueError("tail/hasExtra truncated")
    tail = list(data[off:off + 4]); off += 4
    has_extra = data[off]; off += 1
    return {
        "block_count": block_count,
        "top_field": top_field,
        "party_id": party_id,
        "party_index": party_index,
        "unk": unk,
        "type": ptype,
        "name_flag": name_flag,
        "title_present": title_present,
        "party_name": party_name,
        "is_return": is_return,
        "user_max": user_max,
        "is_event": is_event,
        "info_field": info_field,
        "difficulty": difficulty,
        "dungeon_id": dungeon_id,
        "more": more,
        "aux": aux,
        "aux2": aux2,
        "member_count": member_count,
        "members": members,
        "tail": tail,
        "has_extra": has_extra,
        "parsed_len": off,
        "body_len": len(data),
    }
