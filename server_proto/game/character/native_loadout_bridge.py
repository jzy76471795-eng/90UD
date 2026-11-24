"""Side-channel protocol for dungeon-native one-click loadout moves.

The GM control command cannot safely mutate dungeon equipment rows ahead of the
client.  This module publishes a compact, atomically replaced move plan for
``usdof_native_loadout.dll``.  The DLL calls the frozen client native move
method one action at a time; the Game session releases the next action only
after the normal ``C1/013E`` acknowledgement for ``S1/0013`` arrives.

The file layout is intentionally fixed and duplicated in the DLL.  It carries
only local fixture state and is not a network or codec transport.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from game.client_toast_bridge import (
    ClientToastBridgeError,
    TOAST_MODE_TOAST,
    send_client_toast,
)


PLAN_MAGIC = b"ULNP"
ACK_MAGIC = b"ULNA"
PROTOCOL_VERSION = 1
ACK_OK = 1
ACK_FAILED = 2

# magic, version, action-width, plan-id, character-id, action-count, unix-ms
_PLAN_HEADER = struct.Struct("<4sHHQIIQ")
# source-list, source-slot, destination-list, destination-slot, item-id
_PLAN_ACTION = struct.Struct("<BhBhI")
# magic, version, status, reserved, plan-id, action-index, action-count
_ACK_HEADER = struct.Struct("<4sHBBQII")

SESSION_STATE_ATTR = "_usdof_native_loadout_state"
SESSION_COOLDOWN_UNTIL_ATTR = "_usdof_native_loadout_cooldown_until_monotonic"
DEFAULT_TIMEOUT_SECONDS = 20.0
DUNGEON_LOADOUT_COOLDOWN_SECONDS = 10.0
MAX_ACTION_COUNT = 256


class NativeLoadoutBridgeError(ValueError):
    """Raised when a local native-loadout side-channel file is malformed."""


@dataclass(frozen=True)
class NativeLoadoutAction:
    source_list_type: int
    source_slot: int
    destination_list_type: int
    destination_slot: int
    item_id: int

    def matches_move(self, move: Mapping[str, Any]) -> bool:
        return (
            int(move.get("source_list_type", -1)) == int(self.source_list_type)
            and int(move.get("source_slot", -1)) == int(self.source_slot)
            and int(move.get("destination_list_type", -1))
            == int(self.destination_list_type)
            and int(move.get("destination_slot", -1)) == int(self.destination_slot)
            and int(move.get("move_count", 0)) == 1
            and int(move.get("source_instance_value", 0)) & 0xFFFFFFFF
            == int(self.item_id) & 0xFFFFFFFF
        )

    def pack(self) -> bytes:
        source_list_type = int(self.source_list_type)
        destination_list_type = int(self.destination_list_type)
        source_slot = int(self.source_slot)
        destination_slot = int(self.destination_slot)
        item_id = int(self.item_id)
        if not 0 <= source_list_type <= 0xFF:
            raise NativeLoadoutBridgeError("source list type is out of range")
        if not 0 <= destination_list_type <= 0xFF:
            raise NativeLoadoutBridgeError("destination list type is out of range")
        if not -0x8000 <= source_slot <= 0x7FFF:
            raise NativeLoadoutBridgeError("source slot is out of range")
        if not -0x8000 <= destination_slot <= 0x7FFF:
            raise NativeLoadoutBridgeError("destination slot is out of range")
        if not 0 < item_id <= 0xFFFFFFFF:
            raise NativeLoadoutBridgeError("native action item id is invalid")
        return _PLAN_ACTION.pack(
            source_list_type,
            source_slot,
            destination_list_type,
            destination_slot,
            item_id,
        )

    @classmethod
    def unpack(cls, value: bytes) -> "NativeLoadoutAction":
        if len(value) != _PLAN_ACTION.size:
            raise NativeLoadoutBridgeError("native action size is invalid")
        return cls(*_PLAN_ACTION.unpack(value))


@dataclass(frozen=True)
class NativeLoadoutPlan:
    plan_id: int
    character_id: int
    created_unix_ms: int
    actions: tuple[NativeLoadoutAction, ...]

    def pack(self) -> bytes:
        plan_id = int(self.plan_id)
        character_id = int(self.character_id)
        created_unix_ms = int(self.created_unix_ms)
        actions = tuple(self.actions)
        if not 0 < plan_id <= 0xFFFFFFFFFFFFFFFF:
            raise NativeLoadoutBridgeError("native plan id is invalid")
        if not 0 < character_id <= 0xFFFFFFFF:
            raise NativeLoadoutBridgeError("native plan character id is invalid")
        if not 0 <= created_unix_ms <= 0xFFFFFFFFFFFFFFFF:
            raise NativeLoadoutBridgeError("native plan timestamp is invalid")
        if not 0 < len(actions) <= MAX_ACTION_COUNT:
            raise NativeLoadoutBridgeError("native plan action count is invalid")
        header = _PLAN_HEADER.pack(
            PLAN_MAGIC,
            PROTOCOL_VERSION,
            _PLAN_ACTION.size,
            plan_id,
            character_id,
            len(actions),
            created_unix_ms,
        )
        return header + b"".join(action.pack() for action in actions)

    @classmethod
    def unpack(cls, value: bytes) -> "NativeLoadoutPlan":
        if len(value) < _PLAN_HEADER.size:
            raise NativeLoadoutBridgeError("native plan is shorter than its header")
        (
            magic,
            version,
            action_size,
            plan_id,
            character_id,
            action_count,
            created_unix_ms,
        ) = _PLAN_HEADER.unpack_from(value)
        if magic != PLAN_MAGIC or version != PROTOCOL_VERSION:
            raise NativeLoadoutBridgeError("native plan identity is invalid")
        if action_size != _PLAN_ACTION.size:
            raise NativeLoadoutBridgeError("native plan action layout is invalid")
        if not 0 < action_count <= MAX_ACTION_COUNT:
            raise NativeLoadoutBridgeError("native plan action count is invalid")
        expected_size = _PLAN_HEADER.size + action_count * action_size
        if len(value) != expected_size:
            raise NativeLoadoutBridgeError("native plan payload length is invalid")
        actions = tuple(
            NativeLoadoutAction.unpack(
                value[offset : offset + action_size]
            )
            for offset in range(_PLAN_HEADER.size, expected_size, action_size)
        )
        return cls(
            plan_id=int(plan_id),
            character_id=int(character_id),
            created_unix_ms=int(created_unix_ms),
            actions=actions,
        )


@dataclass(frozen=True)
class NativeLoadoutAck:
    plan_id: int
    action_index: int
    action_count: int
    status: int

    def pack(self) -> bytes:
        if int(self.status) not in (ACK_OK, ACK_FAILED):
            raise NativeLoadoutBridgeError("native acknowledgement status is invalid")
        if not 0 < int(self.plan_id) <= 0xFFFFFFFFFFFFFFFF:
            raise NativeLoadoutBridgeError("native acknowledgement plan id is invalid")
        if not 0 <= int(self.action_index) < MAX_ACTION_COUNT:
            raise NativeLoadoutBridgeError("native acknowledgement index is invalid")
        if not 0 < int(self.action_count) <= MAX_ACTION_COUNT:
            raise NativeLoadoutBridgeError("native acknowledgement action count is invalid")
        return _ACK_HEADER.pack(
            ACK_MAGIC,
            PROTOCOL_VERSION,
            int(self.status),
            0,
            int(self.plan_id),
            int(self.action_index),
            int(self.action_count),
        )

    @classmethod
    def unpack(cls, value: bytes) -> "NativeLoadoutAck":
        if len(value) != _ACK_HEADER.size:
            raise NativeLoadoutBridgeError("native acknowledgement length is invalid")
        magic, version, status, _reserved, plan_id, action_index, action_count = (
            _ACK_HEADER.unpack(value)
        )
        if magic != ACK_MAGIC or version != PROTOCOL_VERSION:
            raise NativeLoadoutBridgeError("native acknowledgement identity is invalid")
        return cls(
            plan_id=int(plan_id),
            action_index=int(action_index),
            action_count=int(action_count),
            status=int(status),
        )


@dataclass
class NativeLoadoutSessionState:
    plan: NativeLoadoutPlan
    next_action_index: int = 0
    created_monotonic: float = field(default_factory=time.monotonic)
    last_progress_monotonic: float = field(default_factory=time.monotonic)

    @property
    def current_action(self) -> NativeLoadoutAction | None:
        if 0 <= int(self.next_action_index) < len(self.plan.actions):
            return self.plan.actions[int(self.next_action_index)]
        return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _sidechannel_path(env_name: str, filename: str) -> Path:
    raw = str(os.environ.get(env_name, "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_repo_root() / "USDOF" / filename).resolve()


def native_loadout_plan_path() -> Path:
    return _sidechannel_path(
        "USDOF_NATIVE_LOADOUT_PLAN_PATH",
        "USDOFLoadoutNativePlan.bin",
    )


def native_loadout_ack_path() -> Path:
    return _sidechannel_path(
        "USDOF_NATIVE_LOADOUT_ACK_PATH",
        "USDOFLoadoutNativeAck.bin",
    )


def native_loadout_enabled() -> bool:
    """Whether this server session is configured to inject the native bridge."""
    raw = str(os.environ.get("USDOF_NATIVE_LOADOUT", "") or "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _atomic_write(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(target))
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _remove_file(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def write_native_loadout_plan(plan: NativeLoadoutPlan) -> Path:
    path = native_loadout_plan_path()
    _atomic_write(path, plan.pack())
    return path


def read_native_loadout_plan(path: Path | None = None) -> NativeLoadoutPlan:
    source = Path(path) if path is not None else native_loadout_plan_path()
    return NativeLoadoutPlan.unpack(source.read_bytes())


def write_native_loadout_ack(ack: NativeLoadoutAck) -> Path:
    path = native_loadout_ack_path()
    _atomic_write(path, ack.pack())
    return path


def read_native_loadout_ack(path: Path | None = None) -> NativeLoadoutAck:
    source = Path(path) if path is not None else native_loadout_ack_path()
    return NativeLoadoutAck.unpack(source.read_bytes())


def _clear_state(session: Any) -> None:
    try:
        delattr(session, SESSION_STATE_ATTR)
    except AttributeError:
        pass


def native_loadout_cooldown_remaining(session: Any) -> float:
    """Return the remaining post-completion dungeon-loadout cooldown.

    The deadline is intentionally session-local.  Only the dungeon request
    path reads it, so a completed dungeon loadout never blocks a town apply.
    """
    if session is None:
        return 0.0
    try:
        deadline = float(getattr(session, SESSION_COOLDOWN_UNTIL_ATTR, 0.0))
    except (TypeError, ValueError):
        deadline = 0.0
    remaining = deadline - time.monotonic()
    if remaining > 0.0:
        return remaining
    try:
        delattr(session, SESSION_COOLDOWN_UNTIL_ATTR)
    except AttributeError:
        pass
    return 0.0


def _start_native_loadout_cooldown(session: Any) -> None:
    setattr(
        session,
        SESSION_COOLDOWN_UNTIL_ATTR,
        time.monotonic() + DUNGEON_LOADOUT_COOLDOWN_SECONDS,
    )


def _notify_native_loadout_complete() -> None:
    """Publish the final native toast without changing move success."""
    try:
        send_client_toast("换装完成", mode=TOAST_MODE_TOAST)
    except (ClientToastBridgeError, OSError):
        pass


def active_native_loadout_state(
    session: Any,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> NativeLoadoutSessionState | None:
    state = getattr(session, SESSION_STATE_ATTR, None)
    if not isinstance(state, NativeLoadoutSessionState):
        return None
    elapsed = time.monotonic() - float(state.last_progress_monotonic)
    if elapsed > max(1.0, float(timeout_seconds)):
        _clear_state(session)
        _remove_file(native_loadout_plan_path())
        _remove_file(native_loadout_ack_path())
        return None
    return state


def queue_native_loadout_plan(
    session: Any,
    *,
    character_id: int,
    actions: tuple[NativeLoadoutAction, ...] | list[NativeLoadoutAction],
) -> NativeLoadoutPlan:
    normalized_actions = tuple(actions)
    plan_id = int(time.time_ns()) & 0xFFFFFFFFFFFFFFFF
    if plan_id <= 0:
        plan_id = 1
    plan = NativeLoadoutPlan(
        plan_id=plan_id,
        character_id=int(character_id),
        created_unix_ms=int(time.time() * 1000),
        actions=normalized_actions,
    )
    # Validate before replacing an existing file or attaching mutable state.
    plan.pack()
    _remove_file(native_loadout_ack_path())
    write_native_loadout_plan(plan)
    setattr(session, SESSION_STATE_ATTR, NativeLoadoutSessionState(plan=plan))
    return plan


def clear_native_loadout_state(session: Any) -> None:
    """Forget only the in-memory session state.

    Normal completion leaves the plan file for the DLL to consume its matching
    acknowledgement.  Call :func:`abort_native_loadout_plan` when a pending
    client action must be stopped as well.
    """
    _clear_state(session)


def abort_native_loadout_plan(session: Any, *, reason: str) -> str:
    """Stop the active plan before a stale native move can reach the database.

    The plan is removed first so a DLL that has not started it cannot pick up a
    stale action.  A matching failure acknowledgement is then published for a
    DLL already waiting on the current action.  Keep that ordering: publishing
    the failure while the plan is still present leaves a short window in which
    a newly attached client can start the old first action.
    """
    state = getattr(session, SESSION_STATE_ATTR, None)
    if not isinstance(state, NativeLoadoutSessionState):
        _clear_state(session)
        return ""

    action_count = len(state.plan.actions)
    action_index = min(
        max(0, int(state.next_action_index)),
        max(0, action_count - 1),
    )
    _remove_file(native_loadout_plan_path())
    _remove_file(native_loadout_ack_path())
    try:
        write_native_loadout_ack(
            NativeLoadoutAck(
                plan_id=state.plan.plan_id,
                action_index=action_index,
                action_count=action_count,
                status=ACK_FAILED,
            )
        )
    finally:
        _clear_state(session)
    return "native_loadout abort action=%s reason=%s" % (
        action_index,
        str(reason or "unspecified"),
    )


def validate_native_loadout_move(
    session: Any,
    move: Mapping[str, Any],
) -> tuple[bool, str]:
    """Accept only the currently released native ``C1/0013`` action.

    A plan is built from one database snapshot, but the DLL invokes the client
    at a later time.  The native method serializes whatever currently occupies
    its source cell.  Matching the actual C1 request *before* the repository
    mutation prevents a changed source cell or an unrelated manual move from
    committing under an active plan.
    """
    if session is None:
        return True, ""
    state = getattr(session, SESSION_STATE_ATTR, None)
    if not isinstance(state, NativeLoadoutSessionState):
        return True, ""
    elapsed = time.monotonic() - float(state.last_progress_monotonic)
    if elapsed > max(1.0, float(DEFAULT_TIMEOUT_SECONDS)):
        return False, abort_native_loadout_plan(
            session,
            reason="plan_timeout_before_move",
        )
    action = state.current_action
    if action is None:
        return False, abort_native_loadout_plan(
            session,
            reason="missing_current_action",
        )
    if action.matches_move(move):
        state.last_progress_monotonic = time.monotonic()
        return True, ""
    return False, abort_native_loadout_plan(
        session,
        reason="move_does_not_match_action_%s" % int(state.next_action_index),
    )


def note_native_loadout_move_result(
    session: Any,
    move: Mapping[str, Any],
    *,
    ok: bool,
) -> str:
    """Release one next native action after its matching server transaction.

    Stage3 emits a PIV for some successful inventory responses, but not for
    every consecutive native wear.  The repository result is the authoritative
    completion boundary: it proves that the exact guarded C1/0013 action was
    accepted and that its normal response packets are ready to leave Game.
    """
    state = active_native_loadout_state(session)
    if state is None:
        return ""
    action = state.current_action
    if action is None or not action.matches_move(move):
        return ""
    action_index = int(state.next_action_index)
    if not ok:
        return abort_native_loadout_plan(
            session,
            reason="repository_move_rejected",
        )
    write_native_loadout_ack(
        NativeLoadoutAck(
            plan_id=state.plan.plan_id,
            action_index=action_index,
            action_count=len(state.plan.actions),
            status=ACK_OK,
        )
    )
    state.next_action_index = action_index + 1
    state.last_progress_monotonic = time.monotonic()
    if state.next_action_index >= len(state.plan.actions):
        _start_native_loadout_cooldown(session)
        _clear_state(session)
        _notify_native_loadout_complete()
        return "native_loadout action=%s server_confirmed; plan_complete" % (
            action_index,
        )
    return "native_loadout action=%s server_confirmed; next=%s" % (
        action_index,
        state.next_action_index,
    )


def note_native_loadout_piv(
    session: Any,
    echoed_flag: int,
    echoed_opcode: int,
) -> str:
    """Keep observing PIV without using it as a native-loadout completion gate."""
    del session, echoed_flag, echoed_opcode
    return ""


__all__ = [
    "ACK_FAILED",
    "ACK_OK",
    "NativeLoadoutAck",
    "NativeLoadoutAction",
    "NativeLoadoutBridgeError",
    "NativeLoadoutPlan",
    "NativeLoadoutSessionState",
    "DUNGEON_LOADOUT_COOLDOWN_SECONDS",
    "active_native_loadout_state",
    "abort_native_loadout_plan",
    "clear_native_loadout_state",
    "native_loadout_ack_path",
    "native_loadout_cooldown_remaining",
    "native_loadout_enabled",
    "native_loadout_plan_path",
    "note_native_loadout_move_result",
    "note_native_loadout_piv",
    "queue_native_loadout_plan",
    "read_native_loadout_ack",
    "read_native_loadout_plan",
    "validate_native_loadout_move",
    "write_native_loadout_ack",
    "write_native_loadout_plan",
]
