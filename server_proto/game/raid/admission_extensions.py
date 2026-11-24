"""Atomic phase-break admission using the existing ticket/clear receipt owner."""
import json
from .ledger import LedgerError, _digest
from .entry_cost import choose_entry_cost
from .ticket_refunds import POLICY


def admit(ledger, instance, family, member, alternatives, now, *, clear_quota):
    ledger.ensure_schema()
    cid, aid = member.character_id, int(member.account_id)
    with ledger.repo.inventory_transaction_scope() as conn:
        ledger._owner(conn, cid, aid)
        row = conn.execute('SELECT manifest_sha256,manifest_json FROM raid_admission_receipts '
                           'WHERE instance_id=? AND family=?', (instance, family)).fetchone()
        if row is None or not row[1]:
            raise LedgerError('phase-break admission has no original ticket receipt')
        manifest = json.loads(row[1])
        if (len(manifest) != 6 or _digest(manifest) != row[0] or manifest[0] != family
                or not isinstance(manifest[5], dict) or manifest[5].get(POLICY) is not True
                or manifest[5].get('entry_cost_alternatives') != [list(x) for x in alternatives]):
            raise LedgerError('phase-break admission policy or receipt changed')
        old = next((x for x in manifest[1] if x[0] == cid), None)
        if old is not None and old != [cid, aid]:
            raise LedgerError('readmission character/account changed')
        refund = conn.execute('SELECT * FROM raid_ticket_refunds WHERE instance_id=? AND character_id=?',
                              (instance, cid)).fetchone()
        if old is not None and refund is None:
            return False  # This instance already owns the unreimbursed debit.
        if conn.execute('SELECT 1 FROM raid_clear_receipts WHERE instance_id=?', (instance,)).fetchone():
            raise LedgerError('completed raid cannot extend admission')
        if clear_quota:
            from .clear_quota import require_available
            require_available(conn, cid, family, now)
        period, limit = manifest[3:5]
        count = conn.execute('SELECT entry_count FROM raid_daily_entries WHERE account_id=? AND family=? AND period_key=?',
                             (aid, family, period)).fetchone()
        # One debit/entry per account in this instance, even after a refunded rejoin.
        new_account = not any(x[1] == aid for x in manifest[1])
        if new_account and limit is not None and count and int(count[0]) >= limit:
            raise LedgerError('daily account entry limit reached')
        if refund is not None and (refund[2] != aid or refund[3] != family):
            raise LedgerError('readmission refund owner changed')
        charges = [x for x in manifest[2] if x[0] == cid]
        if refund is not None and (len(charges) != 1 or charges[0][1:] != list(refund[4:6])):
            raise LedgerError('readmission refund differs from its original debit')
        # A queued, not yet delivered refund can be cancelled without touching
        # inventory. A delivered refund requires a new atomic ticket debit.
        if refund is None or refund[8] is not None:
            stock = ledger.repo.count_inventory_items_by_template(cid, item_ids=tuple(i for i, n in alternatives))
            selected = choose_entry_cost(alternatives, stock)
            if selected is None:
                raise LedgerError('phase-break admission requires its actual ticket')
            iid, amount = selected
            ok, note, detail = ledger.repo.consume_inventory_by_template(cid, iid, amount)
            if not ok or int(detail.get('removed', 0)) != amount:
                raise LedgerError('phase-break ticket debit failed: ' + str(note))
            manifest[2] = sorted([x for x in manifest[2] if x[0] != cid] + [[cid, iid, amount]])
        if old is None:
            manifest[1] = sorted(manifest[1] + [[cid, aid]])
        if new_account:
            conn.execute('INSERT INTO raid_daily_entries VALUES(?,?,?,1) ON CONFLICT(account_id,family,period_key) '
                         'DO UPDATE SET entry_count=entry_count+1', (aid, family, period))
        # Preserve every replaced refund in an append-only admission journal;
        # active refund consumers must not issue a second refund for this join.
        conn.execute('CREATE TABLE IF NOT EXISTS raid_admission_extensions ('
                     'id INTEGER PRIMARY KEY,instance_id TEXT NOT NULL,character_id INTEGER NOT NULL,'
                     'account_id INTEGER NOT NULL,before_sha256 TEXT NOT NULL,after_sha256 TEXT NOT NULL,'
                     'refund_json TEXT,created_at REAL NOT NULL)')
        fingerprint = _digest(manifest)
        conn.execute('INSERT INTO raid_admission_extensions(instance_id,character_id,account_id,before_sha256,'
                     'after_sha256,refund_json,created_at) VALUES(?,?,?,?,?,?,?)',
                     (instance,cid,aid,row[0],fingerprint,json.dumps(list(refund)) if refund else None,now))
        if refund is not None:
            conn.execute('DELETE FROM raid_ticket_refunds WHERE instance_id=? AND character_id=?', (instance,cid))
        conn.execute('UPDATE raid_admission_receipts SET manifest_sha256=?,manifest_json=? WHERE instance_id=?',
                     (fingerprint,json.dumps(manifest,separators=(',',':')),instance))
        return True
