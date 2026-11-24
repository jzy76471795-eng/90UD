"""C1/01CB tournament final-card selection and reward delivery."""
from __future__ import annotations

from typing import Any

from game.chain_loader import load_chain
from game.character.character_db import parse_virtual_premium_grant


_payloads = load_chain("helper-dungeon_payloads.py")
_tournament = load_chain("helper-dungeon_tournament.py")
build_item_list_update_000e = _payloads.build_item_list_update_000e


def _character_id(session: Any) -> int:
    if session is None:
        return 0
    for attr in ("selected_character_id", "character_id", "cid"):
        value = getattr(session, attr, None)
        try:
            character_id = int(value)
        except (TypeError, ValueError):
            continue
        if character_id > 0:
            return character_id
    return 0


class TournamentRewardSelect01CBService:
    """Deliver exactly one selected card from each tournament reward group."""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        raw = bytes(body or b"")
        if len(raw) != 2:
            return [], "C1/01CB TOURNAMENT_REWARD_SELECT reject=len=%d" % len(raw)

        card_type = int(raw[0])
        card_index = int(raw[1])
        return self.deliver_selection(
            session=session,
            card_type=card_type,
            card_index=card_index,
            source="client",
        )

    def deliver_selection(
        self,
        *,
        session: Any,
        card_type: int,
        card_index: int,
        source: str,
    ) -> tuple[list[bytes], str]:
        """Run the shared reserve, persist, commit, and projection transaction."""
        reward, reserve_note = _tournament.reserve_tournament_reward_selection(
            session,
            card_type=card_type,
            card_index=card_index,
        )
        if reward is None:
            return [], "C1/01CB TOURNAMENT_REWARD_SELECT %s" % reserve_note

        character_id = _character_id(session)
        if character_id <= 0:
            _tournament.rollback_tournament_reward_selection(
                session,
                card_type=card_type,
                card_index=card_index,
            )
            return [], (
                "C1/01CB TOURNAMENT_REWARD_SELECT rollback=no_character "
                "%s" % reserve_note
            )

        update_entries: list[tuple[int, int, int]] = []
        grant_note = ""
        try:
            repo = self.server._repo()
            if int(reward.item_id) == 0:
                ok, grant_note, wallet_total = repo.grant_wallet_gold(
                    character_id,
                    max(0, int(reward.amount)),
                )
                if ok:
                    update_entries.append(
                        (
                            0,
                            0,
                            _payloads.clamp_wallet_gold_for_client(wallet_total),
                        )
                    )
            elif int(reward.item_id) > 0 and int(reward.amount) > 0:
                ok, grant_note, slot = repo.grant_inventory_item(
                    character_id,
                    int(reward.item_id),
                    int(reward.amount),
                )
                if ok and int(slot) >= 0:
                    row = repo.get_inventory_item_at(
                        character_id,
                        list_type=0,
                        slot_index=int(slot),
                    )
                    projected_item_id = int(
                        (row or {}).get("item_template_id", reward.item_id)
                        or reward.item_id
                    )
                    projected_count = int(
                        (row or {}).get("stack_count", reward.amount)
                        or reward.amount
                    )
                    update_entries.append(
                        (
                            int(slot) & 0xFFFF,
                            projected_item_id & 0xFFFFFFFF,
                            projected_count & 0xFFFFFFFF,
                        )
                    )
            else:
                ok = True
                grant_note = "empty_reward"
        except Exception as exc:
            ok = False
            grant_note = "grant_error=%s" % type(exc).__name__

        if not ok:
            _tournament.rollback_tournament_reward_selection(
                session,
                card_type=card_type,
                card_index=card_index,
            )
            return [], (
                "C1/01CB TOURNAMENT_REWARD_SELECT rollback type=%d index=%d "
                "%s %s" % (card_type, card_index, grant_note, reserve_note)
            )

        if not _tournament.commit_tournament_reward_selection(
            session,
            card_type=card_type,
            card_index=card_index,
        ):
            return [], (
                "C1/01CB TOURNAMENT_REWARD_SELECT delivered_state_mismatch "
                "type=%d index=%d" % (card_type, card_index)
            )

        try:
            body_01cb = _tournament.build_tournament_reward_selection_01cb(
                session
            )
        except ValueError as exc:
            return [], (
                "C1/01CB TOURNAMENT_REWARD_SELECT projection_error=%s "
                "type=%d index=%d"
                % (str(exc), card_type, card_index)
            )

        packets: list[bytes] = []
        premium_packet: bytes | None = None
        premium_notice = parse_virtual_premium_grant(grant_note)
        if premium_notice is not None:
            if session is not None:
                try:
                    session.account_premium_effects = None
                except Exception:
                    pass
            helper = load_chain("helper-system_reward_activation.py")
            premium_packet = (
                self.server._codec_response(
                    0,
                    0x0042,
                    helper.build_premium_activation_notice(*premium_notice),
                )
            )
        if update_entries:
            body_000e = build_item_list_update_000e(update_entries, list_type=0)
            packets.append(self.server._codec_response(0, 0x000E, body_000e))
        # Stage3 S1/01CB reader 0x015F6A70 uses this projection to mark the
        # selected card and advance the tournament-result interaction state.
        # Its bytes are party slots: 0 for this solo player and 0xFF for an
        # unselected card.  S0/000E alone only refreshes the inventory.
        packets.append(self.server._codec_response(1, 0x01CB, body_01cb))
        if premium_packet is not None:
            packets.append(premium_packet)

        return packets, (
            "C1/01CB TOURNAMENT_REWARD_SELECT delivered source=%s type=%d index=%d "
            "item=%d amount=%d 000E=%d reply=S1/01CB body=%s %s %s"
            % (
                str(source),
                card_type,
                card_index,
                int(reward.item_id),
                int(reward.amount),
                int(bool(update_entries)),
                body_01cb.hex(),
                grant_note,
                reserve_note,
            )
        )
