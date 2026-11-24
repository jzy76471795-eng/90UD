"""Authenticated ordinary-channel party creation/edit/leave, outside raids."""
from dataclasses import dataclass, field
from threading import RLock
import struct
import time

from game.chain_loader import load_chain
from game.raid.runtime import RaidError
from game.normal_party_udp import UdpUnavailable, UdpWaits

_LOCK = RLock()


@dataclass
class Party:
    owner: tuple
    party_id: int
    request: bytes
    members: dict = field(default_factory=dict)
    revision: int = 0


@dataclass
class Invitation:
    sender: tuple
    recipient: tuple
    leader: tuple
    joiner: tuple
    party: object
    mode: int
    expires: float
    delivered: bool = False


def parse_request(body):
    # Frozen 00F35BB7/01E82B71 writers: BB,[dstring],BIBHBBH.
    raw = bytes(body)
    if len(raw) < 14 or raw[0] not in (0, 1) or raw[1] > 6:
        raise ValueError('invalid ordinary party request')
    offset, name = 2, b''
    if raw[1] == 0:
        if len(raw) < 6:
            raise ValueError('truncated ordinary party name')
        length = int.from_bytes(raw[2:6], 'little')
        if length > 63 or len(raw) < 18 + length:
            raise ValueError('invalid ordinary party name length')
        name, offset = raw[6:6 + length], 6 + length
        if b'\0' in name:
            raise ValueError('ordinary party name contains NUL')
    logical = offset + 12
    if len(raw) not in (logical, (logical + 15) // 16 * 16):
        raise ValueError('invalid ordinary party request width')
    maximum, info, difficulty, dungeon, more, aux, aux2 = struct.unpack_from('<BIBHBBH', raw, offset)
    # 00F35C4F sends the UI's three mode choices (0..2). 01E82C2E
    # sends mode 0 when editing. Mode 2 is not the only ordinary-party mode.
    if not 1 <= maximum <= 4 or difficulty > 5 or more != 0 or aux not in (0, 1, 2):
        raise ValueError('unsupported ordinary party mode or capacity')
    return raw[:logical], name, maximum, info, difficulty, dungeon, aux, aux2


class NormalParties:
    def __init__(self, server):
        self.server, self._parties, self._sequence = server, {}, 0
        self._lock = RLock()
        self._invites, self._queues = {}, {}
        self.clock = time.monotonic
        from game.normal_party_select import PartySelectors
        self.selectors = PartySelectors(self)
        from game.normal_party_return import PartyReturns
        self.returns = PartyReturns(self)
        from game.normal_party_run import NormalPartyRuns
        self.runs = NormalPartyRuns(self)
        self.udp = UdpWaits(self)
        from game.normal_party_guide import PartyGuides
        self.guides = PartyGuides(self)
        from game.normal_party_cards import PartyCards
        self.cards = PartyCards(self)

    def _owner(self, session, *, town):
        directory = getattr(self.server, 'native_game_connections', None)
        if directory is None:
            raise RaidError('authenticated ordinary party connection unavailable')
        owner = directory.chat_selection(session, sender=True)
        if type(owner[4]) is not int or not 1 <= owner[4] <= 65534:
            raise RaidError('ordinary party requires an ordinary channel selected actor')
        if owner[3] in (23, 32):
            from game.raid.ownership import current_raid_entry
            if current_raid_entry(self.server, owner[4]) is not None:
                raise RaidError('owned raid membership uses its raid party directory')
        if town and (getattr(session, 'in_dungeon_run', False)
                     or getattr(session, 'in_dungeon_select', False)
                     or not getattr(session, 'enter_seria_settled', False)):
            raise RaidError('ordinary party changes require a settled town actor')
        if town and not any(int(r.character_id) == owner[4] for r in self.server._repo().list_by_account(owner[2])):
            raise RaidError('ordinary party selected actor is not account-owned')
        return owner

    def _live(self, selection):
        try:
            return self.server.native_game_connections.chat_selection(selection[0].session) == selection
        except RaidError:
            return False

    def _queue(self, target, flag, opcode, body, *, invite=None, party=None, revision=None):
        queue = self._queues.setdefault(target[0], [])
        if len(queue) >= 128:
            raise RaidError('ordinary party delivery queue full')
        queue.append((target, flag, opcode, body, invite, party, revision))

    def _roster(self, party):
        _, name, maximum, info, difficulty, dungeon, aux, aux2 = parse_request(party.request)
        # 0165C310/01E83EA0: tail[2] is the actual host's native slot.
        host_slot = next(slot for slot, member in party.members.items() if member == party.owner)
        body = load_chain('helper-party_info.py').build_current_namespace_party_slots(
            party_id=party.party_id, members=tuple((slot,s[4],(0,0,1)) for slot,s in sorted(party.members.items())),
            user_max=maximum, dungeon_id=dungeon, difficulty=difficulty,
            info_field=info, aux=aux, aux2=aux2, tail=(0,0,host_slot,0))
        name_selector = party.request[1]
        if name_selector:
            # 0165C310 (current EXE 14b85f...): a nonzero selector skips
            # the dstring read and selects the native party title template.
            return body[:10] + bytes((name_selector,)) + body[15:]
        return body[:11] + struct.pack('<I',len(name)) + name + body[15:]

    def _notify(self, party, omit=None, *, peers=None, refresh_peers=True):
        party.revision += 1
        body = self._roster(party)
        if refresh_peers and peers is None and len(party.members) > 1:
            try:
                peers = self._peer_addresses(party.members.values())
            except RaidError:
                # Departures must still remove unavailable members. No new
                # endpoint is invented to make an expired transport look live.
                pass
        for target in party.members.values():
            if target != omit:
                if peers is not None:
                    self._queue(target,0,11,peers,party=party,revision=party.revision)
                self._queue(target,0,9,body,party=party,revision=party.revision)
        return body

    def _peer_addresses(self, selections):
        selections = tuple(selections)
        from game.raid.native_network_protocol import PeerAddress, encode_peer_addresses
        from game.character.native_profile_projection import userinfo0_projection
        from game.town_social import service_for
        matches = self._udp_matches(selections)
        peers = []
        for selection, match in zip(selections, matches):
            report = match.report
            key = self.server.native_game_connections.key_for_selection(selection)
            projection = userinfo0_projection(service_for(self.server).build_body(selection))
            if projection is None or projection.character_id != selection[4] or not 0 <= projection.weakness <= 100:
                raise RaidError('ordinary party public actor profile unavailable')
            observed_kind = 3 if getattr(match,'continuity_port',None) is not None else report.nat_kind
            peers.append(PeerAddress(selection[4],report.local_ipv4_u32,report.public_ipv4_u32,
                report.public_port,key,observed_kind,report.mtu,projection.weakness))
        return encode_peer_addresses(peers)

    def _udp_matches(self, selections):
        selections = tuple(selections)
        probe = getattr(self.server, 'native_udp_probe_service', None)
        if probe is None:
            raise RaidError('ordinary party awaits native UDP initialization')
        matches = []
        for selection in selections:
            session = selection[0].session
            snapshot = getattr(probe, 'current_claim_and_match', None)
            if callable(snapshot):
                match, claim = snapshot(session)
            else:
                match = probe.current_match(session)
                claim = getattr(session, '_native_udp_address_claim', None)
            if ((match is not None and match.account_id != selection[2])
                    or (claim is not None and claim.account_id != selection[2])
                    or (match is not None and claim is not None and claim.report is not match.report)):
                raise RaidError('ordinary party UDP claim ownership mismatch')
            if (match is None or claim is None or match.account_id != selection[2]
                    or claim.account_id != selection[2] or claim.report is not match.report):
                reason = 'probe_unavailable' if match is None else 'claim_missing_or_expired'
                raise UdpUnavailable(selections, 'ordinary party peer has no current observed UDP address cid=%s reason=%s' % (selection[4],reason))
            report = match.report
            if report.nat_kind not in (1,2,3,4,5,6) or report.mtu not in (512,1024,1280,1472) or not report.public_port:
                raise RaidError('ordinary party native network report is incomplete')
            matches.append(match)
        return tuple(matches)

    def _clear(self, selection, party):
        body = load_chain('helper-party_info.py').build_current_namespace_party_clear(party_id=party.party_id)
        self._queue(selection,0,9,body)

    def _cancel_invite(self, invite, value=1):
        if self._invites.get(invite.recipient[0]) is not invite:
            return
        self._invites.pop(invite.recipient[0], None)
        if self._live(invite.sender) and len(self._queues.get(invite.sender[0], ())) < 128:
            # Frozen016757F0 consumes H,B,I. Mode0's decline branch needs
            # no extra fields and completes the sender's native request UI.
            self._queue(invite.sender,0,8,struct.pack('<HBI',invite.recipient[4],invite.mode,value))

    def _remove(self, selection, *, refresh_peers=True):
        self.udp.forget(selection)
        party = self._parties.get(id(selection[0].session))
        for key, invite in tuple(self._invites.items()):
            if (selection in (invite.sender, invite.recipient, invite.leader, invite.joiner)
                    or (party is not None and party.owner == selection and invite.party is party)):
                self._cancel_invite(invite)
        if party is None or selection not in party.members.values():
            return
        self.runs.detach(selection[0].session)
        self.cards.forget(selection[0].session)
        self.returns.forget(selection[0].session)
        self.selectors.abort(party)
        self._parties.pop(id(selection[0].session), None)
        party.members = {slot:s for slot,s in party.members.items() if s != selection}
        self._clear(selection,party)
        if party.members:
            changed_host = party.owner == selection
            if party.owner == selection:
                slot = min(party.members)
                # Native transport slots belong to the surviving actors.
                # Host succession must not move one actor into another slot.
                party.owner = party.members[slot]
            self._notify(party, refresh_peers=refresh_peers)
            if changed_host:
                self._notify_host(party)

    def _notify_host(self, party):
        # Native 016528F0 consumes one slot and updates the live dungeon host.
        # PARTY_INFO can stage a roster until town; host ownership cannot wait.
        slot = next(k for k,v in party.members.items() if v == party.owner)
        for member in party.members.values():
            self._queue(member,0,0x1a,bytes([slot]),party=party,revision=party.revision)

    def _prune(self):
        self.udp.prune()
        for party in tuple({id(p):p for p in self._parties.values()}.values()):
            for selection in tuple(party.members.values()):
                if not self._live(selection):
                    self._remove(selection)
        for key, invite in tuple(self._invites.items()):
            if self.clock() >= invite.expires or not self._live(invite.sender) or not self._live(invite.recipient):
                self._cancel_invite(invite)

    def _create(self, owner, raw=None):
        if raw is None:
            raw = b'\x00\x00' + struct.pack('<I',0) + struct.pack('<BIBHBBH',4,0,0,0,0,2,0)
        used = {p.party_id for p in self._parties.values()}
        for _ in range(65534):
            self._sequence = self._sequence % 65534 + 1
            if self._sequence not in used:
                break
        else:
            raise RaidError('ordinary party directory is full')
        party = Party(owner,self._sequence,raw,{0:owner})
        self._parties[id(owner[0].session)] = party
        return party

    def current(self, session):
        with self._lock:
            self._prune()
            party = self._parties.get(id(session))
            return party

    def _peer(self, owner, cid):
        directory = self.server.native_game_connections
        matches = [s for s in directory.chat_selections() if s[3] == owner[3] and s[4] == cid]
        if len(matches) != 1 or matches[0] == owner:
            raise RaidError('party peer requires one different current same-channel actor')
        target = matches[0]
        peer = target[0].session
        if owner[3] in (23, 32):
            from game.raid.ownership import current_raid_entry
            if current_raid_entry(self.server, target[4]) is not None:
                raise RaidError('ordinary party peer already belongs to an owned raid')
        if (getattr(peer, 'in_dungeon_run', False) or getattr(peer, 'in_dungeon_select', False)
                or not getattr(peer, 'enter_seria_settled', False)):
            raise RaidError('ordinary party peer must be settled in town')
        from game.town_social import service_for, scope
        social = service_for(self.server)
        if (scope(owner[0].session) is None or scope(owner[0].session) != scope(target[0].session)
                or not social.visible(owner,target) or not social.visible(target,owner)):
            raise RaidError('party peer is not mutually visible in this public town area')
        return target

    @staticmethod
    def _fixed(body, size):
        raw = bytes(body)
        if len(raw) != size and not (len(raw) in {(size+7)//8*8,(size+15)//16*16} and not any(raw[size:])):
            raise ValueError('invalid ordinary peer request length')
        if len(raw) < size:
            raise ValueError('truncated ordinary peer request')
        return raw[:size]

    def _invite(self, owner, body):
        if self.udp.repeat(owner, 10, body):
            return [], 'ordinary party UDP refresh already pending'
        try:
            return self._invite_ready(owner, body)
        except UdpUnavailable as exc:
            return self.udp.defer(owner, 10, body, exc)

    def _invite_ready(self, owner, body):
        # Pinned US writer01E83F20: H B I H I. N01675300 mode0/12:
        # H B I H H H I. Other modes are separate trade/duel protocols.
        cid, mode, value, aux, value2 = struct.unpack('<HBIHI',self._fixed(body,13))
        if mode not in (0,12):
            raise ValueError('unsupported ordinary party invitation mode')
        recipient = self._peer(owner,cid)
        own_party, target_party = self.current(owner[0].session), self.current(recipient[0].session)
        if own_party is not None and target_party is not None:
            if own_party.owner != owner or target_party.owner != recipient:
                raise RaidError('ordinary party invitation requires its current leaders')
            # Native town/dungeon UI can leave each actor in a singleton.
            # _accept already replaces a singleton only after revalidating
            # both owners and transports. Keep membership untouched here.
            if len(target_party.members) == 1:
                target_party = None
            elif len(own_party.members) == 1:
                own_party = None
            else:
                raise RaidError('both actors already belong to multi-member parties')
        if target_party is not None:
            if target_party.owner != recipient:
                raise RaidError('request must address the current party leader')
            leader,joiner,party = recipient,owner,target_party
        else:
            if own_party is not None and own_party.owner != owner:
                raise RaidError('only the party leader can invite')
            leader,joiner,party = owner,recipient,own_party
        if party is not None and len(party.members) >= parse_request(party.request)[2]:
            raise RaidError('ordinary party is full')
        previous = self._invites.get(recipient[0])
        if previous is not None or self.udp.reserved(recipient, owner):
            raise RaidError('peer already has a pending invitation')
        members = tuple(party.members.values()) if party is not None else (leader,)
        self._peer_addresses(members + (joiner,))
        invite = Invitation(owner,recipient,leader,joiner,party,mode,self.clock()+30)
        self._invites[recipient[0]] = invite
        self._queue(recipient,0,7,struct.pack('<HBIHHHI',owner[4],mode,value,aux,0,0,value2),invite=invite)
        return [self.server._codec_response(1,10,b'\x01')], 'ordinary party invitation queued'

    def _accept(self, owner, body):
        if self.udp.repeat(owner, 11, body):
            return [], 'ordinary party UDP refresh already pending'
        cid,mode,value = struct.unpack('<HBI',self._fixed(body,7))
        invite = self._invites.get(owner[0])
        if (invite is None or invite.recipient != owner or invite.sender[4] != cid
                or invite.mode != mode or not invite.delivered or self.clock() >= invite.expires):
            raise RaidError('ordinary party response has no delivered current invitation')
        try:
            return self._accept_current(owner,cid,mode,value,invite)
        except UdpUnavailable as exc:
            return self.udp.defer(owner, 11, body, exc, invite)
        except (RaidError,ValueError):
            self._cancel_invite(invite)
            raise

    def _accept_current(self, owner, cid, mode, value, invite):
        self._peer(owner,cid)
        ack = self.server._codec_response(1,11,b'\x01'+struct.pack('<HB',cid,mode))
        if value != 0:
            self._cancel_invite(invite,value)
            return [ack], 'ordinary party invitation declined'
        party = self.current(invite.leader[0].session)
        if party is not invite.party or (party is not None and party.owner != invite.leader):
            raise RaidError('invited party or leader changed')
        joiner_party = self.current(invite.joiner[0].session)
        if party is not None and len(party.members) >= parse_request(party.request)[2]:
            raise RaidError('ordinary party is full')
        if joiner_party is not None:
            # Native accept UI can create an empty local singleton first.
            if len(joiner_party.members) != 1 or joiner_party.owner != invite.joiner:
                raise RaidError('invited actor already belongs to another party')
        # Validate both real transports before changing either membership.
        members = tuple(party.members.values()) if party is not None else (invite.leader,)
        peers = self._peer_addresses(members + (invite.joiner,))
        for member in members + (invite.joiner,):
            if len(self._queues.get(member[0], ())) > 124:
                raise RaidError('ordinary party delivery queue full')
        self._invites.pop(owner[0],None)
        if joiner_party is not None:
            self._remove(invite.joiner)
        created = party is None
        if created:
            party = self._create(invite.leader)
            # Concurrent invitations sent before this first acceptance refer
            # to the same still-unmaterialized party. Bind them only here,
            # while this acceptance creates it under the service lock. Never
            # rebind a stale invitation to an arbitrary later party.
            for pending in self._invites.values():
                if pending.party is None and pending.leader == invite.leader:
                    pending.party = party
        maximum = parse_request(party.request)[2]
        free = next((slot for slot in range(maximum) if slot not in party.members),None)
        if free is None:
            raise RaidError('ordinary party is full')
        party.members[free] = invite.joiner
        self._parties[id(invite.joiner[0].session)] = party
        self._notify(party, peers=peers)
        self.udp.joined(invite.leader, party, created=created)
        return [ack], 'ordinary party invitation accepted'

    def _leave_party(self, body, session):
        if body:
            raise ValueError('ordinary leave party requires an empty request')
        with self._lock:
            owner = self._owner(session,town=False)
            previous = self.current(session)
            running = bool(getattr(session,'in_dungeon_run',False))
            selecting = bool(getattr(session,'in_dungeon_select',False))
            if selecting and previous is not None:
                self.selectors.abort(previous)
        town_packets = []
        if running:
            result = self.returns.leave(132,b'',session,explicit_party_leave=True)
            if result is None or getattr(session,'in_dungeon_run',False) or getattr(session,'in_dungeon_select',False):
                raise RaidError('ordinary party leave awaits native town restoration')
            return result[0]+[self.server._codec_response(1,13,b'\1')], 'ordinary party leave awaiting own town confirmation'
        if selecting:
            town_packets,_ = self.server._handle_back_2_village_0084(b'',session=session)
            if getattr(session,'in_dungeon_select',False):
                raise RaidError('ordinary party leave awaits confirmed town restoration')
            session._normal_party_selector = None
        with self._lock:
            if not self._live(owner) or self._parties.get(id(session)) is not previous:
                raise RaidError('ordinary party changed during its native leave')
            self._remove(owner)
            if previous is not None:
                self._queues[owner[0]] = [r for r in self._queues.get(owner[0],()) if r[2] != 9]
        helper = load_chain('helper-party_info.py')
        clear = ([self.server._codec_response(0,9,helper.build_current_namespace_party_clear(party_id=previous.party_id))]
                 if previous is not None else [])
        session.dungeon_party_preloaded = False
        session.dungeon_solo_party_owner = None
        return town_packets+clear+[self.server._codec_response(1,13,b'\1')], 'ordinary party left'

    def handle(self, opcode, body, session):
        if opcode == 13:
            return self._leave_party(body,session)
        with self._lock:
            owner = self._owner(session, town=opcode != 13)
            self._prune()
            if opcode == 10:
                return self._invite(owner,body)
            if opcode == 11:
                return self._accept(owner,body)
            previous = self.current(session)
            helper = load_chain('helper-party_info.py')
            if opcode == 0x79:
                # Current US writer 02A0B82B sends a single native member slot.
                slot = self._fixed(body,1)[0]
                if previous is None or previous.owner != owner or slot not in previous.members:
                    raise RaidError('host appointment requires current leader and member slot')
                if any(getattr(s[0].session,'in_dungeon_run',False)
                       or getattr(s[0].session,'in_dungeon_select',False)
                       or not self._live(s) for s in previous.members.values()):
                    raise RaidError('host appointment requires every member in town')
                for invite in tuple(self._invites.values()):
                    if invite.party is previous:
                        self._cancel_invite(invite)
                previous.owner = previous.members[slot]
                self._notify(previous,refresh_peers=False)
                self._notify_host(previous)
                return [self.server._codec_response(1,0x79,b'\1')], 'ordinary party host appointed'
            if opcode == 14:
                slot = self._fixed(body,1)[0]
                if previous is None or previous.owner != owner or slot == 0 or slot not in previous.members:
                    raise RaidError('party kick requires current leader and member slot')
                self._remove(previous.members[slot])
                return [self.server._codec_response(1,14,b'\x01')], 'ordinary party member removed'
            raw, name, maximum, info, difficulty, dungeon, aux, aux2 = parse_request(body)
            if previous is not None and previous.owner != owner:
                raise RaidError('ordinary party edit requires its current leader')
            if raw[0] == 1 and previous is None:
                raise RaidError('ordinary party edit requires its current owner')
            if previous is None:
                party = self._create(owner,raw)
            else:
                party = previous
                if max(party.members) >= maximum:
                    raise RaidError('party capacity would discard a current member')
                party.request = raw
            try:
                # 016725F0 initializes the selected actor's transport fields
                # too. A solo-created party must not wait for a second member
                # before receiving its already observed network information.
                peers = self._peer_addresses(party.members.values())
            except RaidError:
                if len(party.members) > 1:
                    raise
                peers = None
            roster = self._notify(party,omit=owner,peers=peers)
            if peers is None:
                # C0002 and the actual UDP observation can arrive after C000C.
                # pump revalidates ownership and resolves this placeholder;
                # it never sends an empty or invented address packet.
                self._queue(owner,0,11,b'',party=party,revision=party.revision)
                self._queue(owner,0,9,roster,party=party,revision=party.revision)
            session.dungeon_party_preloaded = False
            session.dungeon_solo_party_owner = None
            packets = [self.server._codec_response(0,11,peers)] if peers is not None else []
            return packets+[self.server._codec_response(0,9,roster),self.server._codec_response(1,12,b'\x01')], 'ordinary owned party created or updated'

    def pump(self, session, client):
        if self.server.account_id != getattr(session, 'account_id', None):
            return
        with self._lock:
            self._prune()
            try:
                owner = self._owner(session,town=False)
            except RaidError:
                return
            if owner[0].client is not client:
                raise RaidError('party delivery socket ownership mismatch')
            resumed = self.udp.resume(owner)
            rows = self._queues.pop(owner[0],())
        for packet in resumed:
            if not self._live(owner):
                return
            client.sendall(packet)
        blocked = set()
        for target,flag,opcode,body,invite,party,revision in rows:
            with self._lock:
                if target != owner or not self._live(owner):
                    continue
                if party is not None and (party.revision != revision or self._parties.get(id(session)) is not party):
                    continue
                if party is not None and (id(party),revision) in blocked:
                    continue
                members = tuple(party.members.values()) if party is not None else ()
                if invite is not None:
                    if self._invites.get(owner[0]) is not invite:
                        continue
                    try:
                        self._peer(owner,invite.sender[4])
                    except RaidError:
                        self._cancel_invite(invite)
                        continue
            if party is not None and opcode == 11:
                try:
                    body = self._peer_addresses(members)
                except RaidError:
                    with self._lock:
                        blocked.add((id(party),revision))
                        if party.revision == revision and self._parties.get(id(session)) is party:
                            self._queue(owner,0,11,body,party=party,revision=revision)
                            self._queue(owner,0,9,self._roster(party),party=party,revision=revision)
                    continue
            with self._lock:
                if (not self._live(owner) or (party is not None and
                        (party.revision != revision or self._parties.get(id(session)) is not party))):
                    continue
            client.sendall(self.server._codec_response(flag,opcode,body))
            if invite is not None:
                with self._lock:
                    if self._invites.get(owner[0]) is invite:
                        invite.delivered = True
        self.selectors.pump(owner, client)
        self.guides.pump(owner, client)
        self.returns.pump(owner, client)
        self.runs.pump(session, client)
        self.cards.pump(owner, client)

    def forget(self, session):
        with self._lock:
            for pending in tuple(self.udp.pending.values()):
                for selection in pending.selections:
                    if selection[0].session is session:
                        self.udp.forget(selection)
                        break
            self.runs.forget(session)
            self.cards.forget(session)
            self.returns.forget(session)
            party = self._parties.get(id(session))
            if party is not None:
                for s in tuple(party.members.values()):
                    if s[0].session is session:
                        self._remove(s)
            for key in tuple(self._queues):
                if key.session is session:
                    self._queues.pop(key,None)
            for key,invite in tuple(self._invites.items()):
                if invite.sender[0].session is session or invite.recipient[0].session is session:
                    self._cancel_invite(invite)
            self.selectors.forget(session)


def service_for(server):
    with _LOCK:
        service = getattr(server, 'normal_parties', None)
        if service is None:
            service = NormalParties(server)
            server.normal_parties = service
        return service


def current(server, session):
    service = getattr(server, 'normal_parties', None)
    return service.current(session) if service is not None else None


def forget(server, session):
    from game.player_exchange import forget as forget_exchange
    forget_exchange(server, session)
    service = getattr(server, 'normal_parties', None)
    if service is not None:
        service.forget(session)


def retire_for_raid(server, session):
    """Retire an owned lobby party before publishing a successful raid join.

    Called outside the raid lock, so ordinary->raid ownership checks cannot
    form a reverse lock order with the ordinary party delivery thread.
    """
    service = getattr(server, 'normal_parties', None)
    if service is None:
        return []
    with service._lock:
        previous = service._parties.get(id(session))
        if previous is None:
            return []
        owner = server.native_game_connections.chat_selection(session, sender=True)
        if owner not in previous.members.values():
            raise RaidError('raid join cannot retire another ordinary party owner')
        service._remove(owner)
        service._queues.pop(owner[0], None)
        session.dungeon_party_preloaded = False
        session.dungeon_solo_party_owner = None
        clear = load_chain('helper-party_info.py').build_current_namespace_party_clear(
            party_id=previous.party_id,
        )
        return [server._codec_response(0, 9, clear)]


def route(server, opcode, body, session):
    from game.player_exchange import route as route_exchange
    exchange_result = route_exchange(server, opcode, body, session)
    if exchange_result is not None:
        return exchange_result
    if opcode not in (10, 11, 12, 13, 14, 15, 16, 37, 39, 45, 42, 0x45, 0x47, 0x48, 121, 132):
        return None
    if getattr(session, 'selected_channel_id', None) in (23, 32):
        from game.raid.ownership import owns_raid_membership
        # Lobby party editing is allowed before joining an actual raid. Raid
        # combat/select/return traffic remains exclusively raid-owned.
        if owns_raid_membership(server, session) or opcode not in (10, 11, 12, 13, 14, 121):
            return None
    try:
        service = getattr(server, 'normal_parties', None)
        if opcode in (0x45, 0x47):
            return service.cards.route(opcode, body, session) if service is not None else None
        if opcode in (42, 132, 0x48) and service is not None:
            result = service.returns.leave(opcode, body, session)
            if result is not None:
                _log_route(server, session, opcode, 'ordinary mid-run return awaiting town confirmation')
                return result
        if opcode == 0x48:
            return None
        if opcode in (16, 37, 39, 45):
            result = service.runs.route(opcode, body, session) if service is not None else None
            if result is not None and opcode != 39:
                _log_route(server, session, opcode, result[1])
            return result
        if opcode in (15, 42, 132):
            service = getattr(server, 'normal_parties', None)
            if service is None or service.current(session) is None:
                return None
            result = service.selectors.route(opcode, body, session)
        else:
            service = service_for(server)
            result = service.handle(opcode, body, session)
        if result is not None:
            _log_route(server, session, opcode, result[1])
        return result
    except (RaidError, ValueError) as exc:
        # Frozen01610F80/01611700: both failed REQUEST/RESPONSE_PEER
        # consume the mode byte after the generic result/error prefix.
        suffix = bytes([body[2] if len(body) >= 3 else 0]) if opcode in (10,11) else (b'\0' if opcode == 16 else b'')
        note = 'ordinary party rejected: ' + str(exc)
        _log_route(server, session, opcode, note)
        return ([] if opcode in (15, 37, 39, 45, 42, 0x45, 0x47, 0x48, 132) else [server._codec_response(1, opcode, b'\x00\x13'+suffix)]), note


def _log_route(server, session, opcode, note):
    # These requests otherwise lose their rejection reason when packet tracing
    # is disabled. No payloads, names, endpoint claims or login keys are logged.
    logger = getattr(server, 'log', None)
    if callable(logger):
        logger('[Party] opcode=%04X channel=%s cid=%s %s' % (
            opcode, getattr(session, 'selected_channel_id', None),
            getattr(session, 'selected_character_id', None), note))


def town_confirmed(server, session, opcode):
    """Accepted native C0023/C0024 is a scene confirmation, not a send receipt."""
    service = getattr(server, 'normal_parties', None)
    if service is not None and opcode in (35, 36):
        service.returns.confirm_town(opcode, session)
        service.runs.confirm_town(session)


def pump(server, session, client):
    from game.player_exchange import pump as pump_exchange
    pump_exchange(server, session, client)
    service = getattr(server,'normal_parties',None)
    if service is not None:
        service.pump(session,client)
