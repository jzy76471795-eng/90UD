# -*- coding: utf-8 -*-
"""C1/S1 0x0063 normal and gold avatar compounding for frozen Stage3."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Sequence

from game.chain_loader import load_chain
from game.character.avatar_compound_index import plan_avatar_compound
from game.character.avatar_compound_repository import apply_avatar_compound
from game.character.item_metadata import resolve_item_metadata
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID = 0x13
_avatar_socket = load_chain("helper-avatar_socket.py")


@dataclass(frozen=True)
class CompoundAvatarRequest:
    compounder_slot: int
    first_avatar_slot: int
    first_avatar_item_id: int
    second_avatar_slot: int
    second_avatar_item_id: int
    result_option: int


def parse_compound_avatar_request(body: bytes) -> CompoundAvatarRequest | None:
    """Parse live Stage3 C1/0063's exact 15-byte request."""
    raw = bytes(body or b"")
    if len(raw) != 15:
        return None
    values = struct.unpack("<HHIHIB", raw)
    request = CompoundAvatarRequest(*map(int, values))
    if (
        request.first_avatar_slot == request.second_avatar_slot
        or request.first_avatar_item_id <= 0
        or request.second_avatar_item_id <= 0
    ):
        return None
    return request


def build_compound_avatar_error(error_code: int = ERROR_INVALID) -> bytes:
    return bytes((0, int(error_code) & 0xFF))


def build_compound_avatar_success(
    *,
    source_rows: Sequence[dict[str, Any]],
    result_row: dict[str, Any],
    result_metadata: Any,
) -> bytes:
    """Build the S1/0063 body consumed at Stage3 VA 0x016193D0."""
    sources = tuple(source_rows)
    if not sources or len(sources) > 0xFF:
        raise ValueError("compound-avatar source count is invalid")
    body = bytearray((1, len(sources)))
    for source in sources:
        list_type = int(source["list_type"])
        slot = int(source["slot_index"])
        count = int(source["remove_count"])
        if not 0 <= list_type <= 0xFF or not 0 <= slot <= 0xFFFF or count <= 0:
            raise ValueError("compound-avatar source row is invalid")
        body.extend(struct.pack("<BHI", list_type, slot, count & 0xFFFFFFFF))

    result_item_id = int(result_row["item_template_id"])
    blobs = _avatar_socket.resolve_avatar_blobs(
        result_item_id,
        result_row.get("extra_json"),
        metadata=result_metadata,
    )
    primary_blob, aux_blob = blobs if blobs is not None else (b"", b"")
    body.extend(
        struct.pack(
            "<HIIH",
            int(result_row["slot_index"]) & 0xFFFF,
            result_item_id & 0xFFFFFFFF,
            int(result_row.get("wire_value") or 0) & 0xFFFFFFFF,
            int(result_row.get("option_value") or 0) & 0xFFFF,
        )
    )
    body.extend(struct.pack("<I", len(primary_blob)))
    body.extend(primary_blob)
    body.extend(struct.pack("<I", len(aux_blob)))
    body.extend(aux_blob)
    return bytes(body)


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


class CompoundAvatar0063Service:
    def __init__(self, server: Any, *, rng: Any = None) -> None:
        self.server = server
        self.rng = rng

    def _emit(self, body: bytes) -> bytes:
        return self.server._codec_response(1, 0x0063, body)

    def _error(self, note: str) -> tuple[list[bytes], str]:
        return [self._emit(build_compound_avatar_error())], (
            note + "; ACK_fail/codec error=%d" % ERROR_INVALID
        )

    def handle(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_compound_avatar_request(body)
        if request is None:
            return self._error(
                "C1/0063 COMPOUND_AVATAR parse_fail plain_len=%d" % len(body or b"")
            )
        base = (
            "C1/0063 COMPOUND_AVATAR binder_slot=%d avatars=(%d,%d),(%d,%d) option=%d"
            % (
                request.compounder_slot,
                request.first_avatar_slot,
                request.first_avatar_item_id,
                request.second_avatar_slot,
                request.second_avatar_item_id,
                request.result_option,
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(base + "; reject=no_character")
        repo = self.server._repo()
        progression = repo.get_character_level_job(character_id)
        if progression is None:
            return self._error(base + "; reject=character_missing")
        _level, job = progression

        binder = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.compounder_slot,
        )
        first = repo.get_inventory_item_at(
            character_id,
            list_type=1,
            slot_index=request.first_avatar_slot,
        )
        second = repo.get_inventory_item_at(
            character_id,
            list_type=1,
            slot_index=request.second_avatar_slot,
        )
        if binder is None or first is None or second is None:
            return self._error(base + "; reject=source_slot_empty")
        if (
            int(first.get("item_template_id") or 0) != request.first_avatar_item_id
            or int(second.get("item_template_id") or 0) != request.second_avatar_item_id
        ):
            return self._error(base + "; reject=source_item_mismatch")
        compounder_item_id = int(binder.get("item_template_id") or 0)

        try:
            first_metadata = resolve_item_metadata(request.first_avatar_item_id)
            second_metadata = resolve_item_metadata(request.second_avatar_item_id)
            plan = plan_avatar_compound(
                job=int(job),
                first=first_metadata,
                second=second_metadata,
                material_item_id=compounder_item_id,
                rng=self.rng,
            )
        except Exception as exc:
            return self._error(
                "%s; reject=pvf_plan error=%s:%s"
                % (base, type(exc).__name__, exc)
            )

        try:
            ok, db_note, detail = apply_avatar_compound(
                repo,
                character_id=character_id,
                compounder_slot=request.compounder_slot,
                expected_compounder_item_id=compounder_item_id,
                first_avatar_slot=request.first_avatar_slot,
                expected_first_avatar_item_id=request.first_avatar_item_id,
                second_avatar_slot=request.second_avatar_slot,
                expected_second_avatar_item_id=request.second_avatar_item_id,
                result_metadata=plan.result.metadata,
                result_option=request.result_option,
            )
        except Exception as exc:
            return self._error(
                "%s; transaction_exception=%s:%s"
                % (base, type(exc).__name__, exc)
            )
        if not ok:
            return self._error(base + "; transaction_reject=" + db_note)

        result_row = detail.get("result_row")
        if not isinstance(result_row, dict):
            raise RuntimeError("avatar compound transaction omitted result row")
        success = build_compound_avatar_success(
            source_rows=detail.get("source_rows") or (),
            result_row=result_row,
            result_metadata=plan.result.metadata,
        )
        return [self._emit(success)], (
            "%s; OK job=%d part=%s grades=(%d,%d) binder=%s bonus=%d "
            "rate=%d roll=%d rare=%d "
            "result=(%d,%d) binder_remain=%d ACK/codec len=%d; %s"
            % (
                base,
                int(job),
                plan.part,
                plan.first_grade,
                plan.second_grade,
                plan.material_kind,
                plan.material_rate_bonus,
                plan.rare_rate,
                plan.roll,
                int(plan.hit_rare),
                int(result_row["slot_index"]),
                int(result_row["item_template_id"]),
                int(detail.get("material_remaining") or 0),
                len(success),
                db_note,
            )
        )


def handle_compound_avatar_0063(
    server: Any,
    body: bytes,
    *,
    session: Any = None,
) -> tuple[list[bytes], str]:
    return CompoundAvatar0063Service(server).handle(body, session=session)


__all__ = [
    "CompoundAvatar0063Service",
    "CompoundAvatarRequest",
    "build_compound_avatar_error",
    "build_compound_avatar_success",
    "handle_compound_avatar_0063",
    "parse_compound_avatar_request",
]
