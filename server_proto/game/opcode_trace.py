from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config import LOG_DIR, ensure_dirs
from game.game_wire import GamePacketHeader, describe_game_header
from game.game_codec import DecodeResult
from game.diagnostic_io import diagnostic_trace_enabled


# 运行时产物，落 logs/ 不进 docs baseline
TRACE_PATH = LOG_DIR / "game_opcode_live_trace.md"


def ensure_trace_file() -> None:
    if not diagnostic_trace_enabled():
        return
    ensure_dirs()
    if not TRACE_PATH.exists():
        TRACE_PATH.write_text(
            "# Game opcode live trace\n\n"
            "记录 live/raw Game packet、header 解析、codec_index、decode 状态和下一步 handler 决策。\n\n",
            encoding="utf-8",
        )


def append_trace(
    *,
    raw_hex: str,
    header: GamePacketHeader | None,
    decode: DecodeResult | None,
    probable_handler: str = "unknown",
    decision: str = "dump-only",
    client_next: str = "pending live observation",
    capture_path: Path | None = None,
) -> None:
    if not diagnostic_trace_enabled():
        return
    ensure_trace_file()
    now = datetime.now().isoformat(timespec="seconds")
    lines = [f"## {now}\n"]
    if capture_path:
        lines.append(f"- capture: `{capture_path}`")
    lines.append(f"- raw packet hex: `{raw_hex}`")
    if header is None:
        lines.append("- decoded header: failed/insufficient")
    else:
        lines.append(f"- decoded header: `{describe_game_header(header)}`")
        lines.append(f"- opcode: `0x{header.opcode:04x}`")
        lines.append(f"- codec_index: `{header.codec_index}`")
    if decode is None:
        lines.append("- decode status: not attempted")
    else:
        lines.append(f"- decode status: ok={decode.ok} note={decode.note}")
        if decode.plain:
            lines.append(f"- decoded/plain prefix: `{decode.plain[:64].hex(' ')}`")
    lines.append(f"- probable handler meaning: {probable_handler}")
    lines.append(f"- server response decision: {decision}")
    lines.append(f"- client next behavior: {client_next}\n")
    with TRACE_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
