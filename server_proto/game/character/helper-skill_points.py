"""Skill SP/TP absolute snapshot for S0/0x0025 (副本实现A / s4a12 parity).

Friends path (ExpNotificationBuilder + SpTableProvider)::

  Page0Sp / Page1Sp / Page0Tp / Page1Tp = remaining absolute balances
  total_sp(level) = sum(spTable[1..level]) + bonus_sp
  remaining ≈ total when no paid skills spent (new / tutorial chars)

PVF: ``Etc/spTable.etc`` ``[sp table]`` / ``[tp table]`` level/gain pairs
via shared preload only. Packs without ``[tp table]`` keep the historical
level-50 compatibility formula.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from pvf import pvf_runtime

_SP_PATHS = (
    "etc/sptable.etc",
    "Etc/spTable.etc",
    "etc/spTable.etc",
    "Etc/SpTable.etc",
    "character/sptable.etc",
)


@dataclass(frozen=True)
class SkillPointProtocolState:
    page0_sp: int = 0
    page1_sp: int = 0
    page0_tp: int = 0
    page1_tp: int = 0

    def as_u16_tuple(self) -> tuple[int, int, int, int]:
        return (
            int(self.page0_sp) & 0xFFFF,
            int(self.page1_sp) & 0xFFFF,
            int(self.page0_tp) & 0xFFFF,
            int(self.page1_tp) & 0xFFFF,
        )


def _read_sp_table_text() -> str:
    for path in _SP_PATHS:
        try:
            text = pvf_runtime.read_text(path)
            if text and str(text).strip():
                return str(text)
        except Exception:
            continue
    return ""


def _section_body(text: str, section: str) -> tuple[str | None, bool]:
    lower = str(text or "").lower()
    open_tag = "[%s]" % str(section).strip().lower()
    close_tag = "[/%s]" % str(section).strip().lower()
    start = lower.find(open_tag)
    if start < 0:
        return None, False
    start += len(open_tag)
    end = lower.find(close_tag, start)
    if end < 0:
        next_tag = lower.find("[", start)
        end = next_tag if next_tag >= 0 else len(text)
    return text[start:end], True


def _parse_level_gain_table(
    text: str,
    section: str,
    *,
    bare_fallback: bool = False,
) -> dict[int, int]:
    body, present = _section_body(text, section)
    if body is None:
        if not bare_fallback:
            return {}
        body = text
    tokens = re.findall(r"-?\d+", body)
    if present and not tokens:
        raise ValueError("[%s] is empty" % section)
    if present and len(tokens) % 2:
        raise ValueError("[%s] requires level/gain pairs" % section)

    out: dict[int, int] = {}
    for index in range(0, len(tokens) - 1, 2):
        level = int(tokens[index])
        gain = int(tokens[index + 1])
        if present:
            if level <= 0:
                raise ValueError("[%s] level must be positive" % section)
            if gain < 0:
                raise ValueError("[%s] gain must be non-negative" % section)
            if level in out:
                raise ValueError("[%s] duplicate level %d" % (section, level))
        elif level <= 0:
            continue
        out[level] = max(0, gain)
    return out


@lru_cache(maxsize=1)
def load_sp_per_level() -> dict[int, int]:
    """Parse [sp table] level→sp_gain from PVF; empty if missing."""
    text = _read_sp_table_text()
    if not text:
        return {}
    return _parse_level_gain_table(text, "sp table", bare_fallback=True)


@lru_cache(maxsize=1)
def load_tp_per_level() -> dict[int, int]:
    """Parse optional [tp table] level→tp_gain from the same PVF file."""
    text = _read_sp_table_text()
    if not text:
        return {}
    return _parse_level_gain_table(text, "tp table")


def warmup_skill_point_tables() -> dict[str, int | str]:
    """Load and validate the SP/TP tables before Game handlers can consume them."""
    sp_table = load_sp_per_level()
    tp_table = load_tp_per_level()
    if not sp_table:
        raise ValueError("etc/sptable.etc [sp table] missing or empty")
    return {
        "sp_rows": len(sp_table),
        "tp_rows": len(tp_table),
        "tp_source": "pvf" if tp_table else "level50_compatibility",
    }


def total_sp_for_level(level: int, bonus_sp: int = 0) -> int:
    """Σ spTable[1..level] + bonus_sp (s4a12 SpTableProvider.GetTotalSp)."""
    table = load_sp_per_level()
    lv = max(1, min(255, int(level or 1)))
    total = 0
    if table:
        for i in range(1, lv + 1):
            total += int(table.get(i, 0) or 0)
    else:
        # conservative fallback if PVF not warm: 1 SP per level (not friend-exact)
        total = lv
    return max(0, total + max(0, int(bonus_sp or 0)))


def total_tp_for_level(level: int, bonus_tp: int = 0) -> int:
    """Sum PVF [tp table] gains through level, then add persistent bonus TP."""
    lv = max(1, min(255, int(level or 1)))
    table = load_tp_per_level()
    if table:
        base = sum(int(gain or 0) for row_level, gain in table.items() if row_level <= lv)
    else:
        base = max(0, lv - 49)
    return max(0, base + max(0, int(bonus_tp or 0)))


def _bonus_from_repo(repo: Any, character_id: int) -> tuple[int, int]:
    if repo is None or int(character_id) <= 0:
        return 0, 0
    try:
        with repo.open() as conn:
            row = conn.execute(
                "SELECT bonus_sp, bonus_tp FROM characters "
                "WHERE character_id = ? AND delete_flag = 0",
                (int(character_id),),
            ).fetchone()
            if row is None:
                return 0, 0
            return int(row["bonus_sp"] or 0), int(row["bonus_tp"] or 0)
    except Exception:
        return 0, 0


def resolve_skill_point_protocol_state(
    *,
    level: int,
    character_id: int = 0,
    repo: Any = None,
    bonus_sp: int | None = None,
    bonus_tp: int | None = None,
    session: Any = None,
    job: int | None = None,
) -> SkillPointProtocolState:
    """绝对 SP/TP 快照（0025 / skillinfo / 学技能 ACK）。

    优先读 character_skill_points 持久化 remaining；
    无账本时按等级 total 派生并（有 repo 时）ensure 落库。
    Page1 镜像 page0 池（本服尚未独立 PVP 树扣点）。
    """
    lv = max(1, min(255, int(level or 1)))
    cid = int(character_id or 0)
    if bonus_sp is None or bonus_tp is None:
        b_sp, b_tp = _bonus_from_repo(repo, cid)
        if bonus_sp is None:
            bonus_sp = b_sp
        if bonus_tp is None:
            bonus_tp = b_tp
    if session is not None:
        try:
            if getattr(session, "bonus_sp", None) is not None and bonus_sp == 0:
                bonus_sp = int(session.bonus_sp or 0)
            if getattr(session, "bonus_tp", None) is not None and bonus_tp == 0:
                bonus_tp = int(session.bonus_tp or 0)
        except Exception:
            pass

    job_i = 0
    if job is not None:
        job_i = int(job) & 0xFF
    elif session is not None:
        try:
            job_i = int(getattr(session, "character_job", 0) or 0) & 0xFF
        except Exception:
            job_i = 0

    if repo is not None and cid > 0:
        try:
            ledger = ensure_skill_points_ledger(
                repo,
                character_id=cid,
                job=job_i,
                level=lv,
                bonus_sp=int(bonus_sp or 0),
                bonus_tp=int(bonus_tp or 0),
            )
            page0_sp = int(ledger["remaining_sp"]) & 0xFFFF
            page1_sp = page0_sp
            page0_tp = int(ledger["remaining_tp"]) & 0xFFFF
            page1_tp = page0_tp
            return SkillPointProtocolState(
                page0_sp=page0_sp,
                page1_sp=page1_sp,
                page0_tp=page0_tp,
                page1_tp=page1_tp,
            )
        except Exception:
            pass

    page0_sp = total_sp_for_level(lv, int(bonus_sp or 0))
    page1_sp = page0_sp
    page0_tp = total_tp_for_level(lv, int(bonus_tp or 0))
    page1_tp = page0_tp
    return SkillPointProtocolState(
        page0_sp=page0_sp & 0xFFFF,
        page1_sp=page1_sp & 0xFFFF,
        page0_tp=page0_tp & 0xFFFF,
        page1_tp=page1_tp & 0xFFFF,
    )


def ensure_skill_points_ledger(
    repo: Any,
    *,
    character_id: int,
    job: int,
    level: int,
    bonus_sp: Optional[int] = None,
    bonus_tp: Optional[int] = None,
) -> dict[str, int]:
    """确保 character_skill_points 与等级同步，返回账本 dict。

    - bonus 未显式传入：从 characters 的持久化技能点书奖励读取
    - 无行：total=等级表，remaining=total-已消耗（初始技能免费）
    - 有行且 synced_level < level：把 total 差额加到 remaining（升级涨点）
    - 有行且 level 未变：直接返回持久化 remaining
    """
    from game.chain_loader import load_chain

    cid = int(character_id)
    lv = max(1, min(255, int(level or 1)))
    job_i = int(job) & 0xFF
    if bonus_sp is None or bonus_tp is None:
        persisted_sp, persisted_tp = _bonus_from_repo(repo, cid)
        if bonus_sp is None:
            bonus_sp = persisted_sp
        if bonus_tp is None:
            bonus_tp = persisted_tp
    total_sp = total_sp_for_level(lv, int(bonus_sp or 0))
    total_tp = total_tp_for_level(lv, int(bonus_tp or 0))

    existing = None
    try:
        existing = repo.get_character_skill_points(cid)
    except Exception:
        existing = None

    def _spent() -> tuple[int, int]:
        skill_data = load_chain("helper-skill_data.py")
        rows = []
        try:
            rows = repo.load_character_skills(cid, page=0) or []
        except Exception:
            rows = []
        pairs = [
            (int(r.get("skill_id", 0) or 0), int(r.get("level", 0) or 0))
            for r in rows
        ]
        # 尽量带 grow_type，使转职送技不计入已消耗 SP
        grow = 0
        try:
            with repo.open() as conn:
                row = conn.execute(
                    "SELECT grow_type FROM characters "
                    "WHERE character_id = ? AND delete_flag = 0",
                    (cid,),
                ).fetchone()
                if row is not None:
                    grow = int(row["grow_type"] or 0) & 0xFF
        except Exception:
            grow = 0
        free_floor = skill_data.free_baseline_levels(job_i, grow)
        return skill_data.compute_spent_points(job_i, pairs, free_floor=free_floor)

    if existing is None or int(existing.get("synced_level") or 0) <= 0:
        spent_sp, spent_tp = _spent()
        remain_sp = max(0, total_sp - spent_sp)
        remain_tp = max(0, total_tp - spent_tp)
        try:
            repo.upsert_character_skill_points(
                cid,
                total_sp=total_sp,
                remaining_sp=remain_sp,
                total_tp=total_tp,
                remaining_tp=remain_tp,
                synced_level=lv,
            )
        except Exception:
            pass
        return {
            "total_sp": total_sp,
            "remaining_sp": remain_sp,
            "total_tp": total_tp,
            "remaining_tp": remain_tp,
            "synced_level": lv,
        }

    cur_lv = max(1, int(existing.get("synced_level") or 1))
    cur_total_sp = int(existing.get("total_sp") or 0)
    cur_remain_sp = int(existing.get("remaining_sp") or 0)
    cur_total_tp = int(existing.get("total_tp") or 0)
    cur_remain_tp = int(existing.get("remaining_tp") or 0)

    if cur_lv < lv or cur_total_sp != total_sp or cur_total_tp != total_tp:
        # 升级 / 表重算：差额加到 remaining（90qiyuan synchronizePointLedger）
        remain_sp = cur_remain_sp + (total_sp - cur_total_sp)
        remain_tp = cur_remain_tp + (total_tp - cur_total_tp)
        remain_sp = max(0, min(total_sp, remain_sp))
        remain_tp = max(0, min(total_tp, remain_tp))
        try:
            repo.upsert_character_skill_points(
                cid,
                total_sp=total_sp,
                remaining_sp=remain_sp,
                total_tp=total_tp,
                remaining_tp=remain_tp,
                synced_level=lv,
            )
        except Exception:
            pass
        return {
            "total_sp": total_sp,
            "remaining_sp": remain_sp,
            "total_tp": total_tp,
            "remaining_tp": remain_tp,
            "synced_level": lv,
        }

    return {
        "total_sp": cur_total_sp,
        "remaining_sp": max(0, min(cur_total_sp, cur_remain_sp)),
        "total_tp": cur_total_tp,
        "remaining_tp": max(0, min(cur_total_tp, cur_remain_tp)),
        "synced_level": cur_lv,
    }
