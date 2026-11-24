"""SET_QUEST_TRIGGER C->S 0x0021 (residual progress).

Stage3 live body: ``21 00 qq qq [triggerType] [isIncrement?]``
S4A12 语义（``QuestService.HandleSetTrigger``）::

  - triggerType 0x10/0x20/0x40 = packed 通道 0/1/2
  - triggerType 0 = raw 减一；1 = raw 加一
  - isIncrement 非 0 时通道递增

兼容旧 residual 桥：仅 1 字节 flag 且非通道掩码时，仍清 trigger=0。
"""
from __future__ import annotations

from typing import Any

from game.quest.daily_challenge import handle_daily_challenge_set_trigger
from game.quest.quest_common import build_set_quest_trigger_ack, env_bool, parse_quest_id_from_body
from game.quest.quest_residual import apply_set_trigger_delta
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_quest_refresh_packets,
    resolve_selected_character,
    resolve_selected_character_detail,
)


class SetQuestTrigger0021Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_set_quest_trigger_0021(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_quest_id_from_body(body, expected_opcode=0x0021)
        handle_on = env_bool("USDOF_GAME_QUEST_HANDLE_0021", True)
        quest_id = parsed.get("quest_id")
        flag = parsed.get("flag")
        trigger_type = parsed.get("trigger_type")
        is_increment = parsed.get("is_increment")
        cid, job, level = resolve_selected_character(self.server, session)
        base = (
            "C1/0021 SET_QUEST_TRIGGER layout=%s quest_id=%s flag=%s type=%s inc=%s cid=%s handle=%s"
            % (
                parsed["layout"],
                quest_id,
                flag,
                trigger_type,
                is_increment,
                cid,
                int(handle_on),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if quest_id is None or int(quest_id) <= 0 or cid is None:
            return [], base + "; reject=invalid"
        qid = int(quest_id) & 0xFFFF
        repo = self.server._repo()
        daily_result = handle_daily_challenge_set_trigger(
            self.server,
            character_repo=repo,
            character_id=int(cid),
            level=int(level or 1),
            quest_id=qid,
            trigger_type=trigger_type,
            is_increment=is_increment,
            flag=flag,
            session=session,
        )
        if daily_result is not None:
            packets, daily_note = daily_result
            return packets, "%s; %s" % (base, daily_note)
        active = {int(q): int(t) for _s, q, t in repo.list_active_quests(int(cid))}
        if qid not in active:
            packets = []
            if env_bool("USDOF_GAME_QUEST_0021_SEND_ACK", True):
                packets.append(
                    self.server._codec_response(
                        1,
                        0x0021,
                        build_set_quest_trigger_ack(success=True, quest_id=qid, trigger_value=0),
                    )
                )
            return packets, base + "; not_active echo_trigger=0"

        old = int(active[qid]) & 0xFFFFFFFF
        tt = int(trigger_type if trigger_type is not None else (flag or 0)) & 0xFF
        # 通道语义：type 含 0x10/20/40，或带 isIncrement 字节，或 type==0/1 raw
        use_channel = (
            env_bool("USDOF_GAME_QUEST_0021_CHANNEL_SEMANTICS", True)
            and (
                (tt & 0x70) != 0
                or is_increment is not None
                or tt in {0, 1}
            )
        )
        if use_channel:
            # 仅 flag 且无 isIncrement 字节：旧 live「flag=1 清 residual」→ 直接 0
            if (
                is_increment is None
                and tt not in {0, 1}
                and (tt & 0x70) == 0
                and env_bool("USDOF_GAME_QUEST_0021_LEGACY_CLEAR_ON_FLAG", True)
            ):
                trigger = 0
                mode = "legacy_clear"
            else:
                inc = bool(is_increment) if is_increment is not None else False
                # 旧客户端只发 flag=1 无 isIncrement：按 residual 完成桥清 0
                if is_increment is None and tt == 1 and env_bool(
                    "USDOF_GAME_QUEST_0021_FLAG1_MEANS_CLEAR", True
                ):
                    trigger = 0
                    mode = "flag1_clear"
                else:
                    trigger = apply_set_trigger_delta(old, trigger_type=tt, is_increment=inc)
                    mode = "channel"
        else:
            # 极旧桥：任意 flag 都清 0
            trigger = 0
            if flag is not None and int(flag) == 0 and env_bool(
                "USDOF_GAME_QUEST_TRIGGER_KEEP_ON_FLAG0", False
            ):
                trigger = old
            mode = "legacy_bridge"

        ok, detail = repo.update_active_quest_trigger(int(cid), qid, int(trigger))
        if ok and session is not None:
            rows = repo.list_active_quests(int(cid))
            pairs = [(int(q), int(tr)) for _s, q, tr in rows]
            fin = [int(q) for q, _f in repo.list_finished_quests(int(cid))]
            bind_session_quest_state(session, active_pairs=pairs, finished_ids=fin, last_quest_id=qid)

        packets: list[bytes] = []
        if env_bool("USDOF_GAME_QUEST_0021_SEND_ACK", True):
            if ok:
                packets.append(
                    self.server._codec_response(
                        1,
                        0x0021,
                        build_set_quest_trigger_ack(
                            success=True, quest_id=qid, trigger_value=int(trigger)
                        ),
                    )
                )
                # 默认 OFF：S1/0021 ACK 本身已通过 sub_137FC30 更新 trigger；
                # trigger==0 时还会 sub_10434A0(qid,1) 点亮可交闪烁。
                # 紧跟 S0/023F clear+rebuild 会冲掉动效（IDA 2026-07-18）。
                if env_bool("USDOF_GAME_QUEST_0021_REFRESH_023F", False):
                    detail2 = resolve_selected_character_detail(self.server, session)
                    refresh, rnote = pack_quest_refresh_packets(
                        self.server,
                        character_id=int(cid),
                        job=int(job or detail2.get("job") or 0),
                        level=int(level or detail2.get("level") or 1),
                        grow_type=int(detail2.get("grow_type") or 0),
                        session=session,
                        include_0015=False,
                        include_023f=True,
                        include_0164=False,
                    )
                    packets.extend(refresh)
                    return packets, "%s; mode=%s old=%s new=%s; %s; %s" % (
                        base,
                        mode,
                        old,
                        trigger,
                        detail,
                        rnote,
                    )
            else:
                packets.append(
                    self.server._codec_response(
                        1, 0x0021, build_set_quest_trigger_ack(success=False, error_code=0x14)
                    )
                )
        return packets, "%s; mode=%s old=%s new=%s; %s" % (base, mode, old, trigger, detail)
