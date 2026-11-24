# -*- coding: utf-8 -*-
"""角色名查重 (C->S 0x02B5)

Stage3 S->C flag1/0x02B5 handler is sub_15FDE00(packet_ctx, success_bool, status_u8)
registered via flag1 registry 0x0160877C (NOT flag0 NOTI 0x01044800).

Wire plaintext (codec7, zero-pad to 8):
  u8 success_bool + u8 status

Handler branches (IDA 0x015FDE00):
  success_bool != 0:
    sub_278B220(ui, 602, msg 33193, 0)   # name available prompt
  success_bool == 0:
    status 0x02:
      sub_2C64AC0(0, strA, strB, 2422, msg 33194)
      # depends on dialog table dword_42D43C4[0]; if slot empty => silent no-op
    status 0x14 / 0x18:
      sub_278B220(ui, 602, msg 33195, 0)  # same prompt path as success
    status 0x9F:
      sub_278B220(ui, 602, msg 33196, 0)
    status 0xA0:
      sub_278B220(ui, 602, msg 90003, 0)
    other:
      return (silent)

Live observation 2026-07-13: codec7 body 00 02 is accepted (client PIV 0x02B5)
but UI shows nothing. That matches case-2 dialog table miss, not "no reply".

Default fail status for duplicate is therefore 0x14 (278B220 path) so the
create page always gets a visible prompt. Override with:
  USDOF_CHECK_NAME_DUP_STATUS=0x02   # classic name-taken code (may be silent)
  USDOF_CHECK_NAME_DUP_STATUS=0x9F
  USDOF_CHECK_NAME_DUP_STATUS=0xA0
"""
from __future__ import annotations

import os
from typing import Any

from game.character.character_db import parse_check_name_body


def _dup_status() -> int:
    raw = os.environ.get("USDOF_CHECK_NAME_DUP_STATUS", "0x14").strip()
    try:
        return int(raw, 0) & 0xFF
    except ValueError:
        return 0x14


class CharCheckNameService:
    """角色名查重 (C->S 0x02B5)"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_check_name(self, body: bytes) -> tuple[list[bytes], str]:
        if body is None or len(body) < 5:
            packet = self.server._codec_response(1, 0x02B5, bytes([0x00, 0x02]))
            return [packet], (
                f"CHECK_NAME invalid-body plain_len={0 if body is None else len(body)}; "
                f"sent status=00 02 wire={len(packet)}"
            )

        parsed = parse_check_name_body(body)
        if parsed is None:
            packet = self.server._codec_response(1, 0x02B5, bytes([0x00, 0x14]))
            return [packet], (
                f"CHECK_NAME unparseable plain_len={len(body)}; "
                f"sent status=00 14 wire={len(packet)}"
            )

        name, _name_raw, source = parsed
        repo = self.server._repo()
        from game.character.character_rename import is_name_reserved
        if repo.get_by_name(name) is not None or is_name_reserved(repo.db_path, name):
            st = _dup_status()
            packet = self.server._codec_response(1, 0x02B5, bytes([0x00, st]))
            return [packet], (
                f"CHECK_NAME duplicate name={name!r} source={source}; "
                f"sent status=00 {st:02x} wire={len(packet)} "
                f"(dup_status_env={os.environ.get('USDOF_CHECK_NAME_DUP_STATUS', '0x14')})"
            )

        packet = self.server._codec_response(1, 0x02B5, bytes([0x01, 0x00]))
        return [packet], (
            f"CHECK_NAME available name={name!r} source={source}; "
            f"sent success status=01 00 wire={len(packet)}"
        )
