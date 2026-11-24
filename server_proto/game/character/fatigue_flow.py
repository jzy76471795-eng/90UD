"""Ordinary-room fatigue integration for the user's 188-point daily profile.

The daily allowance and 06:00 UTC+8 reset are the user-selected local policy,
not a claim about an historical US schedule.
N0024 auxiliary words match this server's existing START projection (all zero).
Client remaining-balance claims never write the ledger.
"""
from functools import lru_cache
import re
import time
import uuid

from .fatigue import FatigueBalance, FatigueStore, build_native_fatigue_body


DAILY_LIMIT = 188
RESET_UTC_OFFSET_SECONDS = 2 * 3600  # UTC+8 minus the 06:00 day boundary.


def ensure_daily_balance(repo, account_id, character_id, *, now=None):
    day = int((time.time() if now is None else now) + RESET_UTC_OFFSET_SECONDS) // 86400
    store = FatigueStore(repo)
    saved = store.load(account_id=account_id, character_id=character_id)
    if saved is None:
        saved = store.initialize(account_id=account_id, character_id=character_id,
            balance=FatigueBalance(DAILY_LIMIT, DAILY_LIMIT),
            source_ref='local-profile:daily-limit-188:20261112')
    with store._write(account_id, character_id) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS character_fatigue_days ('
            'character_id INTEGER PRIMARY KEY REFERENCES character_fatigue_balances(character_id) ON DELETE CASCADE,'
            'day_number INTEGER NOT NULL)')
        old = conn.execute('SELECT day_number FROM character_fatigue_days WHERE character_id=?',
            (character_id,)).fetchone()
        if old is None:
            # Adopting an existing ledger must not erase its spent balance.
            conn.execute('INSERT INTO character_fatigue_days VALUES(?,?)', (character_id, day))
        elif day > old[0]:
            conn.execute('UPDATE character_fatigue_balances SET remaining=?,limit_value=?,revision=revision+1 '
                'WHERE character_id=?', (DAILY_LIMIT, DAILY_LIMIT, character_id))
            conn.execute('UPDATE character_fatigue_days SET day_number=? WHERE character_id=?', (day, character_id))
        current = store._read(conn, character_id, account_id)
        if current.balance.limit != DAILY_LIMIT:
            spent = current.balance.limit - current.balance.remaining
            conn.execute('UPDATE character_fatigue_balances SET remaining=?,limit_value=?,revision=revision+1 '
                'WHERE character_id=?', (max(0, DAILY_LIMIT-spent), DAILY_LIMIT, character_id))
        return store._read(conn, character_id, account_id)


def parse_dungeon_rule(text):
    if re.search(r'^\s*\[no fatigue\]\s*$', text, re.M):
        return 0, True
    free_entry = bool(re.search(r'^\s*\[enter without fatigue\]\s*$', text, re.M))
    rounds = re.search(r'\[tournament round fatigue\]\s*(\d+)', text)
    return (max(0, int(rounds.group(1))) if rounds else 1), free_entry


@lru_cache(maxsize=4096)
def dungeon_rule(dungeon_id):
    from game.quest.quest_index import get_quest_index
    from pvf import pvf_runtime
    index = get_quest_index()
    dungeon = index.get_dungeon(int(dungeon_id))
    if not dungeon.path:
        raise ValueError('fatigue dungeon resource unavailable')
    return parse_dungeon_rule(pvf_runtime.read_text(dungeon.path))


def login_balance(repo, character_id):
    account_id = repo.get_account_id_for_character(int(character_id))
    if account_id is None:
        raise ValueError('fatigue character owner unavailable')
    return ensure_daily_balance(repo, int(account_id), int(character_id)).balance


def native_body(balance):
    return build_native_fatigue_body(balance, actor_aux=0, counter_aux=0, actor_extra=0)


def prepare_run(server, session, dungeon_id):
    """Validate entry and replace the token only for an authorized new run.

    Called after dungeon resolution, before its START_MAP is published. No
    fatigue is spent until FINISH_LOADING for a server-selected room.
    """
    cost, free_entry = dungeon_rule(int(dungeon_id))
    cid = int(getattr(session, 'selected_character_id', 0) or 0)
    if cid <= 0:
        raise ValueError('fatigue selected character unavailable')
    balance = login_balance(server._repo(), cid)
    if cost and not free_entry and balance.remaining == 0:
        session._ordinary_fatigue_run = None
        return False
    session._ordinary_fatigue_run = (uuid.uuid4().hex, cid, int(dungeon_id), cost)
    return True


def finish_room(server, session):
    if session is None or not bool(getattr(session, 'in_dungeon_run', False)):
        return None
    run = getattr(session, '_ordinary_fatigue_run', None)
    if run is None:
        return None
    token, cid, dungeon_id, cost = run
    if (not cost or cid != getattr(session, 'selected_character_id', None)
            or dungeon_id != getattr(session, 'selected_dungeon_id', None)):
        return None
    cell = (getattr(session, 'dungeon_cell_x', None), getattr(session, 'dungeon_cell_y', None))
    if any(type(v) is not int or not 0 <= v <= 255 for v in cell):
        raise ValueError('fatigue authoritative room unavailable')
    repo = server._repo()
    account = repo.get_account_id_for_character(cid)
    amount = cost * (2 if getattr(session, 'fatigue_burning_enabled', False) else 1)
    # Include coordinates, not the requested map ID: a repeated loading ACK or
    # a revisit of the same cell is not another charge. New runs get a UUID.
    key = 'room:%s:%d:%d' % (token, cell[0], cell[1])
    # Freeze a room's first cost across mid-room burning toggles/replayed ACKs.
    costs = getattr(session, '_ordinary_fatigue_room_costs', None)
    if costs is None or costs[0] != token:
        costs = (token, {})
        session._ordinary_fatigue_room_costs = costs
    amount = costs[1].setdefault(cell, amount)
    debit = FatigueStore(repo).debit(account_id=int(account), character_id=cid,
        operation_key=key, amount=amount)
    return native_body(debit.current.balance)
