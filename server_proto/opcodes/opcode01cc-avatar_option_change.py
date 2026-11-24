# -*- coding: utf-8 -*-
"""C1/01CC avatar selected-ability adjustment for frozen Stage3."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from game.character.item_metadata import job_label_for_id, resolve_item_metadata
from game.quest.quest_session import resolve_selected_character_detail


ERROR_INVALID_REQUEST = 0x11


@dataclass(frozen=True)
class AvatarOptionChangeRequest:
    material_slot: int
    material_item_id: int
    avatar_slot: int
    avatar_item_id: int
    new_option: int


def parse_avatar_option_change_request(
    body: bytes,
) -> AvatarOptionChangeRequest | None:
    """Parse the 13-byte Stage3 C1/01CC writer layout seen in live traffic."""
    raw = bytes(body or b"")
    if len(raw) != 13:
        return None
    material_slot, material_item_id, avatar_slot, avatar_item_id, new_option = (
        struct.unpack("<HIHIB", raw)
    )
    if material_item_id <= 0 or avatar_item_id <= 0:
        return None
    return AvatarOptionChangeRequest(
        material_slot=int(material_slot),
        material_item_id=int(material_item_id),
        avatar_slot=int(avatar_slot),
        avatar_item_id=int(avatar_item_id),
        new_option=int(new_option),
    )


def build_avatar_option_change_success(avatar_slot: int, new_option: int) -> bytes:
    """Build the dispatcher result byte followed by the Stage3 success fields."""
    return struct.pack(
        "<BHB",
        1,
        int(avatar_slot) & 0xFFFF,
        int(new_option) & 0xFF,
    )


def build_avatar_option_change_error(error_code: int = ERROR_INVALID_REQUEST) -> bytes:
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


def _normalize_scope(value: str) -> str:
    text = str(value or "").strip().lower().replace("`", "")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return " ".join(text.split())


class AvatarOptionChange01CCService:
    def __init__(
        self,
        server: Any,
    ) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes) -> tuple[bytes, str]:
        return self.server._codec_response(flag, opcode, body), "codec"

    def _error(self, note: str) -> tuple[list[bytes], str]:
        packet, mode = self._emit(
            1,
            0x01CC,
            build_avatar_option_change_error(),
        )
        return [packet], "%s; ACK_fail/%s error=%d" % (
            note,
            mode,
            ERROR_INVALID_REQUEST,
        )

    def handle_avatar_option_change_01cc(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        request = parse_avatar_option_change_request(body)
        if request is None:
            return self._error(
                "C1/01CC AVATAR_OPTION_CHANGE parse_fail plain_len=%d"
                % len(body or b""),
            )
        base = (
            "C1/01CC AVATAR_OPTION_CHANGE material=%d item=%d "
            "avatar=%d item=%d option=%d request_source=%s"
            % (
                request.material_slot,
                request.material_item_id,
                request.avatar_slot,
                request.avatar_item_id,
                request.new_option,
                "wire",
            )
        )
        character_id = _selected_character_id(session, self.server)
        if character_id is None:
            return self._error(base + "; reject=no_character")

        repo = self.server._repo()
        material = repo.get_inventory_item_at(
            character_id,
            list_type=0,
            slot_index=request.material_slot,
        )
        if material is None:
            return self._error(base + "; reject=material_empty")
        if (
            int(material.get("item_template_id") or 0) != request.material_item_id
            or str(material.get("item_kind") or "") in {"avatar", "equipment"}
        ):
            return self._error(base + "; reject=material_mismatch")

        avatar = repo.get_inventory_item_at(
            character_id,
            list_type=1,
            slot_index=request.avatar_slot,
        )
        if avatar is None:
            return self._error(base + "; reject=avatar_empty")
        if int(avatar.get("item_template_id") or 0) != request.avatar_item_id:
            return self._error(base + "; reject=avatar_mismatch")
        if request.avatar_item_id in (2026072401, 2026072901):
            return self._error(base + "; reject=clone_avatar_fixed_attributes")

        material_meta = resolve_item_metadata(request.material_item_id)
        avatar_meta = resolve_item_metadata(request.avatar_item_id)
        scope = tuple(material_meta.avatar_option_change_scope or ())
        target_grade = int(material_meta.avatar_option_change_grade or 0)
        if (
            not material_meta.is_stackable
            or len(scope) != 2
            or target_grade <= 0
        ):
            return self._error(base + "; reject=material_pvf_rule")
        if not avatar_meta.is_avatar or int(avatar_meta.grade) != target_grade:
            return self._error(
                base
                + "; reject=avatar_grade expected=%d actual=%d"
                % (target_grade, int(avatar_meta.grade))
            )

        job_scope = _normalize_scope(scope[0])
        if job_scope != "all":
            try:
                _exp, _gold, _wallet, character_job, _grow = (
                    repo.get_character_economy(character_id)
                )
            except Exception:
                return self._error(base + "; reject=character_job")
            if _normalize_scope(job_label_for_id(character_job)) != job_scope:
                return self._error(base + "; reject=job_scope")
        type_scope = _normalize_scope(scope[1])
        if type_scope not in {
            "all",
            _normalize_scope(avatar_meta.equipment_type),
        }:
            return self._error(base + "; reject=avatar_type_scope")

        old_option = max(0, int(avatar.get("option_value") or 0)) & 0xFF
        ok, db_note, detail = repo.apply_avatar_option_change(
            character_id=character_id,
            target_slot=request.avatar_slot,
            expected_target_item_id=request.avatar_item_id,
            expected_old_option=old_option,
            material_slot=request.material_slot,
            expected_material_item_id=request.material_item_id,
            new_option=request.new_option,
        )
        if not ok:
            return self._error(base + "; transaction_reject=" + db_note)

        packets: list[bytes] = []
        ack_body = build_avatar_option_change_success(
            request.avatar_slot,
            request.new_option,
        )
        ack_packet, ack_mode = self._emit(1, 0x01CC, ack_body)
        packets.append(ack_packet)

        # Stage3 S1/01CC updates AbilityNo on the existing avatar object and then
        # dispatches UI event 146 before closing UI 145. Live attempt 2/3 showed
        # that even an immediate list0 material refresh after the acknowledged
        # S1 packet crashes during this native callback. Keep the response
        # strictly ACK-only so no packet interleaves with that state transition
        # (attempt 3/3); the database transaction remains authoritative.
        return packets, (
            "%s; OK option=%d->%d grade=%d mode=%d material_remain=%d; "
            "ACK/%s len=%d post_ack_refresh=none target_update=S1_native; %s"
            % (
                base,
                int(detail.get("old_option") or 0),
                int(detail.get("new_option") or 0),
                target_grade,
                int(material_meta.avatar_option_change_mode),
                int(detail.get("material_remaining") or 0),
                ack_mode,
                len(ack_body),
                db_note,
            )
        )


__all__ = [
    "AvatarOptionChange01CCService",
    "AvatarOptionChangeRequest",
    "build_avatar_option_change_error",
    "build_avatar_option_change_success",
    "parse_avatar_option_change_request",
]
