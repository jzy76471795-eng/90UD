"""Recipient-owned scene coordination for a real prepared raid squad.

Reuse ordinary-party staging/loading/peer publication, while all dungeon
handlers stay inside RaidTransportHooks. The raid bridge retains its actual
run ticket, shared actor identities, progression and durable rewards. No
resource factory, admission or socket write runs under this coordinator lock.
"""
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from game.chain_loader import load_chain
from game.normal_party_run import NormalPartyRuns, Participant, Run, Cycle, Ticket
from game.normal_party_profiles import build_profiles
from .dungeon_adapter import HatcheryClosed, OwnerIdentity
from .runtime import RaidError


class _Server:
    _HANDLERS = {'_handle_enter_select_dungeon_000f': 0x0f,
                 '_handle_select_dungeon_0010': 0x10,
                 '_handle_finish_loading_0025': 0x25,
                 '_handle_move_map_002d': 0x2d,
                 '_handle_back_2_village_0084': 0x84,
                 '_handle_opcode_0048': 0x48}

    def __init__(self, hooks):
        self.hooks, self.original = hooks, hooks.battle.service.server

    def __getattr__(self, name):
        handler = getattr(self.original, name)
        opcode = self._HANDLERS.get(name)
        if opcode is None:
            return handler
        def owned(body, *, session):
            return self.hooks.handle_opcode(opcode, handler, body, session=session,
                                            preserve_hatchery_rejection=opcode == 0x10)
        return owned


@dataclass(eq=False)
class _HatcheryCycle(Cycle):
    source: tuple = ()
    source_run: object = None
    requester: object = None


class _Parties:
    def __init__(self, hooks):
        self.service = hooks.battle.service
        self.server, self._lock, self.clock = _Server(hooks), RLock(), monotonic
        self.returns = SimpleNamespace(pending={})
        self.helper = load_chain('helper-party_info.py')

    def _live(self, selection):
        try:
            return self.service.server.native_game_connections.chat_selection(selection[0].session) == selection
        except RaidError:
            return False

    def _roster(self, party):
        h = party.header
        host = next(seat.slot for seat in party.members if seat.character_id == party.host_character_id)
        return self.helper.build_current_namespace_party_slots(party_id=party.party_id,
            members=tuple((seat.slot, seat.character_id, (0, 0, 1)) for seat in party.members),
            user_max=4, dungeon_id=h.dungeon_id, difficulty=h.difficulty,
            info_field=h.info_field, more=h.more, aux=h.aux, aux2=h.aux2, tail=(0, 0, host, 0))


class _RecipientSender:
    def __init__(self, runs, session, client):
        self.runs, self.session, self.client = runs, session, client

    def sendall(self, packet, *, on_sent=None):
        from .local_bindings import send_game_packet
        send_game_packet(self.runs.original, self.session, self.client, packet, on_sent=on_sent)


@dataclass(eq=False)
class _CompletedReturn:
    run: object
    member: object
    generation: int
    opcode: int
    body: bytes
    followers: tuple = ()
    packets: object = None
    sent: int = 0
    pump_owned: bool = False
    board_return: bool = False


@dataclass(eq=False)
class _ExpiredReturn:
    run: object
    member: object
    generation: int
    packets: tuple = ()
    sent: int = 0


class _ExpiredReturns:
    """Retire a forced exit only after this recipient's original TCP frames."""
    def __init__(self, runs):
        self.runs, self.pending = runs, {}

    def _valid(self, plan, session):
        runs, member = self.runs, plan.member
        return (member.session is session and runs.server.account_id == member.account_id
                and runs._owned(plan.run, member) and runs._valid(plan.run)
                and plan.run.generations.get(member.selection[0]) == plan.generation
                and getattr(session, 'dungeon_run_generation', None) == plan.generation)

    def prepare(self, session):
        runs = self.runs
        if not runs.hooks.bridge.expired_run(session):
            raise RaidError('forced raid return requires an actually expired owned run')
        with runs._metadata():
            run = runs._current(session)
            if run is None:
                return None
            member = runs._member(run, session)
            plan = _ExpiredReturn(run, member, run.generations.get(member.selection[0]))
            if not self._valid(plan, session):
                raise RaidError('forced raid return lost its actual owner or generation')
            self.pending[id(session)] = plan
        try:
            packets, note = runs.server._handle_back_2_village_0084(b'', session=session)
        except BaseException:
            self.pending.pop(id(session), None)
            raise
        retired = runs.hooks.bridge.current(session) is None
        with runs._metadata():
            if (not self._valid(plan, session) or not packets
                    or getattr(session, 'in_dungeon_run', False)
                    or getattr(session, 'in_dungeon_select', False)
                    or not retired):
                self.pending.pop(id(session), None)
                raise RaidError('forced raid return did not produce its original town responses')
            plan.packets = tuple(packets)
        return list(plan.packets), note

    def note_send(self, session, packet):
        runs = self.runs
        with runs._metadata():
            plan = self.pending.get(id(session))
            if plan is None:
                return
            if not self._valid(plan, session):
                self.pending.pop(id(session), None)
                return
            if (plan.sent >= len(plan.packets) or plan.packets[plan.sent] is not packet
                    or getattr(session, 'in_dungeon_run', False)
                    or getattr(session, 'in_dungeon_select', False)):
                return
            plan.sent += 1
            if plan.sent != len(plan.packets):
                return
            self.pending.pop(id(session), None)
            # Metadata only: retain raid membership, admission and all survivors.
            runs.detach(session)

    def pump(self, session, client):
        runs = self.runs
        with runs._metadata():
            plan = self.pending.get(id(session))
            if plan is None:
                return
            if not self._valid(plan, session) or plan.member.selection[0].client is not client:
                self.pending.pop(id(session), None)
                return
            packets = plan.packets[plan.sent:]
        sender = runs._sender(plan.member, client)
        for packet in packets:
            sender.sendall(packet, on_sent=lambda sent: self.note_send(session, sent))


class _CompletedReturns:
    """Squad returns committed only by exact ordered recipient TCP receipts."""
    def __init__(self, runs):
        self.runs = runs
        self.pending, self.jobs = {}, {}

    def _valid(self, plan, *, completed=False):
        runs, member = self.runs, plan.member
        session = member.session
        return (runs._owned(plan.run, member) and runs._valid(plan.run)
                and runs._current(session) is plan.run
                and plan.run.generations.get(member.selection[0]) == plan.generation
                and getattr(session, 'dungeon_run_generation', None) == plan.generation
                and (not completed or (getattr(session, 'in_dungeon_run', False)
                     and (plan.board_return or int(getattr(session, 'dungeon_clear_phase', 0) or 0) >= 2))))

    def forget(self, session):
        with self.runs._metadata():
            self.pending.pop(id(session), None)
            self.jobs.pop(id(session), None)

    def _handler(self, opcode):
        server = self.runs.server
        return server._handle_opcode_0048 if opcode == 0x48 else server._handle_back_2_village_0084

    def route(self, opcode, body, session):
        if opcode not in (0x48, 0x84):
            return None
        raw = bytes(body)
        board_return = False
        if opcode == 0x48:
            from game.chain_loader import load_chain
            state, option = load_chain('opcode0048-eplp_command.py')._parse_eplp(raw)
            if state != 1 or option not in (0, 1, 2):
                return None
            # The battle board's return button is C0048(1,2) while the
            # dungeon is still active. Menu giveup/0084 keeps its own path.
            board_return = option == 2 and int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2
        runs = self.runs
        with runs._metadata():
            run = runs._current(session)
            prior = self.pending.get(id(session))
            if prior is not None and self._valid(prior):
                return ([] if prior.packets is None else list(prior.packets[prior.sent:])), 'raid squad return awaits exact send receipts'
            if (run is None or not getattr(session, 'in_dungeon_run', False)
                    or not board_return and int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2):
                return None
            member = runs._member(run, session)
            if not board_return and run.party.host_character_id != member.character_id:
                return None  # Individual/menu/settlement return retains its own path.
            if runs.server.account_id != member.account_id:
                raise RaidError('completed raid return requires its owning account thread')
            members = runs._members(run)
            plans = tuple(_CompletedReturn(run, peer, run.generations.get(peer.selection[0]),
                0x84 if board_return and peer is not member else opcode,
                b'' if board_return and peer is not member else bytes((state, option)) if opcode == 0x48 else b'',
                pump_owned=peer is not member, board_return=board_return) for peer in members)
            if not all(self._valid(plan, completed=True) for plan in plans):
                raise RaidError('completed raid squad return awaits every actual member result')
            plan = next(plan for plan in plans if plan.member is member)
            plan.body = raw
            plan.followers = tuple(peer for peer in plans if peer is not plan)
            self.pending[id(session)] = plan
        try:
            if board_return:
                packets, note = self._handler(0x84)(b'', session=session)
                if packets:
                    packets = [runs.original._codec_response(1, 0x48, b'\x01\x01\x02'), *packets]
                note = 'native raid board whole-squad return; ' + note
            else:
                packets, note = self._handler(opcode)(raw, session=session)
        except BaseException:
            self.forget(session)
            raise
        with runs._metadata():
            empty_committed = (not packets and not getattr(session, 'in_dungeon_run', False)
                and not getattr(session, 'in_dungeon_select', False))
            accepted = (self.pending.get(id(session)) is plan and self._valid(plan)
                and not getattr(session, 'in_dungeon_run', False)
                and not getattr(session, 'in_dungeon_select', False) and bool(packets))
            if accepted:
                plan.packets = tuple(packets)
            elif self.pending.get(id(session)) is plan:
                self.pending.pop(id(session), None)
        if empty_committed:
            raise RaidError('native completed return changed scene without wire responses; squad return not committed')
        return packets, note + '; completed squad return waits for original recipient TCP receipts'

    def note_send(self, session, packet):
        runs = self.runs
        finished = False
        with runs._metadata():
            plan = self.pending.get(id(session))
            if (plan is None or plan.pump_owned or plan.member.session is not session or plan.packets is None
                    or runs.server.account_id != plan.member.account_id):
                return
            if not self._valid(plan):
                self.pending.pop(id(session), None)
                return
            if plan.packets[plan.sent] is not packet:
                return
            plan.sent += 1
            if plan.sent != len(plan.packets):
                return
            self.pending.pop(id(session), None)
            for follower in plan.followers:
                if self._valid(follower, completed=True):
                    self.jobs.setdefault(id(follower.member.session), follower)
            finished = True
        if finished:
            # Do not acquire the transport/bridge locks while holding scene metadata.
            runs.confirm_town(session)

    def _follower_sent(self, plan, session, packet):
        """Direct callback at this recipient's successful TCP write boundary."""
        runs = self.runs
        with runs._metadata():
            if (not plan.pump_owned or self.jobs.get(id(session)) is not plan
                    or self.pending.get(id(session)) is not plan
                    or plan.member.session is not session
                    or runs.server.account_id != plan.member.account_id
                    or not self._valid(plan) or plan.packets is None
                    or plan.sent >= len(plan.packets)
                    or plan.packets[plan.sent] is not packet):
                return
            plan.sent += 1

    def pump(self, session, client):
        runs = self.runs
        with runs._metadata():
            plan = self.jobs.get(id(session))
            if plan is None:
                return
            member = plan.member
            if (member.session is not session or member.selection[0].client is not client
                    or runs.server.account_id != member.account_id
                    or not self._valid(plan, completed=plan.packets is None)):
                self.jobs.pop(id(session), None)
                if self.pending.get(id(session)) is plan:
                    self.pending.pop(id(session), None)
                return
            self.pending[id(session)] = plan
        if plan.packets is None:
            try:
                packets, _note = self._handler(plan.opcode)(plan.body, session=session)
            except BaseException:
                self.forget(session)
                raise
            with runs._metadata():
                empty_committed = (not packets and not getattr(session, 'in_dungeon_run', False)
                    and not getattr(session, 'in_dungeon_select', False))
                if (not self._valid(plan) or getattr(session, 'in_dungeon_run', False)
                        or getattr(session, 'in_dungeon_select', False) or not packets):
                    self.forget(session)
                    if empty_committed:
                        raise RaidError('native follower return changed scene without wire responses; recipient return not committed')
                    return
                plan.packets = tuple(packets)
        # Each original frame retains its identity through a partial-send retry.
        sender = runs._sender(member, client)
        for packet in plan.packets[plan.sent:]:
            sender.sendall(packet, on_sent=lambda sent: self._follower_sent(plan, session, sent))
        finished = False
        with runs._metadata():
            if plan.sent == len(plan.packets) and self.jobs.get(id(session)) is plan:
                self.jobs.pop(id(session), None)
                if self.pending.get(id(session)) is plan:
                    self.pending.pop(id(session), None)
                finished = True
        if finished:
            runs.confirm_town(session)


class RaidSquadRuns(NormalPartyRuns):
    def __init__(self, hooks):
        self.hooks, self.service = hooks, hooks.battle.service
        self.original = self.service.server
        super().__init__(_Parties(hooks))
        self.completed_returns = _CompletedReturns(self)
        self.expired_returns = _ExpiredReturns(self)
        self._entry_replays = {}
        self._party_updates = {}
        self.parties.returns = self.completed_returns
        if getattr(self.service.native_lobby, '_squad_returns', None) is not None:
            raise RaidError('raid squad return receipt observer already installed')
        self.service.native_lobby._squad_returns = self.completed_returns
        self.profile_builder = lambda _server, member: build_profiles(self.original, member)

    def _valid(self, run):
        directory = getattr(self.service, 'ordinary_party', None)
        entry, party = run.party.entry, run.party
        current = directory._parties.get((id(entry), party.group)) if directory is not None else None
        if (not run.active or current is None or self.service.raids.get(entry.domain.raid_key) is not entry
                or current.generation != party.generation or current.party_id != party.party_id):
            return False
        survivors = self._members(run)
        if current is not party:
            if not run.departed:
                return False
            if len(current.members) != len(survivors):
                return False
        valid = all(self._live(m) and self.service.current(m.character_id) is entry
            and getattr(m.session, '_raid_selected_epoch', None) is m.identity.generation
            and any(seat.session is m.session and seat.slot == m.slot
                and seat.epoch is m.identity.generation for seat in current.members) for m in survivors)
        if valid:
            run.party = current
            if current is not party:
                for member in survivors:
                    key = id(member.session)
                    pending = self._party_updates.get(key)
                    host_changed = current.host_character_id != party.host_character_id
                    if pending is not None and pending[0] is run:
                        host_changed = host_changed or pending[2]
                    self._party_updates[key] = (run, current, host_changed)
        return valid

    def _start(self, body, session):
        if getattr(session, 'selected_channel_id', None) not in (23, 32):
            return None
        with self._metadata():
            current = self._current(session)
        if current is not None:
            if current.active and current.dungeon_id == 218:
                target = self.hooks.native_entry_target(session)
                if target in range(221, 225):
                    return self._start_hatchery(body, session, current)
                if target is None and self.hooks.has_native_entry_intent(session):
                    # Let native SELECT reject an unconfirmed portal and
                    # release the client's waiting UI, as on the solo path.
                    return None
            if getattr(session, 'in_dungeon_run', False) or current.active:
                return [], 'raid squad entry already queued or active'
            return [], 'raid squad cancellation pending'
        ordinary = getattr(self.service, 'ordinary_party', None)
        if ordinary is None:
            return None
        party = ordinary.current(session, auto=False)
        if party is None or len(party.members) < 2:
            return None
        if party.host_character_id != session.selected_character_id:
            raise RaidError('only the actual raid squad host may choose its dungeon')
        raw = bytes(body)
        parsed = load_chain('flow-dungeon_enter.py').parse_0010_plain(raw)
        dungeon = parsed['dungeon_id']
        if type(dungeon) is not int or not 1 <= dungeon <= 65535 or parsed['difficulty'] not in range(6):
            raise RaidError('invalid raid squad dungeon selection')
        if party.entry.domain.battle is None:
            raise RaidError('raid admission must finish before squad dungeon entry')
        connections = self.original.native_game_connections
        members = tuple(Participant(connections.chat_selection(seat.session), seat.slot,
            seat.character_id, seat.account_id,
            OwnerIdentity(seat.character_id, str(seat.account_id),
                seat.session.selected_channel_id, seat.epoch)) for seat in party.members)
        if any(getattr(m.session, 'in_dungeon_run', False) for m in members):
            waiting = [m for m in members if getattr(m.session, 'in_dungeon_run', False)]
            if all((plan := self.completed_returns.jobs.get(id(m.session))) is not None
                   and plan.run.party is party for m in waiting):
                old = self._entry_replays.get(id(session))
                replay = (session, party, connections.chat_selection(session), raw)
                if old is not None and old[:4] != replay:
                    raise RaidError('raid retry changed while waiting for actual squad departure')
                if old is None:self._entry_replays[id(session)] = replay + (monotonic()+30,)
                return [], 'raid retry waits for every actual completed departure'
            raise RaidError('every raid squad member must leave its previous dungeon first')
        if not ordinary.ready(session):
            raise RaidError('raid squad peer publication is not ready')
        cycle = Cycle('entry', raw, monotonic()+30, first=members[0])
        run = Run(party, members, dungeon, None, Ticket(uuid4().hex, dungeon, members), cycle)
        # Serialize the final roster check and reservation with a manager edit.
        with self.service._lock, self._metadata():
            if not self._valid(run) or any(self._current(m.session) is not None for m in members):
                raise RaidError('raid squad membership changed during entry preflight')
            for member in members:
                self._runs[id(member.session)] = (member.session, run)
                self._queue(member, 'prepare', run, cycle)
        return [], 'raid squad entry queued on each actual account connection'

    def _start_hatchery(self, body, session, current):
        with self._metadata():
            if not self._valid(current):
                raise RaidError('raid squad membership changed before hatchery entry')
            first = self._member(current, session)
            if current.party.host_character_id != first.character_id:
                return [], 'raid hatchery entry waits for the squad host'
            members = self._members(current)
            old_cycle = current.cycle
            if current.move_intent is not None or old_cycle.projected != self._connections(current):
                raise RaidError('raid squad must finish loading the Energy entrance first')
        source = self.hooks.squad_hatchery_source(session, tuple(m.session for m in members))
        _, dungeon, room = source
        raw = bytes(body)
        parsed = load_chain('flow-dungeon_enter.py').parse_0010_plain(raw)
        requested = parsed['dungeon_id']
        if (parsed['difficulty'] not in range(6)
                or requested not in (0, dungeon) and not (requested and requested > 0x1000)):
            raise RaidError('raid squad SELECT disagrees with its hatchery entrance')
        cycle = _HatcheryCycle('entry', raw, self.parties.clock() + 30, first=first, source=source,
                               source_run=current, requester=first)
        run = Run(current.party, members, dungeon, None, Ticket(uuid4().hex, dungeon, members), cycle)
        with self._metadata():
            if (current.cycle is not old_cycle or old_cycle.room != room or not self._valid(current)
                    or current.move_intent is not None or self._members(current) != members
                    or any(not self._scene(current, member) for member in members)):
                raise RaidError('raid squad room changed before hatchery entry')
            current.active = False
            for member in members:
                self._jobs.pop(member.selection[0], None)
                self._runs[id(member.session)] = (member.session, run)
                self._queue(member, 'prepare', run, cycle)
        return [], 'raid squad hatchery entry queued on each actual account connection'

    def route(self, opcode, body, session):
        if opcode in (0x07, 0x57f):
            self.forget(session)
            return None
        if opcode == 0x2d and len(body) >= 2:
            with self._metadata():
                run = self._current(session)
                cells = getattr(session, 'dungeon_required_cells', ())
                if run is not None and cells and tuple(bytes(body)[:2]) not in cells:
                    self._member(run, session)
                    return [self.server._codec_response(1, 0x2d, b'\0')], 'raid door destination outside actual maze; native wait released'
        result = self.completed_returns.route(opcode, body, session)
        if result is not None:
            return result
        return super().route(opcode, body, session)

    def forget(self, session):
        self._entry_replays.pop(id(session), None)
        self.expired_returns.pending.pop(id(session), None)
        self.completed_returns.forget(session)
        return super().forget(session)

    def pump(self, session, client):
        if self.original.account_id != getattr(session, 'account_id', None):
            return
        self.expired_returns.pump(session, client)
        self.completed_returns.pump(session, client)
        replay = self._entry_replays.get(id(session))
        if replay is not None:
            owner, party, selection, raw, deadline = replay
            ordinary = self.service.ordinary_party
            if (owner is not session or monotonic() >= deadline
                    or self.original.native_game_connections.chat_selection(session) != selection
                    or ordinary.current(session, auto=False) is not party):
                self._entry_replays.pop(id(session), None)
            elif (not any(getattr(seat.session, 'in_dungeon_run', False) for seat in party.members)
                    and ordinary.ready(session)):
                self._entry_replays.pop(id(session), None)
                packets, _note = self._start(raw, session)
                for packet in packets:self._sender(self._member(self._current(session),session),client).sendall(packet)
        context = self.hatchery_departures(session)
        if context is not None:
            for packet in self.hooks.refresh_hatchery_entry(session, *context):
                self._sender(self._member(self._current(session), session), client).sendall(packet)
        result = super().pump(session, client)
        self._publish_active_party(session, client)
        return result

    def _publish_active_party(self, session, client):
        key = id(session)
        with self._metadata():
            pending = self._party_updates.get(key)
            if pending is None:
                return
            run, party, host_changed = pending
            if self._current(session) is not run or not run.active:
                self._party_updates.pop(key, None)
                return
            member = self._member(run, session)
            if (member.selection[0].client is not client or not self._scene(run, member)
                    or member.selection[0] not in run.cycle.projected or run.party is not party):
                return
            roster = self.parties._roster(party)
            host_slot = next(seat.slot for seat in party.members if seat.character_id == party.host_character_id)
        sender = self._sender(member, client)
        sender.sendall(self.original._codec_response(0, 9, roster))
        if host_changed:
            sender.sendall(self.original._codec_response(0, 0x1a, bytes((host_slot,))))
        with self._metadata():
            if self._party_updates.get(key) is pending:
                self._party_updates.pop(key, None)

    def detach(self, session):
        # A portal handoff can retain its current Energy survivors. Normal
        # first entry still requires every member of the frozen roster.
        with self._metadata():
            self._party_updates.pop(id(session), None)
            run = self._current(session)
            if run is not None and isinstance(run.cycle, _HatcheryCycle):
                return super().detach(session, continue_entry=True)
            if run is not None and run.active:
                records = [self.hooks.bridge._records.get(id(m.session)) for m in self._members(run)]
                if any(r is not None and not self.hooks.bridge._groups[r.binding.ticket.run_id].accepted
                       for r in records):
                    self._cancel(run, 'raid member left before initial loading acceptance')
            return super().detach(session, continue_move=True)

    def hatchery_departures(self, session):
        with self._metadata():
            run = self._current(session)
            if (run is None or not run.active or not run.departed
                    or not isinstance(run.cycle, _HatcheryCycle)):
                return None
            source, destination, _ = run.cycle.source
            return source, destination, tuple(m.character_id for m in run.participants
                                              if m.selection[0] in run.departed)

    def confirm_town(self, session):
        if self.hooks.bridge.current(session) is None and not getattr(session, 'in_dungeon_run', False):
            self.detach(session)

    def _sender(self, member, client):
        return _RecipientSender(self, member.session, client)

    def _prepare(self, member, run, cycle, client):
        if isinstance(cycle, _HatcheryCycle):
            with self._metadata():
                if (not self._owned(run, member) or member.selection[0] in cycle.prepared
                        or member.selection[0] in cycle.preparing):
                    return
                if member is not cycle.first and cycle.first.selection[0] not in cycle.prepared:
                    self._queue(member, 'prepare', run, cycle)
                    return
            try:
                self.hooks.arm_squad_hatchery(member.session, cycle.source)
                return super()._prepare(member, run, cycle, client)
            except HatcheryClosed:
                if not self._restore_energy_before_handoff(run, cycle):
                    raise
                if member is cycle.requester:
                    self._sender(member, client).sendall(self.original._codec_response(1, 0x10, b'\0\x13\0'))
                return
        return super()._prepare(member, run, cycle, client)

    def _commit_prepared(self, run, cycle):
        if isinstance(cycle, _HatcheryCycle):
            # The first prepared connection may leave before its peers begin.
            # Preserve the Energy run until every remaining connection moves.
            cycle.source_run = None
        super()._commit_prepared(run, cycle)

    def _restore_energy_before_handoff(self, run, cycle):
        with self._metadata():
            source = cycle.source_run
            members = self._members(run)
            if (source is None or not run.active or run.cycle is not cycle or cycle.preparing
                    or cycle.prepared or cycle.committed or not members
                    or any(self._current(m.session) is not run
                           or getattr(m.session, 'selected_dungeon_id', None) != source.dungeon_id
                           or source.generations.get(m.selection[0]) != getattr(m.session, 'dungeon_run_generation', None)
                           for m in members)):
                return False
            if any(self._current(m.session) is not None for m in run.participants if m.selection[0] in run.departed):
                return False
            run.active = False
            source.active = True
            for member in run.participants:
                self._runs[id(member.session)] = (member.session, source)
                if member.selection[0] in run.departed:
                    super().detach(member.session)
                else:
                    self._jobs.pop(member.selection[0], None)
            return True

    def _stage_start(self, m, run, cycle, client):
        return super()._stage_start(m, run, cycle, self._sender(m, client))

    def _send_start(self, m, run, cycle, client):
        return super()._send_start(m, run, cycle, self._sender(m, client))

    def _loading(self, m, run, cycle, body, client):
        return super()._loading(m, run, cycle, body, self._sender(m, client))

    def _release_loading(self, m, run, cycle, client):
        return super()._release_loading(m, run, cycle, self._sender(m, client))

    def _project(self, m, run, cycle, client):
        return super()._project(m, run, cycle, self._sender(m, client))

    def _restore(self, m, run, client):
        return super()._restore(m, run, self._sender(m, client))

    def _drain_deaths(self, session, client):
        # RaidTransportHooks drains typed shared deaths on the recipient loop.
        return


def route(server, opcode, body, session):
    runs = getattr(server, 'raid_squad_runs', None)
    if runs is None or session is None:
        return None
    try:
        return runs.route(opcode, body, session)
    except (RaidError, ValueError) as exc:
        packets = [server._codec_response(1, 0x10, b'\0\x13\0')] if opcode == 0x10 else []
        return packets, 'raid squad rejected: ' + str(exc)
