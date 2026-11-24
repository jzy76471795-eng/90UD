from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def diagnostic_trace_enabled() -> bool:
    raw = os.environ.get("USDOF_GAME_DIAGNOSTIC_TRACE", "0")
    return raw.strip().lower() not in {"0", "false", "no", "off", "n", ""}


class DiscardBinaryCapture:
    def __enter__(self) -> "DiscardBinaryCapture":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None


def open_stream_capture(path: Path) -> Any:
    if diagnostic_trace_enabled():
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    return DiscardBinaryCapture()
