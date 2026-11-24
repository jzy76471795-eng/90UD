"""Send an ordinary dungeon's free reward card from the client timer."""
from __future__ import annotations

import os
from typing import Any, List, Tuple

from game.chain_loader import load_chain


_select_card = load_chain("opcode0047-select_card.py")
_FREE_CARD_REQUEST = b"\x00\x00"
_SHONAN_TOURNAMENT_DUNGEON_IDS = frozenset((120, 121, 122))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def cancel_card_reward_auto_flip(session: Any) -> None:
    if session is None:
        return
    session.dungeon_card_auto_flip_armed = False
    session.dungeon_card_auto_flip_deadline_monotonic = 0.0
    session.dungeon_card_auto_flip_run_generation = 0


def is_shonan_tournament_session(session: Any) -> bool:
    if session is None:
        return False
    try:
        dungeon_id = int(
            getattr(session, "dungeon_tournament_dungeon_id", 0) or 0
        )
    except (TypeError, ValueError):
        return False
    return dungeon_id in _SHONAN_TOURNAMENT_DUNGEON_IDS


def arm_card_reward_auto_flip(
    session: Any,
) -> str:
    """Arm one flip after 002E has frozen the current run's reward plan."""
    cancel_card_reward_auto_flip(session)
    if session is None:
        return "card_auto_flip=inactive reason=no_session"
    session.dungeon_card_auto_flip_fired = False
    if is_shonan_tournament_session(session):
        return "card_auto_flip=inactive reason=shonan_tournament"
    if not _env_bool("USDOF_GAME_DUNGEON_CARD_AUTO_FLIP", True):
        return "card_auto_flip=inactive reason=gate_off"
    if _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_FLIP", False):
        return "card_auto_flip=inactive reason=skip_mode"
    if not bool(getattr(session, "in_dungeon_run", False)):
        return "card_auto_flip=inactive reason=not_in_run"
    phase = int(getattr(session, "dungeon_clear_phase", 0) or 0)
    if phase < 2:
        return "card_auto_flip=inactive reason=not_settled"
    if bool(getattr(session, "dungeon_card_flipped", False)):
        return "card_auto_flip=inactive reason=already_flipped"
    plan = getattr(session, "dungeon_card_reward_plan", None)
    if isinstance(plan, dict) and bool(plan.get("free_delivered", False)):
        return "card_auto_flip=inactive reason=reward_delivered"

    generation = int(getattr(session, "dungeon_run_generation", 0) or 0)
    session.dungeon_card_auto_flip_armed = True
    session.dungeon_card_auto_flip_run_generation = generation
    return "card_auto_flip=armed source=client_0045 generation=%d" % generation


def collect_client_card_reward_auto_flip_packets(
    server: Any,
    *,
    session: Any,
) -> Tuple[List[bytes], str]:
    """Consume Stage3 C1/0045 and directly build the free-card result path."""
    if session is None or not bool(
        getattr(session, "dungeon_card_auto_flip_armed", False)
    ):
        return [], "card_auto_flip=inactive"

    if is_shonan_tournament_session(session):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=shonan_tournament"

    if not _env_bool("USDOF_GAME_DUNGEON_CARD_AUTO_FLIP", True):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=gate_off"
    if _env_bool("USDOF_GAME_DUNGEON_SKIP_CARD_FLIP", False):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=skip_mode"

    armed_generation = int(
        getattr(session, "dungeon_card_auto_flip_run_generation", 0) or 0
    )
    current_generation = int(getattr(session, "dungeon_run_generation", 0) or 0)
    if armed_generation != current_generation:
        cancel_card_reward_auto_flip(session)
        return [], (
            "card_auto_flip=cancelled reason=run_changed armed=%d current=%d"
            % (armed_generation, current_generation)
        )
    if not bool(getattr(session, "in_dungeon_run", False)):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=left_run"
    if int(getattr(session, "dungeon_clear_phase", 0) or 0) < 2:
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=settlement_ended"
    if bool(getattr(session, "dungeon_card_flipped", False)):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=already_flipped"
    plan = getattr(session, "dungeon_card_reward_plan", None)
    if isinstance(plan, dict) and bool(plan.get("free_delivered", False)):
        cancel_card_reward_auto_flip(session)
        return [], "card_auto_flip=cancelled reason=reward_delivered"

    # Consume before entering the reward transaction. A duplicate client
    # signal must not repeat the same persistence transaction.
    session.dungeon_card_auto_flip_armed = False
    session.dungeon_card_auto_flip_deadline_monotonic = 0.0
    try:
        packets, note = _select_card.SelectCard0047Service(server).handle(
            _FREE_CARD_REQUEST,
            session=session,
        )
    except Exception as exc:
        return [], "card_auto_flip=error type=%s" % type(exc).__name__
    session.dungeon_card_auto_flip_fired = bool(
        getattr(session, "dungeon_card_flipped", False)
    )
    state = "fired" if session.dungeon_card_auto_flip_fired else "not_delivered"
    return list(packets), "card_auto_flip=%s source=client_0045; %s" % (
        state,
        note,
    )
