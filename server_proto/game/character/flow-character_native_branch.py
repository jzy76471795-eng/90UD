"""Dual-branch adapter for character-page and enter-Seria requests.

The adapter owns branch order only.  It does not touch SQLite, PVF, sockets,
or packet sequencing.  A native result with ``handled=True`` may short-circuit
the Python handler; every other result falls through to the existing Python
state machine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional

from native_core_loader import character_flow_dispatch as native_character_flow_dispatch


class CharacterFlowOperation(IntEnum):
    UNKNOWN = 0
    SELECT_CHARACTER = 1
    CREATE_CHARACTER = 2
    DELETE_CHARACTER = 3
    RETURN_SELECT_CHARACTER = 4
    GET_USERINFO = 5
    CHANGE_CHARAC_SLOT = 6
    CHECK_CHARACTER_NAME = 7
    CHARACTER_CARD_CLICK = 8
    LOAD_EXTEND_CHARACS = 9
    LOAD_EXTEND_CHARACS_FINISH = 10
    EXTEND_READY = 11
    CHANNEL_MOVE_SELECTING_SCENE = 12


_OPCODE_TO_OPERATION = {
    0x0004: CharacterFlowOperation.SELECT_CHARACTER,
    0x0005: CharacterFlowOperation.CREATE_CHARACTER,
    0x0006: CharacterFlowOperation.DELETE_CHARACTER,
    0x0007: CharacterFlowOperation.RETURN_SELECT_CHARACTER,
    0x0008: CharacterFlowOperation.GET_USERINFO,
    0x0127: CharacterFlowOperation.CHANGE_CHARAC_SLOT,
    0x02B5: CharacterFlowOperation.CHECK_CHARACTER_NAME,
    0x02C1: CharacterFlowOperation.CHARACTER_CARD_CLICK,
    0x02A8: CharacterFlowOperation.LOAD_EXTEND_CHARACS,
    0x01BA: CharacterFlowOperation.LOAD_EXTEND_CHARACS_FINISH,
    0x0355: CharacterFlowOperation.EXTEND_READY,
    0x057F: CharacterFlowOperation.CHANNEL_MOVE_SELECTING_SCENE,
}


@dataclass(frozen=True)
class CharacterFlowDispatchResult:
    """Result of one branch attempt.

    ``response_packets`` is intentionally a tuple so a native branch cannot
    mutate the Python route's packet list by aliasing it.
    """

    handled: bool
    operation_id: int
    response_packets: tuple[bytes, ...] = ()
    note: str = ""
    create_character_userinfo_refresh_pending: Optional[bool] = None


def normalize_character_flow_mode(value: Optional[str] = None) -> str:
    raw = os.environ.get("USDOF_CHARACTER_FLOW_CORE", "cpp") if value is None else value
    mode = str(raw).strip().lower()
    aliases = {
        "cpp": "cpp",
        "native": "cpp",
        "1": "cpp",
        "python": "python",
        "py": "python",
        "0": "python",
    }
    if mode not in aliases:
        raise ValueError("invalid USDOF_CHARACTER_FLOW_CORE mode: %r" % raw)
    return aliases[mode]


def operation_for_opcode(opcode: int) -> int:
    return int(_OPCODE_TO_OPERATION.get(int(opcode), CharacterFlowOperation.UNKNOWN))


def _native_probe(
    opcode: int,
    plain: bytes,
    *,
    server=None,
    session=None,
) -> CharacterFlowDispatchResult:
    expected_operation = operation_for_opcode(opcode)
    operation_id, handled = native_character_flow_dispatch(opcode, plain)
    if int(operation_id) != expected_operation:
        raise ValueError(
            "native character-flow operation mismatch: opcode=0x%04x expected=%d actual=%d"
            % (int(opcode), expected_operation, int(operation_id))
        )
    if int(handled) not in (0, 1):
        raise ValueError("native character-flow handled flag is outside 0/1")
    if not handled:
        return CharacterFlowDispatchResult(
            handled=False,
            operation_id=expected_operation,
            note="native character-flow probe operation=%d handled=0"
            % expected_operation,
        )
    if expected_operation != CharacterFlowOperation.SELECT_CHARACTER:
        raise ValueError(
            "native character-flow operation=%d has no Python host adapter"
            % expected_operation
        )
    if server is None or session is None:
        raise ValueError("native SELECT_CHARACTER requires server and session context")
    packets, note = server._handle_select_character_cpp(plain, session=session)
    return CharacterFlowDispatchResult(
        handled=True,
        operation_id=expected_operation,
        response_packets=tuple(packets),
        note="branch=cpp; " + note,
    )


def dispatch_character_flow(
    opcode: int,
    plain: bytes,
    *,
    cpp_handler: Optional[Callable[[], CharacterFlowDispatchResult]] = None,
    python_handler: Optional[Callable[[], CharacterFlowDispatchResult]] = None,
    error_logger: Optional[Callable[[str], None]] = None,
    server=None,
    session=None,
) -> Optional[CharacterFlowDispatchResult]:
    """Run C++ first and Python second for one registered character opcode.

    Returning ``None`` means the caller should continue its existing route.
    This is used by the first probe-only integration, where the unchanged
    Python opcode chain remains below this adapter.
    """

    operation_id = operation_for_opcode(opcode)
    if operation_id == CharacterFlowOperation.UNKNOWN:
        return None

    if normalize_character_flow_mode() == "python":
        return python_handler() if python_handler is not None else None

    try:
        result = (
            cpp_handler()
            if cpp_handler is not None
            else _native_probe(
                opcode,
                bytes(plain),
                server=server,
                session=session,
            )
        )
        if not isinstance(result, CharacterFlowDispatchResult):
            raise TypeError("character-flow branch must return CharacterFlowDispatchResult")
        if result.operation_id != operation_id:
            raise ValueError(
                "character-flow branch operation mismatch: expected=%d actual=%d"
                % (operation_id, result.operation_id)
            )
    except Exception as exc:
        if error_logger is not None:
            error_logger(
                "character-flow C++ branch failed operation=%d: %s: %s"
                % (operation_id, type(exc).__name__, exc)
            )
        return python_handler() if python_handler is not None else None

    if result.handled:
        return result
    return python_handler() if python_handler is not None else result


__all__ = [
    "CharacterFlowDispatchResult",
    "CharacterFlowOperation",
    "dispatch_character_flow",
    "normalize_character_flow_mode",
    "operation_for_opcode",
]
