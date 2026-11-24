"""Shared control-plane contracts used by HTTP and Game threads."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class ControlApiError(Exception):
    """A stable API error that can cross the control/Game thread boundary."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


@dataclass
class QueuedControlCommand:
    """One authenticated command waiting for the active Game connection."""

    request_id: str
    command: str
    params: dict[str, Any]
    done: Any = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: ControlApiError | None = None
    cancelled: bool = False


__all__ = ["ControlApiError", "QueuedControlCommand"]
