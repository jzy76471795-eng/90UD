"""Durable one-time return of the exact ticket debited for an unfinished raid."""
import json,math
from .ledger import LedgerError

POLICY='unfinished_ticket_refund_v2'
def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS raid_ticket_refunds ('
        'instance_id TEXT NOT NULL,character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,'
        'family TEXT NOT NULL,item_id INTEGER NOT NULL,item_count INTEGER NOT NULL,'
        'reason TEXT NOT NULL,created_at REAL NOT NULL,delivered_at REAL,'
        'PRIMARY KEY(instance_id,character_id))')
    conn.execute('CREATE INDEX IF NOT EXISTS raid_ticket_refund_owner ON raid_ticket_refunds(character_id,account_id,delivered_at)')

def queue(ledger,instance_id,character_id,account_id,reason,now):
    if reason not in ('abandoned','timed_out','server_restart') or not math.isfinite(now) or now<=0:
        raise LedgerError('actual terminal refund reason and server time required')
    ledger.ensure_schema()
    with ledger.repo.inventory_transaction_scope() as conn:
        ledger._owner(conn,character_id,account_id)
        row=conn.execute('SELECT family,manifest_json FROM raid_admission_receipts WHERE instance_id=?',(instance_id,)).fetchone()
        if row is None or not row[1]:return False
        manifest=json.loads(row[1])
        if (len(manifest)!=6 or not isinstance(manifest[5],dict)
                or manifest[5].get(POLICY) is not True):return False
        if manifest[0]!=row[0] or [character_id,account_id] not in manifest[1]:
            raise LedgerError('refund owner differs from the admitted character')
        if isinstance(manifest[5],dict) and manifest[5].get('character_clear_limits'):
            cleared=conn.execute('SELECT 1 FROM raid_character_clears WHERE instance_id=? AND character_id=? AND account_id=?',(instance_id,character_id,account_id)).fetchone()
        else:
            cleared=conn.execute('SELECT 1 FROM raid_clear_receipts WHERE instance_id=?',(instance_id,)).fetchone()
        if cleared:return False
        charges=[r for r in manifest[2] if r[0]==character_id]
        if len(charges)!=1 or any(type(v) is not int or v<=0 for v in charges[0]):
            raise LedgerError('refund requires one exact persisted debit')
        _,iid,count=charges[0]
        old=conn.execute('SELECT account_id,item_id,item_count FROM raid_ticket_refunds WHERE instance_id=? AND character_id=?',(instance_id,character_id)).fetchone()
        if old:
            if tuple(old)!=(account_id,iid,count):raise LedgerError('refund receipt identity changed')
            return False
        conn.execute('INSERT INTO raid_ticket_refunds VALUES(?,?,?,?,?,?,?,?,NULL)',
            (instance_id,character_id,account_id,row[0],iid,count,reason,now))
        return True

def deliver(ledger,character_id,account_id,now):
    if not math.isfinite(now) or now<=0:
        raise LedgerError('finite server time required for a ticket return')
    ledger.ensure_schema();delivered=[]
    with ledger.repo.inventory_transaction_scope() as conn:
        ledger._owner(conn,character_id,account_id)
        rows=conn.execute('SELECT instance_id,item_id,item_count FROM raid_ticket_refunds WHERE character_id=? AND account_id=? AND delivered_at IS NULL ORDER BY created_at,instance_id',
                          (character_id,account_id)).fetchall()
        for instance,iid,count in rows:
            # A failed capacity check rolls back the entire inventory change;
            # the queued return remains durable for a later owner-thread retry.
            ok,note,_=ledger.repo.grant_inventory_item(character_id,iid,count,require_full_capacity=True)
            if not ok:raise LedgerError('ticket return remains pending: '+str(note))
            conn.execute('UPDATE raid_ticket_refunds SET delivered_at=? WHERE instance_id=? AND character_id=? AND delivered_at IS NULL',(now,instance,character_id))
            delivered.append((iid,count))
    return tuple(delivered)

def recover(ledger,active_instances,now):
    """Only new-policy admissions are eligible; never refund legacy history."""
    ledger.ensure_schema();active=set(active_instances);queued=0
    with ledger.repo.open() as conn:
        rows=conn.execute('SELECT instance_id,manifest_json FROM raid_admission_receipts').fetchall()
    for instance,raw in rows:
        if instance in active or not raw:continue
        manifest=json.loads(raw)
        if len(manifest)!=6 or not isinstance(manifest[5],dict) or manifest[5].get(POLICY) is not True:continue
        for cid,aid in manifest[1]:
            queued+=bool(queue(ledger,instance,cid,aid,'server_restart',now))
    return queued
