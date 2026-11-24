"""Owned ordinary-party scenes; handlers and sends run only on each owner thread.

Metadata locks never enclose admission, inventory, profile factories or socket
I/O. Public world identities are shared; progression and loot remain private.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock, local
from time import monotonic
from uuid import uuid4
import struct

from game.game_wire import parse_game_header
from game.raid.dungeon_adapter import OwnerIdentity
from game.raid.runtime import RaidError
from game.raid.shared_world import SharedRunWorld


def _packet_key(packet):
    h = parse_game_header(packet[:0x10])
    return h.flag_or_magic & 255, h.opcode & 65535


@dataclass(frozen=True)
class Participant:
    selection: tuple
    slot: int
    character_id: int
    account_id: int
    identity: OwnerIdentity

    @property
    def session(self):
        return self.selection[0].session


@dataclass(frozen=True)
class Ticket:
    run_id: str
    dungeon_id: int
    participants: tuple


@dataclass(eq=False)
class Cycle:
    kind: str
    body: bytes
    expires: float
    prepared: dict = field(default_factory=dict)
    preparing: set = field(default_factory=set)
    staged: set = field(default_factory=set)
    sent: set = field(default_factory=set)
    loading_tails: dict = field(default_factory=dict)
    loaded: set = field(default_factory=set)
    profiles: dict = field(default_factory=dict)
    projected: set = field(default_factory=set)
    room: object = None
    committed: bool = False
    first: object = None
    previous: object = None
    queued_at: float = field(default_factory=monotonic)


@dataclass(eq=False)
class Run:
    party: object
    participants: tuple
    dungeon_id: int
    world: object
    ticket: Ticket
    cycle: Cycle
    generations: dict = field(default_factory=dict)
    room_definitions: dict = field(default_factory=dict)
    departed: set = field(default_factory=set)
    deaths: dict = field(default_factory=dict)
    cancel_attempts: dict = field(default_factory=dict)
    active: bool = True
    failure: str = ''
    move_intent: object = None
    move_deadline: float = 0
    move_waiters: dict = field(default_factory=dict)


class NormalPartyRuns:
    def __init__(self, parties):
        self.parties, self.server = parties, parties.server
        self._lock, self._scope = RLock(), local()
        self._runs, self._jobs = {}, {}
        self.packet_key = _packet_key
        from game.normal_party_profiles import build_profiles
        self.profile_builder = build_profiles

    @contextmanager
    def _metadata(self):
        with self.parties._lock, self._lock:
            yield

    def _live(self, member):
        return self.parties._live(member.selection)

    def _members(self, run):
        return tuple(m for m in run.participants if m.selection[0] not in run.departed)

    def _connections(self, run):
        return {m.selection[0] for m in self._members(run)}

    def _current(self, session):
        row = self._runs.get(id(session))
        return row[1] if row is not None and row[0] is session else None

    def _member(self, run, session):
        m = next((m for m in self._members(run) if m.session is session), None)
        if m is None or not self._live(m):
            raise RaidError('ordinary shared scene selected connection expired')
        return m

    def _valid(self, run):
        return (run.active and all(self._live(m) and
            self.parties._parties.get(id(m.session)) is run.party and
            run.party.members.get(m.slot) == m.selection for m in self._members(run)))

    def _owned(self, run, member):
        return (self._live(member) and self._current(member.session) is run and
            member.selection[0] not in run.departed)

    def _scene(self, run, member):
        s = member.session
        return (self._owned(run, member) and getattr(s, 'in_dungeon_run', False) and
            id(s) not in self.parties.returns.pending and
            run.generations.get(member.selection[0]) == getattr(s, 'dungeon_run_generation', None))

    def _queue(self, member, kind, run, cycle=None, body=b''):
        rows = self._jobs.setdefault(member.selection[0], [])
        if any(r[:4] == (member, kind, run, cycle) for r in rows):
            return
        if len(rows) >= 16:
            raise RaidError('ordinary shared scene delivery queue full')
        rows.append((member, kind, run, cycle, bytes(body)))

    def _cancel(self, run, reason):
        if not run.active:
            return
        run.active, run.failure = False, reason
        for m in self._members(run):
            c = m.selection[0]
            self._jobs[c] = [r for r in self._jobs.get(c, ()) if r[2] is not run]
            self._queue(m, 'cancel', run)
        run.deaths.clear()

    def detach(self, session, *, continue_move=False):
        """Metadata only; called on confirmed return/actual membership removal."""
        with self._metadata():
            run = self._current(session)
            if run is None:
                return
            m = next((m for m in self._members(run) if m.session is session), None)
            if m is None:
                return
            # Once every native entry committed, a returning member must not
            # cancel survivors merely because their loading is still pending.
            cycle = run.cycle
            keep_move = continue_move and cycle.kind == 'move' and run.active
            if not cycle.committed and not keep_move:
                self._cancel(run, 'member left during shared scene preparation')
            c = m.selection[0]
            run.departed.add(c)
            # A refused door restores the previous room's delivery cursors.
            cycles = (cycle, cycle.previous) if keep_move else (cycle,)
            for room in cycles:
                for values in (room.preparing, room.staged, room.sent, room.loaded, room.projected):
                    values.discard(c)
                for values in (room.prepared, room.profiles, room.loading_tails):
                    values.pop(c, None)
            run.deaths.pop(c, None)
            self._jobs.pop(c, None)
            self._runs.pop(id(session), None)
            run.move_intent = None
            for waiting in run.move_waiters.values():
                if waiting.selection[0] != c:
                    self._queue(waiting, 'door_refused', run, cycle)
            run.move_waiters.clear()
            if not self._members(run):
                run.active = False
            elif run.active:
                if keep_move and not cycle.committed:
                    survivors = self._members(run)
                    if cycle.first is m:
                        cycle.first = next((peer for peer in survivors
                            if peer.selection[0] in cycle.prepared), survivors[0])
                    if set(cycle.prepared) == self._connections(run):
                        self._commit_prepared(run, cycle)
                    else:
                        for peer in survivors:
                            if peer.selection[0] not in cycle.prepared:
                                self._queue(peer, 'prepare', run, cycle)
                self._advance(run,cycle)

    def _commit_prepared(self, run, cycle):
        cycle.committed = True
        for member in self._members(run):
            self._queue(member, 'stage', run, cycle)

    def _advance(self,run,cycle):
        """Metadata only. Each phase is released onto the actual owner loop."""
        connections = self._connections(run)
        if cycle.staged == connections:
            for m in self._members(run):
                if m.selection[0] not in cycle.sent:self._queue(m,'send',run,cycle)
        if cycle.sent == connections and set(cycle.loading_tails) == connections:
            for m in self._members(run):
                if m.selection[0] not in cycle.loaded:self._queue(m,'release_loading',run,cycle)
        if cycle.loaded == connections:
            for m in self._members(run):
                if m.selection[0] not in cycle.projected:self._queue(m,'project',run,cycle)

    def forget(self, session):
        self.detach(session)
        with self._metadata():
            for c in tuple(self._jobs):
                if c.session is session:
                    self._jobs.pop(c, None)

    def confirm_town(self, session):
        """Retire a completed member only after an accepted town request."""
        with self._metadata():
            run = self._current(session)
            if (run is None or getattr(session, 'in_dungeon_run', False)
                    or getattr(session, 'in_dungeon_select', False)
                    or not 1 <= int(getattr(session, 'cur_town_id', 0) or 0) <= 254
                    or not 0 <= int(getattr(session, 'cur_area_id', 255)) <= 254):
                return
            member = self._member(run, session)
            if run.generations.get(member.selection[0]) == getattr(session, 'dungeon_run_generation', None):
                self.detach(session)

    def route(self, opcode, body, session):
        if opcode == 0x10:
            return self._start(body, session)
        with self._metadata():
            run = self._current(session)
            if run is None:
                return None
            member = self._member(run, session)
            if not run.active:
                return ([self.server._codec_response(1, 0x2d, b'\0')] if opcode == 0x2d else []), 'ordinary shared scene cancellation pending'
            cycle = run.cycle
            if opcode == 0x25:
                if member.selection[0] not in cycle.loaded:
                    self._queue(member, 'loading', run, cycle, body)
                return [], 'ordinary shared loading queued on the actual member thread'
            if opcode == 0x27:
                if not self._scene(run, member) or member.selection[0] not in cycle.loaded:
                    return [], 'ordinary shared combat awaits owned scene loading'
                return None
            if opcode == 0x2d:
                raw = bytes(body)
                if (len(raw) < 2 or not self._scene(run, member)
                        or cycle.room != SharedRunWorld.room_key(session)):
                    return [self.server._codec_response(1, 0x2d, b'\0')], 'shared door refused; native wait released'
                # Keep a legitimate request across recipient loading/death
                # callbacks. The client has already set its request latch.
                if run.move_intent is None:
                    run.move_intent = (member, raw, cycle)
                    run.move_deadline = self.parties.clock() + 30
                elif run.move_intent[2] is not cycle or run.move_intent[1][:2] != raw[:2]:
                    return [self.server._codec_response(1, 0x2d, b'\0')], 'another shared door is pending; native wait released'
                run.move_waiters[member.selection[0]] = member
                return [], 'ordinary shared door queued for all actual members'
        return None

    def _start(self, body, session):
        with self._metadata():
            if self._current(session) is not None:
                return [], 'ordinary shared entry already queued or active'
            party = self.parties.current(session)
            if party is None or len(party.members) < 2:
                return None
            owner = self.parties._owner(session, town=False)
            if owner != party.owner:
                raise RaidError('only the actual ordinary leader can start a group dungeon')
            selector = self.parties.selectors.rounds.get(id(party))
            if (selector is None or not self.parties.selectors._valid(selector) or
                    selector.entered != {s[0] for s in selector.members}):
                raise RaidError('ordinary group entry requires all actual members in its selector')
            revision = party.revision
            ordered = [(k,s) for k,s in sorted(party.members.items()) if s == owner]
            ordered += [(k,s) for k,s in sorted(party.members.items()) if s != owner]
            members = tuple(Participant(s,k,s[4],s[2],OwnerIdentity(s[4],str(s[2]),s[3],
                (id(s[0]),s[1],s[6]))) for k,s in ordered)
            if any(not self._live(m) or not getattr(m.session,'in_dungeon_select',False)
                   or getattr(m.session,'in_dungeon_run',False) for m in members):
                raise RaidError('ordinary group member is outside its frozen selector')
        raw = bytes(body)
        if len(raw) not in (17,32):
            raise ValueError('ordinary group SELECT requires the frozen Stage3 body')
        from game.chain_loader import load_chain
        parsed = load_chain('flow-dungeon_enter.py').parse_0010_plain(raw)
        dungeon = parsed['dungeon_id']
        if type(dungeon) is not int or not 1 <= dungeon <= 65535 or parsed['difficulty'] not in range(6):
            raise ValueError('invalid ordinary shared dungeon selection')
        self.parties._peer_addresses(m.selection for m in members)
        world = SharedRunWorld(dungeon, tuple(m.character_id for m in members))
        ticket = Ticket(uuid4().hex, dungeon, members)
        world.attach_ticket(ticket)
        cycle = Cycle('entry', raw, self.parties.clock()+30, first=members[0])
        run = Run(party, members, dungeon, world, ticket, cycle)
        with self._metadata():
            if (party.revision != revision or not self.parties.selectors._valid(selector)
                    or any(self._current(m.session) is not None for m in members)
                    or not self._valid(run)):
                raise RaidError('ordinary shared membership changed during preflight')
            for m in members:
                self._runs[id(m.session)] = (m.session, run)
                self._queue(m,'prepare',run,cycle)
        return [], 'ordinary shared entry queued for every actual member'

    @staticmethod
    def _clear(session):
        return all(bool(r.get('killed')) for r in getattr(session,'dungeon_room_mons',())
                   if r.get('is_blocking',True))

    def _maintain(self):
        for run in tuple({id(r):r for _,r in self._runs.values()}.values()):
            if not run.active:
                continue
            cycle = run.cycle
            if not self._valid(run):
                self._cancel(run,'shared membership or selected connection changed')
            elif cycle.projected != self._connections(run) and self.parties.clock() >= cycle.expires:
                self._cancel(run,'shared scene preparation/loading expired')
            elif run.move_intent is not None:
                member, body, previous = run.move_intent
                members = self._members(run)
                if (previous is not cycle or not self._scene(run,member)
                        or self.parties.clock() >= run.move_deadline):
                    run.move_intent = None
                    if previous is cycle:
                        for waiting in run.move_waiters.values():
                            self._queue(waiting, 'door_refused', run, cycle)
                    run.move_waiters.clear()
                elif cycle.projected == self._connections(run) and all(self._scene(run,m) and self._clear(m.session) and
                        SharedRunWorld.room_key(m.session) == cycle.room for m in members) and not any(run.deaths.values()):
                    run.move_intent = None
                    run.move_waiters.clear()
                    run.cycle = Cycle('move',body,self.parties.clock()+30,first=member,previous=cycle)
                    for m in members:
                        self._queue(m,'prepare',run,run.cycle)

    def pump(self, session, client):
        if self.server.account_id != getattr(session,'account_id',None):
            return
        with self._metadata():
            self._maintain()
            run = self._current(session)
            if run is None:
                return
            member = self._member(run,session)
            connection = member.selection[0]
            if connection.client is not client:
                raise RaidError('ordinary shared delivery socket ownership mismatch')
            jobs = self._jobs.pop(connection,())
        for m,kind,run,cycle,body in jobs:
            with self._metadata():
                if not self._owned(run,m):
                    continue
                if kind != 'cancel' and (not run.active or cycle is not run.cycle
                        or id(session) in self.parties.returns.pending):
                    continue
            try:
                if kind == 'prepare':
                    self._prepare(m,run,cycle,client)
                elif kind == 'stage':
                    self._stage_start(m,run,cycle,client)
                elif kind == 'send':
                    self._send_start(m,run,cycle,client)
                elif kind == 'loading':
                    self._loading(m,run,cycle,body,client)
                elif kind == 'release_loading':
                    self._release_loading(m,run,cycle,client)
                elif kind == 'project':
                    self._project(m,run,cycle,client)
                elif kind == 'door_refused':
                    client.sendall(self.server._codec_response(1, 0x2d, b'\0'))
                elif kind == 'cancel':
                    self._restore(m,run,client)
            except Exception as exc:
                with self._metadata():
                    detail = str(exc).replace('\r',' ').replace('\n',' ')[:512]
                    if m.selection[0] not in run.departed:
                        self._cancel(run,'%s during %s: %s' % (type(exc).__name__,kind,detail))
                logger = getattr(self.server,'log',None)
                if callable(logger):
                    logger('[PartyRun] cid=%s action=%s failed=%s dungeon=%s detail=%s' %
                           (m.character_id,kind,type(exc).__name__,run.dungeon_id,detail))
                if isinstance(exc,OSError):
                    raise
        self._drain_deaths(session,client)

    def _prepare(self,m,run,cycle,client):
        preparation_started = monotonic()
        c,s = m.selection[0],m.session
        with self._metadata():
            if not self._valid(run):
                self._cancel(run,'membership changed before native scene preparation');return
            if c in cycle.prepared or c in cycle.preparing:
                return
            if m is not cycle.first and cycle.first.selection[0] not in cycle.prepared:
                self._queue(m,'prepare',run,cycle);return
            cycle.preparing.add(c)
            before = int(getattr(s,'dungeon_run_generation',0) or 0)
            oldroom = SharedRunWorld.room_key(s) if cycle.kind == 'move' else None
        try:
            prefix = []
            if cycle.kind == 'entry':
                if int(getattr(s,'selected_dungeon_id',0) or 0) != run.dungeon_id:
                    prefix,_ = self.server._handle_enter_select_dungeon_000f(struct.pack('<I',run.dungeon_id),session=s)
                packets,native_note = self.server._handle_select_dungeon_0010(cycle.body,session=s)
            else:
                packets,native_note = self.server._handle_move_map_002d(cycle.body,session=s)
            keys = {self.packet_key(p) for p in packets}
            accepted = ((0,0x1d) in keys and getattr(s,'in_dungeon_run',False) and
                int(getattr(s,'selected_dungeon_id',0) or 0) == run.dungeon_id and
                ((cycle.kind == 'entry' and (0,0x1c) in keys and s.dungeon_run_generation > before)
                 or (cycle.kind == 'move' and SharedRunWorld.room_key(s) != oldroom)))
            if not accepted:
                # The first native door may legitimately refuse special-map
                # admission. Preserve its native reply and the original room.
                if cycle.kind == 'move' and m is cycle.first and SharedRunWorld.room_key(s) == oldroom:
                    with self._metadata():
                        if run.active and run.cycle is cycle and self._owned(run, m):
                            run.cycle = cycle.previous
                            for target in self._members(run):
                                self._queue(target, 'door_refused', run, run.cycle)
                    for p in packets: client.sendall(p)
                    return
                if cycle.kind == 'entry':
                    # Keep the native refusal (e.g. zero fatigue) visible; no
                    # START_MAP is released while another member is refused.
                    for p in packets:
                        if self.packet_key(p) not in ((0,0x1c),(0,0x1d)):
                            client.sendall(p)
                raise RaidError('native member admission did not commit the shared scene: '+str(native_note))
            room = SharedRunWorld.room_key(s)
            profiles = self.profile_builder(self.server,m)
            with self._metadata():
                if c in run.departed:
                    return
                run.generations[c] = s.dungeon_run_generation
                if not self._valid(run) or cycle is not run.cycle:
                    self._cancel(run,'scene changed during native preparation');return
                if cycle.room is not None and cycle.room != room:
                    self._cancel(run,'native members committed different shared rooms');return
                cycle.room = room
                cycle.prepared[c] = tuple(prefix)+tuple(packets)
                cycle.profiles[c] = profiles
                if set(cycle.prepared) == self._connections(run):
                    self._commit_prepared(run, cycle)
        finally:
            with self._metadata():
                cycle.preparing.discard(c)
                # Include a partially mutated own session in safe cancellation.
                if getattr(s,'in_dungeon_run',False):
                    run.generations.setdefault(c,getattr(s,'dungeon_run_generation',None))
            logger = getattr(self.server,'log',None)
            if cycle.kind == 'entry' and callable(logger):
                logger('[PartyRun/Timing] cid=%s dungeon=%s stage=prepare elapsed_ms=%.1f queued_ms=%.1f' %
                       (m.character_id,run.dungeon_id,(monotonic()-preparation_started)*1000,
                        (preparation_started-cycle.queued_at)*1000))

    def _stage_start(self,m,run,cycle,client):
        with self._metadata():
            if m.selection[0] in cycle.staged:return
            if not cycle.committed or not self._valid(run) or not self._scene(run,m):
                self._cancel(run,'shared member expired before entry staging');return
            packets = cycle.prepared[m.selection[0]]
            split = next(i for i,p in enumerate(packets) if self.packet_key(p) == (0,0x1d))
            profiles = tuple(cycle.profiles[p.selection[0]] for p in self._members(run) if p is not m)
            roster = self.parties._roster(run.party)
        for packet in packets[:split]:client.sendall(packet)
        # US 01641AC2/01641B12 reads party actors during START_MAP, then
        # 02972BC0 reads their character binding. Late-only USERINFO cannot
        # supply those objects retroactively. Publish identities, install
        # real slot membership (01E861C0), then bind peer characters.
        for public,_ in profiles:client.sendall(self.server._codec_response(0,2,public))
        client.sendall(self.server._codec_response(0,9,roster))
        for _,combat in profiles:client.sendall(self.server._codec_response(0,2,combat))
        with self._metadata():
            if self._owned(run,m) and run.active and run.cycle is cycle:
                cycle.staged.add(m.selection[0])
                self._advance(run,cycle)

    def _send_start(self,m,run,cycle,client):
        with self._metadata():
            if m.selection[0] in cycle.sent:return
            if (not cycle.committed or cycle.staged != self._connections(run)
                    or not self._valid(run) or not self._scene(run,m)):
                self._cancel(run,'shared member expired before START_MAP delivery');return
            packets = cycle.prepared[m.selection[0]]
            split = next(i for i,p in enumerate(packets) if self.packet_key(p) == (0,0x1d))
        for packet in packets[split:]:client.sendall(packet)
        logger = getattr(self.server,'log',None)
        if cycle.kind == 'entry' and callable(logger):
            logger('[PartyRun/Timing] cid=%s dungeon=%s stage=start_map since_queue_ms=%.1f' %
                   (m.character_id,run.dungeon_id,(monotonic()-cycle.queued_at)*1000))
        with self._metadata():
            if self._owned(run,m) and run.active and run.cycle is cycle:
                cycle.sent.add(m.selection[0])
                self._advance(run,cycle)

    def _loading(self,m,run,cycle,body,client):
        c = m.selection[0]
        with self._metadata():
            if c in cycle.loading_tails:return
            if cycle.sent != self._connections(run):
                self._queue(m,'loading',run,cycle,body);return
            if not self._scene(run,m):return
        packets,_ = self.server._handle_finish_loading_0025(body,session=m.session)
        acks = tuple(p for p in packets if self.packet_key(p) == (1,0x25))
        tails = tuple(p for p in packets if self.packet_key(p) != (1,0x25))
        if len(acks) != 1 or not any(self.packet_key(p) == (0,0x1e) for p in tails):
            raise RaidError('native shared loading did not produce its ACK and release')
        with self._metadata():
            if not self._scene(run,m) or run.cycle is not cycle:return
        for packet in acks:client.sendall(packet)
        with self._metadata():
            if not self._scene(run,m) or run.cycle is not cycle:return
            cycle.loading_tails[c] = tails
            self._advance(run,cycle)

    def _release_loading(self,m,run,cycle,client):
        c = m.selection[0]
        with self._metadata():
            if (c in cycle.loaded or not self._valid(run) or not self._scene(run,m)
                    or cycle.sent != self._connections(run)
                    or set(cycle.loading_tails) != self._connections(run)):return
            packets = cycle.loading_tails[c]
        for packet in packets:client.sendall(packet)
        logger = getattr(self.server,'log',None)
        if cycle.kind == 'entry' and callable(logger):
            logger('[PartyRun/Timing] cid=%s dungeon=%s stage=loading_release since_queue_ms=%.1f' %
                   (m.character_id,run.dungeon_id,(monotonic()-cycle.queued_at)*1000))
        with self._metadata():
            if not self._scene(run,m) or run.cycle is not cycle:return
            cycle.loaded.add(c)
            self._advance(run,cycle)

    def _project(self,m,run,cycle,client):
        with self._metadata():
            if (not self._valid(run) or not self._scene(run,m) or
                    cycle.loaded != self._connections(run) or m.selection[0] in cycle.projected):return
            profiles = tuple(cycle.profiles[p.selection[0]] for p in self._members(run) if p is not m)
            roster = self.parties._roster(run.party)
            dead = tuple(p.character_id for p in self._members(run)
                         if getattr(p.session, 'dungeon_death_pending', False)
                         and getattr(p.session, 'dungeon_death_actor_id', None) == p.character_id)
        # Keep membership before character binding in both publication phases.
        # Each recipient uses its own codec, never host ciphertext.
        for public,_ in profiles:client.sendall(self.server._codec_response(0,2,public))
        client.sendall(self.server._codec_response(0,9,roster))
        for _,combat in profiles:client.sendall(self.server._codec_response(0,2,combat))
        # Restore accepted deaths after loading and actor reconstruction.
        for cid in dead:
            client.sendall(self.server._codec_response(0,0x20,struct.pack('<HB',cid,0)))
        with self._metadata():
            if self._scene(run,m) and run.cycle is cycle:
                cycle.projected.add(m.selection[0])
        logger = getattr(self.server,'log',None)
        if callable(logger):
            logger('[PartyRun] cid=%s cycle=%s room=%s peer_profiles_sent=%s' %
                   (m.character_id,cycle.kind,cycle.room,len(profiles)))

    def _restore(self,m,run,client):
        s,c = m.session,m.selection[0]
        with self._metadata():
            generation = run.generations.get(c)
            valid = self._owned(run,m) and (generation is None or generation == getattr(s,'dungeon_run_generation',None))
        try:
            logger = getattr(self.server,'log',None)
            if callable(logger):
                logger('[PartyRun] cid=%s action=restore_town dungeon=%s reason=%s' %
                       (m.character_id,run.dungeon_id,run.failure))
            if valid and (getattr(s,'in_dungeon_run',False) or getattr(s,'in_dungeon_select',False)):
                packets,_ = self.server._handle_back_2_village_0084(b'',session=s)
                for p in packets:client.sendall(p)
        finally:
            with self._metadata():
                if self._current(s) is run and not run.active:
                    if valid and (getattr(s,'in_dungeon_run',False) or getattr(s,'in_dungeon_select',False)):
                        attempts = run.cancel_attempts.get(c,0) + 1
                        run.cancel_attempts[c] = attempts
                        if attempts < 3:self._queue(m,'cancel',run)
                    else:
                        self._runs.pop(id(s),None)

    def entry_plan(self,session,dungeon,start,seed,difficulty,entry_option):
        with self._metadata():
            run = self._current(session)
            if run is None:return None
            m = self._member(run,session)
            if not run.active or m.selection[0] not in run.cycle.preparing or dungeon != run.dungeon_id:
                raise RaidError('shared entry plan requires owned native preparation')
            return run.world.entry_plan(session,m.identity,start,seed,difficulty,entry_option)

    def map_actor_index(self,session,default):
        with self._metadata():
            run = self._current(session)
            if run is None:return default
            # Each recipient gets a full START_MAP for its own shared scene.
            # US 01641AC2/01641EFB resolves this slot's character and chooses
            # local loading only for self (or no character). Using the leader
            # here sends followers down the remote-character update branch.
            return self._member(run,session).slot

    def bind_room(self,session):
        with self._metadata():
            run = self._current(session)
            if run is None:return None
            m = self._member(run,session)
            if not run.active or (m.selection[0] not in run.cycle.preparing and not self._scene(run,m)):
                raise RaidError('shared room belongs to another run generation')
            return run.world.bind_room(session,m.identity)

    def room_definition(self,session,x,y,room):
        with self._metadata():
            run = self._current(session)
            if run is None:return room
            self._member(run,session)
            key = (int(getattr(session,'dungeon_maze_index',0) or 0),int(x),int(y),int(room['map_index']))
            if key not in run.room_definitions:run.room_definitions[key] = deepcopy(room)
            return deepcopy(run.room_definitions[key])

    def room_plan(self,session,key):
        with self._metadata():
            run = self._current(session)
            if run is None:return None
            return run.world.room_plan(session,self._member(run,session).identity,key)

    def note_death(self,session,row):
        with self._metadata():
            run = self._current(session)
            if run is None or not run.active:return
            m = self._member(run,session)
            if not self._scene(run,m):return
            # Actors created locally after room publication have no peer row.
            identity = run.world.actor_identity(session, m.identity, row, missing_ok=True)
            if identity is None:
                return
            for peer,owner,actual in run.world.owned_actor_views(identity):
                target = next((p for p in self._members(run) if p.session is peer and p.identity == owner),None)
                if target is None or target is m or not self._scene(run,target):continue
                events = run.deaths.setdefault(target.selection[0],{})
                events.setdefault(identity,(target,run.cycle,actual))

    def _drain_deaths(self,session,client):
        with self._metadata():
            run = self._current(session)
            if run is None or not run.active:return
            m = self._member(run,session)
            if not self._scene(run,m) or m.selection[0] not in run.cycle.loaded:return
            events = run.deaths.get(m.selection[0], {})
            pending = tuple(events.items())
        for identity, (_, cycle, row) in pending:
            try:
                with self._metadata():
                    if (not self._scene(run, m) or run.cycle is not cycle or row.get('killed')
                            or SharedRunWorld.room_key(session) != identity.room_key
                            or run.world.actor_identity(session, m.identity, row) is not identity):
                        continue
                # The recipient's native handler owns its XP, quests and drops.
                self._scope.death = (session, run, m, row, identity)
                packets, _ = self.server._handle_die_monster_0027(b'', session=session)
                for packet in packets:
                    client.sendall(packet)
            finally:
                self._scope.death = None
                # Keep the door barrier in place until this delivery finishes.
                with self._metadata():
                    events.pop(identity, None)

    def death_row(self,session):
        value = getattr(self._scope,'death',None)
        if value is None or value[0] is not session:return None
        _,run,m,row,identity = value
        with self._metadata():
            if (not run.active or not self._scene(run,m) or
                    run.world.actor_identity(session,m.identity,row) is not identity):
                raise RaidError('shared death callback lost its actual row binding')
        return row


def _service(server):
    return getattr(getattr(server,'normal_parties',None),'runs',None)

def entry_plan_for(server,session,dungeon,start,seed,difficulty,entry_option):
    s=_service(server)
    return s.entry_plan(session,dungeon,start,seed,difficulty,entry_option) if s else None

def map_actor_index_for(server,session,default=0):
    s=_service(server)
    if s is None or s._current(session) is None:
        s=getattr(server,'raid_squad_runs',None)
    return s.map_actor_index(session,default) if s else default

def bind_room(server,session):
    s=_service(server)
    return s.bind_room(session) if s else None

def room_definition_for(server,session,x,y,room):
    s=_service(server)
    if s is None or s._current(session) is None:
        s=getattr(server,'raid_squad_runs',None)
    return s.room_definition(session,x,y,room) if s else room

def room_plan_for(server,session,key):
    s=_service(server)
    return s.room_plan(session,key) if s else None

def note_actor_death(server,session,row):
    s=_service(server)
    if s:s.note_death(session,row)

def owned_death_row(server,session):
    s=_service(server)
    return s.death_row(session) if s else None
