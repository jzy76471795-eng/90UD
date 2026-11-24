"""C1/0x01DE dungeon scene uid report (副本实现A.md §5.3).

plain: u32 (low 16 used as scene_uid)
reply: C1/0x01DE {0x01}
Does not change run state.
"""
from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no", ""}


def parse_01de_plain(plain: bytes) -> dict[str, Any]:
    raw = bytes(plain or b"")
    out: dict[str, Any] = {
        "raw_hex": raw.hex(" "),
        "raw_len": len(raw),
        "scene_uid": None,
        "layout": "unknown",
    }
    views = [raw]
    if len(raw) >= 7:
        views.append(raw[3:])
    for view in views:
        if len(view) >= 4:
            uid = int.from_bytes(view[:4], "little")
            out["scene_uid"] = uid & 0xFFFF
            out["layout"] = "u32"
            break
        if len(view) >= 2:
            out["scene_uid"] = int.from_bytes(view[:2], "little")
            out["layout"] = "u16"
            break
    return out


class DungeonSceneUid01DEService:
    def __init__(self, server: Any) -> None:
        self.server = server

    def _emit(self, flag: int, opcode: int, body: bytes, mode: str = "codec") -> tuple[bytes, str]:
        mode_norm = (mode or "codec").strip().lower()
        if mode_norm in {"raw", "plain"}:
            return self.server._raw_response(flag, opcode, body), "raw"
        return self.server._codec_response(flag, opcode, body), "codec"

    def handle(self, body: bytes, *, session: Any = None) -> tuple[list[bytes], str]:
        parsed = parse_01de_plain(body)
        handle_on = _env_bool("USDOF_GAME_DUNGEON_SCENE_UID_HANDLE", True)
        base = (
            "C1/01DE SCENE_UID plain_len=%s plain=%s uid=%s handle=%s"
            % (
                parsed["raw_len"],
                parsed["raw_hex"] or "<empty>",
                parsed["scene_uid"],
                int(handle_on),
            )
        )
        if not handle_on:
            return [], base + "; gate=OFF"

        if session is not None and parsed.get("scene_uid") is not None:
            try:
                session.dungeon_scene_uid = int(parsed["scene_uid"]) & 0xFFFF
            except Exception:
                pass

        pkt, mode = self._emit(1, 0x01DE, bytes([0x01]), "codec")
        return [pkt], (
            "%s; ACK/%s; chain_file=game/chains/opcode01de-dungeon_scene_uid.py"
            % (base, mode)
        )
