"""One atomic all-to-account-cargo transition; no shadow/projection fallback."""
from pathlib import Path
import sqlite3

MIGRATION_KEY = 'character_wallets_all_to_account_cargo_v1'
POLICY = 'all_to_account_cargo'
U32_MAX = 0xFFFFFFFF
SQLITE_MAX = 0x7FFFFFFFFFFFFFFF

def _cargo_cap(selection_key, balance):
    # A closed empty cargo is valid for a fresh account. Money requires a
    # known tier and an explicitly warmed PVF capacity index.
    if selection_key == 0 and balance == 0:
        return 0
    from game.character.account_cargo_gold_cap import get_account_cargo_gold_cap
    return get_account_cargo_gold_cap(selection_key)

def _integer(value, name, maximum=SQLITE_MAX):
    if not isinstance(value, int) or value < 0 or value > maximum:
        raise ValueError('invalid_%s' % name)
    return value

def ensure_character_wallet_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS character_wallets (
        character_id INTEGER PRIMARY KEY,
        gold INTEGER NOT NULL DEFAULT 0 CHECK(typeof(gold)='integer' AND gold BETWEEN 0 AND 9223372036854775807),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE CASCADE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS character_wallet_migrations (
        migration_key TEXT PRIMARY KEY, policy TEXT NOT NULL,
        total_before INTEGER NOT NULL, total_after INTEGER NOT NULL,
        account_count INTEGER NOT NULL, character_count INTEGER NOT NULL,
        migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS character_wallet_migration_accounts (
        account_id INTEGER PRIMARY KEY, legacy_gold INTEGER NOT NULL,
        cargo_before INTEGER NOT NULL, cargo_after INTEGER NOT NULL,
        revive_before INTEGER NOT NULL,
        migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')

def validate_character_wallet_migration(conn):
    try:
        marker = conn.execute('''SELECT policy,total_before,total_after,account_count
            FROM character_wallet_migrations WHERE migration_key=?''', (MIGRATION_KEY,)).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError('character_wallet_migration_missing') from exc
    if marker is None:
        raise ValueError('character_wallet_migration_missing')
    if marker[0] != POLICY or marker[1] != marker[2]:
        raise ValueError('character_wallet_migration_invalid')
    rows = conn.execute('''SELECT legacy_gold,cargo_before,cargo_after,revive_before
        FROM character_wallet_migration_accounts ORDER BY account_id''').fetchall()
    before = after = 0
    for row in rows:
        old = _integer(row[0], 'migration_legacy_gold')
        cargo_before = _integer(row[1], 'migration_cargo_before', U32_MAX)
        cargo_after = _integer(row[2], 'migration_cargo_after', U32_MAX)
        _integer(row[3], 'migration_revive_before')
        if old + cargo_before != cargo_after:
            raise ValueError('character_wallet_migration_invalid')
        before += old + cargo_before
        after += cargo_after
    if len(rows) != marker[3] or before != marker[1] or after != marker[2]:
        raise ValueError('character_wallet_migration_invalid')
    if conn.execute('SELECT 1 FROM account_wallets WHERE gold!=0 LIMIT 1').fetchone():
        raise ValueError('legacy_account_gold_reintroduced')
    if conn.execute('''SELECT 1 FROM characters c LEFT JOIN character_wallets w
        ON w.character_id=c.character_id WHERE w.character_id IS NULL LIMIT 1''').fetchone():
        raise ValueError('character_wallet_missing')
    if conn.execute('''SELECT 1 FROM character_wallets WHERE typeof(gold)!='integer'
        OR gold<0 OR gold>9223372036854775807 LIMIT 1''').fetchone():
        raise ValueError('character_wallet_invalid')
    if conn.execute('''SELECT 1 FROM account_cargo_state WHERE typeof(value32)!='integer'
        OR value32<0 OR value32>4294967295 LIMIT 1''').fetchone():
        raise ValueError('cargo_gold_invalid')
    for key, balance in conn.execute('SELECT selection_key,value32 FROM account_cargo_state'):
        if balance > _cargo_cap(key, balance):
            raise ValueError('cargo_gold_business_cap_exceeded')
    return {'noop': True, 'total_before': marker[1], 'total_after': marker[2],
            'account_count': marker[3], 'policy': POLICY}

def migrate_character_wallet_database(path):
    """Called offline before deployment or once by schema startup compatibility."""
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        ensure_character_wallet_schema(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS account_wallets (
            account_id INTEGER PRIMARY KEY,gold INTEGER NOT NULL DEFAULT 0 CHECK(gold>=0),
            revive_coin INTEGER NOT NULL DEFAULT 0 CHECK(revive_coin>=0),
            migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS account_cargo_state (
            account_id INTEGER PRIMARY KEY,selection_key INTEGER NOT NULL DEFAULT 0,
            value32 INTEGER NOT NULL DEFAULT 0,item_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        for operation in ('INSERT', 'UPDATE OF value32'):
            suffix = 'insert' if operation == 'INSERT' else 'update'
            conn.execute('''CREATE TRIGGER IF NOT EXISTS account_cargo_gold_u32_%s
                BEFORE %s ON account_cargo_state
                WHEN typeof(NEW.value32)!='integer' OR NEW.value32<0 OR NEW.value32>4294967295
                BEGIN SELECT RAISE(ABORT,'cargo_gold_invalid'); END''' % (suffix, operation))
        marker = conn.execute('SELECT 1 FROM character_wallet_migrations WHERE migration_key=?',
                              (MIGRATION_KEY,)).fetchone()
        if marker:
            result = validate_character_wallet_migration(conn)
            conn.commit()
            return result
        if conn.execute('SELECT 1 FROM character_wallets LIMIT 1').fetchone():
            raise ValueError('unmarked_character_wallets')
        if conn.execute('SELECT 1 FROM character_wallet_migration_accounts LIMIT 1').fetchone():
            raise ValueError('unmarked_wallet_migration_accounts')
        if conn.execute('SELECT 1 FROM character_wallet_migrations LIMIT 1').fetchone():
            raise ValueError('character_wallet_migration_invalid')
        accounts = [row[0] for row in conn.execute('SELECT account_id FROM accounts ORDER BY account_id')]
        if conn.execute('''SELECT 1 FROM account_wallets w LEFT JOIN accounts a
            ON a.account_id=w.account_id WHERE a.account_id IS NULL LIMIT 1''').fetchone():
            raise ValueError('orphan_account_wallet')
        plans = []
        total_before = 0
        for aid in accounts:
            wallet = conn.execute('SELECT gold,revive_coin FROM account_wallets WHERE account_id=?', (aid,)).fetchone()
            if wallet is None:
                # A missing authoritative shared row must never cause summing
                # duplicated display projections or recovering the old shadow.
                if conn.execute('''SELECT 1 FROM character_items ci JOIN characters c
                    ON c.character_id=ci.character_id WHERE c.account_id=? AND ci.list_type=0
                    AND ((ci.slot_index=0 AND ci.item_template_id=0) OR ci.item_template_id=1)
                    AND ci.stack_count!=0 LIMIT 1''', (aid,)).fetchone():
                    raise ValueError('account_wallet_missing_with_legacy_currency')
                gold = coins = 0
            else:
                gold = _integer(wallet[0], 'legacy_gold')
                coins = _integer(wallet[1], 'revive_coin')
            cargo = conn.execute('SELECT selection_key,value32 FROM account_cargo_state WHERE account_id=?', (aid,)).fetchone()
            stored = _integer(cargo[1], 'cargo_gold', U32_MAX) if cargo else 0
            key = cargo[0] if cargo else 0
            if key == 0 and stored + gold > 0:
                key = 80
            if gold > _cargo_cap(key, stored + gold)-stored:
                raise ValueError('cargo_gold_overflow account_id=%s' % aid)
            plans.append((aid, gold, coins, stored, stored+gold, key))
            total_before += stored+gold
        if total_before > SQLITE_MAX:
            raise ValueError('migration_total_overflow')
        for aid, gold, coins, stored, target, key in plans:
            if not conn.execute('SELECT 1 FROM account_wallets WHERE account_id=?', (aid,)).fetchone():
                conn.execute('INSERT INTO account_wallets(account_id,gold,revive_coin) VALUES(?,0,0)', (aid,))
            if not conn.execute('SELECT 1 FROM account_cargo_state WHERE account_id=?', (aid,)).fetchone():
                conn.execute('INSERT INTO account_cargo_state(account_id,selection_key,value32) VALUES(?,?,?)', (aid,key,target))
            else:
                conn.execute('UPDATE account_cargo_state SET selection_key=?,value32=? WHERE account_id=?', (key,target,aid))
            conn.execute('UPDATE account_wallets SET gold=0 WHERE account_id=?', (aid,))
            conn.execute('''INSERT INTO character_wallet_migration_accounts
                (account_id,legacy_gold,cargo_before,cargo_after,revive_before) VALUES(?,?,?,?,?)''',
                (aid,gold,stored,target,coins))
        characters = conn.execute('SELECT character_id FROM characters ORDER BY character_id').fetchall()
        for (cid,) in characters:
            conn.execute('INSERT INTO character_wallets(character_id,gold) VALUES(?,0)', (cid,))
        # Preserve projection object identities, clear only each fixed gold view.
        conn.execute('''UPDATE character_items SET stack_count=0,instance_value=0
            WHERE list_type=0 AND slot_index=0 AND item_template_id=0''')
        conn.execute('''INSERT INTO character_wallet_migrations
            (migration_key,policy,total_before,total_after,account_count,character_count)
            VALUES(?,?,?,?,?,?)''', (MIGRATION_KEY,POLICY,total_before,total_before,len(plans),len(characters)))
        result = validate_character_wallet_migration(conn)
        # Validate the actual committed candidate balances, not only marker math.
        total_after = sum(row[0] for row in conn.execute('SELECT value32 FROM account_cargo_state'))
        total_after += sum(row[0] for row in conn.execute('SELECT gold FROM character_wallets'))
        if total_after != total_before:
            raise ValueError('wallet_migration_conservation_failed')
        for aid, _gold, coins, _stored, _target, _key in plans:
            actual = conn.execute('SELECT revive_coin FROM account_wallets WHERE account_id=?', (aid,)).fetchone()[0]
            if actual != coins:
                raise ValueError('wallet_migration_revive_changed')
        conn.commit()
        result.update(noop=False, total_after=total_after, character_count=len(characters))
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
