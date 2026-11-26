"""Account-wide progress and inventory settlement for the 2018 weekly event."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from functools import lru_cache
import os
from time import time
from uuid import uuid4

from game.character.item_metadata import resolve_item_metadata
from game.native_mailbox import identity


START_ENV = "USDOF_GAME_WEEKLY_ATTENDANCE_START"
EVENT_ID = 1083
WEEK_SECONDS = int(timedelta(weeks=1).total_seconds())
WEEKLY_ITEMS = (490009970, 490009971, 490009972, 490009973, 490009974,
                490009975, 490009976, 490009977, 490009978, 490009979)
MILESTONE_ITEMS = {3: 490009980, 6: 490009981, 9: 490009982}
ARMOR_VOUCHER = 490009989
DATED_CONTENT_ITEMS = frozenset((490009983, 490009984, 490009985, 490009987,
                                 490009988, ARMOR_VOUCHER, 490009990, 490009991,
                                 490009993, 490009994, 490009995, 490009996,
                                 490009997, 490010029))
RAID_DAILY_REWARDS = {
    490009996: 10162064,
    490009997: 10162065,
    10162064: 10096296,
    10162065: 10159649,
}
LEVEL_GATED_BOXES = frozenset((*WEEKLY_ITEMS, 490009996, 490009997))
WEEKLY_CLEARS = 4
MAKEUP_CLEARS = 20
MAKEUP_LIMIT = 2
EXCLUDED_DUNGEON_DIRS = frozenset(("anton", "luke", "warroom", "towers", "event", "tutorial"))


class Action(IntEnum):
    STATUS = 0
    SIGN_WEEK = 1
    MAKE_UP = 2
    CLAIM_CUMULATIVE = 3


class AttendanceError(ValueError):
    pass


@dataclass(frozen=True)
class Status:
    week: int
    clears: int
    makeup_credits: int
    makeup_week: int
    signed_weeks: tuple[int, ...]
    claimed_milestones: tuple[int, ...]


@lru_cache(maxsize=8)
def _parse_start(value: str) -> int | None:
    if not value:
        return None
    start = datetime.fromisoformat(value)
    if start.utcoffset() is None or start.microsecond:
        raise ValueError(f"{START_ENV} must be an ISO timestamp with a UTC offset")
    return int(start.timestamp())


def activity_start() -> int | None:
    return _parse_start(os.environ.get(START_ENV, "").strip())


def current_week(start: int | None, now: float) -> int:
    if start is None or now < start:
        return 0
    week = int((now - start) // WEEK_SECONDS) + 1
    return week if week <= len(WEEKLY_ITEMS) else 0


def begin_run(session, path: str, *, recommended: bool, raid: bool) -> None:
    directory, _, filename = path.replace("\\", "/").lower().removeprefix("dungeon/").partition("/")
    eligible = bool(filename and recommended and not raid and directory not in EXCLUDED_DUNGEON_DIRS)
    start = activity_start() if eligible else None
    dungeon_id = int(getattr(session, "selected_dungeon_id", 0) or 0)
    session.weekly_attendance_run = (
        (uuid4().hex, dungeon_id, start)
        if dungeon_id > 0 and start is not None and current_week(start, time()) else None
    )


_REWARD_SCHEMA = """CREATE TABLE IF NOT EXISTS weekly_attendance_rewards (
    activity_start INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    kind TEXT NOT NULL CHECK(kind IN ('week', 'cumulative')),
    slot INTEGER NOT NULL,
    is_makeup INTEGER NOT NULL CHECK(is_makeup IN (0, 1)),
    character_id INTEGER NOT NULL,
    granted_at INTEGER NOT NULL,
    PRIMARY KEY(activity_start, account_id, kind, slot))"""


def migrate_reward_ledger(conn) -> None:
    # The receiving character is historical. Account-wide claims survive character deletion.
    if not any(row[2] == "characters" for row in conn.execute("PRAGMA foreign_key_list(weekly_attendance_rewards)")):
        return
    conn.execute("ALTER TABLE weekly_attendance_rewards RENAME TO weekly_attendance_rewards_legacy")
    conn.execute(_REWARD_SCHEMA)
    conn.execute(
        """INSERT INTO weekly_attendance_rewards
           (activity_start, account_id, kind, slot, is_makeup, character_id, granted_at)
           SELECT activity_start, account_id, kind, slot, is_makeup, character_id, granted_at
           FROM weekly_attendance_rewards_legacy"""
    )
    conn.execute("DROP TABLE weekly_attendance_rewards_legacy")


def _ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS weekly_attendance_clears (
           activity_start INTEGER NOT NULL,
           account_id INTEGER NOT NULL REFERENCES accounts(account_id),
           run_id TEXT NOT NULL,
           week INTEGER NOT NULL CHECK(week BETWEEN 1 AND 10),
           PRIMARY KEY(activity_start, account_id, run_id))"""
    )
    conn.execute(_REWARD_SCHEMA)
    migrate_reward_ledger(conn)


def record_clear(server, session, *, now: float | None = None) -> bool:
    """Record a run only after the server has authorized standard dungeon settlement."""
    run = getattr(session, "weekly_attendance_run", None)
    if not run or not getattr(session, "in_dungeon_run", False) or getattr(session, "dungeon_clear_phase", 0) != 1:
        return False
    run_id, dungeon_id, start = run
    if start != activity_start() or dungeon_id != getattr(session, "selected_dungeon_id", 0):
        return False
    week = current_week(start, time() if now is None else now)
    if not week:
        return False
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        scope = (start, session.account_id)
        _ensure_schema(conn)
        count, = conn.execute(
            """SELECT COUNT(*) FROM weekly_attendance_clears
               WHERE activity_start=? AND account_id=? AND week=?""",
            (*scope, week),
        ).fetchone()
        if count >= WEEKLY_CLEARS + MAKEUP_CLEARS * MAKEUP_LIMIT:
            return False
        return conn.execute(
            """INSERT INTO weekly_attendance_clears VALUES (?, ?, ?, ?)
               ON CONFLICT(activity_start, account_id, run_id) DO NOTHING""",
            (*scope, run_id, week),
        ).rowcount == 1


def _read_status(conn, scope, week: int) -> Status:
    counts = dict(conn.execute(
        """SELECT week, COUNT(*) FROM weekly_attendance_clears
           WHERE activity_start=? AND account_id=? GROUP BY week""",
        scope,
    ))
    rewards = list(conn.execute(
        """SELECT kind, slot, is_makeup FROM weekly_attendance_rewards
           WHERE activity_start=? AND account_id=? ORDER BY slot""",
        scope,
    ))
    signed = tuple(slot for kind, slot, _ in rewards if kind == "week")
    claimed = tuple(slot for kind, slot, _ in rewards if kind == "cumulative")
    used = sum(makeup for _, _, makeup in rewards)
    extra = sum(max(0, count - WEEKLY_CLEARS) for count in counts.values())
    credits = max(0, min(extra // MAKEUP_CLEARS, MAKEUP_LIMIT) - used)
    makeup_week = next((slot for slot in range(1, week) if slot not in signed), 0)
    return Status(week, min(counts.get(week, 0), WEEKLY_CLEARS),
                  credits, makeup_week, signed, claimed)


def _targets(action: Action, expected_week: int, status: Status) -> tuple[str, bool, tuple[int, ...]]:
    if action == Action.SIGN_WEEK:
        if expected_week != status.week:
            raise AttendanceError("活动周次已更新，请重新打开签到面板。")
        if expected_week in status.signed_weeks:
            return "week", False, ()
        if status.clears < WEEKLY_CLEARS:
            raise AttendanceError("本周推荐地下城通关次数不足。")
        return "week", False, (expected_week,)
    if action == Action.MAKE_UP:
        if expected_week in status.signed_weeks and 0 < expected_week < status.week:
            return "week", True, ()
        if not status.makeup_credits or not status.makeup_week or expected_week != status.makeup_week:
            raise AttendanceError("当前没有可补签的周次或补签次数不足。")
        return "week", True, (expected_week,)
    if action == Action.CLAIM_CUMULATIVE:
        return "cumulative", False, tuple(
            slot for slot in MILESTONE_ITEMS
            if slot <= len(status.signed_weeks) and slot not in status.claimed_milestones
        )
    return "week", False, ()


def settle(server, session, action: Action, expected_week: int = 0, *, now: float | None = None):
    """Return authoritative status and the slots actually changed in the main bag."""
    now = time() if now is None else now
    start = activity_start()
    week = current_week(start, now)
    if not week:
        if action == Action.STATUS:
            return Status(0, 0, 0, 0, (), ()), ()
        raise AttendanceError("活动尚未开启或已结束。")
    repo = server._repo()
    with repo.inventory_transaction_scope() as conn:
        cid = identity(server, session, conn)
        scope = (start, session.account_id)
        _ensure_schema(conn)
        status = _read_status(conn, scope, week)
        kind, makeup, targets = _targets(action, expected_week, status)
        bag_slots = []
        for target in targets:
            item = WEEKLY_ITEMS[target - 1] if kind == "week" else MILESTONE_ITEMS[target]
            ok, _, item_slot = repo.grant_inventory_item(
                cid, item, 1, list_type=0, expire_time=start + len(WEEKLY_ITEMS) * WEEK_SECONDS,
                require_full_capacity=True,
            )
            if not ok or item_slot < 0:
                raise AttendanceError("请确认消耗品栏有空位后重试。")
            conn.execute(
                "INSERT INTO weekly_attendance_rewards VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*scope, kind, target, int(makeup), cid, int(now)),
            )
            bag_slots.append(item_slot)
        if bag_slots:
            status = _read_status(conn, scope, week)
        return status, tuple(bag_slots)


def redeem_armor_voucher(server, session, voucher_slot: int, armor_slot: int):
    """Consume one original voucher and replace the selected piece in place."""
    if not 3 <= voucher_slot < 0xFFFF or not 9 <= armor_slot < 0xFFFF or voucher_slot == armor_slot:
        raise AttendanceError("invalid_inventory_slots")
    voucher_meta = resolve_item_metadata(ARMOR_VOUCHER)
    ids = voucher_meta.action_values
    if voucher_meta.action_type != "[equipment item period change]" or len(ids) != 100 or len(set(ids)) != 100:
        raise AttendanceError("original_voucher_pair_table_unavailable")
    pairs = dict(zip(ids[::2], ids[1::2], strict=True))
    now = int(time())
    repo = server._repo()
    with repo.inventory_transaction_scope() as conn:
        cid = identity(server, session, conn)
        main = repo.INVENTORY_LIST_MAIN
        voucher = repo.get_inventory_item_at(cid, list_type=main, slot_index=voucher_slot, now_unix=now)
        if (not voucher or voucher["item_template_id"] != ARMOR_VOUCHER or voucher["stack_count"] < 1
                or voucher["expire_time"] <= now):
            raise AttendanceError("voucher_unavailable")
        armor = repo.get_inventory_item_at(cid, list_type=main, slot_index=armor_slot, now_unix=now)
        if (not armor or armor["item_kind"] != "equipment"
                or armor["stack_count"] != 1 or armor["expire_time"] <= now or armor["equipment_lock_id"]):
            raise AttendanceError("selected_armor_unavailable")
        armor_id = armor["item_template_id"]
        permanent_id = pairs.get(armor_id)
        if permanent_id is None:
            raise AttendanceError("armor_not_eligible")
        temporary = resolve_item_metadata(armor_id)
        permanent = resolve_item_metadata(permanent_id)
        if (not temporary.is_equipment or not permanent.is_equipment
                or temporary.minimum_level != 85 or permanent.minimum_level != 85
                or temporary.equipment_type != permanent.equipment_type or temporary.name != permanent.name
                or temporary.usable_period_days <= 0 or permanent.usable_period_days
                or permanent.expiration_timestamp or permanent.daily_delete):
            raise AttendanceError("invalid_equipment_pair")
        ok, _, consumed = repo.consume_inventory_item(cid, list_type=main, slot_index=voucher_slot, count=1)
        if not ok or consumed["removed"] != 1 or consumed["item_template_id"] != ARMOR_VOUCHER:
            raise AttendanceError("voucher_consume_failed")
        if conn.execute(
            """UPDATE character_items SET item_template_id=?, expire_time=0, updated_at=CURRENT_TIMESTAMP
               WHERE item_uid=? AND character_id=? AND list_type=? AND slot_index=? AND item_template_id=?""",
            (permanent_id, armor["item_uid"], cid, main, armor_slot, armor_id),
        ).rowcount != 1:
            raise AttendanceError("armor_replace_failed")
        armor_update = repo.get_inventory_item_at(cid, list_type=main, slot_index=armor_slot, now_unix=now)
        if not armor_update or armor_update["item_template_id"] != permanent_id or armor_update["expire_time"]:
            raise AttendanceError("armor_refresh_failed")
        return armor_id, permanent_id
