"""技能树初始化重置（C1/0x01EC mode!=1）。

Stage3 live 双语义（同 opcode，靠 mode 分流）：
  - mode=0 → 本模块 SKILL_INIT（重置到创角/转职基线 + 全额 SP/TP）
  - mode=1 → helper-auto_skill AUTO_SKILL（保留已学、用剩余 SP 尽量加点）

接线：``opcodes/opcode01ec-skill_init.py`` 按 mode 调用本 helper。
成功后应 arm buy-guard，挡客户端旧方案批量 001D 重买。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from game.chain_loader import load_chain
from game.character.initial_skills import InitialSkill, get_initial_skills_for_job

_sp = load_chain("helper-skill_points.py")
total_sp_for_level = _sp.total_sp_for_level
total_tp_for_level = _sp.total_tp_for_level

# 技能快捷栏（主动技默认落位）；与 helper-buy_skill 一致
_QUICK_BAR_SLOTS: tuple[int, ...] = tuple(list(range(0, 6)) + list(range(198, 204)))
_QUICK_BAR_SET = frozenset(_QUICK_BAR_SLOTS)
_EXTENSION_QUICK_BAR_SLOTS: tuple[int, ...] = tuple(range(198, 204))


def _copy_skill_with_slot(skill: InitialSkill, slot: int) -> InitialSkill:
    command = getattr(skill, "command_bytes", b"") or b""
    if not isinstance(command, (bytes, bytearray, memoryview)):
        command = b""
    return InitialSkill(
        slot=int(slot) & 0xFF,
        skill_id=int(skill.skill_id) & 0xFFFF,
        level=max(1, int(skill.level) & 0xFF),
        command_bytes=bytes(command),
    )


def _allocate_dark_knight_non_quickbar_slot(
    occupied: set[int],
    *preferred_slots: int,
) -> int:
    """Allocate a skill-tree slot without touching either visible quick bar."""
    for raw_slot in preferred_slots:
        slot = int(raw_slot) & 0xFF
        if slot not in _QUICK_BAR_SET and slot not in occupied and slot < 204:
            return slot
    for start, end in ((150, 198), (6, 138)):
        for slot in range(start, end):
            if slot not in occupied:
                return slot
    return -1


def _ensure_dark_knight_skills_on_correct_bars(
    skills: Sequence[InitialSkill],
    *,
    get_skill_static: Any,
    allocate_skill_slot: Any,
    preserve_tp: bool = False,
) -> list[InitialSkill]:
    """Apply the Stage3 Dark Knight split between combo and extension bars.

    Slots 0..5 are the six ``combosetN`` roots.  Skills listed by those roots'
    PVF ``[combo set]`` sections stay in non-shortcut skill-tree slots because
    S0/01C0 owns their 6x5 presentation.  Other active skills, including the
    common Backstep which PVF explicitly rejects from combo slots, use only the
    extension bar 198..203.
    """
    combo_helper = load_chain("helper-dark_knight_combo_skill.py")
    root_slot_by_skill = dict(combo_helper.DARK_KNIGHT_COMBO_ROOT_SLOT_BY_SKILL)
    combo_child_ids = set(combo_helper.dark_knight_combo_child_skill_ids())
    initial_slot_by_skill = {
        int(skill.skill_id) & 0xFFFF: int(skill.slot) & 0xFF
        for skill in get_initial_skills_for_job(combo_helper.DARK_KNIGHT_JOB)
    }

    roots: list[InitialSkill] = []
    combo_actives: list[InitialSkill] = []
    extension_actives: list[InitialSkill] = []
    passives: list[InitialSkill] = []
    specials: list[InitialSkill] = []

    for skill in skills or ():
        sid = int(skill.skill_id) & 0xFFFF
        level = max(0, int(skill.level) & 0xFF)
        if sid <= 0 or level <= 0:
            continue
        static = get_skill_static(combo_helper.DARK_KNIGHT_JOB, sid)
        normalized = _copy_skill_with_slot(skill, int(skill.slot) & 0xFF)
        if static is not None and bool(getattr(static, "is_tp_skill", False)):
            if preserve_tp:
                passives.append(normalized)
            continue
        if sid in root_slot_by_skill:
            roots.append(normalized)
            continue
        is_special = bool(getattr(static, "is_special", False)) if static else (
            200 <= sid <= 208
        )
        is_active = bool(getattr(static, "is_active", False)) if static else sid < 100
        if is_special:
            specials.append(normalized)
        elif is_active and sid in combo_child_ids:
            combo_actives.append(normalized)
        elif is_active:
            extension_actives.append(normalized)
        else:
            passives.append(normalized)

    occupied: set[int] = set()
    out: list[InitialSkill] = []

    root_by_skill = {int(skill.skill_id) & 0xFFFF: skill for skill in roots}
    for root_skill_id, slot in root_slot_by_skill.items():
        root = root_by_skill.get(root_skill_id)
        if root is None:
            continue
        occupied.add(int(slot))
        out.append(_copy_skill_with_slot(root, slot))

    combo_actives.sort(
        key=lambda skill: (
            initial_slot_by_skill.get(int(skill.skill_id) & 0xFFFF, 0xFFFF),
            int(skill.skill_id) & 0xFFFF,
        )
    )
    for skill in combo_actives:
        sid = int(skill.skill_id) & 0xFFFF
        slot = _allocate_dark_knight_non_quickbar_slot(
            occupied,
            initial_slot_by_skill.get(sid, -1),
            int(skill.slot) & 0xFF,
        )
        if slot < 0:
            continue
        occupied.add(slot)
        out.append(_copy_skill_with_slot(skill, slot))

    # Preserve an existing extension order; only newly misplaced skills take
    # the first free Q/W/E/R/T/Y slot.
    extension_actives.sort(
        key=lambda skill: (
            0 if (int(skill.slot) & 0xFF) in _EXTENSION_QUICK_BAR_SLOTS else 1,
            int(skill.slot) & 0xFF
            if (int(skill.slot) & 0xFF) in _EXTENSION_QUICK_BAR_SLOTS
            else int(skill.skill_id) & 0xFFFF,
        )
    )
    for skill in extension_actives:
        current = int(skill.slot) & 0xFF
        if current in _EXTENSION_QUICK_BAR_SLOTS and current not in occupied:
            slot = current
        else:
            slot = next(
                (value for value in _EXTENSION_QUICK_BAR_SLOTS if value not in occupied),
                -1,
            )
        if slot < 0:
            slot = _allocate_dark_knight_non_quickbar_slot(occupied, current)
        if slot < 0:
            continue
        occupied.add(slot)
        out.append(_copy_skill_with_slot(skill, slot))

    passives.sort(key=lambda skill: (int(skill.slot) & 0xFF, int(skill.skill_id)))
    for skill in passives:
        sid = int(skill.skill_id) & 0xFFFF
        want = int(skill.slot) & 0xFF
        if want in occupied or want in _QUICK_BAR_SET:
            static = get_skill_static(9, sid)
            want = int(
                allocate_skill_slot(
                    is_active=False,
                    skill_class=int(getattr(static, "skill_class", 0) or 0)
                    if static
                    else 0,
                    grow_type_count=len(getattr(static, "grow_types", ()) or ())
                    if static
                    else 1,
                    occupied=occupied,
                    tree=0,
                )
            )
        if want < 0 or want in occupied or want in _QUICK_BAR_SET:
            want = _allocate_dark_knight_non_quickbar_slot(occupied)
        if want < 0:
            continue
        occupied.add(want)
        out.append(_copy_skill_with_slot(skill, want))

    for skill in specials:
        slot = _allocate_dark_knight_non_quickbar_slot(
            occupied, int(skill.slot) & 0xFF
        )
        if slot < 0:
            continue
        occupied.add(slot)
        out.append(_copy_skill_with_slot(skill, slot))

    out.sort(key=lambda skill: (int(skill.slot) & 0xFF, int(skill.skill_id) & 0xFFFF))
    return out


@dataclass
class SkillInitResult:
    success: bool
    skill_tree: int = 0
    mode: int = 0
    remain_sp: int = 0
    remain_tp: int = 0
    total_sp: int = 0
    total_tp: int = 0
    skills: list[InitialSkill] = field(default_factory=list)
    baseline_kind: str = ""
    error_code: int = 0
    note: str = ""


def parse_skill_init_body(body: bytes) -> dict[str, int] | None:
    """解析 C1/01EC；至少 2 字节（tree, mode），允许 padding。

    live 解码 plain_len=16：00 01 00 ... → tree=0 mode=1
    """
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


def build_skill_init_ack_ok(*, skill_tree: int = 0) -> bytes:
    """S1/01EC 成功 ACK。

    90qiyuan BuildSkillInitSuccess: success=1, tree, refresh_flag=1
    """
    return bytes([0x01, int(skill_tree) & 0xFF, 0x01])


def build_skill_init_ack_fail(error_code: int = 1) -> bytes:
    return bytes([0x00, int(error_code) & 0xFF])


def skill_init_buy_guard_window_sec() -> float:
    """01EC 成功后抑制客户端「旧方案批量重买」的窗口秒数。"""
    raw = (os.environ.get("USDOF_GAME_SKILL_INIT_BUY_GUARD_SEC") or "3").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


def arm_skill_init_buy_guard(session: Any, *, character_id: int) -> float:
    """在 session 上 arm 买技能护栏；返回 until（monotonic）。"""
    window = skill_init_buy_guard_window_sec()
    until = time.monotonic() + window if window > 0 else 0.0
    if session is not None:
        try:
            session.skill_init_buy_guard_until = until
            session.skill_init_buy_guard_cid = int(character_id)
        except Exception:
            pass
    return until


def skill_init_buy_guard_active(session: Any, *, character_id: int) -> bool:
    """当前角色是否仍在 01EC 后买技能护栏内。"""
    if session is None:
        return False
    try:
        until = float(getattr(session, "skill_init_buy_guard_until", 0) or 0)
    except Exception:
        return False
    if until <= 0 or time.monotonic() >= until:
        return False
    try:
        cid_g = int(getattr(session, "skill_init_buy_guard_cid", 0) or 0)
    except Exception:
        cid_g = 0
    cid = int(character_id)
    return cid_g == 0 or cid_g == cid


def should_suppress_post_init_rebuy(
    entries: Sequence[Any],
    *,
    min_count: int = 2,
) -> bool:
    """是否像 01EC 后客户端批量重买旧方案。

    live 特征：count=14、全部 is_refund=0。
    单条购买 / 含退点包不抑制（用户手动加点/退点）。
    """
    ents = list(entries or ())
    if len(ents) < int(min_count):
        return False
    for e in ents:
        try:
            if int(getattr(e, "is_refund", 0) or 0) != 0:
                return False
        except Exception:
            return False
    return True


def ensure_active_skills_on_quickbar(
    job: int,
    skills: Sequence[InitialSkill],
    *,
    preserve_tp: bool = False,
) -> list[InitialSkill]:
    """初始化后主动技强制落入职业对应快捷栏。

    - 主动：按 skill_id 升序依次填栏（先 0..5 再 198..203）
    - 被动：保留原槽；若与主动抢快捷栏则重分到被动区
    - special 主动（skill_id 200..208）不占快捷栏（slot 语义 0xFF，DB 用 0）
    - 黑暗武士：0..5 仅放 six comboset roots；PVF combo children 留在
      非快捷技能树槽；后跳等非 combo 主动技只放 198..203
    """
    from game.chain_loader import load_chain

    job_i = int(job) & 0xFF
    skill_data = load_chain("helper-skill_data.py")
    get_skill_static = skill_data.get_skill_static
    allocate_skill_slot = skill_data.allocate_skill_slot

    items = [s for s in (skills or ()) if int(s.skill_id) > 0 and int(s.level) > 0]
    if not items:
        return []

    if job_i == 9:
        return _ensure_dark_knight_skills_on_correct_bars(
            items,
            get_skill_static=get_skill_static,
            allocate_skill_slot=allocate_skill_slot,
            preserve_tp=bool(preserve_tp),
        )

    actives: list[InitialSkill] = []
    passives: list[InitialSkill] = []
    specials: list[InitialSkill] = []
    for sk in items:
        sid = int(sk.skill_id) & 0xFFFF
        lv = max(1, min(255, int(sk.level) & 0xFF))
        sd = get_skill_static(job_i, sid)
        # 初始化：TP 强化技一律清空（不进基线、不占栏）
        if sd is not None and bool(getattr(sd, "is_tp_skill", False)):
            continue
        is_special = bool(getattr(sd, "is_special", False)) if sd is not None else (
            200 <= sid <= 208
        )
        is_active = bool(getattr(sd, "is_active", False)) if sd is not None else (
            sid < 100
        )
        if is_special:
            specials.append(InitialSkill(slot=0, skill_id=sid, level=lv))
            continue
        if is_active:
            actives.append(InitialSkill(slot=int(sk.slot) & 0xFF, skill_id=sid, level=lv))
        else:
            passives.append(
                InitialSkill(slot=int(sk.slot) & 0xFF, skill_id=sid, level=lv)
            )

    # 主动：稳定 skill_id 升序填快捷栏
    actives.sort(key=lambda s: int(s.skill_id) & 0xFFFF)
    occupied: set[int] = set()
    out: list[InitialSkill] = []
    qb_i = 0
    for sk in actives:
        if qb_i < len(_QUICK_BAR_SLOTS):
            slot = int(_QUICK_BAR_SLOTS[qb_i])
            qb_i += 1
        else:
            # 快捷栏满：退回分配器（极少见）
            sd = get_skill_static(job_i, int(sk.skill_id) & 0xFFFF)
            slot = allocate_skill_slot(
                is_active=True,
                skill_class=int(getattr(sd, "skill_class", 0) or 0) if sd else 0,
                grow_type_count=int(getattr(sd, "num_growtypes", 1) or 1) if sd else 1,
                occupied=occupied,
                tree=0,
            )
            if slot < 0:
                continue
        occupied.add(int(slot) & 0xFF)
        out.append(
            InitialSkill(
                slot=int(slot) & 0xFF,
                skill_id=int(sk.skill_id) & 0xFFFF,
                level=int(sk.level) & 0xFF,
            )
        )

    # 被动：尽量保留原槽；冲突或落在快捷栏则重分
    passives.sort(key=lambda s: (int(s.slot) & 0xFF, int(s.skill_id) & 0xFFFF))
    for sk in passives:
        sid = int(sk.skill_id) & 0xFFFF
        lv = int(sk.level) & 0xFF
        want = int(sk.slot) & 0xFF
        if want in occupied or want in _QUICK_BAR_SET:
            sd = get_skill_static(job_i, sid)
            slot = allocate_skill_slot(
                is_active=False,
                skill_class=int(getattr(sd, "skill_class", 0) or 0) if sd else 0,
                grow_type_count=int(getattr(sd, "num_growtypes", 1) or 1) if sd else 1,
                occupied=occupied,
                tree=0,
            )
            if slot < 0:
                continue
            want = int(slot) & 0xFF
        occupied.add(want)
        out.append(InitialSkill(slot=want, skill_id=sid, level=lv))

    # special：不占快捷栏展示槽
    for sk in specials:
        out.append(sk)

    out.sort(key=lambda s: (int(s.slot) & 0xFF, int(s.skill_id) & 0xFFFF))
    return out


def strip_tp_skills(job: int, skills: Sequence[InitialSkill]) -> list[InitialSkill]:
    """从技能列表剔除 TP 技能（初始化清空用）。"""
    from game.chain_loader import load_chain

    job_i = int(job) & 0xFF
    get_skill_static = load_chain("helper-skill_data.py").get_skill_static
    out: list[InitialSkill] = []
    for sk in skills or ():
        sid = int(sk.skill_id) & 0xFFFF
        if sid <= 0 or int(sk.level) <= 0:
            continue
        sd = get_skill_static(job_i, sid)
        if sd is not None and bool(getattr(sd, "is_tp_skill", False)):
            continue
        out.append(sk)
    return out


def build_skill_init_snapshot(
    job: int,
    grow_type: int = 0,
    *,
    level: int = 1,
) -> tuple[list[InitialSkill], str]:
    """按转职状态选择初始化技能集合，并保证主动技在快捷栏。

    TP 技能不进入初始化基线（一律清空，TP 点数全额返还）。

    Returns:
      (skills, kind) kind ∈ {create_initial, job_change_baseline}
    """
    from game.chain_loader import load_chain

    job_i = int(job) & 0xFF
    grow = int(grow_type) & 0xFF
    jc = load_chain("helper-job_change.py")
    first, second = jc.decode_grow_type(grow)
    char_lv = max(1, min(255, int(level or 1)))

    # 未转职：创角基础技能（JSON 槽位为底，再强制主动入栏）
    if first <= 0 and second <= 0:
        skills = list(get_initial_skills_for_job(job_i))
        if not skills:
            # JSON 缺失：.chr [initial value] + 槽位分配
            profile = jc.load_chr_skill_profile(job_i)
            levels: dict[int, int] = {}
            for sid, lv in list(profile.get("initial") or []):
                sid_i = int(sid) & 0xFFFF
                lv_i = max(0, int(lv) & 0xFF)
                if sid_i > 0 and lv_i > 0:
                    levels[sid_i] = max(levels.get(sid_i, 0), lv_i)
            skills = list(jc._assign_slots_for_skills(job_i, levels))
        skills = strip_tp_skills(job_i, skills)
        skills = ensure_active_skills_on_quickbar(job_i, skills)
        return skills, "create_initial"

    # 已转职 / 觉醒：与一转重建同一快照（initial + growtype + awakening 送技）
    skills = list(
        jc.build_job_change_skill_snapshot(
            job_i,
            first_grow=int(first),
            second_grow=int(second),
            level=char_lv,
        )
    )
    skills = strip_tp_skills(job_i, skills)
    skills = ensure_active_skills_on_quickbar(job_i, skills)
    return skills, "job_change_baseline"


def execute_skill_init(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    skill_tree: int = 0,
    mode: int = 0,
    bonus_sp: int = 0,
    bonus_tp: int = 0,
) -> SkillInitResult:
    """重置技能树到创角/转职基础技能，并按 PVF 等级表全额发放 SP/TP。

    不改 grow_type / 角色等级；只动 character_skills + character_skill_points。
    - SP 技能：回到创角/转职免费基线
    - TP 技能：一律清空（不进基线）
    - 主动技写入快捷栏
    - remaining_sp/tp = total（底线免费 + TP 全清）
    调用方应在成功后 arm buy-guard 防客户端批量重买。
    """
    cid = int(character_id)
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    tree = 1 if int(skill_tree) == 1 else 0
    grow = int(grow_type) & 0xFF
    mode_i = int(mode) & 0xFF

    if cid <= 0 or repo is None:
        return SkillInitResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            mode=mode_i,
            note="no_cid_or_repo",
        )
    if mode_i > 3:
        return SkillInitResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            mode=mode_i,
            note="bad_mode=%s" % mode_i,
        )

    # bonus：调用方可传；否则从 characters 表读
    b_sp = int(bonus_sp or 0)
    b_tp = int(bonus_tp or 0)
    if b_sp == 0 and b_tp == 0:
        try:
            with repo.open() as conn:
                row = conn.execute(
                    "SELECT bonus_sp, bonus_tp FROM characters "
                    "WHERE character_id = ? AND delete_flag = 0",
                    (cid,),
                ).fetchone()
                if row is not None:
                    b_sp = int(row["bonus_sp"] or 0)
                    b_tp = int(row["bonus_tp"] or 0)
        except Exception:
            pass

    skills, kind = build_skill_init_snapshot(job_i, grow, level=lv)
    if not skills:
        return SkillInitResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            mode=mode_i,
            baseline_kind=kind,
            note="empty_baseline job=%s grow=0x%02X kind=%s" % (job_i, grow, kind),
        )

    # PVF spTable / TP 等级表 → total；底线技能免费 → remaining = total
    total_sp = int(total_sp_for_level(lv, b_sp))
    total_tp = int(total_tp_for_level(lv, b_tp))
    remain_sp = max(0, min(0xFFFF, total_sp))
    remain_tp = max(0, min(0xFFFF, total_tp))

    try:
        # tree0 清两页，避免 page1 残留
        # preserve_commands=True：按 skill_id 保留 C1/014B 释放命令（extra_values）
        clear_all = tree == 0
        replaced = int(
            repo.replace_character_skills(
                cid,
                skills,
                page=tree,
                page_header=int(remain_sp) & 0xFFFF,
                clear_all_pages=clear_all,
                preserve_commands=True,
            )
            or 0
        )
    except Exception as exc:
        return SkillInitResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            mode=mode_i,
            baseline_kind=kind,
            note="replace_fail=%r" % (exc,),
        )

    try:
        repo.upsert_character_skill_points(
            cid,
            total_sp=total_sp,
            remaining_sp=remain_sp,
            total_tp=total_tp,
            remaining_tp=remain_tp,
            synced_level=lv,
        )
    except Exception as exc:
        return SkillInitResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            mode=mode_i,
            remain_sp=remain_sp,
            remain_tp=remain_tp,
            total_sp=total_sp,
            total_tp=total_tp,
            skills=list(skills),
            baseline_kind=kind,
            note="sp_upsert_fail=%r skills_n=%s" % (exc, replaced),
        )

    try:
        repo.update_character_skills_page_header(
            cid, page=tree, page_header=int(remain_sp) & 0xFFFF
        )
    except Exception:
        pass

    # 把 DB 里保留的释放命令挂回 result.skills，供 0013 field4 / 001D 即时回写
    skills_out = _reattach_skill_commands(repo, cid, skills)
    combo_note = ""
    if job_i == 9:
        try:
            combo_helper = load_chain("helper-dark_knight_combo_skill.py")
            _combo_changed, combo_note = combo_helper.ensure_dark_knight_combo_skill_pages(
                repo,
                cid,
                job_i,
                skills_out,
                force_reset=True,
            )
        except Exception as exc:
            combo_note = "dark_knight_combo_reset_fail:%s" % type(exc).__name__

    qb_actives = sum(1 for s in skills_out if (int(s.slot) & 0xFF) in _QUICK_BAR_SET)
    cmd_n = sum(1 for s in skills_out if getattr(s, "command_bytes", b""))
    # 独立库总量（含 01EC 已删非基线技能的命令；重学后仍回挂）
    cmd_store_n = 0
    try:
        if hasattr(repo, "load_character_skill_commands"):
            cmd_store_n = len(repo.load_character_skill_commands(cid) or {})
    except Exception:
        cmd_store_n = 0
    sample = ",".join(
        "%s@%s:lv%s" % (s.skill_id, s.slot, s.level) for s in skills_out[:10]
    ) or "-"
    first = grow & 0x0F
    second = (grow >> 4) & 0x0F
    return SkillInitResult(
        success=True,
        skill_tree=tree,
        mode=mode_i,
        remain_sp=remain_sp,
        remain_tp=remain_tp,
        total_sp=total_sp,
        total_tp=total_tp,
        skills=list(skills_out),
        baseline_kind=kind,
        note=(
            "ok kind=%s grow=0x%02X first=%s second=%s tree=%s mode=%s n=%s "
            "qb_actives=%s cmd_kept=%s cmd_store=%s remain_sp=%s total_sp=%s "
            "remain_tp=%s total_tp=%s sample=%s%s"
            % (
                kind,
                grow,
                first,
                second,
                tree,
                mode_i,
                len(skills_out),
                qb_actives,
                cmd_n,
                cmd_store_n,
                remain_sp,
                total_sp,
                remain_tp,
                total_tp,
                sample,
                (" " + combo_note) if combo_note else "",
            )
        ),
    )


def _reattach_skill_commands(
    repo: Any,
    character_id: int,
    skills: Sequence[InitialSkill],
) -> list[InitialSkill]:
    """replace 后按 skill_id 挂回释放命令到 InitialSkill.command_bytes。

    优先独立库 ``character_skill_commands``（01EC 不删），再 skills.extra_values。
    供 C1/01EC 成功路径即时 0013 field4 / 001D 命令段回写，避免 UI 被默认键刷掉。
    """
    cid = int(character_id or 0)
    base = list(skills or ())
    if repo is None or cid <= 0 or not base:
        return base
    cmd_by_sid: dict[int, bytes] = {}
    # 1) 独立命令库（权威，覆盖已学/未学）
    try:
        if hasattr(repo, "load_character_skill_commands"):
            store = repo.load_character_skill_commands(cid) or {}
            for sid, blob in store.items():
                sid_i = int(sid) & 0xFFFF
                if sid_i <= 0 or not blob:
                    continue
                cmd_by_sid[sid_i] = bytes(blob)
    except Exception:
        pass
    # 2) 当前 skills 行（load 已融合 store；作兜底）
    try:
        rows = repo.load_character_skills(cid) or []
    except Exception:
        rows = []
    for row in rows:
        try:
            sid = int(row.get("skill_id", 0) or 0) & 0xFFFF
            extra = row.get("extra_values")
        except Exception:
            continue
        if sid <= 0 or extra is None or sid in cmd_by_sid:
            continue
        if isinstance(extra, (bytes, bytearray, memoryview)):
            cmd_by_sid[sid] = bytes(extra)
        else:
            try:
                cmd_by_sid[sid] = bytes(extra)
            except Exception:
                continue
    if not cmd_by_sid:
        return base
    out: list[InitialSkill] = []
    for sk in base:
        sid = int(sk.skill_id) & 0xFFFF
        cmd = cmd_by_sid.get(sid) or getattr(sk, "command_bytes", b"") or b""
        if not isinstance(cmd, (bytes, bytearray, memoryview)):
            cmd = b""
        else:
            cmd = bytes(cmd)
        if cmd:
            out.append(
                InitialSkill(
                    slot=int(sk.slot) & 0xFF,
                    skill_id=sid,
                    level=int(sk.level) & 0xFF,
                    command_bytes=cmd,
                )
            )
        else:
            out.append(sk)
    return out


def skills_as_initial_list(skills: Sequence[InitialSkill]) -> list[InitialSkill]:
    return list(skills or ())
