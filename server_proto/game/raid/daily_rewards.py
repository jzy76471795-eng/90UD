"""One durable first-phase award per character/family/kind/server day.

Reservations keep the actual first frozen plan. A failed-raid retry can finish
the existing native reward flow with that same plan without granting it twice.
Resource factories run outside this module's inventory write transaction.
"""
from dataclasses import asdict
import json
from .ledger import LedgerError
from .ticket_refunds import POLICY
from .clear_quota import period_keys
from .rewards import FrozenRewardPlan,RewardChoice,RewardItem,_plan_fingerprint,_validate_reward_item

KINDS={1:'party_card',2:'pcroom_card',3:'squad_item'}

def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS raid_phase0_daily_rewards ('
        'character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,family TEXT NOT NULL,'
        'day_key TEXT NOT NULL,kind INTEGER NOT NULL,plan_json TEXT NOT NULL,plan_sha256 TEXT NOT NULL,'
        'plan_instance_id TEXT NOT NULL,granted_instance_id TEXT,granted_at REAL,'
        'PRIMARY KEY(character_id,family,day_key,kind))')

def _scope(ledger,conn,instance,family,cid,aid,completed_at,kind):
    if family not in ('anton','luke') or kind not in KINDS:
        raise LedgerError('owned first-phase reward kind required')
    ledger._owner(conn,cid,aid)
    row=conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',(instance,)).fetchone()
    manifest=json.loads(row[0]) if row and row[0] else None
    if (not isinstance(manifest,list) or len(manifest)!=6 or manifest[0]!=family
            or [cid,aid] not in manifest[1]):
        raise LedgerError('daily phase reward owner was not admitted')
    if not isinstance(manifest[5],dict) or manifest[5].get(POLICY) is not True:return None
    day,_week=period_keys(completed_at)
    return cid,family,day,kind

def _check_plan(plan,kind):
    if (not isinstance(plan,FrozenRewardPlan) or plan.choice.kind!=KINDS[kind]
            or not 1<=len(plan.items)<=255 or _plan_fingerprint(plan.choice,plan.items)!=plan.fingerprint):
        raise LedgerError('daily reward requires the actual frozen plan')
    for item in plan.items:_validate_reward_item(item)

def _decode(row,aid,kind):
    if int(row[0])!=aid:raise LedgerError('daily reward account owner changed')
    try:
        value=json.loads(row[1])
        if set(value)!={'choice','items','fingerprint'}:raise ValueError('plan fields')
        choice=RewardChoice(**value['choice'])
        items=[]
        for raw in value['items']:
            raw=dict(raw);raw['metadata']=tuple(tuple(pair) for pair in raw['metadata'])
            items.append(RewardItem(**raw))
        plan=FrozenRewardPlan(choice,tuple(items),value['fingerprint'])
        _check_plan(plan,kind)
        if plan.fingerprint!=row[2]:raise ValueError('plan fingerprint')
        return plan
    except (KeyError,TypeError,ValueError) as exc:
        raise LedgerError('persisted daily reward plan is invalid') from exc

def _row(conn,key):
    return conn.execute('SELECT account_id,plan_json,plan_sha256,granted_instance_id '
        'FROM raid_phase0_daily_rewards WHERE character_id=? AND family=? AND day_key=? AND kind=?',key).fetchone()

def freeze(ledger,instance,family,cid,aid,completed_at,kind,factory,*,durable_backend=True):
    ledger.ensure_schema()
    with ledger.repo.open() as conn:
        key=_scope(ledger,conn,instance,family,cid,aid,completed_at,kind)
        if key is not None:
            if durable_backend is not True:raise LedgerError('new daily award policy requires the durable inventory ledger')
            row=_row(conn,key)
            if row is not None:return _decode(row,aid,kind)
    plan=factory()
    if key is None:return plan  # Existing admissions keep their original rules.
    _check_plan(plan,kind)
    raw=json.dumps(asdict(plan),ensure_ascii=False,sort_keys=True,separators=(',',':'))
    with ledger.repo.inventory_transaction_scope() as conn:
        current=_scope(ledger,conn,instance,family,cid,aid,completed_at,kind)
        if current!=key:raise LedgerError('daily reward admission policy changed')
        row=_row(conn,key)
        if row is not None:return _decode(row,aid,kind)
        conn.execute('INSERT INTO raid_phase0_daily_rewards VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
            (cid,aid,family,key[2],kind,raw,plan.fingerprint,instance))
    return plan

def grant_state(ledger,conn,entitlement,plan):
    if entitlement.phase!=0:return None
    key=_scope(ledger,conn,entitlement.instance_id,entitlement.family,entitlement.character_id,
               int(entitlement.account_id),entitlement.completed_at,entitlement.kind)
    if key is None:return None
    row=_row(conn,key)
    if row is None:raise LedgerError('daily first-phase plan must be frozen before granting')
    actual=_decode(row,int(entitlement.account_id),entitlement.kind)
    if actual!=plan:raise LedgerError('daily reward differs from the first frozen plan')
    return key,row[3]

def mark_granted(conn,key,instance,completed_at):
    changed=conn.execute('UPDATE raid_phase0_daily_rewards SET granted_instance_id=?,granted_at=? '
        'WHERE character_id=? AND family=? AND day_key=? AND kind=? AND granted_instance_id IS NULL',
        (instance,completed_at,*key)).rowcount
    if changed!=1:raise LedgerError('daily reward grant reservation changed')

def repeat_notice(server):
    def dstring(raw):return len(raw).to_bytes(4,'little')+raw
    message='本阶段今日奖励已领取，本次不重复发放，可继续攻坚。'.encode('gbk')
    return server._codec_response(0,0x41,b'\0\0'+dstring('系统'.encode('gbk'))+b'\0'+dstring(message))
