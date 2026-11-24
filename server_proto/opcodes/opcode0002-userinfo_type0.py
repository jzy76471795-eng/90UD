# Enter-seria chain 13: S->C 0x0002 USERINFO type0 minimal bind-main (non-PVP n2500)
# Default OFF. Enable USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE0=1
# Stage3 gate: USERINFO type0 reads outer entry_flag into global i (0x41722C9).
# 18D0780 seeds n2500 via 18CEA00 only if dword_4187E2C channel family == i.
# Default entry_flag=0 matches channel family 0 at role-select enter; entry_flag=1 skips seed (Probe R/R2).
#
# Stage3 reverse (idalib authority IDB, 2026-07-13):
# - 0x018D4140 type==0 branch -> userinfo_type0_or_entry_18D0780
# - early reads: blob33 + u16 key + create via 18CD220 when channel matches
# - u8 job -> v71[1]; u8 packed -> v71[2]=low4, v71[3]=high3
# - sub_18CEA00(key, v71): if key==local_key and main_obj char@+1108==0
#     -> sub_18CC9C0(v71, key)
# - 18CC9C0: char = 25A5870(v71[1], v71[2]); set_main_object_write_n2500_1E7A850(...)
# - AFTER seed (still same entry stream, shared with type2 18D2000 helpers):
#     18CD370: u8 -> struct+20  (type2 roster mirrors grow_type)
#     18CE350: class UI seed using job/grow (string id 3630 path)
#     18CD420: u8 level-like -> struct+696  (named userinfo_read_u8_level_to_struct_plus696_18CD420)
#     18CD4D0: u8 -> struct+704
#     then u8 equip/list count for 18CC430 (0 skips list body)
# - Probe AB with all-zero post-job stream hit role-page "Character Level Error";
#   fill level (+ optional +20 grow mirror) from selected character.
from __future__ import annotations

import os
from typing import Any


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
    skip = set(" \t\r\n:_-")
    normalized = "".join(ch for ch in normalized if ch not in skip)
    if len(normalized) % 2:
        raise ValueError("%s bad hex: %r" % (name, raw))
    return bytes.fromhex(normalized)


def build_userinfo_type0_bind_main_plaintext(
    *,
    key: int = 0,
    entry_flag: int = 1,
    job: int = 0,
    grow: int = 0,
    level: int = 1,
    field_plus20: int | None = None,
    field_plus704: int = 0,
    equip_count: int = 0,
    head33: bytes | None = None,
    tail_pad: int = 256,
) -> bytes:
    # Outer: u8 type=0, u16 count=1, u8 entry_flag
    # Entry (18D0780):
    #   33B blob + u16 key + u8 job + u8 grow_packed
    #   + u8(+20 via 18CD370) + u8(level via 18CD420) + u8(+704 via 18CD4D0)
    #   + u8 equip_count (18CC430; keep 0 unless list body proven)
    #   + tail pad for later u8/u32/u16 readers
    if head33 is None:
        head33 = bytes(33)
    if len(head33) != 33:
        raise ValueError("type0 head33 must be exactly 33 bytes, got %s" % len(head33))
    key_u16 = int(key) & 0xFFFF
    flag_u8 = int(entry_flag) & 0xFF
    job_u8 = int(job) & 0xFF
    grow_u8 = int(grow) & 0x0F  # low nibble consumed as v71[2]
    plus20 = int(grow if field_plus20 is None else field_plus20) & 0xFF
    level_u8 = int(level) & 0xFF
    plus704 = int(field_plus704) & 0xFF
    equip_u8 = int(equip_count) & 0xFF
    pad = max(0, int(tail_pad))
    payload = (
        head33
        + key_u16.to_bytes(2, "little")
        + bytes([job_u8, grow_u8, plus20, level_u8, plus704, equip_u8])
        + bytes(pad)
    )
    return bytes([0]) + (1).to_bytes(2, "little") + bytes([flag_u8]) + payload


class EnterSeriaRoomUserinfoType0BindMain0002Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def build_packet(self, selected: Any):
        if not _env_bool("USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE0", False):
            return None, (
                "S->C flag0/opcode=0x0002 USERINFO type0 bind-main suppressed; "
                "set USDOF_GAME_ENTER_SERIA_SEND_0002_USERINFO_TYPE0=1 only for explicit A/B"
            )
        body = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_BODY")
        if body is None:
            key = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_KEY",
                _env_int("USDOF_GAME_ENTER_SERIA_0131_KEY", 0),
            ) & 0xFFFF
            entry_flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_ENTRY_FLAG", 0) & 0xFF
            job = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_JOB",
                int(getattr(selected, "job", 0) or 0),
            ) & 0xFF
            grow = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_GROW",
                int(getattr(selected, "grow_type", 0) or 0),
            ) & 0x0F
            level = _env_int(
                "USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_LEVEL",
                int(getattr(selected, "level", 1) or 1),
            ) & 0xFF
            if level <= 0:
                level = 1
            plus20_raw = os.environ.get("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_PLUS20")
            if plus20_raw is None or not str(plus20_raw).strip():
                field_plus20 = grow
            else:
                field_plus20 = int(str(plus20_raw).strip(), 0) & 0xFF
            field_plus704 = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_PLUS704", 0) & 0xFF
            equip_count = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_EQUIP_COUNT", 0) & 0xFF
            tail_pad = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_TAIL_PAD", 256)
            head = _env_hex_bytes("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_HEAD33")
            body = build_userinfo_type0_bind_main_plaintext(
                key=key,
                entry_flag=entry_flag,
                job=job,
                grow=grow,
                level=level,
                field_plus20=field_plus20,
                field_plus704=field_plus704,
                equip_count=equip_count,
                head33=head,
                tail_pad=tail_pad,
            )
            body_note = (
                "type=0 count=1 entry_flag=%s key=%s job=%s grow=%s level=%s "
                "plus20=%s plus704=%s equip=%s head33=%s tail_pad=%s plain_len=%s"
                % (
                    entry_flag,
                    key,
                    job,
                    grow,
                    level,
                    field_plus20,
                    field_plus704,
                    equip_count,
                    (head.hex(" ") if head is not None else "00*33"),
                    tail_pad,
                    len(body),
                )
            )
        else:
            body_note = "body_override=%s" % body.hex(" ")
        flag = _env_int("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_FLAG", 0) & 0xFF
        mode = os.environ.get("USDOF_GAME_ENTER_SERIA_0002_USERINFO_TYPE0_MODE", "codec").strip().lower()
        if mode in {"raw", "plain"}:
            mode = "raw"
            packet = self.server._raw_response(flag, 0x0002, body)
        else:
            mode = "codec"
            packet = self.server._codec_response(flag, 0x0002, body)
        note = (
            "queued ENTER_SERIA USERINFO type0 bind-main n2500 path "
            "flag=%s opcode=0x0002 mode=%s %s; reverse 018D4140->18D0780->18CEA00->18CC9C0->25A5870->1E7A850; "
            "post-job 18CD370(+20)/18CD420(level)/18CD4D0(+704)/equip_count"
            % (flag, mode, body_note)
        )
        return packet, note
