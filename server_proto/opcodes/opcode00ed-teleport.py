#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C1/00ED town teleport-item flow for frozen Stage3.

Stage3 live vectors captured on 2026-08-02 use the following request body::

  i16 source_slot
  u32 item_template_id
  u8  list_type
  u16 target_town_id
  i16 requested_x
  i16 requested_y

The selected Silver Crown vector is ``07 00 4e ac 27 00 00 01 00 ff 7f ff 7f``.
The client-side S1/00ED reader at ``0x0160AD30`` consumes exactly
``u8 success + u16 source_slot + u32 item_template_id``.  The Stage3 S0/0018
reader at ``0x016800B0`` owns the actual town/area transition, so it must be
sent before the item refresh and command acknowledgement.
"""
from __future__ import annotations

from contextlib import nullcontext
import os
import struct
from typing import Any

from game.chain_loader import load_chain
from game.character.payloads import build_area_users
from game.town_cera_rooms import resolve_login_spawn


TELEPORT_ITEM_TEMPLATE_ID = 0x0027AC4E
_TELEPORT_REQUEST_SIZE = 13

_payloads = load_chain("helper-dungeon_payloads.py")
build_item_list_update_000e = _payloads.build_item_list_update_000e
build_item_list_clear_slot_000e = _payloads.build_item_list_clear_slot_000e


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def parse_teleport_body(plain: bytes) -> dict[str, int] | None:
    """Parse the verified 13-byte Stage3 C1/00ED request body."""
    data = bytes(plain or b"")
    if len(data) < _TELEPORT_REQUEST_SIZE:
        return None
    source_slot = int(struct.unpack_from("<h", data, 0)[0])
    if source_slot < 0:
        return None
    return {
        "source_slot": source_slot,
        "item_template_id": int(struct.unpack_from("<I", data, 2)[0]),
        "list_type": int(data[6]) & 0xFF,
        "target_town_id": int(struct.unpack_from("<H", data, 7)[0]),
        "requested_x": int(struct.unpack_from("<h", data, 9)[0]),
        "requested_y": int(struct.unpack_from("<h", data, 11)[0]),
        "raw_len": len(data),
        "raw_hex": data[:_TELEPORT_REQUEST_SIZE].hex(" "),
    }


def build_teleport_ack_ok(*, source_slot: int, item_template_id: int) -> bytes:
    """Build the Stage3 S1/00ED success body consumed by ``sub_160AD30``."""
    return struct.pack(
        "<BHI",
        0x01,
        int(source_slot) & 0xFFFF,
        int(item_template_id) & 0xFFFFFFFF,
    )


def _character_id(session: Any, server: Any) -> int | None:
    if session is not None:
        for attr in ("selected_character_id", "character_id", "cid"):
            value = getattr(session, attr, None)
            if value is None:
                continue
            try:
                character_id = int(value)
            except (TypeError, ValueError):
                continue
            if character_id > 0:
                return character_id
    try:
        repo = server._repo()
        account_id = int(getattr(server, "account_id", 1) or 1)
        rows = repo.list_by_account(account_id)
        if rows:
            return int(rows[0].character_id)
    except Exception:
        pass
    return None


class Teleport00EDService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _stage(self, name: str) -> Any:
        metrics = getattr(self.server, "hotpath_metrics", None)
        return metrics.stage(name) if metrics is not None else nullcontext()

    def _emit(self, flag: int, opcode: int, body: bytes) -> bytes:
        with self._stage("codec"):
            return self.server._codec_response(flag, opcode, body)

    def handle_teleport_00ed(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_TELEPORT_00ED", True):
            return [], "C1/00ED TELEPORT gate=OFF"

        parsed = parse_teleport_body(body)
        if parsed is None:
            return [], "C1/00ED TELEPORT reject=parse_fail len=%d" % len(body or b"")

        source_slot = int(parsed["source_slot"])
        item_id = int(parsed["item_template_id"])
        list_type = int(parsed["list_type"])
        target_town_id = int(parsed["target_town_id"])
        base = (
            "C1/00ED TELEPORT slot=%d item=%d list=%d target_town=%d "
            "requested_pos=(%d,%d) raw=%s"
            % (
                source_slot,
                item_id,
                list_type,
                target_town_id,
                int(parsed["requested_x"]),
                int(parsed["requested_y"]),
                parsed["raw_hex"],
            )
        )

        if bool(session is not None and getattr(session, "in_dungeon_run", False)):
            return [], base + "; reject=in_dungeon_run"
        if list_type != 0:
            return [], base + "; reject=list_type"
        if item_id != TELEPORT_ITEM_TEMPLATE_ID:
            return [], base + "; reject=item_template"
        if target_town_id <= 0 or target_town_id > 0xFF:
            return [], base + "; reject=target_town"

        character_id = _character_id(session, self.server)
        if character_id is None:
            return [], base + "; reject=no_selected_character"

        from game.raid.landing import is_raid_waiting_position
        if is_raid_waiting_position(session, character_id):
            return [], base + "; reject=anton_waiting_position"

        # The C1 coordinates are 0x7FFF in the live town-select flow.  Never
        # trust them: every destination is the target town's real Seria room.
        spawn = resolve_login_spawn(town_id=target_town_id)
        position = {
            "town_id": int(spawn.town_id),
            "area_id": int(spawn.area_id),
            "pos_x": int(spawn.pos_x),
            "pos_y": int(spawn.pos_y),
            "direction": int(spawn.direction),
            "area_state": int(spawn.area_state),
        }
        repo = self.server._repo()
        with self._stage("db_consume"):
            ok, consume_note, detail = repo.consume_stackable_at(
                int(character_id),
                list_type=0,
                slot_index=source_slot,
                expected_item_id=item_id,
                count=1,
                position=position,
            )
        if not ok or int(detail.get("removed") or 0) != 1:
            return [], "%s; reject=%s" % (base, consume_note)

        remaining = max(0, int(detail.get("remaining") or 0))
        actual_item_id = int(detail.get("actual_item_id") or item_id)
        if session is not None:
            session.cur_town_id = int(spawn.town_id)
            session.cur_area_id = int(spawn.area_id)
            session.cur_pos_x = int(spawn.pos_x)
            session.cur_pos_y = int(spawn.pos_y)
            session.cur_direction = int(spawn.direction)
            session.cur_area_state = int(spawn.area_state)

        packets: list[bytes] = []
        area_body = build_area_users(
            character_id=int(character_id) & 0xFFFF,
            town_id=int(spawn.town_id),
            area_id=int(spawn.area_id),
            pos_x=int(spawn.pos_x),
            pos_y=int(spawn.pos_y),
            direction=int(spawn.direction),
            state=int(spawn.area_state),
        )
        packets.append(self._emit(0, 0x0018, area_body))

        if remaining > 0:
            item_body = build_item_list_update_000e(
                [(source_slot, actual_item_id, remaining)],
                list_type=0,
            )
            item_note = "remain=%d item=%d" % (remaining, actual_item_id)
        else:
            item_body = build_item_list_clear_slot_000e(source_slot, list_type=0)
            item_note = "remain=0 clear_empty item_id=-1"
        packets.append(self._emit(0, 0x000E, item_body))

        ack_body = build_teleport_ack_ok(
            source_slot=source_slot,
            item_template_id=actual_item_id,
        )
        packets.append(self._emit(1, 0x00ED, ack_body))
        return packets, (
            "%s; OK consumed=1 %s; spawn town=%d area=%d pos=(%d,%d) dir=%d "
            "state=%d source=%s; reply=[S0/0018,S0/000E,S1/00ED]; "
            "chain_file=opcodes/opcode00ed-teleport.py"
            % (
                base,
                item_note,
                int(spawn.town_id),
                int(spawn.area_id),
                int(spawn.pos_x),
                int(spawn.pos_y),
                int(spawn.direction),
                int(spawn.area_state),
                spawn.source,
            )
        )
