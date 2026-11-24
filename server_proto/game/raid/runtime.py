"""Server-owned raid aggregate translated from CN's raid bridge boundaries.

No opcodes, PVF defaults, reward rolls, inventory writes, or client assertions
are accepted here. Adapter callbacks must use real catalogs and atomic durable
transactions. Normal raids require the actual family-specific PVF program.
"""

from dataclasses import dataclass, replace
from math import isfinite
from threading import RLock
from time import monotonic
from typing import Callable, Protocol
from uuid import uuid4

# Native management buttons select labels 1..10. Battle admission still
# validates its separate family-specific configuration before any cost.
PREPARATION_GROUP_MAX = 10


class RaidError(ValueError):
    pass


class RaidEventExpired(RaidError):
    """A previously owned event outlived its phase, attempt or map."""


class EntryMaterialsMissing(RaidError):
    pass


class RewardCardOccupied(RaidError):
    pass


@dataclass(frozen=True)
class Member:
    character_id: int
    account_id: str
    group: int
    slot: int = 0


@dataclass(frozen=True)
class AdmissionRule:
    item_id: int
    count: int
    minimum_members: int
    solo_minimum_members: int
    maximum_members: int


@dataclass(frozen=True)
class BattleConfiguration:
    family: str
    admission: AdmissionRule
    phase_seconds: tuple[int, int]
    start_delay_seconds: int
    weekly_limit: int | None
    minimum_clears: tuple[int, int]
    reward_kinds: tuple[tuple[int, ...], tuple[int, ...]]
    source_paths: tuple[str, ...]
    daily_account_limit: int | None = None


@dataclass(frozen=True)
class DungeonState:
    dungeon_id: int
    state: int


class ProgressionProgram(Protocol):
    """Immutable program snapshot from actual Anton/Luke PVF trigger rules.

    tick/reserve/clear return a new snapshot. The adapter owns parsing and the
    CN trigger/symbol/timer executor; a list of map IDs is not a substitute.
    """

    family: str
    phase: int
    epoch: int
    cleared: bool
    states: tuple[DungeonState, ...]

    def tick(self, now: float) -> "ProgressionProgram": ...
    def reserve(self, run_id: str, dungeon_id: int, attempt: int, now: float) -> "ProgressionProgram": ...
    def clear(self, run_id: str, dungeon_id: int, attempt: int, now: float) -> "ProgressionProgram": ...


@dataclass(frozen=True)
class BattleSnapshot:
    instance_id: str
    phase: int
    status: str
    started_at: float
    enter_after: float
    deadline: float
    dungeons: tuple[DungeonState, ...]
    member_clears: tuple[tuple[int, int], ...]
    single_index: int = 0
    completed_at: float = 0
    reward_finished_at: float = 0


@dataclass(frozen=True)
class AdmissionRequest:
    instance_id: str
    family: str
    members: tuple[Member, ...]
    rule: AdmissionRule
    configuration: BattleConfiguration


@dataclass(frozen=True)
class RunTicket:
    run_id: str
    instance_id: str
    phase: int
    attempt: int
    dungeon_id: int
    participants: tuple[Member, ...]


@dataclass(frozen=True)
class CompletionProof:
    """Built only by the server's owned dungeon runtime after settlement.

    Never decode these flags from a raid packet. CN checks the accepted boss
    report, sent boss response, sent settlement entry, and completed run state.
    """

    run_id: str
    character_id: int
    account_id: str
    dungeon_id: int
    boss_accepted: bool
    boss_response_sent: bool
    settlement_entry_sent: bool
    settled: bool


@dataclass(frozen=True)
class RewardEntitlement:
    instance_id: str
    family: str
    phase: int
    character_id: int
    account_id: str
    kind: int
    card: int
    fingerprint: str
    owned_clears: int
    minimum_clears: int
    completed_at: float

    @property
    def receipt_key(self):
        mode = 1 if self.family == "anton" else 2
        return f"raid_reward_v1_{mode}_{self.instance_id}_{self.phase}_{self.kind}"


@dataclass(frozen=True)
class WeeklyIntent:
    instance_id: str
    family: str
    members: tuple[Member, ...]
    limit: int
    completed_at: float


def _time(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not isfinite(now) or now <= 0:
        raise RaidError("server time unavailable")


def _member(member):
    if (not isinstance(member, Member) or not 1 <= member.character_id <= 65535
            or not member.account_id.strip() or member.account_id != member.account_id.strip()
            or not 0 <= member.group <= PREPARATION_GROUP_MAX):
        raise RaidError("member owner or group unavailable")


def _dungeons(states):
    if not isinstance(states, tuple) or not 1 <= len(states) <= 255:
        raise RaidError("actual dungeon states unavailable")
    if len({s.dungeon_id for s in states}) != len(states):
        raise RaidError("repeated dungeon owner")
    if any(not 1 <= s.dungeon_id <= 0xffffffff or s.state not in (0, 1, 2, 3) for s in states):
        raise RaidError("invalid dungeon state")


@dataclass(frozen=True)
class _OrdinaryUdpProof:
    directory: object
    connection: object
    report: object
    observation_key: tuple
    observation: object
    public_word: int
    lease_seconds: int

    def current(self, now):
        # This consumes data only. Never enter the UDP/service/lobby locks
        # while holding the raid aggregate or bridge lock.
        directory, connection = self.directory, self.connection
        connections = directory._connections
        observation = self.observation
        if (directory.clock is not monotonic
                or connections.get(id(connection.session)) is not connection
                or connection.report is not self.report
                or directory._observations.get(self.observation_key) is not observation
                or connection not in observation.eligible_connections
                or directory._same_port_conflicts.get(id(connection.session))
                or now < observation.seen_at):
            return False
        claims = tuple(value for value in tuple(connections.values())
            if connections.get(id(value.session)) is value
            and type(getattr(value.session, 'account_id', None)) is int
            and value.session.account_id == value.account_id
            and value.remote_ip == observation.ip
            and any(report is not None
                and report.public_ipv4_u32 == self.public_word
                and report.public_port == observation.port
                for report in (value.report, getattr(value, 'latest_report', None))))
        if len(claims) != 1 or claims[0] is not connection:
            return False
        direct = (self.report.nat_kind == 1
                  and self.report.local_ipv4_u32 == self.report.public_ipv4_u32)
        return direct or now - observation.seen_at < self.lease_seconds


@dataclass(frozen=True)
class _OrdinaryRelayProof:
    server: object
    controller: object
    key: tuple
    stamp: tuple

    def current(self, now):
        # Immutable worker snapshots only. No callback, HTTP or lock ordering
        # change while the raid aggregate consumes this one-use capability.
        owner=self.controller
        evidence=owner._relay_evidence.get(self.key)
        return (getattr(self.server,'party_relay_controller',None) is owner
            and owner.clock is monotonic and evidence is not None
            and evidence.stamp==self.stamp and 0<=now-evidence.at<1.25
            and 0<=now-owner._published_at<1.25)


@dataclass(frozen=True)
class _OrdinaryPublicationProof:
    references: tuple
    absent: tuple
    attributes: tuple
    sockets: tuple
    udp: tuple

    def current(self):
        # A one-use capability freezes exact producer/send identities, not
        # scalar copies or a callback into a differently ordered mutex.
        try:
            for owner, name, key, expected in self.references:
                if getattr(owner, name).get(key) is not expected:
                    return False
            for owner, name, key in self.absent:
                if key in getattr(owner, name):
                    return False
            for owner, name, expected, identity in self.attributes:
                actual = getattr(owner, name, None)
                if identity and actual is not expected or not identity and actual != expected:
                    return False
            if any(client.fileno() < 0 for client in self.sockets):
                return False
            now = monotonic()
            return all(type(proof) in (_OrdinaryUdpProof,_OrdinaryRelayProof) and proof.current(now) for proof in self.udp)
        except (AttributeError, OSError, TypeError, ValueError):
            return False


@dataclass(frozen=True, eq=False)
class _OrdinaryRunAuthority:
    raid: object
    issuer: object
    actor: int
    instance: str
    phase: int
    attempt: int
    members: tuple
    generation: tuple
    publication: _OrdinaryPublicationProof


class Raid:
    """One admitted raid; service adapter owns cross-raid directory membership.

    Every mutation is serialized, including admission/reward transactions.
    Callbacks may raise and must never partially commit. Persistent receipt
    checks and inventory/weekly transactions remain mandatory in the adapter.
    """

    def __init__(self, raid_key: int, name: str, family: str, single: bool, leader: Member):
        _member(leader)
        if not 1 <= raid_key <= 0xffffffff or not name.strip() or family not in ("anton", "luke"):
            raise RaidError("raid identity unavailable")
        self.raid_key, self.name, self.family, self.single = raid_key, name, family, bool(single)
        self.leader_id = leader.character_id
        self._members = {leader.character_id: replace(leader, slot=0)}
        self._lock = RLock()
        self._battle = None
        self._configuration = None
        self._program = None
        self._single_sequence = ()
        self._admitted = {}
        self._tickets = {}
        self._ordinary_issuer = None
        self._ordinary_grants = {}
        self._accepted_runs = set()
        self._retired_runs = set()
        self._run_departures = {}
        self._squad_leavers = {}
        self._cleared = set()
        self._closed_map_runs = set()
        self._recipients = {}
        self._entitlements = {}
        self._committed_rewards = set()
        self._deferred_rewards = set()
        self._weekly_committed = False

    @property
    def members(self):
        with self._lock:
            return tuple(sorted(self._members.values(), key=lambda m: (m.group, m.slot, m.character_id)))

    @property
    def battle(self):
        with self._lock:
            return self._battle

    @property
    def progression(self):
        """Independent snapshot for ordered symbols/timer transport projection."""
        with self._lock:
            return self._program.clone() if self._program is not None and hasattr(self._program, "clone") else self._program

    def member(self, character_id):
        with self._lock:
            try:
                return self._members[character_id]
            except KeyError:
                raise RaidError("raid member unavailable") from None

    def _leader(self, actor):
        if actor != self.leader_id or actor not in self._members:
            raise RaidError("current leader required")

    def _slot(self, group, excluding=None):
        occupied = {m.slot for m in self._members.values() if m.group == group and m.character_id != excluding}
        for slot in range(20 if group == 0 else 4):
            if slot not in occupied:
                return slot
        raise RaidError("native four-member group is full")

    def rename(self, actor, name):
        with self._lock:
            self._leader(actor)
            if not isinstance(name, str) or not name.strip():
                raise RaidError('raid name is empty')
            self.name = name

    def transfer_leadership(self, actor, successor):
        with self._lock:
            self._leader(actor)
            self.member(successor)
            self.leader_id = successor

    def preparation_open(self):
        with self._lock:
            b = self._battle
            return b is None or (not self.single and b.phase == 0
                and b.status == 'completed' and bool(b.reward_finished_at))

    def join(self, actor, member, *, commit_admission=None):
        with self._lock:
            self._leader(actor)
            _member(member)
            if not self.preparation_open() or self.single or member.character_id in self._members or len(self._members) >= 20:
                raise RaidError("raid cannot admit this member")
            member = replace(member, slot=self._slot(member.group))
            if self._battle is not None:
                if not callable(commit_admission):
                    raise RaidError('phase-break admission requires the atomic ticket owner')
                commit_admission(member)
                self._admitted[member.character_id] = member
            self._members[member.character_id] = member

    def move_member(self, actor, character_id, group):
        with self._lock:
            self._leader(actor)
            if self._battle is not None or not 0 <= group <= PREPARATION_GROUP_MAX:
                raise RaidError("preparation group required")
            current = self.member(character_id)
            if current.group == group:
                return
            self._members[character_id] = replace(current, group=group, slot=self._slot(group, character_id))

    def leave(self, character_id):
        with self._lock:
            self.member(character_id)
            del self._members[character_id]
            if character_id == self.leader_id:
                self.leader_id = min(self._members, default=0)
            # Earned reward/weekly intents retain their frozen accounts.

    def parties(self):
        with self._lock:
            return tuple(tuple(m for m in self.members if m.group == g) for g in range(1, PREPARATION_GROUP_MAX + 1)
                         if any(m.group == g for m in self._members.values()))

    def _configuration_valid(self, configuration):
        if not isinstance(configuration, BattleConfiguration) or configuration.family != self.family:
            raise RaidError("actual family configuration unavailable")
        r = configuration.admission
        if (not 1 <= r.item_id <= 0xffffffff or not 1 <= r.count <= 65535
                or not 1 <= r.minimum_members <= r.maximum_members <= 20
                or not 1 <= r.solo_minimum_members <= r.maximum_members
                or len(configuration.phase_seconds) != 2
                or any(not 1 <= n <= 0xffffffff for n in configuration.phase_seconds)
                or not 0 <= configuration.start_delay_seconds <= 3600
                or len(configuration.minimum_clears) != 2 or len(configuration.reward_kinds) != 2
                or not configuration.source_paths or any(not p for p in configuration.source_paths)):
            raise RaidError("actual admission/phase/reward configuration invalid")
        if configuration.weekly_limit is not None and not 1 <= configuration.weekly_limit <= 255:
            raise RaidError("explicit weekly policy invalid")
        if configuration.daily_account_limit is not None and configuration.daily_account_limit < 1:
            raise RaidError("actual daily account policy invalid")
        if configuration.weekly_limit is None and configuration.daily_account_limit is None:
            raise RaidError("actual entry-limit policy required")
        for phase, clears in enumerate(configuration.minimum_clears):
            if clears < 0 or (clears == 0 and not (self.family == "luke" and phase == 0 and not self.single)):
                raise RaidError("reward clear threshold unavailable")
            kinds = configuration.reward_kinds[phase]
            if not kinds or len(set(kinds)) != len(kinds) or any(k not in (1, 2, 3) for k in kinds):
                raise RaidError("actual reward kind unavailable")

    def _program_valid(self, program, phase, allow_terminal=False):
        if (program is None or program.family != self.family or program.phase != phase
                or program.epoch <= 0 or not allow_terminal and (program.cleared or getattr(program, "failed", False))):
            raise RaidError("actual uncleared family-specific phase program required")
        _dungeons(program.states)

    def start(self, actor, configuration, weekly_counts, now, preflight, commit_cost,
              *, program=None, single_sequence=(), daily_account_counts=None):
        with self._lock:
            self._leader(actor)
            _time(now)
            if self._battle is not None:
                raise RaidError("raid already started; use next_phase")
            self._configuration_valid(configuration)
            members = self.members
            rule = configuration.admission
            minimum = rule.solo_minimum_members if self.single else rule.minimum_members
            if not minimum <= len(members) <= rule.maximum_members:
                raise RaidError("actual raid member minimum/maximum not met")
            if any(not 0 <= m.group <= PREPARATION_GROUP_MAX for m in members):
                raise RaidError("group exceeds configured maximum")
            if configuration.weekly_limit is not None:
                if set(weekly_counts) != {m.character_id for m in members} or any(
                    not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < configuration.weekly_limit
                    for n in weekly_counts.values()
                ):
                    raise RaidError("owned weekly count missing, inconsistent, or exhausted")
            elif weekly_counts:
                raise RaidError("weekly values supplied without actual weekly policy")
            if configuration.daily_account_limit is not None:
                if daily_account_counts is None or set(daily_account_counts) != {m.account_id for m in members} or any(
                    not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < configuration.daily_account_limit
                    for n in daily_account_counts.values()
                ):
                    raise RaidError("daily account count missing, inconsistent, or exhausted")
            if self.single:
                # CN's current terminal/reward owner supports six-map Anton.
                # Luke single has no complete reward owner: never admit it here.
                if self.family != "anton" or len(members) != 1 or program is not None or len(single_sequence) != 6:
                    raise RaidError("supported actual single Anton sequence required")
                states = tuple(DungeonState(d, 0 if i == 0 else 1) for i, d in enumerate(single_sequence))
                _dungeons(states)
                if configuration.minimum_clears[0] != len(single_sequence):
                    raise RaidError("single reward threshold differs from actual sequence")
            else:
                self._program_valid(program, 0)
                if not getattr(program, "battle_dependencies_ready", True):
                    raise RaidError("actual family battle action dependencies unavailable")
                states = program.states
            if not callable(preflight) or not callable(commit_cost):
                raise RaidError("dependency and atomic admission adapters required")
            # Both callbacks consume immutable server snapshots; no success is
            # stored until preflight and every member's transaction succeed.
            preflight(members, configuration)
            instance = uuid4().hex
            enter = now + configuration.start_delay_seconds
            deadline = enter + configuration.phase_seconds[0]
            if deadline > 0xffffffff:
                raise RaidError("phase deadline exceeds native timestamp")
            request = AdmissionRequest(instance, self.family, members, rule, configuration)
            commit_cost(request)
            self._configuration, self._program = configuration, program
            self._single_sequence = tuple(single_sequence)
            self._admitted = {m.character_id: m for m in members}
            self._battle = BattleSnapshot(instance, 0, "active", now, enter, deadline, states, ())
            return self._battle

    def _active(self, now):
        _time(now)
        b = self._battle
        if b is None or b.status != "active" or now < b.enter_after or now >= b.deadline:
            raise RaidError("raid phase is not active")
        return b

    def advance_clock(self, now):
        with self._lock:
            _time(now)
            b = self._battle
            if b is None or b.status != "active":
                return b
            if now >= b.deadline:
                if self.family == "luke" and self._program is not None and hasattr(self._program, "end_phase"):
                    self._program = self._program.end_phase(b.deadline)
                self._battle = replace(b, status="timed_out", completed_at=b.deadline)
            elif now >= b.enter_after and self._program is not None:
                next_program = self._program.tick(now)
                self._program_valid(next_program, b.phase, allow_terminal=True)
                self._program = next_program
                status = "timed_out" if getattr(next_program, "failed", False) else "completed" if next_program.cleared else "active"
                self._battle = replace(b, dungeons=next_program.states, status=status,
                                       completed_at=now if status != "active" else 0)
                if status == "completed":
                    self._freeze_recipients()
            return self._battle

    def _install_ordinary_issuer(self, issuer):
        with self._lock:
            if self._ordinary_issuer is not None and self._ordinary_issuer is not issuer:
                raise RaidError('ordinary run issuer already belongs to another server directory')
            self._ordinary_issuer = issuer

    def _issue_ordinary_run(self, issuer, actor, members, generation, publication):
        with self._lock:
            members = tuple(members)
            battle = self._battle
            if (issuer is not self._ordinary_issuer or battle is None or battle.status != 'active'
                    or type(publication) is not _OrdinaryPublicationProof or not publication.current()
                    or not 1 <= len(members) <= 4 or len({m.character_id for m in members}) != len(members)
                    or actor not in {m.character_id for m in members}
                    or any(self._members.get(m.character_id) is not m or self._admitted.get(m.character_id) is not m for m in members)):
                raise RaidError('actual current admitted ordinary members required')
            authority = _OrdinaryRunAuthority(self, issuer, actor, battle.instance_id, battle.phase,
                self._program.epoch if self._program is not None else 1, members, generation, publication)
            for previous in tuple(self._ordinary_grants):
                if previous.actor == actor or previous.phase != battle.phase or previous.instance != battle.instance_id:
                    self._ordinary_grants.pop(previous, None)
            self._ordinary_grants[authority] = authority
            return authority

    def _revoke_ordinary_run(self, issuer, generation):
        with self._lock:
            for authority in tuple(self._ordinary_grants):
                if authority.issuer is issuer and authority.generation == generation:
                    self._ordinary_grants.pop(authority, None)

    def bind_run(self, actor, dungeon_id, participants, now, *, ordinary_authority=None):
        with self._lock:
            self.advance_clock(now)
            b = self._active(now)
            owner = self.member(actor)
            ids = tuple(participants)
            if (not ids or len(set(ids)) != len(ids) or actor not in ids
                    or len(ids) > 4 or self.single and ids != (actor,)):
                raise RaidError("actual prepared participant set required")
            members = tuple(self.member(cid) for cid in ids)
            if any(m != self._admitted.get(m.character_id) for m in members):
                raise RaidError("prepared account differs from admitted owners")
            if ordinary_authority is None:
                if any(m.group != owner.group for m in members):
                    raise RaidError("prepared group/account differs from admitted owners")
            else:
                authority = (self._ordinary_grants.pop(ordinary_authority, None)
                    if type(ordinary_authority) is _OrdinaryRunAuthority else None)
                if (authority is not ordinary_authority or authority.raid is not self
                        or authority.issuer is not self._ordinary_issuer or authority.actor != actor
                        or authority.instance != b.instance_id or authority.phase != b.phase
                        or authority.attempt != (self._program.epoch if self._program is not None else 1)
                        or len(authority.members) != len(members)
                        or any(actual is not frozen for actual, frozen in zip(members, authority.members))
                        or not authority.publication.current()):
                    raise RaidError('foreign, stale or replayed ordinary run authority')
            if not any(s.dungeon_id == dungeon_id and s.state == 0 for s in b.dungeons):
                raise RaidError("dungeon is not open")
            attempt = self._program.epoch if self._program is not None else 1
            ticket = RunTicket(uuid4().hex, b.instance_id, b.phase, attempt, dungeon_id, members)
            if self.family == "luke" and self._program is not None:
                next_program = self._program.reserve(ticket.run_id, dungeon_id, attempt, now)
                self._program_valid(next_program, b.phase)
                self._program = next_program
                self._battle = replace(b, dungeons=next_program.states)
            self._tickets[ticket.run_id] = ticket
            return ticket

    def _ticket_owner(self, run_id, actor, account_id):
        ticket = self._tickets.get(run_id)
        b = self._battle
        if ticket is None or b is None or ticket.instance_id != b.instance_id or ticket.phase != b.phase:
            raise RaidError("run does not belong to this instance/phase")
        member = next((m for m in ticket.participants if m.character_id == actor), None)
        current = self._members.get(actor)
        if member is None or member.account_id != account_id or current != member:
            raise RaidError("run callback is not its current owned member")
        return ticket

    def accept_run(self, run_id, actor, account_id, now):
        """Call after the whole frozen group has committed ordinary scene entry."""
        with self._lock:
            ticket = self._ticket_owner(run_id, actor, account_id)
            self.advance_clock(now)
            b = self._active(now)
            if run_id in self._retired_runs or self._program is not None and ticket.attempt != self._program.epoch:
                raise RaidError("run was retired or its attempt expired")
            if run_id in self._accepted_runs:
                return False
            if self.family == "luke" and self._program is not None and hasattr(self._program, "enter"):
                program = self._program.enter(run_id, ticket.dungeon_id, ticket.attempt, now)
                self._program_valid(program, b.phase, allow_terminal=True)
                self._program = program
                self._battle = replace(b, dungeons=program.states)
            self._accepted_runs.add(run_id)
            return True

    def give_up(self, run_id, actor, account_id, now, *, left_party=False):
        """Retire a group run after every frozen participant has departed."""
        with self._lock:
            ticket = self._ticket_owner(run_id, actor, account_id)
            _time(now)
            if run_id in self._cleared or run_id in self._retired_runs:
                return False
            if left_party:
                self._squad_leavers.setdefault(run_id, set()).add(actor)
            departed = self._run_departures.setdefault(run_id, set())
            departed.add(actor)
            if departed != {m.character_id for m in ticket.participants}:
                return False
            if (self.family == "luke" and self._program is not None
                    and self._battle.status == "active" and ticket.attempt == self._program.epoch):
                if run_id in self._accepted_runs and hasattr(self._program, "give_up"):
                    program = self._program.give_up(run_id, ticket.dungeon_id, now)
                elif hasattr(self._program, "cancel_reservation"):
                    program = self._program.cancel_reservation(run_id, ticket.dungeon_id)
                else:
                    program = self._program
                self._program_valid(program, self._battle.phase, allow_terminal=True)
                self._program = program
                self._battle = replace(self._battle, dungeons=program.states)
            self._retired_runs.add(run_id)
            return True

    def enter_map(self, run_id, actor, account_id, map_id, now):
        """Call only for a real committed room in the ticket's dungeon runtime."""
        with self._lock:
            self._ticket_owner(run_id, actor, account_id)
            self.advance_clock(now)
            b = self._active(now)
            if run_id not in self._accepted_runs or run_id in self._retired_runs:
                raise RaidError("committed active run required for map event")
            if self.family == "luke" and self._program is not None and hasattr(self._program, "enter_map"):
                program = self._program.enter_map(map_id, now)
                self._program_valid(program, b.phase, allow_terminal=True)
                self._program = program
                self._battle = replace(b, dungeons=program.states)
            return self._battle

    def apply_barrier_damage(self, run_id, actor, account_id, event_id, source, now):
        """Use only an accepted object's unique death identity and PVF source."""
        with self._lock:
            ticket = self._ticket_owner(run_id, actor, account_id)
            self.advance_clock(now)
            b = self._active(now)
            if (run_id not in self._accepted_runs or run_id in self._retired_runs or self.family != "anton"
                    or self._program is None or not hasattr(self._program, "apply_barrier_damage")):
                raise RaidError("owned committed Anton object death required")
            program = self._program.apply_barrier_damage(event_id, source, ticket.dungeon_id, ticket.attempt, now)
            self._program_valid(program, b.phase, allow_terminal=True)
            self._program = program
            self._battle = replace(b, dungeons=program.states)
            return self._battle

    def credit_luke_generator(self, run_id, actor, account_id, event_id, now):
        """A bridge-bound native machine report cannot supply a symbol/value."""
        with self._lock:
            ticket = self._ticket_owner(run_id, actor, account_id)
            self.advance_clock(now)
            battle = self._active(now)
            if (self.family != 'luke' or run_id not in self._accepted_runs or run_id in self._retired_runs
                    or run_id in self._cleared or self._program is None):
                raise RaidError('active accepted Luke machine owner required')
            program = self._program.credit_luke_generator(run_id, event_id, ticket.dungeon_id, ticket.attempt, now)
            self._program_valid(program, battle.phase, allow_terminal=True)
            self._program = program
            self._battle = replace(battle, dungeons=program.states)
            return self._battle

    def _freeze_recipients(self):
        b = self._battle
        threshold = self._configuration.minimum_clears[b.phase]
        counts = dict(b.member_clears)
        recipients = {cid: self._members[cid] for cid, m in self._admitted.items()
                      if cid in self._members and counts.get(cid, 0) >= threshold}
        if not recipients:
            raise RaidError("completed phase has no qualified frozen recipients")
        for cid, member in recipients.items():
            self._recipients[(b.phase, cid)] = member

    def clear(self, proof, now):
        with self._lock:
            _time(now)
            b = self._battle
            ticket = self._tickets.get(proof.run_id)
            if b is None or ticket is None or ticket.instance_id != b.instance_id or ticket.phase != b.phase:
                raise RaidError("clear does not belong to this phase instance")
            self._ticket_owner(proof.run_id, proof.character_id, proof.account_id)
            if proof.run_id in self._retired_runs:
                raise RaidError("clear belongs to a retired run")
            if self._program is not None and hasattr(self._program, "enter") and proof.run_id not in self._accepted_runs:
                raise RaidError("actual committed scene entry required")
            member = next((m for m in ticket.participants if m.character_id == proof.character_id), None)
            if (member is None or member.account_id != proof.account_id or proof.dungeon_id != ticket.dungeon_id
                    or not all((proof.boss_accepted, proof.boss_response_sent, proof.settlement_entry_sent, proof.settled))):
                raise RaidError("owned completed settlement required")
            # Valid replays from followers are no-ops even once phase completes.
            if ticket.run_id in self._cleared:
                return False
            self.advance_clock(now)
            b = self._battle
            if b.status != 'active' or self._program is not None and ticket.attempt != self._program.epoch:
                raise RaidEventExpired("clear belongs to a finished phase or expired attempt")
            self._active(now)
            map_open = any(s.dungeon_id == ticket.dungeon_id and s.state == 0 for s in b.dungeons)
            next_index = b.single_index
            if ticket.run_id in self._closed_map_runs:
                # A different accepted squad closed this map while these
                # players were inside. Their actual settlement still earns
                # personal participation once, including after a timed reopen.
                states, completed = b.dungeons, False
            elif not map_open:
                raise RaidEventExpired("clear dungeon is no longer open")
            elif self.single:
                if ticket.attempt != 1 or self._single_sequence[next_index] != ticket.dungeon_id:
                    raise RaidError("single clear differs from actual sequence")
                next_index += 1
                states = tuple(DungeonState(d, 0 if i == next_index else 1)
                               for i, d in enumerate(self._single_sequence))
                completed = next_index == len(self._single_sequence)
            else:
                next_program = self._program.clear(ticket.run_id, ticket.dungeon_id, ticket.attempt, now)
                if next_program.family != self.family or next_program.phase != b.phase or next_program.epoch != ticket.attempt:
                    raise RaidError("program clear changed its owner")
                _dungeons(next_program.states)
                states, completed = next_program.states, next_program.cleared
                self._program = next_program
                if self.family == 'anton' and not completed and any(
                        d.dungeon_id == ticket.dungeon_id and d.state != 0 for d in states):
                    # Bind is the reservation made against this map opening.
                    # Loading may finish after it closes. A later settlement
                    # still needs an accepted scene and its actual clear proof.
                    for other_id in self._tickets.keys() - self._cleared - self._retired_runs:
                        other = self._tickets[other_id]
                        if (other is not ticket and other.instance_id == ticket.instance_id and
                                (other.phase, other.attempt, other.dungeon_id) == (ticket.phase, ticket.attempt, ticket.dungeon_id)):
                            self._closed_map_runs.add(other_id)
            counts = dict(b.member_clears)
            for member in ticket.participants:
                if member.character_id not in self._squad_leavers.get(ticket.run_id, ()):
                    counts[member.character_id] = counts.get(member.character_id, 0) + 1
            self._cleared.add(ticket.run_id)
            self._battle = replace(b, dungeons=states, member_clears=tuple(sorted(counts.items())),
                                   single_index=next_index, status="completed" if completed else "active",
                                   completed_at=now if completed else 0)
            if completed:
                self._freeze_recipients()
            return True

    def freeze_reward(self, character_id, account_id, kind, card, fingerprint):
        with self._lock:
            b = self._battle
            recipient = self._recipients.get((b.phase, character_id)) if b is not None else None
            if b is None or b.status != "completed" or recipient is None or recipient.account_id != account_id:
                raise RaidError("completed owned reward recipient required")
            if kind not in self._configuration.reward_kinds[b.phase] or not 0 <= card <= (19 if kind == 3 else 3):
                raise RaidError("configured reward kind/card required")
            for (phase, cid, other_kind), taken in self._entitlements.items():
                if phase != b.phase or cid == character_id or other_kind != kind or taken.card != card:
                    continue
                other = self._recipients.get((phase, cid))
                if kind == 3 or (other is not None and recipient.group > 0 and other.group == recipient.group):
                    raise RewardCardOccupied('reward card is already occupied in this phase and squad')
            try:
                digest = bytes.fromhex(fingerprint)
            except (ValueError, TypeError):
                raise RaidError("expanded reward fingerprint invalid") from None
            if len(digest) != 32 or not any(digest):
                raise RaidError("expanded reward fingerprint unavailable")
            entitlement = RewardEntitlement(b.instance_id, self.family, b.phase, character_id, account_id,
                                            kind, card, digest.hex(), dict(b.member_clears).get(character_id, 0),
                                            self._configuration.minimum_clears[b.phase], b.completed_at)
            key = (b.phase, character_id, kind)
            if key in self._entitlements and self._entitlements[key] != entitlement:
                raise RaidError("reward kind already frozen for another card/outcome")
            self._entitlements[key] = entitlement
            return entitlement

    def commit_weekly(self, commit):
        with self._lock:
            b = self._battle
            if (b is None or b.status != "completed" or not (self.single or b.phase == 1)
                    or self._configuration.weekly_limit is None):
                raise RaidError("complete final raid required for weekly commit")
            if self._weekly_committed:
                return True
            members = tuple(m for (phase, cid), m in sorted(self._recipients.items()) if phase == b.phase)
            if not members or not callable(commit):
                raise RaidError("qualified weekly transaction adapter required")
            intent = WeeklyIntent(b.instance_id, self.family, members, self._configuration.weekly_limit, b.completed_at)
            replayed = commit(intent)
            if not isinstance(replayed, bool):
                raise RaidError("weekly transaction must confirm persisted receipt")
            self._weekly_committed = True
            return replayed

    def grant_reward(self, entitlement, commit):
        with self._lock:
            key = (entitlement.phase, entitlement.character_id, entitlement.kind)
            if self._entitlements.get(key) != entitlement:
                raise RaidError("server-frozen reward entitlement required")
            if ((self.single or entitlement.phase == 1) and self._configuration.weekly_limit is not None
                    and not self._weekly_committed):
                raise RaidError("final rewards require committed weekly intent")
            if key in self._committed_rewards:
                return True
            if not callable(commit):
                raise RaidError("durable reward transaction adapter required")
            replayed = commit(entitlement)
            if not isinstance(replayed, bool):
                raise RaidError("reward transaction must confirm persisted receipt")
            self._committed_rewards.add(key)
            return replayed

    def defer_reward(self, entitlement, persist):
        with self._lock:
            key=(entitlement.phase,entitlement.character_id,entitlement.kind)
            if self._entitlements.get(key)!=entitlement:
                raise RaidError('server-frozen deferred reward required')
            if key in self._deferred_rewards or key in self._committed_rewards:return
            if ((self.single or entitlement.phase==1) and self._configuration.weekly_limit is not None
                    and not self._weekly_committed):
                raise RaidError('final rewards require committed weekly intent')
            if not callable(persist) or type(persist(entitlement)) is not bool:
                raise RaidError('durable reward obligation receipt required')
            self._deferred_rewards.add(key)

    def finish_rewards(self, now):
        with self._lock:
            _time(now)
            b = self._battle
            if b is None or b.status != "completed" or now < b.completed_at:
                raise RaidError("completed reward flow required")
            required = {(b.phase, cid, kind) for phase, cid in self._recipients if phase == b.phase
                        for kind in self._configuration.reward_kinds[b.phase]}
            if not required or not required <= self._committed_rewards | self._deferred_rewards:
                raise RaidError("every qualified reward must commit first")
            if not b.reward_finished_at:
                self._battle = replace(b, reward_finished_at=now)
            return self._battle

    def next_phase(self, actor, program, now):
        with self._lock:
            self._leader(actor)
            _time(now)
            b = self._battle
            if (b is None or self.single or b.phase != 0 or b.status != "completed"
                    or not b.reward_finished_at or now < b.reward_finished_at):
                raise RaidError("qualified completed phase reward flow required")
            if any(m != self._admitted.get(m.character_id) for m in self.members):
                raise RaidError("next phase member/account was not admitted")
            enter = now + self._configuration.start_delay_seconds
            if hasattr(self._program, "next_phase"):
                if program is not None:
                    raise RaidError("actual program transition cannot be overridden")
                program = self._program.next_phase(enter)
            self._program_valid(program, 1)
            deadline = enter + self._configuration.phase_seconds[1]
            if deadline > 0xffffffff:
                raise RaidError("next phase deadline exceeds native timestamp")
            self._program = program
            self._battle = BattleSnapshot(b.instance_id, 1, "active", now, enter, deadline, program.states, ())
            return self._battle
