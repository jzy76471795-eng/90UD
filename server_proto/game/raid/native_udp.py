"""Basic native NAT probes correlated to live Game TCP connections.

An observed source matching C0002 is not cryptographic endpoint ownership,
party publication, or combat readiness. Native mode5 zero-byte keepalives can
refresh an existing unique correlation, without a reply. Probe bytes5/6,
reliable combat and relay transport are not answered by this probe handler.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from math import isfinite
import os
from threading import RLock
from time import monotonic

from .native_network_protocol import UdpAddressReport, build_nat_observation

_FACTORY_LOCK = RLock()
OBSERVATION_SECONDS = 30  # Server lease policy, not a native protocol enum.
OBSERVATIONS_PER_IP = 32
PROBE_BURST = 8
PROBES_PER_SECOND = 4


def _enabled():
    # Basic address observations serve ordinary parties as well as raids.
    # Keep endpoint correlation and combat authorization as separate gates.
    return os.environ.get('USDOF_NATIVE_UDP_PROBES', '1').strip().lower() in (
        '1', 'true', 'yes', 'on'
    )


def _ipv4(text):
    if type(text) is not str:
        raise ValueError('native UDP requires an IPv4 source')
    ip = IPv4Address(text)
    if ip.is_unspecified or ip.is_multicast or int(ip) == 0xffffffff:
        raise ValueError('invalid native UDP source')
    return str(ip)


def native_ipv4_word(text):
    """inet_addr's x86 DWORD, subsequently sent unchanged by C0002."""
    return int.from_bytes(IPv4Address(_ipv4(text)).packed, 'little')


def _login_session_id(session):
    # GameSessionState receives this only from the authenticated LoginIdentity,
    # never from C0002/UDP. Builtin/legacy connections cannot inherit leases.
    value = getattr(session, 'login_session_id', None)
    return value if (type(value) is str and len(value) == 32
                     and all(c in '0123456789abcdef' for c in value)) else None


@dataclass(eq=False)
class _Connection:
    session: object
    account_id: int
    remote_ip: str
    report: object = None
    login_session_id: object = field(default=None, repr=False)
    continuable: bool = False
    latest_report: object = None


@dataclass(frozen=True)
class UdpAddressClaim:
    account_id: int
    report: UdpAddressReport


@dataclass(frozen=True)
class _Observation:
    ip: str
    port: int
    listener_port: int
    seen_at: float
    eligible_connections: tuple
    request_probe: bool = True


@dataclass(frozen=True)
class UdpSourceMatch:
    account_id: int
    ip: str
    port: int
    listener_port: int
    seen_at: float
    report: UdpAddressReport
    continuity_port: object = None
    continuity_listener_port: object = None
    continuity_seen_at: object = None


@dataclass(frozen=True)
class _DestinationMapping:
    connection: _Connection
    report: UdpAddressReport
    primary: _Observation
    secondary: _Observation


class NativeUdpProbeService:
    def __init__(self, *, ports=None, clock=monotonic):
        if ports is None:
            ports = (int(os.environ.get('USDOF_GAME_INITIAL_UDP_PORT1', '12311'), 0),
                     int(os.environ.get('USDOF_GAME_INITIAL_UDP_PORT2', '12312'), 0))
        ports = tuple(ports)
        if not ports or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
            raise ValueError('invalid native NAT listening ports')
        self.ports = frozenset(ports)
        self.keepalive_port = ports[1] if len(ports) > 1 else None
        self.clock = clock
        self._lock = RLock()
        self._connections, self._observations, self._buckets = {}, {}, {}
        self._destination_mappings = {}
        # Historical identity only, never returned by current_match. A fresh
        # same-port heartbeat may recover after loss on this exact live TCP.
        self._same_port_owners = {}
        # Conflicts suspend old ownership. Closure alone cannot release it;
        # every conflicting account must establish a different real endpoint.
        self._same_port_conflicts = {}
        self._probe_replies = {}

    def _sole_ip_connection(self, connection):
        return [c for c in self._connections.values()
                if self._current(c) and c.remote_ip == connection.remote_ip] == [connection]

    def _mapping_current(self, mapping, now):
        c, report = mapping.connection, mapping.report
        return (self._current(c) and c.report is report and report.nat_kind in (2,3,4,5,6)
                and not (c.latest_report is not None
                    and c.latest_report.public_ipv4_u32 == native_ipv4_word(c.remote_ip)
                    and c.latest_report.public_port != report.public_port)
                and self._sole_ip_connection(c)
                and report.public_ipv4_u32 == native_ipv4_word(c.remote_ip)
                and mapping.primary.ip == mapping.secondary.ip == c.remote_ip
                and mapping.primary.port == report.public_port
                and mapping.primary.port != mapping.secondary.port
                and mapping.primary.eligible_connections == (c,)
                and mapping.secondary.eligible_connections == (c,)
                and 0 <= now - mapping.secondary.seen_at < OBSERVATION_SECONDS)

    def _bind_destination_mapping(self, connection, now):
        # 027640A0 chooses kind3 when the two actual NAT replies disagree.
        # C0002 still reports the first reply's port; mode5 keeps the second
        # destination alive. Associate only a fresh, unambiguous pair for one
        # live authenticated Game TCP. A zero byte can never establish this.
        report = connection.report
        if (report is None or report.nat_kind not in (2,3,4,5,6) or not self._current(connection)
                or id(connection.session) in self._destination_mappings
                or not self._sole_ip_connection(connection)
                or report.public_ipv4_u32 != native_ipv4_word(connection.remote_ip)):
            return
        primary = [o for o in self._observations.values()
                   if o.ip == connection.remote_ip and o.port == report.public_port
                   and o.listener_port != self.keepalive_port
                   and o.request_probe
                   and o.eligible_connections == (connection,)
                   and 0 <= now - o.seen_at < OBSERVATION_SECONDS]
        secondary = [o for o in self._observations.values()
                     if o.ip == connection.remote_ip and o.port != report.public_port
                     and o.listener_port == self.keepalive_port
                     and o.request_probe
                     and o.eligible_connections == (connection,)
                     and 0 <= now - o.seen_at < OBSERVATION_SECONDS]
        if (len(primary) == len(secondary) == 1
                and abs(primary[0].seen_at - secondary[0].seen_at) <= 5):
            self._destination_mappings[id(connection.session)] = _DestinationMapping(
                connection, report, primary[0], secondary[0])

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not isfinite(now) or now <= 0:
            raise ValueError('trusted UDP probe clock unavailable')
        return now

    def _current(self, connection):
        return (self._connections.get(id(connection.session)) is connection
            and type(getattr(connection.session, 'account_id', None)) is int
            and connection.session.account_id == connection.account_id
            and (connection.login_session_id is None
                 or _login_session_id(connection.session) == connection.login_session_id))

    @staticmethod
    def _closed_owner(connection, observation):
        report = connection.report
        return (connection.continuable and connection.login_session_id is not None
                and report is not None and connection.remote_ip == observation.ip
                and report.public_ipv4_u32 == native_ipv4_word(observation.ip)
                and report.public_port == observation.port)

    def _inherit_observations(self, connection, report=None):
        report = connection.report if report is None else report
        if (connection.login_session_id is None or report is None
                or report.public_ipv4_u32 != native_ipv4_word(connection.remote_ip)):
            return
        for key, observation in tuple(self._observations.items()):
            if observation.port != report.public_port:
                continue
            if any(self._closed_owner(old, observation)
                   and old.account_id == connection.account_id
                   and old.login_session_id == connection.login_session_id
                   and old.remote_ip == connection.remote_ip
                   for old in observation.eligible_connections):
                # Preserve the actual receive time. The new TCP has already
                # supplied its own C0002 for this exact observed endpoint.
                self._observations[key] = _Observation(
                    observation.ip, observation.port, observation.listener_port,
                    observation.seen_at, (connection,), observation.request_probe)

    def _unique_claim(self, ip, port):
        matches = [c for c in self._connections.values() if self._current(c)
                   and self._claims_endpoint(c, ip, port)]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _claims_endpoint(connection, ip, port):
        # A pending authenticated claim still participates in ambiguity checks;
        # it cannot authorize publication until a real observation exists.
        return connection.remote_ip == ip and any(
            r is not None and r.public_ipv4_u32 == native_ipv4_word(ip)
            and r.public_port == port for r in (connection.report, connection.latest_report))

    def _promote_report(self, connection, now):
        report = connection.latest_report
        if (report is None or report.public_ipv4_u32 != native_ipv4_word(connection.remote_ip)
                or self._unique_claim(connection.remote_ip, report.public_port) is not connection):
            return False
        if connection.report is report:
            return True
        self._inherit_observations(connection, report)
        observed = any(o.ip == connection.remote_ip and o.port == report.public_port
            and connection in o.eligible_connections and 0 <= now-o.seen_at < OBSERVATION_SECONDS
            for o in self._observations.values())
        mapping = self._destination_mappings.get(id(connection.session))
        # An already validated destination-dependent mapping may still be live
        # through its secondary heartbeat. Replacing a report never extends it.
        continuation = bool(mapping is not None and self._mapping_current(mapping, now)
            and mapping.report.public_ipv4_u32 == report.public_ipv4_u32
            and mapping.report.public_port == report.public_port
            and mapping.report.nat_kind == report.nat_kind)
        if not observed and not continuation:
            return False
        connection.report = report
        connection.session._native_udp_address_claim = UdpAddressClaim(connection.account_id, report)
        if continuation:
            self._destination_mappings[id(connection.session)] = _DestinationMapping(
                connection, report, mapping.primary, mapping.secondary)
        self._bind_destination_mapping(connection, now)
        return True

    def _recover_same_port_owner(self, connection, ip, port, now):
        key = id(connection.session)
        if (self._same_port_owners.get(key) !=
                (connection, connection.report, ip, port)
                or self._unique_claim(ip, port) is not connection
                or connection.latest_report is None
                or connection.latest_report.public_ipv4_u32 != native_ipv4_word(ip)
                or connection.latest_report.public_port != port):
            return False
        for blocker, since in self._same_port_conflicts.get(key, {}).values():
            successors = [c for c in self._connections.values()
                          if self._current(c) and c.account_id == blocker.account_id
                          and c.remote_ip == blocker.remote_ip]
            if len(successors) != 1:
                return False
            successor = successors[0]
            report = successor.report
            if (report is None or report.public_ipv4_u32 != native_ipv4_word(ip)
                    or report.public_port == port
                    or self._same_port_conflicts.get(id(successor.session))
                    or self._unique_claim(ip, report.public_port) is not successor
                    or not any(o.ip == ip and o.port == report.public_port
                               and successor in o.eligible_connections
                               and since <= o.seen_at <= now
                               and now - o.seen_at < OBSERVATION_SECONDS
                               for o in self._observations.values())):
                return False
        # Called only while handling a newly received matching heartbeat.
        # It neither refreshes an old observation nor transfers TCP ownership.
        return True

    def _direct_connection_mapping(self, observation):
        # 027640A0 chooses NAT kind1 only when local IP equals the observed
        # public IP. That native path does not send mode5 keepalives. Keep its
        # actual initial probe tied to this live TCP, not a fictitious refresh.
        connection = self._unique_claim(observation.ip, observation.port)
        return bool(connection is not None and connection in observation.eligible_connections
                    and connection.report.nat_kind == 1
                    and connection.report.local_ipv4_u32 == connection.report.public_ipv4_u32)

    def _purge(self, now):
        for key, (sent, eligible) in tuple(self._probe_replies.items()):
            if now < sent or now - sent >= 1 or not any(self._current(c) for c in eligible):
                self._probe_replies.pop(key, None)
        for key, connection in tuple(self._connections.items()):
            if not self._current(connection):
                self._connections.pop(key, None)
        for key, (connection, report, ip, port) in tuple(self._same_port_owners.items()):
            if not self._current(connection) or connection.report is not report:
                self._same_port_owners.pop(key, None)
                self._same_port_conflicts.pop(key, None)
                continue
            conflicts = [c for c in self._connections.values()
                         if c is not connection and self._current(c)
                         and self._claims_endpoint(c, ip, port)]
            if conflicts:
                blocked = self._same_port_conflicts.setdefault(key, {})
                for c in conflicts:
                    blocked.setdefault(id(c), (c, now))
                if len(blocked) > OBSERVATIONS_PER_IP:
                    self._same_port_owners.pop(key, None)
                    self._same_port_conflicts.pop(key, None)
        for key, mapping in tuple(self._destination_mappings.items()):
            if not self._mapping_current(mapping, now):
                self._destination_mappings.pop(key, None)
        active_ips = {c.remote_ip for c in self._connections.values()}
        for key, observation in tuple(self._observations.items()):
            age = now - observation.seen_at
            connection = self._unique_claim(observation.ip, observation.port)
            if (age >= 0 and connection is not None and connection.report is not None
                    and connection in observation.eligible_connections
                    and connection.report.nat_kind in (2, 4, 5, 6)):
                self._same_port_owners[id(connection.session)] = (
                    connection, connection.report, observation.ip, observation.port)
            continued = any(m.primary is observation for m in self._destination_mappings.values())
            if (age < 0 or (age >= OBSERVATION_SECONDS and not continued and not self._direct_connection_mapping(observation))
                    or not any(self._current(c) or self._closed_owner(c, observation)
                               for c in observation.eligible_connections)):
                self._observations.pop(key, None)
        for ip in tuple(self._buckets):
            if ip not in active_ips:
                self._buckets.pop(ip, None)

    def open_connection(self, session, remote_ip, account_id):
        with self._lock:
            self._purge(self._now())
            if (type(account_id) is not int or account_id <= 0
                    or type(getattr(session, 'account_id', None)) is not int
                    or session.account_id != account_id or id(session) in self._connections):
                raise ValueError('owned native UDP TCP connection unavailable or already registered')
            connection = _Connection(session, account_id, _ipv4(remote_ip),
                                     login_session_id=_login_session_id(session))
            self._connections[id(session)] = connection
            # A same-IP connection that arrives and immediately closes must
            # not silently restore an earlier sole-owner association.
            self._purge(self._now())
            return connection

    def close_connection(self, connection):
        with self._lock:
            if self._connections.get(id(connection.session)) is connection:
                report = connection.report
                claimants = ([c for c in self._connections.values()
                    if self._current(c) and c.remote_ip == connection.remote_ip
                    and c.report is not None
                    and c.report.public_ipv4_u32 == native_ipv4_word(connection.remote_ip)
                    and c.report.public_port == report.public_port] if report is not None else [])
                connection.continuable = bool(
                    self._current(connection) and connection.login_session_id is not None
                    and not (connection.latest_report is not None and report is not None
                        and connection.latest_report.public_ipv4_u32 == native_ipv4_word(connection.remote_ip)
                        and connection.latest_report.public_port != report.public_port)
                    and claimants and connection in claimants
                    and all(c.account_id == connection.account_id
                            and c.login_session_id == connection.login_session_id for c in claimants))
                if connection.continuable:
                    for key, observation in tuple(self._observations.items()):
                        if (connection in observation.eligible_connections
                                and self._closed_owner(connection, observation)):
                            # Freeze an actually correlated owner's lease.
                            # Other same-IP accounts from the initial probe's
                            # eligible set cannot inherit this closed mapping.
                            self._observations[key] = _Observation(
                                observation.ip, observation.port, observation.listener_port,
                                observation.seen_at, (connection,), observation.request_probe)
                self._connections.pop(id(connection.session), None)
            self._purge(self._now())
            # TCP close and the replacement's C0002 can race. Only the same
            # authenticated owner is eligible, including that overlap case.
            for current in self._connections.values():
                if current.login_session_id == connection.login_session_id:
                    self._promote_report(current, self._now())

    def record_claim(self, session, report):
        with self._lock:
            now = self._now()
            self._purge(now)
            connection = self._connections.get(id(session))
            if connection is None or not self._current(connection) or type(report) is not UdpAddressReport:
                return False
            connection.latest_report = report
            session._native_udp_latest_address_claim = UdpAddressClaim(connection.account_id, report)
            # Retain a report for diagnostics even before its first observation.
            # Once promoted, the effective claim is changed only by promotion.
            if connection.report is None:
                session._native_udp_address_claim = UdpAddressClaim(connection.account_id, report)
            # Re-evaluate extended direct mappings at the mutation itself.
            # Otherwise a conflicting claim could arrive and close before a
            # query, reviving an arbitrarily old direct observation unnoticed.
            self._purge(now)
            self._promote_report(connection, now)
            return True

    def _take_token(self, ip, now):
        tokens, previous = self._buckets.get(ip, (float(PROBE_BURST), now))
        if now < previous:
            return False
        tokens = min(PROBE_BURST, tokens + (now - previous) * PROBES_PER_SECOND)
        if tokens < 1:
            self._buckets[ip] = (tokens, now)
            return False
        self._buckets[ip] = (tokens - 1, now)
        return True

    def _store_observation(self, observation):
        self._observations[(observation.ip, observation.port, observation.listener_port)] = observation
        same_ip = sorted((o for o in self._observations.values() if o.ip == observation.ip),
                         key=lambda o: (o.seen_at, o.port, o.listener_port))
        for old in same_ip[:-OBSERVATIONS_PER_IP]:
            self._observations.pop((old.ip, old.port, old.listener_port), None)

    def handle_datagram(self, payload, endpoint, local_port, sendto):
        """Called with recvfrom's endpoint and the actual listening socket.

        Only request01 gets a seven-byte reply. A mode5 byte00 on the second
        port can renew a uniquely claimed, previously observed mapping. It
        cannot create a mapping or transfer TCP ownership. For a same-port
        endpoint already observed on this exact still-live TCP, a fresh
        heartbeat can recover after a receive gap; stale metadata alone never
        authorizes publication. A same-login TCP reconnect can inherit
        an unexpired observation after its own C0002, without extending
        its timestamp. No UDP payload is an authentication key.
        """
        if (not _enabled() or type(payload) is not bytes or payload not in (b'\x01', b'\x00')
                or type(local_port) is not int or local_port not in self.ports
                or type(endpoint) is not tuple or len(endpoint) != 2):
            return False
        try:
            ip, port = _ipv4(endpoint[0]), endpoint[1]
            if type(port) is not int or not 1 <= port <= 65535:
                return False
            with self._lock:
                now = self._now()
                self._purge(now)
                if payload == b'\x00':
                    mappings = [m for m in self._destination_mappings.values()
                                if m.secondary.ip == ip and m.secondary.port == port
                                and local_port == self.keepalive_port]
                    if len(mappings) == 1 and self._take_token(ip, now):
                        mapping = mappings[0]
                        observed = _Observation(ip, port, local_port, now, (mapping.connection,), False)
                        self._store_observation(observed)
                        self._destination_mappings[id(mapping.connection.session)] = _DestinationMapping(
                            mapping.connection, mapping.report, mapping.primary, observed)
                        return True
                    connection = self._unique_claim(ip, port)
                    if (local_port != self.keepalive_port or connection is None or connection.report is None
                            or connection.report.nat_kind not in (2, 3, 4, 5, 6)
                            or not ((not self._same_port_conflicts.get(id(connection.session))
                                     and any(o.ip == ip and o.port == port
                                        and connection in o.eligible_connections
                                        for o in self._observations.values()))
                                    or self._recover_same_port_owner(connection, ip, port, now))
                            or not self._take_token(ip, now)):
                        return False
                    self._same_port_conflicts.pop(id(connection.session), None)
                    self._store_observation(_Observation(ip, port, local_port, now, (connection,), False))
                    return True
                eligible = tuple(c for c in self._connections.values() if c.remote_ip == ip)
                if not eligible or not self._take_token(ip, now):
                    return False
                # The native reader does not validate the source listener for
                # each NAT stage. A rapid retry's duplicate reply can be read
                # as the next stage. Coalesce only a short same-TCP retry;
                # later retries and a new authenticated connection still reply.
                reply_key = (ip, port, local_port)
                previous = self._probe_replies.get(reply_key)
                if previous is not None and previous[1] == eligible and 0 <= now - previous[0] < .1:
                    return True
                body = build_nat_observation(ip, port)
                # The callback sends only to the observed source. Holding the
                # lock prevents a close/rebind from racing this short UDP send.
                if sendto(body, (ip, port)) != len(body):
                    return False
                eligible = tuple(c for c in eligible if self._current(c))
                if not eligible:
                    return False
                self._probe_replies[reply_key] = (now, eligible)
                for key, mapping in tuple(self._destination_mappings.items()):
                    if (mapping.secondary.ip == ip and local_port == self.keepalive_port
                            and mapping.secondary.port != port):
                        self._destination_mappings.pop(key, None)
                observed = _Observation(ip, port, local_port, now, eligible)
                self._store_observation(observed)
                for connection in eligible:
                    self._promote_report(connection, now)
                owner = self._unique_claim(ip, port)
                if owner is not None and owner in eligible:
                    # A new initial probe restores the ordinary native path.
                    self._same_port_conflicts.pop(id(owner.session), None)
                for key, mapping in tuple(self._destination_mappings.items()):
                    if (mapping.primary.ip == ip and mapping.primary.port == port
                            and mapping.primary.listener_port == local_port
                            and eligible == (mapping.connection,)):
                        self._destination_mappings[key] = _DestinationMapping(
                            mapping.connection, mapping.report, observed, mapping.secondary)
                for connection in eligible:
                    self._bind_destination_mapping(connection, now)
                return True
        except (OSError, ValueError):
            return False

    def current_claim_and_match(self, session):
        """One immutable pair: publication must not compare two generations."""
        with self._lock:
            return self.current_match(session), getattr(session, '_native_udp_address_claim', None)

    def current_match(self, session):
        """Current correlation only; never cache this as a combat capability.

        Kind3 can retain its original primary observation through a separately
        observed secondary mapping. Preserve both endpoints and receive times;
        a secondary heartbeat is not a new primary-port observation.
        """
        if not _enabled():
            return None
        with self._lock:
            self._purge(self._now())
            connection = self._connections.get(id(session))
            if connection is None or not self._current(connection) or connection.report is None:
                return None
            report = connection.report
            if report.public_ipv4_u32 != native_ipv4_word(connection.remote_ip):
                return None
            if self._unique_claim(connection.remote_ip, report.public_port) is not connection:
                return None
            if self._same_port_conflicts.get(id(session)):
                return None
            eligible = [o for o in self._observations.values() if o.ip == connection.remote_ip
                        and o.port == report.public_port and connection in o.eligible_connections]
            if not eligible:
                return None
            observed = max(eligible, key=lambda o: o.seen_at)
            mapping = self._destination_mappings.get(id(session))
            if mapping is not None:
                return UdpSourceMatch(connection.account_id, observed.ip, observed.port,
                    observed.listener_port, observed.seen_at, report, mapping.secondary.port,
                    mapping.secondary.listener_port, mapping.secondary.seen_at)
            return UdpSourceMatch(connection.account_id, observed.ip, observed.port,
                                  observed.listener_port, observed.seen_at, report)

    def stats(self):
        with self._lock:
            self._purge(self._now())
            return dict(connections=len(self._connections), observations=len(self._observations), buckets=len(self._buckets))


def service_for(server):
    with _FACTORY_LOCK:
        service = getattr(server, 'native_udp_probe_service', None)
        if service is None:
            service = NativeUdpProbeService()
            server.native_udp_probe_service = service
        return service


def native_udp_handler_for_game(server):
    return service_for(server).handle_datagram if server is not None and _enabled() else None


@contextmanager
def native_udp_connection(server, session, remote_ip):
    if not _enabled():
        yield
        return
    service = service_for(server)
    connection = service.open_connection(session, remote_ip, server.account_id)
    try:
        yield
    finally:
        service.close_connection(connection)
