"""Native chat bodies and recipient-owned Game-thread delivery.

C0011 main writer01F08DE0: u8,u16,u32,DSTR; whisper kinds add
DSTR,u8. Dedicated dialog writers029601B0/017230C0 have that suffix.
N0041 reader0165F8F0: kind,u8 result,DSTR name,u8 aux,DSTR message;
kind1F alone consumes another DSTR. S1/0011 success has no extra body.
C0298 writer02B0CD80 and N024D reader01633C50 use the raid notice path.
DSTR is u32 byte length followed by current-client codepage bytes.
"""
from collections import deque
from dataclasses import dataclass
import struct
from threading import RLock

from game.raid.runtime import RaidError

_LOCK = RLock()


def _string(raw, offset, maximum, *, empty=False):
    if offset + 4 > len(raw):
        raise ValueError('truncated chat string length')
    size = struct.unpack_from('<I', raw, offset)[0]
    offset += 4
    value = raw[offset:offset + size]
    if (size > maximum or len(value) != size or not empty and not size
            or any(ch < 32 for ch in value)):
        raise ValueError('invalid native chat text')
    try:
        text = value.decode('utf-8')
    except UnicodeDecodeError:
        text = value.decode('cp936')
    if len(text) > (254 if maximum > 120 else 30):
        raise ValueError('chat text exceeds native character capacity')
    return value, offset + size


def _end(raw, logical):
    # Codec padding is retained on legacy transport. Never strip message bytes.
    if len(raw) != logical and not (any(len(raw) == (logical + unit - 1) // unit * unit for unit in (8, 16))
                                    and not any(raw[logical:])):
        raise ValueError('unexpected native chat suffix')


def whisper_name_matches(stored, supplied):
    if not supplied or stored == supplied:
        return True
    try:
        canonical = stored.decode('utf-8', 'strict')
        try:
            name = supplied.decode('utf-8', 'strict')
        except UnicodeDecodeError:
            name = supplied.decode('cp936', 'strict')
        return name == canonical
    except UnicodeDecodeError:
        return False


def dstring(value):
    return struct.pack('<I', len(value)) + value


def parse_message(raw):
    raw = bytes(raw)
    if len(raw) < 12:
        raise ValueError('truncated native chat request')
    kind, target, context = struct.unpack_from('<BHI', raw)
    message, pos = _string(raw, 7, 1020)
    name, aux = b'', 0
    if kind in (1, 7, 0x1f, 0x22, 0x23, 0x24, 0x26, 0x2b):
        name, pos = _string(raw, pos, 120, empty=True)
        if pos >= len(raw) or raw[pos] > 100:
            raise ValueError('invalid native chat auxiliary byte')
        aux = raw[pos]
        pos += 1
    _end(raw, pos)
    return kind, target, context, message, name, aux


@dataclass(frozen=True)
class _Message:
    sender: tuple
    recipient: tuple
    scope: tuple
    opcode: int
    body: bytes


class NativeChat:
    def __init__(self, server):
        self.server = server
        self._queues = {}
        self._lock = RLock()

    def _directory(self):
        value = getattr(self.server, 'native_game_connections', None)
        if value is None:
            raise RaidError('authenticated Game chat directory unavailable')
        return value

    def _record(self, selection):
        return next((r for r in self.server._repo().list_by_account(selection[2])
                     if int(r.character_id) == selection[4]), None)

    def _scope(self, session, kind):
        # Native chat input emits kind3 for normal speech. Kind0 follows the
        # legacy local route; native presentation treats it as a system kind.
        if kind in (0, 3):
            if getattr(session, 'in_dungeon_run', False):
                try:
                    return self._scope(session, 2)
                except RaidError:
                    # A normal solo dungeon has no raid party directory.
                    # Bind its local chat to this authenticated run only.
                    generation = getattr(session, 'dungeon_run_generation', 0)
                    if type(generation) is not int or generation <= 0:
                        raise
                    return ('dungeon_self', id(session), generation)
            town, area = getattr(session, 'cur_town_id', None), getattr(session, 'cur_area_id', None)
            if town is None or area is None:
                raise RaidError('chat town placement unavailable')
            return ('town', session.selected_channel_id, town, area)
        if kind == 2:
            from game.normal_party import current
            party = current(self.server, session)
            if party is not None:
                return ('normal_party', party.party_id, party.owner)
        service = getattr(self.server, 'raid_service', None)
        if service is None:
            raise RaidError('owned chat membership unavailable')
        with service._lock:
            cid, account, _record = service._selected_actor(session)
            entry = service.current(cid)
            if entry is None:
                raise RaidError('chat actor has no raid')
            epoch = service.require_session(entry, session, cid, account)
            # 01F08DE0 checks 2F with01EF6AE0 (current raid); 30 with
            # 01EF7C00 (current raid leader). Kind9 is not raid chat.
            if kind in (0x2f, 0x30):
                return ('raid', entry.domain)
            if kind == 2:
                ordinary = getattr(service, 'ordinary_party', None)
                solo = getattr(service, 'native_solo_party', None)
                if ordinary is not None and ordinary.uses(entry):
                    for party in service.ordinary_party._parties.values():
                        if party.entry is entry and any(seat.session is session and seat.epoch is epoch for seat in party.members):
                            return ('party', party.entry.domain, party.generation, party.party_id)
                elif solo is not None and solo.ready(entry):
                    party = service.native_solo_party._parties.get(id(session))
                    if party is not None:
                        return ('solo', entry.domain, party.party_id)
            raise RaidError('actual ordinary-party chat membership unavailable')

    def route(self, opcode, raw, session):
        directory = self._directory()
        sender = directory.chat_selection(session, sender=True)
        record = self._record(sender)
        if record is None:
            raise RaidError('chat selected character is not owned by this account')
        if opcode == 0x298:
            raw = bytes(raw)
            if not raw or raw[0] > 100:
                raise ValueError('invalid raid message kind')
            message, pos = _string(raw, 1, 900)
            if pos >= len(raw) or raw[pos] > 100:
                raise ValueError('invalid raid message suffix')
            _end(raw, pos + 1)
            kind, scope = 0x30, self._scope(session, 0x30)
            if scope[1].leader_id != sender[4]:
                raise RaidError('raid notice requires the current leader')
            out_opcode = 0x24d
            out_body = bytes([raw[0]]) + dstring(bytes(record.name_bytes) + b': ' + message)
        else:
            kind, target, context, message, name, aux = parse_message(raw)
            if kind in (1, 0x1f):
                scope = ('whisper', target, name)
                try:
                    message.decode('utf-8', 'strict')
                except UnicodeDecodeError:
                    message = message.decode('cp936', 'strict').encode('utf-8')
                    if len(message) > 1020:
                        raise ValueError('whisper text exceeds the native display limit')
            elif kind in (0, 2, 3, 0x2f, 0x30):
                scope = self._scope(session, kind)
                if kind == 0x30 and scope[1].leader_id != sender[4]:
                    raise RaidError('raid leader chat requires current leadership')
            else:
                raise ValueError('unsupported native chat scope')
            out_opcode = 0x41
            out_body = bytes([kind, 0]) + dstring(bytes(record.name_bytes)) + bytes([aux]) + dstring(message)
            if kind == 0x1f:
                out_body += dstring(name)
        recipients = []
        for recipient in directory.chat_selections():
            if recipient[3] != sender[3]:
                continue
            peer = recipient[0].session
            if scope[0] == 'whisper':
                other = self._record(recipient)
                if (other is not None and recipient != sender
                        and (not scope[1] or recipient[4] == scope[1])
                        and whisper_name_matches(bytes(other.name_bytes), scope[2])):
                    recipients.append(recipient)
            else:
                try:
                    if self._scope(peer, kind) == scope:
                        recipients.append(recipient)
                except RaidError:
                    continue
        if scope[0] == 'whisper':
            if len(recipients) != 1:
                raise RaidError('current owned whisper target unavailable or ambiguous')
            # Main writer01F08DE0 renders outgoing kind1 immediately. Echoing
            # it as an incoming whisper duplicates text and reverses its label.
            if kind != 1:
                recipients.append(sender)
        with self._lock:
            live = {row[0] for row in directory.chat_selections()}
            for connection in tuple(self._queues):
                if connection not in live:
                    self._queues.pop(connection, None)
            # Recipient must consume only messages bound to this exact selection.
            for recipient in recipients:
                queue = self._queues.setdefault(recipient[0], deque(maxlen=128))
                queue.append(_Message(sender, recipient, scope, out_opcode, out_body))
        packets = [self.server._codec_response(1, 0x11, b'\x01')] if opcode == 0x11 else []
        return packets, 'native chat queued recipients=%d' % len(recipients)

    def pump(self, session, client):
        directory = self._directory()
        try:
            current = directory.chat_selection(session, sender=True)
        except RaidError:
            with self._lock:
                for connection in tuple(self._queues):
                    if connection.session is session:
                        self._queues.pop(connection, None)
            return
        if current[0].client is not client:
            raise RaidError('chat socket is not owned by the recipient selection')
        with self._lock:
            rows = list(self._queues.pop(current[0], ()))
        for row in rows:
            if row.recipient != current:
                continue
            try:
                if directory.chat_selection(row.sender[0].session) != row.sender:
                    continue
                if row.scope[0] not in ('whisper',):
                    kind = {'town': 0, 'dungeon_self': 0, 'raid': 0x2f, 'party': 2, 'solo': 2, 'normal_party': 2}[row.scope[0]]
                    if self._scope(session, kind) != row.scope or self._scope(row.sender[0].session, kind) != row.scope:
                        continue
            except RaidError:
                continue
            # Codec and socket send run on the recipient account's own Game thread.
            client.sendall(self.server._codec_response(0, row.opcode, row.body))


def service_for(server):
    with _LOCK:
        value = getattr(server, 'native_chat', None)
        if value is None:
            value = NativeChat(server)
            server.native_chat = value
        return value


def route(server, opcode, raw, session):
    try:
        return service_for(server).route(opcode, raw, session)
    except (RaidError, ValueError) as exc:
        packets = [server._codec_response(1, 0x11, b'\x00\x13')] if opcode == 0x11 else []
        return packets, 'native chat rejected: ' + str(exc)


def pump(server, session, client):
    value = getattr(server, 'native_chat', None)
    if value is not None:
        value.pump(session, client)
