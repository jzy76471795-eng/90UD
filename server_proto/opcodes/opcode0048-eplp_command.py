"""C1/0x0048 EPLP_COMMAND — 通关结算「回城 / 再挑战」.

s4a12::
  ENUM_CMDPACKET_EPLP_COMMAND = 0x0048
  CardRewardService.HandleEplpCommand → state==1 则 ReturnToVillage

Stage3 live: ``state=2`` 是按钮 focus；提交态 ``state=1`` 中，``option=0``
是连续/下一地下城路径，``option=2`` 是返回城镇。此包不是 0x0084
（0x0084 多用于选本离开）。
本 handler 在普通副本 ``in_dungeon_run && clear_phase>=2`` 时接管；
湘南大会仅在原生最终奖励交互已打开且两组奖励均已交付后接管右上角按钮；
否则由 flow-dungeon_enter.handle_0048 当 ENTER_PRE 处理。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain

_payloads = load_chain("helper-dungeon_payloads.py")
_leave = load_chain("flow-dungeon_leave_select.py")
_linked_chain = load_chain("helper-dungeon_linked_chain.py")
_altar = load_chain("flow-altar-random.py")
build_eplp_command_ack_0048 = _payloads.build_eplp_command_ack_0048
build_card_select_right_state_0045 = _payloads.build_card_select_right_state_0045
build_card_layout_ack_0046 = _payloads.build_card_layout_ack_0046
arm_linked_dungeon_retry = _linked_chain.arm_linked_dungeon_retry
clear_linked_dungeon_retry = _linked_chain.clear_linked_dungeon_retry
clear_altar_session = _altar.clear_altar_session


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def _parse_eplp(body: bytes) -> tuple[int, int]:
    raw = bytes(body or b"")
    if len(raw) >= 2:
        return int(raw[0]) & 0xFF, int(raw[1]) & 0xFF
    if len(raw) == 1:
        return int(raw[0]) & 0xFF, 2
    # empty body after clear: treat as return-to-town
    return 1, 2


class EplpCommand0048Service:
    def __init__(self, server: Any) -> None:
        self.server = server
        self._leave = _leave.DungeonLeaveSelectService(server)

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    @staticmethod
    def _is_tournament_result_button_context(session: Any) -> bool:
        if session is None or not bool(
            getattr(session, "dungeon_tournament_completed", False)
        ):
            return False
        try:
            tournament_id = int(
                getattr(session, "dungeon_tournament_dungeon_id", 0) or 0
            )
            run_id = int(
                getattr(session, "cur_dungeon_id", 0)
                or getattr(session, "selected_dungeon_id", 0)
                or 0
            )
        except (TypeError, ValueError):
            return False
        if tournament_id <= 0 or run_id != tournament_id:
            return False

        reward_state = getattr(
            session, "dungeon_tournament_reward_selection", None
        )
        if not isinstance(reward_state, dict) or not bool(
            reward_state.get("opened", False)
        ):
            return False
        delivery_states = reward_state.get("delivery_states")
        selected_indexes = reward_state.get("selected_indexes")
        if (
            not isinstance(delivery_states, list)
            or len(delivery_states) != 2
            or any(str(value) != "delivered" for value in delivery_states)
            or not isinstance(selected_indexes, list)
            or len(selected_indexes) != 2
        ):
            return False
        try:
            return all(int(value) >= 0 for value in selected_indexes)
        except (TypeError, ValueError):
            return False

    def is_settlement_context(self, session: Any) -> bool:
        if session is None:
            return False
        in_run = bool(getattr(session, "in_dungeon_run", False))
        phase = int(getattr(session, "dungeon_clear_phase", 0) or 0)
        return in_run and (
            phase >= 2 or self._is_tournament_result_button_context(session)
        )

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_DUNGEON_EPLP_HANDLE", True):
            return [], "C1/0048 EPLP gate=OFF"
        if not self.is_settlement_context(session):
            return [], "C1/0048 EPLP skip=not_settlement (caller should fall back to ENTER_PRE)"

        state, option = _parse_eplp(body)
        packets: list[bytes] = []
        notes: list[str] = []
        altar_settlement = bool(
            getattr(session, "altar_final_clear_pending", False)
        )
        tournament_result_buttons = self._is_tournament_result_button_context(
            session
        )

        if altar_settlement:
            ack = build_eplp_command_ack_0048(state if state else 1, option)
            pkt_a, mode_a = self._emit(1, 0x0048, ack, "codec")
            packets.append(pkt_a)
            notes.append(
                "altar_0048_ACK/%s/len=%d state=%s option=%s"
                % (mode_a, len(ack), state, option)
            )
            if state != 1:
                notes.append("altar_focus_ack_only=1")
            elif not bool(getattr(session, "altar_result_groups_sent", False)):
                notes.append("altar_wait_result_groups=1")
            else:
                notes.append(
                    clear_linked_dungeon_retry(
                        session,
                        reason="altar_committed_return",
                    )
                )
                leave_pkts, leave_note = self._leave.handle_leave(
                    b"", session=session, opcode=0x0048
                )
                packets.extend(leave_pkts)
                notes.append("altar_leave=[%s]" % leave_note)
                clear_altar_session(session)
            return packets, (
                "C1/0048 EPLP altar state=%s option=%s reply=[%s]; "
                "card_layout=altar_native_00D0_00D1"
                % (state, option, ", ".join(notes))
            )

        if tournament_result_buttons:
            ack = build_eplp_command_ack_0048(state if state else 1, option)
            pkt_a, mode_a = self._emit(1, 0x0048, ack, "codec")
            packets.append(pkt_a)
            notes.append(
                "tournament_0048_ACK/%s/len=%d state=%s option=%s"
                % (mode_a, len(ack), state, option)
            )
            if state == 1:
                notes.append(
                    clear_linked_dungeon_retry(
                        session,
                        reason="tournament_committed_option_%d" % option,
                    )
                )
                leave_pkts, leave_note = self._leave.handle_leave(
                    b"", session=session, opcode=0x0048
                )
                packets.extend(leave_pkts)
                notes.append("tournament_leave=[%s]" % leave_note)
            else:
                notes.append("tournament_focus_ack_only=1")
            return packets, (
                "C1/0048 EPLP tournament state=%s option=%s reply=[%s]; "
                "card_layout=tournament_native_0185_01CA_01CB"
                % (state, option, ", ".join(notes))
            )

        skip_card = _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_FLIP", False)

        layout_sent = bool(getattr(session, "dungeon_card_layout_sent", False))
        # 缺布局时补发 0045/0046（结算 UI 关键）。SKIP 只跳过 0047 交互，不挡布局。
        if (not layout_sent) and _env_bool(
            "USDOF_GAME_DUNGEON_EPLP_SEND_CARD_IF_MISSING", True
        ):
            body_45 = build_card_select_right_state_0045(1)
            pkt45, mode45 = self._emit(1, 0x0045, body_45, "codec")
            packets.append(pkt45)
            body_46 = build_card_layout_ack_0046()
            pkt46, mode46 = self._emit(1, 0x0046, body_46, "codec")
            packets.append(pkt46)
            session.dungeon_card_layout_sent = True
            notes.append(
                "card_layout_late 0045/%s 0046/%s skip_flip=%s"
                % (mode45, mode46, int(skip_card))
            )
            # s4a12: first EPLP while ResultShown only opens cards, does not leave.
            # 但用户已点回城；若 state==1 仍继续 leave，避免二次点击。
            if state != 1 and _env_bool("USDOF_GAME_DUNGEON_EPLP_FIRST_ONLY_LAYOUT", False):
                ack = build_eplp_command_ack_0048(state, option)
                pkt_a, mode_a = self._emit(1, 0x0048, ack, "codec")
                packets.append(pkt_a)
                return packets, (
                    "C1/0048 EPLP state=%s option=%s reply=[%s, 0048/%s]; "
                    "opened_cards_only; chain_file=game/chains/opcode0048-eplp_command.py"
                    % (state, option, ", ".join(notes), mode_a)
                )
        elif skip_card:
            notes.append("skip_flip=1 layout_ok=%s" % int(layout_sent))

        # Stage3 sends state=2 when the pointer focuses a settlement button.
        # Echo that UI notification, but only a committed state=1 command may
        # advance into the leave flow.
        ack = build_eplp_command_ack_0048(state if state else 1, option)
        pkt_a, mode_a = self._emit(1, 0x0048, ack, "codec")
        packets.append(pkt_a)
        notes.append("0048_ACK/%s/len=%d state=%s option=%s" % (mode_a, len(ack), state, option))

        if state == 1:
            if option == 0:
                notes.append(arm_linked_dungeon_retry(session))
            else:
                notes.append(
                    clear_linked_dungeon_retry(
                        session,
                        reason="committed_option_%d" % option,
                    )
                )
            leave_pkts, leave_note = self._leave.handle_leave(
                b"", session=session, opcode=0x0048
            )
            packets.extend(leave_pkts)
            notes.append("leave=[%s]" % leave_note)
        else:
            notes.append("focus_ack_only=1")

        return packets, (
            "C1/0048 EPLP state=%s option=%s reply=[%s]; "
            "doc_ref=s4a12 CardRewardService.HandleEplpCommand; "
            "chain_file=game/chains/opcode0048-eplp_command.py"
            % (state, option, ", ".join(notes))
        )
