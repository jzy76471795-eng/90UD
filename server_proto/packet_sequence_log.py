from __future__ import annotations

import json
import os
import string
import threading
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

from config import LOG_DIR


_LOGGER: PacketSequenceLogger | None = None
_LOGGER_LOCK = threading.Lock()


GAME_FLAG0_OPCODE_NAMES = {
    0x0000: "S->C NOTI check/connection candidate",
    0x0001: "S->C NOTI channel/login bootstrap candidate",
    0x0002: "S->C NOTI USERINFO",
    0x0003: "S->C NOTI USER_STATE",
    0x006A: "S->C NOTI CHARACTER_CARD_FRAME",
    0x0080: "S->C USDOF gate candidate",
    0x008B: "S->C USDOF state8 gate candidate",
    0x00D1: "S->C USDOF ready setter candidate",
    0x01BA: "S->C LOAD_EXTEND_CHARACS/charselect gate candidate",
    0x01F9: "S->C LOAD_EXTEND_CHARACS extra candidate",
    0x01FA: "S->C OPEN_CHAR_SELECT_UI candidate",
    0x0043: "S->C guild member-list snapshot",
    0x0047: "S->C guild created notification",
    0x02B3: "S->C guild invite-auth state snapshot",
    0x02B0: "S->C guild-create authentication invitation",
    0x0286: "S->C USDOF gate candidate",
    0x0355: "S->C extend-ready candidate",
}

GAME_FLAG1_OPCODE_NAMES = {
    0x0000: "C->S/S->C CHECK_CONNECTION",
    0x0001: "C->S LOGIN / S->C state-init in current experiments",
    0x0004: "C->S/S->C SELECT_CHARACTER or charselect-open ACK",
    0x0005: "C->S/S->C CREATE_CHARACTER",
    0x0006: "C->S/S->C DELETE_CHARACTER",
    0x0007: "C->S/S->C RETURN_SELECT_CHARACTER",
    0x0008: "C->S GET_USERINFO",
    0x009C: "C->S/S->C CHECK_GUILD_NAME",
    0x00D9: "C->S ACK/overflow-info gate",
    0x013E: "C->S PIV echo of last accepted S->C notipacket",
    0x01BA: "C->S LOAD_EXTEND_CHARACS_FINISH",
    0x01F8: "C->S post-charselect USERINFO refresh candidate",
    0x0043: "C->S/S->C GUILD_MEMBER_LIST",
    0x008C: "C->S/S->C GUILD_ALL_MEMBER_LIST",
    0x015C: "C->S/S->C REQUEST_JOIN_GUILD",
    0x015E: "C->S/S->C APPROVE_JOIN_GUILD",
    0x0160: "C->S/S->C GUILD_JOIN_LIST",
    0x02E7: "C->S/S->C GUILD_INFO_OF_MY_CHARS",
    0x02E8: "C->S/S->C GUILD_SEARCH_FOR_JOIN",
    0x02A8: "C->S LOAD_EXTEND_CHARACS",
    0x02E2: "C->S/S->C CHECK_GUILD_CREATE_PROMOTE_MSG",
    0x02B3: "C->S guild invite-auth request",
    0x02E4: "C->S/S->C guild-create authentication target",
    0x02E5: "C->S guild-create authentication invitation request",
    0x02E6: "C->S guild-create authentication acceptance",
    0x02B5: "C->S/S->C CHECK_DOUBLE_CHARACTER_NAME",
    0x0355: "C->S USDOF extend-ready / create-refresh gate",
}

CHANNEL_OPCODE_NAMES = {
    0x01: "C->S request channel info",
    0x03: "S->C channel info response; body is encrypted packed payload, plaintext logged separately",
    0x09: "C->S request script or S->C script response depending on direction",
    0x0B: "C->S channel connect/bootstrap request",
    0x0C: "S->C server channel session key",
    0x11: "C->S new channel info request",
    0x12: "S->C new channel info response; body is encrypted packed payload, plaintext logged separately",
}


HEADER_FIELD_MEANINGS = {
    "game": {
        "flag": "Game packet class. 0 means notipacket/server event in most S->C paths; 1 means command/ACK family.",
        "opcode": "Game command/notipacket id. Use opcode_meaning for the current known label.",
        "total_len": "Total packet bytes on TCP, including the physical header.",
        "header_len": "Physical header view used for body extraction. C->S plain-wire is 13 bytes; S->C server packets are normally 16 bytes.",
        "wire_body_len": "Bytes after the physical header on the wire.",
        "plain_body_len": "Decoded/plain logical body length logged in body_plain_hex.",
        "tail_or_seq": "Game header +0x0B u16. For C->S this is the compact-header seq/tag field; for S->C it is the server header sub_or_seq field.",
        "raw_hex": "Exact TCP packet bytes read or written by this server.",
        "body_plain_hex": "Caller-supplied logical body. C2S plain-wire is direct; native-codec S2C plaintext is authoritative in the client codec trace.",
    },
    "channel": {
        "flag": "Channel packet flag/classification byte.",
        "opcode": "Channel opcode byte. Use opcode_meaning for the current known label.",
        "total_len": "Total packet bytes on TCP, including the 0x0B-byte channel header.",
        "header_len": "Channel header length, always 0x0B.",
        "wire_body_len": "Bytes after the channel header on the wire.",
        "plain_body_len": "Plaintext body length when available. For encrypted S->C script/channelinfo this logger records the pre-encryption plaintext.",
        "tail_or_seq": "Channel header +0x0A tail/version byte.",
        "raw_hex": "Exact TCP packet bytes read or written by this server.",
        "body_plain_hex": "Readable body bytes. C->S channel requests are already plain; encrypted S->C payloads are logged from the server's plaintext source.",
    },
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ascii_preview(data: bytes, *, limit: int = 512) -> str:
    printable = set(string.printable.encode("ascii")) - {0x0B, 0x0C}
    preview = data[:limit]
    text = "".join(chr(byte) if byte in printable and byte not in {0x0D, 0x0A, 0x09} else "." for byte in preview)
    if len(data) > limit:
        text += f"... <truncated {len(data) - limit} byte(s)>"
    return text


def _wrap_hex(data: bytes, *, width: int = 16) -> str:
    if not data:
        return "<empty>"
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        lines.append(f"{offset:04X}: {chunk.hex(' ')}")
    return "\n".join(lines)


def _hex_int(value: Any, width: int) -> str | None:
    if value is None:
        return None
    try:
        return f"0x{int(value) & ((1 << (width * 4)) - 1):0{width}X}"
    except Exception:
        return str(value)


def opcode_meaning(protocol: str, flag: int | None, opcode: int | None, direction: str = "") -> str:
    if opcode is None:
        return ""
    if protocol == "channel":
        return CHANNEL_OPCODE_NAMES.get(opcode & 0xFF, "unknown channel opcode")
    if protocol != "game":
        return ""
    if flag == 0:
        return GAME_FLAG0_OPCODE_NAMES.get(opcode & 0xFFFF, "unknown flag0 game opcode")
    if flag == 1:
        return GAME_FLAG1_OPCODE_NAMES.get(opcode & 0xFFFF, "unknown flag1 game opcode")
    return "unknown game flag/opcode"


def game_body_field_notes(flag: int | None, opcode: int | None, plain_body: bytes, direction: str = "") -> list[str]:
    notes: list[str] = []
    if flag is None or opcode is None:
        return notes
    flag &= 0xFF
    opcode &= 0xFFFF
    if flag == 1 and opcode == 0x013E:
        if len(plain_body) >= 3:
            echoed_flag = plain_body[0]
            echoed_opcode = int.from_bytes(plain_body[1:3], "little")
            notes.append(f"+0x00 u8 echoed_flag = 0x{echoed_flag:02X}; last accepted S->C notipacket flag echoed by client PIV")
            notes.append(f"+0x01 u16 echoed_opcode = 0x{echoed_opcode:04X}; last accepted S->C notipacket opcode echoed by client PIV")
        if len(plain_body) >= 7:
            tag = int.from_bytes(plain_body[3:7], "little")
            notes.append(f"+0x03 u32 tag = 0x{tag:08X}; opaque PIV/tag dword observed in current client")
        if len(plain_body) > 7:
            notes.append(f"+0x07.. trailing {len(plain_body) - 7} byte(s); padding or currently-unmapped PIV tail")
    elif flag == 1 and opcode == 0x0005 and direction.startswith("C->S"):
        if len(plain_body) >= 5:
            job = plain_body[0]
            name_len = int.from_bytes(plain_body[1:5], "little")
            notes.append(f"+0x00 u8 job = {job}; create-character requested base job id")
            notes.append(f"+0x01 u32 name_len = {name_len}; byte length of UTF-8 character name")
            if 5 + name_len <= len(plain_body):
                name_raw = plain_body[5 : 5 + name_len]
                name = name_raw.decode("utf-8", errors="replace")
                notes.append(f"+0x05 bytes name = {name!r}; raw={name_raw.hex(' ')}")
                notes.append(f"+0x{5 + name_len:02X}.. trailing {len(plain_body) - (5 + name_len)} byte(s); create appearance/body-after-name")
    elif flag == 1 and opcode == 0x0005 and direction.startswith("S->C"):
        if len(plain_body) >= 2:
            notes.append(f"+0x00 u16 result/status = 0x{int.from_bytes(plain_body[:2], 'little'):04X}; CREATE_CHARACTER ACK status")
        if len(plain_body) > 2:
            notes.append(f"+0x02.. trailing {len(plain_body) - 2} byte(s); create ACK extension/body")
    elif flag == 1 and opcode == 0x009C:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            name_len = int.from_bytes(plain_body[:4], "little")
            notes.append(f"+0x00 u32 name_len = {name_len}; byte length for CHECK_GUILD_NAME")
            if 4 + name_len <= len(plain_body):
                name_raw = plain_body[4 : 4 + name_len]
                notes.append(f"+0x04 bytes name = {name_raw.decode('utf-8', errors='replace')!r}; raw={name_raw.hex(' ')}")
        elif direction.startswith("S->C") and len(plain_body) >= 2:
            available = plain_body[0]
            status = plain_body[1]
            status_note = {
                0x00: "available",
                0x6A: "too long",
                0x6B: "already used",
                0x6C: "illegal text",
            }.get(status, "unmapped")
            notes.append(f"+0x00 u8 available = {available}; 1 means the guild name can be used")
            notes.append(f"+0x01 u8 status = 0x{status:02X}; {status_note}")
    elif flag == 1 and opcode == 0x02E2:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            text_len = int.from_bytes(plain_body[:4], "little")
            notes.append(
                f"+0x00 u32 text_len = {text_len}; byte length for CHECK_GUILD_CREATE_PROMOTE_MSG"
            )
            if 4 + text_len <= len(plain_body):
                text_raw = plain_body[4 : 4 + text_len]
                notes.append(
                    f"+0x04 bytes text = {text_raw.decode('utf-8', errors='replace')!r}; "
                    f"raw={text_raw.hex(' ')}"
                )
        elif direction.startswith("S->C") and len(plain_body) >= 2:
            passed = plain_body[0]
            status = plain_body[1]
            status_note = {
                0x00: "accepted",
                0x6C: "illegal text",
            }.get(status, "unmapped")
            notes.append(
                f"+0x00 u8 passed = {passed}; non-zero enters the Stage3 success branch"
            )
            notes.append(f"+0x01 u8 status = 0x{status:02X}; {status_note}")
    elif flag == 1 and opcode == 0x02B3 and direction.startswith("C->S"):
        if plain_body:
            notes.append(
                f"+0x00 u8 request_flag = {plain_body[0]}; "
                "Stage3 guild invite-auth state request"
            )
        if len(plain_body) > 1:
            notes.append(
                f"+0x01.. trailing {len(plain_body) - 1} byte(s); codec padding candidate"
            )
    elif flag == 1 and opcode == 0x015C:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            name_len = int.from_bytes(plain_body[:4], "little")
            notes.append(
                f"+0x00 u32 guild_name_len = {name_len}; UTF-8 guild name byte length"
            )
            name_end = 4 + name_len
            if name_end + 4 <= len(plain_body):
                name_raw = plain_body[4 : 4 + name_len]
                message_len = int.from_bytes(
                    plain_body[name_end : name_end + 4], "little"
                )
                message_start = name_end + 4
                message_end = message_start + message_len
                notes.append(
                    f"+0x04 bytes guild_name = {name_raw.decode('utf-8', errors='replace')!r}; "
                    f"raw={name_raw.hex(' ')}"
                )
                notes.append(
                    f"+0x{name_end:02X} u32 application_message_len = {message_len}; "
                    "UTF-8 application-message byte length"
                )
                if message_end <= len(plain_body):
                    message_raw = plain_body[message_start:message_end]
                    notes.append(
                        f"+0x{message_start:02X} bytes application_message = "
                        f"{message_raw.decode('utf-8', errors='replace')!r}; "
                        f"raw={message_raw.hex(' ')}"
                    )
        elif direction.startswith("S->C") and plain_body:
            notes.append(
                f"+0x00 u8 success = {plain_body[0]}; "
                "success carries guild id and two DSTR fields"
            )
    elif flag == 1 and opcode == 0x0160:
        if direction.startswith("C->S"):
            if any(plain_body):
                notes.append(
                    f"+0x00.. unexpected {len(plain_body)} non-zero byte(s); "
                    "Stage3 guild-join-list request is empty"
                )
            else:
                notes.append("empty body; requests the selected guild member's pending applications")
        elif direction.startswith("S->C") and plain_body:
            notes.append(
                f"+0x00 u8 success = {plain_body[0]}; "
                "success carries a u32 application count"
            )
            if plain_body[0] and len(plain_body) >= 5:
                count = int.from_bytes(plain_body[1:5], "little")
                notes.append(
                    f"+0x01 u32 application_count = {count}; "
                    "each entry starts with character id and applicant DSTR"
                )
    elif flag == 1 and opcode == 0x015E:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            applicant_id = int.from_bytes(plain_body[:4], "little")
            notes.append(
                f"+0x00 u32 applicant_character_id = {applicant_id}; "
                "Stage3 pending guild-join application approval target"
            )
            if len(plain_body) > 4:
                notes.append(
                    f"+0x04.. trailing {len(plain_body) - 4} byte(s); "
                    "codec padding candidate"
                )
        elif direction.startswith("S->C") and plain_body:
            notes.append(
                f"+0x00 u8 success = {plain_body[0]}; "
                "native reader consumes the applicant character id after this field"
            )
            if len(plain_body) >= 5:
                applicant_id = int.from_bytes(plain_body[1:5], "little")
                notes.append(f"+0x01 u32 applicant_character_id = {applicant_id}")
    elif flag == 1 and opcode in {0x0043, 0x008C}:
        request_name = (
            "guild member-list"
            if opcode == 0x0043
            else "guild all-member-list"
        )
        selected_character_name = (
            "guild member list"
            if opcode == 0x0043
            else "guild all-member-list"
        )
        if direction.startswith("C->S"):
            if any(plain_body):
                notes.append(
                    f"+0x00.. unexpected {len(plain_body)} non-zero byte(s); "
                    "Stage3 %s request is empty" % request_name
                )
            else:
                notes.append(
                    "empty body; requests the selected character's %s"
                    % selected_character_name
                )
        elif direction.startswith("S->C"):
            if not plain_body:
                notes.append(
                    "empty response; %s reader expects a success byte"
                    % request_name
                )
            else:
                notes.append(
                    f"+0x00 u8 success = {plain_body[0]}; 1 carries guild name and member records"
                )
    elif flag == 1 and opcode == 0x02E7:
        if direction.startswith("C->S"):
            if any(plain_body):
                notes.append(
                    f"+0x00.. unexpected {len(plain_body)} non-zero byte(s); "
                    "Stage3 guild-account association request is empty"
                )
            else:
                notes.append(
                    "empty body; requests all current-account character/guild associations"
                )
        elif direction.startswith("S->C") and plain_body:
            notes.append(
                f"+0x00 u8 success = {plain_body[0]}; 1 carries a u8 association count"
            )
            if plain_body[0] and len(plain_body) >= 2:
                notes.append(
                    f"+0x01 u8 association_count = {plain_body[1]}; "
                    "each entry is character DSTR, guild DSTR, leader flag"
                )
    elif flag == 1 and opcode == 0x02E8:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            name_length = int.from_bytes(plain_body[:4], "little")
            notes.append(
                f"+0x00 u32 guild_name_len = {name_length}; UTF-8 byte length"
            )
            if 4 + name_length <= len(plain_body):
                name_bytes = plain_body[4 : 4 + name_length]
                notes.append(
                    f"+0x04 bytes guild_name = {name_bytes.decode('utf-8', errors='replace')!r}; "
                    f"raw={name_bytes.hex(' ')}"
                )
        elif direction.startswith("S->C") and plain_body:
            notes.append(
                f"+0x00 u8 success = {plain_body[0]}; "
                "1 carries guild id, name, u16 summary, leader name, u32 summary and u8 table"
            )
    elif flag == 0 and opcode == 0x02B3 and direction.startswith("S->C"):
        if plain_body:
            mode = plain_body[0]
            notes.append(
                f"+0x00 u8 mode = {mode}; "
                "1 is the Stage3 state-snapshot list form"
            )
            if mode == 1 and len(plain_body) >= 5:
                count = int.from_bytes(plain_body[1:5], "little")
                notes.append(
                    f"+0x01 u32 record_count = {count}; "
                    "zero is the local default-pass snapshot"
                )
            elif mode == 0:
                notes.append(
                    "+0x01 records one string/u32 state update; not used by the local default pass"
                )
    elif flag == 1 and opcode == 0x02E4:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            name_len = int.from_bytes(plain_body[:4], "little")
            notes.append(
                f"+0x00 u32 name_len = {name_len}; byte length for guild-create authentication target"
            )
            if 4 + name_len <= len(plain_body):
                name_raw = plain_body[4 : 4 + name_len]
                notes.append(
                    f"+0x04 bytes target_name = {name_raw.decode('utf-8', errors='replace')!r}; "
                    f"raw={name_raw.hex(' ')}"
                )
        elif direction.startswith("S->C") and len(plain_body) >= 2:
            passed = plain_body[0]
            status = plain_body[1]
            notes.append(
                f"+0x00 u8 passed = {passed}; non-zero accepts the selected authentication target"
            )
            notes.append(
                f"+0x01 u8 status = 0x{status:02X}; 0 is the local successful target result"
            )
    elif flag == 1 and opcode == 0x02E5 and direction.startswith("C->S"):
        if plain_body:
            notes.append(
                f"+0x00 u8 request_flag = {plain_body[0]}; guild-create authentication invitation request"
            )
        if len(plain_body) > 1:
            notes.append(
                f"+0x01.. trailing {len(plain_body) - 1} byte(s); codec padding candidate"
            )
    elif flag == 1 and opcode == 0x02E6 and direction.startswith("C->S"):
        if plain_body:
            notes.append(
                f"+0x00.. unexpected {len(plain_body)} byte(s); Stage3 acceptance request is empty"
            )
        else:
            notes.append("empty body; guild-create authentication acceptance")
    elif flag == 0 and opcode == 0x02B0 and direction.startswith("S->C"):
        if plain_body:
            notes.append(
                f"+0x00.. unexpected {len(plain_body)} byte(s); Stage3 invitation notification reader consumes no body"
            )
        else:
            notes.append("empty body; opens the guild-create authentication invitation prompt")
    elif flag == 0 and opcode == 0x0047 and direction.startswith("S->C"):
        if len(plain_body) >= 4:
            guild_id = int.from_bytes(plain_body[:4], "little")
            notes.append(f"+0x00 u32 guild_id = {guild_id}; newly created guild identifier")
        if len(plain_body) > 4:
            notes.append(
                f"+0x04.. trailing {len(plain_body) - 4} byte(s); reader only consumes guild_id"
            )
    elif flag == 1 and opcode == 0x02B5:
        if direction.startswith("C->S") and len(plain_body) >= 4:
            name_len = int.from_bytes(plain_body[:4], "little")
            notes.append(f"+0x00 u32 name_len = {name_len}; byte length for CHECK_DOUBLE_CHARACTER_NAME")
            if 4 + name_len <= len(plain_body):
                name_raw = plain_body[4 : 4 + name_len]
                notes.append(f"+0x04 bytes name = {name_raw.decode('utf-8', errors='replace')!r}; raw={name_raw.hex(' ')}")
        elif direction.startswith("S->C") and len(plain_body) >= 2:
            notes.append(f"+0x00 u16 result/status = 0x{int.from_bytes(plain_body[:2], 'little'):04X}; name-check response")
    elif flag == 1 and opcode == 0x0006 and direction.startswith("C->S"):
        if len(plain_body) >= 6:
            slot = int.from_bytes(plain_body[:2], "little")
            name_len = int.from_bytes(plain_body[2:6], "little")
            notes.append(f"+0x00 u16 slot_index = {slot}; selected character slot/index")
            notes.append(f"+0x02 u32 name_len = {name_len}; byte length of UTF-8 character name")
            if 6 + name_len <= len(plain_body):
                name_raw = plain_body[6 : 6 + name_len]
                notes.append(f"+0x06 bytes name = {name_raw.decode('utf-8', errors='replace')!r}; raw={name_raw.hex(' ')}")
    elif flag == 1 and opcode == 0x0004:
        if direction.startswith("C->S"):
            notes.append("SELECT_CHARACTER request body shape is still under reverse analysis; raw body is logged verbatim.")
        elif len(plain_body) >= 4:
            notes.append(f"+0x00 u8 success = {plain_body[0]}; charselect-open/select ACK success candidate")
            notes.append(f"+0x01 u8 window_id = {plain_body[1]}; current default 12 opens role-select window")
            notes.append(f"+0x02 u16 window_param = {int.from_bytes(plain_body[2:4], 'little')}; window parameter candidate")
            if len(plain_body) > 4:
                notes.append(f"+0x04.. trailing {len(plain_body) - 4} byte(s); extra charselect-open fields")
    elif flag == 1 and opcode == 0x0008:
        notes.append("GET_USERINFO request; current server responds with DB-backed USERINFO roster when enabled.")
    elif flag == 0 and opcode == 0x0002:
        notes.extend(_userinfo_type2_notes(plain_body))
    elif flag == 0 and opcode in {0x0080, 0x008B, 0x00D1, 0x01BA, 0x01F9, 0x01FA, 0x01FB, 0x01FC, 0x0286, 0x0355}:
        notes.append("USDOF charselect/gate notipacket candidate; body is logged verbatim because exact field semantics are only partially reversed.")
    return notes


def _userinfo_type2_notes(body: bytes) -> list[str]:
    notes: list[str] = []
    if not body:
        return ["USERINFO body is empty."]
    notes.append("+0x00 u8 type; type=2 is roster/character-list USERINFO for role-select")
    if body[0] != 0x02:
        notes.append(f"body[0]=0x{body[0]:02X}; not parsed as type=2 roster by this logger")
        return notes
    if len(body) < 0x0E:
        notes.append("body shorter than the proven type=2 fixed header; raw body logged only")
        return notes
    context = body[1]
    count_a = int.from_bytes(body[2:4], "little")
    count_b = int.from_bytes(body[4:6], "little")
    slot_or_count = int.from_bytes(body[6:8], "little")
    account_or_state = int.from_bytes(body[8:12], "little")
    entry_count = int.from_bytes(body[12:14], "little")
    notes.append(f"+0x01 u8 context_byte = {context}")
    notes.append(f"+0x02 u16 count_a/slot-limit candidate = {count_a}")
    notes.append(f"+0x04 u16 count_b/slot-effect/current count candidate = {count_b}")
    notes.append(f"+0x06 u16 slot_or_count UI gate candidate = {slot_or_count}")
    notes.append(f"+0x08 u32 account_or_state = 0x{account_or_state:08X}")
    notes.append(f"+0x0C u16 character_entry_count = {entry_count}")
    offset = 0x0E
    for index in range(entry_count):
        start = offset
        if offset + 6 > len(body):
            notes.append(f"entry[{index}] starts at +0x{start:04X} but body ended before slot/name header")
            break
        slot = int.from_bytes(body[offset : offset + 2], "little")
        offset += 2
        name_len = int.from_bytes(body[offset : offset + 4], "little")
        offset += 4
        if offset + name_len > len(body):
            notes.append(f"entry[{index}] +0x{start:04X}: slot={slot}, name_len={name_len}, but name exceeds body")
            break
        name_raw = body[offset : offset + name_len]
        name = name_raw.decode("utf-8", errors="replace")
        offset += name_len
        if offset + 7 > len(body):
            notes.append(f"entry[{index}] +0x{start:04X}: slot={slot}, name={name!r}, but row prefix is truncated")
            break
        job = body[offset]
        base = body[offset + 1]
        packed_grow = body[offset + 2]
        grow = packed_grow & 0x0F
        subgrow = (packed_grow >> 4) & 0x07
        display_level_a = body[offset + 3]
        display_level_b = body[offset + 4]
        row_1c_state = body[offset + 5]
        equipment_count = body[offset + 6]
        notes.append(
            f"entry[{index}] +0x{start:04X}: u16 slot={slot}, DSTR name={name!r} "
            f"(len={name_len}, raw={name_raw.hex(' ')})"
        )
        notes.append(
            f"entry[{index}] row-prefix +0x{offset:04X}: job={job}, base={base}, "
            f"packed_grow=0x{packed_grow:02X} (grow={grow}, subgrow={subgrow}), "
            f"level_a={display_level_a}, level_b={display_level_b}, row+0x1C/state={row_1c_state}, "
            f"equipment_count={equipment_count}"
        )
        offset += 7
        for equipment_index in range(equipment_count):
            eq_start = offset
            if offset + 10 > len(body):
                notes.append(f"entry[{index}].equipment[{equipment_index}] starts +0x{eq_start:04X} but is truncated")
                offset = len(body)
                break
            equip_slot = body[offset]
            item_id = int.from_bytes(body[offset + 1 : offset + 5], "little")
            custom_len = int.from_bytes(body[offset + 5 : offset + 9], "little")
            offset += 9
            if offset + custom_len + 10 > len(body):
                notes.append(
                    f"entry[{index}].equipment[{equipment_index}] +0x{eq_start:04X}: "
                    f"slot={equip_slot}, item_id={item_id}, custom_len={custom_len}, truncated tail"
                )
                offset = len(body)
                break
            offset += custom_len
            equip_flag = body[offset]
            aux1 = int.from_bytes(body[offset + 1 : offset + 5], "little")
            aux2 = int.from_bytes(body[offset + 5 : offset + 9], "little")
            unknown = body[offset + 9]
            offset += 10
            notes.append(
                f"entry[{index}].equipment[{equipment_index}] +0x{eq_start:04X}: "
                f"slot={equip_slot}, item_id={item_id}, custom_len={custom_len}, "
                f"flag={equip_flag}, aux1={aux1}, aux2={aux2}, unknown={unknown}"
            )
        tail_start = offset
        tail_len = min(30, max(0, len(body) - offset))
        if tail_len:
            tail = body[offset : offset + tail_len]
            notes.append(
                f"entry[{index}] post-equipment tail +0x{tail_start:04X}: {tail_len} byte(s); "
                "known fields include row+720 bool source at tail[13], post_18D02D0_u32 at tail[15..18], "
                "render_flags at tail[19], and extra u8s tail[20..29]"
            )
            if tail_len > 13:
                notes.append(f"entry[{index}] tail[13] row+720 bool source = {tail[13]}")
            if tail_len > 18:
                notes.append(f"entry[{index}] tail[15..18] post_18D02D0_u32 = 0x{int.from_bytes(tail[15:19], 'little'):08X}")
            if tail_len > 19:
                notes.append(f"entry[{index}] tail[19] render_flags = 0x{tail[19]:02X}")
            offset += tail_len
    if offset < len(body):
        remaining = len(body) - offset
        if remaining >= 9:
            ui_flag = body[offset]
            ui_a = int.from_bytes(body[offset + 1 : offset + 5], "little")
            ui_b = int.from_bytes(body[offset + 5 : offset + 9], "little")
            notes.append(f"+0x{offset:04X} u8 ui_flag = {ui_flag}; normal roster default should be 0")
            notes.append(f"+0x{offset + 1:04X} u32 ui_value_a = 0x{ui_a:08X}")
            notes.append(f"+0x{offset + 5:04X} u32 ui_value_b = 0x{ui_b:08X}")
            if remaining > 9:
                notes.append(f"+0x{offset + 9:04X}.. trailing/padding {remaining - 9} byte(s)")
        else:
            notes.append(f"+0x{offset:04X}.. trailing/padding {remaining} byte(s)")
    return notes


def channel_body_field_notes(opcode: int | None, plain_body: bytes, direction: str = "", semantic: str = "") -> list[str]:
    notes: list[str] = []
    if opcode is None:
        return notes
    opcode &= 0xFF
    if opcode == 0x0C and direction.startswith("S->C"):
        if len(plain_body) >= 4:
            notes.append(f"+0x00 u32 status/reserved = 0x{int.from_bytes(plain_body[:4], 'little'):08X}")
        if len(plain_body) > 4:
            notes.append(f"+0x04 bytes channel_session_key = {plain_body[4:].hex(' ')}")
    elif opcode in {0x03, 0x12} and direction.startswith("S->C"):
        notes.append("body_plain_hex is the server-built channel-info plaintext before encrypted packing.")
        notes.append("The raw packet body is encrypted packed payload; use raw_hex for exact wire bytes.")
    elif opcode == 0x09 and direction.startswith("S->C"):
        notes.append("body_plain_hex is the server-built script plaintext before encrypted packing.")
        notes.append("The raw packet body is encrypted packed payload; use raw_hex for exact wire bytes.")
    elif direction.startswith("C->S"):
        notes.append("Channel client request body is logged as received; current known requests usually carry an empty or small opaque body.")
    if semantic:
        notes.append(f"semantic label from server path: {semantic}")
    return notes


def build_packet_extra(
    *,
    protocol: str,
    direction: str,
    flag: int | None,
    opcode: int | None,
    plain_body: bytes,
    semantic: str = "",
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(extra or {})
    result.setdefault("field_meanings", HEADER_FIELD_MEANINGS.get(protocol, {}))
    meaning = opcode_meaning(protocol, flag, opcode, direction)
    if meaning:
        result.setdefault("opcode_meaning", meaning)
    if protocol == "game":
        notes = game_body_field_notes(flag, opcode, plain_body, direction)
    elif protocol == "channel":
        notes = channel_body_field_notes(opcode, plain_body, direction, semantic)
    else:
        notes = []
    if note:
        notes.append(f"server note: {note}")
    if notes:
        result.setdefault("body_field_notes", notes)
    return result


class PacketSequenceLogger:
    """Thread-safe process-wide packet logger for manual protocol inspection."""

    def __init__(self, log_dir: str | os.PathLike[str] | None = None, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        if log_dir is None:
            log_dir = LOG_DIR / f"packet_sequence_{_timestamp()}_{os.getpid()}"
        self.log_dir = Path(log_dir)
        self.jsonl_path = self.log_dir / "packets.jsonl"
        self.md_path = self.log_dir / "packets.md"
        self._lock = threading.Lock()
        self._seq = 0
        if self.enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._write_header()

    def _write_header(self) -> None:
        started = datetime.now().isoformat(timespec="milliseconds")
        with self.md_path.open("a", encoding="utf-8") as handle:
            handle.write(f"# USDOF Packet Sequence Log\n\n")
            handle.write(f"- started: `{started}`\n")
            handle.write("- body_plain_hex is the decoded/plain logical body when available.\n")
            handle.write("- raw_hex is the exact TCP packet bytes written/read by the server.\n\n")

    def log_packet(
        self,
        *,
        protocol: str,
        direction: str,
        connection_id: str,
        raw: bytes,
        plain_body: bytes,
        flag: int | None = None,
        opcode: int | None = None,
        total_len: int | None = None,
        header_len: int | None = None,
        wire_body_len: int | None = None,
        tail_or_seq: int | None = None,
        note: str = "",
        semantic: str = "",
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        if not self.enabled:
            return None
        now = datetime.now().isoformat(timespec="milliseconds")
        with self._lock:
            self._seq += 1
            seq = self._seq
            record: dict[str, Any] = {
                "seq": seq,
                "seq_text": f"{seq:06d}",
                "time": now,
                "protocol": protocol,
                "direction": direction,
                "connection_id": connection_id,
                "flag": flag,
                "flag_hex": _hex_int(flag, 2),
                "opcode": opcode,
                "opcode_hex": _hex_int(opcode, 4 if protocol == "game" else 2),
                "total_len": total_len if total_len is not None else len(raw),
                "header_len": header_len,
                "wire_body_len": wire_body_len,
                "plain_body_len": len(plain_body),
                "tail_or_seq": tail_or_seq,
                "tail_or_seq_hex": _hex_int(tail_or_seq, 4),
                "semantic": semantic,
                "note": note,
                "body_plain_hex": plain_body.hex(" "),
                "body_plain_ascii_preview": _ascii_preview(plain_body),
                "raw_hex": raw.hex(" "),
            }
            if extra:
                record["extra"] = extra

            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            self._append_markdown(record, raw=raw, plain_body=plain_body)
            return seq

    def _append_markdown(self, record: dict[str, Any], *, raw: bytes, plain_body: bytes) -> None:
        title = (
            f"## seq={record['seq_text']} {record['time']} "
            f"{record['protocol']} {record['direction']}"
        )
        with self.md_path.open("a", encoding="utf-8") as handle:
            handle.write(title + "\n\n")
            handle.write(f"- connection: `{record['connection_id']}`\n")
            handle.write(
                "- header: "
                f"flag=`{record['flag_hex']}` opcode=`{record['opcode_hex']}` "
                f"total_len=`{record['total_len']}` header_len=`{record['header_len']}` "
                f"wire_body_len=`{record['wire_body_len']}` plain_body_len=`{record['plain_body_len']}`"
            )
            if record.get("tail_or_seq_hex") is not None:
                handle.write(f" tail_or_seq=`{record['tail_or_seq_hex']}`")
            handle.write("\n")
            if record.get("semantic"):
                handle.write(f"- semantic: `{record['semantic']}`\n")
            if record.get("note"):
                handle.write(f"- note: {record['note']}\n")
            if record.get("extra"):
                handle.write(f"- extra: `{json.dumps(record['extra'], ensure_ascii=True, sort_keys=True)}`\n")
                field_meanings = record["extra"].get("field_meanings") if isinstance(record["extra"], dict) else None
                if isinstance(field_meanings, dict) and field_meanings:
                    handle.write("\nfield_meanings:\n\n")
                    for name, meaning in field_meanings.items():
                        handle.write(f"- `{name}`: {meaning}\n")
                opcode_meaning = record["extra"].get("opcode_meaning") if isinstance(record["extra"], dict) else None
                if opcode_meaning:
                    handle.write(f"- `opcode_meaning`: {opcode_meaning}\n")
                body_notes = record["extra"].get("body_field_notes") if isinstance(record["extra"], dict) else None
                if isinstance(body_notes, list) and body_notes:
                    handle.write("\nbody_field_notes:\n\n")
                    for note in body_notes:
                        handle.write(f"- {note}\n")
            handle.write("\nbody_plain_hex:\n\n")
            handle.write("```text\n")
            handle.write(_wrap_hex(plain_body) + "\n")
            handle.write("```\n\n")
            handle.write("body_plain_ascii_preview:\n\n")
            handle.write("```text\n")
            handle.write(record["body_plain_ascii_preview"] + "\n")
            handle.write("```\n\n")
            handle.write("raw_hex:\n\n")
            handle.write("```text\n")
            handle.write(_wrap_hex(raw) + "\n")
            handle.write("```\n\n")


def get_packet_sequence_logger() -> PacketSequenceLogger | None:
    if not (
        _env_bool("USDOF_GAME_DIAGNOSTIC_TRACE", False)
        or _env_bool("USDOF_PACKET_SEQUENCE_LOG", False)
    ):
        return None
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is None:
            log_dir = os.environ.get("USDOF_PACKET_SEQUENCE_LOG_DIR")
            _LOGGER = PacketSequenceLogger(log_dir=log_dir)
        return _LOGGER
