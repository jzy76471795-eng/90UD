"""N027B directory from published ordinary parties, not raid assignments.

Frozen handler 016491A0 reads B,(H,B,H*,I,i,B,I*)*. The first DWORD is
used party coins; 0100F4A0 subtracts it from the actual dungeon coin cap.
Dungeon identity is resolved from the participation directory by member UID.
Its final vector holds the native party host’s ordered monster HP percentages.
"""
from dataclasses import dataclass
import struct

from .runtime import RaidError

C_RAID_MONSTER_HP = 0x02b8
N_RAID_PARTIES = 0x027b


@dataclass(frozen=True)
class BoardParty:
    group: int
    members: tuple
    sessions: tuple
    host_id: int = 0


def party_snapshot(service, entry):
    with service._lock:
        rows = []
        ordinary = getattr(service, 'ordinary_party', None)
        if ordinary is not None and ordinary.uses(entry):
            ordinary._prune()
            for party in sorted(ordinary._parties.values(), key=lambda p: (p.group, p.party_id)):
                if party.entry is not entry or party.group <= 0:
                    continue
                try:
                    actors = ordinary._actors(party)
                    if not all((sent := service.native_party._sent.get(actor)) is not None
                               and sent.key == ordinary.key(party) and sent.actors == actors
                               for actor in actors):
                        continue
                except RaidError:
                    continue
                rows.append(BoardParty(party.group,
                    tuple(seat.character_id for seat in party.members),
                    tuple(seat.session for seat in party.members), party.host_character_id))
        else:
            solo = getattr(service, 'native_solo_party', None)
            if solo is not None and len(entry.domain.members) == 1 and solo.ready(entry):
                cid = entry.domain.members[0].character_id
                owned = [r for r in service._raid_sessions.values() if r[1] is entry and r[2] == cid]
                if len(owned) == 1:
                    session = owned[0][0]
                    party = solo._parties[id(session)]
                    rows.append(BoardParty(party.key[4], (cid,), (session,), cid))
        return tuple(rows)


def encode_parties(rows, bridge, raid, *, revive_uses=None):
    rows = tuple(rows)
    if len(rows) > 10:
        raise ValueError('native raid board supports at most ten ordinary parties')
    body, seen_parties, seen_members = bytearray([len(rows)]), set(), set()
    for row in rows:
        if (type(row.group) is not int or not 1 <= row.group <= 10
                or row.group in seen_parties or not 1 <= len(row.members) <= 4):
            raise ValueError('invalid actual ordinary party')
        seen_parties.add(row.group)
        body += struct.pack('<HB', row.group, len(row.members))
        for cid in row.members:
            if type(cid) is not int or not 1 <= cid < 65535 or cid in seen_members:
                raise ValueError('invalid or duplicated selected native member')
            seen_members.add(cid)
            body += struct.pack('<H', cid)
        dungeon, current_map = bridge.board_location(raid, row.sessions)
        hp = ()
        if dungeon and row.host_id in row.members:
            host = row.sessions[row.members.index(row.host_id)]
            hp = bridge.board_hp(raid, host, dungeon, current_map)
        used = 0
        if dungeon and revive_uses:
            for session in row.sessions:
                binding = bridge.current(session)
                ticket = binding.ticket if binding is not None else None
                if (ticket is not None and ticket.dungeon_id == dungeon
                        and ticket.run_id in raid._accepted_runs
                        and ticket.run_id not in raid._retired_runs
                        and ticket.instance_id == raid.battle.instance_id
                        and ticket.phase == raid.battle.phase):
                    used = revive_uses.get(ticket.run_id, 0)
                    break
        if type(used) is not int or not 0 <= used <= 255:
            raise ValueError('invalid actual shared party revive count')
        body += struct.pack('<IiB', used, current_map, len(hp))
        body += struct.pack('<' + 'I' * len(hp), *hp)
    return bytes(body)
