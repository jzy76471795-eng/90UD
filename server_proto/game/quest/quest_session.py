"""Quest session helpers (session/DB hydrate + list bodies + unlock seeds)."""
from __future__ import annotations

import os
from typing import Any, Collection, Mapping, Sequence

from game.content_blocking import activate_character_content_blocks
from game.quest.profession_completion_guard import filter_profession_completion_rows
from game.quest.initial_quests import get_acceptable_quest_ids_for_job
from game.character.payloads import build_acceptable_quest_list
from game.quest.daily_challenge import (
    list_today_claimed_daily_challenge_ids,
    list_today_daily_challenge_ids_by_type,
)
from game.quest.daily_challenge_index import get_daily_challenge_candidate_ids
from game.quest.quest_common import (
    active_pairs_from_rows,
    build_accepted_quest_list_023f,
    build_clear_quest_list_0164,
    build_quest_trigger_list_0123,
    build_set_quest_trigger_ack,
    env_bool,
    filter_acceptable_quest_ids,
)
from game.quest.quest_index import (
    compute_acceptable_quest_ids,
    compute_quest_clear_trigger,
    daily_quest_day_key,
    get_quest_index,
    is_daily_quest_meta,
    local_offline_unlock_quest_ids,
    peek_local_unlock_quest_ids,
    repair_worldmap_hunt_monster_trigger,
)


def list_today_daily_completion_ids(repo: Any, character_id: int) -> list[int]:
    """Read the daily ledger without breaking lightweight legacy test repos."""
    getter = getattr(repo, "list_daily_quest_completion_ids", None)
    if not callable(getter):
        return []
    try:
        values = getter(
            int(character_id),
            completed_on=daily_quest_day_key(),
        )
    except Exception:
        return []
    return [int(value) & 0xFFFF for value in (values or ()) if int(value) > 0]


def project_client_finished_quest_ids(
    finished_rows: Sequence[tuple[int, int]],
    *,
    daily_completed_ids: Sequence[int] = (),
) -> tuple[list[int], list[int]]:
    """Hide expired daily completions from the client's full clear-task list.

    The persisted finished flag remains until the task is accepted again, so
    it can still participate in server-side prerequisite evaluation.  The
    Stage3 client needs a fresh 0164 list without that old daily ID before it
    can present the task as available on the following server date.
    """
    completed_today = {
        int(value) & 0xFFFF for value in daily_completed_ids if int(value) > 0
    }
    try:
        quest_index = get_quest_index()
    except Exception:
        quest_index = None
    client_finished: list[int] = []
    expired_daily: list[int] = []
    for quest_id, flag_value in finished_rows or ():
        qid = int(quest_id) & 0xFFFF
        if qid <= 0 or int(flag_value) == 0:
            continue
        meta = None
        if quest_index is not None:
            try:
                meta = quest_index.get_meta(qid)
            except Exception:
                meta = None
            try:
                if quest_index.is_unsequenced_epic_quest(qid):
                    continue
            except Exception:
                pass
        if qid not in completed_today and is_daily_quest_meta(meta):
            expired_daily.append(qid)
            continue
        client_finished.append(qid)
    return client_finished, expired_daily


def project_client_finished_quest_snapshot(
    repo: Any,
    character_id: int,
) -> dict[str, object]:
    """Build the finished-quest view that may be sent to Stage3.

    The persistent finished table is not the complete client projection: a
    claimed Daily Challenge lives in its own ledger and must still be present
    in ``S0/0164`` for the current day.  Keeping this calculation in one
    helper prevents reject/repair paths from sending a shorter replacement
    bitmap than the normal hydrate path.
    """
    cid = int(character_id)
    finished_rows = filter_profession_completion_rows(repo,cid,repo.list_finished_quests(cid))
    daily_completed_ids = list_today_daily_completion_ids(repo, cid)
    client_finished_ids, expired_daily_finished_ids = (
        project_client_finished_quest_ids(
            finished_rows,
            daily_completed_ids=daily_completed_ids,
        )
    )
    daily_challenge_claimed_ids = list_today_claimed_daily_challenge_ids(repo, cid)
    client_finished_ids = list(
        dict.fromkeys(client_finished_ids + daily_challenge_claimed_ids)
    )
    today_clear_grade_ids = list_today_daily_challenge_ids_by_type(
        repo,
        cid,
        quest_type="clear quest by grade",
    )
    return {
        "finished_rows": list(finished_rows),
        "finished_ids": client_finished_ids,
        "daily_completed_ids": daily_completed_ids,
        "daily_challenge_claimed_ids": daily_challenge_claimed_ids,
        "expired_daily_finished_ids": expired_daily_finished_ids,
        "today_clear_grade_ids": today_clear_grade_ids,
    }


def pack_client_finished_quest_correction(
    server: Any,
    *,
    character_id: int,
    allow_empty: bool = False,
) -> tuple[list[bytes], str]:
    """Send a corrective ``S0/0164`` using the same projection as hydrate.

    ``S0/0164`` is a full replacement.  The shared projection therefore always
    includes today's claimed Daily Challenge IDs alongside persistent quest
    completions before a correction is emitted.
    """
    if not env_bool("USDOF_GAME_REPLY_FINISH_QUEST_SEND_0164", True):
        return [], "S0/0164 correction gate=OFF"

    return pack_authoritative_finished_quest_snapshot(
        server,
        character_id=int(character_id),
        allow_empty=allow_empty,
        context="correction",
    )


def pack_authoritative_finished_quest_snapshot(
    server: Any,
    *,
    character_id: int,
    allow_empty: bool = False,
    context: str = "snapshot",
) -> tuple[list[bytes], str]:
    """Pack the complete Stage3 completion projection without a feature gate."""
    data = project_client_finished_quest_snapshot(server._repo(), int(character_id))
    finished_ids = [
        int(value) & 0xFFFF
        for value in (data.get("finished_ids") or [])
        if int(value) > 0
    ]
    expired_daily_n = len(data.get("expired_daily_finished_ids") or [])
    if not finished_ids and not allow_empty:
        return [], "S0/0164 %s skip_empty reset_daily=%s" % (
            str(context),
            expired_daily_n,
        )
    packet = server._codec_response(
        0,
        0x0164,
        build_clear_quest_list_0164(finished_ids),
    )
    return [packet], (
        "S0/0164 %s finished=%s reset_daily=%s claimed_daily=%s"
        % (
            str(context),
            len(finished_ids),
            expired_daily_n,
            len(data.get("daily_challenge_claimed_ids") or []),
        )
    )


def resolve_selected_character(server: Any, session: Any = None) -> tuple[int | None, int | None, int | None]:
    detail = resolve_selected_character_detail(server, session)
    return detail.get("character_id"), detail.get("job"), detail.get("level")


def resolve_selected_character_detail(server: Any, session: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "character_id": None,
        "job": None,
        "level": None,
        "grow_type": None,
        "record": None,
    }
    cid = None
    if session is not None:
        cid = getattr(session, "selected_character_id", None)
    repo = server._repo()
    account_id = getattr(server, "account_id", 1)
    try:
        rows = repo.list_by_account(account_id)
    except Exception:
        if cid is not None:
            out["character_id"] = int(cid)
        return out
    rec = None
    if cid is not None:
        for row in rows:
            if int(row.character_id) == int(cid):
                rec = row
                break
        out["character_id"] = int(cid)
    elif rows:
        rec = rows[0]
        out["character_id"] = int(rec.character_id)
    if rec is not None:
        out["job"] = int(rec.job) & 0xFF
        out["level"] = int(rec.level) & 0xFF
        out["grow_type"] = int(getattr(rec, "grow_type", 0) or 0) & 0xFF
        out["record"] = rec
    return out


def clear_session_quest_scratch(session: Any = None) -> None:
    if session is None:
        return
    for name in (
        "active_quest_ids",
        "active_quest_pairs",
        "finished_quest_ids",
        "last_quest_id",
        "quest_connected",
        "quest_hydrated",
        "daily_challenge_clear_grade_event",
    ):
        if hasattr(session, name):
            if name in {"active_quest_ids", "active_quest_pairs", "finished_quest_ids"}:
                setattr(session, name, [])
            elif name == "quest_connected":
                setattr(session, name, False)
            elif name == "quest_hydrated":
                setattr(session, name, False)
            else:
                setattr(session, name, None)


def ensure_session_quest_fields(session: Any = None) -> None:
    if session is None:
        return
    if not hasattr(session, "active_quest_ids"):
        session.active_quest_ids = []
    if not hasattr(session, "active_quest_pairs"):
        session.active_quest_pairs = []
    if not hasattr(session, "finished_quest_ids"):
        session.finished_quest_ids = []
    if not hasattr(session, "last_quest_id"):
        session.last_quest_id = None
    if not hasattr(session, "quest_connected"):
        session.quest_connected = False
    if not hasattr(session, "quest_hydrated"):
        session.quest_hydrated = False
    if not hasattr(session, "daily_challenge_clear_grade_event"):
        session.daily_challenge_clear_grade_event = None


def ensure_local_map_unlock_quests(server: Any, *, character_id: int) -> dict[str, object]:
    """Mark install/local unlock quests finished in DB (idempotent)."""
    seeds = [int(x) & 0xFFFF for x in peek_local_unlock_quest_ids() if int(x) > 0]
    if not seeds or not env_bool("USDOF_GAME_QUEST_LOCAL_UNLOCK_ENABLE", True):
        return {"seeded": 0, "ids": [], "notes": "unlock_skip"}
    repo = server._repo()
    newly = []
    for qid in seeds:
        if repo.is_quest_finished(int(character_id), qid):
            continue
        ok, _detail = repo.mark_quest_finished(int(character_id), qid, flag_value=1)
        if ok:
            newly.append(qid)
    return {"seeded": len(newly), "ids": newly, "all_seeds": seeds, "notes": "unlock_ok"}


def bind_session_quest_state(
    session: Any,
    *,
    active_pairs: Sequence[tuple[int, int]] = (),
    finished_ids: Sequence[int] = (),
    last_quest_id: int | None = None,
) -> None:
    if session is None:
        return
    ensure_session_quest_fields(session)
    pairs = [(int(q) & 0xFFFF, int(t) & 0xFFFFFFFF) for q, t in (active_pairs or ())]
    session.active_quest_pairs = pairs
    session.active_quest_ids = [q for q, _t in pairs]
    session.finished_quest_ids = [int(x) & 0xFFFF for x in finished_ids if int(x) > 0]
    if last_quest_id is not None:
        session.last_quest_id = int(last_quest_id) & 0xFFFF
    session.quest_hydrated = True


def resolve_acceptable_ids(
    *,
    job: int,
    level: int,
    grow_type: int = 0,
    active_ids: Sequence[int] = (),
    finished_ids: Sequence[int] = (),
    finished_flags: Mapping[int, int] | None = None,
    daily_completed_ids: Sequence[int] = (),
) -> list[int]:
    """Prefer runtime PVF ComputeAcceptable.

    When PVF mode is on (default): any compute failure yields an empty list so
    001F cannot silently accept out-of-eligibility quests via static JSON.
    Static JSON is only for the explicit diagnostic path that turns PVF off.
    """
    if env_bool("USDOF_GAME_ACCEPTABLE_QUEST_FROM_PVF", True):
        try:
            return compute_acceptable_quest_ids(
                level=int(level),
                job=int(job),
                grow_type=int(grow_type),
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                active_ids=active_ids,
                daily_completed_ids=daily_completed_ids,
            )
        except Exception:
            # Fail closed: empty acceptable list → 001F gate rejects.
            return []
    # The diagnostic JSON is not authoritative for registration. Preserve the
    # same quest.lst gate as the PVF path; no registration snapshot means no
    # acceptable list.
    try:
        quest_index = get_quest_index()
        registered_ids = quest_index.registered_quest_ids()
    except Exception:
        return []
    candidates = list(get_acceptable_quest_ids_for_job(int(job)))
    acceptable = filter_acceptable_quest_ids(
        candidates,
        active_ids=active_ids,
        finished_ids=finished_ids,
        registered_ids=registered_ids,
    )
    daily_completed = {int(value) & 0xFFFF for value in daily_completed_ids if int(value) > 0}
    sequence_checker = getattr(
        quest_index,
        "is_unsequenced_epic_quest",
        None,
    )
    return [
        quest_id
        for quest_id in acceptable
        if int(quest_id) not in daily_completed
        and not (
            callable(sequence_checker)
            and sequence_checker(quest_id)
        )
    ]


def _discard_active_quest_ids(
    repo: Any,
    *,
    character_id: int,
    active_rows: Sequence[tuple[int, int, int]],
    invalid_ids: Collection[int],
) -> tuple[list[tuple[int, int, int]], list[int], list[str]]:
    if not invalid_ids:
        return list(active_rows), [], []
    remover = getattr(repo, "giveup_quest", None)
    kept: list[tuple[int, int, int]] = []
    removed: list[int] = []
    errors: list[str] = []
    for row in active_rows or ():
        quest_id = int(row[1]) & 0xFFFF
        if quest_id not in invalid_ids:
            kept.append(row)
            continue
        if not callable(remover):
            errors.append("quest_id=%s remover_missing" % quest_id)
            continue
        try:
            ok, note = remover(int(character_id), quest_id)
        except Exception as exc:
            errors.append("quest_id=%s %s: %s" % (quest_id, type(exc).__name__, exc))
            continue
        if ok:
            removed.append(quest_id)
        else:
            errors.append("quest_id=%s %s" % (quest_id, note))
    return kept, sorted(set(removed)), errors


def discard_daily_challenge_active_quests(
    repo: Any,
    *,
    character_id: int,
    active_rows: Sequence[tuple[int, int, int]],
) -> tuple[list[tuple[int, int, int]], list[int], list[str]]:
    """Remove ordinary active rows owned by the independent Daily ledger."""
    return _discard_active_quest_ids(
        repo,
        character_id=int(character_id),
        active_rows=active_rows,
        invalid_ids=get_daily_challenge_candidate_ids(),
    )


def discard_unsequenced_epic_active_quests(
    repo: Any,
    *,
    character_id: int,
    active_rows: Sequence[tuple[int, int, int]],
) -> tuple[list[tuple[int, int, int]], list[int], list[str]]:
    """Remove stale player-active EpicQuest rows omitted by episode chains."""
    quest_index = get_quest_index()
    invalid_getter = getattr(
        quest_index,
        "get_unsequenced_epic_quest_ids",
        None,
    )
    if not callable(invalid_getter):
        return list(active_rows), [], []
    return _discard_active_quest_ids(
        repo,
        character_id=int(character_id),
        active_rows=active_rows,
        invalid_ids=set(invalid_getter()),
    )


def hydrate_quest_lists(
    server: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    session: Any = None,
    ensure_unlock: bool = True,
) -> dict[str, object]:
    """Load active/finished, optional unlock seeds, build 0015/023F/0164 bodies."""
    repo = server._repo()
    activate_character_content_blocks(
        repo,
        int(character_id),
        session=session,
    )
    if ensure_unlock and env_bool("USDOF_GAME_QUEST_LOCAL_UNLOCK_ENABLE", True):
        unlock = ensure_local_map_unlock_quests(server, character_id=int(character_id))
    else:
        unlock = {"seeded": 0, "ids": [], "notes": "unlock_off"}

    trigger_repairs, trigger_repair_note = repair_worldmap_hunt_monster_triggers(
        repo,
        character_id=int(character_id),
    )
    active_rows = repo.list_active_quests(int(character_id))
    (
        active_rows,
        daily_challenge_active_removed_ids,
        daily_challenge_active_cleanup_errors,
    ) = discard_daily_challenge_active_quests(
        repo,
        character_id=int(character_id),
        active_rows=active_rows,
    )
    (
        active_rows,
        unsequenced_epic_removed_ids,
        unsequenced_epic_cleanup_errors,
    ) = discard_unsequenced_epic_active_quests(
        repo,
        character_id=int(character_id),
        active_rows=active_rows,
    )
    finished_projection = project_client_finished_quest_snapshot(
        repo,
        int(character_id),
    )
    finished_rows = list(finished_projection["finished_rows"])
    active_pairs = active_pairs_from_rows(active_rows)
    finished_ids = [qid for qid, _flag in finished_rows]
    finished_flags = {
        int(quest_id) & 0xFFFF: int(flag) & 0xFF
        for quest_id, flag in finished_rows
        if int(flag) != 0
    }
    active_ids = [qid for qid, _t in active_pairs]
    daily_completed_ids = list(finished_projection["daily_completed_ids"])
    client_finished_ids = list(finished_projection["finished_ids"])
    expired_daily_finished_ids = list(
        finished_projection["expired_daily_finished_ids"]
    )
    daily_challenge_claimed_ids = list(
        finished_projection["daily_challenge_claimed_ids"]
    )
    acceptable = resolve_acceptable_ids(
        job=int(job),
        level=max(1, int(level)),
        grow_type=int(grow_type or 0),
        active_ids=active_ids,
        finished_ids=finished_ids,
        finished_flags=finished_flags,
        daily_completed_ids=daily_completed_ids,
    )
    body_0015 = build_acceptable_quest_list(level=max(1, int(level)), quest_ids=acceptable)
    body_023f = build_accepted_quest_list_023f(active_pairs)
    body_0123 = build_quest_trigger_list_0123(active_pairs)
    body_0164 = build_clear_quest_list_0164(client_finished_ids)
    bind_session_quest_state(
        session,
        active_pairs=active_pairs,
        finished_ids=client_finished_ids,
    )
    return {
        "active_pairs": active_pairs,
        "finished_ids": client_finished_ids,
        "persistent_finished_ids": finished_ids,
        "daily_completed_ids": daily_completed_ids,
        "daily_challenge_claimed_ids": daily_challenge_claimed_ids,
        "daily_challenge_active_removed_ids": daily_challenge_active_removed_ids,
        "daily_challenge_active_cleanup_errors": daily_challenge_active_cleanup_errors,
        "today_clear_grade_ids": list(finished_projection["today_clear_grade_ids"]),
        "expired_daily_finished_ids": expired_daily_finished_ids,
        "unsequenced_epic_removed_ids": unsequenced_epic_removed_ids,
        "unsequenced_epic_cleanup_errors": unsequenced_epic_cleanup_errors,
        "acceptable_ids": acceptable,
        "body_0015": body_0015,
        "body_023f": body_023f,
        "body_0123": body_0123,
        "body_0164": body_0164,
        "unlock": unlock,
        "trigger_repairs": trigger_repairs,
        "trigger_repair_note": trigger_repair_note,
    }


def pack_set_trigger_ack_packets(
    server: Any,
    pairs: Sequence[tuple[int, int]],
) -> tuple[list[bytes], str]:
    """S1/0x0021 success ACK 列表（Stage3 ``sub_160E080`` → ``sub_137FC30``）。

    IDA（2026-07-18）结论：
      - 单任务 trigger 更新走 S1/0021，**不是** 仅靠 S0/023F
      - ``sub_137FC30`` 在 **new_trigger==0** 时调用 ``sub_10434A0(quest, 1)``
        + ``sub_1B7E900(1)``，驱动可交态图标闪烁/动效
      - S0/023F 只 ``sub_1394650`` 重建列表 + ``sub_1B7E900``，**不会**走
        ``sub_10434A0(..., 1)`` / ``sub_159F9F0``（右方/底部 HUD 动效）
      - 023F 若紧跟在 0021 后全量 clear+rebuild，可能冲掉刚点亮的动效

    因此 residual 完成 / init_trigger=0 接任务 / 进房补可交态，都应发 S1/0021。
    进房时还须：
      1) S1/0004 ``quest_display_ids`` 先挂右侧 tracker（sub_1384900→1384700）；
      2) 等城镇 UI 就绪后再 pulse S1/0021（首次 C1/0023）。
    仅 023F 只点亮 F2；无 display_ids 时 0021 的 sub_159F9F0 刷新空列表。
    """
    packets: list[bytes] = []
    for qid, trig in pairs or ():
        q = int(qid) & 0xFFFF
        if q <= 0:
            continue
        body = build_set_quest_trigger_ack(
            success=True,
            quest_id=q,
            trigger_value=int(trig) & 0xFFFFFFFF,
        )
        packets.append(server._codec_response(1, 0x0021, body))
    if not packets:
        return [], "S1/0021 none"
    return packets, "S1/0021 n=%s" % len(packets)


def sync_quest_clear_dependency_triggers(
    repo: Any,
    *,
    character_id: int,
) -> tuple[list[tuple[int, int, int]], str]:
    """Recompute active ``[quest clear]`` residuals from finished DB flags."""
    cid = int(character_id)
    if cid <= 0:
        return [], "quest_dependency skip=cid0"
    finished_ids = tuple(
        int(quest_id) & 0xFFFF
        for quest_id, flag in repo.list_finished_quests(cid)
        if int(flag) != 0 and int(quest_id) > 0
    )
    changed: list[tuple[int, int, int]] = []
    for _slot, raw_quest_id, raw_trigger in repo.list_active_quests(cid):
        quest_id = int(raw_quest_id) & 0xFFFF
        old = int(raw_trigger) & 0xFFFFFFFF
        current = compute_quest_clear_trigger(quest_id, finished_ids)
        if current is None:
            continue
        new = int(current) & 0xFFFFFFFF
        if new == old:
            continue
        ok, _detail = repo.update_active_quest_trigger(cid, quest_id, new)
        if ok:
            changed.append((quest_id, old, new))
    return changed, "quest_dependency changed=%s finished=%s" % (
        len(changed),
        len(finished_ids),
    )


def repair_worldmap_hunt_monster_triggers(
    repo: Any,
    *,
    character_id: int,
) -> tuple[list[tuple[int, int, int]], str]:
    """Persist only malformed regional hunt residual channels during hydrate."""
    cid = int(character_id)
    if cid <= 0:
        return [], "quest_hunt_monster_repair skip=cid0"
    try:
        index = get_quest_index()
    except Exception as exc:
        return [], "quest_hunt_monster_repair skip=index:%r" % (exc,)

    changed: list[tuple[int, int, int]] = []
    for _slot, raw_quest_id, raw_trigger in repo.list_active_quests(cid):
        quest_id = int(raw_quest_id) & 0xFFFF
        old = int(raw_trigger) & 0xFFFFFFFF
        try:
            new = repair_worldmap_hunt_monster_trigger(
                index.get_meta(quest_id),
                old,
            )
        except Exception:
            continue
        if new == old:
            continue
        ok, _detail = repo.update_active_quest_trigger(cid, quest_id, new)
        if ok:
            changed.append((quest_id, old, new))
    return changed, "quest_hunt_monster_repair changed=%s" % len(changed)


def pack_completeable_trigger_acks(
    server: Any,
    pairs: Sequence[tuple[int, int]],
    *,
    pulse: bool | None = None,
) -> tuple[list[bytes], str]:
    """仅对 trigger==0 的已接任务发 S1/0021，用于进房/接任务后补可交闪烁。

    ``pulse=True``（默认 env ``USDOF_GAME_QUEST_COMPLETEABLE_0021_PULSE``）：
      先 S1/0021(trigger=1) 再 S1/0021(trigger=0)。
      023F 已把 trigger 写成 0 时，单发 0021(0) 可能走 field417 early-out，
      跳过 ``sub_10434A0(qid,1)``；1→0 强制走可交转换，点亮右方/底部 HUD 动效。
      仅客户端 ACK，不改 DB（DB 仍为 0）。
    """
    zero_qids = [
        int(q) & 0xFFFF
        for q, t in (pairs or ())
        if (int(q) & 0xFFFF) > 0 and (int(t) & 0xFFFFFFFF) == 0
    ]
    if not zero_qids:
        return [], "S1/0021 completeable=0"
    if pulse is None:
        pulse = env_bool("USDOF_GAME_QUEST_COMPLETEABLE_0021_PULSE", True)
    if pulse:
        packets: list[bytes] = []
        for q in zero_qids:
            for trig in (1, 0):
                body = build_set_quest_trigger_ack(
                    success=True,
                    quest_id=q,
                    trigger_value=int(trig) & 0xFFFFFFFF,
                )
                packets.append(server._codec_response(1, 0x0021, body))
        return packets, "S1/0021 pulse1to0 n=%s" % len(zero_qids)
    return pack_set_trigger_ack_packets(server, [(q, 0) for q in zero_qids])


def arm_completeable_hud_blink(
    server: Any,
    *,
    character_id: int,
    session: Any = None,
    source: str = "arm",
) -> tuple[list[bytes], str]:
    """读 DB 可交任务，发 S1/0021（默认 pulse）驱动右方/底部 HUD 闪烁。"""
    if int(character_id) <= 0:
        return [], "%s S1/0021 completeable skip cid=0" % source
    try:
        rows = server._repo().list_active_quests(int(character_id))
    except Exception as exc:
        return [], "%s S1/0021 completeable db_fail err=%r" % (source, exc)
    pairs = active_pairs_from_rows(rows)
    if session is not None:
        try:
            repo = server._repo()
            finished_rows = filter_profession_completion_rows(repo,int(character_id),repo.list_finished_quests(int(character_id)))
            finished, _expired_daily = project_client_finished_quest_ids(
                finished_rows,
                daily_completed_ids=list_today_daily_completion_ids(
                    repo,
                    int(character_id),
                ),
            )
            finished = list(
                dict.fromkeys(
                    finished
                    + list_today_claimed_daily_challenge_ids(
                        repo, int(character_id)
                    )
                )
            )
        except Exception:
            finished = list(getattr(session, "finished_quest_ids", None) or [])
        bind_session_quest_state(
            session,
            active_pairs=pairs,
            finished_ids=finished,
        )
    packets, note = pack_completeable_trigger_acks(server, pairs)
    return packets, "%s %s" % (source, note)


def pack_quest_refresh_packets(
    server: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    session: Any = None,
    include_0015: bool = True,
    include_023f: bool = True,
    include_0123: bool | None = None,
    include_0164: bool = False,
    ensure_unlock: bool = False,
    allow_empty_0164: bool = False,
    exclude_finished_quest_ids: Collection[int] = (),
) -> tuple[list[bytes], str]:
    data = hydrate_quest_lists(
        server,
        character_id=character_id,
        job=job,
        level=level,
        grow_type=grow_type,
        session=session,
        ensure_unlock=ensure_unlock,
    )
    packets: list[bytes] = []
    notes: list[str] = []
    if include_023f and env_bool("USDOF_GAME_REPLY_ACCEPTED_QUEST_023F", True):
        packets.append(server._codec_response(0, 0x023F, data["body_023f"]))
        notes.append("S0/023F n=%s" % len(data["active_pairs"]))
    # 0123 与 023F 条目相同但 count=u16；默认 env OFF（crash-safe / 思路.md）
    send_0123 = (
        env_bool("USDOF_GAME_REPLY_QUEST_TRIGGER_LIST_0123", False)
        if include_0123 is None
        else bool(include_0123)
    )
    if send_0123:
        packets.append(server._codec_response(0, 0x0123, data["body_0123"]))
        notes.append("S0/0123 n=%s" % len(data["active_pairs"]))
    if include_0015:
        packets.append(server._codec_response(0, 0x0015, data["body_0015"]))
        notes.append("S0/0015 n=%s" % len(data["acceptable_ids"]))
    expired_daily_n = len(data.get("expired_daily_finished_ids") or [])
    requested_0164 = bool(include_0164 or expired_daily_n > 0)
    if requested_0164:
        excluded_finished_ids = {
            int(value) & 0xFFFF
            for value in exclude_finished_quest_ids
            if int(value) > 0
        }
        reported_finished_ids = [
            int(value) & 0xFFFF
            for value in (data["finished_ids"] or [])
            if int(value) > 0 and (int(value) & 0xFFFF) not in excluded_finished_ids
        ]
        finished_n = len(reported_finished_ids)
        # 空 0164 默认 OFF（思路.md empty finish/clear 0164）；allow_empty 覆盖
        if finished_n > 0 or expired_daily_n > 0 or allow_empty_0164 or env_bool(
            "USDOF_GAME_REPLY_FINISH_QUEST_CLEAR_0164", False
        ):
            body_0164 = (
                data["body_0164"]
                if not excluded_finished_ids
                else build_clear_quest_list_0164(reported_finished_ids)
            )
            packets.append(server._codec_response(0, 0x0164, body_0164))
            notes.append(
                "S0/0164 finished=%s reset_daily=%s claimed_daily=%s excluded=%s"
                % (
                    finished_n,
                    expired_daily_n,
                    len(data.get("daily_challenge_claimed_ids") or []),
                    len(excluded_finished_ids),
                )
            )
        else:
            notes.append("S0/0164 skip_empty")
    if data.get("unlock"):
        notes.append("unlock=%s" % data["unlock"].get("notes"))
    if data.get("trigger_repairs"):
        notes.append(str(data.get("trigger_repair_note") or "quest_hunt_monster_repair"))
    if data.get("daily_challenge_active_removed_ids"):
        notes.append(
            "daily_active_cleanup removed=%s"
            % ",".join(
                str(value)
                for value in data["daily_challenge_active_removed_ids"]
            )
        )
    if data.get("daily_challenge_active_cleanup_errors"):
        notes.append(
            "daily_active_cleanup errors=%s"
            % "|".join(data["daily_challenge_active_cleanup_errors"])
        )
    return packets, ";".join(notes)


def pack_levelup_acceptable_quest_refresh(
    server: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    session: Any = None,
) -> tuple[list[bytes], str]:
    """Rebuild only the acceptable-quest UI after a persisted level-up.

    Stage3 consumes S0/0015 through ``sub_1650580`` as a full replacement of
    the acceptable list.  Level-gain paths do not change active or finished
    quest state, so they deliberately do not repeat 023F/0164 here.
    """
    data = hydrate_quest_lists(
        server,
        character_id=int(character_id),
        job=int(job),
        level=max(1, int(level)),
        grow_type=int(grow_type or 0),
        session=session,
        ensure_unlock=False,
    )
    packet = server._codec_response(0, 0x0015, data["body_0015"])
    return [packet], "S0/0015 n=%s level=%s" % (
        len(data["acceptable_ids"]),
        max(1, int(level)),
    )
