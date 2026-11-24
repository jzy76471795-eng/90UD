# -*- coding: utf-8 -*-
"""Stable short-timestamp formatting for USDOF startup milestones."""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional


_SHORT_TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\](?:\s|$)")
_STARTUP_FOOTER_MESSAGES = (
    "GeoIP Allow Country Code : CN",
    "GeoIP Allow Country Code : HK",
    "GeoIP Allow Country Code : KR",
    "GeoIP Allow Country Code : MO",
    "GeoIP Allow Country Code : TW",
    "[!] Monitor Server Connected",
    "[!] Guild Server Connected",
    "[!] Connect To Guild Server ...",
    "[!] Connect To Monitor Server ...",
)


def _short_timestamp(when: Optional[datetime] = None) -> str:
    return (when or datetime.now()).strftime("%H:%M:%S")


def _single_line(value: object) -> str:
    return " ".join(str(value).split())


def has_short_timestamp(message: object) -> bool:
    return bool(_SHORT_TIMESTAMP_RE.match(str(message)))


def ensure_short_timestamp(message: object, when: Optional[datetime] = None) -> str:
    text = str(message)
    if has_short_timestamp(text):
        return text
    return "[%s] %s" % (_short_timestamp(when), text)


def format_init_log(
    component: object,
    detail: Optional[object] = None,
    *,
    success: bool = False,
    when: Optional[datetime] = None,
) -> str:
    name = _single_line(component)
    if not name:
        raise ValueError("startup component must not be empty")
    line = "[%s] [!] Init %s ..." % (_short_timestamp(when), name)
    if detail is not None:
        detail_text = _single_line(detail)
        if detail_text:
            line += " " + detail_text
    if success:
        line += " Success"
    return line


def service_ready_lines(
    control_port: int = 17600,
    login_port: int = 17602,
    when: Optional[datetime] = None,
    *,
    control_host: str = "0.0.0.0",
) -> List[str]:
    services = (
        ("USDOF Channel Server", "0.0.0.0:7001"),
        ("USDOF Game Server", "0.0.0.0:10052+"),
        ("USDOF Login HTTP API", "0.0.0.0:%s" % int(login_port)),
        ("USDOF Control HTTP API", "%s:%s" % (control_host, int(control_port))),
        ("USDOF Extra TCP", "0.0.0.0:10128,17101,17200"),
        ("USDOF Extra UDP", "0.0.0.0:12311,12312"),
    )
    return [
        format_init_log(component, endpoint, success=True, when=when)
        for component, endpoint in services
    ]


def startup_footer_lines(when: Optional[datetime] = None) -> List[str]:
    timestamp = _short_timestamp(when)
    return ["[%s] %s" % (timestamp, message) for message in _STARTUP_FOOTER_MESSAGES]


__all__ = [
    "ensure_short_timestamp",
    "format_init_log",
    "has_short_timestamp",
    "service_ready_lines",
    "startup_footer_lines",
]
