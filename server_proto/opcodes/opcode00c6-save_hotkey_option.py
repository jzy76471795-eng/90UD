#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x00C6 SAVE_HOTKEY_OPTION（Stage3 热键整表落库）。

Stage3 live 2026-07-22（cid=3 改键）::
  flag=1 opcode=0x00C6 codec_index=2
  plain = u32 text_len + ASCII 行表 ``I{nn}\\t{vk}\\n`` + pad
  例: ``I01\\t2\\nI02\\t0\\n...I99\\t32\\n``（I 为 1-based 槽号）
  无 S2C ACK；改任意快捷键后客户端整表重发。

持久化 owner::
  - 账号级普通热键档：account_settings.hotkey_slots / hotkey_text
  - 账号级 Creator 热键档：account_settings.creator_hotkey_slots / creator_hotkey_text
  - 旧 character_hotkey_slots / character_hotkey_text 仅作为首次迁移来源
  - 进房/初始化回放（**2026-07-22 IDA 钉死 Stage3**）：
    - **权威 S2C**：S0/0x01C7，reader ``sub_164D2F0`` @ ``0x0164D2F0``
      1. ``sub_18790A0``：``sub_2C74A00`` 读 **u8 key_type** → option+150
      2. ``sub_18788D0``：``sub_2BF4C90`` → ``sub_2BF4BF0`` 读
         **u32 text_len LE + text 字节**（CP→wchar），再按 tab/换行解析
         ``I{nn}\\t{vk}`` 行写入键位表
    - body = ``u8 key_type + u32 text_len + ASCII "I01\\t2\\n..."``
    - 2026-07-28 IDA ``sub_1877950`` @ ``0x01877950``：
      ``key_type == 1`` 加载 ``ClientOnly/HotKeySystemForCreator_ver2.co``；
      其它值加载 ``ClientOnly/HotKeySystem_ver2.co``。
    - **禁止**再发 S4A12 ``u8 + u32 + u16[]`` 当 01C7：
      live 会把 u16 blob 当文本解析失败 → UI 仍默认键（DB 已存仍无效）
    - **禁止** S0/0x01C6 当热键（PacketEnum 454 另有 INSERT RANDOM 别名；
      文本体易走错 reader / 曾闪退）
    - C1/00C6 仍是 Stage3 文本表落库（无 key_type 前缀；仅 u32+Ixx）

本模块：
  - 解析 Stage3 文本表（权威）与 S4A12 二进制 u16 blob（兼容落库）
  - 整表覆盖落库；进房 S0/01C7 用 Stage3 文本体
  - 导出 build_hotkey_option_noti_body 供 START delayed 0x01C7
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail

# Stage3 / S4A12 默认 99 槽；未指派键 = 0x86
HOTKEY_SLOT_COUNT = 99
UNASSIGNED_KEY = 0x86
ACCOUNT_SCOPED_SLOT_COUNT = 1

_hotkey_defaults = load_chain("helper-hotkey_defaults.py")

# S4A12 AccountSettings.DefaultHotkeySlots（198B = 99×u16）
DEFAULT_HOTKEY_SLOTS_BLOB = bytes(
    [
        0x02,
        0x00,
        0x00,
        0x00,
        0x03,
        0x00,
        0x01,
        0x00,
        0x4E,
        0x00,
        0x39,
        0x00,
        0x50,
        0x00,
        0x0D,
        0x00,
        0x4C,
        0x00,
        0x43,
        0x00,
        0x41,
        0x00,
        0x3F,
        0x00,
        0x45,
        0x00,
        0x52,
        0x00,
        0x4B,
        0x00,
        0x55,
        0x00,
        0x44,
        0x00,
        0x56,
        0x00,
        0x54,
        0x00,
        0x51,
        0x00,
        0x37,
        0x00,
        0x49,
        0x00,
        0x3A,
        0x00,
        0x3C,
        0x00,
        0x3D,
        0x00,
        0x3E,
        0x00,
        0x47,
        0x00,
        0x4D,
        0x00,
        0x3B,
        0x00,
        0x48,
        0x00,
        0x4A,
        0x00,
        0x4F,
        0x00,
        0x2E,
        0x00,
        0x2F,
        0x00,
        0x30,
        0x00,
        0x31,
        0x00,
        0x32,
        0x00,
        0x33,
        0x00,
        0x86,
        0x00,
        0x1C,
        0x00,
        0x53,
        0x00,
        0x57,
        0x00,
        0x86,
        0x00,
        0x6C,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x1A,
        0x00,
        0x46,
        0x00,
        0x68,
        0x00,
        0x69,
        0x00,
        0x42,
        0x00,
        0x1B,
        0x00,
        0x6A,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x38,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x07,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x59,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x58,
        0x00,
        0x86,
        0x00,
        0x5A,
        0x00,
        0x5B,
        0x00,
        0x5C,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x1F,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x86,
        0x00,
        0x15,
        0x00,
        0x86,
        0x00,
        0x18,
        0x00,
        0x86,
        0x00,
    ]
)

_TEXT_LINE_RE = re.compile(r"I(\d+)\t(-?\d+)\n?", re.MULTILINE)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def default_hotkey_slots() -> list[int]:
    """返回默认 99 槽 u16 列表。"""
    blob = DEFAULT_HOTKEY_SLOTS_BLOB
    return [
        int.from_bytes(blob[i : i + 2], "little")
        for i in range(0, len(blob), 2)
    ]


def default_hotkey_slots_for_job(job: int | None) -> list[int]:
    """Return the Stage3 default table selected by the character job."""
    if _hotkey_defaults.is_creator_job(job):
        return list(_hotkey_defaults.get_creator_hotkey_slots())
    return default_hotkey_slots()


def hotkey_key_type_for_job(job: int | None, account_key_type: int = 0) -> int:
    """Creator owns key type 1; other jobs keep the account preference."""
    if _hotkey_defaults.is_creator_job(job):
        return 1
    return int(account_key_type) & 0xFF


def hotkey_profile_type_for_job(job: int | None) -> int:
    """Map a character to the native Stage3 ordinary/Creator profile."""
    return 1 if _hotkey_defaults.is_creator_job(job) else 0


def slots_to_blob(slots: Sequence[int]) -> bytes:
    out = bytearray()
    for v in slots:
        out.extend((int(v) & 0xFFFF).to_bytes(2, "little"))
    return bytes(out)


def blob_to_slots(blob: bytes | None) -> list[int]:
    data = bytes(blob or b"")
    if not data:
        return []
    # 奇数尾丢弃
    n = len(data) - (len(data) % 2)
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, n, 2)]


def normalize_hotkey_slots(
    slots: Sequence[int] | None,
    *,
    count: int = HOTKEY_SLOT_COUNT,
    fill: int = UNASSIGNED_KEY,
) -> list[int]:
    """裁到固定槽数；缺槽用 fill（默认 0x86 未指派）。"""
    n = max(1, int(count))
    src = list(slots or [])
    out = [int(fill) & 0xFFFF] * n
    for i, v in enumerate(src[:n]):
        out[i] = int(v) & 0xFFFF
    return out


def extract_account_scoped_hotkey_blob(slots: Sequence[int] | bytes | None) -> bytes:
    """账号级只保留前 ACCOUNT_SCOPED_SLOT_COUNT 槽（S4A12 连发前缀）。"""
    if isinstance(slots, (bytes, bytearray, memoryview)):
        vals = blob_to_slots(bytes(slots))
    else:
        vals = list(slots or [])
    if not vals:
        vals = default_hotkey_slots()
    n = min(ACCOUNT_SCOPED_SLOT_COUNT, len(vals))
    return slots_to_blob(vals[:n])


def slots_to_stage3_text(slots: Sequence[int]) -> bytes:
    """槽列表 → Stage3 文本行（仅输出 1..N 全部行，便于 S2C 完整回灌）。"""
    lines: list[str] = []
    for i, v in enumerate(slots):
        lines.append("I%02d\t%d\n" % (i + 1, int(v) & 0xFFFF))
    return "".join(lines).encode("ascii")


def build_stage3_hotkey_text_core(slots: Sequence[int]) -> bytes:
    """C2S/S2C 共用核心：u32 text_len + text。"""
    text = slots_to_stage3_text(slots)
    return len(text).to_bytes(4, "little") + text


def extract_stage3_text_core(plain: bytes) -> bytes | None:
    """从 C1/00C6 plain 裁出 ``u32+text`` 核心（去 codec pad）。"""
    data = bytes(plain or b"")
    if len(data) < 8:
        return None
    text_len = int.from_bytes(data[:4], "little")
    if text_len < 4 or text_len > 8192 or len(data) < 4 + text_len:
        return None
    body = data[4 : 4 + text_len]
    if not (body[:1] == b"I" or b"\t" in body[:64]):
        return None
    return data[: 4 + text_len]


def parse_save_hotkey_00c6(
    plain: bytes,
    *,
    default_slots: Sequence[int] | None = None,
) -> tuple[list[int] | None, bytes | None, str]:
    """解析 C1/00C6 plain → (槽列表, stage3_text_core, note)。

    优先 Stage3 文本表；失败再试 S4A12 二进制 ``u32 len + u16[]`` / 裸 u16[]。
    text_core 非空时进房应原样回放（最稳）。
    """
    data = bytes(plain or b"")
    if len(data) < 4:
        return None, None, "too_short len=%s" % (len(data),)

    # --- Stage3 文本：u32 text_len + "I01\\t2\\n..." ---
    text_len = int.from_bytes(data[:4], "little")
    if 4 <= text_len <= 8192 and (4 + min(text_len, len(data) - 4)) >= 8:
        take = min(text_len, len(data) - 4)
        text_bytes = data[4 : 4 + take]
        # 文本特征：以 I 开头或含 tab 数字行
        if text_bytes[:1] == b"I" or b"\t" in text_bytes[:64]:
            try:
                text = text_bytes.decode("ascii", errors="strict")
            except Exception:
                text = text_bytes.decode("latin1", errors="replace")
            found = {
                int(m.group(1)): int(m.group(2))
                for m in _TEXT_LINE_RE.finditer(text)
            }
            if found:
                max_i = max(found)
                # Stage3 常规 99 槽；若客户端扩槽则跟 max
                count = max(HOTKEY_SLOT_COUNT, max_i)
                # 以默认表为底，再用文本行覆盖。
                # live 常省略部分 Ixx 行（非「未指派」语义），禁止把缺省槽写成 0x86 抹掉默认键。
                base = normalize_hotkey_slots(
                    default_slots if default_slots is not None else default_hotkey_slots(),
                    count=count,
                )
                slots = list(base)
                for idx_1based, val in found.items():
                    if 1 <= idx_1based <= count:
                        if val < 0:
                            slots[idx_1based - 1] = UNASSIGNED_KEY
                        else:
                            slots[idx_1based - 1] = int(val) & 0xFFFF
                changed = sum(
                    1
                    for a, b in zip(slots, base)
                    if a != b
                )
                core = data[: 4 + take]
                return (
                    slots,
                    core,
                    "stage3_text text_len=%s entries=%s slots=%s "
                    "diff_default=%s core=%s"
                    % (text_len, len(found), len(slots), changed, len(core)),
                )

    # --- S4A12 二进制：u32 blob_len + u16 slots ---
    blob_len = int.from_bytes(data[:4], "little")
    if 2 <= blob_len <= 4096 and blob_len % 2 == 0 and len(data) >= 4 + blob_len:
        body = data[4 : 4 + blob_len]
        slots = blob_to_slots(body)
        if slots:
            return (
                slots,
                None,
                "binary_len_prefix blob_len=%s slots=%s"
                % (blob_len, len(slots)),
            )

    # --- 裸 u16 数组（无长度前缀）---
    if len(data) >= 4 and len(data) % 2 == 0:
        slots = blob_to_slots(data)
        if 4 <= len(slots) <= HOTKEY_SLOT_COUNT + 16:
            return slots, None, "binary_raw slots=%s" % (len(slots),)

    return None, None, "unrecognized plain_len=%s head=%s" % (
        len(data),
        data[:8].hex(" "),
    )


def build_hotkey_option_noti_body(
    key_type: int = 0,
    slots: Sequence[int] | bytes | None = None,
    *,
    text_core: bytes | None = None,
    stage3_text: bool = True,
    s4a12_binary: bool = False,
) -> bytes:
    """S0/0x01C7 HOTKEY_OPTION body（进房用）。

    **默认 Stage3**（IDA ``sub_164D2F0`` 2026-07-22）::
      u8 key_type + u32 text_len + ASCII ``I{nn}\\t{vk}\\n`` ...

    ``text_core`` 若已是 ``u32+Ixx`` 则直接前缀 key_type 回放（与 C1/00C6 对称）。

    ``s4a12_binary=True`` 仅对照/实验：``u8 + u32 + u16[]``。
    **禁止**默认进房用二进制——Stage3 会当文本解失败，热键看起来「存不上」。
    """
    if s4a12_binary and not stage3_text:
        if isinstance(slots, (bytes, bytearray, memoryview)):
            hotkeys = bytes(slots)
            if len(hotkeys) % 2:
                hotkeys = hotkeys[: len(hotkeys) - 1]
        elif slots is None:
            hotkeys = slots_to_blob(default_hotkey_slots())
        else:
            hotkeys = slots_to_blob(list(slots))
        return (
            bytes([int(key_type) & 0xFF])
            + len(hotkeys).to_bytes(4, "little")
            + hotkeys
        )

    # Stage3 文本路径（权威）
    core: bytes | None = None
    if text_core:
        cand = bytes(text_core)
        if len(cand) >= 8 and (cand[4:5] == b"I" or b"\t" in cand[4:20]):
            core = cand
    if core is None:
        if isinstance(slots, (bytes, bytearray, memoryview)):
            raw = bytes(slots)
            if len(raw) >= 8 and (raw[4:5] == b"I" or b"\t" in raw[4:20]):
                core = raw
            else:
                slot_list = blob_to_slots(raw)
                core = build_stage3_hotkey_text_core(
                    normalize_hotkey_slots(slot_list)
                )
        elif slots is None:
            core = build_stage3_hotkey_text_core(default_hotkey_slots())
        else:
            core = build_stage3_hotkey_text_core(
                normalize_hotkey_slots(list(slots))
            )
    return bytes([int(key_type) & 0xFF]) + core


def _sample_slot_changes(
    slots: Sequence[int],
    limit: int = 8,
    *,
    default_slots: Sequence[int] | None = None,
) -> str:
    """相对默认表的差异采样，便于日志确认改键。"""
    base = list(default_slots) if default_slots is not None else default_hotkey_slots()
    diffs: list[str] = []
    for i, v in enumerate(slots):
        b = base[i] if i < len(base) else None
        if b != v:
            diffs.append("s%s:%s->%s" % (i, b, v))
            if len(diffs) >= limit:
                break
    return ",".join(diffs) if diffs else "no_diff_default"


class SaveHotkeyOption00C6Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_save_hotkey_option_00c6(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SAVE_HOTKEY_OPTION_00C6", True):
            return [], "C1/00C6 SAVE_HOTKEY_OPTION gate=OFF"

        base = "C1/00C6 SAVE_HOTKEY_OPTION plain_len=%s" % (len(body or b""),)
        cid = None
        character_job = None
        try:
            detail = resolve_selected_character_detail(self.server, session)
            cid = detail.get("character_id")
            character_job = detail.get("job")
        except Exception as exc:
            return [], "%s; resolve_fail=%r" % (base, exc)
        if session is not None and cid is None:
            try:
                cid = getattr(session, "selected_character_id", None)
            except Exception:
                pass
        if session is not None and character_job is None:
            try:
                character_job = getattr(session, "character_job", None)
            except Exception:
                pass
        if not cid:
            return [], "%s; skip=no_cid" % (base,)

        account_id = int(getattr(self.server, "account_id", 1) or 1)
        repo = None
        try:
            if hasattr(self.server, "_repo"):
                repo = self.server._repo()
        except Exception:
            repo = None
        if repo is None:
            return [], "%s; skip=no_repo cid=%s" % (base, cid)

        parse_note = "not_parsed"
        try:
            if hasattr(repo, "ensure_account"):
                repo.ensure_account(
                    account_id,
                    getattr(self.server, "account_m_id", "10038"),
                )
            current_slots, _current_text, migration_note = (
                ensure_account_hotkey_profile(
                    repo,
                    account_id,
                    character_id=int(cid),
                    character_job=character_job,
                )
            )
            job_defaults = default_hotkey_slots_for_job(character_job)
            slots, _wire_text_core, parse_note = parse_save_hotkey_00c6(
                body or b"",
                default_slots=current_slots,
            )
            if slots is None:
                return [], "%s; parse_fail %s" % (base, parse_note)

            if len(slots) < HOTKEY_SLOT_COUNT:
                slots = normalize_hotkey_slots(slots, count=HOTKEY_SLOT_COUNT)
            elif len(slots) > HOTKEY_SLOT_COUNT + 32:
                slots = list(slots[: HOTKEY_SLOT_COUNT + 32])

            # Persist a complete canonical table. Sparse C2S text means
            # "unchanged" for omitted lines, so replaying it alone would lose
            # earlier account edits on another character.
            text_core = build_stage3_hotkey_text_core(slots)
            profile_type = hotkey_profile_type_for_job(character_job)
            ok_acc = bool(
                repo.save_account_hotkey_profile(
                    account_id,
                    profile_type,
                    slots_to_blob(slots),
                    text_core,
                )
            )
            if not ok_acc:
                raise RuntimeError("account hotkey profile write returned false")
            account_key_type = 0
            if hasattr(repo, "get_account_hotkey_key_type"):
                account_key_type = int(
                    repo.get_account_hotkey_key_type(account_id) or 0
                ) & 0xFF
        except Exception as exc:
            return [], "%s; store_fail cid=%s err=%r; %s" % (
                base,
                cid,
                exc,
                parse_note,
            )

        sample = _sample_slot_changes(slots, default_slots=job_defaults)
        return [], (
            "%s; stored ok cid=%s account_id=%s account_profile=%s "
            "job=%s account_write=%s account_key_type=%s "
            "slots=%s text_core=%s "
            "%s migration=[%s] sample_diff=[%s]; "
            "chain_file=opcodes/opcode00c6-save_hotkey_option.py"
            % (
                base,
                int(cid),
                account_id,
                profile_type,
                character_job,
                int(ok_acc),
                account_key_type,
                len(slots),
                len(text_core or b""),
                parse_note,
                migration_note,
                sample,
            )
        )


def _resolve_character_job(
    repo: Any,
    character_id: int,
    account_id: int,
    character_job: int | None,
) -> int | None:
    if character_job is not None:
        return int(character_job) & 0xFF
    if character_id <= 0 or not hasattr(repo, "list_by_account"):
        return None
    try:
        for record in repo.list_by_account(int(account_id)):
            if int(getattr(record, "character_id", 0) or 0) == character_id:
                return int(getattr(record, "job", 0) or 0) & 0xFF
    except Exception:
        return None
    return None


def _profile_character_ids(repo: Any, account_id: int, profile_type: int) -> list[int]:
    if not hasattr(repo, "list_by_account"):
        return []
    out: list[int] = []
    try:
        records = repo.list_by_account(int(account_id))
    except Exception:
        return []
    for record in records:
        job = int(getattr(record, "job", 0) or 0) & 0xFF
        if hotkey_profile_type_for_job(job) != int(profile_type):
            continue
        cid = int(getattr(record, "character_id", 0) or 0)
        if cid > 0:
            out.append(cid)
    return out


def ensure_account_hotkey_profile(
    repo: Any,
    account_id: int,
    *,
    character_id: int = 0,
    character_job: int | None = None,
) -> tuple[list[int], bytes, str]:
    """Resolve or migrate the complete account profile for one native type."""
    aid = int(account_id or 1)
    cid = int(character_id or 0)
    job = _resolve_character_job(repo, cid, aid, character_job)
    profile_type = hotkey_profile_type_for_job(job)
    defaults = default_hotkey_slots_for_job(job)

    stored_profile = None
    if hasattr(repo, "load_account_hotkey_profile"):
        stored_profile = repo.load_account_hotkey_profile(aid, profile_type)
    legacy_prefix: list[int] = []
    if stored_profile is not None:
        slots_blob, stored_text = stored_profile
        saved_slots = blob_to_slots(slots_blob)
        if len(saved_slots) >= HOTKEY_SLOT_COUNT:
            slots = normalize_hotkey_slots(
                saved_slots,
                count=max(HOTKEY_SLOT_COUNT, len(saved_slots)),
            )
            text_core = extract_stage3_text_core(stored_text or b"")
            if text_core is None:
                text_core = build_stage3_hotkey_text_core(slots)
                if not repo.save_account_hotkey_profile(
                    aid,
                    profile_type,
                    slots_to_blob(slots),
                    text_core,
                ):
                    raise RuntimeError("account hotkey text repair returned false")
            return (
                slots,
                text_core,
                "account_settings aid=%s profile=%s" % (aid, profile_type),
            )
        if profile_type == 0:
            legacy_prefix = saved_slots

    candidate_ids = _profile_character_ids(repo, aid, profile_type)
    if cid > 0 and cid not in candidate_ids:
        candidate_ids.append(cid)
    legacy = None
    if hasattr(repo, "load_latest_character_hotkey_profile"):
        legacy = repo.load_latest_character_hotkey_profile(aid, candidate_ids)

    source = "defaults"
    if legacy is not None:
        legacy_cid, legacy_slots, _legacy_text = legacy
        slot_count = max(HOTKEY_SLOT_COUNT, len(legacy_slots))
        slots = normalize_hotkey_slots(
            defaults,
            count=slot_count,
        )
        for index, value in enumerate(legacy_slots[:slot_count]):
            slots[index] = int(value) & 0xFFFF
        source = "legacy_character cid=%s" % int(legacy_cid)
    else:
        slots = normalize_hotkey_slots(defaults, count=HOTKEY_SLOT_COUNT)

    if profile_type == 0 and legacy_prefix:
        for index, value in enumerate(legacy_prefix[: len(slots)]):
            slots[index] = int(value) & 0xFFFF
        source += "+legacy_account_prefix n=%s" % len(legacy_prefix)

    text_core = build_stage3_hotkey_text_core(slots)
    if not hasattr(repo, "save_account_hotkey_profile") or not bool(
        repo.save_account_hotkey_profile(
            aid,
            profile_type,
            slots_to_blob(slots),
            text_core,
        )
    ):
        raise RuntimeError("account hotkey profile migration returned false")
    return (
        slots,
        text_core,
        "%s -> account_settings aid=%s profile=%s" % (
            source,
            aid,
            profile_type,
        ),
    )


def resolve_hotkey_for_character(
    repo: Any,
    character_id: int,
    *,
    account_id: int = 1,
    character_job: int | None = None,
) -> tuple[list[int], int, bytes, str]:
    """Read account hotkeys; return (slots, key_type, s2c_body, src_note).

    s2c_body = Stage3 ``u8 key_type + u32 + Ixx 文本``（给 S0/0x01C7）。
    Creator（job 10）固定 key_type=1；其它职业使用账号 key type。
    Ordinary characters share one account profile. Creator characters share a
    separate account profile because Stage3 selects a different native table.
    """
    cid = int(character_id or 0)
    aid = int(account_id or 1)
    job = _resolve_character_job(repo, cid, aid, character_job)
    account_key_type = 0
    if hasattr(repo, "get_account_hotkey_key_type"):
        try:
            account_key_type = int(repo.get_account_hotkey_key_type(aid) or 0) & 0xFF
        except Exception:
            account_key_type = 0
    key_type = hotkey_key_type_for_job(job, account_key_type)
    slots, text_core, source = ensure_account_hotkey_profile(
        repo,
        aid,
        character_id=cid,
        character_job=job,
    )
    body = build_hotkey_option_noti_body(
        key_type,
        slots,
        text_core=text_core,
        stage3_text=True,
    )
    return (
        slots,
        key_type,
        body,
        "%s job=%s n=%s text=1 body=%s" % (
            source,
            job,
            len(slots),
            len(body),
        ),
    )


def resolve_hotkey_slots_for_character(
    repo: Any,
    character_id: int,
    *,
    account_id: int = 1,
    character_job: int | None = None,
) -> tuple[list[int], int, str]:
    """兼容旧接口：只返回 slots。"""
    slots, key_type, _body, src = resolve_hotkey_for_character(
        repo,
        character_id,
        account_id=account_id,
        character_job=character_job,
    )
    return slots, key_type, src
