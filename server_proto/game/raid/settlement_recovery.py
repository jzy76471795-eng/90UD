"""Close departed recipients using durable rewards, then deliver independently."""
from time import monotonic
from dataclasses import replace
from . import settlement_pending

class SettlementRecovery:
    def __init__(self,adapter):
        self.adapter=adapter
        self.jobs={}
        self.next_retry=0
        self.cursor=0
        self.checkpoint_cursor=0
        self.failed_until={}

    def _log(self,exc):
        log=getattr(self.adapter.service.server,'log',None)
        if callable(log):
            try:log('[Raid] earned settlement remains pending: %s'%exc)
            except Exception:pass

    def departed(self,entry,character_id):
        battle=entry.domain.battle
        if (battle is None or battle.status!='completed'
                or (battle.phase,character_id) not in entry.domain._recipients):return
        data=self.adapter._runs[entry.domain.raid_key]
        self.jobs[(battle.instance_id,battle.phase)]=(entry,data)
        try:self._stage(entry,data)
        except Exception as exc:self._log(exc)

    def _stage(self,entry,data):
        raid=entry.domain;battle=raid.battle
        key=(battle.instance_id,battle.phase)
        recipients=tuple(m for (phase,cid),m in sorted(raid._recipients.items()) if phase==battle.phase)
        members={m.character_id for m in raid.members}
        pending=False
        for card,member in enumerate(recipients):
            if member.character_id in members:continue
            for kind in data.configuration.reward_kinds[battle.phase]:
                slot=(battle.phase,member.character_id,kind)
                if slot in raid._committed_rewards or slot in raid._deferred_rewards:continue
                try:
                    cache=data.reward_plans.get((battle.instance_id,battle.phase,member.character_id,kind))
                    selected=(cache[0] if cache else self.adapter._available_reward_card(
                        entry, member, kind, card if kind==3 else 0))
                    plan,entitlement=self.adapter._freeze_claim(entry,member,kind,selected,data=data)
                    if self._has_receipt(entitlement,plan):
                        self._sync_delivered(entitlement,plan)
                    else:
                        raid.defer_reward(entitlement,lambda e: self.adapter.ledger.stage_pending_reward(e,plan))
                except Exception as exc:
                    pending=True;self._log(exc)
        if not pending:self.jobs.pop(key,None)
        self.adapter._finish_phase_rewards(entry)

    def _has_receipt(self,entitlement,plan):
        return settlement_pending.delivered(self.adapter.ledger,entitlement,plan,allow_legacy_card=True)

    def _sync_delivered(self,entitlement,plan):
        """Close only this live instance/phase after verifying its real receipt."""
        e=entitlement
        if not self._has_receipt(e,plan):
            from .ledger import LedgerError
            raise LedgerError('committed recovery receipt required before live state synchronization')
        candidates=[]
        job=self.jobs.get((e.instance_id,e.phase))
        if job is not None:candidates.append(job)
        for entry in tuple(self.adapter.service.raids.values()):
            data=self.adapter._runs.get(entry.domain.raid_key)
            if data is not None and not any(entry is value[0] for value in candidates):
                candidates.append((entry,data))
        for entry,data in candidates:
            raid=entry.domain;battle=raid.battle
            if (battle is None or battle.instance_id!=e.instance_id or battle.phase!=e.phase
                    or battle.status!='completed' or raid.family!=e.family
                    or self.adapter._runs.get(raid.raid_key) is not data):
                continue
            member=raid._recipients.get((e.phase,e.character_id))
            if member is None or member.account_id!=e.account_id:
                continue
            slot=(e.phase,e.character_id,e.kind)
            frozen=raid._entitlements.get(slot)
            if frozen is None:
                card=self.adapter._available_reward_card(entry,member,e.kind,e.card)
                frozen=raid.freeze_reward(e.character_id,e.account_id,e.kind,card,plan.fingerprint)
            if (replace(frozen,card=e.card)!=e
                    or e.kind==3 and frozen.card!=e.card):
                from .ledger import LedgerError
                raise LedgerError('committed recovery differs from this live reward entitlement')
            cache_key=(e.instance_id,e.phase,e.character_id,e.kind)
            cached=data.reward_plans.get(cache_key)
            if cached is not None and (cached[1]!=plan or cached[2]!=frozen):
                from .ledger import LedgerError
                raise LedgerError('committed recovery differs from the live frozen reward plan')
            data.reward_plans.setdefault(cache_key,(frozen.card,plan,frozen))
            def receipt_only(owned):
                if not self._has_receipt(owned,plan):
                    from .ledger import LedgerError
                    raise LedgerError('committed reward receipt required before live phase completion')
                return True
            raid.grant_reward(frozen,receipt_only)
            departed={m.character_id for (phase,cid),m in raid._recipients.items()
                      if phase==e.phase and all(m.character_id!=live.character_id for live in raid.members)}
            required={(e.phase,cid,kind) for cid in departed
                      for kind in data.configuration.reward_kinds[e.phase]}
            if required<=raid._committed_rewards | raid._deferred_rewards:
                self.jobs.pop((e.instance_id,e.phase),None)
            self.adapter._finish_phase_rewards(entry)
            return

    def retry(self,*,force=False):
        with self.adapter._lock:
            now=monotonic()
            if not force and now<self.next_retry:return
            self.next_retry=now+2
            for key,(entry,data) in tuple(self.jobs.items())[:8]:
                battle=entry.domain.battle
                if (battle is None or key!=(battle.instance_id,battle.phase)
                        or battle.status!='completed'
                        or self.adapter._runs.get(entry.domain.raid_key) is not data):
                    self.jobs.pop(key,None)
                    continue
                try:self._stage(entry,data)
                except Exception as exc:self._log(exc)
                if key in self.jobs:
                    self.jobs.pop(key);self.jobs[key]=(entry,data)
            try:
                rows=self.adapter.ledger.pending_reward_batch(self.cursor)
                if not rows and self.cursor:
                    self.cursor=0;rows=self.adapter.ledger.pending_reward_batch(0)
            except Exception as exc:self._log(exc);return
            for row in rows:
                self.cursor=row[0]
                if now<self.failed_until.get(row[0],0):continue
                try:
                    entitlement,plan=settlement_pending.decode(row)
                    if not self._has_receipt(entitlement,plan):
                        self.adapter.ledger.grant_entitlement(entitlement,plan,self.adapter.runtime.reward_resolver)
                    self._sync_delivered(entitlement,plan)
                    self.adapter.service.queue_inventory_refresh(entitlement.character_id,entitlement.account_id,
                        (item.list_type for item in plan.items))
                    self.adapter.ledger.complete_pending_reward(row)
                    self.failed_until.pop(row[0],None)
                except Exception as exc:
                    self.failed_until[row[0]]=now+10;self._log(exc)

            # Completion checkpoints precede departure and share its final-clear
            # transaction. They remain available if staging the pending table fails.
            from . import reward_checkpoints
            try:
                backups=reward_checkpoints.batch(self.adapter.ledger,self.checkpoint_cursor)
                if not backups and self.checkpoint_cursor:
                    self.checkpoint_cursor=0;backups=reward_checkpoints.batch(self.adapter.ledger,0)
            except Exception as exc:self._log(exc);return
            for row in backups:
                self.checkpoint_cursor=row[0]
                try:
                    entitlement,plan=settlement_pending.decode(row)
                    entry=self.adapter.service.current(entitlement.character_id)
                    battle=entry.domain.battle if entry is not None else None
                    if (battle is not None and battle.instance_id==entitlement.instance_id
                            and battle.phase==entitlement.phase):
                        continue  # Preserve the current member's native card selection.
                    if not self._has_receipt(entitlement,plan):
                        self.adapter.ledger.grant_entitlement(entitlement,plan,self.adapter.runtime.reward_resolver)
                    self._sync_delivered(entitlement,plan)
                    self.adapter.service.queue_inventory_refresh(entitlement.character_id,entitlement.account_id,
                        (item.list_type for item in plan.items))
                except Exception as exc:self._log(exc)
