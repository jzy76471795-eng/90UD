from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from config import CAPTURE_DIR, LOG_DIR, ensure_dirs


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_port_list(text: str | None) -> list[int]:
    """Parse a comma/semicolon/space separated port list.

    Empty tokens are ignored.  Values may be decimal or ``0x`` prefixed.
    Duplicate valid ports are removed while preserving order.
    """

    if not text:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for raw in text.replace(";", ",").replace(" ", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        port = int(token, 0)
        if not (0 < port <= 0xFFFF):
            raise ValueError(f"invalid TCP/UDP port: {raw!r}")
        if port not in seen:
            out.append(port)
            seen.add(port)
    return out


@dataclass
class InterceptEvent:
    time: str
    kind: str
    port: int
    message: str
    peer: str | None = None
    bytes_count: int | None = None
    capture_path: str | None = None


class _BaseDumpServer:
    def __init__(self, host: str, port: int, label: str, reason: str) -> None:
        self.host = host
        self.port = port
        self.label = label
        self.reason = reason
        self._stop = threading.Event()
        self._started = threading.Event()
        self._ready = threading.Event()
        self._failed = threading.Event()
        self._bind_error = ""
        self.thread: threading.Thread | None = None
        ensure_dirs()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        self.log_path = LOG_DIR / f"{safe_label}_{host.replace('.', '_')}_{port}_{_timestamp()}.log"

    def log(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        self._started.wait(5.0)
        if self._failed.is_set():
            raise OSError(self._bind_error or "listener bind failed")
        if self._ready.is_set():
            return
        raise TimeoutError(
            "listener did not become ready: %s:%s" % (self.host, self.port)
        )

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError


class GenericTcpDumpServer(_BaseDumpServer):
    """Minimal raw TCP acceptor for newly discovered DOF.exe outbound ports."""

    def serve_forever(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.listen(16)
                server.settimeout(1.0)
                self._ready.set()
                self._started.set()
                self.log(
                    f"[Intercept/TCP] listening on {self.host}:{self.port} "
                    f"reason={self.reason}"
                )
                while not self._stop.is_set():
                    try:
                        client, addr = server.accept()
                    except socket.timeout:
                        continue
                    thread = threading.Thread(target=self._handle_client, args=(client, addr), daemon=True)
                    thread.start()
        except OSError as exc:
            self._bind_error = str(exc)
            self._failed.set()
            try:
                self.log(
                    f"[Intercept/TCP] bind/listen failed on {self.host}:{self.port}: {exc}"
                )
            finally:
                self._started.set()

    def _handle_client(self, client: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        prefix = f"tcp_intercept_{_timestamp()}_{self.port}_{addr[0].replace('.', '_')}_{addr[1]}"
        capture_path = CAPTURE_DIR / f"{prefix}.bin"
        self.log(f"[Intercept/TCP] accepted {peer} -> {self.host}:{self.port} capture={capture_path}")
        with client, capture_path.open("ab") as capture:
            client.settimeout(30.0)
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    self.log(f"[Intercept/TCP] timeout peer={peer}; closing")
                    break
                except OSError as exc:
                    self.log(f"[Intercept/TCP] recv error peer={peer}: {exc}")
                    break
                if not chunk:
                    self.log(f"[Intercept/TCP] peer closed {peer}")
                    break
                capture.write(chunk)
                capture.flush()
                self.log(f"[Intercept/TCP] recv peer={peer} bytes={len(chunk)} hex={chunk[:96].hex(' ')}")


class GenericUdpDumpServer(_BaseDumpServer):
    """Minimal UDP receiver for sendto endpoints advertised by protocol packets."""

    def __init__(self, host, port, label, reason, *, datagram_handler=None):
        super().__init__(host, port, label, reason)
        self.datagram_handler = datagram_handler
        self._next_capture_check = 0.0
        self._capture_enabled = False

    def _capture_requested(self) -> bool:
        """Touch log/udp_capture.enabled for a 15-minute diagnostic window."""
        now = time.monotonic()
        if now >= self._next_capture_check:
            self._next_capture_check = now + 1.0
            try:
                modified = (LOG_DIR / "udp_capture.enabled").stat().st_mtime
                self._capture_enabled = 0 <= time.time() - modified < 900
            except OSError:
                self._capture_enabled = False
        return self._capture_enabled

    def _dispatch_datagram(self, server, payload, addr):
        if self.datagram_handler is not None:
            self.datagram_handler(payload, addr, self.port, server.sendto)

    def serve_forever(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.settimeout(1.0)
                self._ready.set()
                self._started.set()
                self.log(
                    f"[Intercept/UDP] listening on {self.host}:{self.port} "
                    f"reason={self.reason}"
                )
                while not self._stop.is_set():
                    try:
                        payload, addr = server.recvfrom(65535)
                    except socket.timeout:
                        continue
                    self._dispatch_datagram(server, payload, addr)
                    if not self._capture_requested():
                        continue
                    try:
                        peer = f"{addr[0]}:{addr[1]}"
                        prefix = f"udp_intercept_{_timestamp()}_{self.port}_{addr[0].replace('.', '_')}_{addr[1]}"
                        capture_path = CAPTURE_DIR / f"{prefix}.bin"
                        capture_path.write_bytes(payload)
                        self.log(
                            f"[Intercept/UDP] recv peer={peer} -> {self.host}:{self.port} "
                            f"bytes={len(payload)} capture={capture_path} hex={payload[:96].hex(' ')}"
                        )
                    except OSError:
                        # A diagnostics write failure must not close the UDP listener.
                        continue
        except OSError as exc:
            self._bind_error = str(exc)
            self._failed.set()
            try:
                self.log(f"[Intercept/UDP] bind failed on {self.host}:{self.port}: {exc}")
            finally:
                self._started.set()


@dataclass
class PortInterceptorManager:
    """Starts local dump listeners for ports DOF.exe is observed to use.

    The manager deliberately does not implement full service protocols.  It is
    a safety net for discovery: when the debugger sees an outbound
    ``connect``/``sendto`` endpoint, the corresponding local port can be opened
    immediately so the request lands in our captures instead of disappearing.
    """

    host: str
    reserved_tcp_ports: set[int] = field(default_factory=set)
    tcp_servers: dict[int, GenericTcpDumpServer] = field(default_factory=dict)
    udp_servers: dict[int, GenericUdpDumpServer] = field(default_factory=dict)
    events: list[InterceptEvent] = field(default_factory=list)
    udp_datagram_handler: object = None

    def start_tcp_ports(self, ports: Iterable[int], reason: str) -> None:
        for port in ports:
            self.ensure_tcp_port(port, reason)

    def start_udp_ports(self, ports: Iterable[int], reason: str) -> None:
        for port in ports:
            self.ensure_udp_port(port, reason)

    def ensure_tcp_port(self, port: int | None, reason: str) -> bool:
        if port is None or not (0 < int(port) <= 0xFFFF):
            return False
        port = int(port)
        if port in self.reserved_tcp_ports:
            self.events.append(
                InterceptEvent(
                    time=datetime.now().isoformat(timespec="seconds"),
                    kind="tcp-reserved",
                    port=port,
                    message=f"reserved by protocol server; no generic listener started; reason={reason}",
                )
            )
            return False
        if port in self.tcp_servers:
            return True
        server = GenericTcpDumpServer(self.host, port, "tcp_intercept", reason)
        self.tcp_servers[port] = server
        server.start()
        self.events.append(
            InterceptEvent(
                time=datetime.now().isoformat(timespec="seconds"),
                kind="tcp-listen",
                port=port,
                message=f"started generic TCP dump listener; reason={reason}",
                capture_path=str(server.log_path),
            )
        )
        return True

    def ensure_udp_port(self, port: int | None, reason: str) -> bool:
        if port is None or not (0 < int(port) <= 0xFFFF):
            return False
        port = int(port)
        if port in self.udp_servers:
            return True
        server = GenericUdpDumpServer(self.host, port, "udp_intercept", reason,
                                      datagram_handler=self.udp_datagram_handler)
        self.udp_servers[port] = server
        server.start()
        self.events.append(
            InterceptEvent(
                time=datetime.now().isoformat(timespec="seconds"),
                kind="udp-listen",
                port=port,
                message=f"started generic UDP dump listener; reason={reason}",
                capture_path=str(server.log_path),
            )
        )
        return True

    def stop(self) -> None:
        for server in list(self.tcp_servers.values()) + list(self.udp_servers.values()):
            server.stop()

    def summary(self) -> dict[str, object]:
        return {
            "host": self.host,
            "reserved_tcp_ports": sorted(self.reserved_tcp_ports),
            "tcp_ports": sorted(self.tcp_servers),
            "udp_ports": sorted(self.udp_servers),
            "events": [event.__dict__ for event in self.events],
        }
