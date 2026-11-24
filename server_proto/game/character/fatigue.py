"""Explicit character fatigue balances and durable server-owned debits.

The CN reference separates remaining balance from native used-count wire data
and owns per-character writes in a transaction. The pinned US reader 01652C00
also consumes five u16 words; 01E79CC0 clamps the first counter and transmits
max-current as remaining. Its three auxiliary words are required separately.

No legacy player is initialized, daily allowance inferred, client claim accepted
or Game hook enabled here. Initialization requires an independently obtained
balance and a source reference. Room/run identity belongs to the caller.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import struct


def _uint(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('authoritative fatigue ' + label + ' unavailable')
    return value


def _text(value, label):
    if type(value) is not str or not value.strip() or len(value) > 256 or '\0' in value:
        raise ValueError('fatigue ' + label + ' unavailable')
    return value


@dataclass(frozen=True)
class FatigueBalance:
    remaining: int
    limit: int

    def __post_init__(self):
        _uint(self.limit, 65535, 'limit')
        _uint(self.remaining, self.limit, 'remaining balance')

    @property
    def used(self):
        return self.limit - self.remaining


def build_native_fatigue_body(balance, *, actor_aux, counter_aux, actor_extra):
    """N0024: used, limit, explicit actor/counter/extra auxiliary words."""
    if type(balance) is not FatigueBalance:
        raise ValueError('complete authoritative fatigue balance unavailable')
    return struct.pack('<5H', balance.used, balance.limit,
        _uint(actor_aux, 65535, 'actor auxiliary word'),
        _uint(counter_aux, 65535, 'counter auxiliary word'),
        _uint(actor_extra, 65535, 'actor extra word'))


@dataclass(frozen=True)
class SavedFatigue:
    balance: FatigueBalance
    revision: int
    source_ref: str


@dataclass(frozen=True)
class FatigueDebit:
    applied: bool
    before_remaining: int
    after_remaining: int
    current: SavedFatigue

    @property
    def spent(self):
        return self.before_remaining - self.after_remaining


class FatigueStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _owner(conn, account_id, character_id):
        if (_uint(account_id, 0x7fffffff, 'account') == 0
                or _uint(character_id, 0x7fffffff, 'character') == 0):
            raise ValueError('fatigue actor identity unavailable')
        row = conn.execute('SELECT 1 FROM characters WHERE character_id=? '
            'AND account_id=? AND delete_flag=0', (character_id, account_id)).fetchone()
        if row is None:
            raise ValueError('fatigue character owner unavailable')

    @staticmethod
    def _available(conn):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='character_fatigue_balances'").fetchone() is not None

    @staticmethod
    def _read(conn, character_id, account_id=None):
        query = ('SELECT f.remaining,f.limit_value,f.revision,f.source_ref '
            'FROM character_fatigue_balances f ')
        if account_id is None:
            row = conn.execute(query+'WHERE f.character_id=?',(character_id,)).fetchone()
        else:
            row = conn.execute(query+'JOIN characters c ON c.character_id=f.character_id '
                'WHERE f.character_id=? AND c.account_id=? AND c.delete_flag=0',
                (character_id,account_id)).fetchone()
        if row is None:
            return None
        revision = _uint(row[2], 0x7fffffffffffffff, 'revision')
        return SavedFatigue(FatigueBalance(row[0],row[1]),revision,_text(row[3],'source reference'))

    @contextmanager
    def _write(self, account_id, character_id):
        nested = False
        with self.repository.open() as conn:
            # Exit the repository's nested context normally before rejecting.
            # Raising inside it would poison Game's outer rollback_only flag.
            if conn.in_transaction:
                nested = True
            else:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    self._owner(conn,account_id,character_id)
                    yield conn
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        if nested:
            raise ValueError('fatigue write requires its own repository transaction')

    def load(self, *, account_id, character_id):
        with self.repository.open() as conn:
            self._owner(conn,account_id,character_id)
            if not self._available(conn):
                return None
            # Couple the read to its current owner as well as validating the
            # input identity; selection/ownership changes cannot expose a row.
            return self._read(conn,character_id,account_id)

    def initialize(self, *, account_id, character_id, balance, source_ref):
        if type(balance) is not FatigueBalance:
            raise ValueError('explicit authoritative fatigue balance unavailable')
        _text(source_ref,'source reference')
        with self._write(account_id,character_id) as conn:
            # Deliberately lazy: loading an old character or importing this module
            # cannot create a schema or invent its first balance.
            conn.execute('CREATE TABLE IF NOT EXISTS character_fatigue_balances ('
                'character_id INTEGER PRIMARY KEY REFERENCES characters(character_id) ON DELETE CASCADE,'
                'remaining INTEGER NOT NULL CHECK(remaining>=0 AND remaining<=limit_value),'
                'limit_value INTEGER NOT NULL CHECK(limit_value BETWEEN 0 AND 65535),'
                'revision INTEGER NOT NULL CHECK(revision>=0),source_ref TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS character_fatigue_debits ('
                'character_id INTEGER NOT NULL REFERENCES character_fatigue_balances(character_id) ON DELETE CASCADE,'
                'operation_key TEXT NOT NULL,requested INTEGER NOT NULL CHECK(requested BETWEEN 1 AND 65535),'
                'before_remaining INTEGER NOT NULL CHECK(before_remaining BETWEEN 0 AND 65535),'
                'after_remaining INTEGER NOT NULL CHECK(after_remaining BETWEEN 0 AND before_remaining),'
                'PRIMARY KEY(character_id,operation_key))')
            if self._read(conn,character_id) is not None:
                raise ValueError('fatigue initialization cannot overwrite an existing balance')
            conn.execute('INSERT INTO character_fatigue_balances VALUES(?,?,?,?,?)',
                (character_id,balance.remaining,balance.limit,0,source_ref))
            return self._read(conn,character_id)

    def debit(self, *, account_id, character_id, operation_key, amount):
        _text(operation_key,'server operation key')
        if _uint(amount,65535,'debit amount') == 0:
            raise ValueError('fatigue debit amount must be positive')
        with self._write(account_id,character_id) as conn:
            saved = self._read(conn,character_id) if self._available(conn) else None
            if saved is None:
                raise ValueError('known persisted fatigue balance unavailable')
            old = conn.execute('SELECT requested,before_remaining,after_remaining '
                'FROM character_fatigue_debits WHERE character_id=? AND operation_key=?',
                (character_id,operation_key)).fetchone()
            if old is not None:
                if old[0] != amount:
                    raise ValueError('fatigue operation key was reused with a different cost')
                return FatigueDebit(False,old[1],old[2],saved)
            before = saved.balance.remaining
            after = max(0,before-amount)
            conn.execute('UPDATE character_fatigue_balances SET remaining=?,revision=revision+1 '
                'WHERE character_id=?',(after,character_id))
            conn.execute('INSERT INTO character_fatigue_debits VALUES(?,?,?,?,?)',
                (character_id,operation_key,amount,before,after))
            return FatigueDebit(True,before,after,self._read(conn,character_id))
