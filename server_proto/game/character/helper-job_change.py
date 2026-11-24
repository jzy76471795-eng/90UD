"""第一次转职 / 觉醒：按 Stage3 FINISH_QUEST handler（sub_162C740）落库并组包。

Stage3 IDA（2026-07-20，权威 .i64）sub_162C740 success 路径：
  读完 exp/gold/consumed 后：
    u8 chainType (= n3_8)
    chainType ∈ {1, 2, 20}:
      u8 growNumber
      for tree in 0..1:
        u8 grantCount
        grantCount × (u8 level | u16 skill_id | u8 flag)
      然后本地重建 actor（sub_1D07810）并切 UI 模式 13
      （sub_2788D20(0, 13) → 转职动画/界面）

服务端职责（本模块）：
  1. 写 characters.grow_type（chain1=一转 first nibble；chain2=觉醒 high nibble）
  2. chain1：清空技能树 → 初始技能 + .chr [growtype N] 送技；SP 全额重算
  3. chain2：在现有技能上 merge 觉醒送技，不动 SP
  4. 组 S0/0013 SKILLINFO（全量替换）与 S0/0002 USERINFO0（新 grow）

.chr 技能段通过共享 PVF 预加载读（禁止冷开 PvfReader）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

from game.character.initial_skills import InitialSkill, get_initial_skills_for_job
from game.character.payloads import (
    build_skillinfo_from_pvf,
    build_userinfo_subtype0_min,
    build_userinfo_subtype1_min,
)
from game.chain_loader import load_chain
from game.guild.guild_benefits import has_active_guild_membership
from game.guild.guild_repository import GuildRepository
from pvf import pvf_runtime


BASE_JOB_NAMES: dict[int, str] = {
    0: "男鬼剑士",
    1: "格斗家",
    2: "神枪手",
    3: "魔法师",
    4: "圣职者",
    5: "女枪手",
    6: "暗夜使者",
    7: "男格斗家",
    8: "女魔法师",
    9: "黑暗武士",
    10: "缔造者",
    11: "女鬼剑士",
    12: "守护者",
    13: "魔枪士",
    14: "女圣职者",
}


@dataclass(frozen=True)
class JobChangeResult:
    ok: bool
    previous_grow_type: int
    new_grow_type: int
    chain_type: int
    grow_number: int
    skills: tuple[InitialSkill, ...] = ()
    remaining_sp: int = 0
    remaining_tp: int = 0
    note: str = ""


def decode_grow_type(grow_type: int) -> tuple[int, int]:
    g = int(grow_type) & 0xFF
    return g & 0x0F, (g >> 4) & 0x0F


def encode_grow_type(first: int, second: int = 0) -> int:
    return ((int(second) & 0x0F) << 4) | (int(first) & 0x0F)


def compute_new_grow_type(
    current_grow_type: int,
    *,
    chain_type: int,
    grow_number: int,
) -> int:
    first, second = decode_grow_type(current_grow_type)
    ct = int(chain_type)
    gn = int(grow_number) & 0x0F
    if ct == 1:
        first = gn
    elif ct == 2:
        second = gn
    else:
        return int(current_grow_type) & 0xFF
    return encode_grow_type(first, second)


@lru_cache(maxsize=1)
def _load_character_lst() -> dict[int, str]:
    """job_id → character/*.chr 相对路径。"""
    text = ""
    for path in ("character/character.lst", "character/character.jpn.lst"):
        try:
            text = pvf_runtime.read_text(path) or ""
        except Exception:
            text = ""
        if text and str(text).strip():
            break
    out: dict[int, str] = {}
    if not text:
        return out
    pairs = re.findall(r"(\d+)\s+`([^`]+)`", text)
    for job_s, rel in pairs:
        try:
            job_i = int(job_s) & 0xFF
        except Exception:
            continue
        rel_n = ("character/" + rel.replace("\\", "/").lstrip("/")).lower()
        out[job_i] = rel_n
    return out


def _read_chr_text(job: int) -> str:
    paths = _load_character_lst()
    rel = paths.get(int(job) & 0xFF)
    if not rel:
        return ""
    try:
        return pvf_runtime.read_text(rel) or ""
    except Exception:
        return ""


def _section_values(text: str, tag: str) -> list[str]:
    """Read tick-delimited values from one PVF text section."""
    if not text:
        return []
    chunks: list[str] = []
    for match in re.finditer(r"(?im)^\s*\[" + re.escape(tag) + r"\]\s*$", text):
        tail = text[match.end() :]
        next_tag = re.search(r"(?m)^\s*\[", tail)
        body = tail[: next_tag.start()] if next_tag else tail
        chunks.extend(re.findall(r"`([^`]+)`", body))
    return [str(value).strip() for value in chunks if str(value).strip()]


@lru_cache(maxsize=1)
def load_profession_catalog() -> list[dict[str, Any]]:
    """Return the selectable base jobs and PVF-derived transfer branches."""
    catalog: list[dict[str, Any]] = []
    for job_id, source_path in sorted(_load_character_lst().items()):
        text = _read_chr_text(job_id)
        starts = list(re.finditer(r"(?im)^\s*\[growtype\s+(\d+)\]\s*$", text))
        branches: list[dict[str, Any]] = []
        for index, match in enumerate(starts):
            section_number = int(match.group(1))
            if section_number < 2:
                continue
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            block = text[match.start() : end]
            first_grow = section_number - 1
            names = _section_values(block, "growtype name")
            if not names:
                names = _section_values(block, "awakening name")
            awakenings: list[dict[str, Any]] = []
            for awakening_match in re.finditer(
                r"(?im)^\s*\[awakening\s+(\d+)\]\s*$", block
            ):
                awakening_number = int(awakening_match.group(1))
                if awakening_number <= 0:
                    continue
                awakening_end_match = re.search(
                    r"(?im)^\s*\[awakening\s+\d+\]\s*$",
                    block[awakening_match.end() :],
                )
                awakening_end = (
                    awakening_match.end() + awakening_end_match.start()
                    if awakening_end_match
                    else len(block)
                )
                awakening_block = block[awakening_match.start() : awakening_end]
                awakening_names = _section_values(awakening_block, "awakening name")
                awakenings.append(
                    {
                        "grow_number": awakening_number,
                        "name": awakening_names[-1]
                        if awakening_names
                        else "觉醒 %d" % awakening_number,
                    }
                )
            branches.append(
                {
                    "grow_number": first_grow,
                    "name": names[-1] if names else "转职分支 %d" % first_grow,
                    "awakenings": awakenings,
                }
            )
        catalog.append(
            {
                "job_id": int(job_id) & 0xFF,
                "name": BASE_JOB_NAMES.get(int(job_id), "职业 %d" % int(job_id)),
                "source_path": str(source_path),
                "branches": branches,
            }
        )
    return catalog


def _parse_skill_pairs_in_section(
    text: str,
    search_from: int,
    open_tag: str,
    close_tag: str,
    search_limit: int = -1,
) -> list[tuple[int, int]]:
    if search_limit < 0:
        search_limit = len(text)
    if search_from >= search_limit:
        return []
    low = text.lower()
    open_l = open_tag.lower()
    close_l = close_tag.lower()
    tag_start = low.find(open_l, search_from, search_limit)
    if tag_start < 0:
        return []
    tag_start += len(open_tag)
    tag_end = low.find(close_l, tag_start, search_limit)
    if tag_end < 0:
        tag_end = search_limit
    section = text[tag_start:tag_end]
    nums = [int(x) for x in re.findall(r"\d+", section)]
    out: list[tuple[int, int]] = []
    for i in range(0, len(nums) - 1, 2):
        sid = int(nums[i]) & 0xFFFF
        lv = max(0, min(255, int(nums[i + 1])))
        if sid > 0 and lv > 0:
            out.append((sid, lv))
    return out


@lru_cache(maxsize=32)
def load_chr_skill_profile(job: int) -> dict[str, Any]:
    """解析 .chr：initial + growtype[N] 送技 + awakening 送技。

    growType 索引与 S4 CharacterSkillProfile 一致：
      [growtype N] → growType = N-1（N=1 未转职档，N=2 一转分支1…）
    """
    text = _read_chr_text(job)
    profile: dict[str, Any] = {
        "initial": [],
        "growtype": {},  # grow_type_index -> [(skill_id, level)]
        "awakening": {},  # (grow_type, aw_idx) -> [(skill_id, level)]
        "source": "",
    }
    if not text:
        return profile
    paths = _load_character_lst()
    profile["source"] = paths.get(int(job) & 0xFF, "")

    iv = re.search(r"(?is)\[initial value\](.*?)(?=\[growtype\s+\d+\]|\Z)", text)
    if iv:
        profile["initial"] = _parse_skill_pairs_in_section(
            text, iv.start(), "[skill]", "[/skill]", iv.end()
        )

    gt_iter = list(re.finditer(r"(?i)\[growtype\s+(\d+)\]", text))
    for idx, gt in enumerate(gt_iter):
        n = int(gt.group(1))
        grow_type = n - 1
        seg_start = gt.end()
        seg_end = gt_iter[idx + 1].start() if idx + 1 < len(gt_iter) else len(text)
        skills = _parse_skill_pairs_in_section(
            text, seg_start, "[skill]", "[/skill]", seg_end
        )
        if skills:
            profile["growtype"][grow_type] = skills

        aw_iter = list(re.finditer(r"(?i)\[awakening\s+(\d+)\]", text[seg_start:seg_end]))
        for aw_i, aw in enumerate(aw_iter):
            aw_idx = int(aw.group(1))
            if aw_idx < 1:
                continue
            aw_start = seg_start + aw.end()
            next_aw = aw_iter[aw_i + 1].start() if aw_i + 1 < len(aw_iter) else (seg_end - seg_start)
            aw_end = seg_start + next_aw
            aw_skills = _parse_skill_pairs_in_section(
                text, aw_start, "[awakening skill]", "[/awakening skill]", aw_end
            )
            if aw_skills:
                profile["awakening"][(grow_type, aw_idx)] = aw_skills
    return profile


def get_growtype_grants(job: int, first_grow: int, second_grow: int = 0) -> list[tuple[int, int]]:
    """转职/觉醒应免费授予的技能（不含创角 initial）。"""
    profile = load_chr_skill_profile(job)
    out: list[tuple[int, int]] = []
    gt = int(first_grow)
    if gt > 0:
        out.extend(list(profile.get("growtype", {}).get(gt, []) or []))
    sec = int(second_grow)
    if sec > 0 and gt > 0:
        for aw_idx in range(1, sec + 1):
            out.extend(list(profile.get("awakening", {}).get((gt, aw_idx), []) or []))
    return out


def _assign_slots_for_skills(
    job: int,
    skill_levels: dict[int, int],
) -> list[InitialSkill]:
    """按 skill_id→level 分配槽位（主动 0+ / 被动 102+ 分配器）。"""
    from game.chain_loader import load_chain

    skill_data = load_chain("helper-skill_data.py")
    allocate_skill_slot = skill_data.allocate_skill_slot
    get_skill_static = skill_data.get_skill_static

    occupied: set[int] = set()
    out: list[InitialSkill] = []
    # 稳定顺序：skill_id 升序
    for sid in sorted(int(s) & 0xFFFF for s in skill_levels.keys()):
        lv = max(0, min(255, int(skill_levels.get(sid, 0) or 0)))
        if sid <= 0 or lv <= 0:
            continue
        sd = get_skill_static(int(job) & 0xFF, sid)
        is_active = True
        skill_class = 0
        grow_count = 1
        if sd is not None:
            is_active = bool(getattr(sd, "is_active", True))
            skill_class = int(getattr(sd, "skill_class", 0) or 0)
            grow_count = int(getattr(sd, "num_growtypes", 1) or 1)
            # 固定等级技：按当前等级抬升（若有）
            try:
                if getattr(sd, "is_fixed_level_skill", False):
                    fixed = int(sd.get_fixed_level(1) if hasattr(sd, "get_fixed_level") else lv)
                    if fixed > lv:
                        lv = fixed
            except Exception:
                pass
        else:
            # 无 skl：保守按被动区
            is_active = sid < 100
        slot = allocate_skill_slot(
            is_active=is_active,
            skill_class=skill_class,
            grow_type_count=grow_count,
            occupied=occupied,
            tree=0,
        )
        if slot < 0:
            continue
        occupied.add(int(slot))
        out.append(InitialSkill(slot=int(slot) & 0xFF, skill_id=sid, level=lv))
    return out


def build_job_change_skill_snapshot(
    job: int,
    *,
    first_grow: int,
    second_grow: int = 0,
    level: int = 1,
) -> list[InitialSkill]:
    """chainType=1 转职后完整技能快照：initial + growtype/awaken 送技。"""
    profile = load_chr_skill_profile(job)
    levels: dict[int, int] = {}
    initial = list(profile.get("initial") or [])
    if not initial:
        # 回落 JSON 创角技
        for sk in get_initial_skills_for_job(int(job) & 0xFF):
            initial.append((int(sk.skill_id), int(sk.level)))
    for sid, lv in initial:
        levels[int(sid)] = max(levels.get(int(sid), 0), int(lv))
    for sid, lv in get_growtype_grants(job, first_grow, second_grow):
        levels[int(sid)] = max(levels.get(int(sid), 0), int(lv))
    # 固定等级技按角色等级抬升
    from game.chain_loader import load_chain

    skill_data = load_chain("helper-skill_data.py")
    get_skill_static = skill_data.get_skill_static
    char_lv = max(1, min(255, int(level or 1)))
    for sid in list(levels.keys()):
        sd = get_skill_static(int(job) & 0xFF, int(sid))
        if sd is None:
            continue
        try:
            if getattr(sd, "is_fixed_level_skill", False) and hasattr(sd, "get_fixed_level"):
                fixed = int(sd.get_fixed_level(char_lv))
                if fixed > levels[sid]:
                    levels[sid] = fixed
        except Exception:
            pass
    return _assign_slots_for_skills(job, levels)


def merge_awakening_grants_into_existing(
    existing: Sequence[InitialSkill],
    job: int,
    *,
    first_grow: int,
    second_grow: int,
    level: int = 1,
) -> list[InitialSkill]:
    """chainType=2 觉醒：在现有技能上 merge 觉醒送技。"""
    levels: dict[int, int] = {}
    for sk in existing or ():
        sid = int(sk.skill_id) & 0xFFFF
        lv = max(0, int(sk.level) & 0xFF)
        if sid > 0 and lv > 0:
            levels[sid] = max(levels.get(sid, 0), lv)
    for sid, lv in get_growtype_grants(job, first_grow, second_grow):
        levels[int(sid)] = max(levels.get(int(sid), 0), int(lv))
    return _assign_slots_for_skills(job, levels)


def apply_profession_change(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    current_grow_type: int,
    chain_type: int,
    grow_number: int,
) -> JobChangeResult:
    """落库 grow_type + 技能重建/觉醒植入 + SP 重算。"""
    cid = int(character_id)
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    prev = int(current_grow_type) & 0xFF
    ct = int(chain_type)
    gn = int(grow_number) & 0xFF
    if repo is None or cid <= 0:
        return JobChangeResult(
            ok=False,
            previous_grow_type=prev,
            new_grow_type=prev,
            chain_type=ct,
            grow_number=gn,
            note="no_repo_or_cid",
        )
    if ct not in (1, 2):
        return JobChangeResult(
            ok=False,
            previous_grow_type=prev,
            new_grow_type=prev,
            chain_type=ct,
            grow_number=gn,
            note="unsupported_chain=%s" % ct,
        )
    if gn <= 0:
        return JobChangeResult(
            ok=False,
            previous_grow_type=prev,
            new_grow_type=prev,
            chain_type=ct,
            grow_number=gn,
            note="grow_number_zero",
        )

    new_grow = compute_new_grow_type(prev, chain_type=ct, grow_number=gn)
    first, second = decode_grow_type(new_grow)

    notes: list[str] = []
    # 1) grow_type
    try:
        n = int(repo.update_character_grow_type(cid, new_grow) or 0)
        notes.append("grow_db=%s->0x%02X rows=%s" % (prev, new_grow, n))
    except Exception as exc:
        return JobChangeResult(
            ok=False,
            previous_grow_type=prev,
            new_grow_type=prev,
            chain_type=ct,
            grow_number=gn,
            note="grow_db_fail:%r" % (exc,),
        )

    # 2) skills
    skills: list[InitialSkill] = []
    if ct == 1:
        skills = build_job_change_skill_snapshot(
            job_i, first_grow=first, second_grow=0, level=lv
        )
        try:
            replaced = int(
                repo.replace_character_skills(
                    cid,
                    skills,
                    page=0,
                    page_header=0,
                    clear_all_pages=True,
                )
                or 0
            )
            notes.append("skills_rebuild n=%s" % replaced)
        except Exception as exc:
            notes.append("skills_rebuild_fail:%r" % (exc,))
            skills = []
    else:
        # chain 2: merge
        try:
            rows = repo.load_character_skills(cid, page=0) or []
        except Exception:
            rows = []
        existing = [
            InitialSkill(
                slot=int(r.get("slot", 0) or 0) & 0xFF,
                skill_id=int(r.get("skill_id", 0) or 0) & 0xFFFF,
                level=max(0, int(r.get("level", 0) or 0)) & 0xFF,
            )
            for r in rows
            if int(r.get("skill_id", 0) or 0) > 0 and int(r.get("level", 0) or 0) > 0
        ]
        skills = merge_awakening_grants_into_existing(
            existing, job_i, first_grow=first, second_grow=second, level=lv
        )
        try:
            replaced = int(
                repo.replace_character_skills(
                    cid,
                    skills,
                    page=0,
                    page_header=0,
                    clear_all_pages=True,
                )
                or 0
            )
            notes.append("skills_awaken_merge n=%s" % replaced)
        except Exception as exc:
            notes.append("skills_awaken_fail:%r" % (exc,))

    # 3) SP/TP：一转全额重算；觉醒保留事务前的持久化余额。
    remain_sp = 0
    remain_tp = 0
    if ct == 1:
        from game.chain_loader import load_chain

        sp = load_chain("helper-skill_points.py")
        total_sp = int(sp.total_sp_for_level(lv, 0))
        total_tp = int(sp.total_tp_for_level(lv, 0))
        # 转职快照内全部为免费送技（initial + .chr growtype），spent 相对该底线应为 0
        try:
            skill_data = load_chain("helper-skill_data.py")
            pairs = [(int(s.skill_id), int(s.level)) for s in skills]
            free_floor = {int(s.skill_id) & 0xFFFF: int(s.level) & 0xFF for s in skills}
            spent_sp, spent_tp = skill_data.compute_spent_points(
                job_i, pairs, free_floor=free_floor
            )
        except Exception:
            spent_sp, spent_tp = 0, 0
        remain_sp = max(0, total_sp - int(spent_sp))
        remain_tp = max(0, total_tp - int(spent_tp))
        try:
            repo.upsert_character_skill_points(
                cid,
                total_sp=total_sp,
                remaining_sp=remain_sp,
                total_tp=total_tp,
                remaining_tp=remain_tp,
                synced_level=lv,
            )
            notes.append(
                "points_reset sp=%s/%s spent_sp=%s tp=%s/%s spent_tp=%s"
                % (remain_sp, total_sp, spent_sp, remain_tp, total_tp, spent_tp)
            )
        except Exception as exc:
            notes.append("points_reset_fail:%r" % (exc,))
            remain_sp = total_sp
            remain_tp = total_tp
    else:
        try:
            ledger = repo.get_character_skill_points(cid) or {}
            remain_sp = int(ledger.get("remaining_sp") or 0)
            remain_tp = int(ledger.get("remaining_tp") or 0)
        except Exception:
            remain_sp = 0
            remain_tp = 0
        notes.append("points_keep sp=%s tp=%s" % (remain_sp, remain_tp))

    return JobChangeResult(
        ok=True,
        previous_grow_type=prev,
        new_grow_type=new_grow,
        chain_type=ct,
        grow_number=gn,
        skills=tuple(skills),
        remaining_sp=int(remain_sp),
        remaining_tp=int(remain_tp),
        note=";".join(notes),
    )


def apply_base_job_change(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    current_grow_type: int = 0,
) -> JobChangeResult:
    """Switch to an unadvanced base job and rebuild its starter skill tree."""
    cid = int(character_id)
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    previous_grow = int(current_grow_type) & 0xFF
    if repo is None or cid <= 0:
        return JobChangeResult(False, previous_grow, previous_grow, 0, 0, note="no_repo_or_cid")
    try:
        rows = int(
            repo.update_character_job_and_grow_type(
                cid,
                job=job_i,
                grow_type=0,
            )
            or 0
        )
        if rows <= 0:
            return JobChangeResult(False, previous_grow, previous_grow, 0, 0, note="character_missing")
        skills = build_job_change_skill_snapshot(
            job_i,
            first_grow=0,
            second_grow=0,
            level=lv,
        )
        repo.replace_character_skills(
            cid,
            skills,
            page=0,
            page_header=0,
            clear_all_pages=True,
        )
        from game.chain_loader import load_chain

        points = load_chain("helper-skill_points.py")
        skill_data = load_chain("helper-skill_data.py")
        total_sp = int(points.total_sp_for_level(lv, 0))
        total_tp = int(points.total_tp_for_level(lv, 0))
        pairs = [(int(skill.skill_id), int(skill.level)) for skill in skills]
        free_floor = {int(skill.skill_id): int(skill.level) for skill in skills}
        spent_sp, spent_tp = skill_data.compute_spent_points(
            job_i,
            pairs,
            free_floor=free_floor,
        )
        remaining_sp = max(0, total_sp - int(spent_sp))
        remaining_tp = max(0, total_tp - int(spent_tp))
        repo.upsert_character_skill_points(
            cid,
            total_sp=total_sp,
            remaining_sp=remaining_sp,
            total_tp=total_tp,
            remaining_tp=remaining_tp,
            synced_level=lv,
        )
        return JobChangeResult(
            True,
            previous_grow,
            0,
            0,
            0,
            skills=tuple(skills),
            remaining_sp=remaining_sp,
            remaining_tp=remaining_tp,
            note="job_db=%s skills=%s points=%s/%s"
            % (rows, len(skills), remaining_sp, remaining_tp),
        )
    except Exception as exc:
        return JobChangeResult(
            False,
            previous_grow,
            previous_grow,
            0,
            0,
            note="base_job_change_fail:%r" % (exc,),
        )


def pack_job_change_skillinfo(
    server: Any,
    *,
    skills: Sequence[InitialSkill],
    remaining_sp: int,
    remaining_tp: int,
) -> bytes:
    """S0/0013 全量技能表（转职后替换客户端技能管理器）。"""
    body = build_skillinfo_from_pvf(
        list(skills or ()),
        page0_sp=int(remaining_sp) & 0xFFFF,
        page1_sp=int(remaining_sp) & 0xFFFF,
        page0_tp=int(remaining_tp) & 0xFFFF,
        page1_tp=int(remaining_tp) & 0xFFFF,
        mirror_page0_to_page1=True,
    )
    return server._codec_response(0, 0x0013, body)


def pack_job_change_userinfo0(
    server: Any,
    *,
    record: Any,
    equipment: Sequence[Any] | None = None,
) -> bytes:
    """S0/0002 USERINFO subtype0：新 grow_type 外观/职业信息。"""
    guild_id = 0
    guild_name = ""
    try:
        repo = getattr(server, "character_repo", None)
        if repo is None:
            repo = server._repo()
        db_path = getattr(repo, "db_path", None)
        if db_path is not None:
            guild = GuildRepository(db_path).get_guild_for_character(
                int(getattr(record, "character_id", 0) or 0)
            )
            if guild is not None:
                guild_id = int(guild.guild_id)
                guild_name = guild.display_name
    except Exception:
        pass
    body = build_userinfo_subtype0_min(
        record,
        equipment,
        guild_name=guild_name,
        guild_id=guild_id,
    )
    return server._codec_response(0, 0x0002, body)


def pack_job_change_avatar_visibility(server: Any, *, record: Any) -> bytes:
    """Restore persisted grow/avatar visibility after USERINFO0 actor rebuild."""
    visibility_helper = load_chain("helper-avatar_visibility.py")
    return visibility_helper.pack_avatar_visibility(server, record)


def pack_job_change_userinfo1(
    server: Any,
    *,
    repo: Any,
    record: Any,
    skills: Sequence[InitialSkill],
) -> bytes:
    """S0/0002 USERINFO subtype1: rebuild the live actor after a job change."""
    from game.character.adventure_group import (
        calculate_adventure_group_for_character,
    )
    from game.chain_loader import load_chain

    equipment_helper = load_chain("helper-userinfo_equipment.py")
    completion_helper = load_chain("helper-quest_completion_snapshot.py")
    equipment, _equipment_note = equipment_helper.resolve_enter_room_equipment(
        record,
        enabled=True,
        backfill_from_pvf=False,
        repo=repo,
        prefer_live_db=True,
    )
    quest_snapshot = completion_helper.load_finished_quest_snapshot(
        repo,
        int(record.character_id),
    )
    adventure_progression = calculate_adventure_group_for_character(
        repo,
        record,
    )
    guild_membership_active = has_active_guild_membership(repo, record)
    body = build_userinfo_subtype1_min(
        record,
        exp=max(0, int(getattr(record, "exp", 0) or 0)),
        skills=skills,
        equipment=equipment,
        finished_quest_ids=quest_snapshot.finished_quest_ids,
        adventure_group_level=int(adventure_progression.adventure_level),
        guild_membership_active=guild_membership_active,
    )
    return server._codec_response(0, 0x0002, body)
