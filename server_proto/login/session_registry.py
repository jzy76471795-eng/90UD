from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class LoginIdentity:
    account_id: int
    account_m_id: str
    login_session_id: str
    remote_ip: str
    advertise_host: str
    issued_at: float
    expires_at: float
    device_id: str = ""
    credential_digest: str = ""


@dataclass
class _StoredSession:
    identity: LoginIdentity
    token_digest: str
    last_seen_at: float
    revoked: bool = False


class SessionConflictError(RuntimeError):
    def __init__(self, message: str, reason: str = "UNSPECIFIED") -> None:
        super().__init__(message)
        self.reason = reason


class LoginSessionRegistry:
    def __init__(self, *, ttl_seconds: float = 8 * 60 * 60) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self.device_limit_provider = lambda: 2
        self._native_sessions: dict[str, tuple[LoginIdentity,int]] = {}
        # Bind each count to the real Game socket. A logout closes that socket;
        # persistence and final count release still run on its Game owner.
        self._native_transports: dict[str, dict[object, socket.socket]] = {}
        self._native_seen: set[str] = set()
        self._native_idle_since: dict[str, float] = {}
        self._lock = threading.RLock()
        self._by_digest: dict[str, _StoredSession] = {}
        self._digest_by_remote_ip: dict[str, str] = {}
        self._digest_by_account_id: dict[int, str] = {}
        self.native_client_first_ports = frozenset({7001})
        self.legacy_game_ports = frozenset()

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def _remove_locked(self, digest: str) -> None:
        stored = self._by_digest.pop(digest, None)
        if stored is None:
            return
        stored.revoked = True
        identity = stored.identity
        if identity.login_session_id not in self._native_sessions:
            self._native_seen.discard(identity.login_session_id)
            self._native_idle_since.pop(identity.login_session_id, None)
        if self._digest_by_remote_ip.get(identity.remote_ip) == digest:
            self._digest_by_remote_ip.pop(identity.remote_ip, None)
        if self._digest_by_account_id.get(identity.account_id) == digest:
            self._digest_by_account_id.pop(identity.account_id, None)

    def _purge_locked(self, now: float) -> None:
        expired = [
            digest
            for digest, stored in self._by_digest.items()
            if stored.revoked or stored.identity.expires_at <= now
        ]
        for digest in expired:
            self._remove_locked(digest)

    def create(
        self,
        *,
        account_id: int,
        account_m_id: str,
        remote_ip: str,
        advertise_host: str,
        device_id: str = "",
        credential_digest: str = "",
    ) -> tuple[LoginIdentity, str]:
        now = time.time()
        remote = str(ipaddress.ip_address(str(remote_ip)))
        advertised = str(ipaddress.IPv4Address(str(advertise_host)))
        account = int(account_id)
        with self._lock:
            self._purge_locked(now)
            if any(v.account_id==account for v,count in self._native_sessions.values() if count>0):
                raise SessionConflictError("this account already has a running game; exit it before logging in again", "ACCOUNT_GAME_RUNNING")
            if device_id:
                if len(device_id)!=64 or any(ch not in "0123456789abcdef" for ch in device_id):
                    raise SessionConflictError("invalid device identity", "INVALID_DEVICE")
                existing=self._by_digest.get(self._digest_by_account_id.get(account,""))
                if (existing and (existing.identity.device_id not in ("",device_id)
                        or not existing.identity.device_id and existing.identity.remote_ip!=remote)
                        and self._blocks_replacement_locked(existing.identity,now)):
                    raise SessionConflictError("this account already has an active session on another device", "ACCOUNT_SESSION_OTHER_DEVICE")
                occupants=self._device_occupants_locked(device_id,account,now)
                limit=int(self.device_limit_provider())
                if len(occupants)>=limit:
                    raise SessionConflictError("device game limit reached", "DEVICE_GAME_LIMIT")
            else:
                # Only an older launcher uses the address-based routing slot.
                legacy = self._by_digest.get(self._digest_by_remote_ip.get(remote, ""))
                if legacy and legacy.identity.account_id != account:
                    raise SessionConflictError("this source IP already owns another active account session; use the updated launcher", "LEGACY_IP_SESSION")
                existing=self._by_digest.get(self._digest_by_account_id.get(account,""))
                if existing and existing.identity.device_id:
                    raise SessionConflictError("this account already has an identified launcher session", "ACCOUNT_SESSION_OTHER_DEVICE")
                if existing and existing.identity.remote_ip!=remote:
                    raise SessionConflictError("this account already has an active session from another source IP", "ACCOUNT_SESSION_OTHER_IP")
            account_digest=self._digest_by_account_id.get(account)
            if account_digest:self._remove_locked(account_digest)

            token = secrets.token_urlsafe(32)
            digest = self.token_digest(token)
            identity = LoginIdentity(
                account_id=account,
                account_m_id=str(account_m_id),
                login_session_id=secrets.token_hex(16),
                remote_ip=remote,
                advertise_host=advertised,
                issued_at=now,
                expires_at=now + self.ttl_seconds,
                device_id=device_id,
                credential_digest=credential_digest,
            )
            self._by_digest[digest] = _StoredSession(identity, digest, now)
            if not device_id:self._digest_by_remote_ip[remote] = digest
            self._digest_by_account_id[account] = digest
            return identity, token

    def _blocks_replacement_locked(self, identity, now):
        # create() checks all live Game owners for this account under this lock.
        # A seen session with no owners has completed its final Game cleanup.
        if not identity.device_id:
            return True
        return identity.login_session_id not in self._native_seen and now - identity.issued_at < 180

    def _device_slot_reserved_locked(self, identity, now):
        sid = identity.login_session_id
        if sid not in self._native_seen:
            return now - identity.issued_at < 180
        # Keep a seat on this device across a channel change. A signed-in
        # move to a different device removes the old session and its seat.
        idle = self._native_idle_since.get(sid)
        return idle is not None and now - idle < 30

    def _device_occupants_locked(self,device_id,exclude_account,now,*,include_reservations=True):
        active={v.account_id for v,count in self._native_sessions.values() if count>0 and v.device_id==device_id and v.account_id!=exclude_account}
        if include_reservations:
            active.update(v.identity.account_id for v in self._by_digest.values()
                if not v.revoked and v.identity.device_id==device_id and v.identity.account_id!=exclude_account
                and self._device_slot_reserved_locked(v.identity, now))
        return active

    def acquire_native_game(self,identity,*,client=None):
        if client is not None and (not isinstance(client,socket.socket) or client.fileno()<0):
            raise ValueError('native ownership requires an open Game socket')
        transport_key=object() if client is not None else None
        with self._lock:
            now = time.time()
            self._purge_locked(now)
            stored=self._by_digest.get(self._digest_by_account_id.get(identity.account_id,''))
            if stored is None or stored.identity is not identity:raise SessionConflictError('native login session revoked', 'SESSION_REVOKED')
            sid=identity.login_session_id
            previous=self._native_sessions.get(sid)
            if identity.device_id and previous is None:
                # A session without its own reservation must also respect other
                # accounts that have a launch or channel change in progress.
                occupants = self._device_occupants_locked(
                    identity.device_id, identity.account_id, now,
                    include_reservations=not self._device_slot_reserved_locked(identity, now))
                if len(occupants) >= int(self.device_limit_provider()):
                    raise SessionConflictError('device game limit reached', 'DEVICE_GAME_LIMIT')
            self._native_sessions[sid]=(identity,(previous[1] if previous else 0)+1)
            if client is not None:self._native_transports.setdefault(sid,{})[transport_key]=client
            self._native_seen.add(sid)
            self._native_idle_since.pop(sid, None)
        released=False
        def release():
            nonlocal released
            with self._lock:
                if released:return
                released=True
                transports=self._native_transports.get(sid)
                if transports is not None:
                    transports.pop(transport_key,None)
                    if not transports:self._native_transports.pop(sid,None)
                current=self._native_sessions.get(sid)
                if current is None:return
                if current[1]>1:self._native_sessions[sid]=(identity,current[1]-1)
                else:
                    self._native_sessions.pop(sid,None)
                    stored = self._by_digest.get(self._digest_by_account_id.get(identity.account_id, ''))
                    if stored is not None and stored.identity.login_session_id == sid:
                        self._native_idle_since[sid] = time.time()
                    else:
                        self._native_seen.discard(sid)
                        self._native_idle_since.pop(sid, None)
        return release

    def authenticate_native(self,session_id,nonce,proof):
        if len(nonce)!=32 or len(proof)!=32:return None
        now=time.time()
        with self._lock:
            self._purge_locked(now)
            stored=next((v for v in self._by_digest.values() if v.identity.login_session_id==session_id),None)
            if stored is None:return None
            message=b'PY90NT2\n'+bytes.fromhex(session_id)+nonce
            expected=hmac.new(bytes.fromhex(stored.token_digest),message,hashlib.sha256).digest()
            if not hmac.compare_digest(expected,proof):return None
            stored.last_seen_at=now
            return stored.identity

    def authenticate(self, token: str) -> LoginIdentity | None:
        digest = self.token_digest(token)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            stored = self._by_digest.get(digest)
            if stored is None or not hmac.compare_digest(stored.token_digest, digest):
                return None
            stored.last_seen_at = now
            return stored.identity

    def authenticate_native_from_peer(self, session_id, nonce, proof, remote_ip):
        """A valid per-stream proof identifies modern clients across NAT exits.

        The installed game adapter additionally checks policy for the actual
        peer. This never inserts that peer into the legacy IP identity index.
        """
        try:
            remote = str(ipaddress.ip_address(remote_ip))
        except (TypeError, ValueError):
            return None
        identity = self.authenticate_native(session_id, nonce, proof)
        if identity is None or not identity.device_id:
            return None
        policy = getattr(self, 'native_peer_policy', None)
        if policy is None and remote != identity.remote_ip:
            return None
        if policy is not None:
            try:
                if not policy(identity, remote):
                    return None
            except Exception:
                return None
        return identity if self.is_current_identity(identity) else None

    def is_current_identity(self, identity):
        """Revalidate after policy/database work without holding locks over I/O."""
        with self._lock:
            self._purge_locked(time.time())
            stored = self._by_digest.get(self._digest_by_account_id.get(getattr(identity,'account_id',None),''))
            return stored is not None and stored.identity is identity

    def identity_for_remote_ip(self, remote_ip: str) -> LoginIdentity | None:
        try:
            remote = str(ipaddress.ip_address(str(remote_ip)))
        except ValueError:
            return None
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            digest = self._digest_by_remote_ip.get(remote)
            if not digest:
                return None
            stored = self._by_digest.get(digest)
            if stored is None:
                return None
            stored.last_seen_at = now
            return stored.identity

    def revoke(self, token: str) -> bool:
        digest = self.token_digest(token)
        with self._lock:
            stored = self._by_digest.get(digest)
            existed = stored is not None
            clients = tuple(self._native_transports.get(stored.identity.login_session_id, {}).values()) if stored is not None else ()
            self._remove_locked(digest)
        self._shutdown_native(clients)
        return existed

    def revoke_account(self, account_id: int) -> bool:
        with self._lock:
            digest = self._digest_by_account_id.get(int(account_id))
            if not digest:
                return False
            stored = self._by_digest.get(digest)
            clients = tuple(self._native_transports.get(stored.identity.login_session_id, {}).values()) if stored is not None else ()
            self._remove_locked(digest)
        self._shutdown_native(clients)
        return True

    @staticmethod
    def _shutdown_native(clients):
        # Do not hold the registry lock across socket operations. Do not drop
        # the count here: cleanup/persistence must finish on the Game owner.
        for client in clients:
            try:client.shutdown(socket.SHUT_RDWR)
            except OSError:pass # Already closed/reset; the owner's finally releases it.
            # Windows select-based receive polling need not become readable
            # after shutdown alone. Close invalidates the next owner poll too.
            try:client.close()
            except OSError:pass

    def clear(self) -> None:
        with self._lock:
            clients = tuple(client for group in self._native_transports.values() for client in group.values())
            for digest in list(self._by_digest):
                self._remove_locked(digest)
        self._shutdown_native(clients)


_GLOBAL_REGISTRY = LoginSessionRegistry()


def get_login_registry() -> LoginSessionRegistry:
    return _GLOBAL_REGISTRY


def is_loopback_address(value: str) -> bool:
    try:
        return bool(ipaddress.ip_address(str(value)).is_loopback)
    except ValueError:
        return False
