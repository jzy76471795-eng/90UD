"""US raid channel projection, pinned to EXE SHA 14b85f7e...a56634.

Evidence: 0x1601CAC reads the third LOGIN payload u8; 0x1601F11 calls
0x1AB5240 to write it to client manager +0xE60. 0x1AB52A0 returns that
field. 0x1199910 maps kind 23/25 to raid type 0 and kind 32 to type 1;
0x1AB4EB0 enables the ordinary raid predicate only for kinds 23 and 32.

Channel numbers 23 and 32 are the user's chosen server slots. Their numbers
are distinct from these verified client kind values. Existing dungeon group
keys are preserved because new group consumers have not yet been verified.
"""
from __future__ import annotations

import re

_KINDS = {23: 23, 32: 32}
_ROW = re.compile(r'(?m)^(?P<id>[ \t]*(?:23|32))(?P<gap>\s+`[^`]*`\s+)(?P<kind>-?\d+)')
_SERVER = re.compile(r'(?ims)(?P<open>^[ \t]*\[server\][ \t]*\r?\n)(?P<body>.*?)(?P<close>^[ \t]*\[/server\][ \t]*(?:\r?\n|$))')


def raid_kind_for_channel(channel_id: int | None) -> int | None:
    return _KINDS.get(channel_id)


def raid_type_for_kind(kind: int) -> int | None:
    if kind in (23, 25):
        return 0
    if kind == 32:
        return 1
    # The native default is type 2. Its meaning is not part of this port.
    return None


def adapt_login_state_body(body: bytes, channel_id: int | None) -> bytes:
    kind = raid_kind_for_channel(channel_id)
    if kind is None or not body or body[0] != 1:
        return body
    if len(body) < 4:
        raise ValueError('successful US LOGIN state body is missing the kind field')
    return body[:3] + bytes((kind,)) + body[4:]


def adapt_channel_script_kinds(text: str) -> str:
    """Project only the two selected row kinds, preserving formatting/groups."""
    def replace(match: re.Match[str]) -> str:
        return match['id'] + match['gap'] + str(_KINDS[int(match['id'])])
    def server(match: re.Match[str]) -> str:
        return match['open'] + _ROW.sub(replace, match['body']) + match['close']
    return _SERVER.sub(server, text)
