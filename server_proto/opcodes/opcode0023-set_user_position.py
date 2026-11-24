#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x0023 SET_USER_POSITION: town walk position update.

Live Stage3 body (codec7, 8B padded):
  i16 pos_x
  i16 pos_y
  u8  direction
  u16 param/speed (often 0x0068) + pad
  observed: da 01 ea 00 05 68 00 00 -> (474,234) dir=5

S4A12 TownHandler.Handle_ENUM_CMDPACKET_SET_USER_POSITION:
  store x/y; optional broadcast S->C 0x0016 to other players in area.
  Single-player local: persist only, no self-echo required.
"""
from __future__ import annotations

import os
import struct
from typing import Any

from game.character.payloads import build_user_position_seria


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


def parse_set_user_position_body(plain: bytes) -> tuple[dict[str, int] | None, str]:
    if not plain or len(plain) < 4:
        return None, f"too_short len={len(plain) if plain else 0}"
    pos_x = struct.unpack_from("<h", plain, 0)[0]
    pos_y = struct.unpack_from("<h", plain, 2)[0]
    direction = plain[4] & 0xFF if len(plain) >= 5 else 5
    param = struct.unpack_from("<H", plain, 5)[0] if len(plain) >= 7 else 0
    return {
        "pos_x": int(pos_x),
        "pos_y": int(pos_y),
        "direction": direction,
        "param": int(param) & 0xFFFF,
    }, f"pos=({pos_x},{pos_y}) dir={direction} param={param}"


class SetUserPosition0023Service:
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

    def handle_set_user_position_0023(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SET_USER_POSITION_0023", True):
            return [], "SET_USER_POSITION 0x0023 ignored (USDOF_GAME_SET_USER_POSITION_0023=0)"

        parsed, parse_note = parse_set_user_position_body(body or b"")
        if parsed is None:
            return [], f"SET_USER_POSITION 0x0023 parse_fail {parse_note}"

        character_id = self._resolve_character_id(session)
        if character_id <= 0:
            return [], (
                f"SET_USER_POSITION 0x0023 no_selected_character {parse_note}; "
                "chain_file=game/chains/opcode0023-set_user_position.py"
            )

        if session is not None:
            session.cur_pos_x = parsed["pos_x"]
            session.cur_pos_y = parsed["pos_y"]
            session.cur_direction = parsed["direction"]
            from game.normal_party import town_confirmed
            town_confirmed(self.server, session, 0x0023)
            session.town_motion_state = parsed["param"]

        from game.raid.landing import is_raid_waiting_position
        waiting_position = is_raid_waiting_position(session, character_id)
        persist_note = "persist=skip"
        if _env_bool("USDOF_GAME_SET_USER_POSITION_PERSIST", True) and not waiting_position:
            try:
                repo = self.server._repo()
                town_id = int(getattr(session, "cur_town_id", 0) or 0)
                area_id = int(getattr(session, "cur_area_id", 0) or 0)
                area_state = int(getattr(session, "cur_area_state", 3) or 3)
                if town_id <= 0:
                    rows = [
                        r
                        for r in repo.list_by_account(int(getattr(self.server, "account_id", 1) or 1))
                        if int(r.character_id) == character_id
                    ]
                    if rows:
                        town_id = int(rows[0].town_id)
                        area_id = int(rows[0].area_id)
                        area_state = int(rows[0].area_state)
                if town_id <= 0:
                    town_id = 1
                ok = repo.update_position(
                    character_id,
                    town_id=town_id,
                    area_id=area_id,
                    pos_x=parsed["pos_x"],
                    pos_y=parsed["pos_y"],
                    direction=parsed["direction"],
                    area_state=area_state,
                )
                status = "ok" if ok else "miss"
                persist_note = (
                    f"persist={status} cid={character_id} "
                    f"town={town_id} area={area_id}"
                )
            except Exception as exc:
                persist_note = f"persist_fail cid={character_id} err={exc!r}"

        packets: list[bytes] = []
        # Optional self-echo 0x0016 (default OFF; S4 only broadcasts to others).
        if _env_bool("USDOF_GAME_SET_USER_POSITION_SEND_0016", False):
            body_0016 = build_user_position_seria(
                character_id=character_id & 0xFFFF,
                pos_x=parsed["pos_x"] & 0xFFFF,
                pos_y=parsed["pos_y"] & 0xFFFF,
                direction=parsed["direction"],
                param=_env_int("USDOF_GAME_SET_USER_POSITION_0016_PARAM", parsed["param"] or 100),
            )
            mode = os.environ.get("USDOF_GAME_SET_USER_POSITION_0016_MODE", "codec").strip().lower()
            flag = _env_int("USDOF_GAME_SET_USER_POSITION_0016_FLAG", 0) & 0xFF
            if mode in {"raw", "plain"}:
                pkt = self.server._raw_response(flag, 0x0016, body_0016)
                mode = "raw"
            else:
                pkt = self.server._codec_response(flag, 0x0016, body_0016)
                mode = "codec"
            packets.append(pkt)
            echo_note = f"S{flag}/0016 mode={mode} plain={len(body_0016)} wire={len(pkt)}"
        else:
            echo_note = "no_self_echo_0016"

        # 进房后首次 C1/0023：城镇 UI 已就绪，对 trigger==0 任务 pulse S1/0021
        # 点亮右方任务栏 + 底部完成图标闪烁（F2 列表仅靠 023F 数据态不够）。
        # IDA：sub_160E080 → sub_137FC30(0) → sub_10434A0(qid,1) + sub_159F9F0
        hud_note = "hud_blink=skip"
        if (
            session is not None
            and _env_bool("USDOF_GAME_QUEST_0023_ARM_COMPLETEABLE_0021", True)
            and bool(getattr(session, "quest_hud_blink_pending", False))
            and not bool(getattr(session, "quest_hud_blink_armed", False))
        ):
            from game.quest.quest_session import arm_completeable_hud_blink

            arm_pkts, arm_note = arm_completeable_hud_blink(
                self.server,
                character_id=int(character_id),
                session=session,
                source="C1/0023-hud",
            )
            packets.extend(arm_pkts)
            session.quest_hud_blink_pending = False
            session.quest_hud_blink_armed = True
            hud_note = arm_note

        # Phase-T1：城镇就绪后合法进教程本（完整 000F 选本态 + 001C/001D）。
        # 禁止在 START delayed 里叠副本包；门闩=首包 0023 + session.tutorial_armed。
        tut_note = "tutorial_phase_t1=skip"
        if session is not None and bool(getattr(session, "tutorial_armed", False)):
            try:
                from game.chain_loader import load_chain

                _tut_flow = load_chain("flow-tutorial_first_enter.py")
                tut_pkts, tut_note = _tut_flow.try_phase_t1_on_town_ready(
                    self.server,
                    session=session,
                    character_id=int(character_id),
                    source="C1/0023",
                )
                if tut_pkts:
                    packets.extend(tut_pkts)
            except Exception as tut_exc:
                tut_note = "tutorial_phase_t1_fail:%r" % (tut_exc,)

        note = (
            "SET_USER_POSITION 0x0023 "
            f"cid={character_id} {parse_note} {persist_note}; {echo_note}; {hud_note}; "
            f"{tut_note}; "
            "chain_file=opcodes/opcode0023-set_user_position.py; "
            "s4a12_ref=TownHandler.SET_USER_POSITION"
        )
        return packets, note
