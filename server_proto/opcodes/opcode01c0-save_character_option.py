#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x01C0 SAVE_CHARACTER_OPTION：角色选项大包（含技能锁定位图）。

Stage3 live 2026-07-22（CCC / cid=3）::
  flag=1 opcode=0x01C0 codec_index=0 (XTEA-ECB seed0)
  payload_len=0x20D (525B)
  plain 前缀: ``00 02 00 00 00 00 00 00`` + 大量 ``ff``；
  锁定变更体现为中间 u16 非 0xffff 槽位（两次包仅 2 字节差）。

语义（对照 S4A12 SettingsHandler.Handle_SAVE_CHARACTER_OPTION）::
  - 无 S2C ACK；整包 body 按角色落库。
  - 进房还原：S0/0x0187 CHARACTER_OPTION 回放同一 blob
    （S4A12 CharacterOptionBodyBuilder / PacketEnum NOTI 390）。

服务端：写 ``character_init_flags.character_option_blob``；不猜字段细分。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail

# Stage3 live 主块长度（两次锁定操作均为 525B）
EXPECTED_OPTION_LEN = 525


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def normalize_character_option_blob(plain: bytes) -> tuple[bytes | None, str]:
    """裁剪/校验 C1/01C0 plain；过短拒绝，过长截到 EXPECTED 或保留全量。"""
    data = bytes(plain or b"")
    if len(data) < 8:
        return None, "too_short len=%s" % (len(data),)
    # 允许略长（codec pad）；权威长度优先 525，否则原样存有效前缀
    if len(data) >= EXPECTED_OPTION_LEN:
        stored = data[:EXPECTED_OPTION_LEN]
        note = "stored=%s trimmed_from=%s expect=%s" % (
            len(stored),
            len(data),
            EXPECTED_OPTION_LEN,
        )
    else:
        stored = data
        note = "stored=%s short_of_expect=%s" % (len(stored), EXPECTED_OPTION_LEN)
    return stored, note


def build_character_option_noti_body(blob: bytes | None) -> bytes:
    """S0/0187 body：有存档则原样；无则 u32 len=0（S4A12 空位图）。"""
    if blob and len(blob) > 0:
        return bytes(blob)
    return (0).to_bytes(4, "little")


class SaveCharacterOption01C0Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_save_character_option_01c0(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SAVE_CHARACTER_OPTION_01C0", True):
            return [], "C1/01C0 SAVE_CHARACTER_OPTION gate=OFF"

        stored, parse_note = normalize_character_option_blob(body or b"")
        base = "C1/01C0 SAVE_CHARACTER_OPTION plain_len=%s" % (len(body or b""),)
        if stored is None:
            return [], "%s; parse_fail %s" % (base, parse_note)

        cid = None
        try:
            detail = resolve_selected_character_detail(self.server, session)
            cid = detail.get("character_id")
        except Exception as exc:
            return [], "%s; resolve_fail=%r; %s" % (base, exc, parse_note)
        if session is not None and cid is None:
            try:
                cid = getattr(session, "selected_character_id", None)
            except Exception:
                pass
        if not cid:
            return [], "%s; skip=no_cid; %s" % (base, parse_note)

        repo = None
        try:
            if hasattr(self.server, "_repo"):
                repo = self.server._repo()
        except Exception:
            repo = None
        if repo is None:
            return [], "%s; skip=no_repo cid=%s; %s" % (base, cid, parse_note)

        try:
            ok = bool(repo.save_character_option_blob(int(cid), stored))
        except Exception as exc:
            return [], "%s; store_fail cid=%s err=%r; %s" % (
                base,
                cid,
                exc,
                parse_note,
            )

        # 采样：前 2 字节 + 非 0/0xff 槽位计数（锁定位图稀疏）
        non_empty = sum(1 for b in stored if b not in (0, 0xFF))
        head = stored[:8].hex(" ")
        return [], (
            "%s; stored ok=%s cid=%s %s non_empty_bytes=%s head=%s; "
            "chain_file=opcodes/opcode01c0-save_character_option.py"
            % (base, int(ok), int(cid), parse_note, non_empty, head)
        )
