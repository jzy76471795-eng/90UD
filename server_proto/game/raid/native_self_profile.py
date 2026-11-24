"""Selected self weakness from actual server producers and successful sends.

No constructor fallback, client claim or complete peer resolver lives here.
NativeLobby calls note_send only from its reviewed post-send boundary.
"""
from dataclasses import dataclass
from hashlib import sha256
import struct

from game.character.native_profile_projection import (userinfo0_projection, encoded_profile,
    Userinfo0Projection, Userinfo1Projection)
from .runtime import RaidError


@dataclass(frozen=True)
class SentWeakness:
    actor: object
    packet: bytes
    character_id: int
    value: int
    source: str
    body_sha256: str


@dataclass(frozen=True)
class _Prepared:
    actor: object
    packet: bytes
    body: bytes
    projection: object
    source: str


@dataclass(frozen=True)
class _CapturedProfile:
    owner: object
    actor: object
    packet: bytes
    descriptor: object


class SentSelfProfile:
    def __init__(self, directory):
        self.directory = directory
        self._pending, self._models, self._values = {}, {}, {}

    def prepare(self, actor, packet, body, source):
        projection = userinfo0_projection(body)
        if (isinstance(packet, bytes) and projection is not None
                and projection.character_id == actor.character_id):
            self._pending[(id(actor.session), id(packet))] = _Prepared(actor, packet, body, projection, source)

    def capture_send(self, session, packet):
        """Before sendall: bind the exact encoded producer to this selection."""
        descriptor = encoded_profile(packet)
        if descriptor is None:
            return None
        with self.directory._lock:
            try:
                actor = self.directory._own(session, town=False)
            except RaidError:
                pending = self.directory._pending_self.get(id(session))
                if pending is None or pending[1] is not packet:
                    return None
                actor = self.directory._valid(pending[0])
            if (descriptor.projection.character_id != actor.character_id
                    or descriptor.body[3] != session._raid_native_namespace):
                return None
            return _CapturedProfile(self, actor, packet, descriptor)

    def _captured(self, actor, packet, body, receipt):
        if (type(receipt) is not _CapturedProfile or receipt.owner is not self
                or receipt.actor is not actor or receipt.packet is not packet
                or encoded_profile(packet) is not receipt.descriptor):
            raise ValueError('profile model lacks its selected pre-send capture')
        descriptor = receipt.descriptor
        logical = self._frame(body, descriptor.projection.logical_size)
        if (logical != descriptor.body or descriptor.projection.character_id != actor.character_id
                or logical[3] != actor.session._raid_native_namespace):
            raise ValueError('captured codec output differs from its factory model')
        return descriptor

    def proof(self, session):
        with self.directory._lock:
            actor = self.directory._own(session, town=False)
            value = self._values.get(id(session))
            if value is None or value.actor is not actor:
                raise RaidError('actual sent self weakness is unavailable')
            return value

    def weakness(self, session):
        return self.proof(session).value

    def forget_session(self, session):
        with self.directory._lock:
            self._values.pop(id(session), None)
            self._models.pop(id(session), None)
            for key in tuple(self._pending):
                if key[0] == id(session):
                    self._pending.pop(key, None)

    @staticmethod
    def _plain(packet):
        from game.game_codec import decode_payload_skeleton
        from game.game_server import _s2c_plain_wire_enabled, _plain_wire_header_mode
        from game.game_wire import parse_game_header, game_packet_body
        header = parse_game_header(packet)
        if header.length != len(packet):
            raise ValueError('native profile packet has a partial or extra frame')
        plain_wire = _s2c_plain_wire_enabled()
        body = game_packet_body(packet, header, compact_prefix=plain_wire and _plain_wire_header_mode() == '13')
        if body and not plain_wire:
            decoded = decode_payload_skeleton(header.opcode, body)
            if not decoded.ok:
                raise ValueError('native profile codec failed')
            body = decoded.plain
        return header.flag_or_magic, header.opcode, body

    @staticmethod
    def _frame(body, logical):
        # The relevant current codecs use 8/16-byte zero padding. Never strip
        # arbitrary trailing zeroes from the actual fields themselves.
        if (len(body) not in {logical, (logical + 7) // 8 * 8, (logical + 15) // 16 * 16}
                or any(body[logical:])):
            raise ValueError('unsupported native profile body length')
        return body[:logical]

    def _commit(self, actor, packet, body, value, source):
        self._values[id(actor.session)] = SentWeakness(actor, packet, actor.character_id,
            value, source, sha256(body).hexdigest())

    def _model(self, actor, packet, body, receipt):
        prepared = self._pending.get((id(actor.session), id(packet)))
        if prepared is None:
            prepared = self._models.get(id(actor.session))
        if prepared is None or prepared.actor is not actor or prepared.packet is not packet:
            descriptor = self._captured(actor, packet, body, receipt)
            if type(descriptor.projection) is not Userinfo0Projection:
                raise ValueError('self model is not the reviewed USERINFO0 producer')
            prepared = _Prepared(actor, packet, descriptor.body, descriptor.projection, 'encoded-userinfo0')
        if prepared.projection.character_id != actor.character_id:
            raise ValueError('self model lacks its actual factory packet receipt')
        logical = self._frame(body, prepared.projection.logical_size)
        if logical != prepared.body:
            raise ValueError('self model packet differs from its producer body')
        value = logical[prepared.projection.weakness_offset]
        if value != prepared.projection.weakness:
            raise ValueError('self model differs from its typed field projection')
        self._models[id(actor.session)] = prepared
        self._commit(actor, packet, logical, value, prepared.source)
        self._pending.pop((id(actor.session), id(packet)), None)
        return prepared.body

    def note_send(self, session, packet, receipt=None):
        # A tuple codec fixture or equal bytes cannot attest the original
        # encoded factory packet. The transport must call after sendall.
        if not isinstance(packet, bytes):
            return
        if len(packet) < 3:
            return
        flag, opcode = packet[0], int.from_bytes(packet[1:3], 'little')
        relevant = (flag == 0 and opcode in (2, 7, 11, 0x99, 0x247, 0x2f4)
                    or flag == 1 and opcode in (7, 0x158))
        if not relevant:
            return
        with self.directory._lock:
            try:
                actor = self.directory._own(session, town=False)
            except RaidError:
                self.forget_session(session)
                return
            try:
                flag, opcode, body = self._plain(packet)
                if flag == 1 and opcode == 7:
                    raise ValueError('return-selection acknowledgement revokes self profile')
                if flag == 0 and opcode == 2:
                    if (len(body) >= 103 and body[:4] == b'\x03\x01\x00\x00'
                            and 1 <= int.from_bytes(body[10:12], 'little') <= 65534
                            and int.from_bytes(body[16:20], 'little') == 83):
                        # USERINFO3 is the C0008 inspection response. Its
                        # combat block does not write selected self weakness.
                        return
                    if (len(body) >= 39 and body[:3] == b'\x00\x01\x00'
                            and (body[3] != session._raid_native_namespace
                                 or int.from_bytes(body[37:39], 'little') != actor.character_id)):
                        return  # Another namespace/subject model is not self.
                    if len(body) >= 24 and body[:3] == b'\x01\x01\x00':
                        descriptor = encoded_profile(packet)
                        if descriptor is not None and type(descriptor.projection) is Userinfo1Projection:
                            if (descriptor.projection.character_id != actor.character_id
                                    or descriptor.projection.namespace != session._raid_native_namespace):
                                return  # The reviewed model belongs to another actor/namespace.
                            self._captured(actor, packet, body, receipt)
                            # Native 018D3A70 reuses the existing actor-info profile.
                            # It carries no weakness input and does not establish a value.
                            return
                    return self._model(actor, packet, body, receipt)
                # A subsequent notice cannot substitute for the initial owned
                # factory model; unknown/revoked state awaits another model.
                previous = self._values.get(id(session))
                if previous is None or previous.actor is not actor:
                    return
                value = None
                if flag == 0 and opcode == 0x99:
                    if not body:
                        raise ValueError('missing profile count')
                    body = self._frame(body, 1 + body[0] * 5)
                    for offset in range(1, len(body), 5):
                        uid, weakness, _a, _b = struct.unpack_from('<HBBB', body, offset)
                        if not 1 <= uid <= 65534:
                            raise ValueError('invalid profile subject')
                        if uid == actor.character_id:
                            value = weakness
                elif flag == 0 and opcode == 7:
                    if len(body) < 3 or body[2] not in (0, 10, 12):
                        raise ValueError('unsupported profile notification kind')
                    body = self._frame(body, 13 if body[2] == 10 else 17)
                    uid = int.from_bytes(body[:2], 'little')
                    if uid == actor.character_id:
                        value = int.from_bytes(body[9:11], 'little')
                elif flag == 0 and opcode == 0x2f4:
                    body = self._frame(body, 50)
                    uid = int.from_bytes(body[:2], 'little')
                    if not 1 <= uid <= 65534:
                        raise ValueError('invalid profile model subject')
                    if uid == actor.character_id and body[4] == session._raid_native_namespace:
                        value = int.from_bytes(body[12:14], 'little')
                elif flag == 0 and opcode in (11, 0x247):
                    if opcode == 11:
                        if not body:
                            raise ValueError('missing peer count')
                        body = self._frame(body, 1 + body[0] * 22)
                        offsets = range(1, len(body), 22)
                    else:
                        body = self._frame(body, 22)
                        offsets = (0,)
                    for offset in offsets:
                        uid = int.from_bytes(body[offset:offset + 2], 'little')
                        if uid == actor.character_id:
                            value = body[offset + 21]
                elif flag == 1 and opcode == 0x158:
                    if not body or body[0] not in (0, 1):
                        raise ValueError('unsupported peer acknowledgement')
                    if body[0] == 0:
                        return  # The native failed branch does not write it.
                    body = self._frame(body, 24)
                    if int.from_bytes(body[2:4], 'little') == actor.character_id:
                        value = body[23]
                if value is not None:
                    self._commit(actor, packet, body, value, 'S%d/%04X' % (flag, opcode))
            except Exception as exc:
                # Unknown affected layouts never leave an older value looking
                # authoritative. Recovery requires a real producer model send.
                if self._values.pop(id(session), None) is not None:
                    logger = getattr(self.directory.service.server, 'log', None)
                    if callable(logger):
                        logger('[RaidProfile] cid=%s self receipt revoked by S%d/%04X (%s)'
                               % (actor.character_id, flag, opcode, type(exc).__name__))
