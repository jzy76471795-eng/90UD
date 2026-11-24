# -*- coding: utf-8 -*-
"""新角色首进教程本 — Phase-T1（角色就绪后合法进本）。

硬规则（见 analysis_output/newbie_tutorial_safe_enter_ida_90qiyuan_20260719.md）：

  - Stage3 ``S1/0004`` START **永远城镇向**，不能靠 START 直接首场景副本。
  - **禁止** 在 START delayed 里与城镇 paint（0016/0017/0018）同批叠 ``001C+001D``。
  - 安全路径（2026-07-19 实测可进本；2026-07-19 优化）：
      Phase-T0（SELECT/START）：START + 角色必要包（0131/USERINFO/…）+ arm
        · **跳过** 赛丽亚房间 paint：0016/0017/0018（不先展示房间）
        · 回城坐标预置为银色村庄赛丽亚（town gate area0）
      Phase-T1：完整 000F 选本态 → 0010 grant 001C+001D
        · 默认挂在 START delayed 末尾（角色就绪后，不经房间）
        · 0023 仅作 fallback（若仍 armed 且未 done）
        · 魔枪士例外：先完成城镇 paint，收到 C1/0023 后再进入 10005。
          0131 仅确认角色对象，不能作为城镇场景加载完成的依据。

Env：
  USDOF_GAME_NEWBIE_FIRST_ENTER=1
  USDOF_GAME_TUTORIAL_AFTER_0023=1          Phase-T1 总开关（名历史遗留；不强制等 0023）
  USDOF_GAME_TUTORIAL_SKIP_SERIA_ROOM=1     跳过 0016/0017/0018（默认开）
  USDOF_GAME_TUTORIAL_GRANT_AFTER_SETUP=1   START delayed 末尾直接 Phase-T1（默认开）
  USDOF_GAME_TUTORIAL_PHASE_T1_DELAY_MS=0   日志预留
  USDOF_GAME_TUTORIAL_DUNGEON_GRANT=0       旧 hybrid；日常关

产品语义：每角色最多一次；grant 成功后立刻 tutorial_state=2。
离开教程本：强制回当前城镇赛丽亚房间（非副本门口）。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.town_cera_rooms import get_cera_room_for_town

_tut = load_chain("helper-tutorial_dungeon.py")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def phase_t1_enabled() -> bool:
    """是否启用 Phase-T1（与 hybrid GRANT 分离）。"""
    return _env_bool("USDOF_GAME_TUTORIAL_AFTER_0023", False)


def skip_seria_room_paint() -> bool:
    """教程首进时是否跳过赛丽亚房间 paint（0016/0017/0018）。"""
    return _env_bool("USDOF_GAME_TUTORIAL_SKIP_SERIA_ROOM", True)


def grant_after_start_setup() -> bool:
    """是否在 START 角色包 delayed 末尾直接 Phase-T1（默认开，不经 0023）。"""
    return _env_bool("USDOF_GAME_TUTORIAL_GRANT_AFTER_SETUP", True)


def requires_town_ready(session: Any) -> bool:
    """Keep the lancer's first town load separate from its tutorial grant."""
    return (
        session is not None
        and int(getattr(session, "pending_tutorial_job", -1)) == 13
        and int(getattr(session, "pending_tutorial_dungeon_id", 0)) == 10005
    )


def force_tutorial_return_spawn(
    session: Any,
    *,
    town_id: int | None = None,
) -> dict[str, int]:
    """预置/强制：离开教程本后回「当前城镇赛丽亚房间」。

    银色村庄 town=1 → area=0 pos=(474,234)（town_cera_rooms_usdof.json gate）。
    写入 pre_select_*（leave 优先）与 cur_*（enter_select 缓存同源）。
    """
    tid = int(town_id or 0)
    if tid <= 0 and session is not None:
        for attr in ("cur_town_id", "pre_select_town_id"):
            val = getattr(session, attr, None)
            if val is not None and int(val) > 0:
                tid = int(val)
                break
    if tid <= 0:
        tid = 1
    room = get_cera_room_for_town(tid)
    snap = {
        "town_id": int(room.town_id) & 0xFF,
        "area_id": int(room.area_id) & 0xFF,
        "pos_x": int(room.pos_x) & 0xFFFF,
        "pos_y": int(room.pos_y) & 0xFFFF,
        "direction": int(room.direction) & 0xFF,
        "state": int(room.area_state) & 0xFF,
    }
    if session is None:
        return snap
    try:
        session.pre_select_town_id = snap["town_id"]
        session.pre_select_area_id = snap["area_id"]
        session.pre_select_pos_x = snap["pos_x"]
        session.pre_select_pos_y = snap["pos_y"]
        session.pre_select_direction = snap["direction"]
        session.pre_select_area_state = snap["state"]
        session.cur_town_id = snap["town_id"]
        session.cur_area_id = snap["area_id"]
        session.cur_pos_x = snap["pos_x"]
        session.cur_pos_y = snap["pos_y"]
        session.cur_direction = snap["direction"]
        session.cur_area_state = snap["state"]
        session.tutorial_return_spawn_forced = True
    except Exception:
        pass
    return snap


def arm_tutorial_on_start(
    session: Any,
    *,
    dungeon_id: int,
    character_id: int,
    job: int = 0,
    town_id: int | None = None,
) -> str:
    """Phase-T0：仅 arm，禁止发 001C/001D；预置回城赛丽亚坐标。"""
    if session is None:
        return "tutorial_arm skip session=None"
    did = int(dungeon_id) & 0xFFFFFFFF
    if did <= 0:
        return "tutorial_arm skip dungeon_id=0"
    session.tutorial_armed = True
    session.tutorial_phase_t1_done = False
    session.pending_tutorial_dungeon_id = did
    session.pending_tutorial_character_id = int(character_id) & 0xFFFFFFFF
    session.pending_tutorial_job = int(job) & 0xFF
    # 确保 hybrid 路径不会在同批误触发时重复
    session.tutorial_hybrid_granted = False
    snap = force_tutorial_return_spawn(session, town_id=town_id)
    return (
        "tutorial_arm ok dungeon=%s cid=%s job=%s phase=T0_start "
        "return_spawn town=%s area=%s pos=(%s,%s)"
        % (
            did,
            int(character_id),
            int(job) & 0xFF,
            snap["town_id"],
            snap["area_id"],
            snap["pos_x"],
            snap["pos_y"],
        )
    )


def clear_tutorial_arm(session: Any) -> None:
    if session is None:
        return
    session.tutorial_armed = False
    session.pending_tutorial_dungeon_id = 0
    session.pending_tutorial_character_id = 0
    session.pending_tutorial_job = 0


def _run_phase_t1(
    server: Any,
    *,
    session: Any,
    character_id: int,
    dungeon_id: int,
    source: str,
) -> tuple[list[bytes], str]:
    """内部：选本态 + grant + 持久化。调用前须已防重入标 done。"""
    enter_svc = getattr(server, "dungeon_enter_000f_0010", None)
    if enter_svc is None:
        clear_tutorial_arm(session)
        return [], "tutorial_phase_t1 fail no dungeon_enter service"

    character = None
    try:
        character = enter_svc._load_character(int(character_id))
    except Exception:
        character = None

    # 回城坐标：强制赛丽亚（避免 enter_select 缓存到副本门口）
    town_hint = None
    if session is not None:
        town_hint = getattr(session, "cur_town_id", None) or getattr(
            session, "pre_select_town_id", None
        )
    if character is not None and (town_hint is None or int(town_hint or 0) <= 0):
        town_hint = getattr(character, "town_id", 1)
    snap = force_tutorial_return_spawn(session, town_id=town_hint)
    # enter_select 的 _cache_pre_select 仅在 not in_dungeon_select 时写入；
    # 这里已写好 pre_select，再保证 cur 为赛丽亚供缓存同源。
    if session is not None:
        try:
            session.in_dungeon_select = False
        except Exception:
            pass

    packets: list[bytes] = []
    notes: list[str] = [
        "tutorial_phase_t1 begin source=%s dungeon=%s cid=%s return_spawn=%s/%s@(%s,%s)"
        % (
            source,
            dungeon_id,
            int(character_id),
            snap["town_id"],
            snap["area_id"],
            snap["pos_x"],
            snap["pos_y"],
        )
    ]

    # 1) 完整 000F 选本态（area=0xFF + 0005/0017/0003/001A/001B …）
    try:
        sel_pkts, sel_notes = enter_svc._build_enter_select_packets(
            session=session,
            character_id=int(character_id) if character_id else None,
            character=character,
        )
        packets.extend(sel_pkts or [])
        if sel_notes:
            notes.append("enter_select: " + "; ".join(str(x) for x in sel_notes[:12]))
        notes.append("enter_select n=%s" % len(sel_pkts or []))
    except Exception as exc:
        notes.append("enter_select_fail:%r" % (exc,))

    # 2) 再次钉死 pre_select（enter_select 可能用 cur 覆盖 area）
    force_tutorial_return_spawn(session, town_id=snap["town_id"])

    # 3) grant 001C+001D
    grant_ok = False
    try:
        grant_pkts, grant_note = enter_svc.grant_by_dungeon_id(
            session=session,
            dungeon_id=int(dungeon_id),
            difficulty=0,
            source="tutorial_phase_t1",
        )
        packets.extend(grant_pkts or [])
        grant_ok = bool(grant_pkts)
        notes.append(grant_note)
    except Exception as exc:
        notes.append("grant_fail:%r" % (exc,))

    # 4) 持久化：成功写 state=2；失败也写 2，避免反复卡在 arm 环
    try:
        repo = server._repo()
        ok_state = _tut.mark_tutorial_triggered(repo, int(character_id))
        notes.append(
            "tutorial_state->2 triggered_once persist=%s dungeon=%s"
            % ("ok" if ok_state else "fail", dungeon_id)
        )
    except Exception as state_exc:
        notes.append("tutorial_state_persist_fail:%r" % (state_exc,))

    if grant_ok:
        _tut.bind_session_tutorial_flags(session, dungeon_id=dungeon_id, active=True)
        notes.append("tutorial_phase_t1 ok packets=%s" % len(packets))
    else:
        notes.append(
            "tutorial_phase_t1 grant_empty packets=%s (state->2 anyway)" % len(packets)
        )

    clear_tutorial_arm(session)
    delay_ms = _env_int("USDOF_GAME_TUTORIAL_PHASE_T1_DELAY_MS", 0)
    if delay_ms > 0:
        notes.append("delay_ms=%s (config_only)" % delay_ms)

    return packets, "; ".join(notes)


def try_phase_t1_on_town_ready(
    server: Any,
    *,
    session: Any,
    character_id: int,
    source: str = "C1/0023",
) -> tuple[list[bytes], str]:
    """Phase-T1 入口：0023 fallback 或 START delayed 末尾主动调用。

    返回 (packets, note)。无动作时 packets=[]。
    """
    if session is None:
        return [], "tutorial_phase_t1 skip session=None"
    if not phase_t1_enabled():
        return [], "tutorial_phase_t1 off (USDOF_GAME_TUTORIAL_AFTER_0023=0)"
    if not bool(getattr(session, "tutorial_armed", False)):
        return [], "tutorial_phase_t1 skip not_armed"
    if bool(getattr(session, "tutorial_phase_t1_done", False)):
        return [], "tutorial_phase_t1 skip already_done"
    if bool(getattr(session, "in_dungeon_run", False)) or bool(
        getattr(session, "is_tutorial_dungeon_run", False)
    ):
        clear_tutorial_arm(session)
        session.tutorial_phase_t1_done = True
        return [], "tutorial_phase_t1 skip already_in_dungeon"

    dungeon_id = int(getattr(session, "pending_tutorial_dungeon_id", 0) or 0) & 0xFFFFFFFF
    if dungeon_id <= 0:
        clear_tutorial_arm(session)
        return [], "tutorial_phase_t1 skip pending_dungeon=0"

    pending_cid = int(getattr(session, "pending_tutorial_character_id", 0) or 0)
    if pending_cid > 0 and int(character_id) > 0 and pending_cid != int(character_id):
        return [], (
            "tutorial_phase_t1 skip cid_mismatch pending=%s now=%s"
            % (pending_cid, int(character_id))
        )

    # 防重入：先标 done
    session.tutorial_phase_t1_done = True
    session.tutorial_armed = False

    return _run_phase_t1(
        server,
        session=session,
        character_id=int(character_id),
        dungeon_id=dungeon_id,
        source=source,
    )


def build_phase_t1_delayed_for_start(
    server: Any,
    *,
    session: Any,
    character_id: int,
) -> tuple[list[tuple[bytes, str]], str]:
    """START 角色包之后：生成 Phase-T1 delayed 条目（带 note 标签）。

    返回 ([(pkt, note), ...], summary_note)。未启用/未 arm 时列表空。
    """
    if not phase_t1_enabled() or not grant_after_start_setup():
        return [], "tutorial_phase_t1_start_queue skip (off or GRANT_AFTER_SETUP=0)"
    if session is None or not bool(getattr(session, "tutorial_armed", False)):
        return [], "tutorial_phase_t1_start_queue skip not_armed"
    if requires_town_ready(session):
        return [], "tutorial_phase_t1_start_queue wait C1/0023 job=13 dungeon=10005"

    packets, note = try_phase_t1_on_town_ready(
        server,
        session=session,
        character_id=int(character_id),
        source="START_delayed_after_setup",
    )
    if not packets:
        return [], note
    tagged: list[tuple[bytes, str]] = []
    n = len(packets)
    for i, pkt in enumerate(packets):
        tagged.append(
            (
                pkt,
                "tutorial-phase-t1-after-setup; %s [%d/%d]" % (note[:80], i + 1, n),
            )
        )
    return tagged, note
