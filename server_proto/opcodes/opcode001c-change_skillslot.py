"""C->S 0x001C CHANGE_SKILLSLOT：技能栏/技能页槽位对调。

Stage3 live (game_server_20260719_004326.log / live_trace 00:48:13, 00:49:25):
  flag=1 opcode=0x001C codec_index=0 (XTEA-ECB seed0)
  plain prefix:
    00 00 01 ff ff ff ff 00 dd b4 e9 92 ...  → page=0 slot1=0 slot2=1
    00 01 03 ff ff ff ff 00 87 b3 62 47 ...  → page=0 slot1=1 slot2=3
  前 3 字节语义与 S4A12 SkillHandler.Handle_CHANGE_SKILLSLOT 一致：
    u8 page (0 或 1), u8 slot1, u8 slot2
  后续字节为客户端 padding / 校验尾，服务端不依赖。

S->C ACK（S4A12 对照）:
  flag=1 opcode=0x001C body = {0x01, page, slot1, slot2}
  注意 flag=1，避免与 S0/0x001C DUNGEON_INFO 混淆。

持久化：character_skills 三步交换 slot（S4A12 SwapSkillSlot）；
进房 SKILLINFO 从 DB 读 slot 还原技能栏位置。
"""
from __future__ import annotations

import os
from typing import Any

from game.chain_loader import load_chain
from game.quest.quest_session import resolve_selected_character_detail

_skills_helper = load_chain("helper-character_skills.py")
persist_skill_slot_swap = _skills_helper.persist_skill_slot_swap


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def parse_change_skillslot_body(body: bytes) -> dict[str, int] | None:
    """解析 C1/001C 主体；至少 3 字节。"""
    data = bytes(body or b"")
    if len(data) < 3:
        return None
    page_raw = int(data[0]) & 0xFF
    page = 1 if page_raw == 1 else 0
    slot1 = int(data[1]) & 0xFF
    slot2 = int(data[2]) & 0xFF
    return {
        "page": page,
        "page_raw": page_raw,
        "slot1": slot1,
        "slot2": slot2,
        "raw_len": len(data),
    }


def build_change_skillslot_ack_ok(*, page: int, slot1: int, slot2: int) -> bytes:
    """S1/001C 成功 ACK：success + 回显 page/slot1/slot2。"""
    return bytes(
        [
            0x01,
            int(page) & 0xFF,
            int(slot1) & 0xFF,
            int(slot2) & 0xFF,
        ]
    )


def build_change_skillslot_ack_fail() -> bytes:
    """失败 ACK：success=0（客户端应回滚拖放）。"""
    return b"\x00"


class ChangeSkillslot001CService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def handle_change_skillslot_001c(
        self,
        body: bytes,
        *,
        session: Any = None,
    ) -> tuple[list[bytes], str]:
        handle_on = _env_bool("USDOF_GAME_HANDLE_CHANGE_SKILLSLOT_001C", True)
        persist_on = _env_bool("USDOF_GAME_SKILLS_PERSIST", True)
        parsed = parse_change_skillslot_body(body)
        base = "C1/001C CHANGE_SKILLSLOT plain_len=%s" % (len(body or b""),)
        if not handle_on:
            return [], base + "; gate=OFF"
        if parsed is None:
            packet = self.server._codec_response(
                1, 0x001C, build_change_skillslot_ack_fail()
            )
            return [packet], base + "; reject=short_body; sent fail status=00 wire=%s" % (
                len(packet),
            )

        page = int(parsed["page"])
        slot1 = int(parsed["slot1"])
        slot2 = int(parsed["slot2"])
        ack = build_change_skillslot_ack_ok(page=page, slot1=slot1, slot2=slot2)
        packet = self.server._codec_response(1, 0x001C, ack)

        # 会话级记录（调试 / 后续 A/B）
        if session is not None and hasattr(session, "__dict__"):
            history = getattr(session, "skillslot_swaps", None)
            if not isinstance(history, list):
                history = []
                setattr(session, "skillslot_swaps", history)
            history.append({"page": page, "slot1": slot1, "slot2": slot2})

        persist_note = "persist=off"
        if persist_on:
            cid = None
            job = 0
            try:
                detail = resolve_selected_character_detail(self.server, session)
                cid = detail.get("character_id")
                job = int(detail.get("job") or 0)
            except Exception as resolve_exc:
                persist_note = "persist=skip resolve_fail=%r" % (resolve_exc,)
                detail = None
            if session is not None:
                try:
                    if cid is None:
                        cid = getattr(session, "selected_character_id", None)
                    job = int(getattr(session, "character_job", job) or job)
                except Exception:
                    pass
            repo = None
            try:
                if hasattr(self.server, "_repo"):
                    repo = self.server._repo()
            except Exception:
                repo = None
            if cid and repo is not None:
                _ok, persist_note = persist_skill_slot_swap(
                    repo,
                    int(cid),
                    page,
                    slot1,
                    slot2,
                    job=job,
                )
            elif detail is not None or cid is None:
                persist_note = "persist=skip no_cid_or_repo cid=%r" % (cid,)

        same = "same_slot" if slot1 == slot2 else "swap"
        return [packet], (
            "%s; page=%s slot1=%s slot2=%s %s; ACK ok body=%s wire=%s; %s"
            % (
                base,
                page,
                slot1,
                slot2,
                same,
                ack.hex(" "),
                len(packet),
                persist_note,
            )
        )
