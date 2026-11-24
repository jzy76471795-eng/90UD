"""Reviewed US dungeon hooks; only the real socket sender confirms receipts.

The native helpers keep packet encoding, room state and inventory ownership.
Install guard_entry in their resolved-ID branch and the two actor hooks in
their accepted 0027 branches. This module never parses client completion flags.
"""

from dataclasses import dataclass, field
from threading import RLock
from uuid import uuid4

from .dungeon_adapter import BridgeError
from .runtime import RaidError, RaidEventExpired
from .shared_world import SharedRunWorld


@dataclass(frozen=True)
class LifeEvent:
    event_id: str
    binding: object
    run_generation: int
    life_sequence: int
    kind: str


@dataclass(frozen=True)
class SendReceipt:
    capability: str
    session: object
    binding: object
    request_opcode: int
    packet: bytes
    packet_key: tuple[int, int]
    room_state: object = None
    life_event: object = None
    loading_cycle: object = None


@dataclass
class _Scope:
    session: object
    opcode: int
    generation: int
    entry: object = None
    deaths: dict = field(default_factory=dict)
    world: object = None
    replaced_binding: object = None


@dataclass
class _Group:
    raid: object
    ticket: object
    sessions: dict = field(default_factory=dict)
    accepted: bool = False
    world: object = None


@dataclass
class _Life:
    sequence: int = 0
    pending_event: object = None
    death_sent: bool = False


@dataclass(frozen=True)
class _Notification:
    session: object
    raid: object
    owner: object
    selected_epoch: object
    batch: list


@dataclass(frozen=True)
class _SharedDeath:
    session: object
    binding: object
    selected_epoch: object
    room: object
    row: object
    identity: object


_RELEVANT = {
    0x0010: {(0, 0x001C), (0, 0x001D)},
    0x0025: {(1, 0x0025), (0, 0x001E)},
    0x0027: {(0, 0x001F)},
    0x002D: {(0, 0x001D)},
    0x002E: {(0, 0x0022), (1, 0x0045), (1, 0x0046)},
    0x0028: {(0, 0x0020)},
    0x0029: {(0, 0x0020)},
}


class RaidTransportHooks:
    def __init__(self, battle, *, packet_key):
        if not callable(packet_key) or not hasattr(battle, "bridge"):
            raise BridgeError("battle adapter and native packet-key reader required")
        self.battle, self.bridge, self._packet_key = battle, battle.bridge, packet_key
        self._scopes, self._groups, self._receipts, self._pending, self._lives = {}, {}, {}, {}, {}
        self._notifications = {}
        self._shared_deaths = {}
        self._hatchery_warps = {}
        self._rejected_entries = {}
        self._lock = RLock()
        self.bridge._shared_actor_resolver = self.resolve_shared_actor

    def prepare_entry_plan(self, session, start, seed, difficulty, entry_option):
        """Inside real0010, before maze/seed/actor encoding can diverge."""
        with self._lock:
            # Ordinary tutorials can be entered by the server after START/0023;
            # they have no client 0010 scope and no shared raid world to plan.
            if not self._raid_channel(session):
                return None
            scope = self._scopes.get(id(session))
            if scope is None or scope.session is not session or scope.opcode != 0x0010:
                raise BridgeError('shared entry plan requires the actual0010 scope')
            if scope.entry is None:
                return None  # Ordinary channel, unchanged.
            raid, dungeon, ids, ticket, group_key = scope.entry
            group = self._groups.get(group_key)
            if group is not None:
                if group.ticket is not ticket or group.world is None:
                    raise BridgeError('shared follower has no matching authoritative world')
                world = group.world
            else:
                world = SharedRunWorld(dungeon, ids)
                maze_index = raid.progression.sanctum_maze(dungeon) if raid.family == 'luke' else None
                if maze_index is not None:
                    from game.chain_loader import load_chain
                    helper = load_chain('helper-dungeon_start_room.py')
                    info = helper.get_quest_index().get_dungeon(dungeon)
                    if not 0 <= maze_index < len(info.mazes):
                        raise BridgeError('Luke card record exceeds actual maze resource')
                    maze = helper._load_maze_details(dungeon, maze_index)
                    if maze is None or len(maze.start_maps) != 1:
                        raise BridgeError('Luke card maze requires its actual start cell')
                    x, y = maze.start_maps[0][:2]
                    actual = helper.resolve_dungeon_room(dungeon, x, y, difficulty=difficulty, maze_index=maze_index)
                    start = dict(start, **actual)
                    start.update(start_x=x, start_y=y, room_cells=helper.maze_room_cells(maze),
                                 reason='luke_card_set_accepted_clear_ordinal', maze_name=maze.name)
            plan = world.entry_plan(session, self.bridge._owner(session), start, seed, difficulty, entry_option)
            scope.world = world
            return plan

    def _world_for_scope(self, session):
        if not self._raid_channel(session):
            return None
        scope = self._scopes.get(id(session))
        if scope is None or scope.session is not session:
            raise BridgeError('shared room requires an actual ordinary request scope')
        if scope.world is not None:
            return scope.world
        binding = self.bridge.current(session)
        return self._group(binding).world if binding is not None else None

    def prepare_room_plan(self, session, room_key):
        with self._lock:
            world = self._world_for_scope(session)
            return world.room_plan(session, self.bridge._owner(session), room_key) if world is not None else None

    def bind_shared_room(self, session):
        """Bind actual cached PVF rows before recipient-specific drop preload."""
        with self._lock:
            world = self._world_for_scope(session)
            return world.bind_room(session, self.bridge._owner(session)) if world is not None else None

    def resolve_shared_actor(self, session, binding, row):
        # Called under the bridge lock. Do not acquire the transport lock in
        # this direction: ordinary wrappers already hold it before bridge calls.
        try:
            group = self._group(binding)
        except BridgeError:
            return None
        world = group.world
        if world is None:
            return None
        if world.ticket is not binding.ticket:
            raise BridgeError('shared actor belongs to another ticket')
        return world.actor_identity(session, binding.owner, row)

    def _raid_channel(self, session):
        return self.battle.selector_dungeon_ids(session) is not None

    def spend_revive_coin(self, session, spend):
        if getattr(session, 'selected_channel_id', None) not in (23, 32):
            return spend()
        with self._lock:
            binding = self.bridge.current(session)
            if binding is None:
                raise BridgeError('raid revive has no current run')
            self._valid_binding(session, binding)
            life = self._lives[binding.capability]
            if not self._group(binding).accepted or not life.death_sent or life.pending_event is None:
                raise BridgeError('raid revive requires its actually sent death transition')
            return self.battle.spend_raid_revive(session, binding, spend)

    def filter_permission_entries(self, session, rows, difficulty_for_dungeon):
        """Before native S0/0005 encoding; use native PVF difficulty function."""
        ids = self.battle.selector_dungeon_ids(session)
        if ids is None:
            return [row for row in rows if not self.battle.is_raid_dungeon(int(row[0]))]
        existing, result, seen = dict(rows), [], set()
        for value in ids:
            dgn = int(value)
            if dgn in seen:
                continue
            if not self.battle.is_raid_dungeon(dgn):
                raise BridgeError("raid selector contains a dungeon outside the real catalog")
            seen.add(dgn)
            result.append((dgn, existing[dgn] if dgn in existing else difficulty_for_dungeon(dgn)))
        return result

    def guard_preselect(self, session, dungeon_id):
        """Before ordinary 000F soft-preselect mutates selected_dungeon_id."""
        ids, dgn = self.battle.selector_dungeon_ids(session), int(dungeon_id)
        if ids is None:
            if self.battle.is_raid_dungeon(dgn):
                raise BridgeError("raid dungeon selected from ordinary channel")
        elif dgn not in ids:
            with self._lock:
                raid = self.battle.raid_for_session(session)
                if raid is None or raid.battle is None:
                    raise BridgeError("raid dungeon is not open in this owned battle")
                participants = tuple(self.battle.prepare_participants(session))
                group = self._groups.get(self._group_key(raid, dgn, participants))
                if (group is None or group.accepted or not any(
                        self.bridge.current(s) is b for s, b in group.sessions.values())):
                    raise BridgeError("raid dungeon is not open in this owned battle")
                self.battle.guard_dungeon_entry(session, dgn, shared_ticket=group.ticket)

    @staticmethod
    def _group_key(raid, dungeon, participants):
        battle, program = raid.battle, raid.progression
        return (id(raid), battle.instance_id, battle.phase, program.epoch if program else 1,
                int(dungeon), tuple(sorted(participants)))

    def guard_entry(self, session, dungeon_id):
        """Inside native 0010 after soft-preselect/link resolution, before run commit."""
        with self._lock:
            dgn = int(dungeon_id)
            if not self._raid_channel(session):
                if self.battle.is_raid_dungeon(dgn):
                    raise BridgeError("raid dungeon entered from ordinary channel")
                return None
            scope = self._scopes.get(id(session))
            if scope is None or scope.session is not session or scope.opcode != 0x0010:
                raise BridgeError("raid entry requires the reviewed native 0010 wrapper")
            raid = self.battle.raid_for_session(session)
            if raid is None or raid.battle is None:
                raise BridgeError("actual owned started raid unavailable")
            ids = tuple(self.battle.prepare_participants(session))
            if not ids or len(set(ids)) != len(ids):
                raise BridgeError("actual admitted group participants unavailable")
            group_key = self._group_key(raid, dgn, ids)
            group = self._groups.get(group_key)
            warp = self._hatchery_warps.get(id(session))
            if (group is not None and warp is not None and warp[3] is None
                    and group.ticket is warp[1].ticket):
                if len(ids) != 1:
                    raise BridgeError('active same-dungeon party retry requires coordinated departure')
                group = None  # A solo board retry needs a new world, not the old accepted one.
            if group is not None and not any(self.bridge.current(s) is b for s, b in group.sessions.values()):
                self._groups.pop(group_key, None)
                group = None
            ticket = group.ticket if group is not None and not group.accepted else None
            checked_raid, checked_ids = self.battle.guard_dungeon_entry(session, dgn, shared_ticket=ticket)
            if checked_raid is not raid or tuple(sorted(checked_ids)) != tuple(sorted(ids)):
                raise BridgeError("entry guard changed the owned raid/group snapshot")
            if warp is not None:
                if (warp[0] is not session or not warp[4] or dgn != warp[2]
                        or (warp[3] is not None and
                            self.bridge.hatchery_portal(session, warp[1]) != warp[2:4])):
                    raise BridgeError('native navigation SELECT differs from its accepted target')
                if warp[1].ticket.dungeon_id == dgn:
                    batch = self._retire(session, warp[1], 'replaced_run')
                    if batch:
                        self._queue_notification(session, batch)
                else:
                    scope.replaced_binding = warp[1]
            scope.entry = (raid, dgn, ids, ticket, group_key)
            return scope.entry

    def _room(self, session):
        key = (int(session.dungeon_maze_index), int(session.dungeon_cell_x),
               int(session.dungeon_cell_y), int(session.dungeon_map_index))
        states = getattr(session, "dungeon_room_states", {})
        return states.get(key)

    def _valid_binding(self, session, binding):
        if self.bridge.current(session) is not binding:
            raise BridgeError("receipt belongs to a retired or replaced server run")
        # Public bridge checks authenticated owner/generation/dungeon/phase when
        # recording sends. Never mint a replacement using mutable session flags.

    def _group(self, binding):
        for group in self._groups.values():
            if group.ticket is binding.ticket:
                return group
        raise BridgeError("server run has no transport-owned group")

    def _register(self, session, binding, opcode, packets, *, life_event=None):
        for p in packets:
            pair = self._packet_key(p)
            if pair not in _RELEVANT.get(opcode, ()):
                continue
            receipt = SendReceipt(uuid4().hex, session, binding, opcode, p, pair,
                                  self._room(session) if pair == (0, 0x001D) else None,
                                  life_event,
                                  self.bridge.loading_receipt_cycle(session, binding) if opcode == 0x0025 else None)
            pending_key = (id(session), id(p))
            previous = self._pending.get(pending_key)
            if previous is not None and previous.packet is p:
                raise BridgeError("native response object reused by another raid request")
            self._receipts[receipt.capability] = receipt
            self._pending[pending_key] = receipt

    def handle_opcode(self, opcode, ordinary, body, *, session=None, party_leave=False, **kwargs):
        """Native wrapper replacement, forwarding ordinary input/output unchanged."""
        with self._lock:
            if session is None:
                return self._ordinary(ordinary, body, session=session, **kwargs)
            opcode = int(opcode)
            binding = self.bridge.current(session)
            raid_channel = self._raid_channel(session)
            if (binding is not None and opcode in (0x0027, 0x002D, 0x002E)
                    and not self._group(binding).accepted):
                return [], 'raid_transport reject shared run not ready before ordinary mutation'
            if opcode in (0x0007, 0x057F):
                self.selection_changed(session, reason="character_change" if opcode == 0x0007 else "channel_change")
                return self._ordinary(ordinary, body, session=session, **kwargs)
            if opcode in (0x002A, 0x0084):
                self._rejected_entries.pop(id(session), None)
                if party_leave:
                    result = self._ordinary(ordinary, body, session=session, **kwargs)
                    if (binding is not None and self.bridge.current(session) is binding
                            and result[0] and not getattr(session, 'in_dungeon_run', False)
                            and not getattr(session, 'in_dungeon_select', False)):
                        batch = self._retire(session, binding, 'party_leave')
                        if batch:
                            self._queue_notification(session, batch)
                    return result
                if binding is not None:
                    reason = "giveup" if opcode == 0x002A else "back_village"
                    batch = self._retire(session, binding, reason)
                    if batch:
                        self._queue_notification(session, batch)
                return self._ordinary(ordinary, body, session=session, **kwargs)
            if opcode == 0x0048:
                # The board and WarpObject both emit0048(1,1),000F,0010.
                # The following000F target distinguishes normal board entry
                # from a hatchery portal; do not infer the target from0048.
                # Codec padding after the two logical bytes is not constrained.
                raw = bytes(body)
                if (binding is not None and raw[:2] == b'\x01\x01'
                        and len(raw) in (2, 8, 16)
                        and int(getattr(session, 'dungeon_clear_phase', 0) or 0) < 2):
                    self._hatchery_warps[id(session)] = (session, binding, None, None, False)
                    return [], 'owned native navigation armed'
                # The ordinary EPLP handler owns focus/commit parsing and its
                # leave transition. A successful settled exit must release the
                # old raid capability before the next native SELECT request.
                settled = (binding is not None and bool(getattr(session, 'in_dungeon_run', False))
                           and int(getattr(session, 'dungeon_clear_phase', 0) or 0) >= 2)
                result = self._ordinary(ordinary, body, session=session, **kwargs)
                if settled and not bool(getattr(session, 'in_dungeon_run', False)):
                    batch = self._retire(session, binding, 'back_village')
                    if batch:
                        self._queue_notification(session, batch)
                return result
            if opcode == 0x000F and id(session) in self._hatchery_warps:
                warp = self._hatchery_warps[id(session)]
                raw = bytes(body)
                try:
                    if len(raw) not in (4, 8, 16) or warp[0] is not session or warp[1] is not binding:
                        raise BridgeError('native navigation has no matching source run')
                    self.bridge._check(self.bridge._record(session, binding))
                    target, room = int.from_bytes(raw[:4], 'little'), None
                    if 221 <= target <= 224:
                        actual, room = self.bridge.hatchery_portal(session, binding, native_activation=True)
                        if actual != target:
                            raise BridgeError('hatchery preselect differs from its native portal')
                    else:
                        self.battle.guard_dungeon_entry(session, target)
                except RaidError as exc:
                    return [], 'raid_transport reject %s' % exc
                self._hatchery_warps[id(session)] = (session, binding, target, room, True)
                # Preserve the source dungeon until the resolved0010 guard.
                return [], 'owned native %s preselect accepted' % ('hatchery' if room is not None else 'raid-board')
            if (opcode == 0x000F and binding is not None
                    and getattr(session, 'in_dungeon_run', False)):
                # A rejected portal's next000F must not fall through to the
                # ordinary selector while the server still owns the old run.
                return [], 'raid_transport reject current run has no activated hatchery portal'
            if binding is None and not raid_channel:
                # 0010 still gets an active scope so its in-source guard can
                # reject raid IDs in an otherwise ordinary channel.
                if opcode != 0x0010:
                    try:
                        return self._ordinary(ordinary, body, session=session, **kwargs)
                    except RaidError as exc:
                        return [], "raid_transport reject %s" % exc
            if id(session) in self._scopes:
                raise BridgeError("same session recursively entered a transport wrapper")
            scope = _Scope(session, opcode, int(getattr(session, "dungeon_run_generation", 0) or 0))
            if opcode == 0x0010:
                self._rejected_entries.pop(id(session), None)
            self._scopes[id(session)] = scope
            try:
                warp = self._hatchery_warps.get(id(session))
                if opcode == 0x0010 and binding is not None and not (warp and warp[1] is binding and warp[4]):
                    return [], "raid_transport reject duplicate owned entry"
                if binding is None and raid_channel and opcode in (0x0025, 0x0027, 0x002D, 0x002E, 0x0028, 0x0029):
                    return [], "raid_transport reject missing owned run binding"
                if binding is not None:
                    if opcode == 0x0025 and not self.bridge.note_loading_request(session, binding):
                        return [], "raid_transport duplicate loading request"
                    if opcode == 0x002E and not self.bridge.guard_settlement(session, binding):
                        return [], "raid_transport duplicate settlement request"
                prior_pending = bool(getattr(session, "dungeon_death_pending", False))
                cached=(self.bridge.settlement_result(session,binding)
                        if opcode==0x002E and binding is not None else None)
                if cached is None:
                    result = self._ordinary(ordinary, body, session=session, **kwargs)
                    if opcode==0x002E and binding is not None and int(getattr(session,'dungeon_clear_phase',0) or 0)==2:
                        self.bridge.settlement_result(session,binding,result)
                else:
                    from game.chain_loader import load_chain
                    payloads=load_chain('helper-dungeon_payloads.py')
                    packets=list(cached[0]);keys={self._packet_key(p) for p in packets}
                    try:
                        if (0,0x0022) not in keys:
                            raise BridgeError('original ordinary result response unavailable; effects cannot be repeated')
                        if (1,0x0045) not in keys:
                            packets.append(self.battle.codec(1,0x0045,payloads.build_card_select_right_state_0045(1)))
                        if (1,0x0046) not in keys:
                            packets.append(self.battle.codec(1,0x0046,payloads.build_card_layout_ack_0046()))
                    except Exception as exc:
                        raise BridgeError('owned settlement response encoding remains retryable: %s'%exc) from exc
                    session.dungeon_card_layout_sent=True
                    result=(packets,cached[1]+'; resumed owned settlement responses without repeating effects')
                    self.bridge.settlement_result(session,binding,result)
                packets, _note = result
                pairs = {self._packet_key(p) for p in packets}
                if opcode == 0x0010:
                    if not {(0, 0x001C), (0, 0x001D)} <= pairs:
                        return result
                    if scope.entry is None:
                        if raid_channel:
                            raise BridgeError("native entry returned START packets without its resolved-ID guard")
                        return result
                    raid, dgn, ids, ticket, group_key = scope.entry
                    if (int(getattr(session, "dungeon_run_generation", 0) or 0) <= scope.generation
                            or not getattr(session, "in_dungeon_run", False)
                            or int(getattr(session, "selected_dungeon_id", 0) or 0) != dgn):
                        raise BridgeError("native entry did not commit the guarded new run")
                    if scope.replaced_binding is not None:
                        batch = self._retire(session, scope.replaced_binding, 'replaced_run')
                        if batch:
                            self._queue_notification(session, batch)
                    binding = self.bridge.bind_owned_entry(raid, session, ids, ticket=ticket)
                    group = self._groups.setdefault(group_key, _Group(raid, binding.ticket))
                    if group.ticket is not binding.ticket:
                        raise BridgeError("native entry raced another shared ticket")
                    group.sessions[id(session)] = (session, binding)
                    if scope.world is not None:
                        scope.world.attach_ticket(binding.ticket)
                        if group.world is not None and group.world is not scope.world:
                            raise BridgeError('shared entry world changed during commit')
                        group.world = scope.world
                    self._lives[binding.capability] = _Life()
                if binding is None:
                    return result
                self._valid_binding(session, binding)
                if opcode == 0x002E:
                    self.bridge.note_settlement_prepared(session, binding, pairs)
                event = None
                if opcode in (0x0028, 0x0029) and self._group(binding).accepted:
                    pending = bool(getattr(session, "dungeon_death_pending", False))
                    life = self._lives[binding.capability]
                    actor = int(getattr(session, "dungeon_death_actor_id", 0) or 0)
                    if (opcode == 0x0028 and not prior_pending and pending
                            and actor == (binding.owner.character_id & 0xffff) and (0, 0x0020) in pairs
                            and life.pending_event is None):
                        event = LifeEvent(uuid4().hex, binding, binding.run_generation, life.sequence, "death")
                        life.pending_event = event
                    elif (opcode == 0x0029 and prior_pending and not pending
                          and (0, 0x0020) in pairs and life.pending_event is not None and life.death_sent):
                        event = LifeEvent(uuid4().hex, binding, binding.run_generation, life.sequence, "revive")
                # Duplicate death/revive responses remain native packets but
                # cannot become a new rank event or another life cycle.
                if opcode not in (0x0028, 0x0029) or event is not None:
                    self._register(session, binding, opcode, packets, life_event=event)
                if opcode == 0x0027 and packets and scope.deaths:
                    self._queue_shared_deaths(session, binding, scope.deaths.values())
                return result
            except RaidError as exc:
                current = self.bridge.current(session)
                # A rejected phase0 request remains playable. A partially
                # committed native entry or actor-journal error retires it.
                if (opcode in (0x0010, 0x0027) and current is not None
                        and not (opcode == 0x0010 and warp and current is warp[1])):
                    self._retire(session, current, "transport_failure")
                if opcode == 0x0010:
                    self._rejected_entries[id(session)] = (session, getattr(session, '_raid_selected_epoch', None))
                return [], "raid_transport reject %s" % exc
            finally:
                self._scopes.pop(id(session), None)

    def _ordinary(self, handler, body, **kwargs):
        """Drop the metadata lock around account-owned resource/DB work.

        Active scopes remain frozen per selected session. Native callbacks
        acquire this lock only for their short guard/world/journal updates.
        Each Game connection still dispatches on its own account thread.
        """
        self._lock.release()
        try:
            return handler(body, **kwargs)
        finally:
            self._lock.acquire()

    def has_rejected_entry(self, session):
        with self._lock:
            value = self._rejected_entries.get(id(session))
            return bool(value and value[0] is session
                        and value[1] is getattr(session, '_raid_selected_epoch', None))

    def has_native_entry_intent(self, session):
        with self._lock:
            intent = self._hatchery_warps.get(id(session))
            return (self.has_rejected_entry(session) or
                    bool(intent and intent[0] is session and intent[1] is self.bridge.current(session)))

    def native_entry_target(self, session):
        with self._lock:
            intent = self._hatchery_warps.get(id(session))
            if intent and intent[0] is session and intent[4] and intent[1] is self.bridge.current(session):
                return intent[2]
        return None

    def squad_hatchery_source(self, session, sessions):
        with self._lock:
            intent = self._hatchery_warps.get(id(session))
            if not intent or intent[3] is None or self.native_entry_target(session) is None:
                raise BridgeError('raid squad navigation has no accepted hatchery entrance')
            _, source, destination, room, _ = intent
            for peer in sessions:
                binding = self.bridge.current(peer)
                if (binding is None or binding.ticket is not source.ticket
                        or self.bridge.hatchery_portal(peer, binding, native_activation=True) != (destination, room)):
                    raise BridgeError('raid squad member is outside the same loaded hatchery entrance')
            return source.ticket, destination, room

    def arm_squad_hatchery(self, session, source):
        with self._lock:
            ticket, destination, room = source
            binding = self.bridge.current(session)
            if (binding is None or binding.ticket is not ticket
                    or self.bridge.hatchery_portal(session, binding) != (destination, room)):
                raise BridgeError('raid squad hatchery source changed before entry')
            self._hatchery_warps[id(session)] = (session, binding, destination, room, True)

    def note_actor_death(self, session, actual_actor_row):
        with self._lock:
            binding = self.bridge.current(session)
            if binding is None:
                return None
            scope = self._scopes.get(id(session))
            if scope is None or scope.session is not session or scope.opcode != 0x0027:
                raise BridgeError("actor death requires the live ordinary 0027 journal scope")
            death = self.bridge.note_actor_death(session, binding, actual_actor_row)
            scope.deaths[id(actual_actor_row)] = (actual_actor_row, death)
            return death

    def note_clear_gate(self, session, actual_actor_row):
        with self._lock:
            binding = self.bridge.current(session)
            if binding is None:
                return None
            scope = self._scopes.get(id(session))
            if scope is None or scope.session is not session or scope.opcode != 0x0027:
                raise BridgeError("clear gate requires the actual ordinary 0027 branch")
            row, death = scope.deaths.get(id(actual_actor_row), (None, None))
            if row is not actual_actor_row:
                raise BridgeError("clear gate has no accepted actor journal from this request")
            condition = getattr(session, "dungeon_clear_condition_state", None)
            return self.bridge.note_clear_gate(session, binding, death,
                                              condition_state=condition if getattr(condition, "bound", False) else None)

    def _queue_shared_deaths(self, reporter, binding, deaths):
        group = self._group(binding)
        if not group.accepted or group.world is None:
            return
        for row, death in deaths:
            identity = self.resolve_shared_actor(reporter, binding, row)
            for peer, owner, actual_row in group.world.living_peer_views(identity):
                candidate = group.sessions.get(id(peer))
                if peer is reporter or candidate is None or candidate[0] is not peer:
                    continue
                peer_binding = candidate[1]
                if self.bridge.current(peer) is not peer_binding or peer_binding.owner != owner:
                    continue
                key = (id(peer), identity)
                self._shared_deaths.setdefault(key, _SharedDeath(peer, peer_binding,
                    getattr(peer, '_raid_selected_epoch', None), self._room(peer), actual_row, identity))

    def shared_death_packets(self, session, ordinary):
        """Recipient thread only: normal XP/drop handling, never cross-account TLS.

        These are server-owned entity events, not forwarded client packets.
        Unknown passive keys, replaced rooms/selections and retired runs cannot
        be replayed. Each recipient still owns its own row, drops and packets.
        """
        packets = []
        with self._lock:
            if not self._shared_deaths:
                return []
            events = []
            for key, event in tuple(self._shared_deaths.items()):
                if event.session is not session:
                    continue
                self._shared_deaths.pop(key, None)
                events.append(event)
        for event in events:
            try:
                with self._lock:
                    binding = event.binding
                    self._valid_binding(session, binding)
                    if (getattr(session, '_raid_selected_epoch', None) is not event.selected_epoch
                            or self.bridge._owner(session) != binding.owner
                            or self._room(session) is not event.room
                            or event.row.get('killed')
                            or self.resolve_shared_actor(session, binding, event.row) is not event.identity):
                        continue
                    self.bridge.note_room_enter(session, binding, event.room)
                result, note = self.handle_opcode(0x0027, ordinary,
                    event.row['seq'].to_bytes(2, 'little'), session=session)
                packets.extend(result)
            except RaidError:
                # A stale audience must never kill a different room actor.
                continue
        return packets

    def capture_send(self, session, packet):
        """Immediately before sendall; preserve this receipt across the socket call."""
        with self._lock:
            receipt = self._pending.get((id(session), id(packet)))
            if receipt is None or receipt.session is not session or receipt.packet is not packet:
                return None
            self._valid_binding(session, receipt.binding)
            return receipt

    def after_send(self, receipt):
        """Only after successful sendall; returns notifications for caller to send."""
        if receipt is None:
            return []
        with self._lock:
            actual = self._receipts.get(getattr(receipt, "capability", None))
            if actual is not receipt:
                raise BridgeError("foreign or discarded send receipt")
            self._valid_binding(receipt.session, receipt.binding)
            pending_key = (id(receipt.session), id(receipt.packet))
            if self._pending.get(pending_key) is not receipt:
                return []
            try:
                return self._apply_sent(receipt)
            except RaidEventExpired:
                # Only the obsolete business event is discarded. The socket
                # write has already succeeded and the player may return to town.
                return []
            finally:
                self._pending.pop(pending_key, None)

    def _apply_sent(self, receipt):
        event, notifications = receipt.life_event, []
        if event is not None:
            # 0020 is a real successful send too. The bridge's public send
            # path checks selected owner, channel, run and phase even for
            # non-completion packets, before the phase-wide death callback.
            self.bridge.note_send(receipt.session, receipt.binding, *receipt.packet_key,
                                  request_opcode=receipt.request_opcode)
            life = self._lives[receipt.binding.capability]
            if event.life_sequence != life.sequence:
                raise BridgeError("late life response from another revive generation")
            if event.kind == "death":
                if life.pending_event is not event or not getattr(receipt.session, "dungeon_death_pending", False):
                    raise BridgeError("death response no longer belongs to its pending life")
                self.battle.record_accepted_death(receipt.session, event)
                life.death_sent = True
            elif event.kind == "revive":
                if getattr(receipt.session, "dungeon_death_pending", False):
                    raise BridgeError("revive response has no successful ordinary wallet transition")
                life.sequence += 1
                life.pending_event, life.death_sent = None, False
            else:
                raise BridgeError("unknown server life event")
        else:
            update = self.bridge.note_send(receipt.session, receipt.binding, *receipt.packet_key,
                                           request_opcode=receipt.request_opcode, room_state=receipt.room_state,
                                           loading_cycle=receipt.loading_cycle)
            group = self._group(receipt.binding)
            if update.accepted:
                group.accepted = True
                for peer, binding in group.sessions.values():
                    self.bridge.note_room_enter(peer, binding, self._room(peer))
            elif receipt.packet_key == (0, 0x001D) and receipt.request_opcode == 0x002D:
                self.bridge.note_room_enter(receipt.session, receipt.binding, receipt.room_state)
            if update.accepted or update.cleared:
                notifications = self.battle.on_bridge_update(receipt.session, update)
        return notifications

    def _retire(self, session, binding, reason):
        try:
            self.bridge.retire(session, binding, reason=reason)
        finally:
            self._hatchery_warps.pop(id(session), None)
            self._rejected_entries.pop(id(session), None)
            for pending_key, receipt in tuple(self._pending.items()):
                if receipt.binding is binding:
                    self._pending.pop(pending_key, None)
            self._lives.pop(binding.capability, None)
            for key, event in tuple(self._shared_deaths.items()):
                if event.binding is binding:
                    self._shared_deaths.pop(key, None)
            for capability, receipt in tuple(self._receipts.items()):
                if receipt.binding is binding:
                    self._receipts.pop(capability, None)
            for group_key, group in tuple(self._groups.items()):
                if group.ticket is binding.ticket:
                    group.sessions.pop(id(session), None)
                    if not group.sessions:
                        self._groups.pop(group_key, None)
        return self.battle.on_run_retired(session, binding, reason)

    def _queue_notification(self, session, batch):
        raid = self.battle.raid_for_session(session)
        if raid is not None:
            notification = _Notification(session, raid, self.bridge._owner(session),
                                         getattr(session, '_raid_selected_epoch', None), batch)
            self._notifications.setdefault(id(session), []).append(notification)

    def pop_notifications(self, session):
        """Drain wake-ups only for the current unsent original projection."""
        with self._lock:
            queued = self._notifications.pop(id(session), ())
            if not queued:
                return []
            try:
                raid = self.battle.raid_for_session(session)
                if raid is None:
                    return []
                owner = self.bridge._owner(session)
                pending_for = getattr(self.battle, 'pending_for', None)
                pending = pending_for(session) if callable(pending_for) else None
            except RaidError:
                return []
            if pending is None:
                return []
            epoch = getattr(session, '_raid_selected_epoch', None)
            batches = []
            for row in queued:
                if (row.session is session and row.raid is raid and row.owner == owner
                        and row.selected_epoch is epoch and row.batch is pending[0]
                        and not any(row.batch is batch for batch in batches)):
                    batches.append(row.batch)
            return batches

    def transport_failed(self, session):
        with self._lock:
            binding = self.bridge.current(session)
            return self._retire(session, binding, "transport_failure") if binding is not None else []

    def close(self, session):
        with self._lock:
            return self.selection_changed(session, reason="tcp_close")

    def selection_changed(self, session, *, reason):
        """Before C7/057F reset, or TCP finally with root's frozen registry.

        Optional battle.on_selection_changed(session, reason) is root's trusted
        membership/registry cleanup callback and must use its captured owner.
        It runs after bridge retirement even when no dungeon run was ever bound.
        Old retirement projections cannot be delivered after selection cleanup.
        """
        with self._lock:
            if reason not in ("character_change", "channel_change", "tcp_close"):
                raise BridgeError("unverified selection reset reason")
            self._rejected_entries.pop(id(session), None)
            self._notifications.pop(id(session), None)
            for key, event in tuple(self._shared_deaths.items()):
                if event.session is session:
                    self._shared_deaths.pop(key, None)
            binding = self.bridge.current(session)
            try:
                if binding is not None:
                    self._retire(session, binding, reason)
            finally:
                callback = getattr(self.battle, "on_selection_changed", None)
                if callable(callback):
                    callback(session, reason)
            return []
