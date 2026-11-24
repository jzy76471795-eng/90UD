from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from config import CHANNEL_NAME, CHANNEL_RESPONSE_FLAG, GAME_PORT, LOCAL_HOST, PVF_PATH, REGION_NAME
from channel.channel_crypto import build_encrypted_packed_payload, unpack_decrypt_payload
from channel.channel_wire import build_channel_packet

_wire_ports = ContextVar('channel_wire_ports', default=None)


@contextmanager
def advertise_game_ports(mapping, host):
    token = _wire_ports.set((mapping, host) if mapping else None)
    try:
        yield
    finally:
        _wire_ports.reset(token)


def _advertised_port(port):
    route = _wire_ports.get()
    return port if route is None else route[0][port]

CHANNELINFO_PROFILES = (
    "legacy-cain",
    "minimal-valid-server",
    "us-pvf-startarea-label",
    "single-channel",
    "pvf",
    "pvf-plain",
    "pvf-bracketed",
    "cain-pvf",
    "pvf-cain",
    "backup-cain-pvf",
    "cain-backup-pvf",
    "backup-channel-cain",
    "first-pvf",
    "pvf-first",
    "script-first",
    "first2-pvf",
    "pvf-first2",
    "region-probe-pvf",
    "probe-regions-pvf",
    "cain-first-probe",
    "dfo2018-cain",
    "us2018-cain",
    "tw-channel-r",
    "df-channel-r",
    "dxf-new",
    "cain-pvf-paired",
    "pvf-cain-paired",
    "usdof-multi-channel",
)
CHANNELINFO_ENVELOPES = ("none", "u16-zero", "u32-zero", "u32-size", "tw-status-zero")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw, 0)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int_set(name: str) -> set[int] | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    out: set[int] = set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        if not part:
            continue
        try:
            out.add(int(part, 0))
        except ValueError:
            continue
    return out or None


def _env_text_set(name: str) -> set[str] | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    out = {part.strip().strip("[]").lower() for part in re.split(r"[,;\s]+", raw.strip()) if part.strip()}
    return out or None


LOOPBACK_BY_TEXT_LENGTH = {
    # Mirrored from the DOF-115 reference utility conceptually, not copied into
    # the protocol layer as a dependency.  Some clients validate the original
    # char[16] IP text length before calling WinSock.  These zero-padded 127/8
    # strings stay within the fixed field and still resolve to loopback.
    9: "127.0.0.1",
    10: "127.00.0.1",
    11: "127.000.0.1",
    12: "127.000.00.1",
    13: "127.000.00.01",
    14: "127.000.000.01",
    15: "127.000.000.001",
}


def format_loopback_ip_for_length(length: int) -> str:
    try:
        ip = LOOPBACK_BY_TEXT_LENGTH[length]
    except KeyError as exc:
        raise ValueError(f"unsupported loopback IP text length {length}; expected 9..15") from exc
    fixed_c_string(ip, 0x10)
    return ip


def effective_channelinfo_game_ip_text(default: str = LOCAL_HOST) -> str:
    """Return the IP text to serialize into ChannelInfo char[16].

    ``--host`` still controls the local bind address.  This helper only affects
    the per-channel endpoint string returned to the client, so A/B tests can use
    DOF-115-style zero-padded loopback text without moving the listener.
    """

    from login.native_tunnel import local_game_endpoints
    if local_game_endpoints():return "127.0.0.1"
    route = _wire_ports.get()
    if route is not None:
        default = route[1]
    explicit = os.environ.get("USDOF_CHANNELINFO_GAME_IP_TEXT")
    if explicit and explicit.strip():
        ip = explicit.strip()
        fixed_c_string(ip, 0x10)
        return ip
    length = _env_optional_int("USDOF_CHANNELINFO_GAME_IP_LENGTH")
    if length is not None:
        return format_loopback_ip_for_length(length)
    fixed_c_string(default, 0x10)
    return default


def _wire_user_fields(max_users: int, cur_users: int) -> tuple[int, int]:
    """Return the two u32 user/load fields in the selected wire order.

    IDA/df_channel_r names the fields ``max_user_num`` then ``cur_user_num``.
    DOF-115 labels the same offsets more cautiously as
    ``capacity_or_limit``/``load_or_state``.  Keep the normal order by default
    but expose a runtime swap for fast live A/B without editing code.
    """

    mode = os.environ.get("USDOF_CHANNELINFO_USER_FIELD_ORDER", "max-cur").strip().lower()
    if mode in {"cur-max", "swap", "load-capacity", "state-limit"}:
        return cur_users & 0xFFFFFFFF, max_users & 0xFFFFFFFF
    return max_users & 0xFFFFFFFF, cur_users & 0xFFFFFFFF


@dataclass(frozen=True)
class ChannelInfoChannel:
    name: str
    max_users: int = 500
    cur_users: int = 0
    ip: str = LOCAL_HOST
    port: int = GAME_PORT


@dataclass(frozen=True)
class ChannelInfoGroup:
    name: str
    channels: tuple[ChannelInfoChannel, ...]


@dataclass(frozen=True)
class ChannelScriptChannel:
    """One channel row extracted from the first ``[server]`` block.

    ``etc/channel_info.etc`` stores the display row as a loose token stream:
    ``channel_id``, backtick display name, channel kind, backtick dungeon group
    key, then several UI/recommendation integers.  The Stage3 binary
    ChannelInfo parser does *not* accept these dungeon group keys as outer
    ``group.name`` values; it hard-maps only server-region names such as
    ``cain``/``diregie``/``siroco`` in ``0x015AE0A0``.  We still keep the PVF
    channel metadata here so a valid ``cain`` group can expose the real 2018
    channel rows to the UI.
    """

    channel_id: int
    name: str
    kind: int
    group_key: str
    first_value: int = 0
    recommend: tuple[int, ...] = ()


CHANNELINFO_TEXT_ENCODING = "utf-8"


def fixed_c_string(text: str, size: int, *, encoding: str = CHANNELINFO_TEXT_ENCODING) -> bytes:
    raw = text.encode(encoding)
    if len(raw) >= size:
        raise ValueError(f"{text!r} is too long for {size}-byte fixed string")
    return raw + b"\x00" * (size - len(raw))


def _truncate_text_to_encoded_bytes(text: str, max_bytes: int, *, encoding: str = CHANNELINFO_TEXT_ENCODING) -> str:
    """Truncate without splitting a multibyte character."""

    if max_bytes <= 0:
        return ""
    raw = text.encode(encoding)
    if len(raw) <= max_bytes:
        return text
    raw = raw[:max_bytes]
    while raw:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def fit_fixed_c_string_text(text: str, size: int, *, suffix: str = "") -> str:
    """Return UTF-8 text that fits in a NUL-terminated fixed field.

    ChannelInfo names are stored in 20-byte C strings, so the usable display
    text is at most 19 UTF-8 bytes.  The PVF has a few names longer than that
    (``Guild Tournament Hall``); for the local prototype we trim the base name
    but preserve a numeric suffix when present because ``0x015AC980`` extracts
    digits from ``channel_name`` to derive the client's internal channel id.
    """

    max_len = size - 1
    text_bytes = text.encode(CHANNELINFO_TEXT_ENCODING)
    suffix_bytes_text = suffix.encode(CHANNELINFO_TEXT_ENCODING)
    if len(text_bytes) <= max_len:
        return text
    if suffix:
        suffix_bytes = len(suffix_bytes_text)
        keep = max(0, max_len - suffix_bytes)
        base = _truncate_text_to_encoded_bytes(text, keep).rstrip(" -_")
        out = base + suffix
        return _truncate_text_to_encoded_bytes(out, max_len).rstrip()
    return _truncate_text_to_encoded_bytes(text, max_len).rstrip()


def build_channelinfo_plaintext(
    *,
    region_name: str = REGION_NAME,
    channel_name: str = CHANNEL_NAME,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    field_a: int = 500,
    field_b: int = 0,
) -> bytes:
    body = bytearray()
    body.extend(struct.pack("<I", 1))  # group_count
    body.extend(fixed_c_string(region_name, 0x14))
    body.extend(struct.pack("<I", 1))  # channel_count
    body.extend(fixed_c_string(channel_name, 0x14))
    body.extend(struct.pack("<II", *_wire_user_fields(field_a, field_b)))
    body.extend(fixed_c_string(game_ip_text, 0x10))
    body.extend(struct.pack("<I", _advertised_port(game_port) & 0xFFFFFFFF))
    return bytes(body)


def build_channelinfo_grouped_plaintext(groups: list[ChannelInfoGroup] | tuple[ChannelInfoGroup, ...]) -> bytes:
    """Build the legacy 0x03 ChannelInfo plaintext.

    Static analysis of ``0x015AE960`` shows this layout:

    ``u32 group_count`` followed by N groups; each group has a 20-byte UTF-8
    dungeon/channel-group key, a ``u32 channel_count``, then channel entries
    consisting of 20-byte display name, ``u32 max_users``, ``u32 cur_users``,
    16-byte IPv4 string and ``u32 port``.

    The old one-group builder below is just the degenerate form of this layout.
    """

    body = bytearray()
    body.extend(struct.pack("<I", len(groups)))
    for group in groups:
        body.extend(fixed_c_string(group.name, 0x14))
        body.extend(struct.pack("<I", len(group.channels)))
        for ch in group.channels:
            body.extend(fixed_c_string(ch.name, 0x14))
            body.extend(struct.pack("<II", *_wire_user_fields(ch.max_users, ch.cur_users)))
            body.extend(fixed_c_string(ch.ip, 0x10))
            body.extend(struct.pack("<I", _advertised_port(ch.port) & 0xFFFFFFFF))
    return bytes(body)


def _strip_backtick_token(line: str) -> str | None:
    s = line.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1]
    return None


def _strip_display_token(line: str) -> str | None:
    """Strip PVF/Taiwan channel display tokens.

    USDOF's decoded PVF uses backtick strings (``Golden Road``), while the
    leaked Taiwan ``df_channel_r`` script uses localization placeholders such
    as ``<4::channel_info_cname_12>`` for channel names.  The latter are kept
    as tokens in the analysis docs and converted to short ASCII labels when a
    local A/B ChannelInfo profile is built.
    """

    s = line.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1]
    if len(s) >= 2 and s[0] == "<" and s[-1] == ">":
        return s
    return None


def _channel_info_tokens(text: str, *, include_angle_tokens: bool = False) -> list[str]:
    pattern = r"`[^`]*`|\[[^\]\r\n\t ]+\]|-?\d+"
    if include_angle_tokens:
        pattern = r"`[^`]*`|<[^>\r\n\t ]+>|\[[^\]\r\n\t ]+\]|-?\d+(?:\.\d+)?"
    return re.findall(pattern, text.replace("\r\n", "\n").replace("\r", "\n"))


def extract_server_channels_from_script_text(
    text: str,
    *,
    include_none_group: bool = True,
    max_channels: int | None = None,
) -> list[ChannelScriptChannel]:
    """Extract ordered channel rows from the first PVF ``[server]`` block.

    Real USDOF ``etc/channel_info.etc`` first ``[server]`` block is:

    ``[server]``
    ``<server_id>``
    ``<channel_id> `display` <kind> `group` <first> <10 recommend ints>``
    ...
    ``[/server]``

    Example:

    ``1``
    ``1 `Golden Road` 2 `[elven_guard]` 10 0...0``
    ``6 `Trade` 3 `[none]` 0...0``
    ``10 `Start Area A` 0 `[granfloris]` 5 0...0``

    The integer immediately before the backtick display name is the internal
    ``channel_id``.  A previous trailing-id heuristic stole the *next* row's
    leading id (Golden Road became 6) and greys the region/list when
    ChannelInfo digits and script lookup diverge at ``0x015AC980``/``0x0121F950``.
    """

    tokens = _channel_info_tokens(text)
    channels: list[ChannelScriptChannel] = []
    seen_ids: set[int] = set()
    try:
        idx = tokens.index("[server]") + 1
    except ValueError:
        return channels

    def is_int_token(token: str) -> bool:
        return token.lstrip("-").isdigit()

    # Skip only the server block id.
    if idx < len(tokens) and is_int_token(tokens[idx]):
        idx += 1

    while idx < len(tokens):
        token = tokens[idx]
        if token == "[/server]":
            break
        if not is_int_token(token):
            idx += 1
            continue
        channel_id = int(token)
        if idx + 2 >= len(tokens):
            break
        name = _strip_backtick_token(tokens[idx + 1])
        if not name or not is_int_token(tokens[idx + 2]):
            idx += 1
            continue
        kind = int(tokens[idx + 2])
        if idx + 3 >= len(tokens):
            break
        group_token = _strip_backtick_token(tokens[idx + 3])
        if not group_token:
            idx += 1
            continue

        next_idx = idx + 4
        while next_idx < len(tokens):
            next_token = tokens[next_idx]
            if next_token == "[/server]":
                break
            if (
                is_int_token(next_token)
                and next_idx + 1 < len(tokens)
                and _strip_backtick_token(tokens[next_idx + 1]) is not None
            ):
                break
            next_idx += 1

        if channel_id in seen_ids or channel_id <= 0:
            idx = next_idx
            continue
        if group_token.lower() == "[none]" and not include_none_group:
            idx = next_idx
            continue
        # Capture remaining numeric fields between group key and next row.
        # PVF shape: first_value + up to 10 recommend ints (+ optional trailing id).
        numeric_tail: list[int] = []
        scan = idx + 4
        while scan < next_idx:
            tok = tokens[scan]
            if tok.lstrip("-").isdigit():
                numeric_tail.append(int(tok))
            scan += 1
        first_value = numeric_tail[0] if numeric_tail else 0
        recommend = tuple(numeric_tail[1:11]) if len(numeric_tail) > 1 else tuple()
        if len(recommend) < 10:
            recommend = tuple(list(recommend) + [0] * (10 - len(recommend)))

        seen_ids.add(channel_id)
        channels.append(
            ChannelScriptChannel(
                channel_id=channel_id,
                name=name,
                kind=kind,
                group_key=group_token,
                first_value=first_value,
                recommend=recommend,
            )
        )
        if max_channels is not None and len(channels) >= max_channels:
            break
        idx = next_idx
    return channels


def extract_channel_groups_from_script_text(
    text: str,
    *,
    bracketed_group_name: bool = False,
    max_groups: int = 8,
    max_channels_per_group: int = 3,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> list[ChannelInfoGroup]:
    """Extract a small live-channel list from ``etc/channel_info.etc`` text.

    The PVF script's ``[server]`` sections contain channel display names and
    their dungeon-group keys, e.g. ``Golden Road`` -> ``[elven_guard]``.  The
    runtime parser has already loaded this script before ``0x015AE960`` maps
    the group key through ``0x015AE0A0``; therefore the ChannelInfo packet must
    use the same group vocabulary instead of arbitrary server names such as
    ``cain``.
    """

    tokens = _channel_info_tokens(text)
    grouped: dict[str, list[ChannelInfoChannel]] = {}
    order: list[str] = []
    in_first_server = False
    saw_first_server = False
    for idx, token in enumerate(tokens[:-3]):
        if token == "[server]":
            if saw_first_server:
                break
            saw_first_server = True
            in_first_server = True
            continue
        if token == "[/server]" and in_first_server:
            break
        if not in_first_server:
            continue

        channel_id = token
        name = _strip_backtick_token(tokens[idx + 1])
        channel_kind = tokens[idx + 2]
        group_token = _strip_backtick_token(tokens[idx + 3])
        if not (channel_id.lstrip("-").isdigit() and name and channel_kind.lstrip("-").isdigit() and group_token):
            continue
        if group_token.lower() == "[none]":
            continue
        group_name = group_token if bracketed_group_name else group_token.strip("[]")
        if not group_name:
            continue
        bucket = grouped.setdefault(group_name, [])
        if group_name not in order:
            order.append(group_name)
        if len(bucket) < max_channels_per_group:
            bucket.append(ChannelInfoChannel(name=name, max_users=max_users, cur_users=cur_users, ip=game_ip_text, port=game_port))

    groups: list[ChannelInfoGroup] = []
    for group_name in order[:max_groups]:
        channels = tuple(grouped.get(group_name) or ())
        if channels:
            groups.append(ChannelInfoGroup(group_name, channels))
    return groups


def extract_server_channels_from_channel_service_text(
    text: str,
    *,
    server_id: int | None = None,
    include_none_group: bool = True,
    max_channels: int | None = None,
) -> list[ChannelScriptChannel]:
    """Extract one ``[server]`` block from Taiwan ``df_channel_r`` scripts.

    The leaked Taiwan server keeps the same conceptual channel rows but uses
    localization tokens for channel names:

    ``<channel_id> <4::channel_info_cname_N> <kind> `[group]` <rate fields...>``

    ``cfg/channel.cfg`` selects the active server block with ``id = N``.  This
    helper follows that value when supplied and otherwise falls back to the
    first block, mirroring the C++ service's ``ServerGroup`` construction.
    """

    tokens = _channel_info_tokens(text, include_angle_tokens=True)
    rows: list[ChannelScriptChannel] = []
    in_target_server = False
    in_any_server = False
    block_id_seen = False
    found_target = False
    seen_ids: set[int] = set()
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token == "[server]":
            in_any_server = True
            in_target_server = False
            block_id_seen = False
            seen_ids.clear()
            idx += 1
            continue
        if token == "[/server]" and in_any_server:
            if in_target_server:
                break
            in_any_server = False
            idx += 1
            continue
        if not in_any_server:
            idx += 1
            continue
        if not block_id_seen:
            if token.lstrip("-").isdigit():
                current_server_id = int(token)
                in_target_server = server_id is None or current_server_id == server_id
                found_target = found_target or in_target_server
                block_id_seen = True
            idx += 1
            continue
        if not in_target_server:
            idx += 1
            continue

        if idx + 3 >= len(tokens):
            break
        name = _strip_display_token(tokens[idx + 1])
        group_token = _strip_backtick_token(tokens[idx + 3])
        if (
            token.lstrip("-").isdigit()
            and name
            and tokens[idx + 2].lstrip("-").isdigit()
            and group_token
        ):
            channel_id = int(token)
            if channel_id not in seen_ids and (include_none_group or group_token.lower() != "[none]"):
                # Collect first/recommend ints until the next channel row starts.
                numeric_tail: list[int] = []
                scan = idx + 4
                while scan < len(tokens):
                    tok = tokens[scan]
                    if tok == "[/server]":
                        break
                    if (
                        tok.lstrip("-").isdigit()
                        and scan + 1 < len(tokens)
                        and _strip_display_token(tokens[scan + 1]) is not None
                    ):
                        break
                    if tok.lstrip("-").isdigit():
                        numeric_tail.append(int(tok))
                    scan += 1
                first_value = numeric_tail[0] if numeric_tail else 0
                recommend = tuple(numeric_tail[1:11]) if len(numeric_tail) > 1 else tuple()
                if len(recommend) < 10:
                    recommend = tuple(list(recommend) + [0] * (10 - len(recommend)))
                seen_ids.add(channel_id)
                rows.append(
                    ChannelScriptChannel(
                        channel_id=channel_id,
                        name=name,
                        kind=int(tokens[idx + 2]),
                        group_key=group_token,
                        first_value=first_value,
                        recommend=recommend,
                    )
                )
                if max_channels is not None and len(rows) >= max_channels:
                    break
        idx += 1

    if server_id is not None and not found_target:
        return []
    return rows


def _filter_and_order_server_channel_rows(rows: list[ChannelScriptChannel]) -> list[ChannelScriptChannel]:
    if _env_bool("USDOF_LOCAL_RAID_CHANNELS", False):
        # Reserve these IDs in the opt-in local projection. The PVF already
        # uses 23/32 for ordinary channels; leave the PVF and the off path intact.
        rows = [row for row in rows if row.channel_id not in {23, 32}]
        # Server-local metadata rows, not entries claimed to exist in the PVF.
        rows = [
            ChannelScriptChannel(
                channel_id=23,
                name="Anton Raid 23",
                kind=23,
                group_key="[none]",
                first_value=0,
                recommend=(0,) * 10,
            ),
            ChannelScriptChannel(
                channel_id=32,
                name="Luke Raid 32",
                kind=32,
                group_key="[none]",
                first_value=0,
                recommend=(0,) * 10,
            ),
            *rows,
        ]

    include_kinds = _env_int_set("USDOF_CHANNELINFO_INCLUDE_KINDS")
    exclude_kinds = _env_int_set("USDOF_CHANNELINFO_EXCLUDE_KINDS")
    include_groups = _env_text_set("USDOF_CHANNELINFO_INCLUDE_GROUPS")
    exclude_groups = _env_text_set("USDOF_CHANNELINFO_EXCLUDE_GROUPS")

    filtered: list[ChannelScriptChannel] = []
    for row in rows:
        group_key = row.group_key.strip().strip("[]").lower()
        if include_kinds is not None and row.kind not in include_kinds:
            continue
        if exclude_kinds is not None and row.kind in exclude_kinds:
            continue
        if include_groups is not None and group_key not in include_groups:
            continue
        if exclude_groups is not None and group_key in exclude_groups:
            continue
        filtered.append(row)

    preferred_id = _env_optional_int("USDOF_CHANNELINFO_FIRST_CHANNEL_ID")
    if preferred_id is not None:
        preferred = [row for row in filtered if row.channel_id == preferred_id]
        if preferred:
            remaining = [row for row in filtered if row.channel_id != preferred_id]
            filtered = preferred[:1] + remaining
    return filtered


def effective_append_channel_id_to_name(default: bool = True) -> bool:
    return _env_bool("USDOF_CHANNELINFO_APPEND_CHANNEL_ID", default)



def unique_channel_ports_enabled(default: bool = True) -> bool:
    return _env_bool("USDOF_CHANNELINFO_UNIQUE_PORTS", default)


def channelinfo_port_base(default=None) -> int:
    if default is None:
        default = GAME_PORT
    return _env_int("USDOF_CHANNELINFO_PORT_BASE", int(default))


def game_port_for_channel_id(channel_id: int, *, base_port=None) -> int:
    base = channelinfo_port_base() if base_port is None else int(base_port)
    if unique_channel_ports_enabled(True):
        return (int(base) + int(channel_id)) & 0xFFFF
    return int(base) & 0xFFFF


def channel_id_from_game_port(port: int, *, base_port=None):
    if not unique_channel_ports_enabled(True):
        return None
    base = channelinfo_port_base() if base_port is None else int(base_port)
    channel_id = int(port) - int(base)
    # channel_id 0 is not a real PVF row; treat primary/base port as unmapped
    # so callers can fall back to env defaults instead of ch00.
    if channel_id <= 0 or channel_id > 0xFFFF:
        return None
    return int(channel_id)


def collect_game_listen_ports(*, profile=None, pvf_path=PVF_PATH, base_port=None, game_port=None, max_channels=None):
    primary = int(game_port if game_port is not None else GAME_PORT) & 0xFFFF
    ports = [primary]
    if not unique_channel_ports_enabled(True):
        return ports
    base = channelinfo_port_base() if base_port is None else int(base_port)
    import os as _os
    profile_name = (profile or _os.environ.get("USDOF_CHANNELINFO_PROFILE") or "cain-pvf-paired").strip().lower()
    limit = max_channels if max_channels is not None else _env_int("USDOF_CHANNELINFO_MAX_CHANNELS", 64)
    channel_ids = []
    try:
        multi = {"cain-pvf", "pvf-cain", "dfo2018-cain", "us2018-cain", "cain-pvf-paired", "pvf-cain-paired", "usdof-multi-channel", "first-pvf", "pvf-first", "script-first", "first2-pvf", "pvf-first2", "backup-cain-pvf", "cain-backup-pvf", "backup-channel-cain", "pvf", "pvf-plain", "pvf-bracketed", "tw-channel-r", "df-channel-r"}
        if profile_name in multi:
            text_src = _read_pvf_channel_script_text(str(Path(pvf_path)))
            rows = extract_server_channels_from_script_text(text_src, include_none_group=True, max_channels=None)
            rows = _filter_and_order_server_channel_rows(rows)
            if limit and limit > 0:
                rows = rows[: int(limit)]
            channel_ids = [int(row.channel_id) for row in rows]
        else:
            preferred = _env_optional_int("USDOF_CHANNELINFO_FIRST_CHANNEL_ID")
            if preferred is None:
                preferred = _env_optional_int("USDOF_GAME_CHANNEL_INDEX")
            channel_ids = [int(preferred if preferred is not None else 1)]
    except Exception:
        preferred = _env_optional_int("USDOF_CHANNELINFO_FIRST_CHANNEL_ID")
        channel_ids = [int(preferred if preferred is not None else 1)]
    for channel_id in channel_ids:
        port = game_port_for_channel_id(channel_id, base_port=base)
        if port not in ports:
            ports.append(port)
    return ports


def build_region_server_channelinfo_plaintext_from_script_text(
    text: str,
    *,
    region_name: str = REGION_NAME,
    include_none_group: bool = True,
    max_channels: int = 32,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
    append_channel_id_to_name: bool = True,
) -> bytes:
    """Build a Stage3-valid ChannelInfo list from PVF channel rows.

    Dynamic tracing of ``0x015AE0A0`` confirmed that the first field of each
    ChannelInfo group is *not* the PVF dungeon key (``[elven_guard]`` etc.).
    It must be one of the client's hardcoded server-region identifiers:
    ``first``, ``first2``, ``cain``, ``diregie``, ``siroco``, ...; otherwise
    the parser returns ``0x11`` and aborts before any channels are registered.

    This helper therefore wraps real PVF channel rows under a single valid
    server region, currently ``cain`` for local play.  The UI still receives
    the recognizable PVF channel display names and local Game IP/port.
    """

    rows = extract_server_channels_from_script_text(text, include_none_group=include_none_group, max_channels=None)
    rows = _filter_and_order_server_channel_rows(rows)
    rows = rows[:max_channels]
    if not rows:
        raise ValueError("no channel rows extracted from first [server] block")

    channels: list[ChannelInfoChannel] = []
    append_ids = effective_append_channel_id_to_name(append_channel_id_to_name)
    for row in rows:
        display_name = row.name
        suffix = ""
        if append_ids and str(row.channel_id) not in display_name:
            suffix = f" {row.channel_id}"
            display_name = f"{display_name}{suffix}"
        display_name = fit_fixed_c_string_text(display_name, 0x14, suffix=suffix)
        if unique_channel_ports_enabled(True):
            row_port = game_port_for_channel_id(row.channel_id)
        else:
            row_port = int(game_port) & 0xFFFF
        channels.append(
            ChannelInfoChannel(
                name=display_name,
                max_users=max_users,
                cur_users=cur_users,
                ip=game_ip_text,
                port=row_port,
            )
        )
    return build_channelinfo_grouped_plaintext([ChannelInfoGroup(region_name, tuple(channels))])


def default_tw_channel_root(pvf_path: str = PVF_PATH) -> Path:
    return Path(os.environ.get("USDOF_TW_CHANNEL_ROOT") or (Path(pvf_path).parent.parent / "neople" / "channel"))


def read_tw_channel_cfg_server_id(root: str | Path | None = None, *, pvf_path: str = PVF_PATH) -> int | None:
    cfg = (Path(root) if root is not None else default_tw_channel_root(pvf_path)) / "cfg" / "channel.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?mi)^\s*id\s*=\s*(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def _tw_display_name_for_local_profile(row: ChannelScriptChannel) -> str:
    raw = row.name.strip()
    m = re.search(r"channel_info_cname_(\d+)", raw)
    if m:
        return f"TW {m.group(1)}"
    return raw


def build_tw_channel_r_channelinfo_plaintext(
    *,
    tw_root: str | Path | None = None,
    pvf_path: str = PVF_PATH,
    server_id: int | None = None,
    region_name: str = REGION_NAME,
    include_none_group: bool = True,
    max_channels: int = 48,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    """Build an A/B ChannelInfo profile from the Taiwan ``df_channel_r`` data.

    This is a reference-only profile: it uses the Taiwan service's active
    ``[server]`` block and exact field layout but still wraps the rows under
    the USDOF Stage3-valid ``cain`` region and routes Game to localhost.
    """

    root = Path(tw_root) if tw_root is not None else default_tw_channel_root(pvf_path)
    if server_id is None:
        server_id = read_tw_channel_cfg_server_id(root, pvf_path=pvf_path) or 1
    script_path = root / "channel_info" / "channel_info.etc"
    text = script_path.read_text(encoding="utf-8", errors="replace")
    rows = extract_server_channels_from_channel_service_text(
        text,
        server_id=server_id,
        include_none_group=include_none_group,
        max_channels=max_channels,
    )
    if not rows:
        raise ValueError(f"no Taiwan df_channel_r channel rows extracted from server_id={server_id}")

    channels: list[ChannelInfoChannel] = []
    for row in rows:
        name = _tw_display_name_for_local_profile(row)
        suffix = f" {row.channel_id}"
        if str(row.channel_id) not in name:
            name = f"{name}{suffix}"
        name = fit_fixed_c_string_text(name, 0x14, suffix=suffix)
        channels.append(
            ChannelInfoChannel(
                name=name,
                max_users=max_users,
                cur_users=cur_users,
                ip=game_ip_text,
                port=game_port,
            )
        )
    return build_channelinfo_grouped_plaintext([ChannelInfoGroup(region_name, tuple(channels))])


@lru_cache(maxsize=8)
def _read_pvf_channel_script_text(pvf_path: str) -> str:
    from pvf import pvf_runtime

    text = pvf_runtime.read_text("etc/channel_info.etc", pvf_path=pvf_path)
    if _env_bool("USDOF_RAID_LOCAL", False):
        from game.raid.channel_adapter import adapt_channel_script_kinds
        text = adapt_channel_script_kinds(text)
    return text


@lru_cache(maxsize=8)
def _read_backup_channel_script_text(pvf_path: str) -> str:
    from channel.channel_script import iter_backup_channel_dirs

    for directory in iter_backup_channel_dirs(pvf_path):
        path = directory / "channel_info.etc"
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError("channel_info.etc not found in backup channel dirs")


def build_pvf_channelinfo_plaintext(
    *,
    pvf_path: str = PVF_PATH,
    bracketed_group_name: bool = False,
    max_groups: int = 8,
    max_channels_per_group: int = 3,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    text = _read_pvf_channel_script_text(str(Path(pvf_path)))
    groups = extract_channel_groups_from_script_text(
        text,
        bracketed_group_name=bracketed_group_name,
        max_groups=max_groups,
        max_channels_per_group=max_channels_per_group,
        game_ip_text=game_ip_text,
        game_port=game_port,
        max_users=max_users,
        cur_users=cur_users,
    )
    if not groups:
        raise ValueError("no channel groups extracted from etc/channel_info.etc")
    return build_channelinfo_grouped_plaintext(groups)


def build_cain_pvf_channelinfo_plaintext(
    *,
    pvf_path: str = PVF_PATH,
    region_name: str = REGION_NAME,
    max_channels: int = 32,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    text = _read_pvf_channel_script_text(str(Path(pvf_path)))
    return build_region_server_channelinfo_plaintext_from_script_text(
        text,
        region_name=region_name,
        max_channels=max_channels,
        game_ip_text=game_ip_text,
        game_port=game_port,
        max_users=max_users,
        cur_users=cur_users,
    )


def build_backup_cain_channelinfo_plaintext(
    *,
    pvf_path: str = PVF_PATH,
    region_name: str = REGION_NAME,
    max_channels: int = 32,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    """Build ChannelInfo from the extracted ``pvf里面的频道信息`` files.

    This keeps the 0x09 script payload and the 0x03 ChannelInfo rows on the
    same decoded source text.  The client still requires the outer group name
    to be a hardcoded region key such as ``cain``; only the channel rows come
    from the PVF script.
    """

    text = _read_backup_channel_script_text(str(Path(pvf_path)))
    return build_region_server_channelinfo_plaintext_from_script_text(
        text,
        region_name=region_name,
        max_channels=max_channels,
        game_ip_text=game_ip_text,
        game_port=game_port,
        max_users=max_users,
        cur_users=cur_users,
    )


def build_region_probe_pvf_channelinfo_plaintext(
    *,
    pvf_path: str = PVF_PATH,
    region_names: tuple[str, ...] | None = None,
    max_channels: int = 1,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    """Build one identical local channel under several known server keys.

    The 2018 client accepts several textual outer group/server keys in
    ``0x015AE0A0`` (notably ``cain``, ``first`` and ``first2``), but the UI
    insert path then performs a second lookup against the table populated by
    the 0x09 script response.  While the exact selected key is being traced,
    this A/B profile returns the same localhost channel under multiple valid
    keys so the UI has a chance to match whichever region/server id it is
    currently filtering on.  Invalid keys are deliberately not included because
    one unmapped group makes the client abort ChannelInfo construction.
    """

    if region_names is None:
        raw = os.environ.get("USDOF_CHANNELINFO_REGION_PROBE_NAMES", "")
        if raw.strip():
            region_names = tuple(part.strip() for part in re.split(r"[,;\s]+", raw) if part.strip())
        else:
            region_names = ("cain", "first", "first2")

    text = _read_pvf_channel_script_text(str(Path(pvf_path)))
    rows = extract_server_channels_from_script_text(text, include_none_group=True, max_channels=None)
    rows = _filter_and_order_server_channel_rows(rows)
    rows = rows[:max_channels]
    if not rows:
        raise ValueError("no channel rows extracted from first [server] block")

    append_ids = effective_append_channel_id_to_name(True)
    channels: list[ChannelInfoChannel] = []
    for row in rows:
        display_name = row.name
        suffix = ""
        if append_ids and str(row.channel_id) not in display_name:
            suffix = f" {row.channel_id}"
            display_name = f"{display_name}{suffix}"
        display_name = fit_fixed_c_string_text(display_name, 0x14, suffix=suffix)
        channels.append(
            ChannelInfoChannel(
                name=display_name,
                max_users=max_users,
                cur_users=cur_users,
                ip=game_ip_text,
                port=game_port,
            )
        )

    shared = tuple(channels)
    groups = [ChannelInfoGroup(name, shared) for name in region_names]
    return build_channelinfo_grouped_plaintext(groups)


def build_dxf_new_channelinfo_plaintext(
    *,
    channel_name: str = "#ch.11",
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    max_users: int = 500,
    cur_users: int = 0,
) -> bytes:
    """Build the newer flat 0x12-style payload used by the DXF S4A12 server.

    This is not the default USDOF 0x03 response, but keeping it available makes
    A/B tests easy when a client asks for ``CS_ASK_CHANNEL_INFO_NEW``.
    """

    body = bytearray()
    body.extend(b"\x00\x00")
    body.extend(struct.pack("<I", 1))
    body.extend(fixed_c_string(channel_name, 0x14))
    body.extend(struct.pack("<II", *_wire_user_fields(max_users, cur_users)))
    body.extend(fixed_c_string(game_ip_text, 0x10))
    body.extend(struct.pack("<I", _advertised_port(game_port) & 0xFFFFFFFF))
    return bytes(body)


def build_channelinfo_plaintext_by_profile(
    *,
    profile: str | None = None,
    game_ip_text: str = LOCAL_HOST,
    game_port: int = GAME_PORT,
    pvf_path: str = PVF_PATH,
    envelope: str | None = None,
    **kwargs,
) -> bytes:
    profile_name = (profile or os.environ.get("USDOF_CHANNELINFO_PROFILE") or "cain-pvf-paired").strip().lower()
    game_ip_text = effective_channelinfo_game_ip_text(game_ip_text)
    max_users = _env_int("USDOF_CHANNELINFO_MAX_USERS", 500)
    cur_users = _env_int("USDOF_CHANNELINFO_CUR_USERS", 0)
    max_channels = _env_int("USDOF_CHANNELINFO_MAX_CHANNELS", 64)
    max_groups = _env_int("USDOF_CHANNELINFO_MAX_GROUPS", 8)
    max_channels_per_group = _env_int("USDOF_CHANNELINFO_MAX_CHANNELS_PER_GROUP", 3)
    env_region_name = os.environ.get("USDOF_CHANNELINFO_REGION_NAME")
    env_channel_name = os.environ.get("USDOF_CHANNELINFO_CHANNEL_NAME")
    if env_region_name:
        kwargs.setdefault("region_name", env_region_name)
    if profile_name in {
        "minimal-valid-server",
        "minimal-valid",
        "valid-minimal",
        "minimal-server",
        "single-channel",
        "single-channel-script",
    }:
        # Pair for channel_script:minimal-valid-server.  The script response
        # inserts server_id=1/channel_id=1, while ChannelInfo's display name is
        # later parsed by 0x015AC980 to recover channel_id=1 for the UI lookup
        # at 0x0121F950.  Keep this profile intentionally small: one cain
        # region and one selectable localhost channel.
        if env_channel_name:
            kwargs.setdefault("channel_name", env_channel_name)
        kwargs.setdefault("region_name", REGION_NAME)
        kwargs.setdefault("channel_name", CHANNEL_NAME)
        kwargs.setdefault("field_a", max_users)
        kwargs.setdefault("field_b", _env_int("USDOF_CHANNELINFO_CUR_USERS", 1))
        plain = build_channelinfo_plaintext(game_ip_text=game_ip_text, game_port=game_port, **kwargs)
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"us-pvf-startarea-label", "startarea-label", "start-area-label"}:
        # Pair for channel_script:us-pvf-startarea-label.  The script row uses
        # leading channel_id=11 with a quoted post-float label; ChannelInfo must
        # expose a display string containing the same digits so 0x015AC980
        # extracts 11 for the 0x0121F950 lookup.
        kwargs.setdefault("region_name", REGION_NAME)
        kwargs.setdefault("channel_name", "Start Area A 11")
        kwargs.setdefault("field_a", max_users)
        kwargs.setdefault("field_b", _env_int("USDOF_CHANNELINFO_CUR_USERS", 1))
        plain = build_channelinfo_plaintext(game_ip_text=game_ip_text, game_port=game_port, **kwargs)
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"legacy", "legacy-cain", "cain", "single"}:
        if env_channel_name:
            kwargs.setdefault("channel_name", env_channel_name)
        kwargs.setdefault("field_a", max_users)
        kwargs.setdefault("field_b", cur_users)
        plain = build_channelinfo_plaintext(game_ip_text=game_ip_text, game_port=game_port, **kwargs)
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"pvf", "pvf-plain", "pvf-unbracketed"}:
        plain = build_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            bracketed_group_name=False,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_groups=max_groups,
            max_channels_per_group=max_channels_per_group,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"pvf-bracketed", "pvf-brackets"}:
        plain = build_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            bracketed_group_name=True,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_groups=max_groups,
            max_channels_per_group=max_channels_per_group,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"cain-pvf", "pvf-cain", "dfo2018-cain", "us2018-cain", "cain-pvf-paired", "pvf-cain-paired", "usdof-multi-channel"}:
        plain = build_cain_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            region_name=kwargs.get("region_name", REGION_NAME),
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"backup-cain-pvf", "cain-backup-pvf", "backup-channel-cain"}:
        plain = build_backup_cain_channelinfo_plaintext(
            pvf_path=pvf_path,
            region_name=kwargs.get("region_name", REGION_NAME),
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"first-pvf", "pvf-first", "script-first"}:
        # Static 0x015AE960 -> 0x0121F950 analysis shows the final channel
        # insert looks up (server_id, channel_id) in the table populated by the
        # 0x09 script parser.  The first PVF [server] block is keyed by id=1,
        # while the hard-coded region mapper accepts textual keys such as
        # "first", "first2", "cain", ... and then converts them to those
        # numeric server ids.  If "cain" resolves to a later id but the script
        # parser only populated/selected the first block, the packet parses
        # successfully yet the UI list stays empty.  This profile deliberately
        # uses the "first" region key so ChannelInfo aligns with the first
        # script [server] block for the current single-region local prototype.
        plain = build_cain_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            region_name=kwargs.get("region_name", "first"),
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"first2-pvf", "pvf-first2"}:
        plain = build_cain_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            region_name=kwargs.get("region_name", "first2"),
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"region-probe-pvf", "probe-regions-pvf", "cain-first-probe"}:
        plain = build_region_probe_pvf_channelinfo_plaintext(
            pvf_path=pvf_path,
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"tw-channel-r", "df-channel-r", "taiwan-channel-r"}:
        plain = build_tw_channel_r_channelinfo_plaintext(
            pvf_path=pvf_path,
            region_name=kwargs.get("region_name", REGION_NAME),
            max_channels=max_channels,
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    if profile_name in {"dxf-new", "flat-new", "new"}:
        plain = build_dxf_new_channelinfo_plaintext(
            game_ip_text=game_ip_text,
            game_port=game_port,
            max_users=max_users,
            cur_users=cur_users,
        )
        return apply_channelinfo_envelope(plain, envelope=envelope)
    raise ValueError(f"unknown ChannelInfo profile: {profile!r}")


def normalize_channelinfo_envelope(envelope: str | None) -> str:
    name = (envelope or os.environ.get("USDOF_CHANNELINFO_ENVELOPE") or "none").strip().lower()
    aliases = {
        "": "none",
        "plain": "none",
        "no": "none",
        "off": "none",
        "u16": "u16-zero",
        "u16-status": "u16-zero",
        "u32": "u32-zero",
        "u32-status": "u32-zero",
        "len": "u32-size",
        "length": "u32-size",
        "tw": "tw-status-zero",
        "tw-status": "tw-status-zero",
        "status-zero": "tw-status-zero",
        "no-count": "tw-status-zero",
        "replace-count-zero": "tw-status-zero",
    }
    name = aliases.get(name, name)
    if name not in CHANNELINFO_ENVELOPES:
        raise ValueError(
            f"unknown ChannelInfo envelope: {envelope!r}; expected one of {', '.join(CHANNELINFO_ENVELOPES)}"
        )
    return name


def apply_channelinfo_envelope(plain: bytes, *, envelope: str | None = None) -> bytes:
    """Optionally prepend small envelope fields for A/B traces.

    Static ``0x015AE960`` currently appears to read ``group_count`` at stream
    offset 0, so ``none`` remains the default.  The alternates are intentionally
    explicit runtime switches for checking whether ``0x015AC610``/stream setup
    is silently consuming a status/length field before the outer parser runs.
    """

    name = normalize_channelinfo_envelope(envelope)
    if name == "none":
        return plain
    if name == "u16-zero":
        return b"\x00\x00" + plain
    if name == "u32-zero":
        return b"\x00\x00\x00\x00" + plain
    if name == "u32-size":
        return struct.pack("<I", len(plain)) + plain
    if name == "tw-status-zero":
        if len(plain) < 4:
            raise ValueError("ChannelInfo plaintext too short for tw-status-zero transform")
        # Taiwan df_channel_r builds tagSC_ASK_CHANNEL_INFO with size 0x0F:
        # the 0x0B packet header is followed by a 4-byte zero/ack/status area,
        # then tServerGroupInfo/tpServerInfo records are appended.  There is no
        # explicit group_count dword in that server response; consumers iterate
        # records until the decrypted stream is exhausted.  Our grouped builder
        # starts with group_count, so this A/B transform replaces that leading
        # count with the Taiwan-style zero status while preserving the group
        # records verbatim.
        return b"\x00\x00\x00\x00" + plain[4:]
    raise AssertionError(name)


def build_channelinfo_payload(
    *,
    profile: str | None = None,
    pvf_path: str = PVF_PATH,
    envelope: str | None = None,
    **kwargs,
) -> bytes:
    return build_encrypted_packed_payload(
        build_channelinfo_plaintext_by_profile(profile=profile, pvf_path=pvf_path, envelope=envelope, **kwargs)
    )


def build_channelinfo_packet(
    *,
    profile: str | None = None,
    pvf_path: str = PVF_PATH,
    opcode: int = 0x03,
    envelope: str | None = None,
    response_flag: int = CHANNEL_RESPONSE_FLAG,
    **kwargs,
) -> bytes:
    return build_channel_packet(
        opcode,
        build_channelinfo_payload(profile=profile, pvf_path=pvf_path, envelope=envelope, **kwargs),
        flag=response_flag,
    )


def decode_channelinfo_payload_for_test(payload: bytes, plaintext_len: int | None = None) -> bytes:
    plain_padded = unpack_decrypt_payload(payload)
    return plain_padded if plaintext_len is None else plain_padded[:plaintext_len]


def _decode_fixed_c_string(data: bytes) -> str:
    """Decode a ChannelInfo fixed C string using the client compatibility order."""

    raw = data.split(b"\x00", 1)[0]
    for encoding in (CHANNELINFO_TEXT_ENCODING, "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode(CHANNELINFO_TEXT_ENCODING, errors="replace")


def parse_channelinfo_grouped_plaintext(
    data: bytes,
    *,
    envelope: str | None = None,
    max_groups: int = 64,
    max_channels: int = 1024,
) -> dict[str, object]:
    """Parse the grouped ChannelInfo plaintext used by opcode ``0x03``.

    This parser is intentionally diagnostic-oriented: it returns *all* groups
    and channels instead of only the first entry, and it reports the byte offset
    consumed so live diary logs can distinguish "payload parsed but UI ignored
    it" from "payload shape is malformed/truncated".

    ``data`` should be the decrypted plaintext before AES zero padding is
    re-added.  It also accepts the optional A/B envelopes implemented by
    :func:`apply_channelinfo_envelope`.
    """

    envelope_name = normalize_channelinfo_envelope(envelope)
    payload_offset = 0
    envelope_info: dict[str, object] = {"type": envelope_name}
    if envelope_name == "u16-zero":
        if len(data) < 2:
            raise ValueError("ChannelInfo u16-zero envelope is truncated")
        envelope_info["status"] = int.from_bytes(data[:2], "little")
        payload_offset = 2
    elif envelope_name == "u32-zero":
        if len(data) < 4:
            raise ValueError("ChannelInfo u32-zero envelope is truncated")
        envelope_info["status"] = int.from_bytes(data[:4], "little")
        payload_offset = 4
    elif envelope_name == "u32-size":
        if len(data) < 4:
            raise ValueError("ChannelInfo u32-size envelope is truncated")
        envelope_info["declared_size"] = int.from_bytes(data[:4], "little")
        payload_offset = 4
    elif envelope_name == "tw-status-zero":
        if len(data) < 4:
            raise ValueError("ChannelInfo tw-status-zero envelope is truncated")
        envelope_info["status"] = int.from_bytes(data[:4], "little")
        payload_offset = 4

    if len(data) - payload_offset < 4:
        raise ValueError(f"ChannelInfo plaintext too short for group_count: {len(data)}")

    pos = payload_offset
    if envelope_name == "tw-status-zero":
        group_count = None
        group_limit = max_groups
    else:
        group_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if group_count > max_groups:
            raise ValueError(f"ChannelInfo group_count {group_count} exceeds diagnostic max {max_groups}")
        group_limit = group_count

    groups: list[dict[str, object]] = []
    group_index = 0
    while group_index < group_limit and (group_count is None or group_index < group_count):
        if group_count is None:
            # Taiwan df_channel_r-style response has no explicit group count:
            # it appends group records until the encrypted stream ends.  Treat
            # all-zero trailing AES padding as the natural terminator.
            if pos >= len(data) or all(b == 0 for b in data[pos:]):
                break
        if pos + 0x14 + 4 > len(data):
            raise ValueError(
                f"ChannelInfo group[{group_index}] header truncated at offset 0x{pos:x} / len=0x{len(data):x}"
            )
        group_name = _decode_fixed_c_string(data[pos : pos + 0x14])
        pos += 0x14
        channel_count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if channel_count > max_channels:
            raise ValueError(
                f"ChannelInfo group[{group_index}] channel_count {channel_count} exceeds diagnostic max {max_channels}"
            )
        channels: list[dict[str, object]] = []
        for channel_index in range(channel_count):
            if pos + 0x14 + 8 + 0x10 + 4 > len(data):
                raise ValueError(
                    "ChannelInfo channel entry truncated "
                    f"group={group_index} channel={channel_index} offset=0x{pos:x} / len=0x{len(data):x}"
                )
            name = _decode_fixed_c_string(data[pos : pos + 0x14])
            pos += 0x14
            max_users, cur_users = struct.unpack_from("<II", data, pos)
            pos += 8
            ip = _decode_fixed_c_string(data[pos : pos + 0x10])
            pos += 0x10
            port = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            channels.append(
                {
                    "index": channel_index,
                    "name": name,
                    "max_users": max_users,
                    "cur_users": cur_users,
                    "ip": ip,
                    "port": port,
                    "entry_offset": pos - (0x14 + 8 + 0x10 + 4),
                }
            )
        groups.append(
            {
                "index": group_index,
                "name": group_name,
                "channel_count": channel_count,
                "channels": channels,
            }
        )
        group_index += 1

    trailing = data[pos:]
    if group_count is None:
        group_count = len(groups)
    return {
        "plain_len": len(data),
        "payload_offset": payload_offset,
        "bytes_consumed": pos,
        "trailing_len": len(trailing),
        "trailing_zero_len": len(trailing) if trailing and all(b == 0 for b in trailing) else 0,
        "envelope": envelope_info,
        "group_count": group_count,
        "groups": groups,
    }


def parse_channelinfo_plaintext(data: bytes) -> dict[str, object]:
    """Parse the current one-group/one-channel candidate for smoke checks."""
    if len(data) < 76:
        raise ValueError(f"ChannelInfo plaintext too short: {len(data)}")
    pos = 0
    group_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    region = _decode_fixed_c_string(data[pos : pos + 0x14])
    pos += 0x14
    channel_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    channel = _decode_fixed_c_string(data[pos : pos + 0x14])
    pos += 0x14
    field_a, field_b = struct.unpack_from("<II", data, pos)
    pos += 8
    ip = _decode_fixed_c_string(data[pos : pos + 0x10])
    pos += 0x10
    port = struct.unpack_from("<I", data, pos)[0]
    return {
        "group_count": group_count,
        "region_name": region,
        "channel_count": channel_count,
        "channel_name": channel,
        "field_a": field_a,
        "field_b": field_b,
        "game_ip_text": ip,
        "game_port": port,
    }
