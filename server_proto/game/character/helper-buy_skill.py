"""学技能业务（C1/0x001D BUY_SKILL）。

对照：
  - 90qiyuan skillcmd.ApplyBuy + BuildBuySkillSuccess
  - S4A12 BuySkillService + BuySkillAckBuilder
  - Stage3 live: flag=1 opcode=0x001D plain= tree,count,{u16 id,u8 refund,u8 delta}*

规则：
  - 扣 SP/TP 并写入 character_skill_points（持久化）
  - is_refund=1：降级 / 取消学习，100% 返还费用表 SP/TP
  - 免费底线（创角 + 转职/觉醒 .chr 送技）不可退到其下（初始化=批量退到此底线）
  - **TP 技能**（is_tp_skill）：无免费底线，可降级到 0 并全额返 TP
  - 等级归 0：删 DB 行；若在快捷栏 (0..5 / 198..203) 则腾空该槽
  - 新学主动技自动分配技能栏空位 0..5 / 198..203
  - 新学被动进树分组槽；升级保留原槽
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from game.chain_loader import load_chain

_skill_data = load_chain("helper-skill_data.py")
allocate_skill_slot = _skill_data.allocate_skill_slot
free_baseline_levels = _skill_data.free_baseline_levels
get_skill_static = _skill_data.get_skill_static

_sp = load_chain("helper-skill_points.py")
ensure_skill_points_ledger = _sp.ensure_skill_points_ledger

_skills_h = load_chain("helper-character_skills.py")
resolve_skills_for_character = _skills_h.resolve_skills_for_character

# 技能快捷栏槽（主动技默认落位）；等级归 0 时必须腾空，避免幽灵图标
_QUICK_BAR_SLOTS = frozenset(list(range(0, 6)) + list(range(198, 204)))


@dataclass
class BuySkillEntry:
    skill_id: int
    is_refund: int
    level_delta: int


@dataclass
class BuySkillResultEntry:
    slot: int
    skill_id: int
    level: int
    has_cmd: bool = False
    # 释放命令字节（C1/014B）；has_cmd=True 时写入 001D ACK
    command_bytes: bytes = b""


@dataclass
class BuySkillResult:
    success: bool
    skill_tree: int = 0
    remain_sp: int = 0
    remain_tp: int = 0
    entries: list[BuySkillResultEntry] = field(default_factory=list)
    error_code: int = 0
    final_mode: int = 0
    note: str = ""


def parse_buy_skill_body(body: bytes) -> dict[str, Any] | None:
    """解析 C1/001D；宽松长度（live 尾部有 padding）。

    wire: u8 tree, u8 count, count*(u16 skill_id, u8 refund, u8 delta) [, u8 mode]
    """
    data = bytes(body or b"")
    if len(data) < 2:
        return None
    tree = 1 if data[0] == 1 else 0
    count = int(data[1]) & 0xFF
    need = 2 + count * 4
    if len(data) < need:
        return None
    entries: list[BuySkillEntry] = []
    off = 2
    for _ in range(count):
        sid = int.from_bytes(data[off : off + 2], "little") & 0xFFFF
        refund = int(data[off + 2]) & 0xFF
        delta = int(data[off + 3]) & 0xFF
        entries.append(BuySkillEntry(skill_id=sid, is_refund=refund, level_delta=delta))
        off += 4
    final_mode = int(data[off]) & 0xFF if off < len(data) else 0
    return {
        "skill_tree": tree,
        "count": count,
        "entries": entries,
        "final_mode": final_mode,
        "raw_len": len(data),
    }


def build_buy_skill_ack_ok(
    *,
    skill_tree: int,
    remain_sp: int,
    remain_tp: int,
    entries: Sequence[BuySkillResultEntry],
    final_mode: int = 0,
    include_final_mode: bool = True,
) -> bytes:
    """S1/001D 成功 ACK。

    90qiyuan: success, tree, sp, tp, count,
      entries*(slot, id, lv, has_cmd[, cmd_len, cmd...]), mode
    has_cmd=1 时后跟 u8 cmd_len + cmd 字节（释放命令）。
    """
    body = bytearray()
    body.append(0x01)
    body.append(int(skill_tree) & 0xFF)
    body += (int(remain_sp) & 0xFFFF).to_bytes(2, "little")
    body += (int(remain_tp) & 0xFFFF).to_bytes(2, "little")
    ents = list(entries or ())
    if len(ents) > 255:
        ents = ents[:255]
    body.append(len(ents) & 0xFF)
    for e in ents:
        body.append(int(e.slot) & 0xFF)
        body += (int(e.skill_id) & 0xFFFF).to_bytes(2, "little")
        body.append(int(e.level) & 0xFF)
        cmd = getattr(e, "command_bytes", b"") or b""
        if not isinstance(cmd, (bytes, bytearray, memoryview)):
            cmd = b""
        else:
            cmd = bytes(cmd)
        has_cmd = bool(getattr(e, "has_cmd", False)) or bool(cmd)
        if has_cmd and cmd:
            if len(cmd) > 255:
                cmd = cmd[:255]
            body.append(0x01)
            body.append(len(cmd) & 0xFF)
            body += cmd
        else:
            body.append(0x00)
    if include_final_mode:
        body.append(int(final_mode) & 0xFF)
    return bytes(body)


def build_buy_skill_ack_fail(error_code: int = 1) -> bytes:
    return bytes([0x00, int(error_code) & 0xFF])


def _normalize_delta(raw_delta: int) -> int:
    """S4A12：Level<=0 视为 1 级。"""
    d = int(raw_delta) & 0xFF
    return 1 if d <= 0 else d


def _is_quick_bar_slot(slot: int) -> bool:
    return (int(slot) & 0xFF) in _QUICK_BAR_SLOTS


def _reverse_prereq_floor(
    job: int,
    skill_id: int,
    projected_levels: dict[int, int],
) -> int:
    """其它仍保留的技能对本技能要求的最低等级。

    例：B 要求 A≥5 且 B 在 projected 中 level>0 → A 不能退到 5 以下。
    初始化批量退点时，若 B 也在本包被退到 0，则 projected 中 B=0，不再卡住 A。
    """
    sid = int(skill_id) & 0xFFFF
    job_i = int(job) & 0xFF
    need = 0
    for other_id, other_lv in projected_levels.items():
        oid = int(other_id) & 0xFFFF
        olv = int(other_lv or 0)
        if oid == sid or olv <= 0:
            continue
        sd = get_skill_static(job_i, oid)
        if sd is None or not sd.pre_required:
            continue
        for pre_id, pre_lv in sd.pre_required:
            if (int(pre_id) & 0xFFFF) == sid:
                need = max(need, max(0, int(pre_lv)))
    return need


def _project_levels_after_batch(
    *,
    job: int,
    first_grow: int,
    char_level: int,
    work_levels: dict[int, int],
    free_floor: dict[int, int],
    entries: Sequence[BuySkillEntry],
) -> dict[int, int]:
    """预演整包学/退后的等级（仅等级逻辑，不计 SP）。

    用于反向前置：批量初始化时先看最终谁还在，再决定 A 能退多低。
    """
    projected = {int(k) & 0xFFFF: int(v) for k, v in work_levels.items() if int(v) > 0}
    job_i = int(job) & 0xFF
    for req in entries:
        sid = int(req.skill_id) & 0xFFFF
        if sid <= 0:
            continue
        delta = _normalize_delta(req.level_delta)
        cur = int(projected.get(sid, 0) or 0)
        if int(req.is_refund) != 0:
            if cur <= 0:
                continue
            sd_pre = get_skill_static(job_i, sid)
            # TP 技能可退到 0，不走免费底线
            if sd_pre is not None and getattr(sd_pre, "is_tp_skill", False):
                base_lv = 0
            else:
                base_lv = max(0, int(free_floor.get(sid, 0) or 0))
            new_lv = max(base_lv, cur - delta)
            if new_lv <= 0:
                projected.pop(sid, None)
            else:
                projected[sid] = new_lv
            continue
        sd = get_skill_static(job_i, sid)
        if sd is None:
            continue
        grow_max = sd.max_level_for_grow(first_grow)
        if grow_max <= 0:
            continue
        maximum_level = sd.maximum_level_for_grow(first_grow)
        if maximum_level <= 0:
            continue
        hard_max = min(maximum_level, grow_max)
        new_lv = min(hard_max, cur + delta)
        if new_lv <= cur:
            continue
        # 投影也尊重等级/前置，避免错误抬高依赖链
        if sd.required_level_for_target(new_lv) > int(char_level):
            continue
        pre_ok = True
        for pre_id, pre_lv in sd.pre_required:
            if int(projected.get(int(pre_id) & 0xFFFF, 0) or 0) < int(pre_lv):
                pre_ok = False
                break
        if not pre_ok:
            continue
        projected[sid] = new_lv
    return projected


def execute_buy_skill(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    grow_type: int = 0,
    skill_tree: int = 0,
    entries: Sequence[BuySkillEntry],
    final_mode: int = 0,
    bonus_sp: Optional[int] = None,
    bonus_tp: Optional[int] = None,
    level_gate_bonus: int = 0,
) -> BuySkillResult:
    """执行学技能 / 退点；成功则落库 skills + skill_points。

    is_refund=0：学习 / 升级（扣 SP/TP）。
    is_refund=1：降级 / 取消（返 SP/TP）；不低于免费底线。
    初始化技能 UI：客户端对可退技能批量 is_refund=1，效果即退到底线并返点。

    前置反向约束（PVF [pre required skill]）：
      若 B 要求 A≥N 且 B 仍保留，则 A 不能退到 N 以下。
      同包批量退点时按整包最终 projected 等级判断（B 一并退到 0 则放开 A）。
    """
    cid = int(character_id)
    job_i = int(job) & 0xFF
    lv = max(1, min(255, int(level or 1)))
    gate_bonus = max(0, min(255, int(level_gate_bonus or 0)))
    gate_level = min(255, lv + gate_bonus)
    tree = 1 if int(skill_tree) == 1 else 0
    grow = int(grow_type) & 0xFF
    first_grow = grow & 0x0F

    if cid <= 0 or repo is None:
        return BuySkillResult(success=False, error_code=1, note="no_cid_or_repo")
    if not entries:
        return BuySkillResult(success=False, error_code=1, note="empty_entries")

    # 确保技能行 + SP 账本
    resolve_skills_for_character(repo, cid, job_i, seed_if_empty=True)

    ledger = ensure_skill_points_ledger(
        repo,
        character_id=cid,
        job=job_i,
        level=lv,
        bonus_sp=bonus_sp,
        bonus_tp=bonus_tp,
    )
    remain_sp = int(ledger["remaining_sp"])
    remain_tp = int(ledger["remaining_tp"])
    total_sp = int(ledger["total_sp"])
    total_tp = int(ledger["total_tp"])

    # 创角 + 转职/觉醒送技：不可退到此之下
    free_floor = free_baseline_levels(job_i, grow)

    rows = repo.load_character_skills(cid, page=tree)
    by_id: dict[int, dict[str, Any]] = {}
    occupied: set[int] = set()
    cmd_by_sid: dict[int, bytes] = {}
    # 独立命令库优先（01EC 后未重学的技能命令也在）
    try:
        if hasattr(repo, "load_character_skill_commands"):
            for sid_c, blob_c in (repo.load_character_skill_commands(cid) or {}).items():
                sid_i = int(sid_c) & 0xFFFF
                if sid_i > 0 and blob_c:
                    cmd_by_sid[sid_i] = bytes(blob_c)
    except Exception:
        pass
    for row in rows:
        sid = int(row["skill_id"]) & 0xFFFF
        slot = int(row["slot"]) & 0xFF
        by_id[sid] = row
        occupied.add(slot)
        if sid in cmd_by_sid:
            continue
        extra = row.get("extra_values") if isinstance(row, dict) else None
        if extra is None and not isinstance(row, dict):
            try:
                extra = row["extra_values"]
            except Exception:
                extra = None
        if extra is not None:
            try:
                cmd_by_sid[sid] = bytes(extra)
            except Exception:
                pass

    result_entries: list[BuySkillResultEntry] = []
    # level=0 的条目：落库时删行；ACK 仍带旧 slot 让客户端清快捷栏
    removed_skill_ids: set[int] = set()
    work_levels: dict[int, int] = {
        sid: int(r["level"]) for sid, r in by_id.items()
    }
    work_slots: dict[int, int] = {
        sid: int(r["slot"]) & 0xFF for sid, r in by_id.items()
    }

    def _entry_with_cmd(slot: int, skill_id: int, level: int) -> BuySkillResultEntry:
        """学/降级 ACK 带上已持久化的释放命令（有则 has_cmd）。"""
        cmd = cmd_by_sid.get(int(skill_id) & 0xFFFF, b"")
        if level <= 0:
            cmd = b""
        return BuySkillResultEntry(
            slot=int(slot) & 0xFF,
            skill_id=int(skill_id) & 0xFFFF,
            level=int(level) & 0xFF,
            has_cmd=bool(cmd),
            command_bytes=cmd or b"",
        )

    # 整包预演等级（冻结）：反向前置依赖「本包最终谁还在」
    # 初始化批量退点时，若 B 也在本包被退到 0，则不再卡住 A。
    final_projected = _project_levels_after_batch(
        job=job_i,
        first_grow=first_grow,
        char_level=gate_level,
        work_levels=work_levels,
        free_floor=free_floor,
        entries=entries,
    )

    for req in entries:
        sid = int(req.skill_id) & 0xFFFF
        if sid <= 0:
            continue
        delta = _normalize_delta(req.level_delta)
        is_refund = int(req.is_refund) != 0

        if is_refund:
            cur = int(work_levels.get(sid, 0) or 0)
            if cur <= 0:
                continue
            sd = get_skill_static(job_i, sid)
            # TP 技能：免费底线=0，允许降级到 0（100% 返 TP）
            is_tp = bool(sd is not None and getattr(sd, "is_tp_skill", False))
            base_lv = 0 if is_tp else max(0, int(free_floor.get(sid, 0) or 0))
            # 反向前置：其它仍保留技能对本技能的最低要求
            # 例 B 要求 A≥5 且 B 最终仍 >0 → A 不能退到 5 以下
            # TP 强化技通常无前置链，仍统一算 floor
            dep_floor = _reverse_prereq_floor(job_i, sid, final_projected)
            floor_lv = max(base_lv, dep_floor)
            new_lv = cur - delta
            if new_lv < floor_lv:
                new_lv = floor_lv
            if new_lv >= cur:
                # 已在免费底线 / 前置依赖线：忽略，不报错
                continue

            # 有 skl 则按费用表 100% 返还；无表时不瞎返点
            if sd is not None:
                refund_pts = sd.cost_for(new_lv, cur)
                if sd.is_tp_skill:
                    remain_tp += refund_pts
                else:
                    remain_sp += refund_pts
            else:
                # 无 PVF 定义但仍在 DB：允许清到 0/底线，不计费返还
                pass

            old_slot = int(work_slots.get(sid, 0xFF)) & 0xFF
            if new_lv <= 0:
                # 完全取消：删技能 + 腾出快捷栏槽
                work_levels.pop(sid, None)
                if sid in work_slots:
                    old_slot = int(work_slots.pop(sid)) & 0xFF
                    occupied.discard(old_slot)
                removed_skill_ids.add(sid)
                ack_slot = (
                    0xFF
                    if (sd is not None and sd.is_special)
                    else old_slot
                )
                result_entries.append(_entry_with_cmd(ack_slot, sid, 0))
            else:
                work_levels[sid] = new_lv
                # 若仍有等级但曾在快捷栏：保留槽（仅降级）
                if sid not in work_slots:
                    work_slots[sid] = old_slot
                ack_slot = (
                    0xFF
                    if (sd is not None and sd.is_special)
                    else (int(work_slots[sid]) & 0xFF)
                )
                result_entries.append(_entry_with_cmd(ack_slot, sid, new_lv))
            continue

        # ---- 学习 / 升级 ----
        sd = get_skill_static(job_i, sid)
        if sd is None:
            continue

        cur = int(work_levels.get(sid, 0) or 0)
        grow_max = sd.max_level_for_grow(first_grow)
        if grow_max <= 0:
            continue
        maximum_level = sd.maximum_level_for_grow(first_grow)
        if maximum_level <= 0:
            continue
        hard_max = min(maximum_level, grow_max)
        new_lv = min(hard_max, cur + delta)
        if new_lv <= cur:
            continue

        need_lv = sd.required_level_for_target(new_lv)
        if need_lv > gate_level:
            return BuySkillResult(
                success=False,
                error_code=18,
                skill_tree=tree,
                remain_sp=remain_sp,
                remain_tp=remain_tp,
                final_mode=final_mode,
                note=(
                    "level_gate skill=%s need=%s char=%s bonus=%s effective=%s"
                    % (sid, need_lv, lv, gate_bonus, gate_level)
                ),
            )

        for pre_id, pre_lv in sd.pre_required:
            have = int(work_levels.get(int(pre_id) & 0xFFFF, 0) or 0)
            if have < int(pre_lv):
                return BuySkillResult(
                    success=False,
                    error_code=18,
                    skill_tree=tree,
                    remain_sp=remain_sp,
                    remain_tp=remain_tp,
                    final_mode=final_mode,
                    note="prereq skill=%s need=%s:%s have=%s"
                    % (sid, pre_id, pre_lv, have),
                )

        cost = sd.cost_for(cur, new_lv)
        if sd.is_tp_skill:
            if remain_tp < cost:
                return BuySkillResult(
                    success=False,
                    error_code=2,
                    skill_tree=tree,
                    remain_sp=remain_sp,
                    remain_tp=remain_tp,
                    final_mode=final_mode,
                    note="tp_short skill=%s cost=%s rem=%s" % (sid, cost, remain_tp),
                )
            remain_tp -= cost
        else:
            if remain_sp < cost:
                return BuySkillResult(
                    success=False,
                    error_code=2,
                    skill_tree=tree,
                    remain_sp=remain_sp,
                    remain_tp=remain_tp,
                    final_mode=final_mode,
                    note="sp_short skill=%s cost=%s rem=%s" % (sid, cost, remain_sp),
                )
            remain_sp -= cost

        if cur > 0 and sid in work_slots:
            slot = int(work_slots[sid]) & 0xFF
        else:
            allocate_occupied = occupied
            if job_i == 9 and bool(sd.is_active):
                try:
                    combo_helper = load_chain("helper-dark_knight_combo_skill.py")
                    if combo_helper.is_dark_knight_combo_child_skill(sid):
                        # Native combo children live in the skill tree; the
                        # visible 0..5/198..203 bars are owned by combo roots
                        # and non-combo common skills respectively.
                        allocate_occupied = set(occupied) | set(_QUICK_BAR_SLOTS)
                except Exception:
                    allocate_occupied = occupied
            slot = allocate_skill_slot(
                is_active=bool(sd.is_active),
                skill_class=int(sd.skill_class),
                grow_type_count=len(sd.grow_types),
                occupied=allocate_occupied,
                tree=tree,
            )
            if slot < 0:
                return BuySkillResult(
                    success=False,
                    error_code=1,
                    skill_tree=tree,
                    remain_sp=remain_sp,
                    remain_tp=remain_tp,
                    final_mode=final_mode,
                    note="no_slot skill=%s" % sid,
                )
            occupied.add(slot)
            work_slots[sid] = slot

        work_levels[sid] = new_lv
        removed_skill_ids.discard(sid)
        ack_slot = 0xFF if sd.is_special else (int(work_slots[sid]) & 0xFF)
        result_entries.append(_entry_with_cmd(ack_slot, sid, new_lv))

    # S4A12：仅退免费底线时 entries 可为空仍 success（初始化里混有送技）
    has_refund_req = any(int(e.is_refund) != 0 for e in entries)
    if not result_entries and not has_refund_req:
        return BuySkillResult(
            success=False,
            error_code=1,
            skill_tree=tree,
            remain_sp=remain_sp,
            remain_tp=remain_tp,
            final_mode=final_mode,
            note="no_effective_entries",
        )

    remain_sp = max(0, min(0xFFFF, remain_sp))
    remain_tp = max(0, min(0xFFFF, remain_tp))

    for e in result_entries:
        sid = int(e.skill_id) & 0xFFFF
        if int(e.level) <= 0 or sid in removed_skill_ids:
            # level=0：upsert 内部 DELETE 行；slot 用 ACK 槽（快捷栏）以便清占用
            del_slot = int(e.slot) & 0xFF
            if del_slot == 0xFF:
                del_slot = 0
            repo.upsert_character_skill(
                cid,
                page=tree,
                slot=del_slot,
                skill_id=sid,
                level=0,
                page_header=int(remain_sp) & 0xFFFF,
            )
            continue
        real_slot = int(work_slots.get(sid, e.slot)) & 0xFF
        if real_slot == 0xFF:
            real_slot = 0
        repo.upsert_character_skill(
            cid,
            page=tree,
            slot=real_slot,
            skill_id=sid,
            level=int(e.level),
            page_header=int(remain_sp) & 0xFFFF,
        )

    repo.upsert_character_skill_points(
        cid,
        total_sp=total_sp,
        remaining_sp=remain_sp,
        total_tp=total_tp,
        remaining_tp=remain_tp,
        synced_level=lv,
    )
    try:
        repo.update_character_skills_page_header(
            cid, page=tree, page_header=int(remain_sp) & 0xFFFF
        )
    except Exception:
        pass

    note_parts = [
        "ok n=%s remain_sp=%s remain_tp=%s" % (len(result_entries), remain_sp, remain_tp),
    ]
    if any(int(e.is_refund) != 0 for e in entries):
        zeroed = [e for e in result_entries if int(e.level) <= 0]
        note_parts.append(
            "refund zeroed=%s qb_cleared=%s"
            % (
                len(zeroed),
                sum(1 for e in zeroed if _is_quick_bar_slot(e.slot)),
            )
        )
    note_parts.append(
        "slots=%s"
        % (
            ",".join(
                "%s@%s:lv%s" % (e.skill_id, e.slot, e.level) for e in result_entries
            )
            or "-",
        )
    )

    return BuySkillResult(
        success=True,
        skill_tree=tree,
        remain_sp=remain_sp & 0xFFFF,
        remain_tp=remain_tp & 0xFFFF,
        entries=result_entries,
        final_mode=int(final_mode) & 0xFF,
        note=" ".join(note_parts),
    )
