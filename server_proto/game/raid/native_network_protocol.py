"""Pinned-US network layouts; parsing client claims grants no endpoint authority.

027630A0 writes C0002, 016725F0 reads N000B, 027640A0 reads the
seven-byte NAT observation. N000B row+12 is the actor+C connection key;
row+17 is actor+10, consumed as the peer's transport MTU. The server's
connection-key producer and NAT kinds remain unresolved.
These codecs do not establish PARTY_INFO delivery or working UDP combat.

N000B's public port is network order; C0002 reports a host-order u16.
016725F0 swaps that port into actor+8, 02769930 copies it into the
peer transport, and 02D68EB0 swaps it into sendto's sockaddr. Keep the
Python endpoint port host order and encode only this field big endian.
"""
from dataclasses import dataclass, field
from ipaddress import IPv4Address
import struct


def _uint(value, bits, name):
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError('invalid ' + name)
    return value


def _frame(body, logical, unit):
    if type(body) is not bytes or len(body) not in (logical, logical + (-logical % unit)):
        raise ValueError('invalid logical body or native codec alignment')


@dataclass(frozen=True)
class UdpAddressReport:
    nat_kind: int
    local_ipv4_u32: int
    public_ipv4_u32: int
    public_port: int
    mtu: int
    description_bytes: bytes
    frame: bytes = field(repr=False, compare=False)


def parse_udp_address_report(body):
    """C0002: u8, u32, u32, u16, u32, byte-counted narrow string.

    IP words retain native scalar representation. No byte swapping or
    ownership inference is made from these client-controlled fields.
    """
    if type(body) is not bytes or len(body) < 19:
        raise ValueError('truncated C0002 UDP address report')
    size = struct.unpack_from('<I', body, 15)[0]
    logical = 19 + size
    _frame(body, logical, 16)
    return UdpAddressReport(*struct.unpack_from('<BIIHI', body), body[19:logical], body)


@dataclass(frozen=True)
class PeerAddress:
    """Endpoint fields, with public_port expressed as the host-order number."""
    user_id: int
    local_ipv4_u32: int
    public_ipv4_u32: int
    public_port: int
    connection_key: int
    nat_kind: int
    mtu: int
    weakness_percent: int

    def wire(self):
        if type(self.user_id) is not int or not 1 <= self.user_id <= 65534:
            raise ValueError('native peer UID must identify one actual selected actor')
        fields = (self.user_id, _uint(self.local_ipv4_u32, 32, 'local IP word'),
            _uint(self.public_ipv4_u32, 32, 'public IP word'),
            _uint(self.public_port, 16, 'public port'), _uint(self.connection_key, 32, 'connection key'),
            _uint(self.nat_kind, 8, 'NAT kind'), _uint(self.mtu, 32, 'peer MTU'),
            _uint(self.weakness_percent, 8, 'weakness'))
        return (struct.pack('<HII', *fields[:3]) + struct.pack('>H', fields[3])
                + struct.pack('<IBIB', *fields[4:]))


def encode_peer_addresses(peers):
    """N000B has a u8 count and 22 bytes per row (016725F0).

    The native count is u8. Group membership and endpoint authority belong
    to the producer; this helper cannot authorize or silently filter rows.
    """
    peers = tuple(peers)
    if len(peers) > 255 or any(type(row) is not PeerAddress for row in peers):
        raise ValueError('invalid native peer rows')
    rows = [row.wire() for row in peers]
    if len({row.user_id for row in peers}) != len(peers):
        raise ValueError('ambiguous native peer UID')
    return bytes([len(peers)]) + b''.join(rows)


def parse_peer_addresses(body):
    if type(body) is not bytes or not body:
        raise ValueError('missing native peer count')
    _frame(body, 1 + body[0] * 22, 8)
    peers = tuple(PeerAddress(*struct.unpack_from('<HII', body, offset),
                  struct.unpack_from('>H', body, offset + 10)[0],
                  *struct.unpack_from('<IBIB', body, offset + 12))
                  for offset in range(1, 1 + body[0] * 22, 22))
    encode_peer_addresses(peers)
    return peers


def build_nat_observation(peer_ip, peer_port):
    """The observed recvfrom endpoint, never an IP/port taken from a payload.

    This is a reply body only. Caller authentication, rate limits, source
    socket selection and native probe modes need a transport implementation.
    """
    _uint(peer_port, 16, 'observed UDP port')
    if not peer_port:
        raise ValueError('missing observed UDP port')
    # 027640A0 calls ntohl then uses the result as inet_addr's native DWORD
    # (including passing it straight to inet_ntoa). Reverse the four octets
    # on wire; the native byte swap restores the sockaddr representation.
    return b'\x02' + IPv4Address(peer_ip).packed[::-1] + struct.pack('>H', peer_port)
