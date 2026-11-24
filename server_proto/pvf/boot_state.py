"""Process-local PVF boot state for lazy startup."""
from __future__ import annotations

import threading
from typing import Optional


_READY = threading.Event()
_LOCK = threading.RLock()
_ERROR = ""


def reset() -> None:
    global _ERROR
    with _LOCK:
        _ERROR = ""
        _READY.clear()


def mark_ready() -> None:
    global _ERROR
    with _LOCK:
        _ERROR = ""
        _READY.set()


def mark_failed(error: str) -> None:
    global _ERROR
    with _LOCK:
        _ERROR = str(error or "PVF warmup failed")
        _READY.set()


def error() -> str:
    with _LOCK:
        return str(_ERROR)


def is_ready() -> bool:
    return _READY.is_set() and not error()


def wait_ready(timeout: Optional[float] = None) -> bool:
    if timeout is None:
        _READY.wait()
    else:
        _READY.wait(max(0.0, float(timeout)))
    return is_ready()


__all__ = ["error", "is_ready", "mark_failed", "mark_ready", "reset", "wait_ready"]
