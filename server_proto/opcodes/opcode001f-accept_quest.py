"""ACCEPT_QUEST C->S 0x001F (modular quest accept + PVF depend-give items)."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain
from game.content_blocking import activate_character_content_blocks, is_quest_blocked
from game.character.inventory_item_view import InventoryItemView
from game.character.payloads import build_item_list_mode0
from game.quest.quest_common import (
    build_accept_quest_ack,
    env_bool,
    parse_quest_id_from_body,
)
from game.quest.daily_challenge_index import get_daily_challenge_candidate_ids
from game.quest.quest_index import (
    compute_quest_init_trigger,
    get_quest_index,
    is_daily_quest_meta,
    is_repeatable_quest_meta,
)
from game.quest.quest_rewards import resolve_accept_event_items
from game.quest.quest_session import (
    bind_session_quest_state,
    list_today_daily_completion_ids,
    pack_client_finished_quest_correction,
    pack_completeable_trigger_acks,
    pack_quest_refresh_packets,
    resolve_acceptable_ids,
    resolve_selected_character_detail,
)


class SimpleInventoryItem:
    __slots__ = (
        "slot_index",
        "item_template_id",
        "stack_count",
        "durability",
        "instance_value",
        "item_kind",
        "seal_flag",
    )

    def __init__(
        self,
        slot_index: int,
        item_template_id: int,
        stack_count: int,
        durability: int = 0,
        *,
        instance_value: int = 0,
        item_kind: str = "",
        seal_flag: int = 0,
    ) -> None:
        self.slot_index = int(slot_index)
        self.item_template_id = int(item_template_id)
        self.stack_count = int(stack_count)
        self.durability = max(0, int(durability or 0)) & 0xFFFF
        self.instance_value = max(0, int(instance_value or 0)) & 0xFFFFFFFF
        self.item_kind = str(item_kind or "")
        self.seal_flag = max(0, int(seal_flag or 0)) & 0xFF


class AcceptQuest001FService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_accept_quest_001f(
        self,
        body: bytes,
        *,
        session: Any = None,
        force_accept: bool = False,
        reward_policy: str = "default",
        emit_packets: bool = True,
        resolved_character: dict[str, Any] | None = None,
    ) -> tuple[list[bytes], str]:
        """Accept one quest and emit the normal client refresh flow.

        ``force_accept`` is reserved for the GM control command. It bypasses
        the client-facing PVF eligibility list while retaining the finished
        quest, character, slot, persistence, reward, and packet checks below.
        The ordinary C->S route keeps the eligibility gate unchanged.
        ``state_only`` persists the accept state without granting event items.
        A trusted compound transaction may pass its validated actor snapshot
        via ``resolved_character``; ordinary wire requests do not set it.
        """
        policy = str(reward_policy or "default").strip().lower()
        if policy not in {"default", "gold_only", "state_only"}:
            raise ValueError(
                "unsupported quest acceptance reward policy=%r" % policy
            )
        parsed = parse_quest_id_from_body(body, expected_opcode=0x001F)
        handle_on = env_bool("USDOF_GAME_QUEST_HANDLE_001F", True)
        send_ack = env_bool("USDOF_GAME_QUEST_001F_SEND_ACK", True) and bool(
            emit_packets
        )
        quest_id = parsed.get("quest_id")
        detail = (dict(resolved_character) if resolved_character is not None
                  else resolve_selected_character_detail(self.server, session))
        cid = detail.get("character_id")
        job = detail.get("job")
        level = detail.get("level")
        grow = detail.get("grow_type")
        base = (
            "C1/001F ACCEPT_QUEST plain_len=%s layout=%s quest_id=%s cid=%s job=%s "
            "handle=%s force=%s reward_policy=%s emit=%s"
            % (
                parsed["raw_len"],
                parsed["layout"],
                quest_id,
                cid,
                job,
                int(handle_on),
                int(bool(force_accept)),
                policy,
                int(bool(emit_packets)),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if quest_id is None or int(quest_id) <= 0:
            return [], base + "; reject=invalid_quest_id"
        qid = int(quest_id) & 0xFFFF
        if cid is None:
            return [], base + "; reject=no_character"
        mirror_handled = False
        mirror_note = "mirror_mission_accept=skip_inactive"
        if bool(getattr(session, "another_arad_active", False)):
            mirror_handled, mirror_note = load_chain(
                "helper-another_arad_quest.py"
            ).accept_another_arad_quest_state(
                session,
                qid,
            )
        if mirror_handled:
            packets: list[bytes] = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1,
                        0x001F,
                        build_accept_quest_ack(
                            success=True,
                            quest_id=qid,
                            init_trigger=0,
                        ),
                    )
                )
            return packets, "%s; %s; ack=%s session_local=1" % (
                base,
                mirror_note,
                "ok" if send_ack else "suppressed",
            )
        repo = self.server._repo()
        activate_character_content_blocks(repo, int(cid), session=session)
        if qid in get_daily_challenge_candidate_ids():
            packets: list[bytes] = []
            if send_ack and env_bool("USDOF_GAME_QUEST_001F_SEND_FAIL_ACK", True):
                packets.append(
                    self.server._codec_response(
                        1,
                        0x001F,
                        build_accept_quest_ack(success=False, error_code=0x15),
                    )
                )
            return packets, base + "; reject=daily_challenge_ledger_owned"
        quest_index = get_quest_index()
        meta = quest_index.get_meta(qid)
        sequence_checker = getattr(
            quest_index,
            "is_unsequenced_epic_quest",
            None,
        )
        if (
            not force_accept
            and callable(sequence_checker)
            and sequence_checker(qid)
        ):
            packets: list[bytes] = []
            if send_ack and env_bool("USDOF_GAME_QUEST_001F_SEND_FAIL_ACK", True):
                packets.append(
                    self.server._codec_response(
                        1,
                        0x001F,
                        build_accept_quest_ack(success=False, error_code=0x15),
                    )
                )
            return packets, base + "; reject=unsequenced_epic_quest"
        if not force_accept and is_quest_blocked(qid):
            packets: list[bytes] = []
            if send_ack and env_bool("USDOF_GAME_QUEST_001F_SEND_FAIL_ACK", True):
                packets.append(
                    self.server._codec_response(
                        1,
                        0x001F,
                        build_accept_quest_ack(success=False, error_code=0x15),
                    )
                )
            return packets, base + "; reject=content_blocked_quest"
        is_daily = is_daily_quest_meta(meta)
        is_repeatable = is_repeatable_quest_meta(meta)
        daily_completed_ids = list_today_daily_completion_ids(repo, int(cid))
        persist_finished = env_bool("USDOF_GAME_QUEST_PERSIST_FINISHED", True)
        already_finished = persist_finished and repo.is_quest_finished(int(cid), qid)
        if is_daily and qid in daily_completed_ids:
            packets: list[bytes] = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1, 0x001F, build_accept_quest_ack(success=False, error_code=0x12)
                    )
                )
                correction, correction_note = pack_client_finished_quest_correction(
                    self.server,
                    character_id=int(cid),
                    allow_empty=True,
                )
                packets.extend(correction)
            else:
                correction_note = "S0/0164 correction skip=no_ack"
            return (
                packets,
                base + "; reject=daily_completed_today; " + correction_note,
            )
        if already_finished and not is_repeatable:
            packets = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1, 0x001F, build_accept_quest_ack(success=False, error_code=0x12)
                    )
                )
                correction, correction_note = pack_client_finished_quest_correction(
                    self.server,
                    character_id=int(cid),
                    allow_empty=True,
                )
                packets.extend(correction)
            else:
                correction_note = "S0/0164 correction skip=no_ack"
            return packets, base + "; reject=already_finished; " + correction_note

        active_rows = repo.list_active_quests(int(cid))
        finished_rows = repo.list_finished_quests(int(cid))
        active_ids = [int(q) for _s, q, _t in active_rows]
        finished_ids = [int(q) for q, _f in finished_rows]
        finished_flags = {
            int(quest_id) & 0xFFFF: int(flag) & 0xFF
            for quest_id, flag in finished_rows
            if int(flag) != 0
        }
        if not force_accept:
            acceptable = resolve_acceptable_ids(
                job=int(job or 0),
                level=int(level or 1),
                grow_type=int(grow or 0),
                active_ids=active_ids,
                finished_ids=finished_ids,
                finished_flags=finished_flags,
                daily_completed_ids=daily_completed_ids,
            )
            # 空可接列表也要拦：列表外（含“当前没有任何可接”）不得绕过资格门禁。
            if (
                env_bool("USDOF_GAME_QUEST_ACCEPT_REQUIRE_ACCEPTABLE", True)
                and qid not in acceptable
            ):
                packets = []
                if send_ack and env_bool("USDOF_GAME_QUEST_001F_SEND_FAIL_ACK", True):
                    packets.append(
                        self.server._codec_response(
                            1,
                            0x001F,
                            build_accept_quest_ack(success=False, error_code=0x15),
                        )
                    )
                return packets, base + "; reject=not_in_acceptable"

        init_trigger = 0
        try:
            init_trigger = int(
                compute_quest_init_trigger(
                    qid,
                    finished_quest_ids=(
                        [finished_id for finished_id in finished_ids if finished_id != qid]
                        if already_finished and is_repeatable
                        else finished_ids
                    ),
                )
            ) & 0xFFFFFFFF
        except Exception:
            init_trigger = 0

        event_items = []
        if policy == "default" and env_bool(
            "USDOF_GAME_QUEST_GRANT_ACCEPT_ITEMS", True
        ):
            event_items = resolve_accept_event_items(
                qid,
                player_job=int(job if job is not None else -1),
                player_grow_type=int(grow if grow is not None else -1),
            )

        ok, detail_store = True, "store_skipped"
        if env_bool("USDOF_GAME_QUEST_PERSIST_ACTIVE", True):
            if already_finished and is_repeatable:
                ok, detail_store = repo.reaccept_finished_quest(
                    int(cid),
                    qid,
                    trigger_value=init_trigger,
                )
            else:
                ok, detail_store = repo.accept_quest(int(cid), qid, trigger_value=init_trigger)

        granted = []
        mailed = []
        item_note = "event_items=0"
        if ok and event_items:
            pairs = [(it.item_id, it.count) for it in event_items]
            granted, mailed, item_note = repo.grant_quest_items(
                int(cid),
                pairs,
                quest_id=qid,
                reason="quest_accept_depend_give",
            )

        if ok and session is not None and (send_ack or not emit_packets):
            # Keep the server-side snapshot current even for internal batch accepts.
            rows = repo.list_active_quests(int(cid))
            pairs = [(int(q), int(tr)) for _s, q, tr in rows]
            fin = [int(q) for q, _f in repo.list_finished_quests(int(cid))]
            bind_session_quest_state(
                session,
                active_pairs=pairs,
                finished_ids=fin,
                last_quest_id=qid,
            )

        packets = []
        if send_ack and ok:
            ack_items = ()
            if env_bool("USDOF_GAME_QUEST_ACCEPT_ACK_INCLUDE_ITEMS", True) and granted:
                ack_items = tuple((slot, item_id, count) for slot, item_id, count in granted)
            packets.append(
                self.server._codec_response(
                    1,
                    0x001F,
                    build_accept_quest_ack(
                        success=True,
                        quest_id=qid,
                        init_trigger=init_trigger,
                        event_items=ack_items,
                    ),
                )
            )
            refresh, rnote = pack_quest_refresh_packets(
                self.server,
                character_id=int(cid),
                job=int(job or 0),
                level=int(level or 1),
                grow_type=int(grow or 0),
                session=session,
                include_0015=True,
                include_023f=True,
                include_0164=bool(already_finished and is_repeatable),
            )
            packets.extend(refresh)
            # init_trigger==0（见面/对话类）：S1/001F 只走 accept 路径 sub_10434A0(qid,0)；
            # 可交闪烁要再发 S1/0021(trigger=0) 走 sub_10434A0(qid,1)（IDA sub_160E080）。
            # 必须在 023F 之后，保证列表里已有该任务。
            if (
                env_bool("USDOF_GAME_QUEST_ACCEPT_ARM_COMPLETEABLE_0021", True)
                and int(init_trigger) == 0
            ):
                arm_pkts, arm_note = pack_completeable_trigger_acks(
                    self.server, [(qid, 0)]
                )
                packets.extend(arm_pkts)
                rnote = "%s;%s" % (rnote, arm_note)
            if granted and env_bool("USDOF_GAME_QUEST_ACCEPT_DELAYED_ITEMLIST", True):
                rows = repo.list_inventory_items(int(cid), list_type=0)
                items = [InventoryItemView(row) for row in rows]
                list_param16 = int(repo.get_container_list_param(int(cid), list_type=0))
                body_000d = build_item_list_mode0(
                    items,
                    list_type=0,
                    list_param16=list_param16,
                )
                if hasattr(self.server, "queue_delayed_response_packets"):
                    self.server.queue_delayed_response_packets(
                        [
                            (
                                self.server._codec_response(0, 0x000D, body_000d),
                                "accept delayed 0x000D itemlist n=%s" % len(items),
                            )
                        ]
                    )
            return packets, (
                "%s; store=%s; init_trigger=%s; ack=ok; event=%s mailed=%s; %s; %s"
                % (base, detail_store, init_trigger, len(granted), len(mailed), item_note, rnote)
            )
        if send_ack and (not ok) and env_bool("USDOF_GAME_QUEST_001F_SEND_FAIL_ACK", True):
            packets.append(
                self.server._codec_response(
                    1, 0x001F, build_accept_quest_ack(success=False, error_code=0x12)
                )
            )
        if ok:
            return packets, (
                "%s; store=%s; init_trigger=%s; ack=suppressed; event=%s mailed=%s; %s"
                % (base, detail_store, init_trigger, len(granted), len(mailed), item_note)
            )
        return packets, "%s; store=%s; ack=fail_or_off" % (base, detail_store)
