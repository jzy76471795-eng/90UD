# -*- coding: utf-8 -*-
"""C->S 0x01EC：Stage3 技能窗双语义（tree, mode）。

Live 抓包（2026-07-21）：
  - 初始化技能：plain ``00 00...`` → tree=0 **mode=0**
  - 自动学习：  plain ``00 01...`` → tree=0 **mode=1**
    （game_server_20260721_175213.log 17:52:53 / 175359 17:54:45）

路由策略：
  mode == 1 → AUTO_SKILL（helper-auto_skill：保留已学，用剩余 SP 尽量加点）
  其它（0/2/3…）→ SKILL_INIT（helper-skill_init：重置到创角/转职基线 + 全额 SP/TP）

初始化成功回包（与 auto study 同序，IDA 161AA50 final_mode 刷树）：
  S1/01EC ACK
  + S1/001D（基线 skills entries + final_mode=1；**禁止空 count**）
  + S0/0013 skillinfo（权威树 + SP）
  + arm buy-guard（挡客户端旧方案批量 001D 重买）

  live 反证（2026-07-21 18:07:44 hang）：空 001D 客户端无 PIV，技能窗卡等待。

自动学习成功回包：
  S1/01EC ACK
  + S1/001D（变更 entries + final_mode=1）
  + S0/0013
  **不** arm buy-guard
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.character.payloads import build_skillinfo_from_pvf
from game.quest.quest_session import resolve_selected_character_detail
from game.shop.premium_contract_index import get_active_premium_effects

_init = load_chain("helper-skill_init.py")
parse_skill_init_body = _init.parse_skill_init_body
build_skill_init_ack_ok = _init.build_skill_init_ack_ok
build_skill_init_ack_fail = _init.build_skill_init_ack_fail
execute_skill_init = _init.execute_skill_init
arm_skill_init_buy_guard = _init.arm_skill_init_buy_guard

_auto = load_chain("helper-auto_skill.py")
parse_auto_skill_body = _auto.parse_auto_skill_body
build_auto_skill_ack_ok = _auto.build_auto_skill_ack_ok
build_auto_skill_ack_fail = _auto.build_auto_skill_ack_fail
execute_auto_skill = _auto.execute_auto_skill

_buy = load_chain("helper-buy_skill.py")
build_buy_skill_ack_ok = _buy.build_buy_skill_ack_ok

_skills_h = load_chain("helper-character_skills.py")
resolve_skills_for_character = _skills_h.resolve_skills_for_character

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _resolve_character_ctx(
    server: Any,
    session: Any,
) -> tuple[Any, Any, int, int, int, int, int, int, str]:
    """返回 (repo, cid, job, level, grow, exp_i, bonus_sp, bonus_tp, detail_err)。"""
    cid = None
    job = 0
    level = 1
    grow = 0
    exp_i = 0
    detail_err = ""
    try:
        detail = resolve_selected_character_detail(server, session)
        cid = detail.get("character_id")
        job = int(detail.get("job") or 0)
        level = max(1, int(detail.get("level") or 1))
        grow = int(detail.get("grow_type") or 0)
        exp_i = int(detail.get("exp") or 0) & 0xFFFFFFFF
    except Exception as exc:
        detail_err = "resolve_fail=%r" % (exc,)

    if session is not None:
        try:
            if cid is None:
                cid = getattr(session, "selected_character_id", None)
            job = int(getattr(session, "character_job", job) or job)
            level = max(
                1,
                int(getattr(session, "character_level", level) or level),
            )
            if exp_i <= 0:
                exp_i = int(getattr(session, "character_exp", 0) or 0) & 0xFFFFFFFF
        except Exception:
            pass

    repo = None
    try:
        if hasattr(server, "_repo"):
            repo = server._repo()
    except Exception:
        repo = None

    bonus_sp = 0
    bonus_tp = 0
    if cid and repo is not None:
        try:
            exp_db, lv_db = repo.get_character_exp_level(int(cid))
            if int(lv_db or 0) > 0:
                level = max(1, int(lv_db))
            if exp_db is not None:
                exp_i = int(exp_db or 0) & 0xFFFFFFFF
            rows = repo.list_all_alive() if hasattr(repo, "list_all_alive") else []
            for rec in rows or []:
                if int(getattr(rec, "character_id", 0)) == int(cid):
                    job = int(getattr(rec, "job", job) or job)
                    grow = int(getattr(rec, "grow_type", grow) or grow)
                    break
            try:
                with repo.open() as conn:
                    brow = conn.execute(
                        "SELECT bonus_sp, bonus_tp FROM characters "
                        "WHERE character_id = ? AND delete_flag = 0",
                        (int(cid),),
                    ).fetchone()
                    if brow is not None:
                        bonus_sp = int(brow["bonus_sp"] or 0)
                        bonus_tp = int(brow["bonus_tp"] or 0)
            except Exception:
                pass
        except Exception:
            pass

    return (
        repo,
        cid,
        int(job),
        int(level),
        int(grow),
        int(exp_i) & 0xFFFFFFFF,
        int(bonus_sp),
        int(bonus_tp),
        detail_err,
    )


class SkillInit01ECService:
    """C1/01EC 统一入口：按 mode 分流初始化 / 自动学习。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_skill_init_01ec(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        data = bytes(body or b"")
        # 解析 tree/mode；空包默认 mode=0（初始化更安全，避免误 auto 烧 SP）
        parsed = parse_skill_init_body(data)
        if parsed is None:
            if len(data) == 0:
                parsed = {
                    "skill_tree": 0,
                    "skill_tree_raw": 0,
                    "mode": 0,
                    "raw_len": 0,
                }
            else:
                packet = self.server._codec_response(
                    1, 0x01EC, build_skill_init_ack_fail(1)
                )
                return [packet], (
                    "C1/01EC plain_len=%s; reject=bad_body; fail ACK wire=%s"
                    % (len(data), len(packet))
                )

        mode = int(parsed.get("mode") or 0) & 0xFF
        # mode=1 = Auto Study；其它（含 0）= Skill Init
        if mode == 1:
            return self._handle_auto_skill(parsed, session=session)
        return self._handle_skill_init(parsed, session=session)

    # ------------------------------------------------------------------
    # SKILL_INIT（mode != 1）
    # ------------------------------------------------------------------
    def _handle_skill_init(
        self,
        parsed: dict,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_HANDLE_SKILL_INIT_01EC", True)
        base = "C1/01EC SKILL_INIT plain_len=%s" % (
            int(parsed.get("raw_len") or 0),
        )
        if not handle_on:
            return [], base + "; gate=OFF"

        (
            repo,
            cid,
            job,
            level,
            grow,
            exp_i,
            _bonus_sp,
            _bonus_tp,
            detail_err,
        ) = _resolve_character_ctx(self.server, session)

        if not cid or repo is None:
            packet = self.server._codec_response(
                1, 0x01EC, build_skill_init_ack_fail(1)
            )
            return [packet], (
                "%s; reject=no_cid_or_repo cid=%r %s; fail ACK"
                % (base, cid, detail_err)
            )

        result = execute_skill_init(
            repo,
            character_id=int(cid),
            job=int(job),
            level=int(level),
            grow_type=int(grow),
            skill_tree=int(parsed["skill_tree"]),
            mode=int(parsed.get("mode") or 0),
        )

        packets: list[bytes] = []
        skillinfo_note = "no_skillinfo"
        if result.success:
            # 回包顺序对齐「自动学习」live（客户端有 PIV 的成功路径）：
            #   S1/01EC → S1/001D(基线 entries + final_mode=1) → S0/0013 → S0/0025
            # 旧序 01EC→0013→空001D 时，客户端对空 001D 无 PIV，技能窗卡等待
            # （2026-07-21 18:07:44 hang 日志：PIV 只回 01EC/0013/0025，无 001D）。
            ack = build_skill_init_ack_ok(skill_tree=result.skill_tree)
            packets.append(self.server._codec_response(1, 0x01EC, ack))

            # Stage3：class1 0x01EC 无 S2C success reader；刷树靠 001D final_mode
            # 必须带基线 entries（与 auto study 一致），空 count 客户端不吃 001D。
            send_001d_refresh = _env_bool(
                "USDOF_GAME_SKILL_INIT_SEND_001D_REFRESH", True
            )
            if send_001d_refresh:
                try:
                    BuySkillResultEntry = _buy.BuySkillResultEntry
                    init_entries = []
                    for sk in list(result.skills or []):
                        sid = int(getattr(sk, "skill_id", 0) or 0) & 0xFFFF
                        lv = int(getattr(sk, "level", 0) or 0) & 0xFF
                        if sid <= 0 or lv <= 0:
                            continue
                        slot = int(getattr(sk, "slot", 0) or 0) & 0xFF
                        cmd = getattr(sk, "command_bytes", b"") or b""
                        if not isinstance(cmd, (bytes, bytearray, memoryview)):
                            cmd = b""
                        else:
                            cmd = bytes(cmd)
                        init_entries.append(
                            BuySkillResultEntry(
                                slot=slot,
                                skill_id=sid,
                                level=lv,
                                has_cmd=bool(cmd),
                                command_bytes=cmd,
                            )
                        )
                    buy_ack = build_buy_skill_ack_ok(
                        skill_tree=int(result.skill_tree),
                        remain_sp=int(result.remain_sp) & 0xFFFF,
                        remain_tp=int(result.remain_tp) & 0xFFFF,
                        entries=init_entries,
                        final_mode=1,
                    )
                    packets.append(
                        self.server._codec_response(1, 0x001D, buy_ack)
                    )
                    skillinfo_note = (
                        "001D_init n=%s final_mode=1 ack_len=%s sp=%s tp=%s"
                        % (
                            len(init_entries),
                            len(buy_ack),
                            result.remain_sp,
                            result.remain_tp,
                        )
                    )
                except Exception as exc:
                    skillinfo_note = "001D_init_fail=%r" % (exc,)
            else:
                skillinfo_note = "001D_refresh_off"

            try:
                sk_body = build_skillinfo_from_pvf(
                    list(result.skills),
                    page0_sp=int(result.remain_sp),
                    page1_sp=int(result.remain_sp),
                    page0_tp=int(result.remain_tp),
                    page1_tp=int(result.remain_tp),
                    mirror_page0_to_page1=True,
                )
                packets.append(self.server._codec_response(0, 0x0013, sk_body))
                skillinfo_note += (
                    "; 0013_skillinfo kind=%s n=%s sp=%s tp=%s plain_len=%s"
                    % (
                        result.baseline_kind,
                        len(result.skills),
                        result.remain_sp,
                        result.remain_tp,
                        len(sk_body),
                    )
                )
            except Exception as exc:
                skillinfo_note += "; 0013_skillinfo_fail=%r" % (exc,)

            skillinfo_note += "; exp_packet=unused_sp_tp_from_001d_0013"

            until = 0.0
            if _env_bool("USDOF_GAME_SKILL_INIT_BUY_GUARD", True):
                until = arm_skill_init_buy_guard(session, character_id=int(cid))
            skillinfo_note += "; buy_guard_until=%.3f" % (until,)
        else:
            packets.append(
                self.server._codec_response(
                    1, 0x01EC, build_skill_init_ack_fail(result.error_code or 1)
                )
            )
            skillinfo_note = "fail"

        if session is not None and result.success:
            try:
                session.skill_remain_sp = int(result.remain_sp)
                session.skill_remain_tp = int(result.remain_tp)
            except Exception:
                pass

        return packets, (
            "%s; tree=%s mode=%s success=%s err=%s remain_sp=%s remain_tp=%s "
            "total_sp=%s total_tp=%s note=%s %s packets=%s"
            % (
                base,
                parsed["skill_tree"],
                parsed.get("mode", 0),
                int(result.success),
                result.error_code,
                result.remain_sp,
                result.remain_tp,
                result.total_sp,
                result.total_tp,
                result.note,
                skillinfo_note,
                len(packets),
            )
        )

    # ------------------------------------------------------------------
    # AUTO_SKILL（mode == 1）
    # ------------------------------------------------------------------
    def _handle_auto_skill(
        self,
        parsed: dict,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_HANDLE_AUTO_SKILL_01EC", True)
        if not handle_on:
            # 兼容：仅开旧 env 名时也放行
            handle_on = _env_bool("USDOF_GAME_HANDLE_SKILL_INIT_01EC", True)
        base = "C1/01EC AUTO_SKILL plain_len=%s" % (
            int(parsed.get("raw_len") or 0),
        )
        if not handle_on:
            return [], base + "; gate=OFF"

        (
            repo,
            cid,
            job,
            level,
            grow,
            exp_i,
            bonus_sp,
            bonus_tp,
            detail_err,
        ) = _resolve_character_ctx(self.server, session)

        if not cid or repo is None:
            packet = self.server._codec_response(
                1, 0x01EC, build_auto_skill_ack_fail(1)
            )
            return [packet], (
                "%s; reject=no_cid_or_repo cid=%r %s; fail ACK"
                % (base, cid, detail_err)
            )

        account_id = repo.get_account_id_for_character(int(cid))
        if account_id is None:
            account_id = int(getattr(self.server, "account_id", 1) or 1)
        premium_effects = get_active_premium_effects(
            repo,
            int(account_id),
            session=session,
        )
        level_gate_bonus = max(0, int(premium_effects.skill_level_reduction))

        include_tp = _env_bool("USDOF_GAME_AUTO_SKILL_INCLUDE_TP", False)
        result = execute_auto_skill(
            repo,
            character_id=int(cid),
            job=int(job),
            level=int(level),
            grow_type=int(grow),
            skill_tree=int(parsed["skill_tree"]),
            mode=int(parsed.get("mode") or 1),
            bonus_sp=int(bonus_sp),
            bonus_tp=int(bonus_tp),
            level_gate_bonus=level_gate_bonus,
            include_tp=include_tp,
        )

        packets: list[bytes] = []
        skillinfo_note = "no_skillinfo"
        if result.success:
            ack = build_auto_skill_ack_ok(skill_tree=result.skill_tree)
            packets.append(self.server._codec_response(1, 0x01EC, ack))

            send_001d = _env_bool("USDOF_GAME_AUTO_SKILL_SEND_001D", True)
            if send_001d:
                try:
                    buy_ack = build_buy_skill_ack_ok(
                        skill_tree=int(result.skill_tree),
                        remain_sp=int(result.remain_sp) & 0xFFFF,
                        remain_tp=int(result.remain_tp) & 0xFFFF,
                        entries=list(result.entries or []),
                        final_mode=1,
                    )
                    packets.append(
                        self.server._codec_response(1, 0x001D, buy_ack)
                    )
                    skillinfo_note = (
                        "001D_auto n=%s final_mode=1 ack_len=%s sp=%s tp=%s"
                        % (
                            len(result.entries or []),
                            len(buy_ack),
                            result.remain_sp,
                            result.remain_tp,
                        )
                    )
                except Exception as exc:
                    skillinfo_note = "001D_auto_fail=%r" % (exc,)

            try:
                skills, snote = resolve_skills_for_character(
                    repo, int(cid), int(job), seed_if_empty=False
                )
                sk_body = build_skillinfo_from_pvf(
                    list(skills),
                    page0_sp=int(result.remain_sp) & 0xFFFF,
                    page1_sp=int(result.remain_sp) & 0xFFFF,
                    page0_tp=int(result.remain_tp) & 0xFFFF,
                    page1_tp=int(result.remain_tp) & 0xFFFF,
                    mirror_page0_to_page1=True,
                )
                packets.append(self.server._codec_response(0, 0x0013, sk_body))
                skillinfo_note += (
                    "; 0013 n=%s sp=%s tp=%s plain_len=%s (%s)"
                    % (
                        len(skills),
                        result.remain_sp,
                        result.remain_tp,
                        len(sk_body),
                        snote,
                    )
                )
            except Exception as exc:
                skillinfo_note += "; 0013_fail=%r" % (exc,)

            skillinfo_note += "; exp_packet=unused_sp_tp_from_001d_0013"
        else:
            packets.append(
                self.server._codec_response(
                    1, 0x01EC, build_auto_skill_ack_fail(result.error_code or 1)
                )
            )
            skillinfo_note = "fail"

        if session is not None and result.success:
            try:
                session.skill_remain_sp = int(result.remain_sp)
                session.skill_remain_tp = int(result.remain_tp)
            except Exception:
                pass

        return packets, (
            "%s; tree=%s mode=%s success=%s err=%s remain_sp=%s remain_tp=%s "
            "skill_level_reduction=%s plan_n=%s note=%s %s packets=%s"
            % (
                base,
                parsed["skill_tree"],
                parsed.get("mode", 0),
                int(result.success),
                result.error_code,
                result.remain_sp,
                result.remain_tp,
                level_gate_bonus,
                len(result.plan or []),
                result.note,
                skillinfo_note,
                len(packets),
            )
        )
