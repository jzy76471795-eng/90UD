"""Owned ordinary-party generations inside an admitted/preparing raid.

Native keys, endpoint correlation and self profile are mandatory real inputs.
This directory owns membership; a raid group is eligibility, not membership.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import struct
from time import monotonic

from .native_network_protocol import PeerAddress
from .native_party import NativePublicationPending, PartyWireState
from .native_solo_party import NativeSoloParty
from .protocol import encode_raid_modify
from .runtime import RaidError, _OrdinaryPublicationProof, _OrdinaryUdpProof


@dataclass(frozen=True)
class Header:
    info_field: int = 0xffffffff
    difficulty: int = 0xff
    dungeon_id: int = 0
    more: int = 0
    aux: int = 2
    aux2: int = 0


@dataclass(frozen=True, eq=False)
class Seat:
    session: object
    character_id: int
    account_id: int
    epoch: object
    slot: int
    eligibility_group: int


@dataclass(frozen=True, eq=False)
class Party:
    entry: object
    group: int
    generation: int
    party_id: int
    members: tuple
    host_character_id: int
    header: Header
    revision: int = 0
    create_request: bytes | None = None


class OrdinaryParties:
    def __init__(self, service):
        self.service = service
        self._parties, self._paused, self._history = {}, {}, {}
        self._sequence, self._generation, self._used_ids = 0, 0, set()
        if not hasattr(service, '_ordinary_party_id_history'):
            service._ordinary_party_id_history = {value[1] for value in service.native_party._ids.values()}
            solo = getattr(service, 'native_solo_party', None)
            if solo is not None:
                service._ordinary_party_id_history.update(p.party_id for p in solo._parties.values())

    def uses(self, entry):
        # An existing ordinary generation survives a multi->one raid roster.
        return entry is not None and (len(entry.domain.members) > 1
            or any(member.group == 0 for member in entry.domain.members)
            or self._history.get(id(entry)) is entry)

    @staticmethod
    def key(party):
        return id(party.entry), party.group, party.generation, party.revision

    def _selection(self, session):
        cid, aid, _record = self.service._selected_actor(session)
        entry = self.service.current(cid)
        if entry is None:
            raise RaidError('ordinary party requires current owned raid membership')
        epoch = self.service.require_session(entry, session, cid, aid)
        actor = self.service.native_lobby._own(session, town=False)
        if actor.character_id != cid or actor.account_id != aid:
            raise RaidError('ordinary party requires its actual selected actor')
        group = entry.domain.member(cid).group
        return entry, cid, aid, epoch, group, actor

    def _pause_key(self, session):
        entry, cid, aid, epoch, group, _actor = self._selection(session)
        return session, entry, cid, aid, epoch, group

    def auto_prepare_allowed(self, session):
        with self.service._lock:
            try:
                return self._paused.get(id(session)) != self._pause_key(session)
            except RaidError:
                return False

    def free_group(self, entry, *, creating_character_id=None):
        """Reserve both owned squads and effective unpublished manager intent."""
        self._prune()
        occupied = {party.group for party in self._parties.values()
                    if party.entry is entry and party.group > 0}
        occupied.update(self.display_group(entry, member.character_id, member.group)
                        for member in entry.domain.members
                        if member.character_id != creating_character_id)
        group = next((value for value in range(1, 11) if value not in occupied), None)
        if group is None:
            raise RaidError('raid has no free ordinary squad label')
        return group

    def reconnect_solo(self, session, entry, cid, account, epoch):
        # The disconnect lease has authenticated this exact new selection.
        # Keep the immutable admitted Member for its ticket and reward rights.
        self._history[id(entry)] = entry
        self._paused[id(session)] = (session, entry, cid, account, epoch, entry.domain.member(cid).group)

    def manager_assigned(self, entry, character_id, group):
        """Commit an explicit manager edit after its packets have encoded."""
        for key, paused in tuple(self._paused.items()):
            if paused[1] is entry and paused[2] == character_id:
                self._paused.pop(key, None)
        for party in tuple(self._parties.values()):
            if party.entry is not entry:
                continue
            retained = tuple(seat for seat in party.members
                if seat.character_id != character_id or group > 0
                and party.group == group and seat.eligibility_group == group)
            if retained != party.members:
                self._replace_members(party, retained)

    def _replace_members(self, party, members):
        party.entry.domain._revoke_ordinary_run(self, self.key(party))
        members = tuple(sorted(members, key=lambda seat: seat.slot))
        key = (id(party.entry), party.group)
        if not members:
            self._parties.pop(key, None)
            return None
        host = party.host_character_id
        if not any(seat.character_id == host for seat in members):
            host = min(members, key=lambda seat: seat.slot).character_id
        updated = replace(party, members=members, host_character_id=host, revision=party.revision + 1)
        self._parties[key] = updated
        return updated

    def _prune(self):
        for party in tuple(self._parties.values()):
            retained = []
            for seat in party.members:
                try:
                    entry, cid, aid, epoch, group, _actor = self._selection(seat.session)
                    valid = (entry is party.entry and (cid, aid, group) ==
                        (seat.character_id, seat.account_id, seat.eligibility_group) and epoch is seat.epoch)
                except RaidError:
                    valid = False
                if valid:
                    retained.append(seat)
            if tuple(retained) != party.members:
                self._replace_members(party, retained)
        for key, paused in tuple(self._paused.items()):
            try:
                session, entry, cid, aid, epoch, group = paused
                valid = (self.service.require_session(entry, session, cid, aid) is epoch
                         and entry.domain.member(cid).group == group)
            except RaidError:
                valid = False
            if not valid:
                self._paused.pop(key, None)
        active = {id(entry): entry for entry in self.service.raids.values()}
        for key, entry in tuple(self._history.items()):
            if active.get(key) is not entry:
                self._history.pop(key, None)

    def _actors(self, party):
        actors = []
        for seat in party.members:
            entry, cid, aid, epoch, group, actor = self._selection(seat.session)
            if (entry is not party.entry or epoch is not seat.epoch or
                    (cid, aid, group) != (seat.character_id, seat.account_id, seat.eligibility_group)):
                raise RaidError('ordinary membership selection expired')
            actors.append(actor)
        return tuple(actors)

    def _facts(self, actors):
        server = self.service.server
        keys = getattr(server, 'native_game_connections', None)
        udp = getattr(server, 'native_udp_probe_service', None)
        if keys is None or udp is None:
            raise RaidError('actual Game routing/UDP source directories unavailable')
        peers = []
        for actor in actors:
            key = keys.key_for_actor(actor)
            match, claim = udp.current_claim_and_match(actor.session)
            if (match is None or match.account_id != actor.account_id or claim is None
                    or claim.account_id != actor.account_id or claim.report is not match.report):
                raise RaidError('ordinary peer has no current unique actual C0002/source correlation')
            report = match.report
            if (report.nat_kind not in (1, 2, 3, 4, 5, 6) or not report.public_port
                    or type(report.mtu) is not int or report.mtu not in (512, 1024, 1280, 1472)):
                raise RaidError('ordinary peer has no completed native address/MTU report')
            try:
                proof = self.service.native_lobby.self_profile.proof(actor.session)
            except RaidError as exc:
                raise RaidError('member=%s self-profile: %s' % (actor.character_id, exc)) from exc
            if proof.actor is not actor or type(proof.value) is not int or not 0 <= proof.value <= 100:
                raise RaidError('ordinary peer actual sent weakness outside supported range')
            peers.append(PeerAddress(actor.character_id, report.local_ipv4_u32,
                report.public_ipv4_u32, report.public_port, key, report.nat_kind, report.mtu, proof.value))
        return tuple(peers)

    def _allocate(self):
        used = set(self.service._ordinary_party_id_history)
        used.update(value[1] for value in self.service.native_party._ids.values())
        solo = getattr(self.service, 'native_solo_party', None)
        if solo is not None:
            used.update(p.party_id for p in solo._parties.values())
        for _ in range(65534):
            self._sequence = self._sequence % 65534 + 1
            if self._sequence not in used:
                self._used_ids.add(self._sequence)
                self.service._ordinary_party_id_history.add(self._sequence)
                self._generation += 1
                return self._sequence, self._generation
        raise RaidError('ordinary party namespace exhausted')

    def _new(self, entry, group, sessions, *, request=None, enforce_group=True):
        if not 1 <= len(sessions) <= 4:
            raise RaidError('ordinary party requires one to four actual members')
        seats = []
        for slot, session in enumerate(sessions):
            actual, cid, aid, epoch, selected_group, _actor = self._selection(session)
            if actual is not entry or (enforce_group and selected_group != group):
                raise RaidError('ordinary generation differs from actual raid group')
            seats.append(Seat(session, cid, aid, epoch, slot, selected_group))
        header = Header() if request is None else Header(difficulty=5)
        party = Party(entry, group, 0, 0, tuple(seats), seats[0].character_id, header,
                      create_request=request)
        self._facts(self._actors(party))
        pid, generation = self._allocate()
        return replace(party, party_id=pid, generation=generation)

    def _personal(self, session):
        entry, cid, _aid, _epoch, _group, _actor = self._selection(session)
        key = (id(entry), -cid)
        party = self._parties.get(key)
        if party is None:
            party = self._new(entry, -cid, (session,), enforce_group=False)
            self._parties[key] = party
            self._history[id(entry)] = entry
        self._paused.pop(id(session), None)
        return party

    def current(self, session, *, auto=True):
        with self.service._lock:
            self._prune()
            entry, cid, _aid, _epoch, group, _actor = self._selection(session)
            for existing in self._parties.values():
                if existing.entry is entry and any(seat.session is session for seat in existing.members):
                    return existing
            party = self._parties.get((id(entry), group))
            if not auto or not self.uses(entry):
                return None
            if not self.auto_prepare_allowed(session):
                return None
            if group == 0:
                return None  # Unassigned means no ordinary party, including after admission.
            if party is not None:
                if any(getattr(seat.session, 'in_dungeon_run', False) for seat in party.members):
                    return None
                occupied = {seat.slot for seat in party.members}
                slot = next((slot for slot in range(4) if slot not in occupied), None)
                if slot is None:
                    return None
                _entry, cid, aid, epoch, selected_group, _actor = self._selection(session)
                proposed = replace(party, members=tuple(sorted(party.members +
                    (Seat(session, cid, aid, epoch, slot, selected_group),), key=lambda seat: seat.slot)),
                    revision=party.revision + 1)
                self._facts(self._actors(proposed))
                party.entry.domain._revoke_ordinary_run(self, self.key(party))
                self._parties[(id(entry), party.group)] = proposed
                return proposed
            sessions = []
            for member in entry.domain.members:
                if member.group != group:
                    continue
                rows = [row for row in self.service._raid_sessions.values()
                        if row[1] is entry and row[2] == member.character_id]
                if not rows and member.character_id in self.service._disconnected:
                    continue
                if len(rows) != 1:
                    raise RaidError('ordinary automatic formation requires unique owned selections')
                candidate = rows[0][0]
                if self.auto_prepare_allowed(candidate):
                    sessions.append(candidate)
            if session not in sessions:
                return None
            if any(getattr(candidate, 'in_dungeon_run', False) for candidate in sessions):
                raise RaidError('ordinary formation cannot alter an active run')
            party = self._new(entry, group, sessions)
            self._parties[(id(entry), group)] = party
            self._history[id(entry)] = entry
            return party

    def publication(self, session):
        party = self.current(session)
        if party is None:
            return None
        return party, self._actors(party)

    def resolve(self, entry, actors):
        if not actors:
            raise RaidError('ordinary snapshot has no actual actor')
        party = self.current(actors[0].session, auto=False)
        if party is None or party.entry is not entry or self._actors(party) != actors:
            raise RaidError('ordinary snapshot does not match its actual membership')
        header = party.header
        return PartyWireState(header.dungeon_id, header.difficulty, party.host_character_id,
            tuple((actor.character_id, (0, 0, 1)) for actor in actors), (0, 0, 0), self._facts(actors),
            info_field=header.info_field, more=header.more, aux=header.aux, aux2=header.aux2)

    def participants(self, session):
        party = self.current(session)
        if party is None:
            entry, actor, _pause, _proof = self._detached_solo_publication(session)
            return (actor.character_id,)
        return tuple(seat.character_id for seat in party.members)

    def _detached_solo_publication(self, session):
        """Authorize an admitted solo owner after the old party was cleared.

        The explicit departure remains paused: no personal party, peer route
        or native party roster is created. Exact owner TCP/clear receipts and
        the departure generation replace the absent multiplayer publication.
        """
        try:
            service, server = self.service, self.service.server
            entry, cid, aid, epoch, group, actor = self._selection(session)
            pause = self._paused.get(id(session))
            if pause is None and group == 0 and self.current(session, auto=False) is None:
                pause = self._pause_key(session)
                self._paused[id(session)] = pause
            if (pause is None or pause != self._pause_key(session)
                    or self.current(session, auto=False) is not None
                    or entry.domain.battle is None or entry.domain.battle.status != 'active'
                    or entry.domain._admitted.get(cid) is not entry.domain.member(cid)):
                raise RaidError('solo entry requires a current admitted explicit departure')
            lobby, native = service.native_lobby, service.native_party
            keys = server.native_game_connections
            keys.key_for_actor(actor)
            connection = keys._connections[id(session)]
            self_profile = lobby.self_profile.proof(session)
            roster_key = (id(session), epoch)
            roster = encode_raid_modify(entry.domain.raid_key, 3, members=service.members(entry))
            if (self_profile.actor is not actor or not connection.sent
                    or connection.login_session_id != session.login_session_id
                    or connection.channel_id != actor.channel_id
                    or service._lobby_projections.get(roster_key) != (entry.domain.leader_id, entry.name_bytes, roster)):
                raise RaidError('solo owner profile and raid roster await actual delivery')
            references = ((self, '_paused', id(session), pause),
                (service, 'raids', entry.domain.raid_key, entry),
                (service, '_raid_sessions', id(session), service._raid_sessions[id(session)]),
                (lobby, '_by_session', id(session), actor),
                (lobby, '_actors', (actor.channel_id, cid), actor),
                (lobby.self_profile, '_values', id(session), self_profile),
                (keys, '_connections', id(session), connection),
                (service, '_lobby_projections', roster_key, service._lobby_projections[roster_key]))
            absent = ((native, '_sent', actor), (native, '_pending', actor),
                (native, '_clears', actor), (service, '_lobby_pending', roster_key))
            attributes = ((service, 'native_lobby', lobby, True),
                (service, 'native_party', native, True), (lobby, 'self_profile', lobby.self_profile, True),
                (server, 'native_game_connections', keys, True),
                (session, 'selected_character_id', cid, False), (session, 'account_id', aid, False),
                (session, 'selected_channel_id', actor.channel_id, False),
                (session, '_raid_selected_epoch', epoch, True),
                (session, '_raid_native_namespace', session._raid_native_namespace, False),
                (session, 'login_session_id', connection.login_session_id, False),
                (connection, 'selected', connection.selected, True), (connection, 'sent', True, True),
                (connection, 'sent_generation', connection.sent_generation, False),
                (connection, 'channel_id', actor.channel_id, False),
                (connection, 'identity', connection.identity, True))
            proof = _OrdinaryPublicationProof(references, absent, attributes, (connection.client,), ())
            if not proof.current():
                raise RaidError('solo entry awaits actual party-clear and owner delivery')
            return entry, actor, pause, proof
        except RaidError:
            raise
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise RaidError('solo owner publication unavailable: %s: %s' % (type(exc).__name__, exc)) from exc

    def display_group(self, entry, character_id, fallback):
        if not self.uses(entry):
            return fallback
        for party in self._parties.values():
            if party.entry is entry and any(seat.character_id == character_id
                    and seat.eligibility_group == fallback for seat in party.members):
                return max(0, party.group)
        # A manager edit is projected before the aggregate commits. An old
        # ordinary seat must not overwrite that new group in the immediate
        # roster, or restore an earlier edit while the owner pumps catch up.
        for paused in self._paused.values():
            if (paused[1] is entry and paused[2] == character_id and paused[5] == fallback
                    and getattr(paused[0], '_raid_selected_epoch', None) is paused[4]):
                return 0
        return fallback

    def _publication_proof(self, party, actors):
        service, server = self.service, self.service.server
        lobby, native = service.native_lobby, service.native_party
        keys, udp = server.native_game_connections, server.native_udp_probe_service
        if udp.clock is not monotonic:
            raise RaidError('ordinary run requires the actual monotonic UDP lease clock')
        from .native_udp import OBSERVATION_SECONDS
        references = [(self, '_parties', (id(party.entry), party.group), party),
                      (service, 'raids', party.entry.domain.raid_key, party.entry)]
        absent, attributes, sockets, observations = [], [], [], []
        attributes.extend(((service, 'native_lobby', lobby, True),
            (service, 'native_party', native, True), (lobby, 'self_profile', lobby.self_profile, True),
            (server, 'native_game_connections', keys, True), (server, 'native_udp_probe_service', udp, True)))
        for actor in actors:
            session = actor.session
            connection = keys._connections[id(session)]
            source = udp._connections.get(id(session))
            match, claim = udp.current_claim_and_match(session)
            if (source is None or match is None or claim is None
                    or match.account_id != actor.account_id or claim.account_id != actor.account_id
                    or claim.report is not match.report):
                raise RaidError('raid UDP publication lost its current claim/source snapshot')
            report = match.report
            observation_key = (match.ip, match.port, match.listener_port)
            observation = udp._observations.get(observation_key)
            if observation is None:
                raise RaidError('raid UDP publication observation expired')
            roster_key = (id(session), actor.epoch)
            references.extend(((service, '_raid_sessions', id(session), service._raid_sessions[id(session)]),
                (lobby, '_by_session', id(session), actor),
                (lobby, '_actors', (actor.channel_id, actor.character_id), actor),
                (lobby.self_profile, '_values', id(session), lobby.self_profile.proof(session)),
                (keys, '_connections', id(session), connection),
                (native, '_sent', actor, native._sent[actor]),
                (service, '_lobby_projections', roster_key, service._lobby_projections[roster_key])))
            absent.extend(((service, '_lobby_pending', roster_key), (native, '_pending', actor)))
            attributes.extend(((session, 'selected_character_id', actor.character_id, False),
                (session, 'account_id', actor.account_id, False),
                (session, 'selected_channel_id', actor.channel_id, False),
                (session, '_raid_selected_epoch', actor.epoch, True),
                (session, '_raid_native_namespace', session._raid_native_namespace, False),
                (session, 'login_session_id', connection.login_session_id, False),
                (connection, 'selected', connection.selected, True),
                (connection, 'sent', True, True),
                (connection, 'sent_generation', connection.sent_generation, False),
                (connection, 'channel_id', actor.channel_id, False),
                (session, '_native_udp_address_claim', claim, True)))
            sockets.append(connection.client)
            observations.append(_OrdinaryUdpProof(udp, source, report, observation_key,
                observation, report.public_ipv4_u32, OBSERVATION_SECONDS))
            for subject in actors:
                if actor is subject:
                    continue
                delivery = lobby._deliveries[(actor, subject)]
                references.append((lobby, '_deliveries', (actor, subject), delivery))
                attributes.append((delivery, 'sent', True, True))
        proof = _OrdinaryPublicationProof(tuple(references), tuple(absent), tuple(attributes),
            tuple(sockets), tuple(observations))
        if not proof.current():
            raise RaidError('actual ordinary publication changed while issuing its run authority')
        return proof

    def authorize_run(self, raid, session, participants):
        with self.service._lock:
            self.service._actor(session)
            party = self.current(session, auto=False)
            if party is None:
                entry, actor, pause, proof = self._detached_solo_publication(session)
                if entry.domain is not raid or tuple(participants) != (actor.character_id,):
                    raise RaidError('detached solo run must contain only its actual owner')
                raid._install_ordinary_issuer(self)
                return raid._issue_ordinary_run(self, actor.character_id,
                    (raid.member(actor.character_id),),
                    (id(entry), 'detached-solo', actor.character_id, actor.epoch), proof)
            if (party is None or party.entry.domain is not raid or
                    tuple(participants) != tuple(seat.character_id for seat in party.members)
                    or not self.ready(session)):
                raise RaidError('actual prepared ordinary membership cannot authorize this run')
            actors = self._actors(party)
            self._facts(actors)
            try:
                publication = self._publication_proof(party, actors)
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                raise RaidError('actual ordinary publication was revoked during authorization') from exc
            members = tuple(raid.member(seat.character_id) for seat in party.members)
            raid._install_ordinary_issuer(self)
            return raid._issue_ordinary_run(self, session.selected_character_id, members, self.key(party), publication)

    def preparation_error(self, session):
        """Validate exact recipient receipts once per party; encode nothing."""
        with self.service._lock:
            try:
                party = self.current(session)
                if party is None:
                    self._detached_solo_publication(session)
                    return None
                actors = self._actors(party)
                if any(getattr(seat.session, 'in_dungeon_run', False) for seat in party.members):
                    if not all((sent := self.service.native_party._sent.get(actor)) is not None
                            and sent.key == self.key(party) and sent.actors == actors for actor in actors):
                        return 'active ordinary party lacks its exact sent membership'
                    return None
                for actor in actors:
                    own, snapshot = self.service.native_party._snapshot(actor.session)
                    if snapshot is None:
                        return 'member %s has no current native party snapshot' % actor.character_id
                    if snapshot.clearing:
                        return 'member %s still has a native party clear pending' % actor.character_id
                    if self.service.native_party._sent.get(own) != snapshot:
                        return 'member %s current native address/party snapshot has not been sent' % actor.character_id
                return None
            except (RaidError, ValueError, TypeError) as exc:
                return str(exc) or type(exc).__name__

    def ready(self, session):
        return self.preparation_error(session) is None

    def all_ready(self, entry):
        with self.service._lock:
            self._preparation_failure = None
            checked = set()
            for member in entry.domain.members:
                if member.group == 0 and entry.domain.battle is None:
                    continue
                rows = [row for row in self.service._raid_sessions.values() if row[1] is entry and row[2] == member.character_id]
                if len(rows) != 1:
                    self._preparation_failure = (entry, member.character_id, 'requires one current owned selected session')
                    return False
                session = rows[0][0]
                try:
                    party = self.current(session)
                    key = self.key(party) if party is not None else ('solo', member.character_id)
                    if key in checked:
                        continue
                    error = (None if self.ready(session) else
                             self.preparation_error(session) or 'ordinary party is not prepared')
                except (RaidError, ValueError, TypeError) as exc:
                    error = str(exc) or type(exc).__name__
                if error is not None:
                    self._preparation_failure = (entry, member.character_id, error)
                    return False
                checked.add(key)
            return bool(entry.domain.members)

    @contextmanager
    def _transaction(self):
        native = self.service.native_party
        before = (dict(self._parties), dict(self._paused), dict(self._history), self._sequence,
            self._generation, set(self._used_ids), dict(native._pending), dict(native._ids), native._sequence,
            dict(self.service._lobby_pending), set(self.service._ordinary_party_id_history), dict(native._join_replays),
            dict(native._clears))
        try:
            yield
        except Exception:
            (self._parties, self._paused, self._history, self._sequence, self._generation,
             self._used_ids, native._pending, native._ids, native._sequence, self.service._lobby_pending,
             self.service._ordinary_party_id_history, native._join_replays, native._clears) = before
            raise

    def _town(self, session, party=None, *, additional_sessions=()):
        self.service._actor(session)
        self._selection(session)
        sessions = [session] if party is None else [seat.session for seat in party.members] + [session]
        for selected in additional_sessions:
            self._selection(selected)
            sessions.append(selected)
        if any(getattr(value, 'in_dungeon_run', False) or not getattr(value, 'enter_seria_settled', False)
               for value in sessions):
            raise RaidError('ordinary membership changes require all affected members in settled town')
        hooks = getattr(self.service.server, 'raid_transport_hooks', None)
        bridge = getattr(hooks, 'bridge', None)
        # Do not invert service->bridge and entry->service lock order. These
        # are actual monotonic retirement records, not client scene booleans.
        for value in sessions:
            record = getattr(bridge, '_records', {}).get(id(value))
            if record is not None and record.session is value and not record.retired:
                raise RaidError('ordinary membership retains an unretired actual run capability')
        for scope in tuple(getattr(hooks, '_scopes', {}).values()):
            # Scope is installed before guard validation. entry is assigned
            # only after the guard returns, so it is too late as a busy flag.
            if (scope.opcode == 0x0010
                    and any(scope.session is value for value in sessions)):
                raise RaidError('ordinary membership is frozen by an actual guarded dungeon entry')

    def _preflight(self, party):
        self._facts(self._actors(party))
        # Plain snapshots may be validated across owners. Encoding stays in
        # each recipient's own Game thread, never another account's TLS.
        for seat in party.members:
            _actor, snapshot = self.service.native_party._snapshot(seat.session, preview=True)
            if snapshot is None or snapshot.clearing:
                raise RaidError('ordinary recipient snapshot is unavailable')

    def _packets(self, session):
        entry = self.service.current(session.selected_character_id)
        key = (id(session), session._raid_selected_epoch)
        identity = (entry.domain.leader_id, entry.name_bytes,
                    encode_raid_modify(entry.domain.raid_key, 3, members=self.service.members(entry)))
        pending = self.service._lobby_pending.get(key)
        if pending is not None and pending[1] != identity:
            # The owner's newer accepted membership supersedes its unsent
            # roster intent. A late old object will no longer match receipts.
            self.service._lobby_pending.pop(key, None)
        packets = list(self.service.lobby_refresh_packets(session) or [])
        packets.extend(self.service.native_party.pending_packets(session, prepared_lobby=bool(packets)))
        return packets

    def join(self, session, body):
        if (type(body) is not bytes or len(body) not in (7, 8) or body[2] != 0
                or any(body[3:7]) or any(body[7:])):
            raise RaidError('ordinary mode0 join requires its exact zero-valued7/8-byte body')
        target = int.from_bytes(body[:2], 'little')
        with self.service._lock, self._transaction():
            entry, cid, _aid, _epoch, group, _actor = self._selection(session)
            self._town(session)
            target_actor = self.service.native_lobby.lookup(session, target)
            target_entry, _cid, _aid, _epoch, target_group, _actor = self._selection(target_actor.session)
            if target_entry is not entry:
                raise RaidError('ordinary join target differs from the actual raid')
            party = self.current(target_actor.session, auto=False)
            if party is None:
                raise RaidError('ordinary join target owns no current ordinary party')
            self._town(session, party)
            existing = self.current(session, auto=False)
            if existing is not None and existing is not party:
                if len(existing.members) != 1:
                    raise RaidError('actor already belongs to another ordinary party')
                self._replace_members(existing, ())
                existing = None
            if existing is None:
                occupied = {seat.slot for seat in party.members}
                slot = next((slot for slot in range(4) if slot not in occupied), None)
                if slot is None:
                    raise RaidError('ordinary party is full')
                _entry, cid, aid, epoch, selected_group, _actor = self._selection(session)
                party = self._replace_members(party, party.members + (Seat(session, cid, aid, epoch, slot, selected_group),))
                self._paused.pop(id(session), None)
            try:
                self._preflight(party)
            except NativePublicationPending:
                packets = self._packets(session)
                self.service.native_party.defer_join_ack(session, party, target)
                return packets, 'ordinary mode0 membership committed; ACK deferred for owned route and recipient delivery'
            packets = self._packets(session)
            packets.append(self.service._packet(1, 11, b'\1' + struct.pack('<HB', target, 0)))
            return packets, 'actual ordinary mode0 membership joined/replayed; recipient sends await confirmation'

    def accept_application(self, leader, applicant):
        """Commit a delivered join request on the approving host's account loop.

        Only metadata is changed for the applicant. Its own loop publishes
        its roster/addresses; no foreign-account packet is encoded here.
        """
        with self.service._lock, self._transaction():
            party = self.current(leader, auto=False)
            if party is None or party.host_character_id != leader.selected_character_id:
                raise RaidError('join application requires the actual host')
            self._town(leader, party, additional_sessions=(applicant,))
            entry, cid, aid, epoch, group, actor = self._selection(applicant)
            if entry is not party.entry:
                raise RaidError('join application belongs to another raid')
            old = self.current(applicant, auto=False)
            if old is not None and (old is party or len(old.members) != 1):
                raise RaidError('applicant already belongs to a squad')
            occupied = {s.slot for s in party.members}
            slot = next((i for i in range(4) if i not in occupied), None)
            if slot is None:
                raise RaidError('ordinary party is full')
            self._facts(self._actors(party) + (actor,))
            if old is not None:
                self._replace_members(old, ())
            self._replace_members(party, party.members + (Seat(applicant, cid, aid, epoch, slot, group),))
            self._paused.pop(id(applicant), None)
            return self._packets(leader)

    def regroup(self, entry, leader, member_id, group):
        """Change the combat squad while retaining the frozen admitted member."""
        service = self.service
        with service._lock, self._transaction():
            target = service.native_lobby.lookup(leader, member_id).session
            actual, cid, aid, epoch, eligibility_group, actor = self._selection(target)
            if actual is not entry or entry.domain._admitted.get(cid) is not entry.domain.member(cid):
                raise RaidError('second phase group change requires a current admitted member')
            source = self.current(target, auto=False)
            destination = self._parties.get((id(entry), group)) if group else None
            affected = (target,) + (tuple(seat.session for seat in destination.members) if destination else ())
            self._town(leader, source, additional_sessions=affected)
            # The town selector has no frozen roster until an entry is queued.
            # Its next selection resolves the current squad and host anew.
            runs = getattr(service.server, 'raid_squad_runs', None)
            sessions = (leader,) + affected + (tuple(seat.session for seat in source.members) if source else ())
            if runs is not None and any(runs._current(s) is not None for s in sessions):
                raise RaidError('group change awaits completion of an active dungeon entry or return')
            if not group or source is None or source is not destination:
                # Validate the new seat before retiring any previous one.
                if destination is not None:
                    occupied = {seat.slot for seat in destination.members}
                    slot = next((i for i in range(4) if i not in occupied), None)
                    if slot is None:
                        raise RaidError('native four-member group is full')
                    seat = Seat(target, cid, aid, epoch, slot, eligibility_group)
                    self._facts(self._actors(destination) + (actor,))
                elif group:
                    created = self._new(entry, group, (target,), enforce_group=False)
                if source is not None:
                    self._replace_members(source, tuple(seat for seat in source.members if seat.session is not target))
                if not group:
                    self._paused[id(target)] = self._pause_key(target)
                    service.native_party.prepare_clear(target)
                else:
                    if destination is None:
                        self._parties[(id(entry), group)] = created
                        self._history[id(entry)] = entry
                    else:
                        self._replace_members(destination, destination.members + (seat,))
                    self._paused.pop(id(target), None)
            return self._packets(leader)

    def manage(self, session, opcode, body):
        """Native C0079/C000E use the stable party slot, never a character ID."""
        from game.normal_party import NormalParties
        slot = NormalParties._fixed(body, 1)[0]
        with self.service._lock, self._transaction():
            party = self.current(session, auto=False)
            if party is None or party.host_character_id != session.selected_character_id:
                raise RaidError('squad management requires its current host')
            self._town(session, party)
            if any(getattr(s.session, 'in_dungeon_select', False) for s in party.members):
                raise RaidError('squad management requires all members outside the selector')
            target = next((s for s in party.members if s.slot == slot), None)
            if target is None or target.session is session or opcode not in (14, 121):
                raise RaidError('squad management requires another current member slot')
            ack = self.service._packet(1, opcode, b'\1')
            if opcode == 121:
                party.entry.domain._revoke_ordinary_run(self, self.key(party))
                self._parties[(id(party.entry), party.group)] = replace(party,
                    host_character_id=target.character_id, revision=party.revision + 1)
                note = 'owned squad host handed to member=%s' % target.character_id
            else:
                self._replace_members(party, tuple(s for s in party.members if s is not target))
                self._paused[id(target.session)] = self._pause_key(target.session)
                self.service.native_party.prepare_clear(target.session)
                note = 'owned squad member removed; raid admission retained member=%s' % target.character_id
            return self._packets(session) + [ack], note

    def leave(self, session, body, *, returned=False):
        if type(body) is not bytes or body:
            raise RaidError('ordinary leave requires an empty body')
        with self.service._lock, self._transaction():
            if returned:
                self.service._actor(session)
                self._selection(session)
                if getattr(session, 'in_dungeon_run', False) or getattr(session, 'in_dungeon_select', False):
                    raise RaidError('individual town return is still pending')
            else:
                self._town(session)
            party = self.current(session, auto=False)
            # Only the departing owner changes scenes. Surviving seats retain
            # their slot, scene generation and accepted run.
            # Encode ACK before changing membership, including a replay.
            ack = self.service._packet(1, 13, b'\1')
            self._paused[id(session)] = self._pause_key(session)
            if party is not None:
                self._replace_members(party, tuple(seat for seat in party.members if seat.session is not session))
            self.service.native_party.prepare_clear(session)
            packets = self._packets(session) if getattr(session, 'enter_seria_settled', False) else []
            packets.append(ack)
            return packets, 'ordinary party left; raid/group/admission retained and automatic preparation paused'

    def create(self, session, body):
        request = NativeSoloParty._request(body)
        with self.service._lock, self._transaction():
            self._town(session)
            entry, _cid, _aid, _epoch, group, _actor = self._selection(session)
            self._prune()
            previous = self.current(session, auto=False)
            if previous is not None:
                if len(previous.members) != 1 or previous.members[0].session is not session:
                    raise RaidError('ordinary group already owns a nonempty party')
                if previous.group >= 0 and previous.create_request is not None:
                    if previous.create_request != request:
                        raise RaidError('ordinary create replay changed its request')
                    party = previous
                else:
                    self._replace_members(previous, ())
                    previous = None
            if previous is None:
                group = self.free_group(entry, creating_character_id=session.selected_character_id)
                party = self._new(entry, group, (session,), request=request, enforce_group=False)
                self._parties[(id(entry), group)] = party
                self._history[id(entry)] = entry
                self._paused.pop(id(session), None)
            self._preflight(party)
            packets = self._packets(session)
            packets.append(self.service._packet(1, 12, b'\1'))
            return packets, 'actual strict C000C ordinary generation created/replayed'

    def forget_session(self, session):
        with self.service._lock:
            for party in tuple(self._parties.values()):
                if any(seat.session is session for seat in party.members):
                    self._replace_members(party, tuple(seat for seat in party.members if seat.session is not session))
            self._paused.pop(id(session), None)

    def leave_packets(self, session):
        packets, _note = self.leave(session, b'')
        return packets[:-1]


def install(service):
    directory = getattr(service, 'ordinary_party', None)
    if directory is None:
        controller=getattr(service.server,'party_relay_controller',None)
        if controller is not None:
            from game.relay_raid import RelayedRaidParties
            directory=RelayedRaidParties(service,controller)
        else:directory = OrdinaryParties(service)
        service.ordinary_party = directory
        service.native_party.state_resolver = directory.resolve
    return directory
