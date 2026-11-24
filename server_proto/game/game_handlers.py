from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from game.game_codec import parse_piv_plain
from game.game_wire import build_game_packet
from game.initial_equipment import get_userinfo_job_base_for_job


@dataclass(frozen=True)
class HandlerDecision:
    response_packets: list[bytes]
    note: str


def zero_pad_game_payload(data: bytes, unit: int = 0x10) -> bytes:
    if unit <= 0:
        return data
    remainder = len(data) % unit
    return data if remainder == 0 else data + b"\x00" * (unit - remainder)


def _opcode1_profile() -> str:
    explicit = os.environ.get("USDOF_GAME_OPCODE1_PROFILE")
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return "usdof90-channelinfo-protobuf"


USDOF90_TYPE2_ENTRY_TAIL_LEN = 36
USERINFO_POST_EQUIPMENT_TAIL_LEN = 30
USERINFO_POST_TAIL_ROW720_BOOL_SOURCE_OFFSET = 13
USERINFO_POST_TAIL_18D02D0_U32_OFFSET = 15
USERINFO_POST_TAIL_RENDER_FLAGS_OFFSET = 19
USERINFO_POST_TAIL_EXTRA_U8_OFFSET = 20
USERINFO_POST_TAIL_EXTRA_U8_LEN = 10
USERINFO_POST_TAIL_EXTRA_U8_DEFAULT = b"\x00" * USERINFO_POST_TAIL_EXTRA_U8_LEN

ENTER_GAME_FOLLOWUP_OPCODES = {
    0x02EF,
    0x00C2,
    0x0505,
    0x0506,
    0x00FA,
    0x0117,
    0x0555,
}


def _pack_usdof90_grow_subgrow(
    grow_type: int, sub_grow_type: int | None = None
) -> int:
    """Pack the grow/sub-grow byte consumed at ``0x018D2187``.

    USDOF 90 does *not* read the ServerS4A12/DXF ``job, grow, level`` byte
    triplet at the start of a type=2 character entry.  Current x32dbg/IDA
    trace of ``0x018D2000 -> 0x01C21810 -> 0x01CBDAB0`` shows that the third
    byte after the DSTR name is passed through ``0x01E77170`` and then split as:

    - low nibble: ``grow_type``
    - high three bits: ``sub_grow_type``

    The base character id comes from the preceding byte copied to row ``+0x04``.
    Packing ``job`` here makes a Knight call ``sub_1CBDAB0(12, 12, 1)``, which
    returns the client's ``gt/sgt err`` fallback text.
    """

    grow = int(grow_type) & 0xFF
    if sub_grow_type is None:
        # ``characters.grow_type`` stores the Stage3 packed byte.  Preserve its
        # high awakening bits when the record has no separate sub-grow column.
        return grow & 0x7F
    return (grow & 0x0F) | ((int(sub_grow_type) & 0x07) << 4)


def build_userinfo_type2_character_entry_usdof90(
    *,
    slot_index: int = 0,
    job: int = 0,
    grow_type: int = 0,
    sub_grow_type: int | None = None,
    level: int = 1,
    context_byte: int = 0,
    object_id_byte: int | None = None,
    secondary_byte: int | None = None,
    tail_overrides: dict[int, int] | None = None,
) -> bytes:
    """Build one USDOF 2017/90 ``USERINFO type=2`` character entry.

    This is based on the current executable's ``0x018D2000`` parser, not on
    ServerS4A12.  In the zero-list/minimal path, after the initial ``u16
    slot_index`` the parser consumes 36 scalar bytes before returning to the
    type=2 top-level tail at ``0x018D4381``.

    Stable field map from static disassembly:

    ``tail[00]``
        ``u8`` passed to the account singleton at ``+0x102C``.
    ``tail[01]``
        ``u8`` copied to local ``+0x04`` and then to the character object
        via vfunc ``+0x348``.  Current PVF/IDA evidence shows this is the
        base character id from ``character/character.lst`` (Knight=12), not
        the per-job ``grow_type`` from ``character/Knight/Knight.chr``.
    ``tail[02]``
        packed grow/sub-grow byte: ``grow_type | (sub_grow_type << 4)``.
    ``tail[03]``
        scalar stored through helper ``0x18CD370`` at local ``+0x14``.  For a
        visible roster candidate we mirror ``grow_type`` here; this preserves
        the useful value that old DB prefixes happened to put at this offset
        while still setting the real packed grow/sub-grow byte at ``tail[02]``.
    ``tail[04]``
        scalar stored through helper ``0x18CD420`` at local ``+0x2B8``.  The
        old DB prefix put level here; keep the same value because trace shows
        this field survives as a compact one-byte level-like scalar.
    ``tail[06]``
        equipment/appearance list count consumed by ``0x18CC430``.  It must
        be zero for the minimal roster row; a ServerS4A12 appearance count or
        equipment blob here shifts every later field.

    The remaining fixed bytes are deliberately zero until their USDOF-specific
    semantics are proven.  Tests and runtime traces may override individual
    tail offsets with ``tail_overrides`` for controlled A/B probes.
    """

    def u8(value: int) -> int:
        return int(value) & 0xFF

    tail = bytearray(b"\x00" * USDOF90_TYPE2_ENTRY_TAIL_LEN)
    _, pvf_base_id = get_userinfo_job_base_for_job(job)
    base_character_id = pvf_base_id if object_id_byte is None else object_id_byte
    tail[0] = u8(context_byte)
    tail[1] = u8(base_character_id)
    tail[2] = _pack_usdof90_grow_subgrow(grow_type, sub_grow_type)
    tail[3] = u8(grow_type if secondary_byte is None else secondary_byte)
    tail[4] = u8(level)
    # tail[6] is the variable equipment/list count read by 0x18CC430.  Keep it
    # zero unless a later trace deliberately tests an explicit list payload.
    if tail_overrides:
        for offset, value in tail_overrides.items():
            idx = int(offset)
            if not 0 <= idx < USDOF90_TYPE2_ENTRY_TAIL_LEN:
                raise ValueError(f"USDOF90 type2 entry tail override out of range: {idx}")
            tail[idx] = u8(value)
    return (int(slot_index) & 0xFFFF).to_bytes(2, "little") + bytes(tail)


def build_userinfo_type2_minimal_plaintext(*, padded: bool = True) -> bytes:
    """Build the current minimal ``ENUM_NOTIPACKET_USERINFO`` type=2 body.

    Static trace of ``0x018D4140`` shows the type selector byte followed by a
    short set of account/list counters before any character-list loop:

    ``u8 type=2``
    ``u8 context_byte``
    ``u16 count_a``
    ``u16 count_b``
    ``u16 slot_or_count``
    ``u32 account_or_state``
    ``u16 character_entry_count``
    ``u8 ui_flag``
    ``u32 ui_value_a``
    ``u32 ui_value_b``

    All-zero fields intentionally mean “no character rows yet”.  This is not
    enough for enter-town, but it is safer than the earlier 16-byte probe
    because it covers the post-loop reads at ``0x018D4381..0x018D43A3``.
    """

    body = bytearray()
    body.append(0x02)
    body.append(0x00)
    body.extend((0).to_bytes(2, "little"))
    body.extend((0).to_bytes(2, "little"))
    body.extend((0).to_bytes(2, "little"))
    body.extend((0).to_bytes(4, "little"))
    body.extend((0).to_bytes(2, "little"))
    body.append(0x00)
    body.extend((0).to_bytes(4, "little"))
    body.extend((0).to_bytes(4, "little"))
    raw = bytes(body)
    return zero_pad_game_payload(raw, 0x10) if padded else raw


def build_userinfo_type2_character_entry_minimal(
    *,
    slot_index: int = 0,
    byte_00: int = 0,
    byte_01: int = 0,
    byte_02: int = 0,
    byte_03: int = 0,
    dword_04: int = 0,
    byte_08: int = 0,
    byte_09: int = 0,
    byte_0a: int = 0,
    byte_0b: int = 0,
    byte_0c: int = 0,
    dword_0d: int = 0,
    byte_11: int = 0,
    byte_12: int = 0,
    byte_13: int = 0,
    byte_14: int = 0,
    byte_15: int = 0,
    byte_16: int = 0,
    byte_17: int = 0,
    byte_18: int = 0,
    byte_19: int = 0,
    byte_1a: int = 0,
) -> bytes:
    """Build a low-level USDOF 90 ``0x018D2000`` parser probe.

    This function keeps the older ``byte_XX`` argument names used by tests and
    early traces, but the emitted layout has been corrected to the current
    USDOF executable:

    ``u16 slot_index`` + ``36`` fixed tail bytes for the zero-list path.

    Important correction: the former 27-byte/S4A12-prefix assumption was
    incomplete.  ``0x018D2000`` calls several nested readers
    (``0x18CD370``, ``0x18CD420``, ``0x18CC430``, ``0x18CD580``,
    ``0x18CD630``, ``0x18D02D0``, ``0x18CDC90``) and still consumes 36 tail
    bytes when all variable counts are zero.  The top-level USERINFO tail must
    follow those 36 bytes, not the old 27-byte prefix.
    """

    def u8(value: int) -> int:
        return int(value) & 0xFF

    tail = bytearray(b"\x00" * USDOF90_TYPE2_ENTRY_TAIL_LEN)
    # Direct fields before the first nested list reader.
    tail[0] = u8(byte_00)
    tail[1] = u8(byte_01)
    tail[2] = u8(byte_02)
    tail[3] = u8(byte_03)
    # ``0x18CD420`` reads exactly one byte here.  Keep the old dword_04
    # argument's low byte in the historic offset used by Lv.1/90 probes.
    tail[4] = u8(dword_04)
    # tail[6] is the ``0x18CC430`` variable-list count; leave zero.
    # The following offsets are the zero-list fixed scalar path.
    tail[11] = u8(byte_08)
    tail[12] = u8(byte_09)
    tail[13] = u8(byte_0a)
    tail[14] = u8(byte_0b)
    tail[15] = u8(byte_0c)
    tail[16:20] = (int(dword_0d) & 0xFFFFFFFF).to_bytes(4, "little")
    # Map the old trailing byte names onto the last proven fixed scalar bytes.
    for offset, value in zip(range(20, 30), [
        byte_11,
        byte_12,
        byte_13,
        byte_14,
        byte_15,
        byte_16,
        byte_17,
        byte_18,
        byte_19,
        byte_1a,
    ]):
        tail[offset] = u8(value)
    return (int(slot_index) & 0xFFFF).to_bytes(2, "little") + bytes(tail)


def build_userinfo_type2_one_character_plaintext(*, padded: bool = True) -> bytes:
    """Build a type=2 USERINFO body with one minimal character row.

    The no-character probe is structurally accepted by the client but cannot
    populate the character screen because ``character_entry_count`` is zero.
    This variant changes only that counter and appends the conservative
    all-zero ``0x018D2000`` character-entry candidate for slot/index 0.
    """

    body = bytearray()
    body.append(0x02)  # type
    body.append(0x00)  # context_byte
    body.extend((0).to_bytes(2, "little"))  # count_a
    body.extend((0).to_bytes(2, "little"))  # count_b
    body.extend((0).to_bytes(2, "little"))  # slot_or_count
    body.extend((0).to_bytes(4, "little"))  # account_or_state
    body.extend((1).to_bytes(2, "little"))  # character_entry_count
    body.extend(build_userinfo_type2_character_entry_minimal(slot_index=0))
    body.append(0x00)  # ui_flag
    body.extend((0).to_bytes(4, "little"))  # ui_value_a
    body.extend((0).to_bytes(4, "little"))  # ui_value_b
    raw = bytes(body)
    return zero_pad_game_payload(raw, 0x10) if padded else raw


def build_userinfo_type2_one_character_lv1_plaintext(*, padded: bool = True) -> bytes:
    """Build a slightly less-empty type=2 USERINFO roster probe.

    ``@userinfo2-onechar`` is the conservative parser-validation payload: it
    proves the client reaches and commits one ``0x018D2000`` entry, but every
    semantic field is zero.  A fully-zero entry is useful for reversing, yet it
    is a poor visible character-list candidate because the UI may treat the
    account as having no usable slot/level data.

    This helper keeps the same proven read layout while filling only the
    safest account/slot and Lv.1 fields:

    - ``count_a/count_b/slot_or_count = 32``: mirrors the common 32-character
      slot limit used by the DXF/ServerS4A12 reference and feeds the USDOF UI
      gate at ``0x018D4350`` with a non-zero slot count.
    - ``character_entry_count = 1``: one row.
    - character row ``byte_02 = 0`` and ``byte_03 = 0``: packed
      grow/sub-grow and grow placeholders.
    - character row ``dword_04 = 1``: Lv.1 candidate.  Static reads show this
      field is consumed as the first 32-bit scalar in the entry; using only the
      low byte keeps it compatible with older byte-level level encodings.

    It is intentionally still minimal: no appearance/equipment/town init data
    is claimed here.  The purpose is to make the character-selection UI more
    likely to present one selectable row so the next client opcode can be
    captured.
    """

    body = bytearray()
    body.append(0x02)  # type
    body.append(0x00)  # context_byte
    body.extend((32).to_bytes(2, "little"))  # count_a / slot limit candidate
    body.extend((32).to_bytes(2, "little"))  # count_b / slot effect count candidate
    body.extend((32).to_bytes(2, "little"))  # slot_or_count for UI gate
    body.extend((0).to_bytes(4, "little"))  # account_or_state
    body.extend((1).to_bytes(2, "little"))  # character_entry_count
    body.extend(
        build_userinfo_type2_character_entry_minimal(
            slot_index=0,
            byte_00=0,
            byte_01=0,
            byte_02=0,  # job candidate
            byte_03=0,  # grow-type candidate
            dword_04=1,  # level candidate, low byte only
        )
    )
    body.append(0x00)  # ui_flag
    body.extend((0).to_bytes(4, "little"))  # ui_value_a
    body.extend((0).to_bytes(4, "little"))  # ui_value_b
    raw = bytes(body)
    return zero_pad_game_payload(raw, 0x10) if padded else raw


def _dstr_utf8(value: str) -> bytes:
    data = value.encode("utf-8")
    return len(data).to_bytes(4, "little") + data


def _dstr_bytes(value: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, memoryview):
        data = value.tobytes()
    else:
        data = bytes(value)
    return len(data).to_bytes(4, "little") + data


def _record_attr(record: Any, name: str, default: Any = 0) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _record_sub_grow_type(record: Any) -> int | None:
    value = _record_attr(record, "sub_grow_type", None)
    return None if value is None else int(value)


def _record_name(record: Any) -> str:
    value = _record_attr(record, "name", "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _record_name_bytes(record: Any) -> bytes:
    value = _record_attr(record, "name_bytes", None)
    if value:
        return bytes(value)
    return _record_name(record).encode("utf-8")


def _userinfo_equipment_entries(entries: Any) -> list[tuple[int, int, int, int, int, int]]:
    if not entries:
        return []
    result: list[tuple[int, int, int, int, int, int]] = []
    for entry in entries:
        if isinstance(entry, dict):
            slot = entry.get("slot", 0)
            item_id = entry.get("item_id", 0)
            clear_avatar_id = entry.get("clear_avatar_id", 0)
            flag = entry.get("flag", 0)
            aux1 = entry.get("aux1", entry.get("expire_time", 0))
            aux2 = entry.get("aux2", 0)
            unknown = entry.get("unknown", 0)
        else:
            slot = getattr(entry, "slot", None)
            item_id = getattr(entry, "item_id", None)
            clear_avatar_id = getattr(entry, "clear_avatar_id", 0)
            flag = getattr(entry, "flag", 0)
            aux1 = getattr(entry, "aux1", getattr(entry, "expire_time", 0))
            aux2 = getattr(entry, "aux2", 0)
            unknown = getattr(entry, "unknown", 0)
            if slot is None or item_id is None:
                values = list(entry)
                slot = values[0] if len(values) > 0 else 0
                item_id = values[1] if len(values) > 1 else 0
                clear_avatar_id = values[6] if len(values) > 6 else 0
                flag = values[2] if len(values) > 2 else 0
                aux1 = values[3] if len(values) > 3 else 0
                aux2 = values[4] if len(values) > 4 else 0
                unknown = values[5] if len(values) > 5 else 0
        result.append(
            (
                int(slot) & 0xFF,
                int(clear_avatar_id or item_id) & 0xFFFFFFFF,
                int(flag) & 0xFF,
                int(aux1) & 0xFFFFFFFF,
                int(aux2) & 0xFFFFFFFF,
                int(unknown) & 0xFF,
            )
        )
    return result


def _build_userinfo_equipment_entry(entry: tuple[int, int, int, int, int, int]) -> bytes:
    slot, item_id, flag, aux1, aux2, unknown = entry
    custom_blob = b""
    return (
        bytes([slot])
        + item_id.to_bytes(4, "little")
        + len(custom_blob).to_bytes(4, "little")
        + custom_blob
        + bytes([flag])
        + aux1.to_bytes(4, "little")
        + aux2.to_bytes(4, "little")
        + bytes([unknown])
    )


def _build_userinfo_post_equipment_tail(*, rename_required: bool | None = None) -> bytes:
    """Build the scalar tail consumed by current USDOF ``0x018D2000``.

    The render flag byte must be the byte read by ``0x018CDC90``.  x32dbg
    confirmed it is after this exact sequence, not 19 bytes after the old
    15-byte equipment entries.

    IDA confirmed ``0x018CDC90`` writes row ``+882`` as ``bit3(flags) == 0``;
    ``0x01C20EA0`` copies that byte to char-card ``this+4340``.  Dynamic
    x32dbg checks later ruled this field out as the direct red-frame trigger,
    but bit0/bit1/bit3 still need to stay on the known-good render path.

    ``tail[13]`` is the raw byte read at ``0x018D22B7``.  The client stores
    ``tail[13] == 1`` as row ``+720`` and later passes that byte to
    ``0x01BFFC70`` during character-card commit.

    The same commit reads row ``+724/+728/+732`` for ``0x01BFFB90``. Current
    sentinelfix evidence only proves constructor/default zeros for those
    fields; no packet setter in this post-equipment tail has been confirmed.
    Keep them as client defaults instead of assigning guessed wire bytes.

    The final ten u8 fields are still partially named. Static mapping shows
    ``tail[24]`` flows into row ``+796`` and then char-card ``this+4064``;
    ``tail[27]`` flows into row ``+904`` and then char-card ``this+4065``.
    ServerS4A12's verified character-list trailer uses the same relative
    ``0x03`` and ``0x04`` sentinels at those two positions, while leaving the
    surrounding bytes zero. Live USDOF validation on 2026-07-06 showed that
    using those as defaults did not clear the red frame, so they remain only
    env-driven A/B values.
    """

    render_flags = _env_int("USDOF_USERINFO_RENDER_FLAGS", 0x0B) & 0xFF
    if rename_required is not None:
        render_flags = (render_flags & ~1) | int(bool(rename_required))
    post_18d02d0_u32 = _env_int("USDOF_USERINFO_POST_18D02D0_U32", 0x04) & 0xFFFFFFFF
    row720_bool_source = _env_int("USDOF_USERINFO_ROSTER_TAIL_720", 0) & 0xFF
    extra_u8s = _userinfo_post_tail_extra_u8s()
    body = bytearray()
    body.extend((0).to_bytes(4, "little"))  # 0x018D2212 u32
    body.extend(b"\x00\x00\x00\x00")  # 0x018D2232, 0x2253, 0x2274, 0x2288 u8s
    body.extend(b"\x00")  # 0x018CD580 u8
    body.extend((0).to_bytes(4, "little"))  # 0x018CD630 u32
    body.extend(bytes([row720_bool_source]))  # 0x018D22B7 bool source -> row+720
    body.extend(b"\x00")  # 0x018D02D0 u8
    body.extend(post_18d02d0_u32.to_bytes(4, "little"))  # 0x018D22E5 u32
    body.extend(bytes([render_flags]))  # 0x018CDC90 u8
    body.extend(extra_u8s)  # remaining u8 fields before vfunc_788 commit
    if len(body) != USERINFO_POST_EQUIPMENT_TAIL_LEN:
        raise AssertionError(f"USERINFO post-equipment tail length changed: {len(body)}")
    return bytes(body)


def _userinfo_post_tail_extra_u8s() -> bytes:
    data = bytearray(
        _env_hex_bytes(
            "USDOF_USERINFO_POST_TAIL_EXTRA_U8S",
            USERINFO_POST_TAIL_EXTRA_U8_DEFAULT,
            USERINFO_POST_TAIL_EXTRA_U8_LEN,
        )
    )
    for tail_offset in range(
        USERINFO_POST_TAIL_EXTRA_U8_OFFSET,
        USERINFO_POST_TAIL_EXTRA_U8_OFFSET + USERINFO_POST_TAIL_EXTRA_U8_LEN,
    ):
        env_name = f"USDOF_USERINFO_POST_TAIL_{tail_offset}"
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip():
            data[tail_offset - USERINFO_POST_TAIL_EXTRA_U8_OFFSET] = int(raw, 0) & 0xFF
    return bytes(data)


def _template_attr(template: Any, name: str, default: int) -> int:
    if template is None:
        return default
    if isinstance(template, dict):
        return int(template.get(name, default))
    return int(getattr(template, name, default))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw, 0)


def _env_hex_bytes(name: str, default: bytes, expected_len: int) -> bytes:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bytes(default)
    cleaned = raw.strip().replace(" ", "").replace("_", "").replace("-", "")
    if len(cleaned) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits")
    data = bytes.fromhex(cleaned)
    if len(data) != expected_len:
        raise ValueError(f"{name} must decode to {expected_len} bytes, got {len(data)}")
    return data


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return int(raw, 0)


def _env_optional_int_for_job(prefix: str, job: int) -> int | None:
    per_job = _env_optional_int(f"{prefix}_{int(job) & 0xFF}")
    if per_job is not None:
        return per_job
    return _env_optional_int(prefix)


def _slot_limit_from_template(template: Any) -> int:
    primary = _template_attr(template, "gate_or_count1", 128)
    secondary = _template_attr(template, "gate_or_count2", 128)
    if primary > 0:
        return primary
    if secondary > 0:
        return secondary
    return 128


def build_userinfo_type2_character_entry_dxf_style(
    *,
    slot_index: int = 0,
    name: str | bytes = "Hero",
    job: int = 0,
    grow_type: int = 0,
    sub_grow_type: int | None = None,
    level: int = 1,
    equipment_entries: Any = None,
    base_character_id: int | None = None,
    rename_required: bool | None = None,
) -> bytes:
    """Build a current-USDOF DSTR account roster character entry.

    The first one-character P1 probe used a deliberately empty fixed-size row.
    That was useful because it proved USDOF reaches ``0x018D2000`` and commits
    one entry, but the visible UI has no useful data to render: the name is
    empty and the inferred level/job bytes are effectively zero.

    Current ``0x018D2000`` reads DSTR roster entries as:

    ``u16 slot_index``
    ``DSTR character_name`` (``u32 byte_len`` + raw name bytes)
    ``u8 roster-vector key``.  Current tracing shows ``0x018D212B`` reads this
    byte, looks up ``sub_15F4DE0(value)`` and pushes the value into that
    object's ``+0x102C`` vector.  This is *not* the model family byte used by
    the character card.  Sending a non-Slayer job id here makes the client
    enter the non-Slayer forced/invalid card-state path; the normal local
    roster keeps it at zero and uses the next byte for the actual model base.
    ``u8 base_character_id`` from ``character/character.lst``; this is copied
    to row ``+0x04`` and drives the character-card model-family vfunc
    ``u8 packed_grow_subgrow``
    ``u8 level/display scalar`` via ``0x18CD370``
    ``u8 level candidate`` via ``0x18CD420``
    ``u8 row+0x1C/town-death state``.  ``0x01C16C40`` checks this field
    against ``4`` in one special-resource branch, but live testing identified
    this as a town-death / monster-siege state rather than a normal roster
    field.  Keep the default at ``0`` and only change it for explicit A/B
    tests.
    ``u8 equipment_count``
    ``equipment entries...``
    post-equipment scalar/trailer bytes, including the render flag byte read by
    ``0x018CDC90``

    This deliberately differs from the earlier DXF-style assumption.  The
    second byte after the name is what drives the CharCard model family; the
    first byte is kept at zero by default to avoid seeding the client's
    per-class ``+0x102C`` roster vector with an invalid local-single-player
    value.

    The variable list is based on the current USDOF executable's
    ``0x018CC430`` parser.  One list entry is:

    ``u8 slot, u32 item_id, u32 custom_len, custom bytes, u8 flag, u32 aux1,
    u32 aux2, u8 unknown``.

    The ``custom_len`` field is read by ``sub_1AAEBB0(..., 0x29) ->
    sub_2BF4BC0``.  Omitting it shifts later equipment and render fields.

    The ``0x018CDC90`` flag byte is passed through
    ``sub_2E874E0(flags, 1)`` into the character-card vfunc that writes the
    profession text.  Keep bit 0 set as well because the function also copies
    bit 0 into row ``+0x2F5``.

    The surrounding USDOF type=2 header is still the proven Stage3 header
    layout used by ``build_userinfo_type2_one_character_lv1_plaintext``; only
    the per-character row is upgraded from parser-probe bytes to a renderable
    roster row.
    """

    def u8(value: int) -> int:
        return int(value) & 0xFF

    equipment = _userinfo_equipment_entries(equipment_entries)
    if not _env_bool("USDOF_USERINFO_ROSTER_SEND_EQUIPMENT", False):
        # Roster USERINFO is only the role-select card projection.  Keep newly
        # created PVF equipment in SQLite for later room/town flows, but do not
        # feed it into the select-character card by default.  Live USDOF traces
        # show several non-Slayer cards enter the change-name/red-frame overlay
        # when their create equipment is present in this row.
        equipment = ()
    elif (
        int(job) == 12
        and not _env_bool("USDOF_USERINFO_ROSTER_SEND_KNIGHT_WEAPON", False)
    ):
        # Retained A/B mode for equipment rendering probes: Knight has a
        # support-weapon path, so sending the normal weapon here makes the
        # role-select card draw overlapping weapons.
        equipment = tuple(entry for entry in equipment if entry[0] != 12)
    _pvf_job_id, pvf_base_id = get_userinfo_job_base_for_job(job)
    base_id = pvf_base_id if base_character_id is None else int(base_character_id)
    base_override = _env_optional_int("USDOF_USERINFO_BASE_CHARACTER_ID_OVERRIDE")
    if base_override is not None:
        base_id = base_override
    # 2026-07-08 red-frame A/B:
    #   DSTR(name) + 0: read at 0x018D212B, then pushed into
    #                  sub_15F4DE0(value)+0x102C via 0x004C3270.
    #   DSTR(name) + 1: copied to row+0x04 and used by the CharCard base/model
    #                  dispatch.
    #
    # The old default mirrored the PVF job id into both bytes.  That makes
    # non-Slayer rosters seed the class-specific +0x102C vector and correlates
    # with the bogus forced red/change-name card overlay.  Keep the vector key
    # zero for the normal local roster; retain the override for reverse A/B.
    # Native select reads this per-slot state to open window 0x150.
    job_byte = int(bool(rename_required))
    job_override = _env_optional_int("USDOF_USERINFO_JOB_BYTE_OVERRIDE")
    if job_override is not None:
        job_byte = job_override
    grow_override = _env_optional_int_for_job("USDOF_USERINFO_GROW_TYPE_OVERRIDE", job)
    if grow_override is not None:
        grow_type = grow_override
    sub_grow_override = _env_optional_int_for_job("USDOF_USERINFO_SUB_GROW_TYPE_OVERRIDE", job)
    if sub_grow_override is not None:
        sub_grow_type = sub_grow_override
    row_1c_state = _env_int("USDOF_USERINFO_ROW_1C_STATE", 0) & 0xFF
    body = bytearray()
    body.extend((int(slot_index) & 0xFFFF).to_bytes(2, "little"))
    body.extend(_dstr_bytes(name))
    tail_prefix = bytearray(
        [
            u8(job_byte),
            u8(base_id),
            _pack_usdof90_grow_subgrow(grow_type, sub_grow_type),
            u8(level),
            u8(level),
            row_1c_state,
            len(equipment) & 0xFF,
        ]
    )
    for entry in equipment:
        tail_prefix.extend(_build_userinfo_equipment_entry(entry))
    body.extend(tail_prefix)
    body.extend(_build_userinfo_post_equipment_tail(rename_required=rename_required))
    return bytes(body)


def build_userinfo_type2_roster_plaintext(
    characters: list[Any] | tuple[Any, ...],
    *,
    template: Any = None,
    profile: str = "us-trace-dstr",
    padded: bool = True,
    account_or_state: int | None = None,
    ui_flag: int | None = None,
    ui_value_a: int | None = None,
    ui_value_b: int | None = None,
    rename_pending_ids: frozenset[int] | None = None,
) -> bytes:
    """Build a database-backed ``ENUM_NOTIPACKET_USERINFO`` type=2 body.

    This is the bridge between the ServerS4A12 database/business layer and the
    USDOF Stage3 client parser currently under trace.

    Profiles:

    ``us-trace-dstr`` (recommended for non-empty rosters)
        Uses the USDOF-traced type=2 header layout that already reaches
        ``0x018D2000`` and supplies the ``DSTR(name)`` field read immediately
        after ``u16 slot``.  Omitting this string makes the current client use
        its default string resource, observed in UI as "please change name to
        continue playing".

    ``us-trace-entry-prefix`` / ``usdof90`` (A/B only)
        Emits the USDOF 90 ``0x018D2000`` compact row: ``u16 slot`` plus the
        36-byte zero-list tail proven by static disassembly.  The third byte
        after the DSTR name is the packed ``grow_type | (sub_grow_type << 4)`` field, while
        the old S4A12-style ``job,grow,level`` prefix is not used as a format
        authority.

    ``us-trace-fixed``
        Keeps the older fixed-size parser probe row and fills only slot,
        job/grow/level candidates from DB.  Useful if the DSTR row crashes and
        we need to bisect.

    ``us-trace-entry-full``
        Same USDOF-traced type=2 header as ``us-trace-entry-prefix``, but emits
        the complete ServerS4A12/DB `body_after_name` blob after the ``u16``
        slot/index.  Retained only as an A/B regression profile: with the
        current USDOF 2017 Stage3 parser this over-emits, leaving hundreds of
        bytes before the top-level tail and causing tail fields to be read from
        the wrong offset.

    ``s4a12-body``
        Emits the open-source ServerS4A12 type=2 body layout verbatim.  This is
        retained as an A/B reference, but it is not the default because the
        USDOF Stage3 handler trace currently places ``entryCount`` earlier than
        this body does.
    """

    normalized = profile.strip().lower().replace("_", "-")
    rows = list(characters)
    # Keep continuous visual order: sort by DB slot_index before packing type=2.
    rows.sort(
        key=lambda r: (
            int(_record_attr(r, "slot_index", 0x7FFFFFFF)),
            int(_record_attr(r, "character_id", 0)),
        )
    )
    slot_limit = _slot_limit_from_template(template)
    slot_effect = _template_attr(template, "gate_or_count2", slot_limit)
    if slot_effect <= 0:
        slot_effect = slot_limit
    entry_count = min(len(rows), 0xFFFF)
    empty_counters_zero = (
        entry_count == 0
        and os.environ.get("USDOF_USERINFO_EMPTY_COUNTERS_ZERO", "0").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    occupied_count = min(entry_count, slot_limit)
    count_mode = os.environ.get("USDOF_USERINFO_TYPE2_COUNT_MODE", "total-total-occupied").strip().lower()
    if empty_counters_zero:
        header_count_a = 0
        header_count_b = 0
    elif count_mode in {"total-total-occupied", "slotlimit-slotlimit-occupied", "ab-32-32-n"}:
        # Confirmed current default: keep the visible/live slot count in
        # slot_or_count while setting the middle counter to the slot limit.
        # Frontend validation showed 32/32/N suppresses the expanded-slot
        # unlock animation that still occurs with 32/0/N.
        header_count_a = slot_limit
        header_count_b = slot_limit
    elif count_mode in {"ui-occupied-total", "occupied-total", "display-occupied-total"}:
        # Retained A/B mode.  The current normal empty roster no longer needs
        # a launcher-script override: the source default preserves the slot
        # counters as 32/32/0 via total-total-occupied.  This older candidate
        # keeps the empty roster at 32/0/0 when
        # USDOF_USERINFO_EMPTY_COUNTERS_ZERO=0.
        header_count_a = slot_limit
        header_count_b = 0
    elif count_mode in {"legacy-occupied-total", "legacy-32-n-32", "slotlimit-occupied-slotlimit", "ab-32-n-32"}:
        # Explicit name for the early 2026-07-05 A/B shape.  It advertised the
        # live occupied count in count_b while keeping slot_or_count at the full
        # slot limit, producing 32/N/32 for non-empty rosters.
        header_count_a = slot_limit
        header_count_b = occupied_count
    elif count_mode in {"occupied-display", "occupied", "display-occupied"} and entry_count > 0:
        # Historical A/B mode: this rendered 32/1 for a one-character roster
        # and is retained only for regression probes.
        header_count_a = occupied_count
        header_count_b = 0
    else:
        header_count_a = slot_limit
        header_count_b = occupied_count
    if empty_counters_zero:
        header_slot_or_count = 0
    elif count_mode in {
        "ui-occupied-total",
        "occupied-total",
        "display-occupied-total",
        "total-total-occupied",
        "slotlimit-slotlimit-occupied",
        "ab-32-32-n",
    }:
        header_slot_or_count = occupied_count
    elif count_mode in {"legacy-occupied-total", "legacy-32-n-32", "slotlimit-occupied-slotlimit", "ab-32-n-32"}:
        header_slot_or_count = slot_limit
    else:
        header_slot_or_count = slot_limit
    account_or_state_value = (
        _env_int("USDOF_USERINFO_TYPE2_ACCOUNT_OR_STATE", 0)
        if account_or_state is None
        else int(account_or_state)
    ) & 0xFFFFFFFF
    ui_flag_value = (
        _env_int("USDOF_USERINFO_TYPE2_UI_FLAG", 0)
        if ui_flag is None
        else int(ui_flag)
    ) & 0xFF
    ui_value_a_value = (
        _env_int("USDOF_USERINFO_TYPE2_UI_VALUE_A", 0)
        if ui_value_a is None
        else int(ui_value_a)
    ) & 0xFFFFFFFF
    ui_value_b_value = (
        _env_int("USDOF_USERINFO_TYPE2_UI_VALUE_B", 0)
        if ui_value_b is None
        else int(ui_value_b)
    ) & 0xFFFFFFFF

    body = bytearray()

    if normalized in {"s4a12-body", "server-s4a12-body", "dxf-body"}:
        body.append(0x02)
        body.extend((slot_limit & 0xFFFF).to_bytes(2, "little"))
        body.extend((slot_effect & 0xFFFF).to_bytes(2, "little"))
        body.append(_template_attr(template, "flag_or_manage", 0) & 0xFF)
        body.extend((_template_attr(template, "key_or_point", 0) & 0xFFFFFFFF).to_bytes(4, "little"))
        body.extend((_template_attr(template, "unknown16", 0) & 0xFFFF).to_bytes(2, "little"))
        body.extend((_template_attr(template, "unknown32", 0) & 0xFFFFFFFF).to_bytes(4, "little"))
        body.extend((entry_count & 0xFFFF).to_bytes(2, "little"))
        for index, record in enumerate(rows[:entry_count]):
            body.extend(
                build_userinfo_type2_character_entry_dxf_style(
                    slot_index=int(_record_attr(record, "slot_index", index)),
                    name=_record_name_bytes(record),
                    job=int(_record_attr(record, "job", 0)),
                    grow_type=int(_record_attr(record, "grow_type", 0)),
                    level=int(_record_attr(record, "level", 1)),
                    equipment_entries=_record_attr(record, "equipped_entries", ()),
                )
            )
        raw = bytes(body)
        return zero_pad_game_payload(raw, 0x10) if padded else raw

    # USDOF Stage3 traced header used by current accepted probes:
    #   u8  type
    #   u8  context
    #   u16 count_a/slot-limit candidate
    #   u16 count_b/slot-effect candidate
    #   u16 slot_or_count gate
    #   u32 account/state candidate
    #   u16 entry_count
    body.append(0x02)
    body.append(0x00)
    body.extend((header_count_a & 0xFFFF).to_bytes(2, "little"))
    body.extend((header_count_b & 0xFFFF).to_bytes(2, "little"))
    body.extend((header_slot_or_count & 0xFFFF).to_bytes(2, "little"))
    body.extend(account_or_state_value.to_bytes(4, "little"))
    body.extend((entry_count & 0xFFFF).to_bytes(2, "little"))

    if normalized in {"us-trace-fixed", "trace-fixed", "minimal-fixed"}:
        for index, record in enumerate(rows[:entry_count]):
            body.extend(
                build_userinfo_type2_character_entry_minimal(
                    slot_index=int(_record_attr(record, "slot_index", index)),
                    byte_02=_pack_usdof90_grow_subgrow(
                        int(_record_attr(record, "grow_type", 0)),
                        _record_sub_grow_type(record),
                    ),
                    byte_03=int(_record_attr(record, "grow_type", 0)),
                    dword_04=int(_record_attr(record, "level", 1)),
                )
            )
    elif normalized in {
        "us-trace-entry-prefix",
        "us-trace-compact",
        "usdof90",
        "usdof90-compact",
        "trace-compact",
        "trace-entry-prefix",
        "db-entry-prefix",
        "database-compact",
    }:
        for index, record in enumerate(rows[:entry_count]):
            slot_index = int(_record_attr(record, "slot_index", index)) & 0xFFFF
            body.extend(
                build_userinfo_type2_character_entry_usdof90(
                    slot_index=slot_index,
                    job=int(_record_attr(record, "job", 0)),
                    grow_type=int(_record_attr(record, "grow_type", 0)),
                    sub_grow_type=_record_sub_grow_type(record),
                    level=int(_record_attr(record, "level", 1)),
                )
            )
    elif normalized in {
        "us-trace-entry-full",
        "trace-entry-full",
        "us-trace-s4a12-full",
        "server-s4a12-full",
        "db-entry-full",
    }:
        for index, record in enumerate(rows[:entry_count]):
            slot_index = int(_record_attr(record, "slot_index", index)) & 0xFFFF
            body_after_name = _record_attr(record, "body_after_name", None)
            if body_after_name:
                entry_tail = bytes(body_after_name)
            else:
                # Fallback for freshly-created DB characters that do not yet
                # have a captured account_character_entries row.  Keep the
                # proven first bytes, then pad to the full captured 2017 entry
                # size used by the seed DB so 0x018D2000 does not consume the
                # post-loop USERINFO tail as missing per-character fields.
                entry_tail = (
                    bytes(
                        [
                            0,
                            0,
                            int(_record_attr(record, "job", 0)) & 0xFF,
                            int(_record_attr(record, "grow_type", 0)) & 0xFF,
                            int(_record_attr(record, "level", 1)) & 0xFF,
                        ]
                    )
                    + b"\x00" * (324 - 5)
                )
            body.extend((slot_index & 0xFFFF).to_bytes(2, "little"))
            body.extend(entry_tail)
    elif normalized in {"us-trace-dstr", "trace-dstr", "database", "db", "server-s4a12-entry"}:
        if rename_pending_ids is None:
            from game.character.character_rename import pending_ids
            rename_pending_ids = pending_ids()
        for index, record in enumerate(rows[:entry_count]):
            body.extend(
                build_userinfo_type2_character_entry_dxf_style(
                    slot_index=int(_record_attr(record, "slot_index", index)),
                    name=_record_name_bytes(record),
                    job=int(_record_attr(record, "job", 0)),
                    grow_type=int(_record_attr(record, "grow_type", 0)),
                    sub_grow_type=_record_sub_grow_type(record),
                    level=int(_record_attr(record, "level", 1)),
                    equipment_entries=_record_attr(record, "equipped_entries", ()),
                    rename_required=int(_record_attr(record, "character_id", 0)) in rename_pending_ids,
                )
            )
    else:
        raise ValueError(f"unknown USERINFO roster profile: {profile!r}")

    # Stage3 type=2 handler tail reads observed at 0x018D4381..0x018D43AB.
    body.append(ui_flag_value)
    body.extend(ui_value_a_value.to_bytes(4, "little"))
    body.extend(ui_value_b_value.to_bytes(4, "little"))
    raw = bytes(body)
    return zero_pad_game_payload(raw, 0x10) if padded else raw


def build_userinfo_type2_one_character_named_lv1_plaintext(*, padded: bool = True) -> bytes:
    """Build a Stage3 type=2 USERINFO roster with one named Lv.1 character.

    Live USDOF Stage3 tracing proved the header/counter part below is accepted:

    ``u8 type``
    ``u8 context``
    ``u16 count_a``
    ``u16 count_b``
    ``u16 slot_or_count``
    ``u32 account_or_state``
    ``u16 character_entry_count``

    The earlier ``@userinfo2-onechar-lv1`` payload placed an all-zero fixed
    row after that count.  This variant keeps the same accepted header and
    count position, but supplies a DXF-style character row carrying a visible
    name/job/grow/level plus the small post-loop tail already covered by the
    Stage3 handler trace.
    """

    return build_userinfo_type2_roster_plaintext(
        [
            {
                "slot_index": 0,
                "name": os.environ.get("USDOF_P1_CHARACTER_NAME", "Hero"),
                "job": int(os.environ.get("USDOF_P1_CHARACTER_JOB", "0"), 0),
                "grow_type": int(os.environ.get("USDOF_P1_CHARACTER_GROW", "0"), 0),
                "level": int(os.environ.get("USDOF_P1_CHARACTER_LEVEL", "1"), 0),
            }
        ],
        profile="us-trace-dstr",
        padded=padded,
    )


def named_plaintext_probe_payload(name: str) -> bytes:
    normalized = name.strip().lower().replace("_", "-")
    if normalized in {"userinfo2-min", "userinfo-type2-min", "userinfo2-minimal", "user-info2-minimal"}:
        return build_userinfo_type2_minimal_plaintext(padded=True)
    if normalized in {
        "userinfo2-onechar",
        "userinfo2-one-character",
        "userinfo-type2-onechar",
        "userinfo-type2-one-character",
        "user-info2-onechar",
    }:
        return build_userinfo_type2_one_character_plaintext(padded=True)
    if normalized in {
        "userinfo2-onechar-lv1",
        "userinfo2-one-character-lv1",
        "userinfo-type2-onechar-lv1",
        "userinfo-type2-one-character-lv1",
        "user-info2-onechar-lv1",
        "userinfo2-lv1",
        "userinfo-type2-lv1",
    }:
        return build_userinfo_type2_one_character_lv1_plaintext(padded=True)
    if normalized in {
        "userinfo2-onechar-named-lv1",
        "userinfo2-one-character-named-lv1",
        "userinfo-type2-onechar-named-lv1",
        "userinfo-type2-one-character-named-lv1",
        "userinfo2-roster-onechar",
        "userinfo2-roster-one-character",
        "userinfo2-roster-onechar-lv1",
        "userinfo2-dxf-onechar",
        "userinfo2-dxf-one-character",
        "userinfo2-dxf-onechar-lv1",
    }:
        return build_userinfo_type2_one_character_named_lv1_plaintext(padded=True)
    raise ValueError(f"unknown named plaintext payload: {name!r}")


def _ror8(value: int, count: int) -> int:
    count &= 7
    value &= 0xFF
    return ((value >> count) | (value << (8 - count))) & 0xFF


def _u32le(value: int) -> bytes:
    return (int(value) & 0xFFFFFFFF).to_bytes(4, "little")


def _ascii_dstr(value: str) -> bytes:
    data = (value or "").encode("ascii", errors="replace")
    return _u32le(len(data)) + data


def build_login_initial_notice_plaintext(
    *,
    channel_name: str | None = None,
    channel_server_index: int | None = None,
    channel_index: int | None = None,
    server_ip: str | None = None,
    udp_port1: int | None = None,
    udp_port2: int | None = None,
    command_packet_count: int | None = None,
    notification_packet_count: int | None = None,
    unix_time: int | None = None,
) -> bytes:
    """Build the ServerS4A12-style ``flag0/opcode1`` initial login notice.

    ServerS4A12 sends this packet immediately after the Game socket is accepted:

    ``u8 success``
    ``AsciiDstr channelName``
    ``i32 0``
    ``i32 0``
    ``u8 channelServerIndex``
    ``u8 channelIndex``
    ``u8 0``
    ``i32 unixTime``
    ``i32 1``
    ``AsciiDstr serverIp``
    ``i32 initialUdpPort1``
    ``i32 initialUdpPort2``
    ``u8 0``
    ``u8 0``
    ``i32 commandPacketCount``
    ``i32 notificationPacketCount``

    The previous USDOF probe emitted only three zero plaintext bytes.  Static
    reversing of the current P1 registry points opcode ``0x0001`` at
    ``0x01601B80``; its first branch is gated by the leading status byte.  A
    zero status can exercise the failure/notice path and still make the client
    send PIV/ACK, but it leaves the character-select transition under-
    initialized.  This builder mirrors the maintained DXF/ServerS4A12 shape so
    the same handler can run the success branch before USERINFO is sent.
    """

    if unix_time is None:
        fixed = os.environ.get("USDOF_GAME_INITIAL_NOTICE_TIME")
        unix_time = int(fixed, 0) if fixed else int(time.time())

    channel_name = channel_name if channel_name is not None else os.environ.get("USDOF_GAME_CHANNEL_NAME", "ch.11")
    channel_server_index = (
        int(channel_server_index)
        if channel_server_index is not None
        else int(os.environ.get("USDOF_GAME_CHANNEL_SERVER_INDEX", "1"), 0)
    )
    channel_index = (
        int(channel_index)
        if channel_index is not None
        else int(os.environ.get("USDOF_GAME_CHANNEL_INDEX", "11"), 0)
    )
    server_ip = server_ip if server_ip is not None else os.environ.get("USDOF_GAME_SERVER_IP", "127.0.0.1")
    udp_port1 = int(udp_port1) if udp_port1 is not None else int(os.environ.get("USDOF_GAME_INITIAL_UDP_PORT1", "12311"), 0)
    udp_port2 = int(udp_port2) if udp_port2 is not None else int(os.environ.get("USDOF_GAME_INITIAL_UDP_PORT2", "12312"), 0)
    command_packet_count = (
        int(command_packet_count)
        if command_packet_count is not None
        else int(os.environ.get("USDOF_GAME_COMMAND_PACKET_COUNT", "1086"), 0)
    )
    notification_packet_count = (
        int(notification_packet_count)
        if notification_packet_count is not None
        else int(os.environ.get("USDOF_GAME_NOTIFICATION_PACKET_COUNT", "1036"), 0)
    )

    body = bytearray()
    body.append(0x01)
    body.extend(_ascii_dstr(channel_name))
    body.extend(_u32le(0))
    body.extend(_u32le(0))
    body.append(channel_server_index & 0xFF)
    body.append(channel_index & 0xFF)
    body.append(0x00)
    body.extend(_u32le(unix_time))
    body.extend(_u32le(1))
    body.extend(_ascii_dstr(server_ip))
    body.extend(_u32le(udp_port1))
    body.extend(_u32le(udp_port2))
    body.append(0x00)
    body.append(0x00)
    body.extend(_u32le(command_packet_count))
    body.extend(_u32le(notification_packet_count))
    return bytes(body)


def build_usdof90_channelinfo_protobuf_plaintext(*, mode: int | None = None, packet_type: int | None = None,
                                               raid_namespace: int | None = None) -> bytes:
    """Build the USDOF 90 ``flag0/opcode1`` ChannelInfo protobuf envelope.

    This packet is *not* the ServerS4A12/DXF initial-login notice format.
    Static reversing of the current USDOF executable shows that the concrete
    handler at ``0x01664E20`` reads:

    ``u32 protobuf_size``
        via ``0x02C74B00``;

    ``protobuf_size`` bytes
        parsed as ``DNFPB.noti.PB_ENUM_NOTIPACKET_CHANNELINFO`` by the class
        whose parser starts at ``0x004ED0B0``.

    The handler's successful UI-state transition then tests two parsed fields:

    - object ``+0x40`` (protobuf field #2) must be ``0``;
    - object ``+0x6C`` (protobuf field #16) must be ``4``.

    A minimal protobuf body that satisfies the proven handler branch is:

    - field #1, varint, value ``1`` (accepted by the enum validator
      ``0x004F75B0`` and useful as a conservative packet-type marker);
    - field #2, varint, value ``0`` (explicitly keeps ``+0x40 == 0``);
    - field #16, varint, value ``4`` (the branch condition checked at
      ``0x01665014``).

    The surrounding special opcode1 obfuscation is still applied by
    :func:`build_special_channelinfo_payload`; this function returns the
    plaintext *before* that byte transform.
    """

    if packet_type is None:
        packet_type = _env_int("USDOF_GAME_CHANNELINFO_PACKET_TYPE", 1)
    if mode is None:
        mode = _env_int("USDOF_GAME_CHANNELINFO_MODE", 4)

    # Tiny protobuf varint encoder.  Keep it local to avoid pretending that we
    # already have a broad protobuf runtime for every DFO packet.
    def varint(value: int) -> bytes:
        value = int(value)
        if value < 0:
            raise ValueError("protobuf varint value must be non-negative")
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    msg = bytearray()
    if packet_type:
        msg.append(0x08)  # field #1, wire type 0
        msg.extend(varint(packet_type))
    msg.append(0x10)  # field #2, wire type 0 -> object+0x40
    msg.extend(varint(0))
    if raid_namespace is not None:
        if type(raid_namespace) is not int or not 0 <= raid_namespace <= 255:
            raise ValueError('raid namespace must be an explicit u8 actor context')
        # The native current-number cell is populated from protobuf field8.
        # Raid IDs must use the same announced namespace in their high word.
        msg.append(0x40)
        msg.extend(varint(raid_namespace))
    msg.extend(b"\x80\x01")  # field #16, wire type 0 -> object+0x6C
    msg.extend(varint(mode))
    return _u32le(len(msg)) + bytes(msg)


def build_special_channelinfo_payload(plain: bytes | None = None) -> bytes:
    """Build the simple-obfuscated payload for flag0/opcode1.

    The Stage3 receive gate handles ``flag=0/opcode=1`` before the regular
    14-codec path:

    ``0x02BF23D0 -> 0x02754790(buf, len, 0)``

    With seed 0, ``0x02754790`` mutates each byte as
    ``plain = rol(cipher, 6) ^ 0xB5``.  Therefore server-side cipher bytes are
    ``ror(plain ^ 0xB5, 6)``.

    ``USDOF_GAME_OPCODE1_PROFILE`` controls the plaintext profile:

    - ``usdof90-protobuf`` / ``usdof90-channelinfo``: current USDOF 90
      protobuf envelope proven from ``0x01664E20`` / ``0x004ED0B0``.
    - ``minimal``: legacy three-zero probe, retained only for explicit A/B.
    - unset: proven USDOF 90 protobuf profile.  The legacy three-zero probe
      stalls before charselect in the current encrypted runtime.
    - ``s4a12-login-initial``: complete ServerS4A12 initial login notice
      described by :func:`build_login_initial_notice_plaintext`.
    """

    if plain is None:
        env_plain = os.environ.get("USDOF_GAME_CHANNELINFO_PLAIN")
        if env_plain:
            plain = bytes.fromhex("".join(env_plain.split()))
        else:
            profile = _opcode1_profile().lower().replace("_", "-")
            if profile in {
                "usdof90-protobuf",
                "usdof90-channelinfo",
                "usdof90-channelinfo-protobuf",
                "us90-protobuf",
                "protobuf",
            }:
                plain = build_usdof90_channelinfo_protobuf_plaintext()
            elif profile in {"s4a12-login-initial", "s4a12-initial", "login-initial", "initial-login-notice"}:
                plain = build_login_initial_notice_plaintext()
            elif profile in {"minimal", "zero3", "zeros", "legacy"}:
                plain = b"\x00\x00\x00"
            else:
                raise ValueError(f"unknown USDOF_GAME_OPCODE1_PROFILE={profile!r}")
    return bytes(_ror8(b ^ 0xB5, 6) for b in plain)


def handle_decoded_packet(opcode: int, payload: bytes, *, raid_namespace: int | None = None) -> HandlerDecision:
    """Minimal handler registry placeholder.

    P1 deliberately does not implement all 819+ opcodes.  Each new live opcode
    should be added only when it is proven to be on the enter-town path.
    """
    if opcode == 0x0000:
        # Live trace 20260703_040808:
        #   server flag0/opcode0 ENUM_NOTIPACKET_CHECK_CONNECTION
        #   -> client flag1/opcode0 ENUM_CMDPACKET_CHECK_CONNECTION
        #
        # The payload is still codec-protected, but this command is already
        # useful as a sequencing signal.  Continue with the proven
        # flag0/opcode1 ENUM_NOTIPACKET_CHANNELINFO header-only probe, which
        # uses the client's 0x02BF23D0 special path.
        if raid_namespace is None:
            special_payload = build_special_channelinfo_payload()
        else:
            profile = _opcode1_profile().lower().replace('_', '-')
            if (profile not in {'usdof90-protobuf', 'usdof90-channelinfo', 'usdof90-channelinfo-protobuf',
                                'us90-protobuf', 'protobuf'} or os.environ.get('USDOF_GAME_CHANNELINFO_PLAIN')):
                raise ValueError('local raid requires the verified CHANNELINFO protobuf profile without a raw override')
            special_payload = build_special_channelinfo_payload(
                build_usdof90_channelinfo_protobuf_plaintext(raid_namespace=raid_namespace))
        response = build_game_packet(0x0001, special_payload, flag_or_magic=0)
        opcode1_profile = _opcode1_profile()
        return HandlerDecision(
            [response],
            "opcode 0x0000 check-connection observed; replied with flag0/opcode1 "
            f"channelinfo special payload profile={opcode1_profile} encoded_payload={len(special_payload)}",
        )
    if opcode == 0x013E:
        # First observed after the special flag0/opcode1 channelinfo payload:
        #   raw codec body 9c 4f 3b 44 3b 62 f4 1d
        #   idx10 XOR candidate -> 00 01 00 59 a7 2c cf 00
        #
        # The runtime opcode-name table dump has not yet named this command
        # in committed docs, so keep it dump-only.  The next likely server
        # response family is flag0/opcode2 ENUM_NOTIPACKET_USERINFO, but a
        # header-only opcode2 probe was already insufficient; it needs the
        # proper payload and regular Game encode/checksum before being enabled
        # as a handler response.
        piv = parse_piv_plain(payload)
        if piv is not None:
            return HandlerDecision(
                [],
                "opcode 0x013e ENUM_CMDPACKET_PIV observed; "
                f"echo_flag=0x{piv.echoed_flag:02x} "
                f"echo_opcode=0x{piv.echoed_opcode:04x} "
                f"tag={piv.tag_hex} raw={piv.raw.hex(' ')}; "
                "tag is MD5-fold appended by client send hook 0x01924610; "
                "next reverse target is key/blob sync before flag0/opcode2 USERINFO",
            )
        trimmed = payload[:-1] if payload.endswith(b"\x00") else payload
        return HandlerDecision(
            [],
            "opcode 0x013e observed after channelinfo; idx10 plaintext="
            f"{trimmed.hex(' ') or '<empty>'}; next reverse target is exact command name + NOTIPACKET_USERINFO payload/codec",
        )
    if opcode == 0x00D9:
        return HandlerDecision(
            [],
            "opcode 0x00d9 ENUM_CMDPACKET_OVERFLOW_INFO/ACK-like gate observed; "
            "GameServer may attach configured ACK-followup probes such as flag0/opcode2 USERINFO",
        )
    if opcode in ENTER_GAME_FOLLOWUP_OPCODES:
        return HandlerDecision(
            [],
            f"enter-game follow-up C->S opcode=0x{opcode:04x} observed; "
            "no handler/response in first-round state split; captured for IDA/x32dbg-driven town-init work",
        )
    return HandlerDecision([], f"no handler yet for opcode 0x{opcode:04x}; traced for reverse target")
