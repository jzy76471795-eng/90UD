"""Bridge trusted US dungeon journals and successful sends to raid events.

Call only at the reviewed server hooks. No method decodes a client proof.
Owner resolution must authenticate the actual selected repository owner and
connection/selection generation. note_send is called only AFTER sendall returns.
"""

from dataclasses import dataclass, field
from threading import RLock
from time import time
from uuid import uuid4

from .runtime import CompletionProof, RaidError, RaidEventExpired


class BridgeError(RaidError):
    pass


class ExpiredBridgeEvent(BridgeError, RaidEventExpired):
    pass


@dataclass(frozen=True)
class OwnerIdentity:
    character_id: int
    account_id: str
    channel_id: int
    generation: object


@dataclass(frozen=True)
class RunBinding:
    """Opaque server capability; never serialize this into a C2S request."""

    ticket: object
    owner: OwnerIdentity
    run_generation: int
    initial_room_key: tuple[int, int, int, int]
    capability: str


@dataclass(frozen=True)
class ActorDeath:
    event_id: str
    run_id: str
    run_generation: int
    room_key: tuple[int, int, int, int]
    actor_key: int  # Opaque server journal identity; never a native wire actor key.
    actor_code: int
    actor_type: int
    actor_identity: int


@dataclass(frozen=True)
class BridgeUpdate:
    accepted: bool = False
    cleared: bool = False
    proof: CompletionProof | None = None


@dataclass
class _Record:
    binding: RunBinding
    session: object
    raid: object
    entry_sent: set = field(default_factory=set)
    loading_requested: bool = False
    loading_sent: set = field(default_factory=set)
    loading_room: object = None
    board_percentages: tuple = ()
    loading_cycle: int = 0
    sent_rooms: set = field(default_factory=set)
    entered_rooms: set = field(default_factory=set)
    deaths: dict = field(default_factory=dict)
    actor_journal: dict = field(default_factory=dict)
    clear_gate: ActorDeath | None = None
    clear_gate_sent: bool = False
    settlement_guarded: bool = False
    settlement_prepared: bool = False
    settlement_result: object = None
    settlement_sent: set = field(default_factory=set)
    proof: CompletionProof | None = None
    retired: bool = False
    failure_cursor: int = 0
    native_hatchery_portals: set = field(default_factory=set)


@dataclass
class _Group:
    raid: object
    ticket: object
    records: dict = field(default_factory=dict)
    accepted: bool = False
    cleared: bool = False
    barrier_deaths: dict = field(default_factory=dict)
    luke_machine_reports: dict = field(default_factory=dict)
    hatchery_walls: set = field(default_factory=set)


_ENTRY = {(0, 0x001C), (0, 0x001D)}
_LOADING = {(1, 0x0025), (0, 0x001E)}
_SETTLEMENT = {(0, 0x0022), (1, 0x0045), (1, 0x0046)}
_HATCHERY_PORTALS = {19103: (18863, 221), 19119: (18892, 222),
                     19116: (18893, 223), 19100: (18862, 224)}


class RaidRunBridge:
    def __init__(self, owner_resolver, *, clock=time, barrier_classifier=None,
                 channels=None, member_clear=None):
        if not callable(owner_resolver) or not callable(clock):
            raise BridgeError("authenticated owner resolver and server clock required")
        if barrier_classifier is not None and not callable(barrier_classifier):
            raise BridgeError("actual actor PVF source classifier required")
        self._owner_resolver, self._clock = owner_resolver, clock
        self._member_clear = member_clear
        self._barrier_classifier = barrier_classifier
        self._shared_actor_resolver = None
        self._ordinary_authorizer = None
        self._channels = dict(channels or {"anton": 23, "luke": 32})
        self._records, self._groups = {}, {}
        self._lock = RLock()

    def _owner(self, session):
        owner = self._owner_resolver(session)
        if not isinstance(owner, OwnerIdentity):
            raise BridgeError("owner resolver did not return authenticated identity")
        account = str(owner.account_id)
        if (not 1 <= owner.character_id <= 65535 or not account.strip()
                or account != account.strip() or not 1 <= owner.channel_id <= 255
                or owner.generation is None):
            raise BridgeError("selected owner/channel/generation unavailable")
        return OwnerIdentity(owner.character_id, account, owner.channel_id, owner.generation)

    @staticmethod
    def _room_key(session):
        try:
            key = (int(session.dungeon_maze_index), int(session.dungeon_cell_x),
                   int(session.dungeon_cell_y), int(session.dungeon_map_index))
        except (AttributeError, TypeError, ValueError):
            raise BridgeError("committed server room context unavailable") from None
        if key[0] < 0 or not all(0 <= n <= 255 for n in key[1:3]) or not 1 <= key[3] <= 0xffffffff:
            raise BridgeError("committed server room context invalid")
        return key

    def _room(self, session, room_state):
        key = self._room_key(session)
        states = getattr(session, "dungeon_room_states", None)
        if (not isinstance(states, dict) or not isinstance(room_state, dict)
                or states.get(key) is not room_state or room_state.get("key") != key
                or int(room_state.get("map_index", 0)) != key[3]):
            raise BridgeError("actual current server room journal required")
        return key

    def current(self, session):
        with self._lock:
            record = self._records.get(id(session))
            return record.binding if record is not None and record.session is session and not record.retired else None

    def note_luke_machine(self, session, body):
        """C0297 lacks an object ID; bind one report to one actual room machine.

        This bounded native report is not a server-verified passive death and
        never creates an ordinary clear, loot, or arbitrary symbol write.
        """
        import struct
        if not isinstance(body, bytes) or len(body) != 9:
            raise BridgeError('exact native Luke symbol report required')
        symbol, operand, mode = struct.unpack('<IIB', body)
        with self._lock:
            binding = self.current(session)
            if binding is None:
                raise BridgeError('Luke machine report has no owned run')
            record = self._record(session, binding)
            self._check(record)
            raid = record.raid
            rules = raid.progression.luke_rules
            key = self._room_key(session)
            group = self._groups[binding.ticket.run_id]
            dungeon = binding.ticket.dungeon_id
            if (raid.family != 'luke' or binding.ticket.phase != 0 or rules is None or not group.accepted
                    or key not in record.sent_rooms or not getattr(session, 'dungeon_finish_loading', False)
                    or getattr(session, 'dungeon_death_pending', False) or getattr(session, 'dungeon_clear_phase', 0)
                    or operand != 1 or mode != 1 or rules.symbols.get(dungeon) != symbol):
                raise BridgeError('Luke machine report differs from its current native owner')
            rows = [row for row in getattr(session, 'dungeon_room_passive_objects', ())
                    if row.get('type') == 9 and row.get('kind') in ('passive_object', 'special_passive')
                    and rules.object_dungeons.get(row.get('code')) == dungeon]
            if len(rows) != 1:
                raise BridgeError('Luke machine report requires one actual indexed current-room object')
            # Run + committed room is shared by every viewer; no client field
            # can mint additional events by changing the operand or actor key.
            if key in group.luke_machine_reports:
                return False
            event = uuid4().hex
            raid.credit_luke_generator(binding.ticket.run_id, binding.owner.character_id,
                                       binding.owner.account_id, event, self._clock())
            group.luke_machine_reports[key] = (rows[0]['code'], event)
            return True

    def hatchery_portal(self, session, binding, *, native_activation=False):
        """Target of an owned native Energy Containment portal activation.

        Frozen MAP/OBJ/ACT chain: 19103/18863 -> 221, 19119/18892 ->
        222, 19116/18893 -> 223, 19100/18862 -> 224. Static passives
        remain client-owned geometry. Their ACT can destroy the wall locally;
        00FE38E0 then sends0048(01,01),000F,0010 without a guaranteed C0027.
        Keep that activation distinct from an actual actor death journal.
        """
        with self._lock:
            record = self._record(session, binding)
            group = self._check(record)
            raid, ticket = record.raid, binding.ticket
            key = self._room_key(session)
            portal = _HATCHERY_PORTALS.get(key[3])
            if (raid.family != 'anton' or ticket.phase != 1 or ticket.dungeon_id != 218
                    or portal is None or not group.accepted or key not in record.sent_rooms
                    or not getattr(session, 'dungeon_finish_loading', False)
                    or getattr(session, 'dungeon_death_pending', False)
                    or getattr(session, 'dungeon_clear_phase', 0)):
                raise BridgeError('hatchery warp requires its loaded Energy room')
            code, target = portal
            if not any(d.dungeon_id == target and d.state == 0 for d in raid.progression.states):
                raise BridgeError('hatchery is not open in this raid phase')
            rows = tuple(getattr(session, 'dungeon_room_passive_objects', ()) or ())
            rows += tuple(getattr(session, 'dungeon_room_mons', ()) or ())
            matches = [row for row in rows if row.get('code') == code and row.get('type') == 9]
            if native_activation:
                if (len(matches) != 1 or matches[0].get('kind') != 'passive_object'
                        or matches[0].get('runtime_map_index') != key[3]):
                    raise BridgeError('native hatchery activation requires its actual static wall')
                record.native_hatchery_portals.add((key, code))
            if (not matches or ((key, code) not in group.hatchery_walls
                                and (key, code) not in record.native_hatchery_portals)):
                raise BridgeError('hatchery wall has no actual current-room death journal')
            return target, key

    def board_location(self, raid, sessions):
        """Dungeon/maze from a delivered, accepted and still-owned run only.

        011A76E0 indexes the resource's [MAZE] directory with this second
        value, not map/map.lst. Sending a map ID loses the native progress.
        """
        with self._lock:
            for session in sessions:
                record = self._records.get(id(session))
                if (record is None or record.retired or record.session is not session
                        or record.raid is not raid):
                    continue
                try:
                    group = self._check(record)
                    key = self._room_key(session)
                except RaidError:
                    continue
                if group.accepted and key in record.sent_rooms:
                    return record.binding.ticket.dungeon_id, key[0]
            return 0, -1

    def _board_record(self, raid, session):
        record = self._records.get(id(session))
        if record is None or record.session is not session or record.retired or record.raid is not raid:
            raise BridgeError('raid HP report has no current session binding')
        group = self._check(record)
        key = self._room_key(session)
        if (not group.accepted or group.cleared or record.settlement_guarded
                or key not in record.sent_rooms or record.loading_sent != _LOADING
                or not getattr(session, 'dungeon_finish_loading', False)
                or getattr(session, 'dungeon_clear_phase', 0)):
            raise BridgeError('raid HP report requires an active loaded room')
        self._loading_room(record)
        return record, key[0]

    def note_board_hp(self, raid, session, maze, percentages):
        with self._lock:
            record, current_maze = self._board_record(raid, session)
            if maze != (current_maze if raid.family == 'luke' else 0):
                raise BridgeError('raid HP report has a different maze')
            record.board_percentages = percentages

    def board_hp(self, raid, host, dungeon, maze):
        with self._lock:
            try:
                record, current_maze = self._board_record(raid, host)
            except RaidError:
                return ()
            if record.binding.ticket.dungeon_id != dungeon or current_maze != maze:
                return ()
            return record.board_percentages

    def skill_run(self, raid, sessions):
        """One loaded, uncleared target run; no room or ownership claims."""
        with self._lock:
            tickets = []
            for session in sessions:
                record = self._records.get(id(session))
                if (record is None or record.retired or record.session is not session
                        or record.raid is not raid):
                    raise BridgeError('party skill has no current owned target run')
                group = self._check(record)
                if (not group.accepted or group.cleared or record.settlement_guarded
                        or self._room_key(session) not in record.sent_rooms
                        or not getattr(session, 'dungeon_finish_loading', False)
                        or getattr(session, 'dungeon_clear_phase', 0)):
                    raise BridgeError('party skill target is not in a loaded combat room')
                tickets.append(record.binding.ticket)
            if not tickets or any(ticket is not tickets[0] for ticket in tickets):
                raise BridgeError('party skill requires one shared target run')
            return tickets[0].run_id

    def participation_snapshot(self, raid, *, phase):
        """Read only actual accepted, still-owned ordinary run participants.

        A bound ticket or client loading/settlement flags cannot publish a
        party. Accepted groups retain their live records through settlement;
        only retirement, owner change or phase/attempt expiry removes them.
        This method calls no adapter/transport hook while holding this lock.
        """
        with self._lock:
            battle = raid.battle
            if battle is None or phase not in (0, 1):
                return ()
            rows = []
            for run_id, group in sorted(self._groups.items()):
                ticket = group.ticket
                if (group.raid is not raid or not group.accepted
                        or ticket.instance_id != battle.instance_id or ticket.phase != phase):
                    continue
                members = []
                for member in ticket.participants:
                    record = group.records.get(member.character_id)
                    if record is None or record.retired:
                        continue
                    try:
                        if self._check(record) is not group:
                            continue
                    except RaidError:
                        continue
                    members.append(member.character_id)
                if members:
                    rows.append((run_id, ticket.dungeon_id, tuple(members)))
            return tuple(rows)

    def _record(self, session, binding):
        record = self._records.get(id(session))
        if (record is None or record.session is not session or record.binding is not binding
                or record.retired):
            raise BridgeError("stale or foreign server run capability")
        return record

    def _check(self, record, *, require_run=True):
        binding, session, raid = record.binding, record.session, record.raid
        ticket = binding.ticket
        owner = self._owner(session)
        if owner != binding.owner:
            raise BridgeError("owner/channel/selection generation changed")
        if int(getattr(session, "dungeon_run_generation", 0) or 0) != binding.run_generation:
            raise BridgeError("ordinary dungeon run generation changed")
        if (require_run and not bool(getattr(session, "in_dungeon_run", False))) or int(
            getattr(session, "selected_dungeon_id", 0) or 0
        ) != ticket.dungeon_id:
            raise BridgeError("callback belongs to another or retired dungeon")
        battle = raid.battle
        if battle is None or battle.instance_id != ticket.instance_id or battle.phase != ticket.phase:
            raise ExpiredBridgeEvent("raid instance/phase changed")
        group = self._groups[ticket.run_id]
        if battle.status != "active" and not (group.cleared and battle.status == "completed"):
            raise ExpiredBridgeEvent("raid phase is no longer active")
        program = raid.progression
        attempt = program.epoch if program is not None else 1
        if attempt != ticket.attempt:
            raise ExpiredBridgeEvent("raid attempt expired")
        member = raid.member(owner.character_id)
        expected = next((m for m in ticket.participants if m.character_id == owner.character_id), None)
        if expected is None or member != expected or str(member.account_id) != owner.account_id:
            raise BridgeError("current member/account differs from frozen participants")
        return group

    def bind_owned_entry(self, raid, session, participants, *, ticket=None):
        """Call after owned 0010 installed its actual ordinary run context.

        First member mints a ticket; followers MUST reuse that same ticket.
        A rejected/malformed entry must not call this hook. Cancel on send failure.
        """
        # Resolve server membership before acquiring the bridge lock. Raid
        # service mutation paths must never wait in reverse lock order.
        authority = (self._ordinary_authorizer(raid, session, tuple(participants))
                     if ticket is None and self._ordinary_authorizer is not None else None)
        with self._lock:
            owner = self._owner(session)
            if owner.channel_id != self._channels.get(raid.family):
                raise BridgeError("owner is not in this family's configured raid channel")
            generation = int(getattr(session, "dungeon_run_generation", 0) or 0)
            dungeon = int(getattr(session, "selected_dungeon_id", 0) or 0)
            if not bool(getattr(session, "in_dungeon_run", False)) or generation <= 0 or dungeon <= 0:
                raise BridgeError("owned committed 0010 run required")
            member = raid.member(owner.character_id)
            if str(member.account_id) != owner.account_id:
                raise BridgeError("entry account differs from actual raid owner")
            ids = tuple(participants)
            if not ids or len(set(ids)) != len(ids) or owner.character_id not in ids:
                raise BridgeError("actual prepared group participants required")
            initial_key = self._room_key(session)
            states = getattr(session, "dungeon_room_states", None)
            self._room(session, states.get(initial_key) if isinstance(states, dict) else None)
            previous = self._records.get(id(session))
            if previous is not None and not previous.retired:
                if (previous.binding.owner == owner and previous.binding.run_generation == generation
                        and previous.binding.ticket.dungeon_id == dungeon):
                    raise BridgeError("ordinary entry already has a binding")
                self._retire_record(previous, "replaced_run")
            if ticket is None:
                ticket = (raid.bind_run(owner.character_id, dungeon, ids, self._clock(), ordinary_authority=authority)
                          if authority is not None else raid.bind_run(owner.character_id, dungeon, ids, self._clock()))
                if ticket.run_id in self._groups:
                    raise BridgeError("server run identity reused")
                group = _Group(raid, ticket)
                self._groups[ticket.run_id] = group
            else:
                group = self._groups.get(ticket.run_id)
                if group is None or group.raid is not raid or group.ticket is not ticket:
                    raise BridgeError("follower requires this bridge's actual shared ticket")
            if (ticket.dungeon_id != dungeon or {m.character_id for m in ticket.participants} != set(ids)
                    or owner.character_id in group.records or group.cleared
                    or any(peer.binding.initial_room_key != initial_key for peer in group.records.values())):
                raise BridgeError("shared entry dungeon/participants changed")
            binding = RunBinding(ticket, owner, generation, initial_key, uuid4().hex)
            record = _Record(binding, session, raid)
            writes = tuple(getattr(raid.progression, 'symbol_writes', ()))
            record.failure_cursor = writes[-1].sequence if writes else 0
            self._records[id(session)] = record
            group.records[owner.character_id] = record
            self._check(record)
            return binding

    def note_loading_request(self, session, binding):
        """C1/0025 before the ordinary handler; no client boolean is consumed."""
        with self._lock:
            record = self._record(session, binding)
            self._check(record)
            if record.entry_sent != _ENTRY:
                raise BridgeError("actual 001C/001D successful sends required before loading")
            self._loading_room(record)
            if record.loading_requested:
                return False
            record.loading_requested = True
            return True

    def _loading_room(self, record):
        if (not isinstance(record.loading_room, dict)
                or record.loading_room.get('key') != self._room_key(record.session)):
            raise BridgeError('current room requires its actual START_MAP successful send')
        self._room(record.session, record.loading_room)

    def loading_receipt_cycle(self, session, binding):
        """Stamp the ordinary loading responses with their sent map cycle."""
        with self._lock:
            record = self._record(session, binding)
            self._check(record)
            self._loading_room(record)
            if not record.loading_requested:
                raise BridgeError('loading response has no owned loading request')
            return record.loading_cycle

    def _accept_group_if_ready(self, group, record):
        if group.accepted:
            return False
        ids = {m.character_id for m in group.ticket.participants}
        if set(group.records) != ids:
            return False
        for peer in group.records.values():
            if peer.retired:
                return False
            self._check(peer)
            if peer.entry_sent != _ENTRY or not peer.loading_requested or peer.loading_sent != _LOADING:
                return False
        owner = record.binding.owner
        record.raid.accept_run(group.ticket.run_id, owner.character_id, owner.account_id, self._clock())
        group.accepted = True
        return True

    def note_send(self, session, binding, flag, opcode, *, request_opcode, room_state=None,
                  loading_cycle=None):
        """Call after sendall succeeded, with the originating C1 opcode.

        001D additionally carries its real cached room object. Packet construction
        and log lines are insufficient. On transport failure call retire instead.
        """
        with self._lock:
            record = self._record(session, binding)
            group = self._check(record)
            key = (int(flag), int(opcode))
            if key in _ENTRY:
                if request_opcode == 0x0010:
                    if key == (0, 0x001D):
                        room_key = self._room(session, room_state)
                        if room_key != binding.initial_room_key:
                            raise BridgeError("0010 send belongs to a different initial room")
                        record.sent_rooms.add(room_key)
                    record.entry_sent.add(key)
                elif key == (0, 0x001D) and request_opcode == 0x002D and group.accepted:
                    record.sent_rooms.add(self._room(session, room_state))
                else:
                    raise BridgeError("entry/map send came from an unrelated request")
                if key == (0, 0x001D):
                    # Every actual map send starts a room loading cycle, even
                    # when revisiting the same cached room in this run.
                    record.loading_room = room_state
                    record.board_percentages = ()
                    record.loading_cycle += 1
                    record.loading_requested = False
                    record.loading_sent.clear()
            elif key in _LOADING:
                if request_opcode != 0x0025 or not record.loading_requested or record.entry_sent != _ENTRY:
                    raise BridgeError("loading response has no owned loading request")
                self._loading_room(record)
                if loading_cycle is not None and loading_cycle != record.loading_cycle:
                    raise BridgeError('loading receipt belongs to an earlier room cycle')
                record.loading_sent.add(key)
                return BridgeUpdate(accepted=self._accept_group_if_ready(group, record))
            elif key == (0, 0x001F):
                if request_opcode != 0x0027 or record.clear_gate is None or not group.accepted:
                    raise BridgeError("ENABLE_CLEAR has no actual owned clear gate")
                if int(getattr(session, "dungeon_clear_phase", 0) or 0) != 1:
                    raise BridgeError("ENABLE_CLEAR is outside its owned phase-one gate")
                record.clear_gate_sent = True
            elif key in _SETTLEMENT:
                if request_opcode != 0x002E or not record.settlement_prepared:
                    raise BridgeError("settlement send has no protected prepared result")
                record.settlement_sent.add(key)
                return self._finish_clear(record, group)
            return BridgeUpdate()

    def note_room_enter(self, session, binding, room_state):
        """Actual committed room journal after its START_MAP successfully sent."""
        with self._lock:
            record = self._record(session, binding)
            group = self._check(record)
            key = self._room(session, room_state)
            if not group.accepted or key not in record.sent_rooms:
                raise BridgeError("room event requires committed run and actual START_MAP send")
            if key in record.entered_rooms:
                return False
            owner = binding.owner
            record.raid.enter_map(binding.ticket.run_id, owner.character_id, owner.account_id, key[3], self._clock())
            record.entered_rooms.add(key)
            return True

    def note_actor_death(self, session, binding, actor_row):
        """Only the actual current server actor row after accepted 0027 killed it."""
        with self._lock:
            record = self._record(session, binding)
            group = self._check(record)
            room_key = self._room_key(session)
            states = getattr(session, "dungeon_room_states", {})
            self._room(session, states.get(room_key))
            rows = tuple(getattr(session, "dungeon_room_mons", ()) or ()) + tuple(
                getattr(session, "dungeon_room_passive_objects", ()) or ()
            )
            if (not group.accepted or room_key not in record.sent_rooms or not isinstance(actor_row, dict)
                    or not any(row is actor_row for row in rows) or not actor_row.get("killed")):
                raise BridgeError("accepted actual current-room actor death journal required")
            key = (room_key, id(actor_row))
            if key in record.deaths:
                return record.deaths[key]
            actor_code = int(actor_row.get("code") or actor_row.get("monster_code") or 0)
            if actor_code <= 0:
                raise BridgeError("actual dead entity PVF code unavailable")
            # Ordinary US passive rows have no native seq/runtime_object_key;
            # a real ordinary monster may also legitimately have seq zero.
            # Owner, run generation, room and actual row identity were checked
            # above. Allocate a separate server identity only at that boundary.
            # Keep the actual row alive for this run so Python id reuse cannot
            # alias an earlier actor. Do not write a private/native key into it.
            journal_row = record.actor_journal.get(key)
            if journal_row is None:
                journal_row = (actor_row, uuid4().int)
                record.actor_journal[key] = journal_row
            elif journal_row[0] is not actor_row:
                raise BridgeError("server actor journal identity changed")
            actor_key = journal_row[1]
            death = ActorDeath(uuid4().hex, binding.ticket.run_id, binding.run_generation, room_key,
                               actor_key, actor_code, int(actor_row.get("type") or actor_row.get("mon_type") or 0),
                               id(actor_row))
            shared = self._shared_actor_resolver(session, binding, actor_row) if self._shared_actor_resolver else None
            if shared is not None:
                from .shared_world import SharedActorIdentity
                if (not isinstance(shared, SharedActorIdentity) or shared.run_id != binding.ticket.run_id
                        or shared.room_key != room_key or shared.actor_code != actor_code
                        or shared.actor_type != death.actor_type):
                    raise BridgeError('shared actor identity differs from the actual owned death')
            if self._barrier_classifier is not None:
                source = self._barrier_classifier(actor_row)
                if source is not None:
                    self._apply_shared_barrier_death(group, record, actor_row, death, source, shared)
            record.deaths[key] = death
            # The frozen boss LAST destroys its large transmission locally.
            # C0027 for that passive is not guaranteed (including control
            # kills). Journal the proven current-room dependency before CLEAR
            # retires this run. The ordinary shared-entity path deduplicates
            # later client reports and follower views of the same object.
            terminal = getattr(self._barrier_classifier, 'destroyed_passives', None)
            targets = (terminal(actor_row) if terminal is not None
                       and record.raid.family == 'anton' and binding.ticket.dungeon_id == 218 else ())
            for target in targets:
                matches = [row for row in rows if row.get('code') == target
                           and row.get('type') == 9 and row.get('kind') == 'passive_object']
                if len(matches) != 1:
                    raise BridgeError('terminal passive missing or ambiguous in actual room')
                dependent = matches[0]
                was_killed = bool(dependent.get('killed'))
                dependent['killed'] = True
                try:
                    self.note_actor_death(session, binding, dependent)
                except Exception:
                    dependent['killed'] = was_killed
                    raise
            # Static native wall death is shared by this frozen group. Its
            # opener may already have warped out before a follower requests
            # entry; retain the server journal until the whole run retires.
            portal = _HATCHERY_PORTALS.get(room_key[3])
            if (record.raid.family == 'anton' and binding.ticket.dungeon_id == 218
                    and portal is not None and (actor_code, death.actor_type) == (portal[0], 9)):
                group.hatchery_walls.add((room_key, actor_code))
            return death

    def _apply_shared_barrier_death(self, group, record, actor_row, death, source, shared=None):
        """One effect per authoritative run/room/entity, never per viewer.

        The shared-world producer supplies an opaque identity registered from
        actual server spawns before per-owner loot. Legacy controlled worlds
        without that producer must share the actual row object in every live
        peer's sent current room. Equal PVF IDs, seq values or copied private
        fields never establish shared spawn identity.
        """
        authority = shared if shared is not None else actor_row
        key = (death.room_key, id(authority))
        signature = (source, death.actor_code, death.actor_type)
        previous = group.barrier_deaths.get(key)
        if previous is not None:
            if previous[0] is not authority or previous[1] != signature:
                raise BridgeError("shared barrier entity identity/source changed")
            return
        for peer in (() if shared is not None else group.records.values()):
            if peer.retired:
                continue
            self._check(peer)
            rows = tuple(getattr(peer.session, "dungeon_room_mons", ()) or ()) + tuple(
                getattr(peer.session, "dungeon_room_passive_objects", ()) or ())
            if (self._room_key(peer.session) != death.room_key or death.room_key not in peer.sent_rooms
                    or not any(row is actor_row for row in rows)):
                raise BridgeError("multiplayer barrier effect requires one authoritative shared server entity")
        owner = record.binding.owner
        record.raid.apply_barrier_damage(record.binding.ticket.run_id, owner.character_id,
                                        owner.account_id, death.event_id, source, self._clock())
        # Keep the row alive; Python id reuse cannot alias a later entity.
        group.barrier_deaths[key] = (authority, signature, death.event_id)

    def note_clear_gate(self, session, binding, death, *, condition_state=None):
        """Inside the real 0027 phase0->1 ENABLE_CLEAR branch, before send.

        death is the bridge's opaque actual actor journal, not parsed client data.
        For PVF maze conditions pass the actual session-bound condition state.
        """
        with self._lock:
            record = self._record(session, binding)
            self._check(record)
            if not any(value is death for value in record.deaths.values()) or death.room_key != self._room_key(session):
                raise BridgeError("clear gate has no actual current owned death journal")
            if int(getattr(session, "dungeon_clear_phase", 0) or 0) != 1:
                raise BridgeError("owned phase0-to1 gate transition required")
            rows = list(getattr(session, "dungeon_room_mons", ()) or ())
            if not rows or any(row.get("is_blocking", True) and not row.get("killed") for row in rows):
                raise BridgeError("actual blocking room actors are not cleared")
            actual_condition = getattr(session, "dungeon_clear_condition_state", None)
            if actual_condition is not None and getattr(actual_condition, "bound", False) and condition_state is not actual_condition:
                raise BridgeError("cannot bypass this run's actual PVF condition gate")
            if condition_state is not None:
                if (getattr(session, "dungeon_clear_condition_state", None) is not condition_state
                        or int(getattr(condition_state, "dungeon_id", 0)) != binding.ticket.dungeon_id
                        or int(getattr(condition_state, "maze_index", -1)) != death.room_key[0]
                        or not getattr(condition_state, "bound", False) or not getattr(condition_state, "complete", False)
                        or not getattr(condition_state, "destroy_bound", False)):
                    raise BridgeError("actual owned complete PVF maze condition required")
            else:
                cell = death.room_key[1:3]
                boss_cell = (int(getattr(session, "dungeon_boss_x", -1)), int(getattr(session, "dungeon_boss_y", -1)))
                if cell != boss_cell and death.actor_type not in (3, 8):
                    raise BridgeError("owned boss endpoint/type or PVF condition required")
            if record.clear_gate is not None:
                if record.clear_gate is not death:
                    raise BridgeError("clear gate journal changed")
                return False
            record.clear_gate = death
            return True

    def guard_settlement(self, session, binding):
        """Before calling ordinary 002E; False means duplicate, do not re-run it."""
        with self._lock:
            record = self._record(session, binding)
            self._check(record)
            if record.settlement_guarded:
                return not record.settlement_prepared and record.settlement_result is not None
            if (record.clear_gate is None or not record.clear_gate_sent
                    or int(getattr(session, "dungeon_clear_phase", 0) or 0) != 1):
                raise BridgeError("forged phase0 settlement or unsent owned clear gate")
            record.settlement_guarded = True
            return True

    def settlement_result(self,session,binding,result=None):
        """Cache already committed ordinary effects; retry only response encoding."""
        with self._lock:
            record=self._record(session,binding);self._check(record)
            if result is not None:
                if not record.settlement_guarded or int(getattr(session,'dungeon_clear_phase',0) or 0)!=2:
                    raise BridgeError('only the owned ordinary settled result can be cached')
                record.settlement_result=(list(result[0]),result[1])
            return record.settlement_result

    def note_settlement_prepared(self, session, binding, packet_keys):
        """After the protected helper returns; keys are SERVER-built responses."""
        with self._lock:
            record = self._record(session, binding)
            self._check(record)
            if (not record.settlement_guarded or record.clear_gate is None or not record.clear_gate_sent
                    or int(getattr(session, "dungeon_clear_phase", 0) or 0) != 2
                    or not _SETTLEMENT <= set(packet_keys)):
                raise BridgeError("protected phase1->2 result and all native layout responses required")
            record.settlement_prepared = True

    def _finish_clear(self, record, group):
        if record.proof is not None:
            return BridgeUpdate()
        if record.settlement_sent != _SETTLEMENT:
            return BridgeUpdate()
        self._check(record)
        if int(getattr(record.session, "dungeon_clear_phase", 0) or 0) != 2:
            raise BridgeError("settlement state changed before actual final send")
        binding = record.binding
        # Each true is derived from stored server evidence above, never from a
        # C2S boolean or merely setting phase2/loading/layout session flags.
        proof = CompletionProof(binding.ticket.run_id, binding.owner.character_id, binding.owner.account_id,
                                binding.ticket.dungeon_id, record.clear_gate is not None,
                                record.clear_gate_sent, (0, 0x0022) in record.settlement_sent,
                                record.settlement_prepared and record.settlement_sent == _SETTLEMENT)
        changed = False
        if not group.cleared:
            changed = bool(record.raid.clear(proof, self._clock()))
            group.cleared = True
        if self._member_clear is not None:
            self._member_clear(record.raid.family, binding.ticket.phase, proof)
        record.proof = proof
        return BridgeUpdate(cleared=changed, proof=proof)

    def expired_run(self, session):
        """Owned loaded combat invalidated by total timeout or a PVF reset."""
        with self._lock:
            record = self._records.get(id(session))
            if record is None or record.session is not session or record.retired:
                return False
            binding, raid = record.binding, record.raid
            ticket, battle = binding.ticket, raid.battle
            if (self._owner(session) != binding.owner
                    or getattr(session, 'dungeon_run_generation', 0) != binding.run_generation
                    or not getattr(session, 'in_dungeon_run', False)
                    or getattr(session, 'selected_dungeon_id', None) != ticket.dungeon_id
                    or battle is None or battle.instance_id != ticket.instance_id
                    or battle.phase != ticket.phase):
                return False
            progress = raid.progression
            if battle.status == 'timed_out' or (progress is not None and ticket.attempt != progress.epoch):
                return True
            # Naval and Luke energy failures issue this authored command
            # without necessarily changing the phase/attempt. Observe each
            # new write, including repeated value1, only for pre-existing runs.
            writes = tuple(getattr(progress, 'symbol_writes', ()))
            if any(w.sequence > record.failure_cursor and w.name == 'KILL ALL CHARACTER' and w.value == 1
                   for w in writes):
                return True
            if writes:
                record.failure_cursor = writes[-1].sequence
            return False

    def retire(self, session, binding, *, reason):
        """2A/84/rebind/TCP-close cleanup; frozen identity survives ordinary reset."""
        with self._lock:
            record = self._records.get(id(session))
            if record is None:
                return False
            if record.session is not session or record.binding is not binding:
                raise BridgeError("old capability cannot retire a replacement run")
            return self._retire_record(record, reason)

    def _retire_record(self, record, reason):
        if record.retired:
            return False
        if reason not in ("giveup", "back_village", "party_leave", "tcp_close", "transport_failure", "replaced_run", "character_change", "channel_change"):
            raise BridgeError("server retirement reason unavailable")
        group, owner = self._groups[record.binding.ticket.run_id], record.binding.owner
        try:
            battle = record.raid.battle
            if (not group.cleared and battle is not None and battle.instance_id == group.ticket.instance_id
                    and battle.phase == group.ticket.phase):
                # Domain tracks each frozen participant's departure and only
                # retires/releases Luke when every participant has left.
                args = (record.binding.ticket.run_id, owner.character_id, owner.account_id, self._clock())
                if reason == 'party_leave':
                    record.raid.give_up(*args, left_party=True)
                else:
                    record.raid.give_up(*args)
        finally:
            record.retired = True
            if self._records.get(id(record.session)) is record:
                del self._records[id(record.session)]
            if all(peer.retired for peer in group.records.values()):
                # A failed prepare can leave frozen participants who never
                # acquired a bridge record. They cannot later emit a retire
                # callback. Once every actual record has retired, close those
                # unaccepted seats so Luke's reservation is released as well.
                battle = record.raid.battle
                if (not group.accepted and not group.cleared and battle is not None
                        and battle.instance_id == group.ticket.instance_id
                        and battle.phase == group.ticket.phase):
                    for member in group.ticket.participants:
                        if member.character_id not in group.records:
                            record.raid.give_up(group.ticket.run_id, member.character_id,
                                                str(member.account_id), self._clock())
                self._groups.pop(group.ticket.run_id, None)
        return True
