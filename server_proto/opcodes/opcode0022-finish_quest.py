"""FINISH_QUEST C->S 0x0022 (modular crash-safe finish + PVF rewards).

多奖励动画（Stage3 IDA 2026-07-18 二次修正）
--------------------------------------------
S1 公共层 ``sub_1AB3810`` 剥 success 后，按 opcode 查 **mapA**
（``sub_1AB3EB0``），**不是** mapB（``sub_1AB4050`` / play-result）。

| 表 | 初始化 | opcode 0x22 handler | 用途 |
|---|---|---|---|
| mapA | ``sub_1AB3BC0`` → ``sub_1630830`` | **``sub_162C740``** | **FINISH_QUEST 真 handler** |
| mapB | ``sub_1AB3F90`` → ``sub_1649540`` | ``sub_1635AF0`` | PLAY_RESULT（另一张表） |

``sub_162C740`` chainType=0 读：
  u16 quest | u8 completion | u32 exp | **u32 gold** | consumed | chain |
  insertCount × **19B** 行
  （slot u16 | item u32 | countOrSeed u32 | up u8 | dura u16 | reserved u32 | flags u8 | **notify u8**）

与对照源差异：
  - S4A12：同骨架但 insert **18B**（无末尾 notify）→ 塞 18B 会在第 2 行错位闪退
  - 90qiyuan：无 gold 字段 + insert **20B** → 高版本 EXE，**不可照搬 Stage3**

因此 Stage3 定案：
  - S1/0022 ACK **带 19B insert** 驱动多奖励动画（默认 ON）
  - S0/000E 仅作背包兜底（默认 OFF，避免与 handler 内入包叠刷）
"""
from __future__ import annotations

import os
from typing import Any, Sequence

from game.character.adventure_group import (
    calculate_adventure_group_for_character,
)
from game.chain_loader import load_chain
from game.character.inventory_item_view import InventoryItemView
from game.character.item_metadata import resolve_item_metadata
from game.character.character_db import parse_virtual_premium_grants
from game.character.payloads import (
    build_item_list_mode0,
    build_item_list_update_000e as build_inventory_item_list_update_000e,
    build_userinfo_subtype1_min,
)
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.quest.daily_challenge import (
    arm_daily_challenge_clear_grade_event,
    handle_daily_challenge_entry_reward,
)
from game.quest.quest_common import (
    EQUIP_TOP_QUALITY_SEED,
    build_finish_quest_ack,
    env_bool,
    env_int,
    parse_quest_id_from_body,
)
from game.quest.quest_index import (
    daily_quest_day_key,
    get_quest_index,
    is_daily_quest_meta,
    question_answer_count,
    resolve_question_answer_flag,
)
from game.quest.quest_residual import (
    seeking_completion_consume_pairs,
    seeking_required_gold,
)
from game.quest.quest_rewards import parse_finish_extra_fields, resolve_quest_reward
from game.quest.quest_session import (
    bind_session_quest_state,
    pack_client_finished_quest_correction,
    pack_completeable_trigger_acks,
    pack_quest_refresh_packets,
    pack_set_trigger_ack_packets,
    resolve_selected_character_detail,
    sync_quest_clear_dependency_triggers,
)

_dungeon_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _dungeon_payloads.build_item_list_update_000e
build_wallet_gold_update_000e = _dungeon_payloads.build_wallet_gold_update_000e
clamp_wallet_gold_for_client = _dungeon_payloads.clamp_wallet_gold_for_client
_epic_chain_complete = load_chain("helper-epic_chain_complete.py")
complete_epic_chain_without_rewards = _epic_chain_complete.complete_epic_chain_without_rewards
_quest_completion = load_chain("helper-quest_completion_snapshot.py")
_character_exp = load_chain("helper-character_exp.py")
grant_exp_to_character = _character_exp.grant_exp_to_character
_level_titles = load_chain("helper-level_milestone_titles.py")
_mirror_mission = load_chain("helper-another_arad_quest.py")


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


def build_quest_reward_item_update_000e(
    repo: Any,
    *,
    character_id: int,
    granted_items: Sequence[tuple[int, int, int]],
    include_all: bool,
) -> tuple[bytes | None, int]:
    """Build the post-finish inventory update from persisted item instances.

    ``S1/0022`` reward inserts do not carry an absolute instance expiration.
    A reward with a PVF lifetime therefore needs the established full 108-byte
    ``S0/000E`` entry, whose offset 43 carries ``expire_time``. Ordinary
    rewards retain the existing opt-in update behavior; timed entries always
    receive this narrow refresh.
    """
    rows = repo.list_inventory_items(int(character_id), list_type=0)
    by_slot = {int(row.get("slot_index", -1)): row for row in rows}
    update_items: list[InventoryItemView] = []
    for slot, _item_id, _count in granted_items:
        row = by_slot.get(int(slot))
        if row is None:
            continue
        if not include_all and int(row.get("expire_time", 0) or 0) <= 0:
            continue
        update_items.append(InventoryItemView(row))
    if not update_items:
        return None, 0
    return (
        build_inventory_item_list_update_000e(update_items, list_type=0),
        len(update_items),
    )


class FinishQuest0022Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _handle_another_arad_finish(
        self,
        *,
        session: Any,
        character_id: int,
        quest_id: int,
        send_ack: bool,
        base: str,
    ) -> tuple[list[bytes], str] | None:
        state = _mirror_mission.get_another_arad_quest_state(session)
        if state is None or int(state.spec.quest_id) != (int(quest_id) & 0xFFFF):
            return None
        packets: list[bytes] = []
        if not state.completed or not state.settlement_evaluated:
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1,
                        0x0022,
                        build_finish_quest_ack(success=False, error_code=0x16),
                    )
                )
            return packets, (
                "%s; mirror_session=1 reject=%s progress=%d/%d"
                % (
                    base,
                    "incomplete" if not state.completed else "unsettled",
                    state.progress,
                    state.required,
                )
            )

        claim = _mirror_mission.claim_another_arad_reward(
            self.server,
            session,
            character_id=int(character_id),
        )
        insert_entries: list[tuple] = []
        if claim.granted and not claim.already_claimed:
            from game.quest.quest_common import build_finish_reward_insert_entries

            granted_meta: list[tuple[int, int, int, int, bool]] = []
            for slot, item_id, count in claim.granted:
                metadata = resolve_item_metadata(int(item_id))
                granted_meta.append(
                    (
                        int(slot) & 0xFFFF,
                        int(item_id) & 0xFFFFFFFF,
                        int(count) & 0xFFFFFFFF,
                        int(metadata.durability or 0) & 0xFFFF,
                        bool(metadata.is_equipment),
                    )
                )
            insert_entries = build_finish_reward_insert_entries(
                granted_items=granted_meta,
                notify_flag=env_int("USDOF_GAME_QUEST_FINISH_INSERT_NOTIFY", 0),
            )
        if send_ack:
            packets.append(
                self.server._codec_response(
                    1,
                    0x0022,
                    build_finish_quest_ack(
                        success=True,
                        quest_id=int(quest_id),
                        # Stage3 consumes this u32 as one completion-event
                        # quantity.  The reward amount itself is carried by
                        # the insert rows and the persisted wallet.
                        completion_event_quantity=(
                            0 if claim.already_claimed else 1
                        ),
                        insert_entries=insert_entries,
                    ),
                )
            )
            mirror_reward_note = str(getattr(claim, "note", "") or "")
            if mirror_reward_note:
                helper_activation = load_chain("helper-system_reward_activation.py")
                for premium_type, remaining in parse_virtual_premium_grants(
                    mirror_reward_note
                ):
                    if session is not None:
                        try:
                            session.account_premium_effects = None
                        except Exception:
                            pass
                    packets.append(
                        self.server._codec_response(
                            0,
                            0x0042,
                            helper_activation.build_premium_activation_notice(
                                premium_type, remaining
                            ),
                        )
                    )
        if claim.granted and not claim.already_claimed:
            body_000e = build_item_list_update_000e(claim.granted, list_type=0)
            packets.append(self.server._codec_response(0, 0x000E, body_000e))
        return packets, (
            "%s; mirror_session=1 persistent_active=0 complete=1 "
            "already_claimed=%d insert=%d granted=%d mailed=%d; %s"
            % (
                base,
                int(claim.already_claimed),
                len(insert_entries),
                len(claim.granted),
                len(claim.mailed),
                claim.note,
            )
        )

    def handle_finish_quest_0022(
        self,
        body: bytes,
        *,
        session: Any = None,
        force_complete: bool = False,
        reward_policy: str = "default",
        emit_packets: bool = True,
    ) -> tuple[list[bytes], str]:
        from game.quest import anton_volcano
        qid = parse_quest_id_from_body(body, expected_opcode=0x0022).get("quest_id")
        if qid != anton_volcano.QUEST_ID:
            return self._finish_quest(body, session, force_complete, reward_policy, emit_packets)
        repo = self.server._repo()
        cid = resolve_selected_character_detail(self.server, session).get("character_id")
        if not cid:
            return self._finish_quest(body, session, force_complete, reward_policy, emit_packets)
        try:
            with repo.inventory_transaction_scope():
                before = repo.count_inventory_items_by_template(cid, list_type=0, item_ids=[anton_volcano.CELL_ID])
                was_done = repo.is_quest_finished(cid, qid)
                result = self._finish_quest(body, session, force_complete, reward_policy, emit_packets)
                if not was_done and repo.is_quest_finished(cid, qid) and reward_policy == "default":
                    after = repo.count_inventory_items_by_template(cid, list_type=0, item_ids=[anton_volcano.CELL_ID])
                    if after.get(anton_volcano.CELL_ID, 0) != before.get(anton_volcano.CELL_ID, 0) + 1:
                        raise anton_volcano.MissingQuestReward()
                return result
        except anton_volcano.MissingQuestReward:
            ack = self.server._codec_response(1, 0x0022, build_finish_quest_ack(success=False, error_code=0x16))
            return [ack] if emit_packets else [], "personal volcano quest reward delivery failed"

    def _finish_quest(self, body, session, force_complete, reward_policy, emit_packets):
        policy = str(reward_policy or "default").strip().lower()
        # Internal completion tickets use state_only: task state, title-book,
        # slot and profession transitions persist, while tangible rewards do not.
        if policy not in {"default", "gold_only", "state_only"}:
            raise ValueError(
                "unsupported quest completion reward policy=%r" % policy
            )
        parsed = parse_quest_id_from_body(body, expected_opcode=0x0022)
        handle_on = env_bool("USDOF_GAME_QUEST_HANDLE_0022", True)
        send_ack = env_bool("USDOF_GAME_QUEST_0022_SEND_ACK", True) and bool(
            emit_packets
        )
        quest_id = parsed.get("quest_id")
        detail = resolve_selected_character_detail(self.server, session)
        cid = detail.get("character_id")
        job = detail.get("job")
        level = detail.get("level")
        grow = detail.get("grow_type")
        base = (
            "C1/0022 FINISH_QUEST layout=%s quest_id=%s cid=%s handle=%s force=%s "
            "reward_policy=%s emit=%s"
            % (
                parsed["layout"],
                quest_id,
                cid,
                int(handle_on),
                int(force_complete),
                policy,
                int(bool(emit_packets)),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF"
        if quest_id is None or int(quest_id) <= 0 or cid is None:
            return [], base + "; reject=invalid"
        qid = int(quest_id) & 0xFFFF
        repo = self.server._repo()
        mirror_result = self._handle_another_arad_finish(
            session=session,
            character_id=int(cid),
            quest_id=qid,
            send_ack=send_ack,
            base=base,
        )
        if mirror_result is not None:
            return mirror_result
        daily_result = handle_daily_challenge_entry_reward(
            self.server,
            character_repo=repo,
            character_id=int(cid),
            level=int(level or 1),
            quest_id=qid,
            session=session,
        )
        if daily_result is not None:
            packets, daily_note = daily_result
            return packets, "%s; %s" % (base, daily_note)
        if repo.is_quest_finished(int(cid), qid):
            # 思路.md 7/11：已完成幂等拒绝 + 0164 纠正客户端防重复交
            packets: list[bytes] = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1, 0x0022, build_finish_quest_ack(success=False, error_code=0x16)
                    )
                )
                correction, correction_note = pack_client_finished_quest_correction(
                    self.server,
                    character_id=int(cid),
                    allow_empty=False,
                )
                packets.extend(correction)
            else:
                correction_note = "S0/0164 correction skip=no_ack"
            return (
                packets,
                base
                + "; reject=already_finished finish-fail-ack already-cleared; "
                + correction_note,
            )
        active_rows = repo.list_active_quests(int(cid))
        active = {int(q): int(t) for _s, q, t in active_rows}
        if qid not in active:
            packets = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1, 0x0022, build_finish_quest_ack(success=False, error_code=0x16)
                    )
                )
            return packets, base + "; reject=not_active"
        from game.quest import anton_volcano
        if qid == anton_volcano.QUEST_ID and not anton_volcano.has_quest_clear(repo, int(cid)):
            packets = [self.server._codec_response(1, 0x0022, build_finish_quest_ack(
                success=False, error_code=0x16))] if send_ack else []
            return packets, base + "; reject=personal_anton_volcano_clear_required"
        if qid == anton_volcano.QUEST_ID and repo.count_inventory_items_by_template(
            int(cid), list_type=0, item_ids=[anton_volcano.HIDE_ID]
        ).get(anton_volcano.HIDE_ID, 0) < 3:
            packets = [self.server._codec_response(1, 0x0022, build_finish_quest_ack(
                success=False, error_code=0x16))] if send_ack else []
            return packets, base + "; reject=anton_hides_required"
        dependency_updates, dependency_note = sync_quest_clear_dependency_triggers(
            repo,
            character_id=int(cid),
        )
        if dependency_updates:
            active_rows = repo.list_active_quests(int(cid))
            active = {int(q): int(t) for _s, q, t in active_rows}
        extra = parse_finish_extra_fields(
            body,
            quest_id_layout=str(parsed.get("layout") or ""),
        )
        meta = get_quest_index().get_meta(qid)
        answer_count = question_answer_count(meta)
        question_flag = 1
        if answer_count is not None and not force_complete:
            resolved_flag = resolve_question_answer_flag(
                meta,
                trigger_value=int(active.get(qid, 0)),
                reward_select_idx=int(extra.get("reward_select_idx", -1)),
            )
            if resolved_flag is None:
                packets = []
                if send_ack:
                    packets.append(
                        self.server._codec_response(
                            1,
                            0x0022,
                            build_finish_quest_ack(
                                success=False,
                                error_code=0x16,
                            ),
                        )
                    )
                return packets, (
                    base
                    + "; reject=question_answer trigger=%s answers=%s select=%s"
                    % (
                        active.get(qid),
                        answer_count,
                        extra.get("reward_select_idx", -1),
                    )
                )
            question_flag = int(resolved_flag)
        elif (
            not force_complete
            and env_bool("USDOF_GAME_QUEST_FINISH_REQUIRE_TRIGGER0", True)
            and int(active.get(qid, 0)) != 0
        ):
            packets = []
            if send_ack:
                packets.append(
                    self.server._codec_response(
                        1, 0x0022, build_finish_quest_ack(success=False, error_code=0x16)
                    )
                )
            return packets, base + "; reject=trigger_nonzero=%s" % active.get(qid)

        consume_seeking = (
            env_bool("USDOF_GAME_QUEST_FINISH_CONSUME_SEEKING", True)
            and not force_complete
        )
        seeking_gold_cost = (
            seeking_required_gold(meta)
            if consume_seeking and meta is not None
            else 0
        )
        if seeking_gold_cost > 0:
            wallet_ok, wallet_note, wallet = repo.get_account_wallet(int(cid))
            wallet_total = int(wallet.get("gold", 0)) if wallet_ok else 0
            if not wallet_ok or wallet_total < seeking_gold_cost:
                packets = []
                if send_ack:
                    packets.append(
                        self.server._codec_response(
                            1,
                            0x0022,
                            build_finish_quest_ack(
                                success=False,
                                error_code=0x16,
                            ),
                        )
                    )
                return packets, (
                    base
                    + "; reject=seeking_gold have=%s need=%s detail=%s"
                    % (wallet_total, seeking_gold_cost, wallet_note)
                )

        if env_bool("USDOF_GAME_QUEST_COMPLETE_MAIN_CHAIN_NO_REWARD", False) or env_bool(
            "USDOF_GAME_QUEST_FINISH_EPIC_CHAIN_NO_REWARD", False
        ):
            batch = complete_epic_chain_without_rewards(
                repo,
                character_id=int(cid),
                quest_id=qid,
            )
            if batch.is_epic:
                packets = []
                if not batch.ok:
                    if send_ack:
                        packets.append(
                            self.server._codec_response(
                                1,
                                0x0022,
                                build_finish_quest_ack(success=False, error_code=0x16),
                            )
                        )
                    return packets, (
                        "%s; main_chain=%s ids=%s no_reward=1 persist=%s"
                        % (base, batch.chain_key, len(batch.quest_ids), batch.note)
                    )

                if send_ack:
                    packets.append(
                        self.server._codec_response(
                            1,
                            0x0022,
                            build_finish_quest_ack(
                                success=True,
                                quest_id=qid,
                                # This is a real quest completion, not a
                                # reward amount; Stage3 dispatches one event.
                                completion_event_quantity=1,
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
                    # Do not feed the client task-complete loop a new chapter.
                    include_0015=False,
                    include_023f=True,
                    include_0164=True,
                )
                packets.extend(refresh)
                return packets, (
                    "%s; main_chain=%s ids=%s added=%s no_reward=1 "
                    "no_economy=1 no_items=1 no_consume=1; %s"
                    % (
                        base,
                        batch.chain_key,
                        len(batch.quest_ids),
                        batch.finished_added,
                        rnote,
                    )
                )

        reward = resolve_quest_reward(
            qid,
            player_level=int(level or 1),
            player_job=int(job if job is not None else -1),
            player_grow_type=int(grow if grow is not None else -1),
            reward_select_idx=int(extra.get("reward_select_idx", -1)),
            multiplier=int(extra.get("multiplier", 1)),
        )

        # fallback defaults only when PVF disabled / missing
        # note 可能是 "ok" / "ok title=1" / "ok clear=N"；凡以 ok 开头即解析成功
        exp = int(reward.exp)
        gold = int(reward.gold)
        note_ok = str(reward.note or "").startswith("ok")
        if not note_ok:
            exp = env_int("USDOF_GAME_QUEST_FINISH_DEFAULT_EXP", exp)
            gold = env_int("USDOF_GAME_QUEST_FINISH_DEFAULT_GOLD", gold)
        if policy in {"gold_only", "state_only"}:
            exp = 0
        if policy == "state_only":
            gold = 0
        grant_rewards = env_bool("USDOF_GAME_QUEST_GRANT_REWARDS", True)
        grant_items = (
            policy == "default"
            and env_bool("USDOF_GAME_QUEST_GRANT_ITEMS", True)
            and grant_rewards
        )
        apply_economy = (
            policy != "state_only"
            and env_bool("USDOF_GAME_QUEST_APPLY_ECONOMY", True)
            and grant_rewards
        )

        # 收集类(seeking)：交任务时丢弃当前材料的多余数量，但必须先为其它
        # active seeking 任务保留需求量，避免共享模板的任务互相清空材料。
        consumed_entries: list[tuple[int, int, int]] = []
        consume_update_rows: list[tuple[int, int, int]] = []
        consume_note = "consume_skip"
        seeking_gold_spent = 0
        seeking_gold_total = 0
        if consume_seeking:
            quest_index = get_quest_index()
            meta_q = quest_index.get_meta(qid)
            seek_pairs = (
                seeking_completion_consume_pairs(meta_q)
                if meta_q is not None
                else []
            )
            if seeking_gold_cost > 0:
                gold_ok, gold_note, seeking_gold_total = repo.spend_wallet_gold(
                    int(cid),
                    seeking_gold_cost,
                )
                if not gold_ok:
                    packets = []
                    if send_ack:
                        packets.append(
                            self.server._codec_response(
                                1,
                                0x0022,
                                build_finish_quest_ack(
                                    success=False,
                                    error_code=0x16,
                                ),
                            )
                        )
                    return packets, (
                        base
                        + "; reject=seeking_gold_race need=%s detail=%s"
                        % (seeking_gold_cost, gold_note)
                    )
                seeking_gold_spent = seeking_gold_cost
                consume_note = "seeking_gold paid=%s remain=%s" % (
                    seeking_gold_spent,
                    seeking_gold_total,
                )
            if seek_pairs:
                reserved_by_item: dict[int, int] = {}
                for _slot, other_qid, _trigger in active_rows:
                    other_id = int(other_qid) & 0xFFFF
                    if other_id <= 0 or other_id == qid:
                        continue
                    other_meta = quest_index.get_meta(other_id)
                    if other_meta is None:
                        continue
                    for other_item_id, other_need in seeking_completion_consume_pairs(
                        other_meta
                    ):
                        iid_other = int(other_item_id) & 0xFFFFFFFF
                        reserved_by_item[iid_other] = (
                            int(reserved_by_item.get(iid_other, 0))
                            + max(0, int(other_need))
                        )
                notes_c: list[str] = []
                for item_id, _need in seek_pairs:
                    keep_count = int(reserved_by_item.get(int(item_id), 0))
                    ok_c, cnote, muts = repo.consume_seeking_inventory_by_template(
                        int(cid),
                        int(item_id),
                        required_count=int(_need),
                        reserved_count=keep_count,
                        list_type=0,
                    )
                    notes_c.append(cnote if ok_c else "fail:%s" % cnote)
                    for m in muts:
                        slot = int(m.get("slot_index", -1))
                        removed = int(m.get("removed", 0) or 0)
                        remaining = int(m.get("remaining", 0) or 0)
                        if slot < 0 or removed <= 0:
                            continue
                        # S1/0022 consumed: u8 type=0 | u16 slot | u32 count(removed)
                        consumed_entries.append((0, slot & 0xFFFF, removed & 0xFFFFFFFF))
                        consume_update_rows.append(
                            (
                                slot & 0xFFFF,
                                int(item_id) if remaining > 0 else -1,
                                max(0, remaining),
                            )
                        )
                item_consume_note = "seeking_consume " + ";".join(notes_c)
                consume_note = (
                    "%s;%s" % (consume_note, item_consume_note)
                    if consume_note != "consume_skip"
                    else item_consume_note
                )

        econ_note = "economy_skip"
        progression_result = None
        level_title_grants = []
        level_title_note = ""
        if apply_economy and (exp or gold):
            if exp and os.environ.get("USDOF_CN2018_SHOP") == "1":
                from game.character.exp_bonus_consumable import get_active_exp_bonus_effect

                effect = get_active_exp_bonus_effect(repo, int(cid), quest_bonus=True)
                if effect is not None:
                    exp = min(0xFFFFFFFF, exp + effect.compute_bonus_exp(exp))
            progression_result = grant_exp_to_character(
                repo,
                int(cid),
                exp,
                gold_delta=gold,
                session=session,
            )
            econ_note = str(progression_result.get("note") or "")
            if progression_result.get("ok"):
                level = int(progression_result.get("new_level") or level or 1)
                rec = detail.get("record")
                if rec is not None:
                    try:
                        from dataclasses import replace as _dc_replace

                        detail["record"] = _dc_replace(
                            rec,
                            level=level,
                            exp=int(progression_result.get("exp") or 0),
                            expertise_exp=int(
                                progression_result.get("expertise_exp") or 0
                            ),
                        )
                        detail["level"] = level
                    except Exception:
                        pass
                if _level_titles.has_crossed_level_title_threshold(
                    int(progression_result.get("old_level") or 1),
                    int(progression_result.get("new_level") or 1),
                ):
                    try:
                        level_title_grants, level_title_note = (
                            _level_titles.claim_level_title_rewards(
                                repo,
                                character_id=int(cid),
                                previous_level=int(
                                    progression_result.get("old_level") or 1
                                ),
                                current_level=int(
                                    progression_result.get("new_level") or 1
                                ),
                            )
                        )
                    except Exception as exc:
                        level_title_note = "level_title err=%s" % type(exc).__name__
            elif policy == "gold_only":
                packets: list[bytes] = []
                if send_ack:
                    packets.append(
                        self.server._codec_response(
                            1,
                            0x0022,
                            build_finish_quest_ack(success=False, error_code=0x16),
                        )
                    )
                return packets, base + "; reject=gold_reward_persist %s" % econ_note

        granted: list[tuple[int, int, int]] = []
        mailed: list[tuple[int, int]] = []
        item_note = "items_skip"
        if grant_items and reward.items:
            pairs = [(it.item_id, it.count) for it in reward.items if it.item_id > 0 and it.count > 0]
            granted, mailed, item_note = repo.grant_quest_items(
                int(cid),
                pairs,
                quest_id=qid,
                reason="quest_finish_reward",
            )

        # 称号奖励：不进背包，写入称号簿（S0/0168+0166 见下方组包）
        title_granted: list[dict] = []
        title_note = "title_skip"
        if (
            policy != "gold_only"
            and grant_rewards
            and env_bool("USDOF_GAME_QUEST_GRANT_TITLEBOOK", True)
            and (reward.is_title or reward.title_items)
        ):
            title_ids = [
                int(it.item_id)
                for it in (reward.title_items or ())
                if int(it.item_id) > 0
            ]
            if title_ids:
                _title_grant = load_chain("helper-titlebook_grant.py")
                title_granted, title_note = _title_grant.grant_title_rewards(
                    repo,
                    character_id=int(cid),
                    quest_id=qid,
                    title_item_ids=title_ids,
                )
            else:
                title_note = "title_skip empty_items"

        # 转职/觉醒：先落 grow_type + 技能/SP，再 mark finished + 组 ACK
        # Stage3 IDA sub_162C740：chainType∈{1,2,20} 读 growNumber 后重建 actor
        # 并切 UI 模式 13（转职动画）；服务端须先提交 DB，再发 ACK/0013/USERINFO0。
        profession_note = "profession_skip"
        profession_result = None
        chain_type = int(reward.chain_type or 0)
        grow_number = int(getattr(reward, "grow_number", 0) or 0)
        slot_expansion_ok = True
        slot_expansion_refresh = False
        slot_expansion_note = "slot_expansion_skip"
        if policy != "gold_only" and grant_rewards and chain_type == 21:
            slot_expansion_ok, slot_state, slot_expansion_note = repo.unlock_ex_equip_slot(
                int(cid),
                grow_number,
            )
            slot_expansion_refresh = bool(slot_expansion_ok)
            if slot_expansion_ok:
                rec = detail.get("record")
                if rec is not None:
                    try:
                        from dataclasses import replace as _dc_replace

                        detail["record"] = _dc_replace(
                            rec,
                            ex_equip_slot_stat=int(slot_state) & 0xFF,
                        )
                    except Exception:
                        pass
        if (
            env_bool("USDOF_GAME_QUEST_APPLY_PROFESSION", True)
            and policy != "gold_only"
            and grant_rewards
            and chain_type in (1, 2)
            and grow_number > 0
            and not (policy == "state_only" and chain_type == 1)
        ):
            _job_change = load_chain("helper-job_change.py")
            profession_result = _job_change.apply_profession_change(
                repo,
                character_id=int(cid),
                job=int(job or 0),
                level=int(level or 1),
                current_grow_type=int(grow if grow is not None else 0),
                chain_type=chain_type,
                grow_number=grow_number,
            )
            profession_note = (
                "profession ok=%s chain=%s grow=%s 0x%02X->0x%02X skills=%s sp=%s; %s"
                % (
                    int(bool(profession_result.ok)),
                    profession_result.chain_type,
                    profession_result.grow_number,
                    profession_result.previous_grow_type,
                    profession_result.new_grow_type,
                    len(profession_result.skills),
                    profession_result.remaining_sp,
                    profession_result.note,
                )
            )
            if profession_result.ok:
                grow = int(profession_result.new_grow_type) & 0xFF
                if session is not None:
                    try:
                        session.character_grow_type = grow
                    except Exception:
                        pass
                # 刷新 detail.record 的 grow，供 USERINFO0 使用
                rec = detail.get("record")
                if rec is not None:
                    try:
                        from dataclasses import replace as _dc_replace

                        detail["record"] = _dc_replace(rec, grow_type=grow)
                        detail["grow_type"] = grow
                    except Exception:
                        pass

        ok = bool(slot_expansion_ok)
        persist_detail = "finished_skip_persist" if ok else slot_expansion_note
        if ok and env_bool("USDOF_GAME_QUEST_PERSIST_FINISHED", True):
            ok, persist_detail = repo.mark_quest_finished(
                int(cid),
                qid,
                flag_value=int(question_flag),
                daily_completed_on=(
                    daily_quest_day_key() if is_daily_quest_meta(meta) else None
                ),
                count_for_adventure=policy == "default" and not force_complete,
            )

        if ok:
            post_dependency_updates, post_dependency_note = (
                sync_quest_clear_dependency_triggers(
                    repo,
                    character_id=int(cid),
                )
            )
            if post_dependency_updates:
                by_quest = {
                    int(quest_id): (int(quest_id), int(old), int(new))
                    for quest_id, old, new in dependency_updates
                }
                for quest_id, old, new in post_dependency_updates:
                    prior = by_quest.get(int(quest_id))
                    by_quest[int(quest_id)] = (
                        int(quest_id),
                        int(prior[1]) if prior is not None else int(old),
                        int(new),
                    )
                dependency_updates = list(by_quest.values())
            dependency_note = "%s;%s" % (
                dependency_note,
                post_dependency_note,
            )

        finished_quest_ids: tuple[int, ...] = ()
        quest_snapshot = None
        if ok:
            quest_snapshot = _quest_completion.load_finished_quest_snapshot(repo, int(cid))
            finished_quest_ids = quest_snapshot.finished_quest_ids
        clear_grade_event_note = "daily_clear_grade_event=not_armed"
        clear_grade_daily_ids: tuple[int, ...] = ()
        claimed_clear_grade_ids: tuple[int, ...] = ()
        if ok and session is not None:
            rows = repo.list_active_quests(int(cid))
            pairs = [(int(q), int(tr)) for _s, q, tr in rows]
            fin = finished_quest_ids
            bind_session_quest_state(session, active_pairs=pairs, finished_ids=fin, last_quest_id=qid)
            clear_grade_event_note = arm_daily_challenge_clear_grade_event(
                session,
                character_repo=repo,
                character_id=int(cid),
                source_quest_id=qid,
                source_grade=str(getattr(meta, "grade", "") or ""),
            )
            clear_grade_event = getattr(
                session,
                "daily_challenge_clear_grade_event",
                None,
            )
            if isinstance(clear_grade_event, dict):
                clear_grade_daily_ids = tuple(
                    int(value) & 0xFFFF
                    for value in (clear_grade_event.get("challenge_ids") or ())
                    if int(value) > 0
                )
                claimed_clear_grade_ids = tuple(
                    int(value) & 0xFFFF
                    for value in (
                        clear_grade_event.get("claimed_challenge_ids") or ()
                    )
                    if int(value) > 0
                )

        # Stage3 consumes the second S1/0022 u32 as the quantity for event 10,
        # not as the quest's gold reward.  A successful finish always emits
        # one event; the actual gold amount stays in the item_id=0 insert row
        # and the persisted wallet.  The old conditional implementation only
        # fixed clear-grade Daily entries, leaving ordinary gold quests able to
        # fan out thousands of client-side trigger events.
        ack_include_gold = env_bool("USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_GOLD", True)
        completion_event_quantity = 1
        if clear_grade_daily_ids:
            clear_grade_quantity_note = (
                "daily_completion_quantity=1 clear_grade_ids=%s"
                % ",".join(str(value) for value in clear_grade_daily_ids)
            )
        else:
            clear_grade_quantity_note = (
                "daily_completion_quantity=1 source=finish_ack"
            )

        from game.quest.personality_rewards import get_personality_reward_index
        from game.quest.time_fragment_rewards import (
            finished_time_fragment_count,
            is_time_fragment_reward_quest,
        )

        personality_reward_refresh = qid in get_personality_reward_index()
        time_fragment_reward_refresh = is_time_fragment_reward_quest(
            qid,
            job_id=int(job or 0),
        )
        special_reward_refresh = bool(
            personality_reward_refresh or time_fragment_reward_refresh
        )
        map_unlock_refresh = qid in get_quest_index().get_map_unlock_quest_ids()

        packets: list[bytes] = []
        if send_ack and ok:
            # Stage3：转职前可先推 S0/0013，让客户端在 ACK 重建 actor 时已有新技能树
            if (
                profession_result is not None
                and profession_result.ok
                and env_bool("USDOF_GAME_QUEST_PROFESSION_PRE_ACK_SKILLINFO", True)
            ):
                _job_change = load_chain("helper-job_change.py")
                packets.append(
                    _job_change.pack_job_change_skillinfo(
                        self.server,
                        skills=profession_result.skills,
                        remaining_sp=profession_result.remaining_sp,
                        remaining_tp=profession_result.remaining_tp,
                    )
                )

            # S1/0022 ACK：sub_162C740 读 19B insert 驱动多奖励动画（默认 ON）
            insert_entries: list[tuple] = []
            if env_bool("USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_ITEMS", True) and chain_type == 0:
                from game.quest.quest_common import build_finish_reward_insert_entries

                bag_by_slot: dict[int, dict] = {}
                if granted:
                    try:
                        bag_by_slot = {
                            int(r["slot_index"]): r
                            for r in repo.list_inventory_items(int(cid), list_type=0)
                        }
                    except Exception:
                        bag_by_slot = {}
                granted_meta: list[tuple[int, int, int, int, bool]] = []
                for slot, item_id, count in granted:
                    meta = resolve_item_metadata(int(item_id))
                    row = bag_by_slot.get(int(slot))
                    if row is not None:
                        dura = max(0, int(row.get("durability", 0) or 0)) & 0xFFFF
                    else:
                        dura = max(0, int(getattr(meta, "durability", 0) or 0)) & 0xFFFF
                    granted_meta.append(
                        (
                            int(slot) & 0xFFFF,
                            int(item_id) & 0xFFFFFFFF,
                            int(count) & 0xFFFFFFFF,
                            int(dura),
                            bool(meta.is_equipment),
                        )
                    )
                gold_for_insert = gold if ack_include_gold else 0
                insert_entries = build_finish_reward_insert_entries(
                    gold=int(gold_for_insert),
                    granted_items=granted_meta,
                    notify_flag=env_int("USDOF_GAME_QUEST_FINISH_INSERT_NOTIFY", 0),
                )
            packets.append(
                self.server._codec_response(
                    1,
                    0x0022,
                    build_finish_quest_ack(
                        success=True,
                        quest_id=qid,
                        exp=exp if env_bool("USDOF_GAME_QUEST_FINISH_ACK_INCLUDE_EXP", True) else 0,
                        completion_event_quantity=int(completion_event_quantity),
                        chain_type=chain_type,
                        grow_number=grow_number,
                        slot_expansion_actor_key=(int(cid) & 0xFFFF),
                        insert_entries=insert_entries,
                        consumed_entries=consumed_entries,
                    ),
                )
            )

            # Account-owned contract rewards are consumed in the repository
            # transaction and have no S1/0022 inventory insert row. Publish the
            # resulting timer only after the quest success ACK is accepted.
            for premium_type, remaining in parse_virtual_premium_grants(item_note):
                if session is not None:
                    try:
                        session.account_premium_effects = None
                    except Exception:
                        pass
                helper_activation = load_chain("helper-system_reward_activation.py")
                packets.append(
                    self.server._codec_response(
                        0,
                        0x0042,
                        helper_activation.build_premium_activation_notice(
                            premium_type, remaining
                        ),
                    )
                )

            # 任务材料 000E（默认 ON；清空写 -1，预留材料写剩余模板/数量）
            consume_000e_n = 0
            if seeking_gold_spent > 0 and env_bool(
                "USDOF_GAME_QUEST_FINISH_CONSUME_SEND_000E", True
            ):
                body_wallet = build_wallet_gold_update_000e(seeking_gold_total)
                packets.append(self.server._codec_response(0, 0x000E, body_wallet))
                consume_000e_n += 1
            if consume_update_rows and env_bool(
                "USDOF_GAME_QUEST_FINISH_CONSUME_SEND_000E", True
            ):
                body_consume = build_item_list_update_000e(
                    consume_update_rows, list_type=0
                )
                packets.append(self.server._codec_response(0, 0x000E, body_consume))
                consume_000e_n = len(consume_update_rows)

            # S0/000E：普通奖励背包兜底默认 OFF；但带 PVF 期限的实例必须
            # 用完整 108B 条目补齐绝对 expire_time，0022 insert 本身没有该字段。
            reward_000e_n = consume_000e_n
            reward_wallet_000e_sent = False
            clear_grade_wallet_note = "daily_wallet_000e=0"
            send_reward_000e = (
                env_bool("USDOF_GAME_QUEST_FINISH_SEND_000E", False)
                and int(reward.chain_type or 0) == 0
            )
            if send_reward_000e:
                if gold > 0 and env_bool("USDOF_GAME_QUEST_FINISH_000E_INCLUDE_GOLD", True):
                    try:
                        _exp_now, _g, wallet, _j, _gr = repo.get_character_economy(int(cid))
                        wallet_total = int(wallet if wallet is not None else gold)
                    except Exception:
                        wallet_total = int(gold)
                    body_wallet = build_wallet_gold_update_000e(wallet_total)
                    packets.append(self.server._codec_response(0, 0x000E, body_wallet))
                    reward_000e_n += 1
                    reward_wallet_000e_sent = True
                    if clear_grade_daily_ids:
                        clear_grade_wallet_note = (
                            "daily_wallet_000e=1 total=%d" % wallet_total
                        )
            # The normal reward 000E path is deliberately OFF.  A clear-grade
            # event still needs the authoritative post-reward wallet projection
            # because its S1/0022 event quantity is no longer the gold amount.
            if (
                clear_grade_daily_ids
                and gold > 0
                and progression_result is not None
                and progression_result.get("ok")
                and not reward_wallet_000e_sent
            ):
                try:
                    _exp_now, _g, wallet_total, _j, _gr = repo.get_character_economy(
                        int(cid)
                    )
                    body_wallet = build_wallet_gold_update_000e(wallet_total)
                    packets.append(self.server._codec_response(0, 0x000E, body_wallet))
                    reward_000e_n += 1
                    reward_wallet_000e_sent = True
                    clear_grade_wallet_note = (
                        "daily_wallet_000e=1 total=%d" % int(wallet_total)
                    )
                except Exception as exc:
                    clear_grade_wallet_note = (
                        "daily_wallet_000e=error=%s" % type(exc).__name__
                    )
            if granted:
                try:
                    body_000e, update_count = build_quest_reward_item_update_000e(
                        repo,
                        character_id=int(cid),
                        granted_items=granted,
                        include_all=send_reward_000e,
                    )
                except Exception:
                    body_000e, update_count = None, 0
                if body_000e is not None:
                    packets.append(self.server._codec_response(0, 0x000E, body_000e))
                    reward_000e_n += int(update_count)

            # 转职后：USERINFO0（新 grow）+ 再发一次 0013（兜底，若 pre-ack 被关）
            if profession_result is not None and profession_result.ok:
                _job_change = load_chain("helper-job_change.py")
                if env_bool("USDOF_GAME_QUEST_PROFESSION_SEND_USERINFO0", True):
                    rec = detail.get("record")
                    if rec is not None:
                        equipment = getattr(rec, "equipped_entries", None) or ()
                        packets.append(
                            _job_change.pack_job_change_userinfo0(
                                self.server,
                                record=rec,
                                equipment=equipment,
                            )
                        )
                        packets.append(
                            _job_change.pack_job_change_avatar_visibility(
                                self.server,
                                record=rec,
                            )
                        )
                if env_bool("USDOF_GAME_QUEST_PROFESSION_POST_ACK_SKILLINFO", False):
                    packets.append(
                        _job_change.pack_job_change_skillinfo(
                            self.server,
                            skills=profession_result.skills,
                            remaining_sp=profession_result.remaining_sp,
                            remaining_tp=profession_result.remaining_tp,
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
                include_0164=env_bool(
                    # S0/0164 is a complete replacement. Ordinary tasks keep
                    # it off; stateful tasks opt in to publish their
                    # authoritative completion projection immediately.
                    "USDOF_GAME_REPLY_FINISH_QUEST_SEND_0164", False
                ) or (
                    special_reward_refresh
                    and env_bool(
                        "USDOF_GAME_QUEST_FINISH_SPECIAL_REWARD_SEND_0164",
                        True,
                    )
                ) or (
                    map_unlock_refresh
                    and env_bool(
                        "USDOF_GAME_QUEST_FINISH_MAP_UNLOCK_SEND_0164",
                        True,
                    )
                ),
                # The Daily reward path already completed its own terminal
                # projection and rebuilt 0287 without this row. A later main
                # quest refresh must not put the claimed ID back into 0164.
                exclude_finished_quest_ids=claimed_clear_grade_ids,
            )
            packets.extend(refresh)

            dependency_pairs = [
                (int(quest_id), int(new))
                for quest_id, _old, new in dependency_updates
            ]
            nonzero_pairs = [pair for pair in dependency_pairs if pair[1] != 0]
            zero_pairs = [pair for pair in dependency_pairs if pair[1] == 0]
            dependency_packet_notes: list[str] = []
            if nonzero_pairs:
                dependency_packets, dependency_packet_note = (
                    pack_set_trigger_ack_packets(self.server, nonzero_pairs)
                )
                packets.extend(dependency_packets)
                dependency_packet_notes.append(dependency_packet_note)
            if zero_pairs:
                dependency_packets, dependency_packet_note = (
                    pack_completeable_trigger_acks(self.server, zero_pairs)
                )
                packets.extend(dependency_packets)
                dependency_packet_notes.append(dependency_packet_note)
            if dependency_packet_notes:
                rnote = "%s;%s;%s" % (
                    rnote,
                    dependency_note,
                    ";".join(dependency_packet_notes),
                )

            # 称号簿：DB 已按 titlebook.etc **固定槽**写入。
            # 默认发 S0/0168 获取动画；默认不叠 0166（live 交 1028 叠 0166 曾闪）。
            # 关通知: USDOF_GAME_TITLEBOOK_SEND_ON_FINISH=0
            title_pkt_note = "title_pkts=0"
            title_pkts: list[bytes] = []
            if title_granted:
                _title_grant = load_chain("helper-titlebook_grant.py")
                title_pkts, title_pkt_note = _title_grant.pack_title_grant_packets(
                    self.server,
                    character_id=int(cid),
                    quest_id=qid,
                    granted_rows=title_granted,
                )
            if level_title_grants:
                level_title_pkts, level_title_packet_note = (
                    _level_titles.pack_level_title_grant_packets(
                        self.server,
                        grants=level_title_grants,
                    )
                )
                title_pkts.extend(level_title_pkts)
                title_pkt_note = "%s;%s" % (
                    title_pkt_note,
                    level_title_packet_note,
                )
            if level_title_note:
                title_note = "%s;%s" % (title_note, level_title_note)

            delayed: list[tuple[bytes, str]] = []
            # 默认 delayed 发出 0168，避开 FINISH 同步风暴
            title_delayed = env_bool("USDOF_GAME_TITLEBOOK_FINISH_0166_DELAYED", True)
            if title_pkts:
                if title_delayed:
                    for i, pkt in enumerate(title_pkts):
                        delayed.append(
                            (
                                pkt,
                                "finish delayed titlebook i=%s %s"
                                % (i, title_pkt_note if i == 0 else ""),
                            )
                        )
                    title_pkt_note = title_pkt_note + ";delayed=1"
                else:
                    packets.extend(title_pkts)
                    title_pkt_note = title_pkt_note + ";delayed=0"

            # 默认 OFF：全量 000D 与 000E 叠发易闪退；需要时再开
            if env_bool("USDOF_GAME_QUEST_FINISH_DELAYED_ITEMLIST", False) and (
                granted or gold > 0 or env_bool("USDOF_GAME_QUEST_FINISH_ALWAYS_ITEMLIST", False)
            ):
                rows = repo.list_inventory_items(int(cid), list_type=0)
                items = [InventoryItemView(row) for row in rows]
                list_param16 = int(repo.get_container_list_param(int(cid), list_type=0))
                body_000d = build_item_list_mode0(
                    items,
                    list_type=0,
                    list_param16=list_param16,
                )
                delayed.append(
                    (
                        self.server._codec_response(0, 0x000D, body_000d),
                        "finish delayed 0x000D itemlist n=%s" % len(items),
                    )
                )
            time_fragment_count = finished_time_fragment_count(
                finished_quest_ids,
                job_id=int(job or 0),
            )
            profession_refresh = bool(
                profession_result is not None and profession_result.ok
            )
            if env_bool("USDOF_GAME_QUEST_FINISH_DELAYED_USERINFO1", True) and (
                profession_refresh
                or exp
                or gold
                or slot_expansion_refresh
                or personality_reward_refresh
                or time_fragment_reward_refresh
            ):
                # Rebuild USERINFO1 after progression or profession changes.
                # 硬约束：equipment 必须带 live 穿戴；空 equip_count 会经
                # sub_166AED0 清空客户端装备栏（2026-07-21：通关交任务领装后槽全空）。
                # 副本内默认跳过 delayed U1（mid-dungeon reprime 禁令），经验仍靠 ACK/0025。
                rec = detail.get("record")
                in_dungeon = bool(
                    getattr(session, "in_dungeon_run", False) if session is not None else False
                )
                skip_u1_in_dungeon = in_dungeon and not env_bool(
                    "USDOF_GAME_QUEST_FINISH_DELAYED_USERINFO1_IN_DUNGEON", False
                )
                if rec is not None and not skip_u1_in_dungeon:
                    exp_now, _g, _w, _j, _gr = repo.get_character_economy(int(cid))
                    equipment = ()
                    equip_note = "equip=none"
                    try:
                        equip_helper = load_chain("helper-userinfo_equipment.py")
                        equipment, equip_note = equip_helper.resolve_enter_room_equipment(
                            rec,
                            enabled=True,
                            backfill_from_pvf=False,
                            repo=repo,
                            prefer_live_db=True,
                        )
                    except Exception as equip_exc:
                        equipment = getattr(rec, "equipped_entries", None) or ()
                        equip_note = "equip_fallback=%s err=%s" % (
                            len(equipment),
                            type(equip_exc).__name__,
                        )
                    adventure_progression = calculate_adventure_group_for_character(
                        repo,
                        rec,
                    )
                    adventure_group_level = int(
                        adventure_progression.adventure_level
                    )
                    guild_membership_active = has_active_guild_membership(repo, rec)
                    u1 = build_userinfo_subtype1_min(
                        rec,
                        exp=int(exp_now),
                        entry_flag=0,
                        skills=(
                            profession_result.skills if profession_refresh else None
                        ),
                        equipment=equipment,
                        finished_quest_ids=finished_quest_ids,
                        adventure_group_level=adventure_group_level,
                        guild_membership_active=guild_membership_active,
                    )
                    delayed.append(
                        (
                            self.server._codec_response(0, 0x0002, u1),
                            "finish delayed 0x0002 userinfo1 exp=%s equip_n=%s skill_n=%s %s "
                            "time_fragments=%s adventure_level=%s %s"
                            % (
                                exp_now,
                                len(equipment),
                                (
                                    len(profession_result.skills)
                                    if profession_refresh
                                    else 0
                                ),
                                equip_note,
                                time_fragment_count,
                                adventure_group_level,
                                quest_snapshot.describe() if quest_snapshot else "quest_snapshot=none",
                            ),
                        )
                    )
                    delayed.extend(
                        pack_guild_membership_status_rows(
                            self.server,
                            guild_membership_active,
                        )
                    )
                # skip_u1_in_dungeon：不入 delayed（mid-dungeon 禁 U1 reprime）
            if delayed and hasattr(self.server, "queue_delayed_response_packets"):
                self.server.queue_delayed_response_packets(delayed)

            return packets, (
                "%s; finished_persist=%s; reward=%s exp=%s gold=%s items=%s mailed=%s "
                "title=%s select=%s mult=%s path=%s chain=%s grow=%s; econ=%s; %s; %s; %s; "
                "%s; %s; %s; insert=%s; consumed=%s; 000e=%s; delayed=%s"
                % (
                    base,
                    persist_detail,
                    reward.note,
                    exp,
                    gold,
                    len(granted),
                    len(mailed),
                    len(title_granted),
                    extra.get("reward_select_idx", -1),
                    extra.get("multiplier", 1),
                    reward.source_path or "-",
                    chain_type,
                    grow_number,
                    econ_note,
                    item_note,
                    title_note,
                    title_pkt_note,
                    consume_note,
                    profession_note,
                    rnote,
                    len(insert_entries),
                    len(consumed_entries),
                    reward_000e_n,
                    len(delayed),
                )
                + "; %s; %s" % (slot_expansion_note, clear_grade_event_note)
                + "; %s; %s" % (clear_grade_quantity_note, clear_grade_wallet_note)
            )
        return packets, "%s; finished_persist=%s; %s" % (
            base,
            persist_detail,
            "%s; %s" % (clear_grade_event_note, clear_grade_quantity_note),
        )
