# WARNING 2026-07-12: Stage3 PacketEnum names S0/0x0029 as ENUM_NOTIPACKET_PVP_ROOM_INFO.
# Live symptom after sending the map-table body: PVP room create UI + C->S 0x0036/0x003B.
# Town enter should prefer 0x0017 USER_AREA (seria07). Keep that S0 probe off.
# The C1/0x0029 handler below is a distinct, verified revive request direction.
# Stage3: vfunc16 writes map+0x8C type; type 9/10 opens PVP UI; force VFUNC16=0 for town A/B; 0x002B updates existing map only
from __future__ import annotations
import os
from contextlib import nullcontext
import struct
from typing import Any

from game.chain_loader import load_chain

_payloads = load_chain("helper-dungeon_payloads.py")
_mirror_mission = load_chain("helper-another_arad_quest.py")
build_wallet_revive_coin_update_000e = _payloads.build_wallet_revive_coin_update_000e

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

def _env_hex_bytes(name: str):
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    skip = set(" :_-" + chr(9) + chr(10) + chr(13))
    normalized = "".join(ch for ch in normalized if ch not in skip)
    if len(normalized) % 2:
        raise ValueError("%s bad hex: %r" % (name, raw))
    return bytes.fromhex(normalized)

class EnterSeriaRoomMapTable0029Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0029_MAP_TABLE", False):
            return None, (
                "S->C flag0/opcode=0x0029 PVP_ROOM_INFO/map-table suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0029_MAP_TABLE=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0029_BODY")
        if body is None:
            map_id = _env_int(
                "USDOF_GAME_ENTER_SERIA_0029_MAP_ID",
                int(getattr(selected, "town_id", 1) or 1),
            ) & 0xFFFF
            local_key = _env_int(
                "USDOF_GAME_ENTER_SERIA_0029_LOCAL_OBJECT_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            type_or_flag = _env_int("USDOF_GAME_ENTER_SERIA_0029_TYPE_OR_FLAG", 1) & 0xFF
            field_b = _env_int("USDOF_GAME_ENTER_SERIA_0029_FIELD_B", 0) & 0xFF
            vfunc88_arg = _env_int("USDOF_GAME_ENTER_SERIA_0029_VFUNC88", 0) & 0xFF
            field_d = _env_int("USDOF_GAME_ENTER_SERIA_0029_FIELD_D", 0) & 0xFFFF
            vfunc16_arg = _env_int("USDOF_GAME_ENTER_SERIA_0029_VFUNC16", 0) & 0xFF
            tail_byte = _env_int("USDOF_GAME_ENTER_SERIA_0029_TAIL_BYTE", 0) & 0xFF
            tail_u32 = _env_int("USDOF_GAME_ENTER_SERIA_0029_TAIL_U32", 0) & 0xFFFFFFFF
            slot_match_index = _env_int("USDOF_GAME_ENTER_SERIA_0029_SLOT_MATCH_INDEX", 0)
            if slot_match_index < 0 or slot_match_index > 7:
                slot_match_index = 0
            body = (1).to_bytes(2, "little")
            body += map_id.to_bytes(2, "little")
            body += bytes([type_or_flag, field_b, vfunc88_arg])
            body += field_d.to_bytes(2, "little")
            body += bytes([vfunc16_arg])
            match_all_slots = _env_bool("USDOF_GAME_ENTER_SERIA_0029_MATCH_ALL_SLOTS", False)
            for index in range(8):
                slot_flag = 0
                if match_all_slots:
                    slot_key = local_key
                else:
                    slot_key = local_key if index == slot_match_index else 0xFFFF
                body += bytes([slot_flag & 0xFF])
                body += (slot_key & 0xFFFF).to_bytes(2, "little")
            body += bytes([tail_byte])
            body += tail_u32.to_bytes(4, "little")
            body_note = (
                "map_count=1 map_id=%s type_or_flag=%s local_key=%s slot_match_index=%s match_all_slots=%s "
                "field_b=%s vfunc88=%s field_d=%s vfunc16=%s tail_byte=%s tail_u32=%s"
            ) % (map_id, type_or_flag, local_key, slot_match_index, int(match_all_slots), field_b, vfunc88_arg, field_d, vfunc16_arg, tail_byte, tail_u32)
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0029_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0029_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0029, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0029, body)
        note = (
            "queued ENTER_SERIA map/PVP_ROOM_INFO-0029 candidate "
            "S->C flag%s/opcode=0x0029 mode=%s %s plain_len=%s wire=%s; "
            "chain_file=game/chains/opcode0029-map_table.py; "
            "reverse: registry maps 41/0x0029 -> 0x0165E310; "
            "matching slot_key installs dword_4296BB8 via sub_1F170B0/sub_1F16EC0; "
            "area vtable+0xFC then fills object+0x37C for 0x0324"
        ) % (flag, mode, body_note, len(body), len(packet))
        return packet, note


class UseReviveCoin0029Service:
    """C1/0029 revive request for the death state armed by C1/0028.

    The Stage3 click path calls ``sub_2BF0580`` which writes exactly a u16
    actor id.  The flag-1 response dispatcher consumes the leading result
    byte, then ``sub_15F7CD0`` reads a u16 value and a u8 revive
    mode. The u16 value was previously misidentified as a u32 actor key.
    Keep the existing value while correcting its native width.
    That response completes the native request before the alive notification and wallet
    refresh and its empty S0/0012 inventory-action completion.
    """

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_use_revive_coin_0029(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        def reject(note):
            # A negative native response releases the pending C0029 action.
            # Silence here leaves the client's revive/give-up UI waiting.
            return [self.server._codec_response(1, 0x0029, b"\0\x13\0")], note

        raw = bytes(body or b"")
        if session is None or not bool(getattr(session, "in_dungeon_run", False)):
            return reject(
                "C1/0029 USE_REVIVE_COIN rejected=in_dungeon_run_false "
                "plain_len=%d raw=%s" % (len(raw), raw.hex(" "))
            )
        if len(raw) < 2:
            return reject(
                "C1/0029 USE_REVIVE_COIN rejected=body_too_short "
                "plain_len=%d raw=%s" % (len(raw), raw.hex(" "))
            )
        if not bool(getattr(session, "dungeon_death_pending", False)):
            return reject(
                "C1/0029 USE_REVIVE_COIN rejected=death_not_pending "
                "plain_len=%d raw=%s" % (len(raw), raw.hex(" "))
            )

        actor_id = struct.unpack_from("<H", raw)[0]
        pending_actor_id = int(getattr(session, "dungeon_death_actor_id", 0) or 0)
        if actor_id != pending_actor_id:
            return reject(
                "C1/0029 USE_REVIVE_COIN rejected=actor_mismatch "
                "actor=%d pending_actor=%d plain_len=%d raw=%s"
                % (actor_id, pending_actor_id, len(raw), raw.hex(" "))
            )

        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        if character_id <= 0:
            return reject(
                "C1/0029 USE_REVIVE_COIN rejected=selected_character_missing "
                "actor=%d" % actor_id
            )
        if pending_actor_id != (character_id & 0xFFFF):
            return reject("C1/0029 USE_REVIVE_COIN rejected=expired_selected_actor actor=%d" % actor_id)

        repo = self.server._repo()
        ordinary = getattr(session, 'selected_channel_id', None) not in (23, 32)
        transaction = repo.inventory_transaction_scope() if ordinary else nullcontext()
        with transaction:
            def spend():
                return repo.spend_account_revive_coin(character_id, 1, reason="dungeon_revive")
            raid_hooks = getattr(self.server, 'raid_transport_hooks', None)
            ok, wallet_note, remaining = (raid_hooks.spend_revive_coin(session, spend)
                                          if raid_hooks is not None else spend())
            if not ok:
                return reject(
                    "C1/0029 USE_REVIVE_COIN rejected=%s actor=%d "
                    "remaining=%d plain_len=%d raw=%s"
                    % (wallet_note, actor_id, int(remaining), len(raw), raw.hex(" "))
                )

            revive_state = struct.pack("<HBB", actor_id, 1, 0)
            revive_packet = self.server._codec_response(0, 0x0020, revive_state)
            revive_ack_body = struct.pack("<BHB", 1, actor_id, 0)
            revive_ack_packet = self.server._codec_response(
                1, 0x0029, revive_ack_body
            )
            wallet_body = build_wallet_revive_coin_update_000e(int(remaining))
            wallet_packet = self.server._codec_response(0, 0x000E, wallet_body)
            # The wallet is a virtual Main slot.  Like the verified dungeon cube
            # update, its 000E refresh must be followed by the zero-entry action
            # completion or the next shortcut consumable remains locally gated.
            completion_body = struct.pack("<H", 0)
            completion_packet = self.server._codec_response(0, 0x0012, completion_body)
            mirror_note = _mirror_mission.note_another_arad_revive_used(session)
        session.dungeon_death_pending = False
        session.dungeon_death_actor_id = 0
        return [
            revive_ack_packet,
            revive_packet,
            wallet_packet,
            completion_packet,
        ], (
            "C1/0029 USE_REVIVE_COIN actor=%d wallet=%s remaining=%d "
            "reply=[S1/0029 result=1 value_u16=%d mode=0 len=%d,"
            "S0/0020 state=1 len=%d,"
            "S0/000E slot=1 item=1 len=%d,S0/0012 entry_count=0 len=%d] "
            "ida=c1_writer_sub_2BF0580_u16_actor;"
            "s1_reader_sub_15F7CD0_u16_value_u8_mode;%s"
            % (
                actor_id,
                wallet_note,
                int(remaining),
                actor_id,
                len(revive_ack_body),
                len(revive_state),
                len(wallet_body),
                len(completion_body),
                mirror_note,
            )
        )

