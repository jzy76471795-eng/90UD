"""Town fatigue recovery from PVF and the persisted character fatigue ledger.

Frozen Stage3: 02EBBB80 maps [add fatigue] to 0x36. The retained player
trace 2026-10-30 00:22:07 sends C1/0204, 19 bytes, slot107/action0x36.
No request field is an authoritative recovery amount.
"""
from dataclasses import dataclass
from functools import lru_cache
import re
import time

from .fatigue import FatigueBalance, FatigueStore
from .fatigue_flow import RESET_UTC_OFFSET_SECONDS, ensure_daily_balance
from .item_metadata import job_label_for_id
from pvf import pvf_runtime


ACTION = 0x36
SERIA_FATIGUE_ITEM_ID = 10098032
SERIA_FATIGUE_DAILY_COUNT = 5
# Lucky gift-box potions: each item ID has its own three uses per character/day.
LETTER_FATIGUE_ITEM_IDS = (10147585, 10015010, 50030627)
LETTER_FATIGUE_DAILY_COUNT = 3
SCHEMA = '''CREATE TABLE IF NOT EXISTS character_fatigue_consumables (
    character_id INTEGER NOT NULL REFERENCES characters(character_id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL CHECK(item_id>0), day_number INTEGER NOT NULL,
    use_count INTEGER NOT NULL CHECK(use_count>=0),
    last_used_ms INTEGER NOT NULL CHECK(last_used_ms>=0),
    PRIMARY KEY(character_id,item_id))'''


@dataclass(frozen=True)
class FatigueConsumableRule:
    amount: int
    cooltime_ms: int
    daily_count: int


@lru_cache(maxsize=128)
def parse_rule(text):
    action = re.search(r'\[action type\]\s*`\[add fatigue\]`\s*(\d+)\s*\[/action type\]', text)
    village = re.search(r'\[action usable place\]\s*`\[village\]`\s*\[/action usable place\]', text)
    if not action or not village:
        raise ValueError('fatigue PVF action/place is unsupported')
    def number(tag):
        match = re.search(r'\['+re.escape(tag)+r'\]\s*(\d+)', text)
        return int(match[1]) if match else 0
    amount = int(action[1])
    if not 1 <= amount <= 65535:
        raise ValueError('fatigue PVF amount is invalid')
    return FatigueConsumableRule(amount, number('cool time'), number('total usable count'))


def consume_at(repo, *, account_id, character_id, slot, item_id, metadata,
               in_dungeon=False, now_ms=None):
    """Consume, restore and record limits in one IMMEDIATE transaction.

    The 188/day profile defines the cap and 06:00 reset. Each letter gift
    potion has three uses per character/day, retaining existing per-item counts.
    Other [total usable count] limits and per-item cooldowns remain PVF based.
    """
    if in_dungeon:
        return False, 'in_dungeon', None
    if (metadata.item_id != item_id or metadata.action_type != '[add fatigue]'
            or not metadata.is_stackable or not metadata.source_path or slot < 3):
        return False, 'fatigue_rule_mismatch', None
    rule = parse_rule(pvf_runtime.read_text(metadata.source_path))
    now = int(time.time()*1000) if now_ms is None else int(now_ms)
    day = (now//1000 + RESET_UTC_OFFSET_SECONDS)//86400
    ensure_daily_balance(repo, account_id, character_id, now=now/1000)
    store = FatigueStore(repo)
    with store._write(account_id, character_id) as conn:
        conn.execute(SCHEMA)
        marker = conn.execute('SELECT day_number FROM character_fatigue_days '
            'WHERE character_id=?', (character_id,)).fetchone()
        if marker is None or marker[0] != day:
            return False, 'fatigue_day_changed', None
        character = conn.execute('SELECT level,job FROM characters WHERE character_id=?',
            (character_id,)).fetchone()
        if character[0] < metadata.minimum_level:
            return False, 'below_minimum_level', None
        jobs = {j.lower().strip('[]') for j in metadata.usable_jobs}
        if jobs and 'all' not in jobs and job_label_for_id(character[1]) not in jobs:
            return False, 'unusable_job', None
        row = conn.execute('SELECT item_uid,item_template_id,item_kind,stack_count,expire_time '
            'FROM character_items WHERE character_id=? AND list_type=0 AND slot_index=?',
            (character_id, slot)).fetchone()
        if row is None or row[1] != item_id or row[2] != 'stackable' or row[3] <= 0:
            return False, 'item_mismatch', None
        if row[4] and row[4] <= now//1000:
            return False, 'item_expired', None
        saved = store._read(conn, character_id, account_id)
        if saved is None or saved.balance.used < rule.amount:
            return False, 'insufficient_spent_fatigue', None
        previous = conn.execute('SELECT day_number,use_count,last_used_ms '
            'FROM character_fatigue_consumables WHERE character_id=? AND item_id=?',
            (character_id, item_id)).fetchone()
        count = previous[1] if previous and previous[0] == day else 0
        daily_count = rule.daily_count
        if item_id == SERIA_FATIGUE_ITEM_ID:
            daily_count = SERIA_FATIGUE_DAILY_COUNT
        if item_id in LETTER_FATIGUE_ITEM_IDS:
            daily_count = LETTER_FATIGUE_DAILY_COUNT
        if daily_count and count >= daily_count:
            return False, 'daily_count_exhausted', None
        if previous and now-previous[2] < rule.cooltime_ms:
            return False, 'cooltime_active', None
        balance = FatigueBalance(saved.balance.remaining+rule.amount, saved.balance.limit)
        remaining = row[3]-1
        if remaining:
            conn.execute('UPDATE character_items SET stack_count=?,instance_value=?, '
                'updated_at=CURRENT_TIMESTAMP WHERE item_uid=?', (remaining,remaining,row[0]))
        else:
            conn.execute('DELETE FROM character_items WHERE item_uid=?', (row[0],))
        conn.execute('UPDATE character_fatigue_balances SET remaining=?,revision=revision+1 '
            'WHERE character_id=?', (balance.remaining, character_id))
        conn.execute('INSERT INTO character_fatigue_consumables VALUES(?,?,?,?,?) '
            'ON CONFLICT(character_id,item_id) DO UPDATE SET '
            'day_number=excluded.day_number,use_count=excluded.use_count,last_used_ms=excluded.last_used_ms',
            (character_id,item_id,day,count+1,now))
        return True, 'fatigue_recovered', balance
