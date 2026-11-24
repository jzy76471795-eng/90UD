"""Local request format for the Stage3 native toast bridge.

The game server publishes one atomically replaced request. The injected
``usdof_func_extend`` DLL consumes it on DFO's verified main-window thread.
``comfirm`` retains the established generic prompt route. ``toast`` enqueues
server-provided text through the client's native toast queue. This is
deliberately a local sidechannel, not a Game packet or codec path.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TOAST_MAGIC = b"UTST"
COMFIRM_PROTOCOL_VERSION = 1
TOAST_PROTOCOL_VERSION = 2
TOAST_MODE_COMFIRM = "comfirm"
TOAST_MODE_TOAST = "toast"
MAX_UTF16_UNITS = 1023
MAX_UTF8_BYTES = MAX_UTF16_UNITS * 4

# V1/V2: magic, route version, UTF-8 byte length, request id, created Unix
# milliseconds, followed by exactly that many UTF-8 bytes.
_REQUEST_HEADER = struct.Struct("<4sHHQQ")


class ClientToastBridgeError(ValueError):
    """Raised when a native-toast request cannot be published safely."""


def _validate_mode(value: str) -> str:
    if not isinstance(value, str):
        raise ClientToastBridgeError("toast mode must be str")
    if value not in {TOAST_MODE_COMFIRM, TOAST_MODE_TOAST}:
        raise ClientToastBridgeError("toast mode is invalid")
    return value


def _validate_text(value: str) -> tuple[str, bytes]:
    if not isinstance(value, str):
        raise ClientToastBridgeError("toast text must be str")
    if not value:
        raise ClientToastBridgeError("toast text is empty")
    if "\x00" in value:
        raise ClientToastBridgeError("toast text contains NUL")
    try:
        utf16_units = len(value.encode("utf-16-le")) // 2
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ClientToastBridgeError("toast text is not encodable") from exc
    if utf16_units > MAX_UTF16_UNITS:
        raise ClientToastBridgeError("toast text exceeds 1023 UTF-16 units")
    if len(encoded) > MAX_UTF8_BYTES:
        raise ClientToastBridgeError("toast UTF-8 payload exceeds bridge limit")
    return value, encoded


@dataclass(frozen=True)
class ClientToastRequest:
    request_id: int
    created_unix_ms: int
    text: str
    mode: str = TOAST_MODE_COMFIRM

    def pack(self) -> bytes:
        request_id = int(self.request_id)
        created_unix_ms = int(self.created_unix_ms)
        if not 0 < request_id <= 0xFFFFFFFFFFFFFFFF:
            raise ClientToastBridgeError("toast request id is invalid")
        if not 0 < created_unix_ms <= 0xFFFFFFFFFFFFFFFF:
            raise ClientToastBridgeError("toast request timestamp is invalid")
        mode = _validate_mode(self.mode)
        text, encoded = _validate_text(self.text)
        del text
        if mode == TOAST_MODE_COMFIRM:
            protocol_version = COMFIRM_PROTOCOL_VERSION
        else:
            protocol_version = TOAST_PROTOCOL_VERSION
        return _REQUEST_HEADER.pack(
            TOAST_MAGIC,
            protocol_version,
            len(encoded),
            request_id,
            created_unix_ms,
        ) + encoded

    @classmethod
    def unpack(cls, value: bytes) -> "ClientToastRequest":
        if len(value) < _REQUEST_HEADER.size:
            raise ClientToastBridgeError("toast request is shorter than its header")
        magic, version, text_size, request_id, created_unix_ms = (
            _REQUEST_HEADER.unpack_from(value)
        )
        if not 0 < request_id <= 0xFFFFFFFFFFFFFFFF:
            raise ClientToastBridgeError("toast request id is invalid")
        if not 0 < created_unix_ms <= 0xFFFFFFFFFFFFFFFF:
            raise ClientToastBridgeError("toast request timestamp is invalid")
        if magic != TOAST_MAGIC:
            raise ClientToastBridgeError("toast request identity is invalid")
        if version == COMFIRM_PROTOCOL_VERSION:
            mode = TOAST_MODE_COMFIRM
        elif version == TOAST_PROTOCOL_VERSION:
            mode = TOAST_MODE_TOAST
        else:
            raise ClientToastBridgeError("toast request identity is invalid")
        if not 0 < text_size <= MAX_UTF8_BYTES:
            raise ClientToastBridgeError("toast request text length is invalid")
        if len(value) != _REQUEST_HEADER.size + text_size:
            raise ClientToastBridgeError("toast request payload length is invalid")
        try:
            text = value[_REQUEST_HEADER.size :].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ClientToastBridgeError("toast request text is not UTF-8") from exc
        _validate_text(text)
        return cls(
            request_id=int(request_id),
            created_unix_ms=int(created_unix_ms),
            text=text,
            mode=mode,
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def client_toast_request_path() -> Path:
    raw = str(
        os.environ.get("USDOF_FUNC_EXTEND_TOAST_REQUEST_PATH", "") or ""
    ).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (
        _repo_root()
        / "server_proto"
        / "data"
        / "runtime_cache"
        / "func_extend"
        / "USDOFFuncExtendToastRequest.bin"
    ).resolve()


def client_toast_bridge_enabled() -> bool:
    raw = str(os.environ.get("USDOF_FUNC_EXTEND", "1") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _atomic_write(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        "%s.tmp.%s.%s" % (target.name, os.getpid(), time.time_ns())
    )
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(target))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def write_client_toast_request(request: ClientToastRequest) -> Path:
    path = client_toast_request_path()
    _atomic_write(path, request.pack())
    return path


def read_client_toast_request(
    path: Optional[Path] = None,
) -> ClientToastRequest:
    source = Path(path) if path is not None else client_toast_request_path()
    return ClientToastRequest.unpack(source.read_bytes())


def send_client_toast(
    text: str,
    mode: str = TOAST_MODE_COMFIRM,
) -> ClientToastRequest:
    """Publish one toast request for the currently injected Stage3 client.

    The request slot is intentionally last-write-wins.  Server flows that need
    distinct visible prompts should issue the next request after the preceding
    UI event has settled instead of treating this as a packet queue.
    """
    if not client_toast_bridge_enabled():
        raise ClientToastBridgeError("USDOF_FUNC_EXTEND is disabled")
    request_id = int(time.time_ns()) & 0xFFFFFFFFFFFFFFFF
    if request_id <= 0:
        request_id = 1
    request = ClientToastRequest(
        request_id=request_id,
        created_unix_ms=max(1, int(time.time() * 1000)),
        text=text,
        mode=mode,
    )
    write_client_toast_request(request)
    return request


__all__ = [
    "ClientToastBridgeError",
    "ClientToastRequest",
    "COMFIRM_PROTOCOL_VERSION",
    "MAX_UTF16_UNITS",
    "MAX_UTF8_BYTES",
    "TOAST_MAGIC",
    "TOAST_MODE_COMFIRM",
    "TOAST_MODE_TOAST",
    "TOAST_PROTOCOL_VERSION",
    "client_toast_bridge_enabled",
    "client_toast_request_path",
    "read_client_toast_request",
    "send_client_toast",
    "write_client_toast_request",
]
