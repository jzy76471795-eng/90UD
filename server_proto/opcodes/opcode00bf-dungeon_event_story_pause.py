"""C1/0x00BF DUNGEON_EVENT_STORY_PAUSE → S0/0x00AA echo.

Stage3 / PacketEnum:
  ENUM_CMDPACKET_DUNGEON_EVENT_STORY_PAUSE = 191 (0x00BF)
  ENUM_NOTIPACKET_DUNGEON_EVENT_STORY_PAUSE = 170 (0x00AA)

对照（只读）：
  - 90qiyuan ``dungeon_story_pause.go``：C2S 2B → S0/0x00AA 4B
  - S4A12 ``DungeonTutorialHandler.HandleStoryPause``：同形
  - Live 2026-07-21 特殊本「遗忘之森」2009：进图后连发 C1/00BF，
    无 handler 时剧情对话状态机不闭合 → **对话无头像**

Wire（current EXE / 90qiyuan current_exe_sub_1D3A650）：
  C1/00BF plain:
    u8 pauseFlag    # 0/1
    u8 requestType  # 0..2
    (+ AES pad 到 16B 常见；语义仍取前 2B)
  S0/00AA plain:
    u16 actorObjectKey  # 本地角色 key（character_id / USERINFO key）
    u8  pauseFlag
    u8  requestType

C2S 加密问题（2026-07-21 实测，2026-07-24 已迁移）：
  codec9 seed0 解 00BF 得乱码（pause/type 越界）→ domain_mismatch → 不回 00AA。
  现由唯一 owner ``usdof_codec_passthrough`` 在全局 encode 边界统一提供明文，
  服务端同会话使用 plain-wire；功能 DLL 不再维护 opcode 白名单。

本 handler **只**闭合剧情 pause 交换，不改房间/清房/任务/结算。
"""
from __future__ import annotations

import os
import struct
from typing import Any, Optional


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def parse_00bf_plain(plain: bytes) -> dict[str, Any]:
    """解析 C1/0x00BF 明文（语义 2B；允许 pad / 紧凑头后 body）。"""
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" ") if raw else "",
        "raw_len": len(raw),
        "layout": "unknown",
        "pause_flag": None,
        "request_type": None,
        "valid": False,
    }
    if len(raw) < 2:
        out["layout"] = "too_short"
        return out
    pause_flag = int(raw[0]) & 0xFF
    request_type = int(raw[1]) & 0xFF
    out["pause_flag"] = pause_flag
    out["request_type"] = request_type
    out["layout"] = "u8_pause+u8_type"
    # 90qiyuan domain：stateFlag∈{0,1} requestType∈{0,1,2}
    if pause_flag <= 1 and request_type <= 2:
        out["valid"] = True
        # 尾部若全是 pad 零，记一下便于日志
        if len(raw) > 2 and all(b == 0 for b in raw[2:]):
            out["layout"] = "u8_pause+u8_type+zero_pad"
    return out


def try_raw_plain_00bf(codec_body: bytes) -> Optional[bytes]:
    """DLL 强制明文时：线上体已是 2B 语义（可带 pad），勿再 seed0 解密。

    返回用于 handler 的 plain；无法识别则 None（调用方走 codec）。
    与 001A/0191 的 raw-plain 门闩同模式。
    """
    raw = bytes(codec_body or b"")
    if not raw:
        return None
    # 紧凑 13B 头 + 2B body：codec_body 即 2B
    # 16B 视图：reserved(3)+payload 可能把 2B 明文夹在前缀后 — 试前 2B / 跳过 3B
    candidates = [raw]
    if len(raw) >= 5:
        candidates.append(raw[3:])  # 去掉可能的 reserved 前缀
    for cand in candidates:
        parsed = parse_00bf_plain(cand)
        if parsed["valid"]:
            # 只回语义 2B，handler 不关心 pad
            return bytes([int(parsed["pause_flag"]), int(parsed["request_type"])])
    return None


def build_story_pause_00aa(
    actor_object_key: int,
    pause_flag: int,
    request_type: int,
) -> bytes:
    """S0/0x00AA body：u16 actor + u8 pause + u8 type。"""
    return struct.pack(
        "<HBB",
        int(actor_object_key) & 0xFFFF,
        int(pause_flag) & 0xFF,
        int(request_type) & 0xFF,
    )


def _resolve_actor_key(session: Any) -> int:
    """本地 actor key：优先 character_id（与 START n2_8 / USERINFO0 key 一致）。"""
    if session is None:
        return 0
    for attr in (
        "selected_character_id",
        "character_id",
        "primary_user_key",
        "user_id",
        "actor_object_key",
    ):
        val = getattr(session, attr, None)
        if val is not None:
            try:
                n = int(val)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n & 0xFFFF
    return 0


class DungeonEventStoryPause00BFService:
    """C1/0x00BF → S0/0x00AA 剧情暂停闭合。"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_GAME_DUNGEON_STORY_PAUSE_00BF", True):
            return [], "C1/00BF STORY_PAUSE gate=OFF"

        parsed = parse_00bf_plain(body)
        in_run = bool(getattr(session, "in_dungeon_run", False)) if session is not None else False
        actor_key = _resolve_actor_key(session)
        dungeon_id = (
            getattr(session, "selected_dungeon_id", None) if session is not None else None
        )
        map_index = (
            getattr(session, "dungeon_map_index", None) if session is not None else None
        )

        base_note = (
            "C1/00BF STORY_PAUSE plain_len=%d plain=%s layout=%s "
            "pause=%s type=%s valid=%s cid=%s in_run=%s dungeon_id=%s map_index=%s"
            % (
                parsed["raw_len"],
                parsed["raw_hex"][:48],
                parsed["layout"],
                parsed["pause_flag"],
                parsed["request_type"],
                int(bool(parsed["valid"])),
                actor_key,
                int(in_run),
                dungeon_id,
                map_index,
            )
        )

        if not parsed["valid"]:
            return [], base_note + "; reject=domain_mismatch; no_00aa"

        # 无角色 / 非副本：仍允许 echo（S4 不强制 in_run），但无 key 则无法组 00AA
        if actor_key <= 0:
            return [], base_note + "; reject=no_actor_key; no_00aa"

        # 可选：仅副本内回（默认开；关逃可在城镇也 echo）
        require_run = _env_bool("USDOF_GAME_DUNGEON_STORY_PAUSE_REQUIRE_RUN", True)
        if require_run and not in_run:
            return [], base_note + "; reject=not_in_dungeon_run; no_00aa"

        pause_flag = int(parsed["pause_flag"])
        request_type = int(parsed["request_type"])
        body_aa = build_story_pause_00aa(actor_key, pause_flag, request_type)
        pkt, mode = self._emit(0, 0x00AA, body_aa, "codec")

        # 会话记最后一次 pause（便于日志 / 后续剧情状态）
        if session is not None:
            try:
                session.dungeon_story_pause_flag = pause_flag
                session.dungeon_story_pause_type = request_type
            except Exception:
                pass

        note = (
            base_note
            + "; reply=[S0/00AA mode=%s plain_len=%d actor=0x%04x pause=%d type=%d]; "
            "doc_ref=90qiyuan dungeon_story_pause + S4A12 HandleStoryPause; "
            "chain_file=opcodes/opcode00bf-dungeon_event_story_pause.py"
            % (mode, len(body_aa), actor_key, pause_flag, request_type)
        )
        return [pkt], note
