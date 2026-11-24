"""Owned native C000C/C000D for one actual raid member.

No peer endpoint/profile is fabricated. Native S0009 excludes self from peer
connection setup. Multi-member preparation retains NativeParty's full gate.
"""
from dataclasses import dataclass

from game.chain_loader import load_chain
from .runtime import RaidError


@dataclass
class _Party:
    key: tuple
    party_id: int
    request: bytes
    packets: tuple
    published: bool = False


class NativeSoloParty:
    def __init__(self, service):
        self.service = service
        self._parties = {}
        self._auto_prepare_paused = {}
        self._sequence = 0

    def _key(self, entry, session):
        cid, account, _record = self.service._actor(session)
        epoch = self.service.require_session(entry, session, cid, account)
        if len(entry.domain.members) != 1 or entry.domain.members[0].character_id != cid:
            raise RaidError('complete multiplayer party producer required')
        self.service.native_lobby.current_body(session)
        return (entry.domain, cid, account, epoch, entry.domain.member(cid).group)

    @staticmethod
    def _request(body):
        if not isinstance(body, bytes) or len(body) < 18:
            raise RaidError('truncated native create-party request')
        length = int.from_bytes(body[2:6], 'little')
        logical = 18 + length
        if length >= 256 or len(body) not in (logical, (logical + 15) // 16 * 16):
            raise RaidError('invalid native create-party name length')
        body = body[:logical]
        suffix = b'\x04\xff\xff\xff\xff\x05\x00\x00\x00\x02\x00\x00'
        if body[:2] != b'\x00\x00' or b'\x00' in body[6:6 + length] or body[6 + length:] != suffix:
            raise RaidError('unsupported native raid create-party variant')
        return body

    def create(self, session, body):
        request = self._request(body)
        with self.service._lock:
            cid, _account, _record = self.service._actor(session)
            entry = self.service.current(cid)
            if (entry is None or entry.domain.battle is None or entry.domain.battle.status != 'active'
                    or getattr(session, 'in_dungeon_run', False)
                    or not getattr(session, 'enter_seria_settled', False)):
                raise RaidError('native create-party requires owned raid preparation')
            key = self._key(entry, session)
            previous = self._parties.get(id(session))
            if previous is not None and previous.key == key:
                if previous.request != request:
                    raise RaidError('native create-party replay changed its owned request')
                return list(previous.packets), 'owned native self-party replay'
            used = {party.party_id for party in self._parties.values()}
            ordinary = getattr(self.service, 'ordinary_party', None)
            if ordinary is not None:
                used.update(self.service._ordinary_party_id_history)
            used.update(value[1] for value in self.service.native_party._ids.values())
            for _ in range(65534):
                self._sequence = self._sequence % 65534 + 1
                if self._sequence not in used:
                    break
            else:
                raise RaidError('native self-party directory full')
            helper = load_chain('helper-party_info.py')
            # Actor +42E constructor value0; network ready reset0 is encoded
            # by e2=1 (native consumer stores e2==0). Host is actual self slot0.
            body = helper.build_current_namespace_party_slots(party_id=self._sequence,
                members=((0, cid, (0, 0, 1)),), user_max=4, dungeon_id=0,
                difficulty=5, info_field=0xffffffff, aux=2, tail=(0, 0, 0, 0))
            packets = (self.service._packet(0, 9, body), self.service._packet(1, 12, b'\x01'))
            if getattr(self.service, 'ordinary_party', None) is not None:
                self.service._ordinary_party_id_history.add(self._sequence)
            self._parties[id(session)] = _Party(key, self._sequence, request, packets)
            self._auto_prepare_paused.pop(id(session), None)
            return list(packets), 'actual owned native self-party created'

    def leave(self, session, body):
        if not isinstance(body, bytes) or body:
            raise RaidError('native leave-party requires an empty request')
        with self.service._lock:
            cid, _account, _record = self.service._actor(session)
            entry = self.service.current(cid)
            if (entry is None or getattr(session, 'in_dungeon_run', False)
                    or not getattr(session, 'enter_seria_settled', False)):
                raise RaidError('native leave-party requires owned settled raid member')
            key = self._key(entry, session)
            party = self._parties.get(id(session))
            if party is not None and party.key != key:
                raise RaidError('native leave-party owner changed')
            packets = []
            if party is not None:
                clear = load_chain('helper-party_info.py').build_current_namespace_party_clear(party_id=party.party_id)
                packets.append(self.service._packet(0, 9, clear))
            packets.append(self.service._packet(1, 13, b'\x01'))
            self._parties.pop(id(session), None)
            self._auto_prepare_paused[id(session)] = key
            self.service.forget_lobby_refresh(session)
            return packets, 'actual owned native self-party left; raid membership retained'

    def display_group(self, entry, character_id, fallback):
        if len(entry.domain.members) == 1 and entry.domain.members[0].character_id == character_id:
            if not self.auto_prepare_allowed(entry):
                return 0
        return fallback

    def auto_prepare_allowed(self, entry):
        with self.service._lock:
            if len(entry.domain.members) != 1:
                return True
            cid = entry.domain.members[0].character_id
            frozen = [row for row in self.service._raid_sessions.values() if row[1] is entry and row[2] == cid]
            if len(frozen) != 1:
                return True
            session = frozen[0][0]
            paused = self._auto_prepare_paused.get(id(session))
            try:
                return paused is None or paused != self._key(entry, session)
            except RaidError:
                return True

    def note_send(self, session, packet):
        with self.service._lock:
            party = self._parties.get(id(session))
            if party is None or packet is not party.packets[0]:
                return
            entry = self.service.current(party.key[1])
            if entry is not None and self._key(entry, session) == party.key:
                party.published = True

    def ready(self, entry):
        with self.service._lock:
            if len(entry.domain.members) != 1:
                try:
                    self.service.native_party.require_prepared(entry)
                    return True
                except RaidError:
                    return False
            cid = entry.domain.members[0].character_id
            frozen = [row for row in self.service._raid_sessions.values() if row[1] is entry and row[2] == cid]
            if len(frozen) != 1:
                return False
            session = frozen[0][0]
            party = self._parties.get(id(session))
            try:
                return bool(party and party.published and party.key == self._key(entry, session))
            except RaidError:
                return False

    def leave_packets(self, session):
        with self.service._lock:
            self._auto_prepare_paused.pop(id(session), None)
            party = self._parties.pop(id(session), None)
            if party is None:
                return []
            body = load_chain('helper-party_info.py').build_current_namespace_party_clear(party_id=party.party_id)
            return [self.service._packet(0, 9, body)]

    def forget_session(self, session):
        self._parties.pop(id(session), None)
        self._auto_prepare_paused.pop(id(session), None)
