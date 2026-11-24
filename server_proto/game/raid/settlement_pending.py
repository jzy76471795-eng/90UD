"""Durable exact reward obligations; no client request or new reward roll."""
from dataclasses import asdict,replace
import json,hashlib,math
from .ledger import LedgerError
from .runtime import RewardEntitlement
from .rewards import FrozenRewardPlan,RewardChoice,RewardItem,_plan_fingerprint,_validate_reward_item

KINDS={1:'party_card',2:'pcroom_card',3:'squad_item'}

def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS raid_pending_rewards ('
        'instance_id TEXT NOT NULL,phase INTEGER NOT NULL,kind INTEGER NOT NULL,'
        'character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,'
        'payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,'
        'PRIMARY KEY(instance_id,phase,kind,character_id))')

def _payload(entitlement,plan):
    e=entitlement
    if (not isinstance(e,RewardEntitlement) or not isinstance(plan,FrozenRewardPlan)
            or e.phase not in (0,1) or e.kind not in KINDS or e.family not in ('anton','luke')
            or not e.instance_id or not math.isfinite(e.completed_at) or e.completed_at<=0 or e.owned_clears<e.minimum_clears
            or not 0<=e.card<=(19 if e.kind==3 else 3)
            or plan.choice.kind!=KINDS[e.kind] or not plan.items
            or e.fingerprint!=plan.fingerprint or _plan_fingerprint(plan.choice,plan.items)!=plan.fingerprint):
        raise LedgerError('complete frozen reward obligation required')
    if int(e.account_id)<=0 or str(int(e.account_id))!=e.account_id:
        raise LedgerError('frozen reward owner unavailable')
    for item in plan.items:_validate_reward_item(item)
    raw=json.dumps(dict(entitlement=asdict(e),plan=asdict(plan)),sort_keys=True,separators=(',',':'))
    return raw,hashlib.sha256(raw.encode('utf-8')).hexdigest()


def delivered(ledger,entitlement,plan,*,conn=None,allow_legacy_card=False):
    """Verify the complete committed receipt; never grant or relax C2S claims.

    Older completion fallbacks recorded display card0 before a selected card.
    Recovery may recognize that precise legacy receipt only when all other
    entitlement fields and the entire frozen plan match its persisted digest.
    """
    from .ledger import _digest
    _payload(entitlement,plan)
    e=entitlement;aid=int(e.account_id)
    if conn is None:
        ledger.ensure_schema()
        with ledger.repo.open() as owned:
            return delivered(ledger,e,plan,conn=owned,allow_legacy_card=allow_legacy_card)
    ledger._owner(conn,e.character_id,aid)
    admission=conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',(e.instance_id,)).fetchone()
    manifest=json.loads(admission[0]) if admission and admission[0] else None
    if not manifest or manifest[0]!=e.family or [e.character_id,aid] not in manifest[1]:
        raise LedgerError('receipt recovery owner was not admitted')
    from .daily_rewards import grant_state
    grant_state(ledger,conn,e,plan)
    if e.phase==1 and isinstance(manifest[5],dict) and manifest[5].get('character_clear_limits'):
        if not conn.execute('SELECT 1 FROM raid_character_clears WHERE instance_id=? AND character_id=? AND account_id=?',
                            (e.instance_id,e.character_id,aid)).fetchone():
            raise LedgerError('receipt recovery requires its personal final clear')
    row=conn.execute('SELECT account_id,plan_sha256 FROM raid_reward_receipts WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',
                     (e.instance_id,e.phase,e.kind,e.character_id)).fetchone()
    if row is None:return False
    if int(row[0])!=aid:raise LedgerError('committed reward receipt owner changed')
    if row[1]==_digest({'entitlement':asdict(e),'plan':plan.fingerprint}):return True
    if allow_legacy_card and e.kind in (1,2):
        for card in range(4):
            if row[1]==_digest({'entitlement':asdict(replace(e,card=card)),'plan':plan.fingerprint}):
                return True
    raise LedgerError('committed reward receipt differs from complete frozen obligation')

def stage(ledger,entitlement,plan):
    raw,digest=_payload(entitlement,plan);e=entitlement;aid=int(e.account_id)
    ledger.ensure_schema()
    with ledger.repo.inventory_transaction_scope() as conn:
        ledger._owner(conn,e.character_id,aid)
        row=conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',(e.instance_id,)).fetchone()
        manifest=json.loads(row[0]) if row and row[0] else None
        if not manifest or manifest[0]!=e.family or [e.character_id,aid] not in manifest[1]:
            raise LedgerError('deferred reward owner was not admitted')
        from .daily_rewards import grant_state
        grant_state(ledger,conn,e,plan)
        if e.phase==1 and isinstance(manifest[5],dict) and manifest[5].get('character_clear_limits'):
            if not conn.execute('SELECT 1 FROM raid_character_clears WHERE instance_id=? AND character_id=? AND account_id=?',
                                (e.instance_id,e.character_id,aid)).fetchone():
                raise LedgerError('deferred final reward requires its persisted clear')
        if delivered(ledger,e,plan,conn=conn,allow_legacy_card=True):
            return True  # Already committed; no new pending row is necessary.
        key=(e.instance_id,e.phase,e.kind,e.character_id)
        previous=conn.execute('SELECT account_id,payload_sha256 FROM raid_pending_rewards WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',key).fetchone()
        if previous:
            if tuple(previous)!=(aid,digest):raise LedgerError('deferred reward plan or owner changed')
            return True
        conn.execute('INSERT INTO raid_pending_rewards VALUES(?,?,?,?,?,?,?)',key+(aid,raw,digest))
        return False

def batch(ledger,cursor,limit=16):
    ledger.ensure_schema()
    with ledger.repo.open() as conn:
        return tuple(tuple(row) for row in conn.execute('SELECT rowid,instance_id,phase,kind,character_id,account_id,payload_json,payload_sha256 '
            'FROM raid_pending_rewards WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit)))

def decode(row):
    try:
        value=json.loads(row[6]);entitlement=RewardEntitlement(**value['entitlement']);raw=value['plan']
        items=[]
        for item in raw['items']:
            item=dict(item);item['metadata']=tuple(tuple(pair) for pair in item['metadata']);items.append(RewardItem(**item))
        plan=FrozenRewardPlan(RewardChoice(**raw['choice']),tuple(items),raw['fingerprint'])
        _,digest=_payload(entitlement,plan)
        if (digest!=row[7] or (entitlement.instance_id,entitlement.phase,entitlement.kind,
                entitlement.character_id,int(entitlement.account_id))!=tuple(row[1:6])):
            raise ValueError('pending reward identity changed')
        return entitlement,plan
    except (ValueError,TypeError,KeyError) as exc:
        raise LedgerError('invalid pending reward payload') from exc

def complete(ledger,row):
    with ledger.repo.inventory_transaction_scope() as conn:
        conn.execute('DELETE FROM raid_pending_rewards WHERE rowid=? AND payload_sha256=?',(row[0],row[7]))
