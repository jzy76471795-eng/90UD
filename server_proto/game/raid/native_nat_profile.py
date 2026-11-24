"""Configured native NAT observation endpoints for initial CHANNELINFO.

Configuration and successful UDP listener startup are not an observed peer,
source ownership, or combat readiness. No client payload supplies these
addresses. The finite profile has one configured primary IPv4, two actual
observation ports, and an empty alternate pool.
"""
from dataclasses import dataclass
from ipaddress import IPv4Address
import os

from .native_udp import NativeUdpProbeService, _ipv4, native_udp_handler_for_game


@dataclass(frozen=True, eq=False)
class _ConfiguredProfile:
    server: object
    manager: object
    service: NativeUdpProbeService
    primary_ipv4: str
    port1: int
    port2: int


def _same_handler(handler, service):
    return (getattr(handler, '__self__', None) is service
            and getattr(handler, '__func__', None) is NativeUdpProbeService.handle_datagram)


def _bindings_current(profile):
    service, manager = profile.service, profile.manager
    if (getattr(profile.server, 'native_udp_probe_service', None) is not service
            or service.ports != frozenset((profile.port1, profile.port2))
            or service.keepalive_port != profile.port2
            or not _same_handler(getattr(manager, 'udp_datagram_handler', None), service)):
        return False
    for port in (profile.port1, profile.port2):
        listener = getattr(manager, 'udp_servers', {}).get(port)
        if (listener is None or getattr(listener, 'port', None) != port
                or not _same_handler(getattr(listener, 'datagram_handler', None), service)):
            return False
        try:
            bound = IPv4Address(listener.host)
            if not bound.is_unspecified and str(bound) != profile.primary_ipv4:
                return False
            if (not listener._ready.is_set() or listener._failed.is_set()
                    or listener._stop.is_set() or not listener.thread.is_alive()):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
    return True


def install_profile(server, manager, *, listen_ports, environment=None):
    """Run after start.py resolves/starts UDP ports, before Game threads start.

    listen_ports is that startup's resolved extra_udp_ports (including the
    DEFAULT_EXTRA_UDP_PORTS policy when applicable), never a second guessed
    port list. Unsupported explicit configuration fails before publication.
    Missing advertise IP or a disabled native probe leaves the peer domain
    unset, preserving the pure clock helper's existing behavior.
    """
    if server is None:
        return None
    server._native_channelinfo_nat_profile = None
    handler = native_udp_handler_for_game(server)
    if handler is None:
        return None
    env = os.environ if environment is None else environment
    text = env.get('USDOF_GAME_SERVER_IP', '').strip()
    if not text:
        return None
    primary = _ipv4(text)
    try:
        ports = (int(env.get('USDOF_GAME_INITIAL_UDP_PORT1', '12311'), 0),
                 int(env.get('USDOF_GAME_INITIAL_UDP_PORT2', '12312'), 0))
        planned = tuple(listen_ports)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid configured native observation port policy') from exc
    if (any(type(p) is not int or not 1 <= p <= 65535 for p in ports + planned)
            or ports[0] == ports[1] or not set(ports) <= set(planned)):
        raise ValueError('native observation ports must match actual startup listener policy')
    service = handler.__self__
    if not isinstance(service, NativeUdpProbeService):
        raise ValueError('configured native observation service unavailable')
    profile = _ConfiguredProfile(server, manager, service, primary, *ports)
    if not _bindings_current(profile):
        raise ValueError('native observation listeners are not bound to the configured probe service')
    server._native_channelinfo_nat_profile = profile
    return profile


def profile_for(server):
    """A current configuration snapshot only; this does not create a probe."""
    profile = getattr(server, '_native_channelinfo_nat_profile', None)
    if (isinstance(profile, _ConfiguredProfile) and profile.server is server
            and _bindings_current(profile)):
        return profile
    return None


def configured_fields(profile):
    """Return only the finite fields proven in the frozen native consumer."""
    if not isinstance(profile, _ConfiguredProfile) or profile_for(profile.server) is not profile:
        raise ValueError('current startup-owned native observation profile required')
    return (profile.primary_ipv4.encode('ascii'),), (), profile.port1, profile.port2
