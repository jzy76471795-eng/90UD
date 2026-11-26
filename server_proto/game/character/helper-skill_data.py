"""从共享预加载 PVF 解析职业技能静态数据（学技能用）。

路径：skill/skilllist.lst → skill/{JobSkill.lst} → skill/{rel.skl}
仅用 pvf_runtime 共享 reader，禁止业务路径冷开 PVF。

字段对齐 90qiyuan skill.Skill / S4A12 SkillStaticData 子集。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Sequence

from pvf import pvf_runtime

_SKILL_LIST_PATHS = (
    "skill/skilllist.lst",
    "Skill/skilllist.lst",
)


@dataclass(frozen=True)
class SkillStaticData:
    job: int
    skill_id: int
    path: str = ""
    name: str = ""
    is_active: bool = False
    # Legacy scalar view of [maximum level] index 0. Runtime gates must use
    # maximum_level_for_grow() because Stage3 PVF can provide one value per
    # first grow type.
    max_level: int = 1
    maximum_levels: tuple[int, ...] = ()
    required_level: int = 0
    level_interval: int = 1
    skill_class: int = 0
    is_tp_skill: bool = False
    is_special: bool = False  # skill_id 200..208 → ACK slot 0xFF
    growtype_max_levels: tuple[int, ...] = ()
    purchase_cost: tuple[int, ...] = ()
    special_purchase_cost: tuple[int, ...] = ()
    pre_required: tuple[tuple[int, int], ...] = ()  # (skill_id, level)*
    grow_types: tuple[int, ...] = ()
    combo_set_skill_ids: tuple[int, ...] = ()

    def maximum_level_for_grow(self, grow_type: int = 0) -> int:
        """Return the [maximum level] stacking cap for the first grow type."""
        gt = max(0, int(grow_type) & 0x0F)
        values = self.maximum_levels
        if values:
            # A single value is a global cap. Multi-value sections use
            # 0=base class and 1..5=first advancement.
            if len(values) == 1:
                return max(0, int(values[0]))
            if gt < len(values):
                return max(0, int(values[gt]))
            return 0
        return max(1, int(self.max_level or 1))

    def max_level_for_grow(self, grow_type: int = 0) -> int:
        """Return the [growtype maximum level] learnable cap."""
        gt = max(0, int(grow_type) & 0x0F)
        if self.growtype_max_levels:
            if gt < len(self.growtype_max_levels):
                v = int(self.growtype_max_levels[gt])
                if v > 0:
                    return v
            # 表存在但该方向 0 = 不可学
            if any(int(x) > 0 for x in self.growtype_max_levels):
                # 若 grow 越界，取 0
                if gt >= len(self.growtype_max_levels):
                    return 0
                return max(0, int(self.growtype_max_levels[gt]))
        return self.maximum_level_for_grow(gt)

    def cost_for(self, from_level: int, to_level: int) -> int:
        """从 from_level 升到 to_level 的 SP/TP 总消耗（不含免费初始段外的负向）。"""
        a = max(0, int(from_level))
        b = max(0, int(to_level))
        if b <= a:
            return 0
        costs = self.special_purchase_cost if self.is_tp_skill else self.purchase_cost
        if not costs:
            # 缺表时保守 1 点/级，避免免费狂点
            return b - a
        total = 0
        for lv in range(a, b):
            idx = lv if lv < len(costs) else len(costs) - 1
            total += max(0, int(costs[idx]))
        return total

    def required_level_for_target(self, target_level: int) -> int:
        """reqLevel + (target-1)*interval。"""
        t = max(1, int(target_level))
        base = max(0, int(self.required_level))
        interval = max(1, int(self.level_interval or 1))
        return base + (t - 1) * interval


def _parse_lst_pairs(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not text:
        return out
    for m in re.finditer(r"(-?\d+)\s*`([^`]*)`", text):
        try:
            sid = int(m.group(1))
        except ValueError:
            continue
        if sid < 0:
            continue
        out[sid] = m.group(2).strip().replace("\\", "/")
    return out


def _section_body(text: str, tag: str) -> str | None:
    """取 [tag] ... [/tag] 或 [tag] 后到下一 [ 的正文。"""
    if not text:
        return None
    low = text.lower()
    open_t = "[%s]" % tag.lower()
    close_t = "[/%s]" % tag.lower()
    start = low.find(open_t)
    if start < 0:
        return None
    start += len(open_t)
    end = low.find(close_t, start)
    if end < 0:
        # 无闭合：到下一 [
        nxt = low.find("[", start)
        end = nxt if nxt >= 0 else len(text)
    return text[start:end]


def _ints(s: str | None) -> tuple[int, ...]:
    if not s:
        return ()
    return tuple(int(x) for x in re.findall(r"-?\d+", s))


def _first_int(s: str | None, default: int = 0) -> int:
    vals = _ints(s)
    return int(vals[0]) if vals else int(default)


def _strip_bt(s: str | None) -> str:
    if not s:
        return ""
    return s.strip().strip("`").strip()


@lru_cache(maxsize=1)
def load_job_skill_list_map() -> dict[int, str]:
    """job_id → job skill lst 相对名（如 Knight.lst）。"""
    for path in _SKILL_LIST_PATHS:
        try:
            text = pvf_runtime.read_text(path)
        except Exception:
            text = None
        if text and str(text).strip():
            return _parse_lst_pairs(str(text))
    return {}


@lru_cache(maxsize=32)
def load_job_skill_paths(job: int) -> dict[int, str]:
    """skill_id → skl 相对路径（相对 skill/）。"""
    job_i = int(job) & 0xFF
    job_map = load_job_skill_list_map()
    lst_name = job_map.get(job_i)
    if not lst_name:
        return {}
    rel = lst_name.replace("\\", "/").lstrip("/")
    candidates = (
        "skill/%s" % rel,
        "Skill/%s" % rel,
        "skill/%s" % rel.lower(),
    )
    text = None
    for path in candidates:
        try:
            text = pvf_runtime.read_text(path)
        except Exception:
            text = None
        if text and str(text).strip():
            break
    if not text:
        return {}
    return _parse_lst_pairs(str(text))


def _read_skl_text(rel: str) -> str:
    rel_n = (rel or "").replace("\\", "/").lstrip("/")
    if not rel_n:
        return ""
    # 已含 skill/ 前缀则直接试
    candidates = []
    if rel_n.lower().startswith("skill/"):
        candidates.append(rel_n)
        candidates.append(rel_n.lower())
    else:
        candidates.extend(
            [
                "skill/%s" % rel_n,
                "Skill/%s" % rel_n,
                "skill/%s" % rel_n.lower(),
            ]
        )
    for path in candidates:
        try:
            text = pvf_runtime.read_text(path)
        except Exception:
            text = None
        if text and str(text).strip():
            return str(text)
    return ""


def _parse_is_active(text: str) -> bool:
    """判定主动技。

    USDOF skl 真形（换行 + 反引号包方括号）::
      [type]
      `[active]`
      [skill class]
    注意 ``[active]`` 内的 ``[`` 不能当段结束符。
    """
    if not text:
        return False
    # 独立 [type] 标签（前后非字母，避免 feature skill type）
    m = re.search(
        r"(?<![a-z ])\[type\](?![a-z])\s*[`\r\n\t \[]*(active|passive)",
        text,
        flags=re.I,
    )
    if m:
        return m.group(1).lower() == "active"
    return False


def _parse_skl(job: int, skill_id: int, rel: str, text: str) -> SkillStaticData:
    is_active = _parse_is_active(text)
    name = _strip_bt(_section_body(text, "name") or "")
    maximum_levels = _ints(_section_body(text, "maximum level"))
    max_level = max(1, int(maximum_levels[0]) if maximum_levels else 1)
    req_lv = max(0, _first_int(_section_body(text, "required level"), 0))
    interval = max(1, _first_int(_section_body(text, "required level range"), 1))
    skill_class = _first_int(_section_body(text, "skill class"), 0)
    grow_max = _ints(_section_body(text, "growtype maximum level"))
    purchase = _ints(_section_body(text, "purchase cost"))
    special_purchase = _ints(_section_body(text, "special purchase cost"))
    feature = _strip_bt(_section_body(text, "feature skill type") or "")
    is_tp = bool(feature and feature not in {"0", ""}) or bool(special_purchase)
    fitness = _ints(_section_body(text, "skill fitness growtype"))
    pre_raw = _ints(_section_body(text, "pre required skill"))
    pre: list[tuple[int, int]] = []
    for i in range(0, len(pre_raw) - 1, 2):
        pre.append((int(pre_raw[i]) & 0xFFFF, max(0, int(pre_raw[i + 1]))))
    combo_set = tuple(
        int(value) & 0xFFFF
        for value in _ints(_section_body(text, "combo set"))
        if int(value) & 0xFFFF
    )
    sid = int(skill_id) & 0xFFFF
    return SkillStaticData(
        job=int(job) & 0xFF,
        skill_id=sid,
        path=rel,
        name=name,
        is_active=is_active,
        max_level=max_level,
        maximum_levels=maximum_levels,
        required_level=req_lv,
        level_interval=interval,
        skill_class=skill_class,
        is_tp_skill=is_tp,
        is_special=200 <= sid <= 208,
        growtype_max_levels=grow_max,
        purchase_cost=purchase,
        special_purchase_cost=special_purchase,
        pre_required=tuple(pre),
        grow_types=fitness,
        combo_set_skill_ids=combo_set,
    )


@lru_cache(maxsize=2048)
def get_skill_static(job: int, skill_id: int) -> SkillStaticData | None:
    """job + skill_id → 静态数据；找不到返回 None。"""
    job_i = int(job) & 0xFF
    sid = int(skill_id) & 0xFFFF
    if sid <= 0:
        return None
    paths = load_job_skill_paths(job_i)
    rel = paths.get(sid)
    if not rel:
        return None
    text = _read_skl_text(rel)
    if not text:
        return None
    return _parse_skl(job_i, sid, rel, text)


def free_baseline_levels(job: int, grow_type: int = 0) -> dict[int, int]:
    """免费底线等级（不可退点到此之下）。

    对齐 S4A12 SkillPointLedger.BuildFreeBaseline：
      1) 创角 initial（JSON / .chr [initial value]）
      2) 转职/觉醒 .chr 送技（growtype + awakening）

    同 skill_id 取最大免费等级。转职后送的被动等因此不能取消学习。

    **TP 技能不进免费底线**（用户确认 2026-07-21）：
      强化系 TP 技可随时降级 / 清空，初始化时也一律清空。
    """
    from game.character.initial_skills import get_initial_skills_for_job

    out: dict[int, int] = {}
    job_i = int(job) & 0xFF
    for sk in get_initial_skills_for_job(job_i):
        sid = int(sk.skill_id) & 0xFFFF
        lv = max(0, int(sk.level) & 0xFF)
        if sid > 0 and lv > 0:
            out[sid] = max(out.get(sid, 0), lv)

    g = int(grow_type) & 0xFF
    first = g & 0x0F
    second = (g >> 4) & 0x0F
    if first > 0 or second > 0:
        try:
            from game.chain_loader import load_chain

            jc = load_chain("helper-job_change.py")
            grants = jc.get_growtype_grants(job_i, first, second) or ()
            for sid, lv in grants:
                sid_i = int(sid) & 0xFFFF
                lv_i = max(0, int(lv) & 0xFF)
                if sid_i > 0 and lv_i > 0:
                    out[sid_i] = max(out.get(sid_i, 0), lv_i)
        except Exception:
            pass

    # 剔除 TP 技能：可退到 0，不占免费底线
    if out:
        drop: list[int] = []
        for sid in out:
            try:
                sd = get_skill_static(job_i, int(sid))
            except Exception:
                sd = None
            if sd is not None and bool(getattr(sd, "is_tp_skill", False)):
                drop.append(int(sid))
        for sid in drop:
            out.pop(sid, None)
    return out


def skill_growth_grants(job: int, grow_type: int) -> set[int]:
    from game.chain_loader import load_chain

    grants = set(free_baseline_levels(job, grow_type))
    profile = load_chain("helper-job_change.py").load_chr_skill_profile(job)
    grants.update(int(skill_id) for skill_id, level in profile.get("initial", ()) if level > 0)
    return grants


def skill_allowed_for_growth(job: int, grow_type: int, skill_id: int, grants: set[int]) -> bool:
    """Allow native starter/granted abilities and skills learnable by this branch."""
    if skill_id in grants:
        return True
    skill = get_skill_static(job, skill_id)
    first_grow = int(grow_type) & 0x0F
    return (skill is not None and skill.max_level_for_grow(first_grow) > 0
            and skill.maximum_level_for_grow(first_grow) > 0)


def allocate_skill_slot(
    *,
    is_active: bool,
    skill_class: int,
    grow_type_count: int,
    occupied: set[int],
    tree: int = 0,
) -> int:
    """90qiyuan allocateSkillSlot / S4A12 SkillSlotAllocator。

    主动技 tree0：优先 0..5，再 198..203。
    被动：按 group 落 6/54/102/150 起 48 槽。
    """
    if int(tree) == 0 and is_active:
        for s in range(0, 6):
            if s not in occupied:
                return s
        for s in range(198, 204):
            if s not in occupied:
                return s
    # group: active→3, class4→2, growtypes<=2→1, else 0
    if is_active:
        group = 3
    elif int(skill_class) == 4:
        group = 2
    elif int(grow_type_count) <= 2:
        group = 1
    else:
        group = 0
    starts = (6, 54, 102, 150)
    start = starts[group] if 0 <= group < len(starts) else 6
    for s in range(start, start + 48):
        if s not in occupied:
            return s
    # 兜底全表
    for s in range(0, 204):
        if s not in occupied and not (138 <= s < 150):
            return s
    return -1


def compute_spent_points(
    job: int,
    skills: Sequence[tuple[int, int]],
    *,
    free_floor: dict[int, int] | None = None,
) -> tuple[int, int]:
    """已学技能相对免费底线的 SP/TP 消耗。skills=(skill_id, level)*。"""
    floor = free_floor if free_floor is not None else free_baseline_levels(job)
    spent_sp = 0
    spent_tp = 0
    for skill_id, level in skills:
        sid = int(skill_id) & 0xFFFF
        lv = max(0, int(level))
        if sid <= 0 or lv <= 0:
            continue
        base = int(floor.get(sid, 0) or 0)
        if lv <= base:
            continue
        sd = get_skill_static(job, sid)
        if sd is None:
            # 无 PVF 行：不计费（.chr 赠送 / 缺失定义）
            continue
        cost = sd.cost_for(base, lv)
        if sd.is_tp_skill:
            spent_tp += cost
        else:
            spent_sp += cost
    return spent_sp, spent_tp
