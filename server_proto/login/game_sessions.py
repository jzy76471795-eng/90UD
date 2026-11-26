"""Authenticated loopback adapter for the Go launcher API.

Only live game session creation/resolution, metadata validation and mailbox
refresh remain here. Password verification and player business transactions
are owned by Go. The control server authenticates every call before dispatch.
"""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import os
import threading
from datetime import datetime, timezone

from game.mail_store import MailStore, stamp
from login.account_repository import AccountLoginRepository
from login.session_registry import SessionConflictError, get_login_registry


class LoginApiError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _rfc3339(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _legacy_game_ports(game_ports, reserved):
    used = set(game_ports) | set(reserved)
    aliases = {}
    for port in game_ports:
        preferred = port + 1000
        if 1024 <= preferred <= 65535 and preferred not in used:
            aliases[port] = preferred
            used.add(preferred)
    remaining = (port for port in range(1024, 65536) if port not in used)
    for port in game_ports:
        if port not in aliases:
            aliases[port] = next(remaining)
    return aliases

class GameSessionService:
    def __init__(self, game_server, channel_server, *, host="127.0.0.1", port=17602,
                 db_path=None, pvf_profile="cain-pvf-paired", pvf_sha256="", registry=None):
        self.game_server, self.channel_server = game_server, channel_server
        self.account_repository = AccountLoginRepository(db_path)
        self.store = MailStore(self.account_repository.db_path)
        self.pvf_profile, self.pvf_sha256 = pvf_profile, pvf_sha256
        self.registry = registry or get_login_registry()
        relay_config=os.environ.get('USDOF_PARTY_RELAY_CONFIG','').strip()
        self.party_relay=None
        if relay_config:
            from login.party_relay_client import RelayConfig,RelayControl
            self.party_relay=RelayControl(RelayConfig.load(relay_config))
        self._ready, self._stopped = threading.Event(), threading.Event()
        # One legacy server-first endpoint for each logical Game channel.
        # Keep stage3.game_ports and the existing modern clients on their original ports.
        from config import CONTROL_PORT, DEFAULT_EXTRA_TCP_PORTS, LOGIN_PORT
        game_ports = tuple(getattr(game_server, 'listen_ports', None) or [game_server.port])
        legacy_by_game = _legacy_game_ports(game_ports, (channel_server.port, port, CONTROL_PORT,
                                                        LOGIN_PORT, *DEFAULT_EXTRA_TCP_PORTS))
        legacy_ports = set(legacy_by_game.values())
        game_server.legacy_game_ports = {alias: port for port, alias in legacy_by_game.items()}
        channel_server.legacy_game_ports = legacy_by_game
        self.registry.legacy_game_ports = frozenset(legacy_ports)
        self._install_guards()
        game_server._launcher_service = self
        self.party_relay_controller=None
        if self.party_relay is not None:
            from game.relay_party import install
            self.party_relay_controller=install(game_server,self.party_relay)

    def _install_guards(self):
        registry = self.registry
        if getattr(registry, "_go_launcher_guard", False):
            raise RuntimeError("launcher registry is already owned")
        registry.native_client_first_ports = frozenset({int(self.channel_server.port)})
        def guard(original):
            def checked(*args, **kwargs):
                identity = original(*args, **kwargs)
                if identity is None:
                    return None
                try:
                    if not self.store.identity_allowed(identity):
                        return None
                    return identity if registry.is_current_identity(identity) else None
                except Exception:
                    # Database or policy failure must not bypass authentication.
                    return None
            return checked
        registry.authenticate = guard(registry.authenticate)
        registry.authenticate_native = guard(registry.authenticate_native)
        registry.identity_for_remote_ip = guard(registry.identity_for_remote_ip)
        registry.native_peer_policy = self._peer_allowed
        def limit():
            with self.store.connect() as conn:
                return 2 if self.store.config(conn, "multiOpenPolicy").get("enabled", True) else 1
        registry.device_limit_provider = limit
        registry._go_launcher_guard = True

    def serve_forever(self):
        self._ready.set()
        self._stopped.wait()

    def wait_ready(self, timeout=None):
        return self._ready.wait(timeout)

    def stop(self):
        self._ready.clear()
        self._stopped.set()
        if self.party_relay_controller is not None:self.party_relay_controller.stop()

    def _services_ready(self):
        # Relay failures gate relay grants/parties, not unrelated login services.
        return all(not callable(getattr(s, "wait_ready", None)) or s.wait_ready(0)
                   for s in (self.game_server, self.channel_server))

    @staticmethod
    def _fields(body, required):
        if not isinstance(body, dict) or set(body) != set(required):
            raise LoginApiError(400, "INVALID_REQUEST", "invalid game adapter fields")

    @staticmethod
    def _identity(identity, *, remote_ip=None):
        return {"account_id": identity.account_id, "account": identity.account_m_id,
                "session_id": identity.login_session_id, "remote_ip": identity.remote_ip if remote_ip is None else remote_ip,
                "device_id": identity.device_id, "advertise_host": identity.advertise_host,
                "issued_at": _rfc3339(identity.issued_at), "expires_at": _rfc3339(identity.expires_at)}

    def _peer_allowed(self, identity, remote_ip):
        try:
            remote = str(ipaddress.ip_address(remote_ip))
        except (TypeError, ValueError):
            return False
        if remote != identity.remote_ip and not identity.device_id:
            return False
        with self.store.connect() as conn:
            return self.store.allows(conn, identity.account_id, remote, identity.device_id)

    def _resolve(self, body):
        self._fields(body, {"token", "remote_ip"})
        token = body["token"]
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise LoginApiError(401, "AUTH_FAILED", "invalid session")
        identity = self.registry.authenticate(token)
        if (identity is None or not self._peer_allowed(identity, body["remote_ip"])
                or not self.registry.is_current_identity(identity)):
            raise LoginApiError(401, "AUTH_FAILED", "invalid session")
        return identity

    def _create(self, body):
        self._fields(body, {"account_id", "account", "password_hash", "remote_ip", "device_id", "advertise_host"})
        if not self._services_ready():
            raise LoginApiError(503, "SERVER_NOT_READY", "game services are not ready")
        aid = body["account_id"]
        if type(aid) is not int or aid <= 0 or not all(isinstance(body[k], str) for k in body if k != "account_id"):
            raise LoginApiError(400, "INVALID_REQUEST", "invalid session fields")
        ip = str(ipaddress.ip_address(body["remote_ip"]))
        host = str(ipaddress.IPv4Address(body["advertise_host"]))
        encoded = body["password_hash"]
        if not encoded or len(encoded) > 2048:
            raise LoginApiError(401, "AUTH_FAILED", "invalid credential")
        with self.store.connect() as conn:
            row = conn.execute("SELECT m_id,password_hash FROM accounts WHERE account_id=?", (aid,)).fetchone()
            if row is None or row[0] != body["account"] or not hmac.compare_digest(str(row[1]), encoded):
                raise LoginApiError(401, "AUTH_FAILED", "credential changed")
            if not self.store.allows(conn, aid, ip, body["device_id"]):
                raise LoginApiError(403, "ACCOUNT_BANNED", "game policy rejected session")
        try:
            identity, token = self.registry.create(account_id=aid, account_m_id=body["account"], remote_ip=ip,
                advertise_host=host, device_id=body["device_id"], credential_digest=hashlib.sha256(encoded.encode()).hexdigest())
        except SessionConflictError as exc:
            code = "MULTI_OPEN_LIMIT" if "device game limit" in str(exc) else "SESSION_CONFLICT"
            raise LoginApiError(409, code, str(exc)) from None
        try:
            with self.store.transaction() as conn:
                changed = conn.execute("UPDATE accounts SET last_login_ip=?,last_login_at=? WHERE account_id=? AND password_hash=?",
                                       (ip, stamp(), aid, encoded)).rowcount
                if changed != 1 or not self.store.allows(conn, aid, ip, body["device_id"]):
                    raise LoginApiError(401, "AUTH_FAILED", "credential or policy changed")
                conn.execute("""INSERT INTO gmkit_identity(account_id,remote_ip,machine_id,trusted_machine,updated_at)
                    VALUES(?,?,?,0,?) ON CONFLICT(account_id) DO UPDATE SET remote_ip=excluded.remote_ip,
                    machine_id=excluded.machine_id,trusted_machine=0,updated_at=excluded.updated_at""", (aid, ip, body["device_id"], stamp()))
                conn.execute("INSERT INTO gmkit_account_logins(account_id,remote_ip,machine_id,machine_source,session_id,created_at) VALUES(?,?,?,?,?,?)",
                    (aid, ip, body["device_id"], "launcher-reported" if body["device_id"] else "legacy-unreported", identity.login_session_id, stamp()))
        except BaseException:
            self.registry.revoke(token)
            raise
        result = self._identity(identity)
        result.update(access_token=token, token_type="Bearer")
        return result

    def _adventure_collection(self, body):
        if not isinstance(body, dict):
            raise LoginApiError(400, "INVALID_REQUEST", "invalid collection request")
        action = body.get("action")
        if action not in ("list", "set", "remove", "expedition_start", "expedition_cancel", "expedition_claim"):
            raise LoginApiError(400, "INVALID_REQUEST", "unknown collection action")
        required = {"action", "token", "remote_ip"}
        if action in ("set", "remove"):
            required.add("character_id")
        elif action == "expedition_start":
            required.update(("request_id", "area_id", "hours", "members", "rotation_key"))
        elif action in ("expedition_cancel", "expedition_claim"):
            required.add("run_id")
        self._fields(body, required)
        identity = self._resolve({"token": body["token"], "remote_ip": body["remote_ip"]})
        character = body.get("character_id")
        if action in ("set", "remove") and (type(character) is not int or not 1 <= character <= 0xFFFFFFFF):
            raise LoginApiError(400, "INVALID_REQUEST", "invalid collection character")
        if action == "expedition_start":
            for field in ("request_id", "area_id", "hours", "rotation_key"):
                if type(body[field]) is not int or not 1 <= body[field] <= 0xFFFFFFFF:
                    raise LoginApiError(400, "INVALID_REQUEST", "invalid expedition start value")
            members = body["members"]
            if (not isinstance(members, list) or not 1 <= len(members) <= 4
                    or any(type(member) is not int or not 1 <= member <= 0xFFFFFFFF for member in members)):
                raise LoginApiError(400, "INVALID_REQUEST", "invalid expedition members")
        if action in ("expedition_cancel", "expedition_claim") and (
            type(body["run_id"]) is not int or not 1 <= body["run_id"] <= 0x7FFFFFFFFFFFFFFF
        ):
            raise LoginApiError(400, "INVALID_REQUEST", "invalid expedition run")
        if not self._services_ready():
            raise LoginApiError(503, "SERVER_NOT_READY", "game services are not ready")

        from game.character.cn2018_expedition import CN2018Expedition, ExpeditionRejected
        from pvf.cn_expedition_index import get_cn_expedition_index

        try:
            index = get_cn_expedition_index()
        except (KeyError, FileNotFoundError, ValueError):
            raise LoginApiError(503, "COLLECTION_UNAVAILABLE", "CN collection configuration is unavailable") from None
        service = CN2018Expedition(self.game_server._repo(), index)
        operation = None
        try:
            if action == "set":
                service.set_representative(identity.account_id, character)
            elif action == "remove" and not service.remove_representative(identity.account_id, character):
                raise ExpeditionRejected("collection_representative_missing")
            elif action == "expedition_start":
                operation = service.start(identity.account_id, str(body["request_id"]), body["area_id"],
                                          body["hours"], members, body["rotation_key"])
            elif action == "expedition_cancel":
                operation = service.cancel(identity.account_id, body["run_id"])
            elif action == "expedition_claim":
                operation = service.claim(identity.account_id, body["run_id"])
        except ExpeditionRejected as exc:
            raise LoginApiError(409, "COLLECTION_REJECTED", str(exc)) from None
        if action == "expedition_claim":
            refresh_wallet = getattr(self.game_server, "_refresh_adventure_expedition_wallet", None)
            if callable(refresh_wallet):
                refresh_wallet(identity, body["run_id"])
        status = service.status(identity.account_id)
        attributes = {area: tuple(index.attributes[attribute] for attribute in required)
                      for area, required in status.pop("rotation").items()}
        for run in status["runs"]:
            run["required_attributes"] = tuple(index.attributes[value] for value in run["required_attributes"])
        return {"characters": service.collection_view(identity.account_id),
                "expedition_attributes": attributes, "expedition": status,
                "expedition_operation": operation}

    def dispatch(self, action, body):
        if action == "adventure_operation":
            if not isinstance(body, dict) or body.get("action") not in (
                "state", "buy", "use_capsule", "weekly_status", "weekly_sign", "weekly_make_up", "weekly_claim",
            ):
                raise LoginApiError(400, "INVALID_REQUEST", "invalid adventure action")
            fields = {"action", "request_id"}
            if body["action"] not in ("state", "weekly_status"):
                fields.update(("character_id", "connection_generation"))
            if body["action"] in ("buy", "use_capsule"):
                fields.add("quantity")
            if body["action"] == "buy":
                fields.add("item_id")
            if body["action"] in ("weekly_sign", "weekly_make_up"):
                fields.add("week")
            self._fields(body, fields | {"token", "remote_ip"})
            for name in fields - {"action"}:
                limit = {"quantity": 99, "week": 10}.get(name, 0xFFFFFFFF)
                if type(body[name]) is not int or not 1 <= body[name] <= limit:
                    raise LoginApiError(400, "INVALID_REQUEST", "invalid adventure operation value")
            identity = self._resolve({"token": body["token"], "remote_ip": body["remote_ip"]})
            operation = getattr(self.game_server, "_adventure_group_operation", None)
            if not self._services_ready() or not callable(operation):
                raise LoginApiError(503, "SERVER_NOT_READY", "game services are not ready")
            return operation(identity, {name: body[name] for name in fields})
        if action == "meta":
            self._fields(body, {"advertise_host"})
            host = str(ipaddress.IPv4Address(body["advertise_host"]))
            ports = getattr(self.game_server, "listen_ports", None) or [getattr(self.game_server, "port", 10052)]
            return {"api_version": 1, "transport": "http-direct-tcp", "server_ready": self._services_ready(),
                "stage3": {"exe_sha256": "14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634",
                    "pvf_profile": self.pvf_profile, "pvf_sha256": self.pvf_sha256, "launch_mode": 9,
                    "channel_host": host, "channel_port": int(getattr(self.channel_server, "port", 7001)),
                    "game_ports": sorted({int(p) for p in ports if 1 <= int(p) <= 65535}), "native_tunnel_version": 2,
                    "party_relay_version": 1 if self.party_relay is not None else 0,
                    "opaque_account": str(int(getattr(self.game_server, "launcher_account_id", 3012345))),
                    "opaque_token": str(getattr(self.game_server, "launcher_token", "")),
                    "tail": [0,0,0,0,0,2,0,0,0,0,0,0,0,0,0,0]}}
        if action == "create":
            return self._create(body)
        if action == 'relay_grant':
            self._fields(body, {'token','remote_ip','request_id'})
            identity=self._resolve({'token':body['token'],'remote_ip':body['remote_ip']})
            if not getattr(identity,'device_id','') or self.party_relay is None:
                raise LoginApiError(503,'RELAY_UNAVAILABLE','authenticated party relay unavailable')
            rollout=getattr(self.party_relay,'rollout',None)
            if rollout is not None and not rollout.contains(identity.account_id):
                raise LoginApiError(403,'RELAY_NOT_ENROLLED','account outside the current party relay test')
            controller=getattr(self,'party_relay_controller',None)
            if controller is not None and not controller.healthy():
                raise LoginApiError(503,'RELAY_UNAVAILABLE','authenticated party relay unavailable')
            from login.party_relay_client import RelayUnavailable
            try:return self.party_relay.issue(identity,body['request_id'])
            except RelayUnavailable:
                raise LoginApiError(503,'RELAY_UNAVAILABLE','authenticated party relay unavailable') from None
        if action in ("resolve", "revoke"):
            identity = self._resolve(body)
            if action=='revoke' and self.party_relay is not None:
                from login.party_relay_client import RelayUnavailable
                try:self.party_relay.revoke_login(identity.account_id,identity.login_session_id)
                except RelayUnavailable:pass
            # Go verifies that its request peer equals the resolved response.
            # Report that authenticated request's peer without rewriting the
            # canonical login identity or its ownership/revocation keys.
            return self._identity(identity, remote_ip=str(ipaddress.ip_address(body['remote_ip']))) if action == "resolve" else {"revoked": self.registry.revoke(body["token"])}
        if action == "revoke_account":
            self._fields(body, {"account_id"})
            if type(body["account_id"]) is not int or body["account_id"] < 1:
                raise LoginApiError(400, "INVALID_REQUEST", "invalid account")
            revoked = self.registry.revoke_account(body["account_id"])
            disconnect = getattr(self.game_server, "_manager_disconnect", None)
            if callable(disconnect):
                disconnect(body["account_id"])
            return {"revoked": revoked}
        if action == "prepare_rewards":
            self._fields(body, {"rewards"})
            rewards = body["rewards"]
            if not isinstance(rewards, list) or not 1 <= len(rewards) <= 100:
                raise LoginApiError(400, "INVALID_REQUEST", "invalid rewards")
            for reward in rewards:
                if not isinstance(reward, dict) or reward.get("kind") not in ("points", "gold", "item"):
                    raise LoginApiError(400, "INVALID_REQUEST", "invalid reward")
                if reward["kind"] == "item" and (type(reward.get("itemId")) is not int or not 1 <= reward["itemId"] <= 2_000_000_000
                    or type(reward.get("quantity")) is not int or not 1 <= reward["quantity"] <= 9999):
                    raise LoginApiError(400, "INVALID_REQUEST", "invalid item")
            from game.native_mailbox import prepare_rewards
            try:
                return prepare_rewards(rewards)
            except ValueError as exc:
                raise LoginApiError(409, "CDK_REJECTED", str(exc)) from None
        if action == "assist":
            required = {"account_id", "session_id", "action", "request_id"}
            if not isinstance(body, dict) or not required.issubset(body):
                raise LoginApiError(400, "INVALID_REQUEST", "invalid game adapter fields")
            return self.game_server._player_assist(body)
        if action == "adventure_collection":
            return self._adventure_collection(body)
        if action == "refresh_mailbox":
            self._fields(body, {"account_id", "character_id", "request_id"})
            aid, cid, rid = body["account_id"], body["character_id"], body["request_id"]
            if type(aid) is not int or type(cid) is not int or aid < 1 or cid < 1 or not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise LoginApiError(400, "INVALID_REQUEST", "invalid mailbox target")
            snapshot = self.game_server.get_control_session_snapshot()
            targets = [e for e in snapshot.get("connections", []) if e["account_id"] == aid and e["character_id"] == cid]
            if snapshot.get("all_connections_complete") is not True or len(targets) != 1:
                return {"notified": False}
            self.game_server.submit_control_command(request_id=rid, command="manager.mailbox_refresh", params={
                "_manager_expected_character": cid, "_manager_expected_epoch": targets[0]["session_epoch"]}, timeout=2)
            return {"notified": True}
        raise LoginApiError(404, "NOT_FOUND", "unknown game adapter operation")
