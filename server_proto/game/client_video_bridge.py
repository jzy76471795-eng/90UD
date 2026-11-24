"""Atomic local command bridge for the Stage3 native video player.

``usdof_func_extend.dll`` polls this one-slot sidechannel from the existing
main-window ``WH_GETMESSAGE`` owner.  A play command invokes only the verified
``Ready -> Open -> Play`` route; stop invokes the verified native ``Stop``.
This module does not publish a Game packet or participate in codec handling.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


VIDEO_COMMAND_MAGIC = b"UVPC"
VIDEO_COMMAND_VERSION = 1
VIDEO_ACTION_PLAY = 1
VIDEO_ACTION_STOP = 2
VIDEO_ACTION_NAME_PLAY = "play"
VIDEO_ACTION_NAME_STOP = "stop"
MAX_VIDEO_PATH_UTF8_BYTES = 4095

# magic, version, action, request id, created Unix milliseconds, UTF-8 bytes
_VIDEO_COMMAND_HEADER = struct.Struct("<4sHHQQI")
_VIDEO_COMMAND_FILENAME = "USDOFFuncExtendVideoCommand.bin"


class ClientVideoBridgeError(ValueError):
    """Raised when a native-video command violates the local bridge contract."""


def _truthy_environment(name: str, default: str = "1") -> bool:
    raw = str(os.environ.get(name, default) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def client_video_bridge_enabled() -> bool:
    """Return whether the unified extension and its video component are active."""

    return _truthy_environment("USDOF_FUNC_EXTEND") and _truthy_environment(
        "USDOF_FUNC_EXTEND_VIDEO"
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def client_video_command_path() -> Path:
    raw = str(
        os.environ.get("USDOF_FUNC_EXTEND_VIDEO_COMMAND_PATH", "") or ""
    ).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (
        _repo_root()
        / "server_proto"
        / "data"
        / "runtime_cache"
        / "func_extend"
        / _VIDEO_COMMAND_FILENAME
    ).resolve()


def _validate_request_identity(request_id: int, created_unix_ms: int) -> None:
    if not 0 < int(request_id) <= 0xFFFFFFFFFFFFFFFF:
        raise ClientVideoBridgeError("video request id is invalid")
    if not 0 < int(created_unix_ms) <= 0xFFFFFFFFFFFFFFFF:
        raise ClientVideoBridgeError("video request timestamp is invalid")


def _encode_video_path(path: str) -> bytes:
    if not isinstance(path, str) or not path:
        raise ClientVideoBridgeError("video path is required for play")
    if "\x00" in path or "\r" in path or "\n" in path:
        raise ClientVideoBridgeError("video path contains a control character")
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ClientVideoBridgeError("video path is not UTF-8 encodable") from exc
    if not encoded or len(encoded) > MAX_VIDEO_PATH_UTF8_BYTES:
        raise ClientVideoBridgeError("video path exceeds the bridge limit")
    return encoded


def _validate_local_avi_path(value: Union[str, os.PathLike[str]]) -> Path:
    try:
        source = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ClientVideoBridgeError("video path is invalid") from exc
    if not source.is_absolute():
        raise ClientVideoBridgeError("video path must be absolute")
    try:
        resolved = source.resolve(strict=True)
        if not resolved.is_file():
            raise ClientVideoBridgeError("video path is not a file")
        with resolved.open("rb") as stream:
            header = stream.read(12)
    except ClientVideoBridgeError:
        raise
    except (OSError, ValueError) as exc:
        raise ClientVideoBridgeError("video path is not readable") from exc
    if resolved.suffix.lower() != ".avi":
        raise ClientVideoBridgeError("video path must end in .avi")
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"AVI ":
        raise ClientVideoBridgeError("video file is not RIFF/AVI")
    _encode_video_path(str(resolved))
    return resolved


@dataclass(frozen=True)
class ClientVideoCommand:
    request_id: int
    created_unix_ms: int
    action: str
    path: Optional[str] = None

    def pack(self) -> bytes:
        _validate_request_identity(self.request_id, self.created_unix_ms)
        if self.action == VIDEO_ACTION_NAME_PLAY:
            action_code = VIDEO_ACTION_PLAY
            path_bytes = _encode_video_path(str(self.path or ""))
        elif self.action == VIDEO_ACTION_NAME_STOP:
            action_code = VIDEO_ACTION_STOP
            if self.path not in {None, ""}:
                raise ClientVideoBridgeError("stop command does not accept a path")
            path_bytes = b""
        else:
            raise ClientVideoBridgeError("video action must be play or stop")
        return _VIDEO_COMMAND_HEADER.pack(
            VIDEO_COMMAND_MAGIC,
            VIDEO_COMMAND_VERSION,
            action_code,
            int(self.request_id),
            int(self.created_unix_ms),
            len(path_bytes),
        ) + path_bytes

    @classmethod
    def unpack(cls, value: bytes) -> "ClientVideoCommand":
        if len(value) < _VIDEO_COMMAND_HEADER.size:
            raise ClientVideoBridgeError("video command is shorter than its header")
        magic, version, action_code, request_id, created_unix_ms, path_size = (
            _VIDEO_COMMAND_HEADER.unpack_from(value)
        )
        if magic != VIDEO_COMMAND_MAGIC or version != VIDEO_COMMAND_VERSION:
            raise ClientVideoBridgeError("video command identity is invalid")
        _validate_request_identity(request_id, created_unix_ms)
        if path_size > MAX_VIDEO_PATH_UTF8_BYTES:
            raise ClientVideoBridgeError("video command path length is invalid")
        if len(value) != _VIDEO_COMMAND_HEADER.size + int(path_size):
            raise ClientVideoBridgeError("video command payload length is invalid")
        payload = value[_VIDEO_COMMAND_HEADER.size :]
        if action_code == VIDEO_ACTION_PLAY:
            if not payload:
                raise ClientVideoBridgeError("play command path is empty")
            try:
                path = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ClientVideoBridgeError("video command path is not UTF-8") from exc
            _encode_video_path(path)
            action = VIDEO_ACTION_NAME_PLAY
        elif action_code == VIDEO_ACTION_STOP:
            if payload:
                raise ClientVideoBridgeError("stop command path must be empty")
            path = None
            action = VIDEO_ACTION_NAME_STOP
        else:
            raise ClientVideoBridgeError("video command action is invalid")
        return cls(
            request_id=int(request_id),
            created_unix_ms=int(created_unix_ms),
            action=action,
            path=path,
        )

    def as_dict(self) -> dict:
        result = {
            "action": self.action,
            "request_id": int(self.request_id),
            "created_unix_ms": int(self.created_unix_ms),
        }
        if self.path is not None:
            result["path"] = self.path
        return result


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


def write_client_video_command(
    command: ClientVideoCommand,
    path: Optional[Path] = None,
) -> Path:
    target = Path(path) if path is not None else client_video_command_path()
    _atomic_write(target, command.pack())
    return target


def read_client_video_command(path: Optional[Path] = None) -> ClientVideoCommand:
    source = Path(path) if path is not None else client_video_command_path()
    return ClientVideoCommand.unpack(source.read_bytes())


def _new_command(action: str, path: Optional[str] = None) -> ClientVideoCommand:
    request_id = int(time.time_ns()) & 0xFFFFFFFFFFFFFFFF
    if request_id <= 0:
        request_id = 1
    return ClientVideoCommand(
        request_id=request_id,
        created_unix_ms=max(1, int(time.time() * 1000)),
        action=action,
        path=path,
    )


def play_client_video(
    path: Union[str, os.PathLike[str]],
    *,
    command_path: Optional[Path] = None,
) -> ClientVideoCommand:
    """Publish one validated local AVI play command for the active client."""

    if not client_video_bridge_enabled():
        raise ClientVideoBridgeError("client video extension is disabled")
    source = _validate_local_avi_path(path)
    command = _new_command(VIDEO_ACTION_NAME_PLAY, str(source))
    write_client_video_command(command, path=command_path)
    return command


def stop_client_video(*, command_path: Optional[Path] = None) -> ClientVideoCommand:
    """Publish one native Stop command for the active client."""

    if not client_video_bridge_enabled():
        raise ClientVideoBridgeError("client video extension is disabled")
    command = _new_command(VIDEO_ACTION_NAME_STOP)
    write_client_video_command(command, path=command_path)
    return command


__all__ = [
    "ClientVideoBridgeError",
    "ClientVideoCommand",
    "MAX_VIDEO_PATH_UTF8_BYTES",
    "VIDEO_ACTION_NAME_PLAY",
    "VIDEO_ACTION_NAME_STOP",
    "VIDEO_ACTION_PLAY",
    "VIDEO_ACTION_STOP",
    "VIDEO_COMMAND_MAGIC",
    "VIDEO_COMMAND_VERSION",
    "client_video_bridge_enabled",
    "client_video_command_path",
    "play_client_video",
    "read_client_video_command",
    "stop_client_video",
    "write_client_video_command",
]
