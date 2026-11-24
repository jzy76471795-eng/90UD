"""C->S 0x001D BUY_SKILL：学习 / 升级 / 退点 / 初始化技能。

Stage3 live (game_server_20260719_010221.log 01:03:34):
  flag=1 opcode=0x001D codec_index=1
  plain: 00 03 08 00 00 01 06 00 00 01 04 00 00 01 ...
    → tree=0 count=3 skills={8,6,4} each +1 (is_refund=0)

退点 / 初始化 (game_server_20260721_152717.log 15:28:41):
  is_refund=1 + level_delta：降级或取消，返还 SP/TP
  初始化技能 = 客户端对可退技能批量 refund → 退到免费底线
  免费底线 = 创角 initial + 转职/觉醒 .chr 送技（不可取消）
  等级归 0：删 DB 行；快捷栏槽 (0..5 / 198..203) 腾空

安全：
  01EC mode=0 初始化成功后会 arm buy-guard（USDOF_GAME_SKILL_INIT_BUY_GUARD=1）。
  短窗内客户端若批量 C1/001D 重买旧方案，本 handler 抑制并回 final_mode=1。
  mode=1 auto study 不 arm 护栏。

注意：S0/0x001D 另用于副本 START_MAP / room_context A/B（flag=0）；
本 handler 只处理 C1 学技能，ACK 用 flag=1 避免混淆。

S->C ACK（90qiyuan BuildBuySkillSuccess / S4A12 BuySkillAckBuilder）:
  success=1, tree, remain_sp u16, remain_tp u16, count,
  entries*(slot, skill_id u16, level, has_cmd), final_mode
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.shop.premium_contract_index import get_active_premium_effects
from game.character.payloads import build_skillinfo_from_pvf
from game.quest.quest_session import resolve_selected_character_detail

_buy = load_chain("helper-buy_skill.py")
parse_buy_skill_body = _buy.parse_buy_skill_body
build_buy_skill_ack_ok = _buy.build_buy_skill_ack_ok
build_buy_skill_ack_fail = _buy.build_buy_skill_ack_fail
execute_buy_skill = _buy.execute_buy_skill

_init = load_chain("helper-skill_init.py")
skill_init_buy_guard_active = _init.skill_init_buy_guard_active
should_suppress_post_init_rebuy = _init.should_suppress_post_init_rebuy

_skills_h = load_chain("helper-character_skills.py")
resolve_skills_for_character = _skills_h.resolve_skills_for_character


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


class BuySkill001DService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_buy_skill_001d(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_HANDLE_BUY_SKILL_001D", True)
        base = "C1/001D BUY_SKILL plain_len=%s" % (len(body or b""),)
        if not handle_on:
            return [], base + "; gate=OFF"

        parsed = parse_buy_skill_body(body)
        if parsed is None:
            packet = self.server._codec_response(
                1, 0x001D, build_buy_skill_ack_fail(1)
            )
            return [packet], base + "; reject=bad_body; fail ACK wire=%s" % (
                len(packet),
            )

        cid = None
        job = 0
        level = 1
        grow = 0
        try:
            detail = resolve_selected_character_detail(self.server, session)
            cid = detail.get("character_id")
            job = int(detail.get("job") or 0)
            level = max(1, int(detail.get("level") or 1))
            grow = int(detail.get("grow_type") or 0)
        except Exception as exc:
            detail_err = "resolve_fail=%r" % (exc,)
        else:
            detail_err = ""

        if session is not None:
            try:
                if cid is None:
                    cid = getattr(session, "selected_character_id", None)
                job = int(getattr(session, "character_job", job) or job)
                level = max(
                    1,
                    int(getattr(session, "character_level", level) or level),
                )
            except Exception:
                pass

        repo = None
        try:
            if hasattr(self.server, "_repo"):
                repo = self.server._repo()
        except Exception:
            repo = None

        if cid and repo is not None:
            try:
                exp_db, lv_db = repo.get_character_exp_level(int(cid))
                if int(lv_db or 0) > 0:
                    level = max(1, int(lv_db))
                # job/grow from character row if available
                rows = repo.list_all_alive() if hasattr(repo, "list_all_alive") else []
                for rec in rows or []:
                    if int(getattr(rec, "character_id", 0)) == int(cid):
                        job = int(getattr(rec, "job", job) or job)
                        grow = int(getattr(rec, "grow_type", grow) or grow)
                        break
            except Exception:
                pass

        if not cid or repo is None:
            packet = self.server._codec_response(
                1, 0x001D, build_buy_skill_ack_fail(1)
            )
            return [packet], (
                "%s; reject=no_cid_or_repo cid=%r %s; fail ACK"
                % (base, cid, detail_err)
            )

        entries = list(parsed["entries"])
        tree = int(parsed["skill_tree"])

        # 01EC 后短窗：挡客户端批量重买旧方案（live entries_in=14 all buy）
        if (
            _env_bool("USDOF_GAME_SKILL_INIT_BUY_GUARD", True)
            and skill_init_buy_guard_active(session, character_id=int(cid))
            and should_suppress_post_init_rebuy(entries)
        ):
            remain_sp = 0
            remain_tp = 0
            if session is not None:
                try:
                    remain_sp = int(getattr(session, "skill_remain_sp", 0) or 0)
                    remain_tp = int(getattr(session, "skill_remain_tp", 0) or 0)
                except Exception:
                    pass
            try:
                ledger = repo.get_character_skill_points(int(cid)) or {}
                if ledger:
                    remain_sp = int(ledger.get("remaining_sp") or remain_sp)
                    remain_tp = int(ledger.get("remaining_tp") or remain_tp)
            except Exception:
                pass

            # final_mode=1：与 01EC 后服务端主动刷树一致（IDA 161AA50 n3==1||3）
            ack = build_buy_skill_ack_ok(
                skill_tree=tree,
                remain_sp=int(remain_sp) & 0xFFFF,
                remain_tp=int(remain_tp) & 0xFFFF,
                entries=[],
                final_mode=1,
            )
            packets: list[Any] = [
                self.server._codec_response(1, 0x001D, ack)
            ]
            # 重推 0013，把客户端拉回服务端权威基线（含主动技快捷栏）
            skillinfo_note = "no_resend_0013"
            try:
                skills, snote = resolve_skills_for_character(
                    repo, int(cid), int(job), seed_if_empty=False
                )
                sk_body = build_skillinfo_from_pvf(
                    list(skills),
                    page0_sp=int(remain_sp) & 0xFFFF,
                    page1_sp=int(remain_sp) & 0xFFFF,
                    page0_tp=int(remain_tp) & 0xFFFF,
                    page1_tp=int(remain_tp) & 0xFFFF,
                    mirror_page0_to_page1=True,
                )
                packets.append(self.server._codec_response(0, 0x0013, sk_body))
                skillinfo_note = (
                    "resend_0013 n=%s sp=%s tp=%s (%s)"
                    % (len(skills), remain_sp, remain_tp, snote)
                )
            except Exception as exc:
                skillinfo_note = "resend_0013_fail=%r" % (exc,)

            return packets, (
                "%s; tree=%s entries_in=%s success=1 err=0 remain_sp=%s "
                "remain_tp=%s note=suppress_post_init_rebuy %s ack_len=%s "
                "packets=%s"
                % (
                    base,
                    tree,
                    len(entries),
                    remain_sp,
                    remain_tp,
                    skillinfo_note,
                    len(ack),
                    len(packets),
                )
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

        result = execute_buy_skill(
            repo,
            character_id=int(cid),
            job=int(job),
            level=int(level),
            grow_type=int(grow),
            skill_tree=tree,
            entries=entries,
            final_mode=int(parsed.get("final_mode") or 0),
            level_gate_bonus=level_gate_bonus,
        )

        if result.success:
            ack = build_buy_skill_ack_ok(
                skill_tree=result.skill_tree,
                remain_sp=result.remain_sp,
                remain_tp=result.remain_tp,
                entries=result.entries,
                final_mode=result.final_mode,
            )
        else:
            ack = build_buy_skill_ack_fail(result.error_code or 1)

        packet = self.server._codec_response(1, 0x001D, ack)

        if session is not None and result.success:
            try:
                session.skill_remain_sp = int(result.remain_sp)
                session.skill_remain_tp = int(result.remain_tp)
            except Exception:
                pass

        return [packet], (
            "%s; tree=%s entries_in=%s success=%s err=%s remain_sp=%s remain_tp=%s "
            "skill_level_reduction=%s note=%s ack_len=%s wire=%s"
            % (
                base,
                parsed["skill_tree"],
                len(parsed["entries"]),
                int(result.success),
                result.error_code,
                result.remain_sp,
                result.remain_tp,
                level_gate_bonus,
                result.note,
                len(ack),
                len(packet),
            )
        )
