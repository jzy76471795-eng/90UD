"""Real Game TCP LOGIN routing keys, independent of endpoint/party readiness.

Identity is supplied only by the existing authenticated Game accept scope.
The native key is public routing state: LOGIN body+5, then peer actor+C. It
is never a password, C0002 claim, UDP endpoint proof, or combat capability.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import ip_address
from threading import RLock
from login.native_tunnel import authenticated_native_peer

from .runtime import RaidError

_FACTORY_LOCK = RLock()
_KEY_LOCK = RLock()
_KEY_OWNERS = {}
_NEXT_KEY = 0


def _allocate(owner):
    global _NEXT_KEY
    with _KEY_LOCK:
        if len(_KEY_OWNERS) >= 0xffffffff:
            raise RaidError('native connection routing keys exhausted')
        while True:
            _NEXT_KEY = _NEXT_KEY % 0xffffffff + 1
            if _NEXT_KEY not in _KEY_OWNERS:
                _KEY_OWNERS[_NEXT_KEY] = owner
                return _NEXT_KEY


@dataclass(eq=False)
class _Connection:
    session: object
    client: object
    identity: object
    account_id: int
    login_session_id: object
    channel_id: object
    owner: object = field(default_factory=object)
    key: int = 0
    sent: bool = False
    sent_generation: int = 0
    pending: dict = field(default_factory=dict)
    selected: object = None


@dataclass(frozen=True, eq=False)
class _LoginSend:
    connection: _Connection
    packet: bytes
    view: tuple


@dataclass(frozen=True, eq=False)
class _Selection:
    actor: object
    generation: int


class NativeConnectionDirectory:
    def __init__(self, server):
        self.server = server
        self._connections = {}
        self._lock = RLock()

    def open_connection(self, session, client, *, identity):
        """Only after native_tunnel/legacy authentication, on the owner thread."""
        account = getattr(session, 'account_id', None)
        sid = getattr(session, 'login_session_id', None)
        try:
            remote = ip_address(client.getpeername()[0])
            live = client.fileno() >= 0
        except (OSError, ValueError, AttributeError):
            raise RaidError('actual live Game socket unavailable') from None
        if (type(account) is not int or account <= 0 or account != self.server.account_id
                or not live or identity is not None and
                (identity.account_id != account or identity.login_session_id != sid
                 or (ip_address(identity.remote_ip) != remote
                     and not authenticated_native_peer(client, identity)))
                or identity is None and (not remote.is_loopback or sid is not None)):
            raise RaidError('authenticated Game connection identity mismatch')
        with self._lock:
            if id(session) in self._connections:
                raise RaidError('Game session already owns a routing connection')
            connection = _Connection(session, client, identity, account, sid,
                                     getattr(session, 'selected_channel_id', None))
            connection.key = _allocate(connection.owner)
            self._connections[id(session)] = connection
            return connection

    def _drop(self, connection):
        if self._connections.get(id(connection.session)) is connection:
            self._connections.pop(id(connection.session), None)
        connection.pending.clear()
        connection.selected = None
        connection.sent = False
        with _KEY_LOCK:
            if _KEY_OWNERS.get(connection.key) is connection.owner:
                _KEY_OWNERS.pop(connection.key, None)

    def close_connection(self, connection):
        if type(connection) is not _Connection:
            return
        with self._lock:
            if self._connections.get(id(connection.session)) is not connection:
                return
        # Preserve the captured raid owner before dropping this exact Game
        # connection. Secondary scene observers cannot prevent the drop.
        from .local_bindings import close_game_session
        from game.normal_party import forget
        from game.town_social import forget as forget_town
        try:
          for label, callback in (('raid', lambda: close_game_session(self.server, connection.session)),
                                  ('party', lambda: forget(self.server, connection.session)),
                                  ('town', lambda: forget_town(self.server, connection.session))):
            try:
                callback()
            except Exception as exc:
                log = getattr(self.server, 'log', None)
                if callable(log):
                    try:log('[Raid] Game-close observer deferred component=%s error=%s' % (label, exc))
                    except Exception:pass
        finally:
            with self._lock:
                self._drop(connection)

    def _current(self, session, *, sender=False):
        connection = self._connections.get(id(session))
        if connection is None or connection.session is not session:
            raise RaidError('actual owned Game routing connection unavailable')
        if (getattr(session, 'account_id', None) != connection.account_id
                or getattr(session, 'login_session_id', None) != connection.login_session_id
                or connection.client.fileno() < 0):
            self._drop(connection)
            raise RaidError('Game routing connection identity expired')
        if sender and self.server.account_id != connection.account_id:
            raise RaidError('LOGIN producer requires its owning account thread')
        channel = getattr(session, 'selected_channel_id', None)
        if channel != connection.channel_id:
            connection.channel_id = channel
            connection.sent = False
            connection.pending.clear()
            connection.selected = None
        return connection

    @staticmethod
    def _view(connection):
        session = connection.session
        return (getattr(session, 'selected_character_id', None),
                getattr(session, '_raid_selected_epoch', None), connection.channel_id)

    def login_key(self, session):
        with self._lock:
            return self._current(session, sender=True).key

    def prepare_login_body(self, session, body, flag):
        with self._lock:
            connection = self._current(session, sender=True)
            if flag != 1 or not body or body[0] != 1:
                return body
            if type(body) is not bytes or len(body) != 152:
                raise RaidError('native LOGIN routing key requires the established152-byte body')
            return body[:5] + connection.key.to_bytes(4, 'little') + body[9:]

    def track_login_packet(self, session, packet, body, flag):
        with self._lock:
            connection = self._current(session, sender=True)
            if flag != 1 or not body or body[0] != 1:
                return
            if (type(packet) is not bytes or len(body) != 152
                    or int.from_bytes(body[5:9], 'little') != connection.key):
                raise RaidError('LOGIN packet differs from its actual routing-key producer')
            connection.pending[id(packet)] = _LoginSend(connection, packet, self._view(connection))

    def capture_send(self, session, client, packet):
        with self._lock:
            try:
                connection = self._current(session, sender=True)
            except RaidError:
                return None
            if connection.client is not client:
                return None
            receipt = connection.pending.get(id(packet))
            if receipt is None or receipt.packet is not packet:
                return None
            if receipt.view != self._view(connection):
                connection.pending.pop(id(packet), None)
                return None
            return receipt

    def confirm_send(self, receipt):
        """Only the real socket sender calls after its own sendall succeeds."""
        if receipt is None:
            return False
        with self._lock:
            if type(receipt) is not _LoginSend:
                raise RaidError('foreign LOGIN send receipt')
            connection = receipt.connection
            try:
                current = self._current(connection.session, sender=True)
            except RaidError:
                return False
            if (current is not connection or connection.pending.get(id(receipt.packet)) is not receipt
                    or receipt.view != self._view(connection)):
                return False
            connection.pending.pop(id(receipt.packet), None)
            connection.sent = True
            connection.sent_generation += 1
            connection.selected = None
            return True

    def login_delivered(self, session):
        with self._lock:
            try:
                return self._current(session).sent
            except RaidError:
                return False

    def chat_selection(self, session, *, sender=False):
        """Freeze an authenticated, logged-in selection for queued chat delivery."""
        with self._lock:
            connection = self._current(session, sender=sender)
            cid = getattr(session, 'selected_character_id', None)
            if (not connection.sent or type(cid) is not int or cid <= 0
                    or not (getattr(session, 'enter_seria_settled', False)
                            or getattr(session, 'in_dungeon_run', False))):
                raise RaidError('chat requires an entered selected character')
            return (connection, connection.sent_generation, connection.account_id,
                    connection.channel_id, cid, getattr(session, 'chat_selection_generation', 0),
                    getattr(session, '_raid_selected_epoch', None))

    def chat_selections(self):
        with self._lock:
            result = []
            for connection in tuple(self._connections.values()):
                try:
                    result.append(self.chat_selection(connection.session))
                except RaidError:
                    continue
            return tuple(result)

    def key_for_selection(self, selection):
        with self._lock:
            if self.chat_selection(selection[0].session) != selection:
                raise RaidError('peer selection changed since delivered LOGIN')
            return self._current(selection[0].session).key

    def _actual_actor(self, actor):
        service = getattr(self.server, 'raid_service', None)
        lobby = getattr(service, 'native_lobby', None)
        return (lobby is not None and lobby._by_session.get(id(actor.session)) is actor
                and lobby._actors.get((actor.channel_id, actor.character_id)) is actor)

    def bind_selected_actor(self, actor):
        with self._lock:
            try:
                connection = self._current(actor.session)
            except RaidError:
                return False
            if (not connection.sent or not self._actual_actor(actor)
                    or self._view(connection) != (actor.character_id, actor.epoch, actor.channel_id)
                    or connection.account_id != actor.account_id):
                return False
            connection.selected = _Selection(actor, connection.sent_generation)
            return True

    def invalidate_selection(self, session, *, reset_login=False):
        with self._lock:
            connection = self._connections.get(id(session))
            if connection is not None and connection.session is session:
                connection.selected = None
                connection.pending.clear()
                if reset_login:
                    connection.sent = False

    def key_for_actor(self, actor):
        with self._lock:
            connection = self._current(actor.session)
            selected = connection.selected
            if (not connection.sent or selected is None or selected.actor is not actor
                    or selected.generation != connection.sent_generation
                    or not self._actual_actor(actor)
                    or self._view(connection) != (actor.character_id, actor.epoch, actor.channel_id)
                    or connection.account_id != actor.account_id):
                connection.selected = None
                raise RaidError('current selected actor has no actually delivered LOGIN routing key')
            return connection.key

    def verify_peer_keys(self, actors, peers):
        with self._lock:
            if len(actors) != len(peers):
                raise RaidError('peer routing keys do not cover actual actors')
            for actor, peer in zip(actors, peers):
                if peer.user_id != actor.character_id or peer.connection_key != self.key_for_actor(actor):
                    raise RaidError('peer key differs from target current delivered LOGIN')


def service_for(server):
    with _FACTORY_LOCK:
        directory = getattr(server, 'native_game_connections', None)
        if directory is None:
            directory = NativeConnectionDirectory(server)
            server.native_game_connections = directory
        return directory


def _registered(server, session):
    directory = getattr(server, 'native_game_connections', None)
    return directory if directory is not None and id(session) in directory._connections else None


def key_for_login(server, session):
    directory = getattr(server, 'native_game_connections', None)
    return directory.login_key(session) if directory is not None else None


def prepare_login_body(server, session, body, flag):
    directory = getattr(server, 'native_game_connections', None)
    return directory.prepare_login_body(session, body, flag) if directory is not None else body


def track_login_packet(server, session, packet, body, flag):
    directory = getattr(server, 'native_game_connections', None)
    if directory is not None:
        directory.track_login_packet(session, packet, body, flag)


def capture_login_send(server, session, client, packet):
    directory = _registered(server, session)
    return directory.capture_send(session, client, packet) if directory is not None else None


def confirm_login_send(server, receipt):
    directory = getattr(server, 'native_game_connections', None)
    return directory.confirm_send(receipt) if directory is not None else False


def bind_selected_actor(server, actor):
    directory = _registered(server, actor.session)
    return directory.bind_selected_actor(actor) if directory is not None else False


def invalidate_selection(server, session, *, reset_login=False):
    from game.normal_party import forget
    forget(server, session)
    from game.town_social import forget as forget_town
    forget_town(server, session)
    from .native_clock import invalidate_start_clock
    invalidate_start_clock(session)
    directory = getattr(server, 'native_game_connections', None)
    if directory is not None:
        directory.invalidate_selection(session, reset_login=reset_login)


@contextmanager
def native_game_connection(server, session, client, *, identity):
    directory = service_for(server)
    connection = directory.open_connection(session, client, identity=identity)
    try:
        yield connection
    finally:
        directory.close_connection(connection)
