"""Typed provenance for the actual USERINFO0 factory's native profile byte.

The stamp is server-only metadata and is never serialized. Converting to
ordinary bytes deliberately removes its producer authority.
"""
from dataclasses import dataclass

_FACTORY = object()


@dataclass(frozen=True)
class Userinfo0Projection:
    character_id: int
    weakness_offset: int
    weakness: int
    logical_size: int


@dataclass(frozen=True)
class Userinfo1Projection:
    character_id: int
    namespace: int
    logical_size: int


@dataclass(frozen=True)
class EncodedProfile:
    body: bytes
    projection: object


class _ProducedUserinfo0(bytes):
    def __new__(cls, raw, projection, token):
        if token is not _FACTORY:
            raise TypeError('actual USERINFO0 factory stamp required')
        value = super().__new__(cls, raw)
        object.__setattr__(value, '_projection', projection)
        return value

    def __setattr__(self, name, value):
        raise AttributeError('immutable USERINFO0 producer projection')


def _stamp_userinfo0(body, *, character_id, weakness_offset):
    if (type(body) is not bytes or type(character_id) is not int
            or len(body) < 39 or body[:4] != b'\x00\x01\x00\x00'
            or int.from_bytes(body[37:39], 'little') != character_id
            or type(weakness_offset) is not int or not 39 <= weakness_offset < len(body)):
        raise ValueError('actual USERINFO0 projection does not match its model')
    projection = Userinfo0Projection(character_id, weakness_offset, body[weakness_offset], len(body))
    return _ProducedUserinfo0(body, projection, _FACTORY)


def userinfo0_projection(body):
    return body._projection if type(body) is _ProducedUserinfo0 else None


class _ProducedUserinfo1(bytes):
    def __new__(cls, raw, projection, token):
        if token is not _FACTORY:
            raise TypeError('actual USERINFO1 factory stamp required')
        value = super().__new__(cls, raw)
        object.__setattr__(value, '_projection', projection)
        return value

    def __setattr__(self, name, value):
        raise AttributeError('immutable USERINFO1 producer projection')


def _stamp_userinfo1(body, *, character_id):
    if (type(body) is not bytes or type(character_id) is not int
            or len(body) < 139 or body[:3] != b'\x01\x01\x00'
            or int.from_bytes(body[22:24], 'little') != character_id):
        raise ValueError('actual USERINFO1 projection does not match its model')
    projection = Userinfo1Projection(character_id, body[3], len(body))
    return _ProducedUserinfo1(body, projection, _FACTORY)


class _EncodedProfilePacket(bytes):
    def __new__(cls, raw, descriptor, token):
        if token is not _FACTORY:
            raise TypeError('actual profile codec producer required')
        value = super().__new__(cls, raw)
        object.__setattr__(value, '_descriptor', descriptor)
        return value

    def __setattr__(self, name, value):
        raise AttributeError('immutable encoded profile provenance')


def stamp_encoded_profile(packet, flag, opcode, body):
    """Exact codec output carries factory metadata; wire bytes are unchanged."""
    if flag == 0 and opcode == 2 and type(body) in (_ProducedUserinfo0, _ProducedUserinfo1):
        return _EncodedProfilePacket(packet, EncodedProfile(body, body._projection), _FACTORY)
    return packet


def encoded_profile(packet):
    return packet._descriptor if type(packet) is _EncodedProfilePacket else None
