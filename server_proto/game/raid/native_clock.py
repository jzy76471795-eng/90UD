"""Initial raid CHANNELINFO clock anchor for the pinned 90US native client.

016650CC writes field11 low32 as the seconds base; 01AC58D0 advances it
with timeGetTime. Positive CHANNELINFO also starts LOGIN, so send it only
once per Game connection. This transport receipt proves send, not client
consumption or successful native initialization.
"""
from dataclasses import dataclass
from math import ceil, isfinite
import os
import struct
from time import time

from game.game_handlers import HandlerDecision, build_special_channelinfo_payload, _opcode1_profile
from game.game_wire import build_game_packet

_INT32_MAX = 0x7fffffff
_PROFILES = frozenset(('usdof90-protobuf', 'usdof90-channelinfo',
    'usdof90-channelinfo-protobuf', 'us90-protobuf', 'protobuf'))


@dataclass(frozen=True, eq=False)
class _ClockSend:
    session: object
    channel_id: int
    packet: bytes
    unix_seconds: int
    server_sample: float


@dataclass(frozen=True, eq=False)
class _StartClockSend:
    session: object
    channel_id: int
    packet: bytes
    unix_seconds: int
    server_sample: float
    character_id: int


def track_start_clock(session, packet, *, unix_seconds, server_sample):
    """Paired raid-runtime-v2 observes the existing START+5 Unix timestamp.

    This records exact socket delivery, not proof of native DLL execution.
    Keep CHANNELINFO mode0, whose positive path also restarts LOGIN.
    """
    if (session is None or getattr(session, 'selected_channel_id', None) not in (23, 32)
            or os.environ.get('USDOF_RAID_START_CLOCK', '0') != '1'):
        return
    cid = getattr(session, 'selected_character_id', None)
    if type(cid) is not int or cid <= 0 or not 1577836800 <= unix_seconds <= _INT32_MAX:
        raise ValueError('selected START clock identity or timestamp unavailable')
    session._raid_clock_sent = None
    session._raid_start_clock_pending = _StartClockSend(session, session.selected_channel_id,
        packet, unix_seconds, float(server_sample), cid)


def invalidate_start_clock(session):
    if isinstance(getattr(session, '_raid_clock_sent', None), _StartClockSend):
        session._raid_clock_sent = None
    session._raid_start_clock_pending = None


def _varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def build_channelinfo_plaintext(*, channel_name, namespace, unix_seconds, nat_profile=None):
    """Finite cain/server0, actor0, bank0 profile; optional startup NAT config."""
    if type(namespace) is not int or namespace != 0:
        raise ValueError('raid clock profile requires existing native actor namespace0')
    if type(unix_seconds) not in (int, float) or not isfinite(unix_seconds):
        raise ValueError('finite Unix clock sample required')
    seconds = int(unix_seconds)
    if not 1 <= seconds <= _INT32_MAX:
        raise ValueError('native seconds base must fit positive signed int32')
    if type(channel_name) is not str or not channel_name or '\0' in channel_name:
        raise ValueError('trusted nonempty ASCII channel name required')
    try:
        name = channel_name.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError('native CHANNELINFO widens bytes; ASCII channel name required') from exc
    # Fields5/6 retain current neutral defaults. Peer fields are absent
    # unless a current startup-owned NAT configuration is explicitly passed.
    msg = (b'\x08\x01\x10\x01\x22' + _varint(len(name)) + name
           + b'\x38\x00\x40\x00\x48\x00\x58' + _varint(seconds)
           + b'\x80\x01\x04')
    if nat_profile is not None:
        from .native_nat_profile import configured_fields
        primary, alternate, port1, port2 = configured_fields(nat_profile)
        for field, addresses in ((12, primary), (30001, alternate)):
            for address in addresses:
                msg += _varint((field << 3) | 2) + _varint(len(address)) + address
        msg += b'\x68' + _varint(port1) + b'\x70' + _varint(port2)
    return struct.pack('<I', len(msg)) + msg


def _channel_name(channel_id):
    from channel.channel_info import _filter_and_order_server_channel_rows
    from config import REGION_NAME
    if os.environ.get('USDOF_CHANNELINFO_REGION_NAME', REGION_NAME) != 'cain':
        raise ValueError('raid clock profile is verified only for the current cain group')
    # These are the same server-owned local raid rows used by the channel
    # list producer. Do not derive a display name from C2S bytes or invent a
    # substitute if the authorized row has been removed/filtered.
    rows = [row for row in _filter_and_order_server_channel_rows([])
            if row.channel_id == channel_id and row.kind == channel_id]
    if len(rows) != 1:
        raise ValueError('selected local raid channel has no unique trusted metadata row')
    return rows[0].name


def clock_ready(session):
    receipt = getattr(session, '_raid_clock_sent', None)
    return (isinstance(receipt, (_ClockSend, _StartClockSend)) and receipt.session is session
            and receipt.channel_id == getattr(session, 'selected_channel_id', None)
            and (not isinstance(receipt, _StartClockSend)
                 or receipt.character_id == getattr(session, 'selected_character_id', None))
            and getattr(session, '_raid_native_namespace', None) == 0)


def initial_channelinfo(session, *, namespace, clock=time, server=None):
    """Called only by the raid23/32 initial Game CHECK_CONNECTION branch."""
    if clock_ready(session):
        return HandlerDecision([], 'raid CHANNELINFO already sent; skip LOGIN flow reset')
    if (type(namespace) is not int or namespace != 0
            or getattr(session, 'selected_channel_id', None) not in (23, 32)
            or getattr(session, 'selected_character_id', None) is not None):
        raise ValueError('raid clock profile requires an initial bound raid23/32 connection')
    if (_opcode1_profile().lower().replace('_', '-') not in _PROFILES
            or os.environ.get('USDOF_GAME_CHANNELINFO_PLAIN')):
        raise ValueError('raid clock requires the verified protobuf profile without a raw override')
    pending = getattr(session, '_raid_clock_pending', None)
    if pending is not None:
        if (not isinstance(pending, _ClockSend) or pending.session is not session
                or pending.channel_id != session.selected_channel_id):
            raise ValueError('raid clock pending connection identity changed')
        return HandlerDecision([pending.packet], 'retry original unsent raid CHANNELINFO')
    from .native_nat_profile import profile_for
    nat_profile = profile_for(server) if server is not None else None
    sample = clock()
    plain = build_channelinfo_plaintext(channel_name=_channel_name(session.selected_channel_id),
        namespace=namespace, unix_seconds=sample, nat_profile=nat_profile)
    packet = build_game_packet(1, build_special_channelinfo_payload(plain), flag_or_magic=0)
    # Keep the exact bytes object through sendall and the namespace receipt.
    # No clock-ready state is committed at producer/queue time.
    # build_channelinfo_plaintext floors the one supplied clock sample. The
    # receipt retains the packet and connection identity, not a second clock.
    session._raid_clock_pending = _ClockSend(session, session.selected_channel_id, packet, int(sample), float(sample))
    session._raid_namespace_pending = (packet, namespace)
    return HandlerDecision([packet], 'initial positive raid CHANNELINFO; Unix seconds clock anchor')


def note_send(session, packet):
    """Commit only after sendall of this connection's exact original packet."""
    start = getattr(session, '_raid_start_clock_pending', None)
    if (isinstance(start, _StartClockSend) and start.session is session
            and start.packet is packet and start.channel_id == getattr(session, 'selected_channel_id', None)
            and start.character_id == getattr(session, 'selected_character_id', None)):
        session._raid_clock_sent = start
        session._raid_start_clock_pending = None
    pending = getattr(session, '_raid_clock_pending', None)
    namespace = getattr(session, '_raid_namespace_pending', None)
    if (isinstance(pending, _ClockSend) and pending.session is session
            and pending.packet is packet and pending.channel_id == getattr(session, 'selected_channel_id', None)
            and namespace is not None and namespace[0] is packet and namespace[1] == 0):
        session._raid_native_namespace = 0
        session._raid_namespace_pending = None
        session._raid_clock_sent = pending
        session._raid_clock_pending = None


def native_deadline(entry, end, duration):
    """Legacy three-argument epoch mapper; protect native signed arithmetic."""
    if type(duration) is not int or not 1 <= duration <= _INT32_MAX:
        raise ValueError('native timer duration must be positive signed int32')
    if type(end) not in (int, float) or not isfinite(end):
        raise ValueError('native timer needs a finite Unix deadline')
    deadline = ceil(end)
    if not duration <= deadline <= _INT32_MAX:
        raise ValueError('native timer start/end must fit nonnegative signed int32')
    return deadline


def native_timer_deadline(session, entry, end, duration):
    """Map using the actual sent field11 base B and its single server sample S.

    ceil(B + end - S) removes the fixed floor(S) fractional bias. It still
    rounds to integer seconds and cannot recover the unknown client clock
    write time or network/processing delay. It is not a zero-error clock or
    a proof that the native progress bar's start is already in the past.
    """
    if not clock_ready(session):
        return None
    if type(end) not in (int, float) or not isfinite(end):
        raise ValueError('native timer needs a finite Unix deadline')
    receipt = session._raid_clock_sent
    mapped = receipt.unix_seconds + (end - receipt.server_sample)
    return native_deadline(entry, mapped, duration)
