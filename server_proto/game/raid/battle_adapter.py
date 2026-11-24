"""Local normal-raid boundary over actual US repositories and PVF programs.

Ordinary dungeon proofs belong exclusively to RaidRunBridge. This adapter
never decodes a client completion boolean, creates native single-raid data,
or replaces inventory instance generation. Unknown UI fields are callbacks.
"""

from dataclasses import dataclass, field, replace
from math import ceil
from secrets import randbelow, randbits
from time import time

from .battle_protocol import (
    C_RAID_ENTRY_COST_INFO, C_SELECT_RAID_REWARD_CARD, EntryCost, MemberReward,
    RewardItem as NativeRewardItem, N_RAID_ENTRY_COST_INFO, N_RAID_REWARD_LIST,
    N_RAID_DUNGEON_STATE, N_RAID_PARTICIPATION, N_RAID_REMAIN_TIME, N_RAID_STATE, N_RAID_RESULT,
    encode_entry_cost, encode_participation, encode_raid_state, encode_raid_timer,
    encode_reward_list, lobby_metadata, parse_entry_cost_request, parse_select_card_request,
    N_PREPARE_START_RAID, encode_prepare_start,
    N_RAID_PHASE_COIN_INFO, encode_phase_coin_info,
)
from .battle_projection import N_RAID_SYMBOL, delay_body, dungeon_change_body, dungeon_states_body, symbols_body, result_body
from .battle_events import participation_add_body, participation_remove_body
from .dungeon_adapter import OwnerIdentity, RaidRunBridge
from .entry_cost import EntryCostUnavailable, choose_entry_cost
from .ledger import LedgerError
from .protocol import C_START_RAID, N_RAID_MODIFY, encode_raid_modify, parse_start_request
from .runtime import AdmissionRule, BattleConfiguration, BattleSnapshot, RaidError, EntryMaterialsMissing, RewardCardOccupied
from .rewards import freeze_reward_plan
from .party_board import N_RAID_PARTIES, party_snapshot, encode_parties

REWARD_AUTO_SELECT_SECONDS = 15
REWARD_OFFLINE_FALLBACK_SECONDS = 30
PERSONAL_CARD_DISPLAY_SECONDS = 4
# Paired per-recipient display hint, outside the shipped PVF symbol range.
# The low byte is zero on opening and the frozen member count on a team list.
ANTON_TEAM_DRAWER_SYMBOL = 0x52445731


@dataclass
class RunData:
    configuration: BattleConfiguration
    admitted: tuple
    phase_deaths: dict = field(default_factory=dict)
    death_events: dict = field(default_factory=dict)
    reward_plans: dict = field(default_factory=dict)
    phase_revives: dict = field(default_factory=dict)
    run_revives: dict = field(default_factory=dict)
    break_until: float = 0
    skills: dict = field(default_factory=dict)
    phase_result_delivery: object = None
    drawer_opened: dict = field(default_factory=dict)


@dataclass
class ProjectionState:
    """Delivery cursor for one frozen selected session, never a whole raid."""
    symbol_cursor: tuple = (-1, -1, 0)
    native_state: tuple | None = None
    native_dungeons: dict = field(default_factory=dict)
    native_tracked_dungeon: int | None = None
    native_participation: dict = field(default_factory=dict)
    native_timers: dict = field(default_factory=dict)
    native_remaining: dict = field(default_factory=dict)
    native_phase_revives: tuple | None = None
    native_parties: bytes | None = None
    native_skills: bytes | None = None
    native_rewards: frozenset = frozenset()
    native_party_only_completion: int | None = None
    personal_delivery: tuple | None = None
    scene_generation: int | None = None


class RaidBattleAdapter:
    def __init__(self, service, runtime, ledger, *, entry_period_key,
                 owner_resolver, codec=None, broadcast=None, grant_callback=None,
                 native_deadline=None, reward_projection=None, result_projection=None,
                 pc_room_eligible=None, barrier_classifier=None, clock=time,
                 seed_factory=lambda: randbits(64) or 1, roll=None, entry_period_resolver=None,
                 native_prepared=None, native_auto_prepare=None, native_clock_ready=None,
                 native_timer_deadline=None, native_participants=None,
                 native_session_prepared=None, native_session_auto_prepare=None, clear_quota=False):
        if (not isinstance(entry_period_key, str) or not entry_period_key.strip()
                or entry_period_key != entry_period_key.strip()
                or not callable(owner_resolver) or not callable(clock) or not callable(seed_factory)):
            raise RaidError('explicit local entry period and trusted owner/clock required')
        self.service, self.runtime, self.ledger = service, runtime, ledger
        self.clear_quota = bool(clear_quota)
        self.entry_period_key = entry_period_key
        if entry_period_resolver is not None and not callable(entry_period_resolver):
            raise RaidError('trusted entry-period resolver required')
        self.entry_period_resolver = entry_period_resolver
        if native_prepared is not None and not callable(native_prepared):
            raise RaidError('native preparation proof resolver required')
        self.native_prepared = native_prepared
        if native_auto_prepare is not None and not callable(native_auto_prepare):
            raise RaidError('native automatic preparation policy required')
        self.native_auto_prepare = native_auto_prepare
        for callback in (native_participants, native_session_prepared, native_session_auto_prepare):
            if callback is not None and not callable(callback):
                raise RaidError('native selected-session preparation callback required')
        self.native_participants = native_participants
        self.native_session_prepared = native_session_prepared
        self.native_session_auto_prepare = native_session_auto_prepare
        self.codec = codec or service._packet
        self.broadcast, self.grant_callback = broadcast, grant_callback
        self.native_deadline = native_deadline
        if native_clock_ready is not None and not callable(native_clock_ready):
            raise RaidError('native clock delivery proof resolver required')
        self.native_clock_ready = native_clock_ready
        if native_timer_deadline is not None and not callable(native_timer_deadline):
            raise RaidError('recipient native timer mapping resolver required')
        self.native_timer_deadline = native_timer_deadline
        self.reward_projection, self.result_projection = reward_projection, result_projection
        self.pc_room_eligible = pc_room_eligible or (lambda member: False)
        self.clock, self.seed_factory, self.roll = clock, seed_factory, roll or randbelow
        self.owner_resolver = owner_resolver
        self.bridge = RaidRunBridge(owner_resolver, clock=clock, barrier_classifier=barrier_classifier,
                                    member_clear=self._record_personal_quest_clear)
        self._runs, self._pending, self._projections = {}, {}, {}
        self._refund_retry = {}
        # Lobby dispatch already owns service._lock before entering this
        # adapter. Tick projects the party board, which also owns that lock.
        # One recursive owner lock serializes both paths without a separate
        # battle->service / service->battle acquisition cycle.
        self._lock = service._lock
        from .settlement_recovery import SettlementRecovery
        self.settlement_recovery = SettlementRecovery(self)
        self._configs = {c.raid_id: c for c in runtime.catalog.raids}
        self._dungeon_ids = frozenset(d.dungeon_id for c in runtime.catalog.raids
                                      for phase in c.phases for d in phase.dungeons)

    def _entry(self, session):
        cid, account, _record = self.service._actor(session)
        owner = self.owner_resolver(session)
        if (not isinstance(owner, OwnerIdentity) or owner.character_id != cid
                or owner.account_id != str(account) or owner.generation is None
                or owner.channel_id != getattr(session, 'selected_channel_id', None)):
            raise RaidError('trusted selected raid owner/generation changed')
        entry = self.service.current(cid)
        if entry is None:
            raise RaidError('selected actor has no current raid')
        if entry.raid_type != {23: 0, 32: 1}.get(owner.channel_id):
            raise RaidError('selected channel differs from the current raid family')
        member = entry.domain.member(cid)
        if member.account_id != owner.account_id:
            raise RaidError('raid account differs from repository owner')
        self.service.require_session(entry, session, cid, account)
        return entry, owner

    def _audience(self, entry, session):
        current, owner = self._entry(session)
        if current is not entry:
            raise RaidError('projection belongs to another raid')
        epoch = self.service.require_session(entry, session, owner.character_id, int(owner.account_id))
        return (entry.domain.raid_key, id(session), epoch), owner

    def projection_state(self, session):
        with self._lock:
            entry, _owner = self._entry(session)
            key, _owner = self._audience(entry, session)
            return self._projections.setdefault(key, ProjectionState())

    def pending_for(self, session):
        with self._lock:
            entry, _owner = self._entry(session)
            key, _owner = self._audience(entry, session)
            return self._pending_in_phase(entry, key)

    def _pending_in_phase(self, entry, key):
        pending = self._pending.get(key)
        if pending is not None:
            battle = entry.domain.battle
            # The snapshot's symbol cursor starts with the originating raid phase.
            if battle is None or pending[2][4][0] != battle.phase:
                self._pending.pop(key)
                pending[3]._raid_projection_sent = None
                return None
        return pending

    def forget_session(self, session):
        with self._lock:
            for mapping in (self._pending, self._projections):
                for key in tuple(mapping):
                    if key[1] == id(session):
                        mapping.pop(key, None)

    def raid_for_session(self, session):
        if getattr(session, 'selected_channel_id', None) not in (23, 32):
            return None
        try:
            return self._entry(session)[0].domain
        except RaidError:
            return None

    def is_raid_dungeon(self, dungeon_id):
        return dungeon_id in self._dungeon_ids

    def prepare_participants(self, session):
        entry, owner = self._entry(session)
        data = self._runs.get(entry.domain.raid_key)
        if data is None or entry.domain.battle is None:
            raise RaidError('normal raid has not been admitted')
        member = entry.domain.member(owner.character_id)
        if self.native_participants is not None:
            ids = tuple(self.native_participants(entry, session))
            if (not 1 <= len(ids) <= 4 or len(set(ids)) != len(ids) or owner.character_id not in ids):
                raise RaidError('actual ordinary participants unavailable')
            group = tuple(entry.domain.member(cid) for cid in ids)
        else:
            group = tuple(m for m in entry.domain.members if m.group == member.group)
        if not group or len(group) > 4 or any(m not in data.admitted for m in group):
            raise RaidError('prepared group differs from actual admitted members')
        return tuple(m.character_id for m in group)

    def _prepared_for(self, entry, session):
        if self.native_session_prepared is not None:
            return self.native_session_prepared(entry, session)
        return self.native_prepared is None or self.native_prepared(entry)

    def selector_dungeon_ids(self, session):
        if getattr(session, 'selected_channel_id', None) not in (23, 32):
            return None
        raid = self.raid_for_session(session)
        if raid is None or raid.battle is None:
            return ()
        now = self.clock()
        raid.advance_clock(now)
        battle = raid.battle
        if (battle.status != 'active' or not battle.enter_after <= now < battle.deadline
                or not self._prepared_for(self.service.current(session.selected_character_id), session)):
            return ()
        return tuple(d.dungeon_id for d in battle.dungeons if d.state == 0)

    def guard_dungeon_entry(self, session, dungeon_id, *, shared_ticket=None):
        with self._lock:
            entry, owner = self._entry(session)
            raid, now = entry.domain, self.clock()
            participants = self.prepare_participants(session)
            raid.advance_clock(now)
            battle = raid.battle
            if battle.status != 'active' or not battle.enter_after <= now < battle.deadline:
                raise RaidError('normal raid phase is not open for entry')
            if not self._prepared_for(entry, session):
                raise RaidError('native raid party preparation has not been delivered')
            if shared_ticket is not None:
                # Server-only opaque object identity, never a reconstructed C2S token.
                actual = raid._ticket_owner(shared_ticket.run_id, owner.character_id, owner.account_id)
                if (actual is not shared_ticket or actual.dungeon_id != dungeon_id
                        or actual.attempt != raid.progression.epoch
                        or tuple(m.character_id for m in actual.participants) != participants
                        or actual.run_id in raid._accepted_runs or actual.run_id in raid._retired_runs):
                    raise RaidError('follower ticket differs from this pending owned group')
            elif not any(d.dungeon_id == dungeon_id and d.state == 0 for d in battle.dungeons):
                raise RaidError('dungeon is not currently available in the actual PVF program')
            return raid, participants

    def _configuration(self, entry):
        config, rewards = self._configs[entry.raid_type], self.runtime.rewards[entry.raid_type]
        if entry.domain.single or config.minimum_members != 1:
            raise RaidError('this local profile requires actual normal minimum one; native single unsupported')
        if config.weekly_clear_limit is not None:
            raise RaidError('a verified weekly owner is required before enabling weekly policy')
        values = tuple(self.pc_room_eligible(member) for member in entry.domain.members)
        eligibility = set(values)
        if any(type(value) is not bool for value in values) or len(eligibility) != 1:
            raise RaidError('mixed or unverified PC-room eligibility is unsupported in the local profile')
        kinds = tuple(tuple(kind for kind, name in ((1, 'party_card'), (2, 'pcroom_card'), (3, 'squad_item'))
                            if (kind != 2 or True in eligibility) and any(c.kind == name for c in phase.choices))
                      for phase in rewards.phases)
        item, count = config.entry_cost[0]
        # The shared domain requires a solo field even for single=False. This
        # unused structural value copies normal minimum, not native solo data.
        admission = AdmissionRule(item, count, config.minimum_members, config.minimum_members, config.member_max)
        return BattleConfiguration(entry.family, admission, tuple(config.phase_seconds),
                                   config.start_delay_seconds, 5 if self.clear_quota else None,
                                   tuple(p.minimum_clears for p in rewards.phases), kinds,
                                   (config.path, config.path + '.symbol'),
                                   None if self.clear_quota else config.daily_account_enter_limit)

    def _preflight(self, entry, members, configuration):
        failures = tuple(issue for issue in self.runtime.catalog.issues
                         if issue.scope in ('catalog', 'ordinary_dungeon', 'entry_reward'))
        if failures:
            raise RaidError('actual normal-raid resource closure failed')
        self.runtime.reward_resolver.preflight(self.runtime.rewards[entry.raid_type])
        for member in members:
            records = self.ledger.repo.list_by_account(int(member.account_id))
            if not any(int(r.character_id) == member.character_id for r in records):
                raise RaidError('admitted member no longer belongs to repository account')

    def metadata(self, entry):
        metadata = lobby_metadata(raid_id=entry.domain.raid_key, name_bytes=entry.name_bytes,
                                  raid_type=entry.raid_type,
                                  leader=self.service._member(entry, entry.domain.member(entry.domain.leader_id)))
        battle = entry.domain.battle
        if battle is None:
            return metadata
        if battle.status not in ('active', 'completed', 'timed_out'):
            raise RaidError('native terminal metadata needs verified projection')
        return replace(metadata, state=self._native_state(entry, battle), state_index=battle.phase)

    def _native_state(self, entry, battle, session=None):
        if battle.status == 'timed_out':
            # 016486D0 case6 performs terminal scene cleanup without the
            # case4 result drawer or case5 successful-settlement directory.
            return 6
        if battle.status == 'completed':
            return 5 if battle.reward_finished_at else 4
        if battle.status == 'active':
            auto = (self.native_session_auto_prepare(entry, session)
                    if session is not None and self.native_session_auto_prepare is not None
                    else self.native_auto_prepare(entry) if self.native_auto_prepare is not None else True)
            if not auto:
                return 2
            # A renewed route or roster publication is not another raid start.
            # Admission/entry retain their independent native-ready guards.
            if self.clock() < battle.enter_after:
                return 1
            return 2
        return None

    def _party_only_display_complete(self, entry, battle, data, owner):
        # CN's normal Luke phase-zero flow has no team pool. The US 0259
        # reader at 0166FA49..0166FADE makes its no-team decision only on
        # category2 (wire kind3), after the local party cards exist.
        if (entry.raid_type != 1 or battle.phase != 0 or battle.status != 'completed'
                or 3 in data.configuration.reward_kinds[battle.phase]):
            return False
        raid = entry.domain
        frozen = raid._recipients.get((battle.phase, owner.character_id))
        if frozen is None or frozen.account_id != owner.account_id:
            return False
        visible = tuple(m for (phase, _), m in raid._recipients.items()
                        if phase == battle.phase and (m.character_id == frozen.character_id
                            or frozen.group and m.group == frozen.group))
        # Peers with a durable pending reward must not hold this owner's drawer.
        # The local card still requires a real inventory receipt before closing.
        settled = raid._committed_rewards | raid._deferred_rewards
        return bool(visible) and all((battle.phase, m.character_id, kind) in
            (raid._committed_rewards if m.character_id == owner.character_id else settled)
            for m in visible for kind in data.configuration.reward_kinds[battle.phase] if kind != 3)

    def _bodies(self, entry, battle, progress, data, projection, owner, *, session, native_clock_ready):
        state = self._native_state(entry, battle, session)
        personal_key = (battle.phase, owner.character_id, 1)
        show_personal = (battle.status == 'completed'
            and personal_key in entry.domain._committed_rewards)
        delivered = projection.personal_delivery
        show_team = (not show_personal or delivered is not None
            and delivered[0] == battle.phase and self.clock() >= delivered[1] + PERSONAL_CARD_DISPLAY_SECONDS)
        if state == 5 and not show_team:
            state = 4
        bodies = []
        before_drawer = []
        native_participation = ({} if state in (5, 6) else {
            run: (dungeon, members) for run, dungeon, members
            in self.bridge.participation_snapshot(entry.domain, phase=battle.phase)})
        # Remove before adding/rebuilding, and before 24C5 clears the native
        # directory. Each recipient commits this cursor only after its send.
        for run, previous in projection.native_participation.items():
            if native_participation.get(run) != previous:
                bodies.append((0, N_RAID_PARTICIPATION, participation_remove_body(*previous)))
        for run, current in native_participation.items():
            if projection.native_participation.get(run) != current:
                bodies.append((0, N_RAID_PARTICIPATION, participation_add_body(*current)))
        native_state = (state, battle.phase) if state is not None else projection.native_state
        party_only = self._party_only_display_complete(entry, battle, data, owner)
        finish_after_cards = state == 5 and native_state != projection.native_state
        initialize_result = (battle.status == 'completed' and state in (4, 5)
            and (projection.native_state is None or projection.native_state[1] != battle.phase
                 or projection.native_state[0] not in (4, 5)))
        wait_for_team = False
        if entry.raid_type == 0 and battle.phase == 1 and battle.status == 'completed':
            recipient = entry.domain._recipients.get((battle.phase, owner.character_id))
            wait_for_team = (recipient is not None and recipient.account_id == owner.account_id
                             and 3 in data.configuration.reward_kinds[battle.phase]
                             and self.reward_projection is None and self.result_projection is None)
        if initialize_result:
            if self.result_projection is not None:
                bodies.extend(self.result_projection(entry, battle, dict(data.phase_deaths)))
            else:
                deaths = data.phase_deaths.get((battle.phase, owner.character_id), 0)
                rank = self.runtime.rewards[entry.raid_type].phases[battle.phase].rank_for_deaths(deaths)
                # 007BE450 initializes the unlabeled number board to zero.
                # Flag1 leaves environment+31C at its native reset value0;
                # it is not the PC-room eligibility flag.
                bodies.append((0, N_RAID_RESULT, result_body(native_kind=0,
                    phase_index=battle.phase,
                    elapsed_seconds=max(0, int(battle.completed_at - battle.enter_after)),
                    display_count=0, rank=rank, environment_flag_byte=1)))
            if state == 5:
                # Recreate the result drawer before replaying already granted
                # cards; retain the truthful terminal state in the next frame.
                bodies.append((0, N_RAID_STATE, encode_raid_state(4, battle.phase)))
        if state is not None and native_state != projection.native_state and not finish_after_cards:
            bodies.append((0, N_RAID_STATE, encode_raid_state(state, battle.phase)))
            if state == 1:
                bodies.append((0, N_PREPARE_START_RAID, encode_prepare_start()))
        native_dungeons = dict(projection.native_dungeons)
        native_tracked_dungeon = projection.native_tracked_dungeon
        if battle.status == 'active':
            native_dungeons = {d.dungeon_id: d.state for d in battle.dungeons}
            native_tracked_dungeon = progress.infection_dungeon
            if (projection.native_state is None or projection.native_state[1] != battle.phase
                    or projection.native_tracked_dungeon != native_tracked_dungeon):
                bodies.append((0, N_RAID_DUNGEON_STATE, dungeon_states_body(
                    native_dungeons.items(), tracked_dungeon_id=native_tracked_dungeon)))
            else:
                for dungeon, value in native_dungeons.items():
                    if projection.native_dungeons.get(dungeon) != value:
                        bodies.append((0, 0x024b, dungeon_change_body(dungeon, value)))
        elif (entry.raid_type == 0 and battle.status == 'completed'
                and projection.native_state != (5, battle.phase)
                and (not projection.native_dungeons or projection.native_state is None
                     or projection.native_state[1] != battle.phase)):
            # The native delta reader needs a directory. A newly connected
            # recipient can see the first result before seeing any active map.
            native_dungeons = {d.dungeon_id: d.state for d in battle.dungeons}
            native_tracked_dungeon = getattr(progress, 'infection_dungeon', 0)
            if native_tracked_dungeon not in native_dungeons:
                native_tracked_dungeon = 0
            before_drawer.append((0, N_RAID_DUNGEON_STATE, dungeon_states_body(
                native_dungeons.items(), tracked_dungeon_id=native_tracked_dungeon)))
            if state == 5:
                native_dungeons = {}
        elif battle.status == 'completed' and projection.native_dungeons:
            # Publish the final per-map changes ahead of the result drawer.
            # Only diff a map directory already delivered for this phase.
            if projection.native_state is not None and projection.native_state[1] == battle.phase:
                native_dungeons = {d.dungeon_id: d.state for d in battle.dungeons}
                before_drawer.extend((0, 0x024b, dungeon_change_body(d, value))
                    for d, value in native_dungeons.items()
                    if d in projection.native_dungeons and projection.native_dungeons[d] != value)
            if state == 5:
                native_dungeons = {}
        elif battle.status == 'completed' and state == 5:
            # 24C4 starts the reward drawer. Only 24C5 clears the native map
            # directory, after authoritative reward delivery has finished.
            native_dungeons = {}
        elif state == 6:
            native_dungeons = {}
            native_tracked_dungeon = 0
            if projection.native_dungeons or projection.native_state != native_state:
                bodies.append((0, N_RAID_DUNGEON_STATE, dungeon_states_body((), tracked_dungeon_id=0)))
        cursor = projection.symbol_cursor[2] if projection.symbol_cursor[:2] == (battle.phase, progress.epoch) else 0
        writes = progress.writes_after(cursor)
        for offset in range(0, len(writes), 255):
            destination = before_drawer if battle.status == 'completed' else bodies
            destination.append((0, N_RAID_SYMBOL, symbols_body((w.symbol_id, w.value) for w in writes[offset:offset + 255])))
        bodies[:0] = before_drawer
        native_timers = ({} if state == 6 else
            {key: value for key, value in projection.native_timers.items() if key in progress.timers})
        if ((self.native_deadline is not None or self.native_timer_deadline is not None)
                and native_clock_ready and battle.status == 'active'):
            for (timer_id, dungeon_id), timer in progress.timers.items():
                if projection.native_timers.get((timer_id, dungeon_id)) != (timer.end, timer.duration):
                    end = (self.native_timer_deadline(session, entry, timer.end, timer.duration)
                           if self.native_timer_deadline is not None
                           else self.native_deadline(entry, timer.end, timer.duration))
                    if end is None:
                        continue  # unverified native clock: keep backend timer only
                    bodies.append((0, 0x0255, delay_body(timer_id, dungeon_id, end, timer.duration)))
                    native_timers[(timer_id, dungeon_id)] = (timer.end, timer.duration)
        remaining = max(0, ceil(battle.deadline - self.clock())) if battle.status == 'active' else 0
        native_remaining = {**projection.native_remaining, 0: remaining}
        if projection.native_remaining.get(0) != remaining:
            bodies.append((0, N_RAID_REMAIN_TIME, encode_raid_timer(0, remaining)))
        if data.break_until:
            remaining_reward = max(0, ceil(data.break_until - self.clock()))
            native_remaining[1] = remaining_reward
            if projection.native_remaining.get(1) != remaining_reward:
                bodies.append((0, N_RAID_REMAIN_TIME, encode_raid_timer(1, remaining_reward)))
        elif projection.native_remaining.get(1, 0):
            native_remaining[1] = 0
            bodies.append((0, N_RAID_REMAIN_TIME, encode_raid_timer(1, 0)))
        native_phase_revives = projection.native_phase_revives
        limits = getattr(self.runtime, 'revive_limits', None)
        rules = limits.get(entry.raid_type) if limits is not None else None
        if rules is not None and rules.phase[battle.phase]:
            native_phase_revives = (battle.phase, data.phase_revives.get(battle.phase, 0))
        elif rules is not None and entry.raid_type == 0:
            # Anton's resource limit belongs to the current shared run, not
            # the whole phase. The matching native board extension supplies
            # the resource cap (12); 0308 remains an actual used-count byte.
            binding = self.bridge.current(session) if session is not None else None
            ticket = binding.ticket if binding is not None else None
            run = (ticket.run_id if ticket is not None
                   and ticket.run_id in entry.domain._accepted_runs
                   and ticket.run_id not in entry.domain._retired_runs
                   and ticket.phase == battle.phase else None)
            native_phase_revives = (battle.phase, run, data.run_revives.get(run, 0))
        if native_phase_revives != projection.native_phase_revives:
            bodies.append((0, N_RAID_PHASE_COIN_INFO, encode_phase_coin_info(native_phase_revives[-1])))
        native_parties = encode_parties(party_snapshot(self.service, entry) if state not in (5, 6) else (),
                                       self.bridge, entry.domain, revive_uses=data.run_revives)
        if native_parties != projection.native_parties:
            bodies.append((0, N_RAID_PARTIES, native_parties))
        from .skills import N_RAID_SKILLS, projection as project_skills
        native_skills = project_skills(self, entry, battle, data, session, native_clock_ready)
        if native_skills != projection.native_skills:
            bodies.append((0, N_RAID_SKILLS, native_skills))
        if initialize_result and wait_for_team:
            bodies.append((0, N_RAID_SYMBOL, symbols_body(((ANTON_TEAM_DRAWER_SYMBOL,
                owner.character_id << 8),))))
        reward_keys = {key for key in projection.native_rewards if key[1] == battle.phase}
        # Kind3 can switch the native reward drawer (0166FA07..0166FA10).
        # On a fresh scene or a last-member claim, finish every visible
        # personal category before delivering any team-category transition.
        ordered_rewards = sorted(data.reward_plans.items(),
                                 key=lambda row: (row[0][1], row[0][3], row[0][2]))
        team_rows = []
        for key, (_card, plan, entitlement) in ordered_rewards:
            if (entitlement.phase == battle.phase and self._reward_visible(entry, owner, entitlement)
                    and (entitlement.phase, entitlement.character_id, entitlement.kind) in entry.domain._committed_rewards):
                if entitlement.kind == 3 and not show_team:
                    continue
                if entitlement.kind == 3 and self.reward_projection is None:
                    team_rows.append((key, plan, entitlement))
                    continue
                if key in reward_keys:
                    continue
                bodies.extend(self._reward_bodies(entry, entitlement, plan))
                reward_keys.add(key)
        if (any(ent.character_id == owner.character_id for _, _, ent in team_rows)
                and any(key not in reward_keys for key, _, _ in team_rows)):
            # 0166FA10 checks the local member after every kind3 packet. Send
            # an atomic team snapshot containing self, including on a later
            # partial-grant retry; never expose peers before self has settled.
            members = tuple(MemberReward(ent.character_id, ent.card,
                tuple(NativeRewardItem(item.display_kind, item.item_id, item.quantity)
                      for item in plan.items)) for _, plan, ent in team_rows)
            if wait_for_team:
                # Accepted grants and their card contents are immutable per settlement.
                # A larger owner-visible membership is a new complete snapshot.
                bodies.append((0, N_RAID_SYMBOL, symbols_body(((ANTON_TEAM_DRAWER_SYMBOL,
                    (owner.character_id << 8) | len(members)),))))
            bodies.append((0, N_RAID_REWARD_LIST, encode_reward_list(3, members)))
            reward_keys.update(key for key, _, _ in team_rows)
        native_party_only_completion = projection.native_party_only_completion
        if party_only and show_team and native_party_only_completion != battle.phase:
            # This contains zero award rows. It must follow the local cards,
            # never create a kind3 entitlement or wait for another squad's click.
            bodies.append((0, N_RAID_REWARD_LIST, encode_reward_list(3, ())))
            native_party_only_completion = battle.phase
        if finish_after_cards:
            bodies.append((0, N_RAID_STATE, encode_raid_state(state, battle.phase)))
        snapshot = (native_state, native_dungeons, native_timers, native_remaining,
                    (battle.phase, progress.epoch, writes[-1].sequence if writes else cursor),
                    native_tracked_dungeon, native_participation, native_phase_revives, native_parties, native_skills,
                    frozenset(reward_keys), native_party_only_completion)
        return bodies, snapshot

    def _project(self, entry, session):
        key, owner = self._audience(entry, session)
        self._commit_clear(entry)
        generation = int(getattr(session, 'town_scene_generation', 0) or 0)
        projection = self._projections.setdefault(key, ProjectionState())
        pending_result = self._pending_in_phase(entry, key)
        battle = entry.domain.battle
        if (projection.scene_generation != generation and pending_result is not None
                and battle is not None and battle.status == 'completed'
                and pending_result[2][0] in ((4, battle.phase), (5, battle.phase))):
            # A town return can arrive between writes in a result batch. Keep
            # the actual-write receipts and retry only its unsent occurrences;
            # resetting here would reopen cards already received by this owner.
            projection.scene_generation = generation
        if projection.scene_generation != generation:
            self._pending.pop(key, None)
            session._raid_projection_sent = None
            battle = entry.domain.battle
            delivered_result = (battle is not None and battle.status == 'completed'
                and projection.native_state in ((4, battle.phase), (5, battle.phase)))
            previous = projection
            projection = ProjectionState(scene_generation=generation)
            if delivered_result:
                # This cursor is bound to the selected-session epoch. Returning
                # to town must not reopen an already delivered result drawer.
                # New connections/selections still start with an empty cursor,
                # so recovery can replay unobserved results and reward cards.
                projection.native_state = previous.native_state
                projection.native_rewards = previous.native_rewards
                projection.personal_delivery = previous.personal_delivery
                projection.native_party_only_completion = previous.native_party_only_completion
            self._projections[key] = projection
        pending = self._pending.get(key)
        if pending is not None:
            sent = getattr(session, '_raid_projection_sent', None)
            if sent is not None and sent[0] is pending[0] and sent[1]:
                # Retry only occurrences whose actual owner socket write has
                # not completed. A later ACK failure must not replay an already
                # delivered reward display (or reset other projected UI state).
                remaining = [packet for index, packet in enumerate(pending[0])
                             if index not in sent[1]]
                if not remaining:
                    self.projection_sent(entry, pending[0], session)
                    session._raid_projection_sent = None
                    return []
                pending = (remaining, *pending[1:])
                self._pending[key] = pending
                session._raid_projection_sent = None
            return pending[0]  # Keep the current unsent batch's exact identity.
        data, battle, progress = self._runs[entry.domain.raid_key], entry.domain.battle, entry.domain.progression
        projection = self._projections.setdefault(key, ProjectionState())
        bodies, snapshot = self._bodies(entry, battle, progress, data, projection, owner, session=session,
            native_clock_ready=self.native_clock_ready is None or self.native_clock_ready(session))
        batch = [self.codec(*body) for body in bodies]
        if batch and battle.phase == 0 and battle.status == 'completed':
            from .local_bindings import PhaseResultDelivery
            delivery = getattr(data, 'phase_result_delivery', None)
            if delivery is None:
                delivery = data.phase_result_delivery = PhaseResultDelivery()
            batch = [delivery.packet(packet) for packet in batch]
        if batch:
            self._pending[key] = (batch, projection, snapshot, session, owner)
            if self.broadcast is not None and self.broadcast(entry, batch) is True:
                self.projection_sent(entry, batch, session)
                return []
        return batch

    def projection_sent(self, entry, batch, session=None):
        """Confirm only the exact batch, owner and selected-session generation."""
        with self._lock:
            matches = [(key, value) for key, value in self._pending.items()
                       if key[0] == entry.domain.raid_key and value[0] is batch
                       and (session is None or value[3] is session)]
            if len(matches) != 1:
                return False
            key, pending = matches[0]
            try:
                current_key, owner = self._audience(entry, pending[3])
            except RaidError:
                return False
            if current_key != key or owner != pending[4] or self._pending_in_phase(entry, key) is not pending:
                return False
            generation = int(getattr(pending[3], 'town_scene_generation', 0) or 0)
            if pending[1].scene_generation != generation:
                battle = entry.domain.battle
                if (battle is None or battle.status != 'completed'
                        or pending[2][0] not in ((4, battle.phase), (5, battle.phase))):
                    return False
                # 0084 retires/queues the result before ordinary return
                # increments the scene. Successful writes can therefore be
                # confirmed before _project gets another chance to reconcile
                # the scene. Keep the same owner/epoch/phase checks here too.
                pending[1].scene_generation = generation
            projection = pending[1]
            (projection.native_state, projection.native_dungeons, projection.native_timers,
             projection.native_remaining, projection.symbol_cursor,
             projection.native_tracked_dungeon, projection.native_participation,
             projection.native_phase_revives, projection.native_parties, projection.native_skills,
             projection.native_rewards, projection.native_party_only_completion) = pending[2]
            phase = pending[2][4][0]
            if projection.native_state == (4, phase):
                data = self._runs[entry.domain.raid_key]
                data.drawer_opened.setdefault((phase, owner.character_id), self.clock())
            if ((projection.personal_delivery is None or projection.personal_delivery[0] != phase)
                    and any(k[1:] == (phase, owner.character_id, 1) for k in projection.native_rewards)):
                projection.personal_delivery = (phase, self.clock())
            self._pending.pop(key, None)
            return True

    def start(self, entry, body, session):
        with self._lock:
            parse_start_request(body)
            current, owner = self._entry(session)
            if current is not entry or getattr(session, 'in_dungeon_run', False):
                raise RaidError('START belongs to another raid or an active dungeon')
            if entry.domain.battle is not None:
                battle = entry.domain.battle
                if battle.phase != 0 or battle.status != 'completed' or not battle.reward_finished_at:
                    raise RaidError('normal raid already started or first-phase rewards unfinished')
                packets = self._next_phase(entry, owner.character_id, session)
                if packets is None:
                    raise RaidError('first-phase result delivery is finishing; phase switch scheduled')
                packets.append(self.codec(1, C_START_RAID, b'\x01'))
                session.raid_phase = entry.domain.battle.phase
                return packets, 'owned leader continued the actual second phase; no additional entry debit'
            if (self.native_prepared is not None and len(entry.domain.members) > 1
                    and not self.native_prepared(entry)):
                raise RaidError('complete native multiplayer preparation required before admission')
            configuration = self._configuration(entry)
            config = self._configs[entry.raid_type]
            for member in entry.domain.members:
                stock = self.ledger.repo.count_inventory_items_by_template(member.character_id,
                    item_ids=tuple(item for item, count in config.entry_cost))
                try:
                    choose_entry_cost(config.entry_cost, stock)
                except EntryCostUnavailable as exc:
                    raise EntryMaterialsMissing('owned entry materials unavailable for character%s' % member.character_id) from exc
            now = self.clock()
            progress = self.runtime.programs[entry.raid_type].begin(0, now + configuration.start_delay_seconds, self.seed_factory())
            data = RunData(configuration, entry.domain.members)
            key, projection_owner = self._audience(entry, session)
            projection = self._projections.setdefault(key, ProjectionState())
            preview = BattleSnapshot('preflight-only', 0, 'active', now,
                                     now + configuration.start_delay_seconds,
                                     now + configuration.start_delay_seconds + configuration.phase_seconds[0], progress.states, ())
            bodies, snapshot = self._bodies(entry, preview, progress, data, projection, owner, session=session,
                native_clock_ready=self.native_clock_ready is None or self.native_clock_ready(session))
            bodies.insert(0, (0, N_RAID_PARTICIPATION, encode_participation(())))
            bodies.append((1, C_START_RAID, b'\x01'))
            # Encode every known field before the atomic debit can commit.
            packets = [self.codec(*value) for value in bodies]
            config = self._configs[entry.raid_type]
            period = self.entry_period_resolver() if self.entry_period_resolver is not None else self.entry_period_key
            if not isinstance(period, str) or not period.strip() or period != period.strip() or len(period) > 128:
                raise RaidError('trusted daily admission period unavailable')
            weekly_counts = {}
            if self.clear_quota:
                from .clear_quota import period_keys, ClearQuotaExceeded
                period = period_keys(now)[0]
                for member in entry.domain.members:
                    daily, weekly = self.ledger.clear_counts(member.character_id, int(member.account_id), entry.family, now)
                    if daily >= 1 or weekly >= 5:
                        raise ClearQuotaExceeded('character daily (1) or weekly (5) raid clear limit reached')
                    weekly_counts[member.character_id] = weekly
            counts = {m.account_id: self.ledger.daily_count(int(m.account_id), entry.family, period)
                      for m in entry.domain.members}
            def commit(request):
                self.ledger.admit_from_alternatives(request.instance_id, request.family,
                    tuple((m.character_id, int(m.account_id)) for m in request.members),
                    config.entry_cost, period, None if self.clear_quota else config.daily_account_enter_limit,
                    clear_quota_at=now if self.clear_quota else None)
            battle = entry.domain.start(owner.character_id, configuration, weekly_counts, now,
                         lambda members, configuration: self._preflight(entry, members, configuration), commit,
                         program=progress, daily_account_counts=counts)
            self._runs[entry.domain.raid_key] = data
            self._pending[key] = (packets, projection, snapshot, session, projection_owner)
            for member in data.admitted:
                data.phase_deaths[(0, member.character_id)] = 0
            session.raid_instance_id, session.raid_phase = battle.instance_id, battle.phase
            for member in data.admitted:
                self.service.queue_inventory_refresh(member.character_id, member.account_id, (0,))
            return packets, 'normal raid started with actual PVF program and atomic OR entry debit'

    def admit_during_break(self, entry, member):
        with self._lock:
            raid, now = entry.domain, self.clock()
            data = self._runs[raid.raid_key]
            if (not raid.preparation_open() or raid.battle is None
                    or not data.break_until or now >= data.break_until):
                raise RaidError('phase-break admission window has closed')
            from .admission_extensions import admit
            admit(self.ledger, raid.battle.instance_id, entry.family, member,
                  self._configs[entry.raid_type].entry_cost, now, clear_quota=self.clear_quota)
            data.admitted = tuple(m for m in data.admitted if m.character_id != member.character_id) + (member,)
            self.service.exit_refunds.pending.pop((raid.battle.instance_id,member.character_id),None)
            self.service.queue_inventory_refresh(member.character_id,member.account_id,(0,))

    def _next_phase(self, entry, actor, session):
        raid, now, data = entry.domain, self.clock(), self._runs[entry.domain.raid_key]
        if actor != raid.leader_id:
            raise RaidError('current leader required to continue the normal raid')
        # Let connected recipients finish their first result before a leader's
        # continue request invalidates the phase-one send receipts.
        if now < raid.battle.reward_finished_at + REWARD_OFFLINE_FALLBACK_SECONDS:
            for selected, actual, cid, _aid, epoch in self.service._raid_sessions.values():
                if actual is entry and (0, cid, 1) in raid._committed_rewards:
                    viewer = self._projections.get((raid.raid_key, id(selected), epoch))
                    if viewer is None or viewer.native_state != (5, 0):
                        data.break_until = now
                        return None
        enter = now + data.configuration.start_delay_seconds
        progress = raid.progression.next_phase(enter)
        old = raid.battle
        preview = BattleSnapshot(old.instance_id, 1, 'active', now, enter,
                                enter + data.configuration.phase_seconds[1], progress.states, ())
        delivery = getattr(data, 'phase_result_delivery', None)
        old_break = data.break_until
        data.break_until = 0
        try:
            key, owner = self._audience(entry, session)
            projection = self._projections.setdefault(key, ProjectionState())
            bodies, snapshot = self._bodies(entry, preview, progress, data, projection, owner, session=session,
                native_clock_ready=self.native_clock_ready is None or self.native_clock_ready(session))
            packets = [self.codec(*body) for body in bodies]
            if delivery is not None and not delivery.try_close():
                data.break_until = min(old_break, now)
                return None
            raid.next_phase(actor, None, now)
        except Exception:
            if delivery is not None:
                delivery.reopen()
            data.break_until = old_break
            raise
        for member in data.admitted:
            data.phase_deaths[(1, member.character_id)] = 0
        # Retire outstanding first-phase notifications for every selected owner.
        for pending_key in tuple(self._pending):
            if pending_key[0] == raid.raid_key:
                self._pending_in_phase(entry, pending_key)
        self._pending[key] = (packets, projection, snapshot, session, owner)
        return packets

    def material_snapshot(self, entry, *, force=False):
        # The per-raid cache bounds full-party inventory reads, even when many
        # recipients are looking at the same roster or entering simultaneously.
        from time import monotonic
        now = monotonic()
        members = tuple(m.character_id for m in entry.domain.members)
        cache = getattr(entry, '_material_cache', None)
        if not force and cache and cache[0] == members and now < cache[1]:
            return cache[2]
        config = self._configs[entry.raid_type]
        rows = []
        for member in entry.domain.members:
            stock = self.ledger.repo.count_inventory_items_by_template(member.character_id,
                              item_ids=tuple(item for item, count in config.entry_cost))
            try:
                choose_entry_cost(config.entry_cost, stock)
                owned = 1
            except EntryCostUnavailable:
                owned = 0
            # Native shortage UI compares/displays firstpair current stock.
            # Its u16 is rendered signed: cap DISPLAY ONLY at32767. Atomic
            # selection/debit still uses the exact unrestricted stock.
            held = stock.get(config.entry_cost[0][0], 0)
            rows.append(EntryCost(member.character_id, owned, min(held, 32767)))
        body = encode_entry_cost(rows)
        entry._material_cache = (members, now + 1.0, body)
        return body

    def entry_cost_info(self, entry, body, session):
        with self._lock:
            parse_entry_cost_request(body)
            actual, _owner = self._entry(session)
            if actual is not entry:
                raise RaidError('entry-cost query belongs to another raid')
            value = self.material_snapshot(entry, force=True)
            packet = self.service.view_packet(session, 'material', self.service.view_owner(session, entry),
                                              N_RAID_ENTRY_COST_INFO, value, force=True)
            return [packet, self.codec(1, C_RAID_ENTRY_COST_INFO, b'\x01')], 'actual ticket availability and held count'

    def _reward_visible(self, entry, owner, entitlement):
        if entitlement.kind == 3 or entitlement.character_id == owner.character_id:
            return True
        recipients = entry.domain._recipients
        viewer = recipients.get((entitlement.phase, owner.character_id))
        subject = recipients.get((entitlement.phase, entitlement.character_id))
        # Group zero means unassigned, not one shared four-card squad.
        return (viewer is not None and subject is not None and viewer.group > 0
                and viewer.group == subject.group)

    def _available_reward_card(self, entry, member, kind, preferred=0):
        raid, phase = entry.domain, entry.domain.battle.phase
        existing = raid._entitlements.get((phase, member.character_id, kind))
        if existing is not None:
            return existing.card
        occupied = set()
        for (p, cid, k), taken in raid._entitlements.items():
            other = raid._recipients.get((p, cid))
            if (p == phase and k == kind and cid != member.character_id
                    and (kind == 3 or other is not None and member.group > 0 and other.group == member.group)):
                occupied.add(taken.card)
        limit = 20 if kind == 3 else 4
        for card in (preferred, *range(limit)):
            if 0 <= card < limit and card not in occupied:
                return card
        raise RewardCardOccupied('no unused card remains for this frozen reward group')

    def _reward_bodies(self, entry, entitlement, plan):
        if self.reward_projection is not None:
            bodies = self.reward_projection(entry, entitlement, plan)
        else:
            # Current config enums: party0, absent gold1, PC2, squad3.
            group = {1: 0, 2: 2, 3: 3}[entitlement.kind]
            items = tuple(NativeRewardItem(item.display_kind, item.item_id, item.quantity) for item in plan.items)
            bodies = ((0, N_RAID_REWARD_LIST, encode_reward_list(group, (
                      MemberReward(entitlement.character_id, entitlement.card, items),))),)
        return bodies

    def _reward_packets(self, entry, entitlement, plan):
        return [self.codec(*body) for body in self._reward_bodies(entry, entitlement, plan)]

    def _commit_clear(self, entry):
        battle = entry.domain.battle
        if battle is None or battle.status != 'completed':
            return
        data=self._runs[entry.domain.raid_key]
        checkpointed=getattr(data,'checkpointed_phases',set())
        durable=self.grant_callback is None
        def persist(intent=None):
            with self.ledger.repo.inventory_transaction_scope() as conn:
                first=self.ledger.commit_clear(intent) if intent is not None else True
                if durable and battle.phase not in checkpointed:
                    from .reward_checkpoints import stage
                    from .runtime import RewardEntitlement
                    recipients=tuple(m for (phase,cid),m in sorted(entry.domain._recipients.items()) if phase==battle.phase)
                    for card,member in enumerate(recipients):
                        for kind in data.configuration.reward_kinds[battle.phase]:
                            plan=self._reward_plan(entry,member,kind,data)
                            entitlement=RewardEntitlement(battle.instance_id,entry.family,battle.phase,
                                member.character_id,member.account_id,kind,card if kind==3 else 0,
                                plan.fingerprint,dict(battle.member_clears).get(member.character_id,0),
                                data.configuration.minimum_clears[battle.phase],battle.completed_at)
                            stage(self.ledger,conn,entitlement,plan)
                return not first
        if self.clear_quota and battle.phase==1:
            entry.domain.commit_weekly(persist)
        elif durable and battle.phase not in checkpointed:
            persist()
        if durable:
            checkpointed.add(battle.phase);data.checkpointed_phases=checkpointed

    def _reward_plan(self,entry,member,kind,data):
        battle=entry.domain.battle
        key=(battle.instance_id,battle.phase,member.character_id,kind)
        plans=getattr(data,'checkpoint_plans',None)
        if plans is None:data.checkpoint_plans=plans={}
        cached=data.reward_plans.get(key)
        phase=self.runtime.rewards[entry.raid_type].phases[battle.phase]
        name={1:'party_card',2:'pcroom_card',3:'squad_item'}[kind]
        def create_plan():
            if cached is not None:return cached[1]
            if key not in plans:
                plans[key]=freeze_reward_plan(phase,name,data.phase_deaths[(battle.phase,member.character_id)],
                    dict(battle.member_clears).get(member.character_id,0),True,self.roll,self.runtime.reward_resolver,
                    pc_room_eligible=kind!=2 or self.pc_room_eligible(member) is True)
            return plans[key]
        daily=getattr(self.ledger,'freeze_daily_phase_reward',None)
        return (daily(battle.instance_id,entry.family,member.character_id,int(member.account_id),
                       battle.completed_at,kind,create_plan,durable_backend=self.grant_callback is None)
                if battle.phase==0 and daily is not None else create_plan())

    def _freeze_claim(self, entry, member, kind, card, *, data=None):
        raid, battle = entry.domain, entry.domain.battle
        data = self._runs[entry.domain.raid_key] if data is None else data
        if battle is None or battle.status != 'completed' or kind not in data.configuration.reward_kinds[battle.phase]:
            raise RaidError('actual completed configured reward kind required')
        self._commit_clear(entry)
        key = (battle.instance_id, battle.phase, member.character_id, kind)
        cached = data.reward_plans.get(key)
        if cached is not None:
            old_card, plan, entitlement = cached
            if old_card != card:
                raise RewardCardOccupied('this owned reward was already frozen for another card')
        else:
            # Domain validates the recipient before any RNG draw. Fingerprint
            # is not supplied by a C2S request; this preliminary owner check
            # uses the actual frozen recipient journal from Raid.clear.
            recipient = raid._recipients.get((battle.phase, member.character_id))
            if recipient != member:
                raise RaidError('actor is not this phase\'s qualified owned recipient')
            plan = self._reward_plan(entry,member,kind,data)
            entitlement = raid.freeze_reward(member.character_id, member.account_id, kind, card, plan.fingerprint)
            data.reward_plans[key] = (card, plan, entitlement)
        if self.grant_callback is None:
            from .reward_checkpoints import bind_choice
            bind_choice(self.ledger,entitlement,plan)
        return plan,entitlement

    def _grant_frozen(self,entry,entitlement,plan):
        def commit(entitlement):
            if self.grant_callback is not None:
                return self.grant_callback(entitlement, plan, self.runtime.reward_resolver)
            first=self.ledger.grant_entitlement(entitlement, plan, self.runtime.reward_resolver)
            self.service.queue_inventory_refresh(entitlement.character_id,entitlement.account_id,
                                                (item.list_type for item in plan.items))
            return not first
        entry.domain.grant_reward(entitlement, commit)

    def _claim(self, entry, member, kind, card, *, display=True):
        battle=entry.domain.battle;data=self._runs[entry.domain.raid_key]
        plan,entitlement=self._freeze_claim(entry,member,kind,card)
        key=(battle.instance_id,battle.phase,member.character_id,kind)
        packets=self._reward_packets(entry,entitlement,plan) if display else []
        self._grant_frozen(entry,entitlement,plan)
        replay=getattr(self.ledger,'daily_phase_reward_replay',None)
        noticed=getattr(data,'daily_reward_notices',None)
        if noticed is None:data.daily_reward_notices=noticed=set()
        if (kind==1 and key not in noticed and replay is not None
                and replay(entitlement,plan)):
            from .daily_rewards import repeat_notice
            packets.append(repeat_notice(self.service.server));noticed.add(key)
        return packets

    def _finish_phase_rewards(self, entry):
        raid, data, now = entry.domain, self._runs[entry.domain.raid_key], self.clock()
        battle = raid.battle
        recipients = tuple(m for (phase, cid), m in sorted(raid._recipients.items()) if phase == battle.phase)
        required = {(battle.phase, m.character_id, kind) for m in recipients
                    for kind in data.configuration.reward_kinds[battle.phase]}
        if required and required <= raid._committed_rewards | raid._deferred_rewards:
            raid.finish_rewards(now)
            if battle.phase == 0 and not data.break_until:
                data.break_until = raid.battle.reward_finished_at + self._configs[entry.raid_type].phase_break_seconds

    def _auto_squad(self, entry):
        raid, data, battle = entry.domain, self._runs[entry.domain.raid_key], entry.domain.battle
        recipients = tuple(m for (phase, cid), m in sorted(raid._recipients.items()) if phase == battle.phase)
        required_party = {(battle.phase, m.character_id, kind) for m in recipients
                          for kind in data.configuration.reward_kinds[battle.phase] if kind != 3}
        packets = []
        settled=raid._committed_rewards | raid._deferred_rewards
        # A missing click or a temporarily hidden result drawer must not hold
        # every member's second round indefinitely. Existing frozen plans and
        # the durable ledger make this timeout a choice, never a second grant.
        now = self.clock()
        for member in recipients:
            opened = data.drawer_opened.get((battle.phase, member.character_id))
            fallback = battle.completed_at + REWARD_OFFLINE_FALLBACK_SECONDS
            deadline = fallback if opened is None else opened + REWARD_AUTO_SELECT_SECONDS
            if now >= deadline:
                for kind in data.configuration.reward_kinds[battle.phase]:
                    if kind == 3 or (battle.phase, member.character_id, kind) in settled:
                        continue
                    card = self._available_reward_card(entry, member, kind)
                    plan, entitlement = self._freeze_claim(entry, member, kind, card)
                    self._settle_automatic(entry, entitlement, plan)
        settled = raid._committed_rewards | raid._deferred_rewards
        if required_party <= settled and 3 in data.configuration.reward_kinds[battle.phase]:
            for card, member in enumerate(recipients):
                if (battle.phase, member.character_id, 3) not in settled:
                    # Commit once, then let each recipient's projection encode
                    # its own display and acknowledge its actual TCP delivery.
                    plan, entitlement = self._freeze_claim(entry, member, 3, card)
                    self._settle_automatic(entry, entitlement, plan)
        self._finish_phase_rewards(entry)
        return packets

    def _settle_automatic(self, entry, entitlement, plan):
        try:
            self._grant_frozen(entry, entitlement, plan)
        except LedgerError as exc:
            # Only the repository's exact capacity rejection is retryable here.
            # Identity, plan, database and callback failures must remain errors.
            if self.grant_callback is not None or str(exc) != 'reward grant failed: container_full':
                raise
            # Persist the frozen award before allowing the other recipients and
            # next phase to proceed. Recovery grants this same plan once space
            # is available, including after a restart or departure.
            entry.domain.defer_reward(entitlement,
                lambda owned: self.ledger.stage_pending_reward(owned, plan))

    def select_reward(self, entry, body, session):
        with self._lock:
            group, card = parse_select_card_request(body)
            actual, owner = self._entry(session)
            if actual is not entry or entry.domain.battle is None or entry.domain.battle.status != 'completed' or card > 3:
                raise RaidError('owned completed phase and legal card required')
            if group == 1:
                # Current US PVF declares no gold pool. Native sends gold
                # before party unconditionally; finish the empty request only.
                return [self.codec(1, C_SELECT_RAID_REWARD_CARD, b'\x01')], 'configured gold pool absent; acknowledged empty selection'
            if group not in (0, 2):
                raise RaidError('squad is server settlement; unsupported reward request group')
            kind = 1 if group == 0 else 2
            member = entry.domain._recipients.get((entry.domain.battle.phase, owner.character_id))
            if member is None or member.account_id != owner.account_id:
                raise RaidError('owned frozen reward recipient required')
            try:
                packets = self._claim(entry, member, kind, card, display=False)
            except RewardCardOccupied:
                packets = list(self._project(entry, session))
                packets.append(self.codec(1, C_SELECT_RAID_REWARD_CARD, b'\0'))
                key, _owner = self._audience(entry, session)
                pending = self._pending.get(key)
                if pending is not None:
                    self._pending[key] = (packets, *pending[1:])
                return packets, 'reward card occupied; actual choices replayed without granting'
            packets.extend(self._auto_squad(entry))
            packets.extend(self._project(entry, session))
            # Preserve the actual returned object as the pending projection
            # batch even when reward packets are prepended to the same list.
            key, _owner = self._audience(entry, session)
            pending = self._pending.get(key)
            packets.append(self.codec(1, C_SELECT_RAID_REWARD_CARD, b'\x01'))
            if pending is not None:
                self._pending[key] = (packets, *pending[1:])
            return packets, 'owned frozen party reward and automatic squad; atomic durable callback'

    def _record_personal_quest_clear(self, family, phase, proof):
        from game.quest import anton_volcano
        if family == "anton" and phase == 1 and proof.dungeon_id == anton_volcano.RAID_DUNGEON_ID:
            anton_volcano.record_quest_clear(self.ledger.repo, proof.character_id, proof.account_id)

    def on_bridge_update(self, session, update):
        with self._lock:
            entry, _owner = self._entry(session)
            session.raid_phase = entry.domain.battle.phase
            # RaidRunBridge already changed the domain from actual journals.
            # An arbitrary update flag cannot itself create a clear or reward.
            return self._project(entry, session) if update.accepted or update.cleared else []

    def on_run_retired(self, session, binding, reason):
        with self._lock:
            # Ordinary exit/close may already have reset the selected session.
            # The trusted bridge's frozen capability still owns this cleanup.
            entry = self.service.current(binding.owner.character_id)
            if entry is None or entry.domain._tickets.get(binding.ticket.run_id) is not binding.ticket:
                return []
            return self._project(entry, session)

    def party_hp_report(self, session, body):
        from struct import unpack_from
        from .party_board import party_snapshot
        if not isinstance(body, bytes) or len(body) < 5:
            raise RaidError('party HP report header is incomplete')
        maze, count = unpack_from('<IB', body)
        if len(body) != 5 + count * 4:
            raise RaidError('party HP report has an invalid count')
        percentages = unpack_from('<' + 'I' * count, body, 5)
        if any(value > 100 for value in percentages):
            raise RaidError('party HP percentage exceeds 100')
        with self._lock:
            entry, owner = self._entry(session)
            if not any(row.host_id == owner.character_id and session in row.sessions
                       for row in party_snapshot(self.service, entry)):
                raise RaidError('current published party host required for HP report')
            self.bridge.note_board_hp(entry.domain, session, maze, percentages)
        return [], 'party board HP received'

    def luke_machine_report(self, session, body):
        with self._lock:
            entry, _owner = self._entry(session)
            changed = self.bridge.note_luke_machine(session, body)
            return (self._project(entry, session) if changed else []), 'owned Luke machine report accepted' if changed else 'owned Luke machine report replay'

    def spend_luke_revive(self, session, binding, spend):
        """Compatibility entry for existing Luke mechanics callers."""
        ok, reason, remaining = self.spend_raid_revive(session, binding, spend)
        reason = {'raid_phase_coin_limit': 'luke_phase_coin_limit',
                  'raid_dungeon_coin_limit': 'luke_dungeon_coin_limit'}.get(reason, reason)
        return ok, reason, remaining

    def spend_raid_revive(self, session, binding, spend):
        """Charge the shared run/phase quota only after a successful wallet spend."""
        with self._lock:
            entry, owner = self._entry(session)
            raid = entry.domain
            with raid._lock:
                ticket = raid._ticket_owner(binding.ticket.run_id, owner.character_id, owner.account_id)
                raid.advance_clock(self.clock())
                battle = raid._active(self.clock())
                limits = getattr(self.runtime, 'revive_limits', None)
                rules = limits.get(entry.raid_type) if limits is not None else None
                if (ticket is not binding.ticket or rules is None
                        or ticket.run_id not in raid._accepted_runs or ticket.run_id in raid._retired_runs
                        or ticket.run_id in raid._cleared or ticket.attempt != raid.progression.epoch):
                    raise RaidError('accepted current raid revive owner and resource limits required')
                data = self._runs[raid.raid_key]
                phase_limit = rules.phase[battle.phase]
                run_limit = rules.dungeon[ticket.dungeon_id]
                if phase_limit and data.phase_revives.get(battle.phase, 0) >= phase_limit:
                    return False, 'raid_phase_coin_limit', 0
                if run_limit and data.run_revives.get(ticket.run_id, 0) >= run_limit:
                    return False, 'raid_dungeon_coin_limit', 0
                result = spend()
                if result[0]:
                    data.phase_revives[battle.phase] = data.phase_revives.get(battle.phase, 0) + 1
                    data.run_revives[ticket.run_id] = data.run_revives.get(ticket.run_id, 0) + 1
                return result

    def refund_unfinished(self, entry, character_id=None):
        battle = entry.domain.battle
        if battle is None or (battle.status == 'completed' and (entry.domain.single or battle.phase == 1)):
            return 0
        if character_id is None and battle.status != 'timed_out':
            raise RaidError('whole-raid refund requires actual terminal failure')
        from .ticket_refunds import queue
        members = entry.domain._admitted.values()
        reason = 'timed_out' if battle.status == 'timed_out' else 'abandoned'
        return sum(bool(queue(self.ledger, battle.instance_id, m.character_id,
            int(m.account_id), reason, self.clock())) for m in members
            if character_id is None or m.character_id == character_id)

    def refund_packets(self, session):
        cid, account, _record = self.service._selected_actor(session)
        if self.service.server.account_id != account:
            raise RaidError('ticket returns require their owning recipient account')
        now = self.clock()
        key = (cid, account)
        if now < self._refund_retry.get(key, 0):return []
        self._refund_retry[key] = now + 2
        from .ticket_refunds import deliver
        try:
            returned = deliver(self.ledger, cid, account, now)
        except LedgerError:
            self._refund_retry[key] = now + 10
            return []
        if not returned:return []
        commands = getattr(self.service.server, 'gm_item_commands', None)
        if commands is None:return []  # Next login still reloads persisted items.
        packets, _detail = commands._full_container_packets(self.ledger.repo, cid, 0)
        return list(packets)

    def tick(self, session):
        with self._lock:
            self.settlement_recovery.retry()
            entry, _owner = self._entry(session)
            if entry.domain.battle is None:
                return []
            entry.domain.advance_clock(self.clock())
            if entry.domain.battle.status == 'timed_out':
                self.refund_unfinished(entry)
            data = self._runs[entry.domain.raid_key]
            battle = entry.domain.battle
            rewards=self._auto_squad(entry) if battle.status=='completed' else []
            battle=entry.domain.battle
            if battle.phase == 0 and battle.status == 'completed' and battle.reward_finished_at and data.break_until <= self.clock():
                packets = self._next_phase(entry, entry.domain.leader_id, session)
                if packets is None:
                    return self._project(entry, session)
                session.raid_phase = entry.domain.battle.phase
            else:
                session.raid_phase = entry.domain.battle.phase
                packets=self._project(entry,session)
            if rewards:
                packets=rewards+list(packets)
                key,_owner=self._audience(entry,session)
                pending=self._pending.get(key)
                if pending is not None:self._pending[key]=(packets,*pending[1:])
            return packets

    def record_accepted_death(self, session, event_token):
        """Server-generated event capability after actual owned S0/0020 send."""
        with self._lock:
            entry, owner = self._entry(session)
            binding = self.bridge.current(session)
            if (binding is None or getattr(event_token, 'binding', None) is not binding
                    or binding.owner != owner
                    or getattr(event_token, 'run_generation', None) != binding.run_generation
                    or getattr(event_token, 'kind', None) != 'death'
                    or type(getattr(event_token, 'life_sequence', None)) is not int
                    or event_token.life_sequence < 0
                    or not getattr(session, 'dungeon_death_pending', False)
                    or int(getattr(session, 'dungeon_death_actor_id', 0)) != owner.character_id
                    or not getattr(session, 'in_dungeon_run', False)
                    or int(getattr(session, 'dungeon_run_generation', 0)) != binding.run_generation
                    or int(getattr(session, 'selected_dungeon_id', 0)) != binding.ticket.dungeon_id):
                raise RaidError('actual sent owned death transition capability required')
            if (entry.domain.battle.status != 'active' or entry.domain.battle.instance_id != binding.ticket.instance_id
                    or entry.domain.battle.phase != binding.ticket.phase or entry.domain.progression.epoch != binding.ticket.attempt):
                return False
            data = self._runs[entry.domain.raid_key]
            key = (binding.capability, event_token.life_sequence)
            if key in data.death_events:
                return False
            data.death_events[key] = event_token
            counter = (binding.ticket.phase, owner.character_id)
            data.phase_deaths[counter] = data.phase_deaths.get(counter, 0) + 1
            return True

    def phase_deaths(self, session):
        entry, owner = self._entry(session)
        return self._runs[entry.domain.raid_key].phase_deaths[(entry.domain.battle.phase, owner.character_id)]
