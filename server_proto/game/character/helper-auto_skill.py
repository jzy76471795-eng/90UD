# -*- coding: utf-8 -*-
"""自动学习技能（Stage3 live C1/0x01EC mode=1 = AUTO_SKILL）。

同 opcode 双语义：
  - mode=1 → 本模块（Auto Study）
  - mode=0 → helper-skill_init（初始化重置）

语义：
  - **不**清空已学技能
  - 在剩余 SP（及可选 TP）预算内，按角色等级（含达人契约学习门加成）/
    转职 / 前置 / 费用表，尽量把可学技能点到当前能到的最高级
  - 落库 character_skills + skill_points
  - 调用方用 BUY_SKILL 形 ACK（entries + final_mode）+ 0013/0025 刷 UI

对照：
  - PacketEnum ENUM_CMDPACKET_AUTO_SKILL=490（0x1EA，旧表）
  - Stage3 live wire：C1/0x01EC tree=0 mode=1（2026-07-21 抓包）
  - 学技规则复用 helper-buy_skill / helper-skill_data（PVF skl）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from game.chain_loader import load_chain

_buy = load_chain("helper-buy_skill.py")
BuySkillEntry = _buy.BuySkillEntry
BuySkillResult = _buy.BuySkillResult
BuySkillResultEntry = _buy.BuySkillResultEntry
execute_buy_skill = _buy.execute_buy_skill

_data = load_chain("helper-skill_data.py")
get_skill_static = _data.get_skill_static
load_job_skill_paths = _data.load_job_skill_paths
free_baseline_levels = _data.free_baseline_levels

_sp = load_chain("helper-skill_points.py")
ensure_skill_points_ledger = _sp.ensure_skill_points_ledger

_skills_h = load_chain("helper-character_skills.py")
resolve_skills_for_character = _skills_h.resolve_skills_for_character


@dataclass
class AutoSkillPlanEntry:
    skill_id: int
    from_level: int
    to_level: int
    cost_sp: int = 0
    cost_tp: int = 0


@dataclass
class AutoSkillResult:
    success: bool
    skill_tree: int = 0
    mode: int = 0
    remain_sp: int = 0
    remain_tp: int = 0
    total_sp: int = 0
    total_tp: int = 0
    entries: list[Any] = field(default_factory=list)  # BuySkillResultEntry
    plan: list[AutoSkillPlanEntry] = field(default_factory=list)
    error_code: int = 0
    note: str = ""


def parse_auto_skill_body(body: bytes) -> dict[str, int] | None:
    """解析 C1/01EC auto-study；至少 2 字节 tree, mode。"""
    data = bytes(body or b"")
    if len(data) < 2:
        return None
    tree_raw = int(data[0]) & 0xFF
    tree = 1 if tree_raw == 1 else 0
    mode = int(data[1]) & 0xFF
    return {
        "skill_tree": tree,
        "skill_tree_raw": tree_raw,
        "mode": mode,
        "raw_len": len(data),
    }


def build_auto_skill_ack_ok(*, skill_tree: int = 0) -> bytes:
    """S1/01EC 成功 ACK：success, tree, refresh_flag=1。"""
    return bytes([0x01, int(skill_tree) & 0xFF, 0x01])


def build_auto_skill_ack_fail(error_code: int = 1) -> bytes:
    return bytes([0x00, int(error_code) & 0xFF])


def _prereq_ok(
    sd: Any,
    work_levels: dict[int, int],
) -> bool:
    for pre_id, pre_lv in getattr(sd, "pre_required", ()) or ():
        have = int(work_levels.get(int(pre_id) & 0xFFFF, 0) or 0)
        if have < int(pre_lv):
            return False
    return True


def plan_auto_skill(
    *,
    job: int,
    level: int,
    grow_type: int = 0,
    skill_tree: int = 0,
    work_levels: dict[int, int],
    remain_sp: int,
    remain_tp: int,
    include_tp: bool = False,
    max_steps: int = 4000,
) -> tuple[list[AutoSkillPlanEntry], int, int]:
    """贪心自动加点：反复选「下一合法 +1 级」直到 SP/TP 不够。

    排序键（稳定、可复现）：
      1) 目标等级的角色等级门槛 required_level_for_target
      2) skill_class
      3) skill_id
    主动/被动都学；默认 **跳过 TP 技能**（include_tp=False）。
    """
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    first = int(grow_type) & 0x0F
    tree = 1 if int(skill_tree) == 1 else 0
    _ = tree  # 槽位在 execute_buy 里分配；规划只看等级

    levels = {int(k) & 0xFFFF: max(0, int(v)) for k, v in (work_levels or {}).items()}
    sp = max(0, int(remain_sp))
    tp = max(0, int(remain_tp))

    paths = load_job_skill_paths(job_i) or {}
    skill_ids = sorted(int(s) & 0xFFFF for s in paths.keys() if int(s) > 0)

    plan: list[AutoSkillPlanEntry] = []
    # 合并同一 skill 的连续升级为一条 plan（from→to）
    open_plan: dict[int, AutoSkillPlanEntry] = {}

    steps = 0
    while steps < int(max_steps):
        steps += 1
        best = None  # (req_lv, skill_class, sid, cost, is_tp, new_lv)
        for sid in skill_ids:
            sd = get_skill_static(job_i, sid)
            if sd is None:
                continue
            is_tp = bool(getattr(sd, "is_tp_skill", False))
            if is_tp and not include_tp:
                continue
            cur = int(levels.get(sid, 0) or 0)
            grow_max = int(sd.max_level_for_grow(first))
            if grow_max <= 0:
                continue
            maximum_level = int(sd.maximum_level_for_grow(first))
            if maximum_level <= 0:
                continue
            hard_max = min(maximum_level, grow_max)
            if cur >= hard_max:
                continue
            new_lv = cur + 1
            need_lv = int(sd.required_level_for_target(new_lv))
            if need_lv > lv:
                continue
            if not _prereq_ok(sd, levels):
                continue
            cost = int(sd.cost_for(cur, new_lv))
            if is_tp:
                if cost > tp:
                    continue
            else:
                if cost > sp:
                    continue
            skill_class = int(getattr(sd, "skill_class", 0) or 0)
            key = (need_lv, skill_class, sid, cost, 1 if is_tp else 0, new_lv)
            if best is None or key < best:
                best = key

        if best is None:
            break

        need_lv, skill_class, sid, cost, is_tp_i, new_lv = best
        is_tp = bool(is_tp_i)
        cur = int(levels.get(sid, 0) or 0)
        if is_tp:
            tp -= cost
        else:
            sp -= cost
        levels[sid] = int(new_lv)

        pe = open_plan.get(sid)
        if pe is None:
            pe = AutoSkillPlanEntry(
                skill_id=sid,
                from_level=cur,
                to_level=int(new_lv),
                cost_sp=0 if is_tp else cost,
                cost_tp=cost if is_tp else 0,
            )
            open_plan[sid] = pe
            plan.append(pe)
        else:
            pe.to_level = int(new_lv)
            if is_tp:
                pe.cost_tp += cost
            else:
                pe.cost_sp += cost

    return plan, sp, tp


def plan_to_buy_entries(plan: Sequence[AutoSkillPlanEntry]) -> list[Any]:
    """转为 BUY_SKILL 条目：每 skill 一条 level_delta=to-from, is_refund=0。"""
    out: list[Any] = []
    for p in plan or ():
        sid = int(p.skill_id) & 0xFFFF
        delta = max(0, int(p.to_level) - int(p.from_level))
        if sid <= 0 or delta <= 0:
            continue
        # level_delta 是 u8；超过 255 拆条（极少见）
        left = delta
        while left > 0:
            step = min(255, left)
            out.append(
                BuySkillEntry(
                    skill_id=sid,
                    is_refund=0,
                    level_delta=step,
                )
            )
            left -= step
    return out


def execute_auto_skill(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    skill_tree: int = 0,
    mode: int = 1,
    bonus_sp: Optional[int] = None,
    bonus_tp: Optional[int] = None,
    level_gate_bonus: int = 0,
    include_tp: bool = False,
) -> AutoSkillResult:
    """规划并执行自动学习；成功则 DB 已更新。"""
    cid = int(character_id)
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    gate_bonus = max(0, min(255, int(level_gate_bonus or 0)))
    gate_level = min(255, lv + gate_bonus)
    tree = 1 if int(skill_tree) == 1 else 0
    grow = int(grow_type) & 0xFF
    mode_i = int(mode) & 0xFF

    if cid <= 0 or repo is None:
        return AutoSkillResult(
            success=False, error_code=1, skill_tree=tree, mode=mode_i, note="no_cid_or_repo"
        )

    resolve_skills_for_character(repo, cid, job_i, seed_if_empty=True)
    ledger = ensure_skill_points_ledger(
        repo,
        character_id=cid,
        job=job_i,
        level=lv,
        bonus_sp=bonus_sp,
        bonus_tp=bonus_tp,
    )
    remain_sp0 = int(ledger["remaining_sp"])
    remain_tp0 = int(ledger["remaining_tp"])
    total_sp = int(ledger["total_sp"])
    total_tp = int(ledger["total_tp"])

    rows = repo.load_character_skills(cid, page=tree) or []
    work_levels: dict[int, int] = {}
    for row in rows:
        sid = int(row["skill_id"]) & 0xFFFF
        slv = max(0, int(row["level"]) & 0xFF)
        if sid > 0 and slv > 0:
            work_levels[sid] = max(work_levels.get(sid, 0), slv)

    plan, _sp_left, _tp_left = plan_auto_skill(
        job=job_i,
        level=gate_level,
        grow_type=grow,
        skill_tree=tree,
        work_levels=work_levels,
        remain_sp=remain_sp0,
        remain_tp=remain_tp0,
        include_tp=bool(include_tp),
    )

    if not plan:
        return AutoSkillResult(
            success=True,
            skill_tree=tree,
            mode=mode_i,
            remain_sp=remain_sp0,
            remain_tp=remain_tp0,
            total_sp=total_sp,
            total_tp=total_tp,
            entries=[],
            plan=[],
            note=(
                "noop no_affordable_upgrade remain_sp=%s n_skills=%s "
                "char_level=%s level_gate_bonus=%s effective_level=%s"
                % (remain_sp0, len(work_levels), lv, gate_bonus, gate_level)
            ),
        )

    buy_entries = plan_to_buy_entries(plan)
    buy = execute_buy_skill(
        repo,
        character_id=cid,
        job=job_i,
        level=lv,
        grow_type=grow,
        skill_tree=tree,
        entries=buy_entries,
        final_mode=1,
        bonus_sp=bonus_sp,
        bonus_tp=bonus_tp,
        level_gate_bonus=gate_bonus,
    )
    if not buy.success:
        return AutoSkillResult(
            success=False,
            skill_tree=tree,
            mode=mode_i,
            remain_sp=int(buy.remain_sp or remain_sp0),
            remain_tp=int(buy.remain_tp or remain_tp0),
            total_sp=total_sp,
            total_tp=total_tp,
            plan=list(plan),
            error_code=int(buy.error_code or 1),
            note="buy_fail %s plan_n=%s" % (buy.note, len(plan)),
        )

    sample = ",".join(
        "%s:%s->%s" % (p.skill_id, p.from_level, p.to_level) for p in plan[:12]
    ) or "-"
    spent_sp = sum(int(p.cost_sp) for p in plan)
    spent_tp = sum(int(p.cost_tp) for p in plan)
    return AutoSkillResult(
        success=True,
        skill_tree=tree,
        mode=mode_i,
        remain_sp=int(buy.remain_sp),
        remain_tp=int(buy.remain_tp),
        total_sp=total_sp,
        total_tp=total_tp,
        entries=list(buy.entries or []),
        plan=list(plan),
        note=(
            "ok plan_n=%s buy_n=%s spent_sp=%s spent_tp=%s remain_sp=%s remain_tp=%s "
            "char_level=%s level_gate_bonus=%s effective_level=%s sample=%s buy=%s"
            % (
                len(plan),
                len(buy.entries or []),
                spent_sp,
                spent_tp,
                buy.remain_sp,
                buy.remain_tp,
                lv,
                gate_bonus,
                gate_level,
                sample,
                buy.note,
            )
        ),
    )
