"""进赛丽亚 delayed burst 结束后的装备栏补刷。

背景（2026-07-19 日志对照 CCC 进房）:
  - 服务端 U0/U1/000E 内容与顺序正确（n=5、U0→U1→000E）
  - 客户端 PIV 顺序：先 0004/000D/0131，再发 008c/0162，之后才处理 delayed
  - USERINFO type0/1 **不回** 0002 PIV；000E 有时回有时不回
  - Stage3：000E list3 只更新 USERINFO1/sub_166AED0 已建槽；槽未就绪则静默跳过
    → Inventory 装备栏概率空

2026-07-19 二次对照（CCC 空栏 vs 正常）:
  - 正常进房（080919）：**无** post-settle U1，栏正常
  - 空栏进房（083145）：burst 后补发 U1+000E；客户端两次 000E 均 PIV，栏仍空
  - 推断：会话内 **过早二次 USERINFO1** 可能重建/打乱槽对象

2026-07-19 三次加固（用户要求栏必正常）:
  - 主路径：等 C->S PIV 0x0131 后再发 U0/U1/000E（见 game_server）
  - post-settle：仅短 delay 补 **000E**，**不** mark 终态
  - on-008c（UI settle）：默认仅补 **000E** 并 mark 终态

2026-07-23 冷启动剑魂光剑对照：
  - 主路径 U1→000E→0013 后光剑仍在槽 12；post-settle 000E 也正常
  - on-008c 二次 U1→000E 后，客户端主动发 0013 卸下光剑
  - 二次 U1 会在完整 0013 技能树之后重建精简技能/装备状态；默认关闭，
    需要诊断历史空栏时才显式打开 ON_008C_RESEND_USERINFO1

2026-07-23 第二次冷启动对照：
  - 关闭二次 U1 后，on-008c 单独的 000E 仍紧接着触发 0013 卸装
  - 主进房已经完成一次 U1→000E→0013；两个 settle 补刷都改为默认关闭
"""
from __future__ import annotations

import os
from typing import Any

from game.character.adventure_group import (
    calculate_adventure_group_for_character,
)
from game.chain_loader import load_chain
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.character.payloads import build_userinfo_subtype1_min


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip(), 0)
    except ValueError:
        return int(default)


def post_settle_equip_refresh_enabled() -> bool:
    return _env_bool("USDOF_GAME_ENTER_SERIA_POST_SETTLE_EQUIP_REFRESH", False)


def on_008c_equip_refresh_enabled() -> bool:
    """客户端进房后首个 C1/008C 触发装备栏 000E 补刷（默认关）。"""
    return _env_bool("USDOF_GAME_ENTER_SERIA_ON_008C_EQUIP_REFRESH", False)


def post_settle_equip_refresh_delay_ms() -> int:
    # 默认 200ms：短兜底，避免长时间阻塞 handler（阻塞会导致 008C 迟到）
    return max(0, min(5000, _env_int("USDOF_GAME_ENTER_SERIA_POST_SETTLE_EQUIP_REFRESH_MS", 200)))


def post_settle_equip_u1_before_000e_gap_ms() -> int:
    return max(
        0,
        min(
            2000,
            _env_int(
                "USDOF_GAME_ENTER_SERIA_POST_SETTLE_U1_BEFORE_000E_MS",
                300,
            ),
        ),
    )


def resend_userinfo1_enabled(reason: str = "") -> bool:
    """是否在补刷中重发 USERINFO1 / sub_166AED0。

    - post-settle：默认关（过早二次 U1 曾与空栏相关）
    - on-008c：默认关；主路径已建槽，二次 U1 会覆盖完整技能树后的本地状态
    """
    tag = str(reason or "").strip().lower()
    if tag.startswith("on-008c"):
        return _env_bool("USDOF_GAME_ENTER_SERIA_ON_008C_RESEND_USERINFO1", False)
    if tag.startswith("post-settle"):
        return _env_bool("USDOF_GAME_ENTER_SERIA_POST_SETTLE_RESEND_USERINFO1", False)
    # 未知名：保守，跟 post-settle 一样默认不重发 U1
    return _env_bool("USDOF_GAME_ENTER_SERIA_POST_SETTLE_RESEND_USERINFO1", False)


def resend_000e_enabled() -> bool:
    return _env_bool("USDOF_GAME_ENTER_SERIA_POST_SETTLE_RESEND_000E", True)


def _load_character_record(repo: Any, character_id: int, account_id: int) -> Any | None:
    cid = int(character_id)
    if cid <= 0:
        return None
    try:
        rows = list(repo.list_by_account(int(account_id)))
    except Exception:
        return None
    for row in rows:
        if int(getattr(row, "character_id", -1) or -1) == cid:
            return row
    return None


def build_post_settle_equip_refresh_packets(
    server: Any,
    *,
    character_id: int,
    account_id: int = 1,
    reason: str = "post-settle",
) -> tuple[list[tuple[bytes, str]], str]:
    """构造装备栏补刷包：可选 USERINFO1 + 000E list3。

    Args:
        reason: 日志标签（post-settle / on-008c 等）

    Returns:
        (packets_with_notes, summary_note)
    """
    tag = str(reason or "post-settle").strip() or "post-settle"

    if not post_settle_equip_refresh_enabled() and tag.startswith("post-settle"):
        return [], "post_settle_equip_refresh=off"
    if tag.startswith("on-008c") and not on_008c_equip_refresh_enabled():
        return [], "on_008c_equip_refresh=off"

    cid = int(character_id)
    if cid <= 0:
        return [], "%s_equip_refresh=skip no_cid" % tag

    repo = server._repo()
    quest_completion = load_chain("helper-quest_completion_snapshot.py")
    selected = _load_character_record(repo, cid, int(account_id))
    if selected is None:
        return [], "%s_equip_refresh=skip char_not_found cid=%s" % (tag, cid)

    equip_helper = load_chain("helper-userinfo_equipment.py")
    equipment, equip_note = equip_helper.resolve_enter_room_equipment(
        selected,
        repo=repo,
    )

    packets: list[tuple[bytes, str]] = []
    send_u1 = resend_userinfo1_enabled(tag)
    guild_status_rows = ()

    # 1) 可选：再灌 USERINFO1 / sub_166AED0（默认关，仅诊断历史空栏时显式开）
    if send_u1:
        from game.character.character_stats import compute_combat_stats, describe_growth_phase

        job = int(getattr(selected, "job", 0) or 0)
        grow = int(getattr(selected, "grow_type", 0) or 0)
        raw_sub = getattr(selected, "sub_grow_type", None)
        sub_grow = None if raw_sub is None else int(raw_sub)
        level = max(1, int(getattr(selected, "level", 1) or 1))
        exp = int(getattr(selected, "exp", 0) or 0) & 0xFFFFFFFF
        try:
            exp_db, lv_db = repo.get_character_exp_level(cid)
            exp = int(exp_db or 0) & 0xFFFFFFFF
            if int(lv_db or 0) > 0:
                level = max(1, int(lv_db))
        except Exception:
            pass

        # 与 flow-start_map_enter 一致：grow=0 基线默认嵌 U1 技能页。
        # 2026-07-20：关技能后 grow=0 冷进必闪；20:24 可玩会话 skill_page0=16。
        skills: tuple = ()
        skill_n = 0
        skill_note = "u1_skills=off"
        if _env_bool("USDOF_GAME_ENTER_SERIA_START_USERINFO1_SKILLS", True):
            try:
                skills_helper = load_chain("helper-character_skills.py")
                skill_list, skill_note = skills_helper.resolve_skills_for_character(
                    repo,
                    cid,
                    job,
                    seed_if_empty=False,
                )
                skills = tuple(skill_list or ())
                skill_n = len(skills)
                skill_note = "u1_skills=on %s" % skill_note
            except Exception as skill_exc:
                skill_note = "skills_fail=%r" % (skill_exc,)
                skills = ()
                skill_n = 0

        phase = describe_growth_phase(grow, sub_grow_type=sub_grow)
        stats = compute_combat_stats(job, level, grow, sub_grow_type=sub_grow)
        quest_snapshot = quest_completion.load_finished_quest_snapshot(repo, cid)
        from game.quest.time_fragment_rewards import finished_time_fragment_count

        time_fragment_count = finished_time_fragment_count(
            quest_snapshot.finished_quest_ids,
            job_id=job,
        )
        adventure_progression = calculate_adventure_group_for_character(
            repo,
            selected,
        )
        adventure_group_level = int(adventure_progression.adventure_level)
        guild_membership_active = has_active_guild_membership(repo, selected)
        u1 = build_userinfo_subtype1_min(
            selected,
            exp=exp,
            skills=skills,
            entry_flag=0,
            equipment=equipment,
            finished_quest_ids=quest_snapshot.finished_quest_ids,
            adventure_group_level=adventure_group_level,
            guild_membership_active=guild_membership_active,
        )
        packets.append(
            (
                server._codec_response(0, 0x0002, u1),
                (
                    "%s-equip-refresh 0x0002 userinfo_subtype1_stats "
                    "job=%s grow=%s sub=%s lv=%s exp=%s key=%s equip_166aed0=%s "
                    "skill_page0=%s first=%s second=%s classed=%s awakened=%s "
                    "hp=%s mp=%s plain_len=%s %s time_fragments=%s "
                    "adventure_level=%s %s"
                    % (
                        tag,
                        job,
                        grow,
                        sub_grow,
                        level,
                        exp,
                        cid & 0xFFFF,
                        len(equipment),
                        skill_n,
                        phase.first,
                        phase.second,
                        int(phase.classed),
                        int(phase.awakened),
                        stats.hp_max,
                        stats.mp_max,
                        len(u1),
                        equip_note,
                        time_fragment_count,
                        adventure_group_level,
                        skill_note + " " + quest_snapshot.describe(),
                    )
                ),
            )
        )
        guild_status_rows = pack_guild_membership_status_rows(
            server,
            guild_membership_active,
        )

    # 2) 000E list3 更新装备栏（默认开；槽由主进房 U1 或上面补发 U1 创建）
    if resend_000e_enabled():
        equip_bar_helper = load_chain("helper-equip_itemlist.py")
        equip_body, equip_n, equip_bar_note = (
            equip_bar_helper.build_equipment_bar_update_000e_body(repo, cid)
        )
        packets.append(
            (
                server._codec_response(0, 0x000E, equip_body),
                "%s-equip-refresh %s" % (tag, equip_bar_note),
            )
        )
        n_note = "n=%s" % equip_n
    else:
        n_note = "000e=off"

    # Keep status mutation outside the USERINFO1 -> 000E equipment-slot gap.
    packets.extend(guild_status_rows)

    if not packets:
        return [], "%s_equip_refresh=empty_packets" % tag

    summary = (
        "%s_equip_refresh packets=%s delay_ms=%s u1_gap_ms=%s resend_u1=%s %s %s"
        % (
            tag,
            len(packets),
            post_settle_equip_refresh_delay_ms(),
            post_settle_equip_u1_before_000e_gap_ms(),
            int(send_u1),
            n_note,
            equip_note,
        )
    )
    return packets, summary


__all__ = [
    "post_settle_equip_refresh_enabled",
    "on_008c_equip_refresh_enabled",
    "post_settle_equip_refresh_delay_ms",
    "post_settle_equip_u1_before_000e_gap_ms",
    "resend_userinfo1_enabled",
    "resend_000e_enabled",
    "build_post_settle_equip_refresh_packets",
]
