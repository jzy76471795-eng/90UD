"""Persistent account growth capsules driven by the Adventure Group PVF."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import time

from game.character.cn2018_expedition import CN_TIME
from game.chain_loader import load_chain
from pvf import pvf_runtime
from pvf.adventure_group_feature_index import _field_values, _lines
from pvf.cn_expedition_index import ADVENTURER_2018_PATH


class CapsuleRejected(ValueError):
    pass


@dataclass(frozen=True)
class CapsuleRules:
    minimum_exp: int
    maximum_exp: int
    maximum_count: int
    minimum_level: int
    maximum_level: int
    reward_exp: int
    reset_anchor: int

    def week(self, now):
        return (int(now) - self.reset_anchor) // (7 * 86400)


@lru_cache(maxsize=4)
def parse_rules(text):
    rows = _lines(text)

    def field(name):
        value, = _field_values(rows, f"capsule {name}")
        return value

    minimum = int(field("need minimum exp"))
    maximum = int(field("max exp"))
    count = int(field("max count"))
    level_min = int(field("limit level min"))
    level_max = int(field("limit level max"))
    reward = int(field("give exp"))
    reset = datetime.fromisoformat(field("update date").strip("`")).replace(tzinfo=CN_TIME)
    if min(minimum, maximum, count, reward) <= 0 or (maximum != minimum * count) or not 1 <= level_min <= level_max < 90:
        raise ValueError("invalid adventure capsule configuration")
    return CapsuleRules(minimum, maximum, count, level_min, level_max, reward, int(reset.timestamp()))


def rules():
    return parse_rules(pvf_runtime.read_text(ADVENTURER_2018_PATH))


def _stored(conn, account, week):
    row = conn.execute(
        "SELECT stored_exp FROM account_cn2018_capsules WHERE account_id=? AND week_key=?",
        (account, week),
    ).fetchone()
    return int(row[0]) if row else 0


def view(repo, account, now):
    config = rules()
    with repo.open() as conn:
        stored = _stored(conn, account, config.week(now))
    return (stored, config.maximum_exp, stored // config.minimum_exp,
            config.reward_exp, config.minimum_level, config.maximum_level)


def accumulate_tx(conn, account, experience, now=None):
    try:
        config = rules()
    except (KeyError, FileNotFoundError, ValueError):
        # A legacy-only PVF may run with the shared repository implementation.
        return
    week = config.week(time.time() if now is None else now)
    conn.execute(
        "INSERT INTO account_cn2018_capsules(account_id,week_key,stored_exp) VALUES (?,?,?) "
        "ON CONFLICT(account_id) DO UPDATE SET week_key=excluded.week_key, stored_exp="
        "MIN(?, CASE WHEN week_key=excluded.week_key THEN stored_exp+excluded.stored_exp "
        "ELSE excluded.stored_exp END) WHERE week_key<=excluded.week_key",
        (account, week, min(experience, config.maximum_exp), config.maximum_exp),
    )


def use(repo, account, character, quantity, request, now):
    config = rules()
    if not 1 <= quantity <= config.maximum_count or not 1 <= request <= 0xFFFFFFFF:
        raise CapsuleRejected("invalid_capsule_quantity")
    week = config.week(now)
    with repo.inventory_transaction_scope() as conn:
        receipt = conn.execute(
            "SELECT character_id,quantity FROM account_cn2018_capsule_uses WHERE account_id=? AND request_id=?",
            (account, request),
        ).fetchone()
        if receipt is not None:
            if tuple(receipt) != (character, quantity):
                raise CapsuleRejected("request_id_conflict")
            return None
        row = conn.execute(
            "SELECT level,exp FROM characters WHERE character_id=? AND account_id=? AND delete_flag=0",
            (character, account),
        ).fetchone()
        if row is None or not config.minimum_level <= int(row[0]) <= config.maximum_level:
            raise CapsuleRejected("capsule_character_level")
        spent = config.minimum_exp * quantity
        if _stored(conn, account, week) < spent:
            raise CapsuleRejected("capsules_insufficient")
        progression = load_chain("helper-character_exp.py").grant_exp_to_character(
            repo, character, quantity * config.reward_exp, expected_level=int(row[0]), expected_exp=int(row[1]),
        )
        if not progression["ok"] or progression["ordinary_exp_added"] != quantity * config.reward_exp:
            raise CapsuleRejected("capsule_experience_not_applied")
        conn.execute(
            "UPDATE account_cn2018_capsules SET stored_exp=stored_exp-? WHERE account_id=? AND week_key=?",
            (spent, account, week),
        )
        conn.execute(
            "INSERT INTO account_cn2018_capsule_uses(account_id,request_id,character_id,quantity) VALUES (?,?,?,?)",
            (account, request, character, quantity),
        )
    return progression
