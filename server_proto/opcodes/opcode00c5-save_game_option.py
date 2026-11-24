#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x00C5 SAVE_GAME_OPTION: account-level 79-option blob persist.

Stage3 live body plain:
  u32 primary_len (=158)
  79 x u16 options
  trailing pad (often 6 zero bytes)

Store main 158B only. No ACK required by live client.
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
OPTION_COUNT = load_chain('helper-00ad_defaults.py').OPTION_COUNT
PRIMARY_LEN = load_chain('helper-00ad_defaults.py').PRIMARY_LEN
_avatar_visibility = load_chain("helper-avatar_visibility.py")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def parse_save_game_option_main(plain: bytes) -> tuple[bytes | None, str]:
    if not plain or len(plain) < 4:
        return None, f"too_short len={len(plain) if plain else 0}"
    primary_len = int.from_bytes(plain[:4], "little")
    if primary_len <= 0:
        return None, f"primary_len={primary_len}"
    if len(plain) < 4 + primary_len:
        return None, f"truncated primary_len={primary_len} have={len(plain) - 4}"
    if primary_len > 512:
        return None, f"primary_len_too_large={primary_len}"
    main = bytes(plain[4 : 4 + primary_len])
    return main, f"primary_len={primary_len} stored={len(main)}"


class SaveGameOption00C5Service:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_save_game_option_00c5(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_SAVE_GAME_OPTION_00C5", True):
            return [], "SAVE_GAME_OPTION 0x00C5 ignored (USDOF_GAME_SAVE_GAME_OPTION_00C5=0)"

        main, parse_note = parse_save_game_option_main(body or b"")
        if main is None:
            return [], f"SAVE_GAME_OPTION 0x00C5 parse_fail {parse_note}"

        account_id = int(getattr(self.server, "account_id", 1) or 1)
        character_id = int(getattr(session, "selected_character_id", 0) or 0)
        set_mask, clear_mask = _avatar_visibility.visibility_update_masks(main)
        repo = self.server._repo()
        try:
            repo.ensure_account(account_id, getattr(self.server, "account_m_id", "10038"))
            old_visibility, new_visibility = (
                repo.save_main_game_option_and_character_visibility(
                    account_id,
                    main,
                    character_id=character_id if character_id > 0 else None,
                    visibility_set_mask=set_mask,
                    visibility_clear_mask=clear_mask,
                )
            )
        except Exception as exc:
            return [], (
                f"SAVE_GAME_OPTION 0x00C5 store_fail account_id={account_id} "
                f"{parse_note} err={exc!r}"
            )

        packets: list[bytes] = []
        visibility_note = "visibility=no_selected_character"
        if (
            character_id > 0
            and old_visibility is not None
            and new_visibility is not None
        ):
            visibility_note = (
                f"visibility=0x{old_visibility:02x}->0x{new_visibility:02x}"
            )
            if new_visibility != old_visibility:
                visibility_body = _avatar_visibility.build_avatar_visibility_body(
                    character_id,
                    new_visibility,
                )
                packets.append(
                    self.server._codec_response(0, 0x0165, visibility_body)
                )
                visibility_note += " notify=S0/0165"
            else:
                visibility_note += " notify=unchanged"

        sample = []
        for idx in (0, 1, 3, 4, 18, 22, 41, 55, 74):
            if len(main) >= (idx + 1) * 2:
                val = int.from_bytes(main[idx * 2 : idx * 2 + 2], "little")
                sample.append(f"opt[{idx}]={val}")
        sample_s = " ".join(sample) if sample else "no_u16"
        return packets, (
            "SAVE_GAME_OPTION 0x00C5 stored "
            f"account_id={account_id} {parse_note} option_slots~={len(main) // 2} "
            f"expect_stage3={PRIMARY_LEN}B/{OPTION_COUNT} {sample_s} "
            f"{visibility_note}; chain_file=opcodes/opcode00c5-save_game_option.py"
        )
