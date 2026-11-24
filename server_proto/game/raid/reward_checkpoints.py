"""Completion-time reward recovery, atomic with the personal final clear.

These rows freeze outcomes, not the player's display-card choice. Active members
retain their native selection. Normal grants delete the backup in the same
inventory transaction; only departed/restarted recipients use fallback delivery.
"""
import json
from dataclasses import replace
from .ledger import LedgerError
from .settlement_pending import _payload


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS raid_reward_checkpoints ('
        'instance_id TEXT NOT NULL,phase INTEGER NOT NULL,kind INTEGER NOT NULL,'
        'character_id INTEGER NOT NULL,account_id INTEGER NOT NULL,'
        'payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,'
        'PRIMARY KEY(instance_id,phase,kind,character_id))')


def stage(ledger,conn,entitlement,plan):
    raw,digest=_payload(entitlement,plan)
    e=entitlement;aid=int(e.account_id)
    ledger._owner(conn,e.character_id,aid)
    admission=conn.execute('SELECT manifest_json FROM raid_admission_receipts WHERE instance_id=?',(e.instance_id,)).fetchone()
    manifest=json.loads(admission[0]) if admission and admission[0] else None
    if not manifest or manifest[0]!=e.family or [e.character_id,aid] not in manifest[1]:
        raise LedgerError('checkpoint requires its admitted reward owner')
    key=(e.instance_id,e.phase,e.kind,e.character_id)
    old=conn.execute('SELECT account_id,payload_sha256 FROM raid_reward_checkpoints WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',key).fetchone()
    if old:
        if tuple(old)!=(aid,digest):raise LedgerError('completion checkpoint outcome or owner changed')
        return
    conn.execute('INSERT INTO raid_reward_checkpoints VALUES(?,?,?,?,?,?,?)',key+(aid,raw,digest))


def batch(ledger,cursor,limit=16):
    ledger.ensure_schema()
    with ledger.repo.open() as conn:
        return tuple(tuple(row) for row in conn.execute(
            'SELECT b.rowid,b.instance_id,b.phase,b.kind,b.character_id,b.account_id,b.payload_json,b.payload_sha256 '
            'FROM raid_reward_checkpoints b WHERE b.rowid>? AND NOT EXISTS('
            'SELECT 1 FROM raid_pending_rewards p WHERE p.instance_id=b.instance_id AND p.phase=b.phase AND p.kind=b.kind AND p.character_id=b.character_id) '
            'ORDER BY b.rowid LIMIT ?',(cursor,limit)))


def complete_claim(conn,entitlement,plan):
    e=entitlement;key=(e.instance_id,e.phase,e.kind,e.character_id)
    row=conn.execute('SELECT account_id,payload_json FROM raid_reward_checkpoints WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',key).fetchone()
    if row is None:return
    payload=json.loads(row[1])
    if int(row[0])!=int(e.account_id) or payload['plan']['fingerprint']!=plan.fingerprint:
        raise LedgerError('claimed reward differs from completion checkpoint')
    conn.execute('DELETE FROM raid_reward_checkpoints WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',key)


def bind_choice(ledger,entitlement,plan):
    """Persist the first owned display choice without rerolling its outcome."""
    from .settlement_pending import decode
    raw,digest=_payload(entitlement,plan)
    e=entitlement
    ledger.ensure_schema()
    with ledger.repo.inventory_transaction_scope() as conn:
        ledger._owner(conn,e.character_id,int(e.account_id))
        key=(e.instance_id,e.phase,e.kind,e.character_id)
        row=conn.execute('SELECT rowid,instance_id,phase,kind,character_id,account_id,payload_json,payload_sha256 '
                         'FROM raid_reward_checkpoints WHERE instance_id=? AND phase=? AND kind=? AND character_id=?',key).fetchone()
        if row is None:return  # A committed grant may already have removed it.
        old,old_plan=decode(tuple(row))
        if (old_plan!=plan or replace(old,card=e.card)!=e
                or old.card!=e.card and (e.kind==3 or old.card!=0)):
            raise LedgerError('selected card differs from its owned completion checkpoint')
        if row[7]==digest:return
        conn.execute('UPDATE raid_reward_checkpoints SET payload_json=?,payload_sha256=? WHERE rowid=? AND payload_sha256=?',
                     (raw,digest,row[0],row[7]))
