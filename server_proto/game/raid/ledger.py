"""Durable raid receipts sharing the US inventory transaction owner.

The adapter supplies server-validated ticket/reward plans and period keys.
This module does not choose alternative tickets, reset times, or loot rolls.
"""
from pathlib import Path
from dataclasses import asdict
import hashlib
import json

from game.quest.adventure_season1 import record_raid_clear


class LedgerError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


def reward_fingerprint(items):
    if not items or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for pair in items for value in pair):
        raise LedgerError('server reward plan unavailable')
    return _digest(items)


class RaidLedger:
    def __init__(self, repository):
        self.repo = repository
        self._schema_ready = False

    def ensure_schema(self):
        if self._schema_ready:
            return
        path = Path(__file__).resolve().parents[2] / 'data' / 'resources' / 'migrations' / '20261013_raid.sql'
        statements = [line for line in path.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('--')]
        with self.repo.inventory_transaction_scope() as conn:
            # executescript commits an active SQLite transaction; preserve the
            # repository's transaction boundary by executing each statement.
            for statement in '\n'.join(statements).split(';'):
                if statement.strip():
                    conn.execute(statement)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(raid_admission_receipts)')}
            if 'manifest_json' not in columns:
                conn.execute("ALTER TABLE raid_admission_receipts ADD COLUMN manifest_json TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE TABLE IF NOT EXISTS raid_clear_receipts ('
                         'instance_id TEXT PRIMARY KEY, family TEXT NOT NULL, manifest_sha256 TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS raid_character_clears ('
                         'instance_id TEXT NOT NULL, character_id INTEGER NOT NULL, account_id INTEGER NOT NULL, '
                         'family TEXT NOT NULL, completed_at REAL NOT NULL, day_key TEXT NOT NULL, week_key TEXT NOT NULL, '
                         'PRIMARY KEY(instance_id,character_id))')
            conn.execute('CREATE INDEX IF NOT EXISTS raid_character_clear_period '
                         'ON raid_character_clears(character_id,family,week_key,day_key)')
            from .ticket_refunds import schema
            schema(conn)
            from .reward_checkpoints import schema as checkpoint_schema
            checkpoint_schema(conn)
            from .daily_rewards import schema as daily_schema
            daily_schema(conn)
            from .settlement_pending import schema as pending_schema
            pending_schema(conn)
        self._schema_ready = True

    def stage_pending_reward(self,entitlement,plan):
        from .settlement_pending import stage
        return stage(self,entitlement,plan)

    def pending_reward_batch(self,cursor):
        from .settlement_pending import batch
        return batch(self,cursor)

    def complete_pending_reward(self,row):
        from .settlement_pending import complete
        return complete(self,row)

    @staticmethod
    def _owner(conn, character_id, account_id):
        row = conn.execute('SELECT account_id, delete_flag FROM characters WHERE character_id=?', (character_id,)).fetchone()
        if row is None or int(row[0]) != int(account_id) or int(row[1]) != 0:
            raise LedgerError('character/account owner mismatch')

    def daily_count(self, account_id, family, period_key):
        self.ensure_schema()
        with self.repo.open() as conn:
            row = conn.execute('SELECT entry_count FROM raid_daily_entries WHERE account_id=? AND family=? AND period_key=?', (account_id, family, period_key)).fetchone()
        return int(row[0]) if row else 0

    def freeze_daily_phase_reward(self, instance, family, cid, aid, completed_at, kind, factory, *, durable_backend=True):
        from .daily_rewards import freeze
        return freeze(self,instance,family,cid,aid,completed_at,kind,factory,durable_backend=durable_backend)

    def daily_phase_reward_replay(self, entitlement, plan):
        from .daily_rewards import grant_state
        self.ensure_schema()
        with self.repo.open() as conn:state=grant_state(self,conn,entitlement,plan)
        return bool(state and state[1] is not None and state[1]!=entitlement.instance_id)

    def admit(self, instance_id, family, members, charges, period_key, daily_limit, *, context=None, clear_quota_at=None):
        if (not instance_id or family not in ('anton', 'luke') or not period_key
                or (daily_limit is None and clear_quota_at is None)
                or (daily_limit is not None and (type(daily_limit) is not int or daily_limit <= 0))):
            raise LedgerError('actual admission policy unavailable')
        if not members or len({cid for cid, aid in members}) != len(members):
            raise LedgerError('actual member owners unavailable')
        owners = dict(members)
        if not charges or set(cid for cid, iid, count in charges) != set(owners):
            raise LedgerError('ticket plan does not cover each member')
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for charge in charges for value in charge):
            raise LedgerError('invalid ticket plan')
        if any(iid > 0xffffffff or count > 0xffffffff for cid, iid, count in charges):
            raise LedgerError('ticket plan exceeds native integer range')
        manifest_value = (family, tuple(sorted(members)), tuple(sorted(charges)), period_key, daily_limit, context)
        manifest = _digest(manifest_value)
        self.ensure_schema()
        with self.repo.inventory_transaction_scope() as conn:
            for cid, aid in members:
                self._owner(conn, cid, aid)
            row = conn.execute('SELECT manifest_sha256 FROM raid_admission_receipts WHERE instance_id=?', (instance_id,)).fetchone()
            if row:
                if row[0] != manifest:
                    raise LedgerError('admission receipt identity changed')
                return False
            if clear_quota_at is not None:
                from .clear_quota import require_available
                for cid in owners:
                    require_available(conn, cid, family, clear_quota_at)
            for aid in sorted(set(owners.values())):
                row = conn.execute('SELECT entry_count FROM raid_daily_entries WHERE account_id=? AND family=? AND period_key=?', (aid, family, period_key)).fetchone()
                if daily_limit is not None and row and int(row[0]) >= daily_limit:
                    raise LedgerError('daily account entry limit reached')
            for cid, iid, count in charges:
                ok, note, detail = self.repo.consume_inventory_by_template(cid, iid, count)
                if not ok or int(detail.get('removed', 0)) != count:
                    raise LedgerError('ticket debit failed: ' + str(note))
            for aid in sorted(set(owners.values())):
                conn.execute('INSERT INTO raid_daily_entries(account_id,family,period_key,entry_count) VALUES(?,?,?,1) ON CONFLICT(account_id,family,period_key) DO UPDATE SET entry_count=entry_count+1', (aid, family, period_key))
            conn.execute('INSERT INTO raid_admission_receipts(instance_id,family,manifest_sha256,manifest_json) VALUES(?,?,?,?)', (instance_id, family, manifest, json.dumps(manifest_value, separators=(',', ':'))))
        return True

    def admit_from_alternatives(self, instance_id, family, members, alternatives, period_key, daily_limit, *, clear_quota_at=None):
        from .entry_cost import choose_entry_cost, EntryCostUnavailable
        self.ensure_schema()
        from .ticket_refunds import POLICY
        context = {'entry_cost_alternatives': alternatives, POLICY: True}
        if clear_quota_at is not None:
            from .clear_quota import DAILY_LIMIT, WEEKLY_LIMIT
            context['character_clear_limits'] = (DAILY_LIMIT, WEEKLY_LIMIT)
        with self.repo.inventory_transaction_scope() as conn:
            for cid, aid in members:
                self._owner(conn, cid, aid)
            replay = conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?', (instance_id,)).fetchone()
            if replay:
                if not replay[0]:
                    raise LedgerError('admission receipt has no verified manifest')
                manifest = json.loads(replay[0])
                expected_context = json.loads(json.dumps(context))
                if (manifest[0] != family or manifest[1] != [list(pair) for pair in sorted(members)]
                        or manifest[3:5] != [period_key, daily_limit] or len(manifest) != 6
                        or manifest[5] != expected_context):
                    raise LedgerError('admission receipt policy or member identity changed')
                return False
            charges = []
            for cid, aid in members:
                stock = self.repo.count_inventory_items_by_template(cid, item_ids=tuple(iid for iid, count in alternatives))
                try:
                    selection = choose_entry_cost(alternatives, stock)
                except EntryCostUnavailable as exc:
                    raise LedgerError('owned entry ticket unavailable') from exc
                if selection is None:
                    raise LedgerError('this normal raid requires an actual entry-cost configuration')
                iid, count = selection
                charges.append((cid, iid, count))
            return self.admit(instance_id, family, members, tuple(charges), period_key, daily_limit,
                              context=context, clear_quota_at=clear_quota_at)

    def clear_counts(self, character_id, account_id, family, timestamp):
        from .clear_quota import counts
        if family not in ('anton', 'luke'):
            raise LedgerError('unknown clear quota family')
        self.ensure_schema()
        with self.repo.open() as conn:
            self._owner(conn, character_id, account_id)
            return counts(conn, character_id, family, timestamp)

    def commit_clear(self, intent):
        from .clear_quota import period_keys, require_available, WEEKLY_LIMIT
        from .runtime import WeeklyIntent
        if (not isinstance(intent, WeeklyIntent) or not intent.instance_id or intent.family not in ('anton', 'luke')
                or intent.limit != WEEKLY_LIMIT or not intent.members):
            raise LedgerError('frozen final clear intent required')
        members = tuple(sorted((m.character_id, int(m.account_id)) for m in intent.members))
        if len({cid for cid, aid in members}) != len(members):
            raise LedgerError('duplicate clear recipient')
        day, week = period_keys(intent.completed_at)
        fingerprint = _digest((intent.family, members, intent.completed_at, day, week, intent.limit))
        self.ensure_schema()
        with self.repo.inventory_transaction_scope() as conn:
            for cid, aid in members:
                self._owner(conn, cid, aid)
            admission = conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',
                                     (intent.instance_id,)).fetchone()
            manifest = json.loads(admission[0]) if admission and admission[0] else None
            if manifest is None or manifest[0] != intent.family or any(list(m) not in manifest[1] for m in members):
                raise LedgerError('clear recipient or family was not admitted')
            old = conn.execute('SELECT manifest_sha256 FROM raid_clear_receipts WHERE instance_id=?',
                               (intent.instance_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise LedgerError('clear receipt identity changed')
                return False
            if any(conn.execute('SELECT 1 FROM raid_ticket_refunds WHERE instance_id=? AND character_id=?',
                    (intent.instance_id,cid)).fetchone() for cid,aid in members):
                raise LedgerError('refunded character cannot commit a later successful clear')
            for cid, aid in members:
                require_available(conn, cid, intent.family, intent.completed_at)
            for cid, aid in members:
                conn.execute('INSERT INTO raid_character_clears VALUES(?,?,?,?,?,?,?)',
                             (intent.instance_id, cid, aid, intent.family, intent.completed_at, day, week))
            conn.execute('INSERT INTO raid_clear_receipts VALUES(?,?,?)',
                         (intent.instance_id, intent.family, fingerprint))
            for aid in sorted({aid for _, aid in members}):
                record_raid_clear(conn, aid, intent.family, intent.instance_id, intent.completed_at)
        return True

    def grant_reward(self, instance_id, phase, kind, character_id, account_id, items, fingerprint):
        if not instance_id or phase not in (0, 1) or kind not in (1, 2, 3) or fingerprint != reward_fingerprint(items):
            raise LedgerError('frozen server reward plan mismatch')
        self.ensure_schema()
        with self.repo.inventory_transaction_scope() as conn:
            self._owner(conn, character_id, account_id)
            admission = conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?', (instance_id,)).fetchone()
            if admission is None or not admission[0]:
                raise LedgerError('reward has no verified admitted instance')
            manifest = json.loads(admission[0])
            if [character_id, account_id] not in manifest[1]:
                raise LedgerError('reward owner was not admitted to this instance')
            from .ticket_refunds import POLICY
            if phase==0 and isinstance(manifest[5],dict) and manifest[5].get(POLICY) is True:
                raise LedgerError('new daily first-phase rewards require a frozen entitlement')
            row = conn.execute('SELECT account_id,plan_sha256 FROM raid_reward_receipts WHERE instance_id=? AND phase=? AND kind=? AND character_id=?', (instance_id, phase, kind, character_id)).fetchone()
            if row:
                if int(row[0]) != int(account_id) or row[1] != fingerprint:
                    raise LedgerError('reward receipt identity changed')
                return False
            for item_id, count in items:
                ok, note, slot = self.repo.grant_inventory_item(character_id, item_id, count, require_full_capacity=True)
                if not ok:
                    raise LedgerError('reward grant failed: ' + str(note))
            conn.execute('INSERT INTO raid_reward_receipts(instance_id,phase,kind,character_id,account_id,plan_sha256) VALUES(?,?,?,?,?,?)', (instance_id, phase, kind, character_id, account_id, fingerprint))
        return True

    def grant_entitlement(self, entitlement, plan, resolver):
        from .runtime import RewardEntitlement
        from .rewards import FrozenRewardPlan, RewardError, _plan_fingerprint
        kinds = {1: 'party_card', 2: 'pcroom_card', 3: 'squad_item'}
        if (not isinstance(entitlement, RewardEntitlement) or not isinstance(plan, FrozenRewardPlan)
                or entitlement.phase not in (0, 1) or entitlement.kind not in kinds
                or plan.choice.kind != kinds[entitlement.kind]
                or not 0 <= entitlement.card <= (19 if entitlement.kind == 3 else 3)
                or entitlement.family not in ('anton', 'luke') or not entitlement.instance_id
                or entitlement.completed_at <= 0 or entitlement.owned_clears < entitlement.minimum_clears
                or not plan.items or plan.fingerprint != entitlement.fingerprint
                or _plan_fingerprint(plan.choice, plan.items) != plan.fingerprint):
            raise LedgerError('server-frozen complete reward entitlement changed')
        try:
            aid = int(entitlement.account_id)
            if aid <= 0 or str(aid) != entitlement.account_id:
                raise ValueError('owner')
            replay = self.daily_phase_reward_replay(entitlement,plan)
            metadata = () if replay else resolver.metadata_for_plan(plan)
        except (RewardError, ValueError, TypeError, AttributeError) as exc:
            raise LedgerError('verified frozen reward metadata unavailable') from exc
        fingerprint = _digest({'entitlement': asdict(entitlement), 'plan': plan.fingerprint})
        cid = entitlement.character_id
        self.ensure_schema()
        with self.repo.inventory_transaction_scope() as conn:
            self._owner(conn, cid, aid)
            admission = conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',
                                     (entitlement.instance_id,)).fetchone()
            if admission is None or not admission[0]:
                raise LedgerError('reward has no verified admitted instance')
            manifest = json.loads(admission[0])
            if manifest[0] != entitlement.family or [cid, aid] not in manifest[1]:
                raise LedgerError('reward owner or family was not admitted')
            key = (entitlement.instance_id, entitlement.phase, entitlement.kind, cid)
            row = conn.execute('SELECT account_id,plan_sha256 FROM raid_reward_receipts WHERE instance_id=? AND phase=? AND kind=? AND character_id=?', key).fetchone()
            if row:
                if int(row[0]) != aid or row[1] != fingerprint:
                    raise LedgerError('reward receipt card, metadata or owner changed')
                from .reward_checkpoints import complete_claim
                complete_claim(conn,entitlement,plan)
                return False
            from .daily_rewards import grant_state,mark_granted
            daily=grant_state(self,conn,entitlement,plan)
            newly_granted=daily is None or daily[1] is None
            if newly_granted:
                if len(metadata)!=len(plan.items):raise LedgerError('daily reward grant state changed')
                for item, meta in zip(plan.items, metadata):
                    ok, note, slot = self.repo.grant_inventory_item(cid, item.item_id, item.quantity,
                                                                  metadata=meta, require_full_capacity=True)
                    if not ok:
                        raise LedgerError('reward grant failed: ' + str(note))
                if daily is not None:mark_granted(conn,daily[0],entitlement.instance_id,entitlement.completed_at)
            conn.execute('INSERT INTO raid_reward_receipts(instance_id,phase,kind,character_id,account_id,plan_sha256) VALUES(?,?,?,?,?,?)',
                         key + (aid, fingerprint))
            from .reward_checkpoints import complete_claim
            complete_claim(conn,entitlement,plan)
        return newly_granted
