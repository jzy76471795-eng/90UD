"""Character-owned friends and atomic two-character trade settlement.

Offers are immutable snapshots, not deductions. Every asset is revalidated
inside one SQLite write transaction after both participants' final consent.
"""
import json
import sqlite3
from contextlib import contextmanager


class ExchangeError(ValueError):
    pass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS character_player_friends (
 character_id INTEGER NOT NULL REFERENCES characters(character_id) ON DELETE CASCADE,
 friend_character_id INTEGER NOT NULL REFERENCES characters(character_id) ON DELETE CASCADE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(character_id,friend_character_id),CHECK(character_id<>friend_character_id));
CREATE TABLE IF NOT EXISTS character_player_trade_receipts (
 trade_id TEXT PRIMARY KEY,first_character_id INTEGER NOT NULL,
 second_character_id INTEGER NOT NULL,result_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
'''


class ExchangeStore:
    def __init__(self, path, *, allowed=None, slot_range=None):
        self.path = str(path)
        self.allowed = allowed or self._allowed
        self.slot_range = slot_range or self._slot_range
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def owned(db, account, character):
        row = db.execute('SELECT * FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0',
                         (character, account)).fetchone()
        if row is None:
            raise ExchangeError('selected character is not owned by this account')
        return dict(row)

    def wallet(self, account, character):
        with self.connect() as db:
            self.owned(db, account, character)
            row = db.execute('SELECT gold FROM character_wallets WHERE character_id=?', (character,)).fetchone()
            if row is None:
                raise ExchangeError('missing character wallet')
            return int(row[0])

    def character(self, account, character):
        with self.connect() as db:
            return self.owned(db, account, character)

    def find_character(self, name):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM characters WHERE name=? AND delete_flag=0', (name,)).fetchall()
            if len(rows) != 1:
                raise ExchangeError('character name is absent or ambiguous')
            return dict(rows[0])

    def friends(self, account, character):
        with self.connect() as db:
            self.owned(db, account, character)
            return [dict(r) for r in db.execute('''SELECT c.* FROM character_player_friends f
                JOIN characters c ON c.character_id=f.friend_character_id
                WHERE f.character_id=? AND c.delete_flag=0 ORDER BY c.character_id''', (character,))]

    def add_friend(self, account, character, target):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.owned(db, account, character)
            if target == character or db.execute('SELECT 1 FROM characters WHERE character_id=? AND delete_flag=0', (target,)).fetchone() is None:
                raise ExchangeError('invalid friend target')
            if db.execute('SELECT 1 FROM character_player_friends WHERE character_id=? AND friend_character_id=?', (character,target)).fetchone():
                return
            if db.execute('SELECT count(*) FROM character_player_friends WHERE character_id=?', (character,)).fetchone()[0] >= 64:
                raise ExchangeError('friend list full')
            db.execute('INSERT INTO character_player_friends(character_id,friend_character_id) VALUES(?,?)', (character,target))

    def remove_friend(self, account, character, name):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self.owned(db,account,character)
            db.execute('''DELETE FROM character_player_friends WHERE character_id=? AND friend_character_id IN
                (SELECT character_id FROM characters WHERE name=?)''', (character,name))

    @staticmethod
    def _allowed(row):
        from game.character.item_metadata import resolve_item_metadata
        meta = resolve_item_metadata(int(row['item_template_id']))
        # Limited-use ownership rules require a distinct transfer counter.
        return (meta.normalized_attach_type() != 'trade limit'
                and meta.is_tradable_instance(int(row.get('seal_flag') or 0)))

    @staticmethod
    def _slot_range(row):
        from game.character.item_metadata import resolve_item_metadata
        if int(row['list_type']) != 0:
            raise ExchangeError('unsupported persistent trade container')
        return resolve_item_metadata(int(row['item_template_id'])).slot_range()

    def validate_item(self, row, count):
        extra=json.loads(row.get('extra_json') or '{}')
        # These records are character-local; never transfer dangling set IDs.
        if not isinstance(extra,dict) or extra.get('customability'):
            raise ExchangeError('customability equipment needs a set-record transfer')
        if (row['owner_scope'] != 'character' or row['owner_id'] != row['character_id']
                or row['list_type'] != 0 or row['item_template_id'] <= 0
                or row['item_kind'] not in ('equipment','stackable')
                or type(count) is not int or not 0 < count <= row['stack_count']
                or row.get('equipment_lock_id') or row.get('sort_lock_flag')
                or row.get('pet_serial_or_handle')
                or row.get('expire_time') or not self.allowed(row)):
            raise ExchangeError('item is not transferable in its current state')
        if row['item_kind'] == 'equipment' and (count != 1 or row['stack_count'] != 1):
            raise ExchangeError('invalid equipment quantity')

    def item(self, account, character, slot, count):
        with self.connect() as db:
            self.owned(db,account,character)
            row = db.execute("SELECT * FROM character_items WHERE owner_scope='character' AND owner_id=? AND character_id=? AND list_type=0 AND slot_index=?", (character,character,slot)).fetchone()
            if row is None:
                raise ExchangeError('inventory slot is empty')
            row = dict(row);self.validate_item(row,count)
            return row

    def commit(self, trade_id, owners, offers, gold):
        cids = [cid for _,cid in owners]
        if len(set(cids)) != 2 or len(owners) != 2:
            raise ExchangeError('trade needs two different owned characters')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM character_player_trade_receipts WHERE trade_id=?',(trade_id,)).fetchone():
                raise ExchangeError('trade already settled')
            wallets = {}
            for account,cid in owners:
                self.owned(db,account,cid)
                row = db.execute('SELECT gold FROM character_wallets WHERE character_id=?',(cid,)).fetchone()
                amount = gold.get(cid,0)
                if row is None or type(amount) is not int or not 0 <= amount <= int(row[0]):
                    raise ExchangeError('invalid or insufficient wallet gold')
                wallets[cid] = int(row[0])
            seen = set();moves = []
            for cid in cids:
                slots = set()
                if len(offers.get(cid,())) > 9:
                    raise ExchangeError('trade has too many items')
                for offer in offers.get(cid,()):
                    prior,count,slot = offer['row'],offer['count'],offer['slot']
                    if prior['item_uid'] in seen or slot in slots or not 3 <= slot <= 11:
                        raise ExchangeError('duplicate item or offer slot')
                    seen.add(prior['item_uid']);slots.add(slot)
                    current = db.execute('SELECT * FROM character_items WHERE item_uid=?',(prior['item_uid'],)).fetchone()
                    if current is None or dict(current) != prior or prior['character_id'] != cid:
                        raise ExchangeError('offered item changed before settlement')
                    self.validate_item(prior,count)
                    moves.append((cid,offer))
            next_uid = int(db.execute('SELECT COALESCE(MAX(item_uid),0)+1 FROM character_items').fetchone()[0])
            # Free full outgoing slots within this same transaction before
            # allocating incoming slots. Rollback restores every original row.
            for cid,offer in moves:
                row,count = offer['row'],offer['count']
                if count == row['stack_count']:
                    # Preserve the item row and any FK-linked instance data.
                    reserve=-int(row['item_uid'])-1
                    db.execute('UPDATE character_items SET slot_index=? WHERE item_uid=?',(reserve,row['item_uid']))
                else:
                    db.execute('UPDATE character_items SET stack_count=stack_count-?,instance_value=stack_count-? WHERE item_uid=?',(count,count,row['item_uid']))
            transfers = {cid:[] for cid in cids}
            for cid,offer in moves:
                target = cids[1] if cid == cids[0] else cids[0]
                row,count = dict(offer['row']),offer['count']
                low,high = self.slot_range(row)
                occupied = {int(r[0]) for r in db.execute("SELECT slot_index FROM character_items WHERE owner_scope='character' AND owner_id=? AND list_type=0",(target,))}
                dest = next((n for n in range(low,high+1) if n not in occupied),None)
                if dest is None:
                    raise ExchangeError('recipient inventory is full')
                full=count == row['stack_count']
                if not full:
                    row['item_uid'] = next_uid
                    next_uid += 1
                    # Preserve every other column, including instance metadata.
                row.update(owner_id=target,character_id=target,slot_index=dest,stack_count=count)
                if row['item_kind']=='stackable':row['instance_value']=count
                if full:
                    db.execute('UPDATE character_items SET owner_id=?,character_id=?,slot_index=? WHERE item_uid=?',
                               (target,target,dest,row['item_uid']))
                else:
                    columns=list(row)
                    db.execute('INSERT INTO character_items ('+','.join('"'+c+'"' for c in columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[c] for c in columns))
                transfers[target].append(dict(slot=offer['slot'],destination=dest,item_uid=row.get('item_uid'),count=count))
            fee = 0
            for cid in cids:
                peer = cids[1] if cid == cids[0] else cids[0]
                # No unverified CN-client fee is imposed on this US protocol.
                tax = 0
                fee += tax
                balance = wallets[cid]-gold.get(cid,0)+gold.get(peer,0)-tax
                if not 0 <= balance <= 2_000_000_000:
                    raise ExchangeError('recipient wallet limit exceeded')
                wallets[cid] = balance
                db.execute('UPDATE character_wallets SET gold=?,updated_at=CURRENT_TIMESTAMP WHERE character_id=?',(balance,cid))
            result=dict(wallets=wallets,transfers=transfers,fee=fee)
            db.execute('INSERT INTO character_player_trade_receipts(trade_id,first_character_id,second_character_id,result_json) VALUES(?,?,?,?)',(trade_id,*cids,json.dumps(result)))
            return result
