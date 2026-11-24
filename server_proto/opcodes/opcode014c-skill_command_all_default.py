#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C->S 0x014C SKILL_COMMAND_ALL_DEFAULT：全部技能释放命令恢复默认。

S4A12: ACK ``{0x01}`` + ClearAllSkillCommands（extra_values=NULL）。
"""
from __future__ import annotations

import os
from typing import Any

from game.quest.quest_session import resolve_selected_character_detail


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


class SkillCommandAllDefault014CService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_skill_command_all_default_014c(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_HANDLE_SKILL_COMMAND_014C", True):
            return [], "C1/014C SKILL_COMMAND_ALL_DEFAULT gate=OFF"

        pkt = self.server._codec_response(1, 0x014C, b"\x01")
        base = "C1/014C SKILL_COMMAND_ALL_DEFAULT plain_len=%s" % (
            len(body or b""),
        )

        persist_note = "persist=off"
        if _env_bool("USDOF_GAME_SKILL_COMMAND_PERSIST", True):
            cid = None
            try:
                detail = resolve_selected_character_detail(self.server, session)
                cid = detail.get("character_id")
            except Exception as exc:
                persist_note = "persist=skip resolve_fail=%r" % (exc,)
            if session is not None and cid is None:
                try:
                    cid = getattr(session, "selected_character_id", None)
                except Exception:
                    pass
            repo = None
            try:
                if hasattr(self.server, "_repo"):
                    repo = self.server._repo()
            except Exception:
                repo = None
            if cid and repo is not None:
                try:
                    n = int(repo.clear_all_character_skill_commands(int(cid)) or 0)
                    persist_note = "persist ok cid=%s cleared=%s" % (int(cid), n)
                except Exception as exc:
                    persist_note = "persist_fail cid=%s err=%r" % (cid, exc)
            else:
                persist_note = "persist=skip no_cid_or_repo cid=%r" % (cid,)

        return [pkt], (
            "%s; ACK ok wire=%s; %s; "
            "chain_file=opcodes/opcode014c-skill_command_all_default.py"
            % (base, len(pkt), persist_note)
        )
