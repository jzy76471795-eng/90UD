"""Apply the supplied CN adventure configuration to authenticated game events."""
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import accumulate
import os
from time import time
from uuid import uuid4

from game.mail_store import ITEM_ID_BASE
from game.native_mailbox import identity, metadata, store_for
from game.quest.adventure_cn_data import (
    BASIC_REWARDS, CONFIG_SHA256, DAILY, DAILY_ADVANCED_RATIO, LEVEL_XP, MISSIONS,
    RIFT_DUNGEON_ID, SEASON, SERVER_PVF_SHA256, WEEKLY,
)


START_ENV = "USDOF_GAME_ADVENTURE_CN_START"
CHINA_TIME = timezone(timedelta(hours=8))
DAY_SECONDS = 86400
SEASON_SECONDS = len(DAILY) * DAY_SECONDS
TOTALS = tuple(accumulate(LEVEL_XP[:-1]))
MAX_LEVEL = len(LEVEL_XP)
SPECIAL_DUNGEON_CLEAR_EVENTS = {
    RIFT_DUNGEON_ID: "magic_rift_clear",
    5097: "aiken_clear",  # 艾肯 - 遗落的神殿 in both paired PVFs
    7100: "dimensional_rift_clear",  # dungeon/impossible/dimension.dgn in both PVFs
}
EVENTS = {
    "defeat_monster": (1, -1),
    "defeat_boss": (2, -1),
    "gold_card_flip": (22, 1),
    "recommended_clear": (0, -1),
    "no_revive_clear": (5, -1),
    "magic_rift_clear": (24, -1),
    "login_day": (30, -1),
    "enter_guild_hall": (10, 8),
    "daily_completed": (36, 0),
    "weekly_completed": (36, 1),
    "enhance_attempt": (16, -1),
    "amp_attempt": (38, -1),
    "enhance_10": (39, 10),
    "amp_7": (40, 7),
    "emblem_combine": (19, -1),
    "splendid_emblem": (20, 2),
    "brilliant_emblem": (20, 3),
    "avatar_combine": (41, -1),
    "avatar_disassemble": (42, -1),
    "expedition_start": (12, -1),
    "quest_complete": (13, -1),
    "disjoint_equipment": (17, -1),
    "fatigue_spent": (3, -1),
    "enchant_equipment": (27, -1),
    "despair_floor_clear": (7, -1),
    "grief_tower_clear": (45, -1),
    "hell_party_clear": (23, -1),
    "aiken_clear": (8, -1),
    "dimensional_rift_clear": (44, -1),
    "raid_clear": (32, -1),
    "luke_raid_clear": (34, -1),
    "glory_crystals_earned": (29, -1),
}
WIRED = frozenset(EVENTS.values())
AVATAR_EVENTS = frozenset(("avatar_combine", "avatar_disassemble"))
SETTLED_GAME_ACTIONS = frozenset((
    "expedition_start", "quest_complete", "disjoint_equipment", "fatigue_spent", "enchant_equipment",
    "glory_crystals_earned",
))
EMBLEM_UPGRADES = {(1, 2): "splendid_emblem", (2, 3): "brilliant_emblem"}


@lru_cache(maxsize=8)
def _parse_start(value: str) -> int | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError(f"{START_ENV} must include a UTC offset")
    local = moment.astimezone(CHINA_TIME)
    if (local.hour, local.minute, local.second, local.microsecond) != (6, 0, 0, 0):
        raise ValueError(f"{START_ENV} must start at 06:00 in China")
    return int(moment.timestamp())


def active_start(now: float) -> int | None:
    start = _parse_start(os.environ.get(START_ENV, "").strip())
    return start if start is not None and start <= now < start + SEASON_SECONDS else None


def _optional_start(now: float) -> int | None:
    try:
        return active_start(now)
    except ValueError:
        return None


def _rotation(start: int, now: float):
    day = int((now - start) // DAY_SECONDS)
    first_thursday = (3 - datetime.fromtimestamp(start, CHINA_TIME).weekday()) % 7 or 7
    week = 0 if day < first_thursday else 1 + (day - first_thursday) // 7
    return {
        "daily": (day, DAILY[day]),
        "weekly": (week, WEEKLY[week] if week < len(WEEKLY) else ()),
        "season": (0, SEASON),
    }


def _ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adventure_cn_events (
           activity_start INTEGER NOT NULL,
           account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
           period TEXT NOT NULL, bucket INTEGER NOT NULL, mission_id INTEGER NOT NULL, event_key TEXT NOT NULL,
           PRIMARY KEY(activity_start, account_id, period, bucket, mission_id, event_key))""",
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adventure_cn_mission_receipts (
           activity_start INTEGER NOT NULL,
           account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
           period TEXT NOT NULL, bucket INTEGER NOT NULL, mission_id INTEGER NOT NULL, xp INTEGER NOT NULL,
           PRIMARY KEY(activity_start, account_id, period, bucket, mission_id))""",
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adventure_cn_basic_receipts (
           activity_start INTEGER NOT NULL,
           account_id INTEGER NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
           reward_level INTEGER NOT NULL, mail_id INTEGER NOT NULL,
           PRIMARY KEY(activity_start, account_id, reward_level))""",
    )


def _count(conn, scope) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM adventure_cn_events
           WHERE activity_start=? AND account_id=? AND period=? AND bucket=? AND mission_id=?""", scope,
    ).fetchone()[0]


def _record(conn, start: int, now: float, account_id: int, event: str, event_key: str) -> bool:
    signature = EVENTS[event]
    matched = False
    _ensure_schema(conn)
    for period, (bucket, codes) in _rotation(start, now).items():
        # The fourth daily slot requires the donor's advanced entitlement.
        if period == "daily":
            codes = codes[:3]
        for code in codes:
            _, target, _, kind, qualifier = MISSIONS[code]
            if (kind, qualifier) != signature:
                continue
            scope = (start, account_id, period, bucket, code)
            if _count(conn, scope) < target:
                inserted = conn.execute(
                    """INSERT INTO adventure_cn_events VALUES (?,?,?,?,?,?)
                       ON CONFLICT(activity_start, account_id, period, bucket, mission_id, event_key) DO NOTHING""",
                    (*scope, event_key),
                ).rowcount
                matched = matched or inserted == 1
    return matched


def record_login(repo, account_id: int, *, now: float | None = None) -> bool:
    now = time() if now is None else now
    start = _optional_start(now)
    if start is None:
        return False
    day = int((now - start) // DAY_SECONDS)
    with repo.inventory_transaction_scope() as conn:
        return _record(conn, start, now, account_id, "login_day", str(day))


def record_guild_hall_entry(server, session) -> bool:
    now = time()
    start = _optional_start(now)
    if start is None:
        return False
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        return _record(conn, start, now, session.account_id, "enter_guild_hall", "entered")


def begin_run(session, *, recommended: bool, raid: bool, now: float | None = None) -> None:
    session.adventure_season1_run = None
    if raid:
        return
    start = _optional_start(time() if now is None else now)
    dungeon_id = int(getattr(session, "selected_dungeon_id", 0) or 0)
    if start is not None and dungeon_id > 0:
        session.adventure_season1_run = (uuid4().hex, dungeon_id, start, recommended, False)


def note_revive(session) -> None:
    run = getattr(session, "adventure_season1_run", None)
    if run:
        session.adventure_season1_run = (*run[:4], True)


def _current_run(session, now: float):
    run = getattr(session, "adventure_season1_run", None)
    if (not run or not getattr(session, "in_dungeon_run", False)
            or run[1] != session.selected_dungeon_id or run[2] != _optional_start(now)):
        return None
    return run


def record_monster_defeat(
    server, session, *, sequence: int, boss: bool, shared_key: str | None, now: float | None = None,
) -> bool:
    now = time() if now is None else now
    run = _current_run(session, now)
    if run is None or session.dungeon_clear_phase >= 2 or not 1 <= sequence <= 0xfffe:
        return False
    run_id, _, start, _, _ = run
    codes = _rotation(start, now)["daily"][1][:3]
    signatures = {MISSIONS[code][3:] for code in codes}
    candidates = ("defeat_monster", "defeat_boss") if boss else ("defeat_monster",)
    events = [event for event in candidates if EVENTS[event] in signatures]
    if not events:
        return False
    if shared_key:
        key = f"party:{shared_key}"
    else:
        room = (session.dungeon_maze_index, session.dungeon_cell_x, session.dungeon_cell_y, session.dungeon_map_index)
        if any(type(value) is not int or value < 0 for value in room):
            return False
        key = ":".join(map(str, (run_id, *room, sequence)))
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        recorded = [_record(conn, start, now, session.account_id, event, key) for event in events]
    return any(recorded)


def record_golden_card(server, session, *, now: float | None = None) -> bool:
    now = time() if now is None else now
    run = _current_run(session, now)
    if run is None or session.dungeon_clear_phase < 2:
        return False
    run_id, _, start, _, _ = run
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        return _record(conn, start, now, session.account_id, "gold_card_flip", run_id)


def record_clear(
    server, session, *, now: float | None = None,
    despair_floor_cleared: bool = False, grief_tower_cleared: bool = False,
    hell_party_cleared: bool = False,
) -> bool:
    now = time() if now is None else now
    run = _current_run(session, now)
    if run is None or session.dungeon_clear_phase != 1:
        return False
    run_id, dungeon_id, start, recommended, revived = run
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        eligible = []
        if recommended:
            eligible.append("recommended_clear")
        if not revived:
            eligible.append("no_revive_clear")
        special_event = SPECIAL_DUNGEON_CLEAR_EVENTS.get(dungeon_id)
        if special_event is not None:
            eligible.append(special_event)
        if despair_floor_cleared:
            eligible.append("despair_floor_clear")
        if grief_tower_cleared:
            eligible.append("grief_tower_clear")
        if hell_party_cleared:
            eligible.append("hell_party_clear")
        changes = [_record(conn, start, now, session.account_id, event, run_id) for event in eligible]
        return any(changes)


def record_raid_clear(conn, account_id: int, family: str, instance_id: str, completed_at: float) -> None:
    start = _optional_start(completed_at)
    if start is None:
        return
    _record(conn, start, completed_at, account_id, "raid_clear", instance_id)
    if family == "luke":
        _record(conn, start, completed_at, account_id, "luke_raid_clear", instance_id)


def record_avatar_action(
    conn, account_id: int, objective: str, consumed_item_uid: int, now: float | None = None,
) -> bool:
    if objective not in AVATAR_EVENTS or consumed_item_uid <= 0 or not conn.in_transaction:
        return False
    now = time() if now is None else now
    start = _optional_start(now)
    return start is not None and _record(conn, start, now, account_id, objective, str(consumed_item_uid))


def record_emblem_compound(conn, account_id: int, source_grade: int, result_grades) -> None:
    now = time()
    start = _optional_start(now)
    if start is None:
        return
    operation = uuid4().hex
    _record(conn, start, now, account_id, "emblem_combine", operation)
    for grade in set(result_grades):
        objective = EMBLEM_UPGRADES.get((source_grade, grade))
        if objective is not None:
            _record(conn, start, now, account_id, objective, operation)


def record_equipment_upgrade(conn, account_id: int, *, amplified: bool, successful_level: int) -> None:
    now = time()
    start = _optional_start(now)
    if start is None:
        return
    operation = uuid4().hex
    _record(conn, start, now, account_id, "amp_attempt" if amplified else "enhance_attempt", operation)
    if amplified and successful_level == 7:
        _record(conn, start, now, account_id, "amp_7", operation)
    elif not amplified and successful_level == 10:
        _record(conn, start, now, account_id, "enhance_10", operation)


def record_game_actions(conn, account_id: int, objective: str, keys, *, now: float | None = None) -> None:
    """Count successful game actions in the same transaction as their settlement."""
    if objective not in SETTLED_GAME_ACTIONS or not conn.in_transaction:
        return
    now = time() if now is None else now
    start = _optional_start(now)
    if start is not None:
        for key in keys:
            _record(conn, start, now, account_id, objective, str(key))


def complete_mission(server, session, period: str, bucket: int, mission_id: int, *, now: float | None = None) -> bool:
    if not isinstance(period, str) or type(bucket) is not int or type(mission_id) is not int:
        raise ValueError("探险记任务编号无效。")
    now = time() if now is None else now
    start = active_start(now)
    if start is None:
        raise ValueError("探险记任务当前未开放。")
    rotation = _rotation(start, now).get(period)
    if rotation is None or rotation[0] != bucket or mission_id not in rotation[1]:
        raise ValueError("探险记任务已刷新，请重新打开。")
    if period == "daily" and mission_id == rotation[1][3]:
        raise ValueError("该任务需要高级探险家资格。")
    _, target, xp, kind, qualifier = MISSIONS[mission_id]
    if (kind, qualifier) not in WIRED:
        raise ValueError("该探险记任务尚未接入。")
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        _ensure_schema(conn)
        aid = session.account_id
        scope = (start, aid, period, bucket, mission_id)
        if _count(conn, scope) < target:
            raise ValueError("探险记任务进度尚未达标。")
        added = conn.execute(
            """INSERT INTO adventure_cn_mission_receipts VALUES (?,?,?,?,?,?)
               ON CONFLICT(activity_start, account_id, period, bucket, mission_id) DO NOTHING""",
            (*scope, xp),
        ).rowcount == 1
        if added and period in ("daily", "weekly"):
            _record(conn, start, now, aid, f"{period}_completed", f"{bucket}:{mission_id}")
        return added


def _xp_and_level(conn, start: int, account_id: int) -> tuple[int, int]:
    earned, = conn.execute(
        "SELECT COALESCE(SUM(xp), 0) FROM adventure_cn_mission_receipts WHERE activity_start=? AND account_id=?",
        (start, account_id),
    ).fetchone()
    xp = min(TOTALS[-1], earned)
    return xp, bisect_right(TOTALS, xp) + 1


def status(server, session, *, now: float | None = None) -> dict | None:
    now = time() if now is None else now
    start = active_start(now)
    if start is None:
        return None
    with server._repo().inventory_transaction_scope() as conn:
        identity(server, session, conn)
        _ensure_schema(conn)
        aid = session.account_id
        xp, level = _xp_and_level(conn, start, aid)
        scope = (start, aid)
        completed = {
            tuple(row) for row in conn.execute(
                "SELECT period,bucket,mission_id FROM adventure_cn_mission_receipts WHERE activity_start=? AND account_id=?",
                scope,
            )
        }
        missions = {}
        for period, (bucket, codes) in _rotation(start, now).items():
            rows = []
            for index, code in enumerate(codes):
                name, target, mission_xp, kind, qualifier = MISSIONS[code]
                advanced = period == "daily" and index == 3
                wired = (kind, qualifier) in WIRED
                current = _count(conn, (*scope, period, bucket, code))
                claimed = (period, bucket, code) in completed
                rows.append({
                    "mission_id": code, "bucket": bucket, "name": name, "current": current, "target": target,
                    "advanced": advanced, "wired": wired, "claimed": claimed,
                    "xp": mission_xp * (DAILY_ADVANCED_RATIO if advanced else 1),
                    "can_complete": wired and not advanced and not claimed and current >= target,
                })
            missions[period] = rows
        claimed_levels = _claimed_levels(conn, start, aid)
        compatible = _reward_profile_matches()
        basic_rewards = [
            {"level": reward_level, "items": bundle, "claimed": reward_level in claimed_levels,
             "can_claim": compatible and reward_level <= level and reward_level not in claimed_levels}
            for reward_level, bundle in sorted(BASIC_REWARDS.items())
        ]
        end = start + SEASON_SECONDS
        next_daily = min(end, start + (int((now - start) // DAY_SECONDS) + 1) * DAY_SECONDS)
        next_weekly = next_daily + (3 - datetime.fromtimestamp(next_daily, CHINA_TIME).weekday()) % 7 * DAY_SECONDS
        level_floor = TOTALS[level - 2] if level > 1 else 0
        return {
            "catalog": CONFIG_SHA256, "activity_start": start, "activity_end": end,
            "next_daily": next_daily, "next_weekly": min(end, next_weekly),
            "level": level, "xp": xp, "level_xp": xp - level_floor,
            "xp_for_next_level": LEVEL_XP[level - 1] if level < MAX_LEVEL else None,
            "missions": missions, "basic_rewards": basic_rewards, "advanced_entitled": False,
        }


def _reward_profile_matches() -> bool:
    from pvf.pvf_runtime import source_sha256
    try:
        return source_sha256().lower() == SERVER_PVF_SHA256
    except RuntimeError:
        return False


def _claimed_levels(conn, start, aid):
    return {row[0] for row in conn.execute(
        "SELECT reward_level FROM adventure_cn_basic_receipts WHERE activity_start=? AND account_id=?", (start, aid),
    )}


def _claim_basic_rewards(server, session, level: int | None, *, now: float | None = None) -> list[int]:
    if level is not None and (type(level) is not int or not 1 <= level <= MAX_LEVEL):
        raise ValueError("奖励等级无效。")
    start = active_start(time() if now is None else now)
    if start is None:
        raise ValueError("探险记奖励当前未开放。")
    if (level is not None and level not in BASIC_REWARDS) or not _reward_profile_matches():
        raise ValueError("该等级的完整 CN 奖励尚未通过当前 PVF 核对。")
    store = store_for(server)
    with server._repo().inventory_transaction_scope() as conn:
        cid = identity(server, session, conn)
        aid = session.account_id
        _ensure_schema(conn)
        current_level = _xp_and_level(conn, start, aid)[1]
        if level is not None and current_level < level:
            raise ValueError("当前探险记等级不足。")
        previous = _claimed_levels(conn, start, aid)
        candidates = (level,) if level is not None else sorted(BASIC_REWARDS)
        delivered = []
        for reward_level in candidates:
            if reward_level > current_level or reward_level in previous:
                continue
            mid = store.send_mail(
                {"characterId": cid, "title": "阿拉德探险记", "body": f"Lv.{reward_level} 初级探险家奖励。"}, conn,
            )
            if mid >= ITEM_ID_BASE:
                raise ValueError("奖励邮件编号超出客户端范围。")
            for position, (item_id, quantity) in enumerate(BASIC_REWARDS[reward_level]):
                attachment = conn.execute(
                    "INSERT INTO gmkit_mail_items(mail_id,position,item_id,quantity,item_name) VALUES(?,?,?,?,?)",
                    (mid, position, item_id, quantity, metadata(item_id).name),
                ).lastrowid
                if attachment >= ITEM_ID_BASE:
                    raise ValueError("奖励附件编号超出客户端范围。")
            conn.execute("INSERT INTO adventure_cn_basic_receipts VALUES (?,?,?,?)", (start, aid, reward_level, mid))
            delivered.append(reward_level)
        return delivered


def claim_basic_reward(server, session, level: int, *, now: float | None = None) -> bool:
    if type(level) is not int:
        raise ValueError("奖励等级无效。")
    return bool(_claim_basic_rewards(server, session, level, now=now))


def claim_all_basic_rewards(server, session, *, now: float | None = None) -> list[int]:
    return _claim_basic_rewards(server, session, None, now=now)
