#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x0024 SET_USER_AREA: leave/enter town area (Seria door, gates).

Live Stage3 (game_server_20260716_184042):
  wire codec8, plain (padded to 16):
    u8  town_id
    u8  area_id
    i16 pos_x
    i16 pos_y
    u8  direction
    u8  area_state_hint
  observed leave-Seria body: 01 01 3e 07 ff 00 05 01
    -> town=1 area=1 x=1854 y=255 dir=5 state_hint=1

S4A12 TownHandler.Handle_ENUM_CMDPACKET_SET_USER_AREA (reference only):
  parse first 6B, force direction=5 area_state=3
  S->C 0x0017 USER_AREA self
  S->C 0x0018 AREA_USERS roster (self only in single-player)

Stage3 S->C field layouts already proven in payloads.build_user_area / build_area_users
and enter-Seria START_MAP chain (codec9 for 0x0017, codec for 0x0018).
"""
from __future__ import annotations

import os
import sqlite3
import struct
from typing import Any

from game.character.payloads import build_area_users, build_user_area
from game.quest.adventure_season1 import record_guild_hall_entry


# Both PVFs map town 8 to guildagit.twn; the 90US file has areas 0–4.
GUILD_HALL_TOWN_ID = 8
GUILD_HALL_AREAS = range(5)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def parse_set_user_area_body(plain: bytes) -> tuple[dict[str, int] | None, str]:
    if not plain or len(plain) < 6:
        return None, f"too_short len={len(plain) if plain else 0}"
    town_id = plain[0] & 0xFF
    area_id = plain[1] & 0xFF
    pos_x = struct.unpack_from("<h", plain, 2)[0]
    pos_y = struct.unpack_from("<h", plain, 4)[0]
    direction = plain[6] & 0xFF if len(plain) >= 7 else 5
    state_hint = plain[7] & 0xFF if len(plain) >= 8 else 3
    # S4A12 overwrites direction/state after parse; Stage3 live often already
    # carries dir=5. Keep client dir when present, force active state=3 for
    # town bind (matches enter-Seria / S4A12 default).
    area_state = _env_int("USDOF_GAME_SET_USER_AREA_FORCE_STATE", 3) & 0xFF
    if _env_bool("USDOF_GAME_SET_USER_AREA_KEEP_CLIENT_STATE", False):
        area_state = state_hint
    if _env_bool("USDOF_GAME_SET_USER_AREA_FORCE_DIR5", True):
        direction = 5
    return {
        "town_id": town_id,
        "area_id": area_id,
        "pos_x": int(pos_x),
        "pos_y": int(pos_y),
        "direction": direction & 0xFF,
        "area_state": area_state,
        "state_hint": state_hint,
    }, (
        f"town={town_id} area={area_id} pos=({pos_x},{pos_y}) "
        f"dir={direction & 0xFF} state={area_state} hint={state_hint}"
    )


class SetUserArea0024Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _resolve_character_id(self, session: Any) -> int:
        cid = int(getattr(session, "selected_character_id", 0) or 0)
        if cid > 0:
            return cid
        try:
            repo = self.server._repo()
            account_id = int(getattr(self.server, "account_id", 1) or 1)
            rows = repo.list_by_account(account_id)
            if rows:
                return int(rows[0].character_id)
        except Exception:
            pass
        return 0

    def handle_set_user_area_0024(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SET_USER_AREA_0024", True):
            return [], "SET_USER_AREA 0x0024 ignored (USDOF_GAME_SET_USER_AREA_0024=0)"

        parsed, parse_note = parse_set_user_area_body(body or b"")
        if parsed is None:
            return [], f"SET_USER_AREA 0x0024 parse_fail {parse_note}"

        character_id = self._resolve_character_id(session)
        if character_id <= 0:
            return [], (
                f"SET_USER_AREA 0x0024 no_selected_character {parse_note}; "
                "chain_file=game/chains/opcode0024-set_user_area.py"
            )

        from game.raid.lobby_bindings import guard_area
        target_town, target_area = parsed['town_id'], parsed['area_id']
        parsed, raid_gate_note = guard_area(self.server, session, parsed)
        if raid_gate_note:
            parse_note = raid_gate_note
        if parsed is None:
            # Native 0161C7A0 consumes failure + error + town:u8 + area:u8.
            # Error 0x15 releases the pending transition via 02BA9BE0(0)
            # without changing the self actor or reloading the source map.
            # Acknowledge every retry: dropping one can leave that lock set.
            return [self.server._codec_response(1, 0x0024,
                bytes((0, 0x15, target_town, target_area)))], parse_note

        previous_town = getattr(session, "cur_town_id", None)
        if session is not None:
            # An accepted native area request rebuilds local actors even when
            # its town/area equals the previous scope. Movement (0023) does not.
            from game.town_social import scene_changed
            scene_changed(session)
            session.cur_town_id = parsed["town_id"]
            session.cur_area_id = parsed["area_id"]
            session.cur_pos_x = parsed["pos_x"]
            session.cur_pos_y = parsed["pos_y"]
            session.cur_direction = parsed["direction"]
            session.cur_area_state = parsed["area_state"]
            from game.normal_party import town_confirmed
            town_confirmed(self.server, session, 0x0024)

        from game.raid.landing import is_raid_waiting_position
        waiting_position = is_raid_waiting_position(session, character_id)
        persist_note = "persist=skip"
        position_saved = False
        if _env_bool("USDOF_GAME_SET_USER_AREA_PERSIST", True) and not waiting_position and not raid_gate_note:
            try:
                repo = self.server._repo()
                position_saved = repo.update_position(
                    character_id,
                    town_id=parsed["town_id"],
                    area_id=parsed["area_id"],
                    pos_x=parsed["pos_x"],
                    pos_y=parsed["pos_y"],
                    direction=parsed["direction"],
                    area_state=parsed["area_state"],
                )
                status = "ok" if position_saved else "miss"
                persist_note = f"persist={status} cid={character_id}"
            except Exception as exc:
                persist_note = f"persist_fail cid={character_id} err={exc!r}"

        key = character_id & 0xFFFF
        packets: list[bytes] = []
        notes: list[str] = []
        if (
            position_saved and previous_town is not None and previous_town != GUILD_HALL_TOWN_ID
            and parsed["town_id"] == GUILD_HALL_TOWN_ID and parsed["area_id"] in GUILD_HALL_AREAS
            and getattr(session, "enter_seria_settled", False) and not getattr(session, "in_dungeon_run", False)
        ):
            try:
                if record_guild_hall_entry(self.server, session):
                    persist_note += " adventure_season1_guild_hall=1"
            except (OSError, ValueError, sqlite3.Error) as exc:
                persist_note += f" adventure_season1_guild_hall_error={type(exc).__name__}:{exc}"

        if _env_bool("USDOF_GAME_SET_USER_AREA_SEND_0017", True):
            body_0017 = build_user_area(
                character_id=key,
                town_id=parsed["town_id"],
                area_id=parsed["area_id"],
                pos_x=parsed["pos_x"] & 0xFFFF,
                pos_y=parsed["pos_y"] & 0xFFFF,
                direction=parsed["direction"],
                state=parsed["area_state"],
            )
            mode = os.environ.get("USDOF_GAME_SET_USER_AREA_0017_MODE", "codec").strip().lower()
            flag = _env_int("USDOF_GAME_SET_USER_AREA_0017_FLAG", 0) & 0xFF
            if mode in {"raw", "plain"}:
                pkt = self.server._raw_response(flag, 0x0017, body_0017)
                mode = "raw"
            else:
                pkt = self.server._codec_response(flag, 0x0017, body_0017)
                mode = "codec"
            packets.append(pkt)
            notes.append(f"S{flag}/0017 mode={mode} plain={len(body_0017)} wire={len(pkt)}")

        if _env_bool("USDOF_GAME_SET_USER_AREA_SEND_0018", True):
            body_0018 = build_area_users(
                character_id=key,
                town_id=parsed["town_id"],
                area_id=parsed["area_id"],
                pos_x=parsed["pos_x"] & 0xFFFF,
                pos_y=parsed["pos_y"] & 0xFFFF,
                direction=parsed["direction"],
                state=parsed["area_state"],
            )
            mode = os.environ.get("USDOF_GAME_SET_USER_AREA_0018_MODE", "codec").strip().lower()
            flag = _env_int("USDOF_GAME_SET_USER_AREA_0018_FLAG", 0) & 0xFF
            if mode in {"raw", "plain"}:
                pkt = self.server._raw_response(flag, 0x0018, body_0018)
                mode = "raw"
            else:
                pkt = self.server._codec_response(flag, 0x0018, body_0018)
                mode = "codec"
            packets.append(pkt)
            notes.append(f"S{flag}/0018 mode={mode} plain={len(body_0018)} wire={len(pkt)}")

        reply = ", ".join(notes) if notes else "none"
        note = (
            "SET_USER_AREA 0x0024 "
            f"cid={character_id} {parse_note} {persist_note}; "
            f"reply=[{reply}]; "
            "chain_file=game/chains/opcode0024-set_user_area.py; "
            "s4a12_ref=TownHandler.SET_USER_AREA -> 0017+0018"
        )
        return packets, note
