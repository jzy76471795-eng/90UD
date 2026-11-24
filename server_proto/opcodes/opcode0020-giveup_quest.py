"""GIVEUP_QUEST C->S 0x0020 (modular)."""
from __future__ import annotations

from typing import Any

from game.quest.quest_common import build_giveup_quest_ack, env_bool, parse_quest_id_from_body
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_quest_refresh_packets,
    resolve_selected_character,
    resolve_selected_character_detail,
)


class GiveupQuest0020Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_giveup_quest_0020(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        parsed = parse_quest_id_from_body(body, expected_opcode=0x0020)
        handle_on = env_bool("USDOF_GAME_QUEST_HANDLE_0020", True)
        quest_id = parsed.get("quest_id")
        cid, job, level = resolve_selected_character(self.server, session)
        base = (
            "C1/0020 GIVEUP_QUEST layout=%s quest_id=%s cid=%s handle=%s"
            % (parsed["layout"], quest_id, cid, int(handle_on))
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if quest_id is None or int(quest_id) <= 0 or cid is None:
            return [], base + "; reject=invalid"
        qid = int(quest_id) & 0xFFFF
        repo = self.server._repo()
        ok, detail = repo.giveup_quest(int(cid), qid)
        if ok and session is not None:
            rows = repo.list_active_quests(int(cid))
            pairs = [(int(q), int(tr)) for _s, q, tr in rows]
            fin = [int(q) for q, _f in repo.list_finished_quests(int(cid))]
            bind_session_quest_state(session, active_pairs=pairs, finished_ids=fin, last_quest_id=qid)
        packets: list[bytes] = []
        if env_bool("USDOF_GAME_QUEST_0020_SEND_ACK", True) and ok:
            packets.append(self.server._codec_response(1, 0x0020, build_giveup_quest_ack(success=True, quest_id=qid)))
            detail2 = resolve_selected_character_detail(self.server, session)
            # 成功放弃后必须刷 023F（进行中）+ 0015（可接）。
            # Stage3 IDA: S1/0020 sub_163A1A0 只从本地进行中移除；
            # 可接列表由 S0/0015 sub_1650580 全量重建。缺 0015 则 UI 任务消失，重进角色才恢复。
            refresh, rnote = pack_quest_refresh_packets(
                self.server,
                character_id=int(cid),
                job=int(job or 0),
                level=int(level or 1),
                grow_type=int(detail2.get("grow_type") or 0),
                session=session,
                include_0015=env_bool("USDOF_GAME_REPLY_GIVEUP_QUEST_REFRESH_0015", True),
                include_023f=True,
                include_0164=False,
            )
            packets.extend(refresh)
            return packets, "%s; %s; %s" % (base, detail, rnote)
        # 放弃失败（不在身上等）：S4A12 冻结 fail=0x13；对齐客户端错误分支
        if env_bool("USDOF_GAME_QUEST_0020_SEND_ACK", True) and not ok:
            packets.append(
                self.server._codec_response(
                    1, 0x0020, build_giveup_quest_ack(success=False, error_code=0x13)
                )
            )
            return packets, "%s; %s; giveup-fail-ack=0x13" % (base, detail)
        return packets, "%s; %s" % (base, detail)

