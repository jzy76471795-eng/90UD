# -*- coding: utf-8 -*-
"""C1/S1 0x00CA avatar recast for the frozen Stage3 client."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Sequence

from game.character.avatar_recast_repository import (
    AvatarRecastReward,
    apply_avatar_recast,
)
from game.character.avatar_recast_emblem_index import plan_avatar_recast_rewards
from game.character.item_metadata import resolve_item_metadata
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID = 0x13
ERROR_INVENTORY_FULL = 0x04

@dataclass(frozen=True)
class DisjointAvatarRequest:
    avatar_slot: int
    avatar_item_id: int


def parse_disjoint_avatar_request(body: bytes) -> DisjointAvatarRequest | None:
    """Parse Stage3 C1/00CA: ``u16 avatar_slot, u32 avatar_item_id``."""
    raw = bytes(body or b"")
    if len(raw) != 6:
        return None
    avatar_slot, avatar_item_id = struct.unpack("<HI", raw)
    if avatar_item_id == 0:
        return None
    return DisjointAvatarRequest(
        avatar_slot=int(avatar_slot),
        avatar_item_id=int(avatar_item_id),
    )


def build_disjoint_avatar_success(
    *,
    avatar_slot: int,
    reward_rows: Sequence[dict[str, Any]],
) -> bytes:
    """Build Stage3 success rows with the quantity granted by this recast."""
    rows = tuple(reward_rows)
    if len(rows) > 0xFFFF:
        raise ValueError("avatar recast reward count exceeds u16")
    body = bytearray((1,))
    body.extend(struct.pack("<HH", int(avatar_slot) & 0xFFFF, len(rows)))
    for row in rows:
        granted_count = int(row["granted_count"])
        if granted_count <= 0 or granted_count > 0xFFFFFFFF:
            raise ValueError("avatar recast granted count is invalid")
        body.extend(
            struct.pack(
                "<HII",
                int(row["slot_index"]) & 0xFFFF,
                int(row["item_template_id"]) & 0xFFFFFFFF,
                granted_count,
            )
        )
    return bytes(body)


def build_disjoint_avatar_error(error_code: int) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def _selected_character_id(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            try:
                character_id = int(getattr(session, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if character_id > 0:
                return character_id
    try:
        character_id = int(
            resolve_selected_character_detail(server, session).get("character_id") or 0
        )
    except Exception:
        return None
    return character_id if character_id > 0 else None


class DisjointAvatar00CAService:
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng

    def _emit(self, body: bytes) -> bytes:
        return self.server._codec_response(1, 0x00CA, body)

    def _error(self, error_code: int, note: str) -> tuple[list[bytes], str]:
        body = build_disjoint_avatar_error(error_code)
        return [self._emit(body)], "%s; ACK_fail/codec error=%d" % (
            note,
            error_code,
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_disjoint_avatar_request(body)
        if request is None:
            return self._error(
                ERROR_INVALID,
                "C1/00CA DISJOINT_AVATAR parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = "C1/00CA DISJOINT_AVATAR slot=%d item=%d" % (
            request.avatar_slot,
            request.avatar_item_id,
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(ERROR_INVALID, base + "; reject=no_character")

        try:
            target_metadata = resolve_item_metadata(request.avatar_item_id)
        except Exception as exc:
            return self._error(
                ERROR_INVALID,
                "%s; reject=target_metadata_exception error=%s:%s"
                % (base, type(exc).__name__, exc),
            )
        if not target_metadata.is_avatar:
            return self._error(ERROR_INVALID, base + "; reject=target_pvf")

        try:
            plan = plan_avatar_recast_rewards(target_metadata, rng=self.rng)
            rewards = tuple(
                AvatarRecastReward(item_id=entry.item_id, metadata=entry.metadata)
                for entry in plan.entries
            )
        except Exception as exc:
            return self._error(
                ERROR_INVALID,
                "%s; reject=reward_plan_exception error=%s:%s"
                % (base, type(exc).__name__, exc),
            )

        try:
            ok, db_note, detail = apply_avatar_recast(
                self.server._repo(),
                character_id=character_id,
                target_slot=request.avatar_slot,
                expected_target_item_id=request.avatar_item_id,
                rewards=rewards,
            )
        except Exception as exc:
            return self._error(
                ERROR_INVALID,
                "%s; transaction_exception=%s:%s"
                % (base, type(exc).__name__, exc),
            )
        if not ok:
            error_code = (
                ERROR_INVENTORY_FULL
                if db_note == "inventory_full"
                else ERROR_INVALID
            )
            return self._error(
                error_code,
                base + "; transaction_reject=" + db_note,
            )

        reward_rows = list(detail.get("reward_rows") or [])
        success = build_disjoint_avatar_success(
            avatar_slot=request.avatar_slot,
            reward_rows=reward_rows,
        )
        return [self._emit(success)], (
            "%s; OK target_kind=%s rarities=%s rolled=%s rewards=%s "
            "ACK/codec len=%d; %s"
            % (
                base,
                plan.target_kind,
                list(plan.reward_rarities),
                [entry.item_id for entry in plan.entries],
                [
                    (
                        row["slot_index"],
                        row["item_template_id"],
                        row["granted_count"],
                        row["final_stack_count"],
                    )
                    for row in reward_rows
                ],
                len(success),
                db_note,
            )
        )


def handle_disjoint_avatar_00ca(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return DisjointAvatar00CAService(server).handle(body, session=session)


__all__ = [
    "DisjointAvatar00CAService",
    "DisjointAvatarRequest",
    "build_disjoint_avatar_error",
    "build_disjoint_avatar_success",
    "handle_disjoint_avatar_00ca",
    "parse_disjoint_avatar_request",
]
