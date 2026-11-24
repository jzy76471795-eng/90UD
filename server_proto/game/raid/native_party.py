"""Native preparation publication after real actor and raid-roster delivery.

The optional resolver must supply trusted, current address/profile/flag fields.
No production resolver is installed while those producers remain incomplete.
Publication confirmation is separate from successful UDP connectivity/combat.
"""
from dataclasses import dataclass, replace

from game.chain_loader import load_chain
from .native_network_protocol import PeerAddress, encode_peer_addresses
from .protocol import encode_raid_modify
from .runtime import PREPARATION_GROUP_MAX, RaidError


class NativePublicationPending(RaidError):
    """Owned recipient delivery is incomplete, rather than invalid membership."""


@dataclass(frozen=True)
class PartyWireState:
    dungeon_id: int
    difficulty: int
    host_character_id: int
    member_flags: tuple
    tail_flags: tuple
    peer_addresses: tuple
    info_field: int = 0
    more: int = 0
    aux: int = 0
    aux2: int = 0


@dataclass(frozen=True)
class _Snapshot:
    key: tuple
    party_id: int
    actors: tuple
    bodies: tuple
    clearing: bool = False
    peer_routes: tuple = ()


@dataclass
class _Delivery:
    snapshot: _Snapshot
    packets: tuple
    next_index: int = 0


@dataclass(frozen=True)
class _JoinReplay:
    actor: object
    party_id: int
    target: int
    packet: object = None


class NativeParty:
    def __init__(self, service, state_resolver=None):
        self.service, self.state_resolver = service, state_resolver
        self._ids, self._sent, self._pending = {}, {}, {}
        self._clears = {}
        self._join_replays = {}
        self._sequence = 0

    def defer_join_ack(self, session, party, target):
        """Keep one replay per live owner; never admit a new member here."""
        with self.service._lock:
            ordinary = self.service.ordinary_party
            actor = self.service.native_lobby.lookup(session, session.selected_character_id)
            if (ordinary.current(session, auto=False) is not party
                    or target not in {seat.character_id for seat in party.members}):
                raise RaidError('deferred join requires its exact existing owned party')
            old = self._join_replays.get(id(session))
            if old is None or (old.actor is not actor or old.party_id != party.party_id or old.target != target):
                self._join_replays[id(session)] = _JoinReplay(actor, party.party_id, target)

    def _join_replay_packets(self, session, actor, snapshot):
        replay = self._join_replays.get(id(session))
        if replay is None:
            return []
        ordinary = self.service.ordinary_party
        try:
            party = ordinary.current(session, auto=False)
            if (replay.actor is not actor or party is None or party.party_id != replay.party_id
                    or replay.target not in {seat.character_id for seat in party.members}):
                raise RaidError('deferred join owner or membership changed')
        except (RaidError, ValueError, TypeError):
            self._join_replays.pop(id(session), None)
            return []
        if snapshot is None or snapshot.clearing or snapshot.party_id != replay.party_id:
            return []
        try:
            ordinary._town(session, party)
            ordinary._preflight(party)
        except (RaidError, ValueError, TypeError):
            # The accepted seat persists across a route or profile refresh.
            return []
        if replay.packet is None:
            import struct
            packet = self.service._packet(1, 11, b'\1' + struct.pack('<HB', replay.target, 0))
            replay = replace(replay, packet=packet)
            self._join_replays[id(session)] = replay
        return [replay.packet]

    def _prune_ids(self):
        active = {id(entry) for entry in self.service.raids.values()}
        retained = {snapshot.key for snapshot in self._sent.values()}
        retained.update(value.snapshot.key for value in self._pending.values())
        for key in tuple(self._ids):
            if key[0] not in active and key not in retained:
                self._ids.pop(key, None)

    def _party_id(self, key, entry):
        self._prune_ids()
        if key not in self._ids:
            used = {value[1] for value in self._ids.values()}
            for _ in range(65534):
                self._sequence = self._sequence % 65534 + 1
                if self._sequence not in used:
                    # Retain the entry while its old publication can still be
                    # cleared; Python object-id reuse cannot alias a new raid.
                    self._ids[key] = (entry, self._sequence)
                    break
            else:
                raise RaidError('native party directory has no free key')
        if self._ids[key][0] is not entry:
            raise RaidError('native party key belongs to another raid instance')
        return self._ids[key][1]

    def _snapshot(self, session, *, preview=False):
        try:
            return self._build_snapshot(session, preview=preview)
        except RaidError:
            raise
        except Exception as exc:
            raise RaidError('native party wire-state source failed') from exc

    def prepare_clear(self, session):
        """Retire every possibly delivered party ID without forming a new one.

        The exact clear plan survives raid membership removal. Its ordered
        owner-socket receipts, rather than the removed raid roster, retire it.
        """
        with self.service._lock:
            actor = self.service.native_lobby._own(session, town=False)
            if actor in self._clears:
                return
            snapshots = []
            sent, pending = self._sent.get(actor), self._pending.get(actor)
            if sent is not None:snapshots.append(sent)
            if pending is not None:snapshots.append(pending.snapshot)
            if not snapshots:
                return
            ids = tuple(dict.fromkeys(snapshot.party_id for snapshot in snapshots))
            helper = load_chain('helper-party_info.py')
            bodies = tuple((9, helper.build_current_namespace_party_clear(party_id=pid)) for pid in ids)
            previous = snapshots[-1]
            self._clears[actor] = _Snapshot(previous.key, previous.party_id, (), bodies, True)
            self._pending.pop(actor, None)
            self._join_replays.pop(id(session), None)

    def _build_snapshot(self, session, *, preview=False):
        service = self.service
        actor = service.native_lobby.lookup(session, session.selected_character_id)
        if actor in self._clears:
            return actor, self._clears[actor]
        entry = service.current(actor.character_id)
        helper = load_chain('helper-party_info.py')
        if entry is None:
            previous = self._sent.get(actor)
            if previous is None and actor in self._pending:
                previous = self._pending[actor].snapshot
            if previous is None:
                return actor, None
            body = helper.build_current_namespace_party_clear(party_id=previous.party_id)
            return actor, _Snapshot(previous.key, previous.party_id, (), ((9, body),), True)
        service.require_session(entry, session, actor.character_id, actor.account_id)
        member = entry.domain.member(actor.character_id)
        ordinary = getattr(service, 'ordinary_party', None)
        owned = ordinary.publication(session) if ordinary is not None and ordinary.uses(entry) else None
        if ordinary is not None and ordinary.uses(entry) and owned is None:
            previous = self._sent.get(actor)
            if previous is None and actor in self._pending:
                previous = self._pending[actor].snapshot
            if previous is None:
                return actor, None
            body = helper.build_current_namespace_party_clear(party_id=previous.party_id)
            return actor, _Snapshot(previous.key, previous.party_id, (), ((9, body),), True)
        party = owned[0] if owned is not None else None
        key = ordinary.key(party) if party is not None else (id(entry), member.group)
        config = getattr(service.battle_adapter, '_configs', {}).get(entry.raid_type)
        maximum = getattr(config, 'member_max', None)
        members = entry.domain.members
        if (type(maximum) is not int or not 1 <= maximum <= 20
                or len(members) > maximum
                or not 0 <= member.group <= PREPARATION_GROUP_MAX
                or (member.group != 0 and sum(peer.group == member.group for peer in members) > 4)
                or (member.group == 0 and (party is None
                    or party.group <= 0 and len(party.members) != 1))):
            raise RaidError('actual PVF native party capacity unavailable')
        # Raid member_max limits the whole roster, independently of the ten
        # sparse preparation labels. Each native ordinary party has four slots.
        capacity = 4
        own_key = (id(session), actor.epoch)
        roster = encode_raid_modify(entry.domain.raid_key, 3, members=service.members(entry))
        if (not preview and (own_key in service._lobby_pending
                or service._lobby_projections.get(own_key) != (entry.domain.leader_id, entry.name_bytes, roster))):
            raise NativePublicationPending('current native raid roster and metadata await recipient delivery')
        actors, rows, waiting = [], [], False
        selected_members = (tuple((entry.domain.member(seat.character_id), seat.slot) for seat in party.members)
                            if party is not None else tuple((peer, peer.slot) for peer in entry.domain.members
                                                           if peer.group == member.group))
        for peer, ordinary_slot in selected_members:
            frozen = [value for value in service._raid_sessions.values()
                      if value[1] is entry and value[2] == peer.character_id]
            if len(frozen) != 1:
                raise RaidError('native party member has no unique owned live selection')
            selected, _entry, cid, aid, _epoch = frozen[0]
            service.require_session(entry, selected, cid, aid)
            subject = service.native_lobby.lookup(session, cid)
            if getattr(selected, '_raid_native_approval_pending', None) is not None:
                raise NativePublicationPending('native approval delivery is still pending')
            if not 0 <= ordinary_slot < capacity:
                raise RaidError('native party member has no valid stable slot')
            actors.append(subject)
            rows.append((ordinary_slot, peer.character_id))
            if not service.native_lobby.visible(session, selected):
                service.native_lobby.queue_peer(session, selected)
                waiting = True
        if waiting:
            raise NativePublicationPending('native same-group actor bodies await recipient delivery')
        if self.state_resolver is None:
            raise RaidError('complete authoritative native party wire-state producer unavailable')
        # The resolver uses these frozen owned actors and server state. It must
        # not encode for another account or copy C0002 fields as authority.
        state = self.state_resolver(entry, tuple(actors))
        if type(state) is not PartyWireState:
            raise RaidError('complete authoritative native party wire-state producer unavailable')
        flags = tuple(state.member_flags)
        flag_map = dict(flags)
        uids = tuple(uid for _slot, uid in rows)
        if len(flag_map) != len(flags) or set(flag_map) != set(uids):
            raise RaidError('native party flags do not cover its exact frozen members')
        tail = tuple(state.tail_flags)
        if (len(tail) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in tail)
                or type(state.host_character_id) is not int or state.host_character_id not in uids):
            raise RaidError('native party host or explicit tail fields unavailable')
        peers = tuple(state.peer_addresses)
        if (any(type(p) is not PeerAddress for p in peers)
                or tuple(p.user_id for p in peers) != uids
                or any(type(p.weakness_percent) is not int or not 0 <= p.weakness_percent <= 100 for p in peers)):
            raise RaidError('native peer addresses must cover the exact ordered party members')
        connections = getattr(service.server, 'native_game_connections', None)
        if connections is not None:
            # Real Game accepts always own this directory. Controlled codec
            # fixtures without a transport remain explicit resolver inputs.
            connections.verify_peer_keys(tuple(actors), peers)
        addresses = encode_peer_addresses(peers)
        party_id = party.party_id if party is not None else self._party_id(key, entry)
        host_slot = next(slot for slot, uid in rows if uid == state.host_character_id)
        body = helper.build_current_namespace_party_slots(party_id=party_id,
            members=tuple((slot, uid, flag_map[uid]) for slot, uid in rows),
            user_max=capacity, dungeon_id=state.dungeon_id, difficulty=state.difficulty,
            tail=(tail[0], tail[1], host_slot, tail[2]), info_field=state.info_field,
            more=state.more, aux=state.aux, aux2=state.aux2)
        bodies = ((11, addresses), (9, body))
        previous = self._sent.get(actor)
        routes = tuple((p.user_id, p.local_ipv4_u32, p.public_ipv4_u32,
                        p.public_port, p.connection_key, p.nat_kind) for p in peers)
        # A renewed relay authorization changes a virtual endpoint while the
        # selected native actor and ordinary membership can remain unchanged.
        # Retire the old native party before republishing its real addresses;
        # an unchanged PARTY_INFO alone leaves an already-connected peer alive.
        # MTU/profile-only changes do not request a native reconnect.
        if party is not None and previous is not None and (
                previous.party_id != party_id or previous.peer_routes != routes):
            bodies = ((9, helper.build_current_namespace_party_clear(party_id=previous.party_id)),) + bodies
        return actor, _Snapshot(key, party_id, tuple(actors), bodies, peer_routes=routes)

    def pending_packets(self, session, *, prepared_lobby=False):
        with self.service._lock:
            if getattr(session, 'in_dungeon_run', False):
                self._join_replays.pop(id(session), None)
                return []
            try:
                self.service.native_lobby.lookup(session, getattr(session, 'selected_character_id', None))
            except RaidError:
                self.forget_session(session)
                return []
            self.service._actor(session)
            try:
                if prepared_lobby:
                    key = (id(session), getattr(session, '_raid_selected_epoch', None))
                    entry = self.service.current(session.selected_character_id)
                    pending = self.service._lobby_pending.get(key)
                    identity = (entry.domain.leader_id, entry.name_bytes,
                                encode_raid_modify(entry.domain.raid_key, 3, members=self.service.members(entry)))
                    if pending is None or pending[1] != identity:
                        raise RaidError('ordinary publication requires its exact prepared recipient roster batch')
                actor, snapshot = self._snapshot(session, preview=prepared_lobby)
            except NativePublicationPending:
                self._discard_pending(session)
                return []
            except (RaidError, ValueError, TypeError):
                self._discard_pending(session)
                return []
            if snapshot is None:
                self._join_replays.pop(id(session), None)
            if snapshot is None or self._sent.get(actor) == snapshot:
                self._pending.pop(actor, None)
                return self._join_replay_packets(session, actor, snapshot) if snapshot is not None else []
            pending = self._pending.get(actor)
            if pending is None or pending.snapshot != snapshot:
                packets = tuple(self.service._packet(0, opcode, body) for opcode, body in snapshot.bodies)
                pending = self._pending[actor] = _Delivery(snapshot, packets)
            return list(pending.packets[pending.next_index:])

    def note_send(self, session, packet):
        if packet is None:
            return
        with self.service._lock:
            replay = self._join_replays.get(id(session))
            if replay is not None and replay.packet is packet:
                self.service._actor(session)
                try:
                    actor, snapshot = self._snapshot(session)
                except (RaidError, ValueError, TypeError):
                    self._join_replays.pop(id(session), None)
                    return
                if self._sent.get(actor) == snapshot and self._join_replay_packets(session, actor, snapshot) == [packet]:
                    self._join_replays.pop(id(session), None)
                return
            if not any(actor.session is session and any(value is packet for value in pending.packets)
                       for actor, pending in self._pending.items()):
                return
            self.service._actor(session)
            try:
                actor, snapshot = self._snapshot(session)
            except (RaidError, ValueError, TypeError):
                self._discard_pending(session)
                return
            pending = self._pending.get(actor)
            if pending is None:
                return
            if snapshot != pending.snapshot:
                self._pending.pop(actor, None)
                return
            if packet is not pending.packets[pending.next_index]:
                return
            pending.next_index += 1
            if pending.next_index == len(pending.packets):
                self._pending.pop(actor, None)
                if snapshot.clearing:
                    self._sent.pop(actor, None)
                    self._clears.pop(actor, None)
                else:
                    # The leading old-party clear is a transition effect,
                    # not part of the new party's continuing canonical state.
                    self._sent[actor] = (replace(snapshot, bodies=snapshot.bodies[-2:])
                        if len(snapshot.bodies) == 3 and snapshot.bodies[0][0] == 9 else snapshot)
                self._prune_ids()

    def require_prepared(self, entry):
        """Confirm current publication only; this grants no UDP combat proof."""
        with self.service._lock:
            ordinary = getattr(self.service, 'ordinary_party', None)
            if ordinary is not None and ordinary.uses(entry):
                if not ordinary.all_ready(entry):
                    raise RaidError('all admitted ordinary parties await actual recipient publication')
                return
            for member in entry.domain.members:
                frozen = [value for value in self.service._raid_sessions.values()
                          if value[1] is entry and value[2] == member.character_id]
                if len(frozen) != 1:
                    raise RaidError('native party preparation has no unique live recipient')
                actor, snapshot = self._snapshot(frozen[0][0])
                if snapshot is None or snapshot.clearing or self._sent.get(actor) != snapshot:
                    raise RaidError('current native peer addresses and PARTY_INFO await ordered recipient delivery')

    def _discard_pending(self, session):
        for actor in tuple(self._pending):
            if actor.session is session:
                self._pending.pop(actor, None)

    def forget_session(self, session):
        with self.service._lock:
            ordinary = getattr(self.service, 'ordinary_party', None)
            if ordinary is not None:
                ordinary.forget_session(session)
            self._discard_pending(session)
            for actor in tuple(self._clears):
                if actor.session is session:self._clears.pop(actor, None)
            self._join_replays.pop(id(session), None)
            for actor in tuple(self._sent):
                if actor.session is session:
                    self._sent.pop(actor, None)
            self._prune_ids()
