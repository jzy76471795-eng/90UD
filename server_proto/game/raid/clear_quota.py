"""Character clear quotas and the frozen client's six-byte N0519 snapshot.

Days turn at 06:00 Asia/Shanghai; weeks turn on Monday at the same time.
Admission and failed runs never spend a clear. Ledger transactions own writes.
"""
from datetime import datetime, timedelta, timezone
from math import isfinite
from time import monotonic, time

from .ledger import LedgerError, RaidLedger
from .runtime import RaidError

DAILY_LIMIT = 1
WEEKLY_LIMIT = 5
N_RAID_ENTRANCE_INFO = 0x0519
_BEIJING = timezone(timedelta(hours=8))


class ClearQuotaExceeded(LedgerError, RaidError):
    pass


def period_keys(timestamp):
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not isfinite(timestamp):
        raise LedgerError('trusted completion time required')
    day = (datetime.fromtimestamp(timestamp, _BEIJING) - timedelta(hours=6)).date()
    return day.isoformat(), (day - timedelta(days=day.weekday())).isoformat()


def counts(conn, character_id, family, timestamp):
    day, week = period_keys(timestamp)
    row = conn.execute('SELECT COALESCE(SUM(day_key=?),0), COUNT(*) FROM raid_character_clears '
                       'WHERE character_id=? AND family=? AND week_key=?',
                       (day, character_id, family, week)).fetchone()
    return int(row[0]), int(row[1])


def require_available(conn, character_id, family, timestamp):
    daily, weekly = counts(conn, character_id, family, timestamp)
    if weekly >= WEEKLY_LIMIT:
        raise ClearQuotaExceeded('character weekly raid clear limit reached (5)')
    if daily >= DAILY_LIMIT:
        raise ClearQuotaExceeded('character daily raid clear limit reached (1)')
    return daily, weekly


def entrance_body(ledger, character_id, account_id, timestamp):
    # 0119E790 reads exactly two {u8 kind, u8 used, u8 maximum} records.
    # A zero maximum uses the weekly-exhausted localized label. Never invent
    # today's count to represent a weekly limit, or reuse account entry counts.
    rows = []
    for kind, family in ((1, 'anton'), (2, 'luke')):
        daily, weekly = ledger.clear_counts(character_id, account_id, family, timestamp)
        rows.extend((kind, min(daily, DAILY_LIMIT), DAILY_LIMIT if weekly < WEEKLY_LIMIT else 0))
    return bytes(rows)


def ledger_for(server):
    ledger = getattr(server, '_raid_clear_quota_ledger', None)
    if ledger is None:
        ledger = RaidLedger(server._repo())
        ledger.ensure_schema()
        server._raid_clear_quota_ledger = ledger
    return ledger


def pump(server, session, client):
    """Refresh all channels after successful login, clear, selection or reset."""
    cid, aid = getattr(session, 'selected_character_id', None), getattr(session, 'account_id', None)
    if not cid or not aid or not getattr(session, 'enter_seria_settled', False):
        return
    if int(getattr(server, 'account_id', 0) or 0) != int(aid):
        return
    identity = (cid, aid, getattr(session, 'selected_channel_id', None),
                getattr(session, '_raid_selected_epoch', None))
    now = monotonic()
    previous = getattr(session, '_raid_clear_quota_sent', None)
    if previous is not None and previous[0] == identity and now - previous[2] < 1:
        return
    body = entrance_body(ledger_for(server), cid, aid, time())
    if previous is None or previous[:2] != (identity, body):
        client.sendall(server._codec_response(0, N_RAID_ENTRANCE_INFO, body))
    session._raid_clear_quota_sent = (identity, body, now)
