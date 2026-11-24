"""Structured timing for the server boot path."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


_WRITE_LOCK = threading.Lock()


class ServerStartupMetrics:
    """Append monotonic startup events without importing business domains."""

    def __init__(
        self,
        server_root: Path,
        *,
        process_entry_ns: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> None:
        now_ns = time.perf_counter_ns()
        try:
            started_ns = int(process_entry_ns if process_entry_ns is not None else now_ns)
        except (TypeError, ValueError):
            started_ns = now_ns
        self.started_ns = min(started_ns, now_ns)
        self.run_id = str(run_id or "server-%d-%d" % (os.getpid(), time.time_ns()))
        self.path = Path(server_root).resolve() / "log" / "startup_timing.jsonl"

    def record_at(
        self,
        event: str,
        event_ns: int,
        **fields: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "event": str(event),
            "elapsed_ms": round(
                max(0, int(event_ns) - self.started_ns) / 1_000_000.0,
                3,
            ),
            "run_id": self.run_id,
            "pid": os.getpid(),
        }
        payload.update(fields)
        line = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ) + "\n"
        try:
            with _WRITE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
        except OSError:
            # Timing output must never change whether the server can start.
            pass
        return payload

    def record(self, event: str, **fields: Any) -> Dict[str, Any]:
        return self.record_at(event, time.perf_counter_ns(), **fields)


__all__ = ["ServerStartupMetrics"]
