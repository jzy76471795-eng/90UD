"""Stage3 account snapshot for the dedicated special-dungeon selector."""
from __future__ import annotations

import struct
from typing import Any, Dict, Mapping, Optional, Tuple


# sub_9D0EF0 maps the requiem IDs/aliases to slots 0..6 and the fusion IDs to
# slots 7..12. S0/0318 itself remains keyed by the concrete dungeon ID, so the
# account snapshot projects every ID the frozen client may look up.
REQUIEM_DUNGEON_IDS = tuple(range(7101, 7108))
REQUIEM_ALIAS_DUNGEON_IDS = tuple(range(200, 207))
FUSION_DUNGEON_IDS = (7100,) + tuple(range(62, 68))
SPECIAL_DUNGEON_IDS = frozenset(
    REQUIEM_DUNGEON_IDS
    + REQUIEM_ALIAS_DUNGEON_IDS
    + FUSION_DUNGEON_IDS
)
SELECTOR_WIRE_DUNGEON_IDS = (
    REQUIEM_DUNGEON_IDS
    + REQUIEM_ALIAS_DUNGEON_IDS
    + FUSION_DUNGEON_IDS
)
SELECTOR_CANONICAL_PROGRESS_KEYS = (7100,) + REQUIEM_DUNGEON_IDS

SELECTOR_RECORD_COUNT = 32
SELECTOR_INITIAL_HIGHEST_DIFFICULTY = 3
SELECTOR_HIGHEST_DIFFICULTY_MAX = 100
SELECTOR_STATE_0317_BLOB_SIZE = 0x104
SELECTOR_RECORDS_0318_BLOB_SIZE = 0x184


def canonical_progress_key_for_dungeon(dungeon_id: object) -> Optional[int]:
    """Map every Stage3 selector alias to its account-owned progress key."""
    try:
        candidate = int(dungeon_id)
    except (TypeError, ValueError):
        return None
    if 7101 <= candidate <= 7107:
        return candidate
    if 200 <= candidate <= 206:
        return 7101 + candidate - 200
    if candidate == 7100 or 62 <= candidate <= 67:
        return 7100
    return None


def is_special_dungeon(dungeon_id: object) -> bool:
    return canonical_progress_key_for_dungeon(dungeon_id) is not None


def parse_selector_player_uid(body: bytes) -> Optional[int]:
    """Parse the exact C1/0356 and C1/0357 player-object u16 UID."""
    raw = bytes(body or b"")
    if len(raw) != 2:
        return None
    player_uid = int(struct.unpack("<H", raw)[0])
    return player_uid if player_uid > 0 else None


def selected_character_id(session: Any) -> Optional[int]:
    if session is None:
        return None
    for attr in ("selected_character_id", "character_id", "cid"):
        try:
            value = int(getattr(session, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def normalize_highest_difficulty(value: object) -> int:
    """Clamp account progress to the Stage3/PVF endless-selector bounds."""
    try:
        highest = int(value)
    except (TypeError, ValueError):
        highest = SELECTOR_INITIAL_HIGHEST_DIFFICULTY
    return max(
        SELECTOR_INITIAL_HIGHEST_DIFFICULTY,
        min(SELECTOR_HIGHEST_DIFFICULTY_MAX, highest),
    )


def wire_highest_cleared_for_unlocked(highest_unlocked: object) -> int:
    """Project the account unlock ceiling to the Stage3 S0/0318 record value.

    The selector page computes its ceiling in ``sub_2A638E0`` by taking the
    party's highest-cleared record and then opening one additional option.
    Account persistence stores that resulting highest-unlocked option, so the
    wire record is exactly one lower.
    """
    return normalize_highest_difficulty(highest_unlocked) - 1


def special_entry_option_is_unlocked(
    entry_option: object,
    highest_difficulty: object,
) -> bool:
    try:
        selected = int(entry_option)
    except (TypeError, ValueError):
        return False
    return 1 <= selected <= normalize_highest_difficulty(highest_difficulty)


def next_highest_difficulty(
    highest_difficulty: object,
    entry_option: object,
) -> int:
    """Unlock exactly one next option after clearing the active highest one."""
    highest = normalize_highest_difficulty(highest_difficulty)
    if not special_entry_option_is_unlocked(entry_option, highest):
        return highest
    if int(entry_option) != highest:
        return highest
    return min(SELECTOR_HIGHEST_DIFFICULTY_MAX, highest + 1)


def _new_blob(player_uid: int, size: int) -> bytearray:
    uid = int(player_uid)
    if not 0 < uid <= 0xFFFF:
        raise ValueError("invalid special selector player UID: %r" % player_uid)
    blob = bytearray(size)
    struct.pack_into("<HH", blob, 0, uid, 0)
    return blob


def build_selector_state_0317_blob(player_uid: int) -> bytes:
    """Build the 0x104 player-owned state blob consumed by Stage3 S0/0317."""
    # The 32 eight-byte state rows are independent from endless difficulty.
    # No server-owned state has been observed for this feature, so keep the
    # reader's native zero-initialized rows and only bind the player UID.
    return bytes(_new_blob(player_uid, SELECTOR_STATE_0317_BLOB_SIZE))


def _canonical_progress_snapshot(
    progress_by_key: Mapping[int, int],
) -> Dict[int, int]:
    snapshot = {
        key: SELECTOR_INITIAL_HIGHEST_DIFFICULTY
        for key in SELECTOR_CANONICAL_PROGRESS_KEYS
    }
    for raw_key, raw_highest in dict(progress_by_key or {}).items():
        canonical_key = canonical_progress_key_for_dungeon(raw_key)
        if canonical_key is None:
            continue
        snapshot[canonical_key] = max(
            snapshot[canonical_key],
            normalize_highest_difficulty(raw_highest),
        )
    return snapshot


def build_selector_records_0318_blob(
    player_uid: int,
    progress_by_key: Mapping[int, int],
) -> bytes:
    """Build the 0x184 account snapshot consumed by Stage3 S0/0318.

    Each populated row is ``u32 dungeon_id, i16 highest_cleared, i16 aux,
    u32 aux``. The final selector-page calculation in Stage3 ``sub_2A638E0``
    opens ``highest_cleared + 1`` up to the PVF cap. Account persistence keeps
    the already-open ceiling instead, so it must be projected one level down
    at this boundary.
    """
    blob = _new_blob(player_uid, SELECTOR_RECORDS_0318_BLOB_SIZE)
    snapshot = _canonical_progress_snapshot(progress_by_key)
    for index, dungeon_id in enumerate(SELECTOR_WIRE_DUNGEON_IDS):
        if index >= SELECTOR_RECORD_COUNT:
            break
        canonical_key = canonical_progress_key_for_dungeon(dungeon_id)
        if canonical_key is None:
            continue
        highest_cleared = wire_highest_cleared_for_unlocked(
            snapshot[canonical_key]
        )
        struct.pack_into(
            "<IhhI",
            blob,
            4 + index * 12,
            int(dungeon_id) & 0xFFFFFFFF,
            highest_cleared,
            0,
            0,
        )
    return bytes(blob)


def build_selector_state_0317_body(player_uid: int) -> bytes:
    blob = build_selector_state_0317_blob(player_uid)
    return struct.pack("<I", len(blob)) + blob


def build_selector_records_0318_body(
    player_uid: int,
    progress_by_key: Mapping[int, int],
) -> bytes:
    blob = build_selector_records_0318_blob(player_uid, progress_by_key)
    return struct.pack("<I", len(blob)) + blob


def build_selector_sync_packets(
    server: Any,
    player_uid: int,
    progress_by_key: Mapping[int, int],
) -> Tuple[bytes, bytes]:
    """Build one native S0/0317 then S0/0318 account refresh pair."""
    state_body = build_selector_state_0317_body(player_uid)
    records_body = build_selector_records_0318_body(player_uid, progress_by_key)
    return (
        server._codec_response(0, 0x0317, state_body),
        server._codec_response(0, 0x0318, records_body),
    )


def account_progress_snapshot(repo: Any, account_id: int) -> Dict[int, int]:
    """Return all canonical selector progress for one account."""
    persisted = repo.list_account_special_dungeon_highest_unlocked(int(account_id))
    return _canonical_progress_snapshot(persisted)


def selector_state_context(
    repo: Any,
    session: Any,
    player_uid: object,
    *,
    character_id: object = None,
) -> Tuple[Optional[Tuple[int, int, int, Dict[int, int]]], str]:
    """Resolve one player UID to its selected character and account snapshot."""
    try:
        uid = int(player_uid)
    except (TypeError, ValueError):
        uid = 0
    if not 0 < uid <= 0xFFFF:
        return None, "special_selector invalid_player_uid=%r" % (player_uid,)

    if character_id is None:
        selected_id = selected_character_id(session)
    else:
        try:
            selected_id = int(character_id)
        except (TypeError, ValueError):
            selected_id = None
        if selected_id is not None and selected_id <= 0:
            selected_id = None
    if selected_id is None:
        return None, "special_selector no_selected_character player_uid=%d" % uid

    try:
        account_id = repo.get_account_id_for_character(int(selected_id))
        if account_id is None:
            raise ValueError("character account missing")
        progress = account_progress_snapshot(repo, int(account_id))
    except Exception as exc:
        return None, "special_selector state_err=%s:%s" % (type(exc).__name__, exc)
    return (
        uid,
        int(selected_id),
        int(account_id),
        progress,
    ), ""


def selector_request_context(
    repo: Any,
    session: Any,
    body: bytes,
) -> Tuple[Optional[Tuple[int, int, int, Dict[int, int]]], str]:
    """Resolve a C1/0356 or C1/0357 player-UID request."""
    player_uid = parse_selector_player_uid(body)
    if player_uid is None:
        return None, "special_selector malformed_player_uid body_len=%d" % len(
            body or b""
        )
    context, note = selector_state_context(
        repo,
        session,
        player_uid,
    )
    if context is None:
        return None, note
    if session is not None:
        session.special_dungeon_selector_player_uid = int(player_uid)
    return context, ""


__all__ = [
    "FUSION_DUNGEON_IDS",
    "REQUIEM_ALIAS_DUNGEON_IDS",
    "REQUIEM_DUNGEON_IDS",
    "SELECTOR_CANONICAL_PROGRESS_KEYS",
    "SELECTOR_HIGHEST_DIFFICULTY_MAX",
    "SELECTOR_INITIAL_HIGHEST_DIFFICULTY",
    "SELECTOR_RECORDS_0318_BLOB_SIZE",
    "SELECTOR_STATE_0317_BLOB_SIZE",
    "SELECTOR_WIRE_DUNGEON_IDS",
    "SPECIAL_DUNGEON_IDS",
    "account_progress_snapshot",
    "build_selector_records_0318_body",
    "build_selector_records_0318_blob",
    "build_selector_sync_packets",
    "build_selector_state_0317_body",
    "build_selector_state_0317_blob",
    "canonical_progress_key_for_dungeon",
    "is_special_dungeon",
    "next_highest_difficulty",
    "normalize_highest_difficulty",
    "parse_selector_player_uid",
    "selected_character_id",
    "selector_request_context",
    "selector_state_context",
    "special_entry_option_is_unlocked",
    "wire_highest_cleared_for_unlocked",
]
