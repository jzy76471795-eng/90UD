from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from typing import Any, Optional


DIRECTORY_CACHE_MAGIC = b"USDPVFD1"
DIRECTORY_CACHE_SCHEMA = 1
DIRECTORY_SEMANTIC_VERSION = 1
_HEADER = struct.Struct("<8sIIQ32s32s")
_MAX_IDENTITY_BYTES = 64 * 1024
_MAX_PAYLOAD_BYTES = 1024 * 1024 * 1024
_LOCK = threading.RLock()


@dataclass(frozen=True)
class DirectoryIdentity:
    canonical_path: str
    source_sha256: str
    source_size: int
    file_count: int
    group_count: int
    profile_name: str
    python_implementation: str
    python_version: str
    native_abi_version: int
    directory_semantic_version: int


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "n"}


def build_identity(
    *,
    path: pathlib.Path,
    source_sha256: str,
    source_size: int,
    file_count: int,
    group_count: int,
    profile_name: str,
    native_abi_version: int,
) -> DirectoryIdentity:
    canonical_path = os.path.normcase(str(path.resolve()))
    return DirectoryIdentity(
        canonical_path=canonical_path,
        source_sha256=str(source_sha256).lower(),
        source_size=int(source_size),
        file_count=int(file_count),
        group_count=int(group_count),
        profile_name=str(profile_name),
        python_implementation=sys.implementation.name,
        python_version="%d.%d.%d" % sys.version_info[:3],
        native_abi_version=int(native_abi_version),
        directory_semantic_version=DIRECTORY_SEMANTIC_VERSION,
    )


def _identity_bytes(identity: DirectoryIdentity) -> bytes:
    return json.dumps(
        asdict(identity),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def cache_path(identity: DirectoryIdentity) -> pathlib.Path:
    configured = os.environ.get("USDOF_PVF_INDEX_CACHE_DIR", "").strip()
    if configured:
        root = pathlib.Path(configured).expanduser().resolve()
    else:
        root = (
            pathlib.Path(__file__).resolve().parents[1]
            / "data"
            / "runtime_cache"
            / "pvf_indexes"
        )
    path_token = hashlib.sha256(
        identity.canonical_path.encode("utf-8")
    ).hexdigest()[:24]
    return (
        root
        / "native_directory"
        / path_token
        / identity.source_sha256
        / "pvf_directory.bin"
    )


def _encode(identity: DirectoryIdentity, payload: bytes) -> bytes:
    identity_raw = _identity_bytes(identity)
    if len(identity_raw) > _MAX_IDENTITY_BYTES:
        raise ValueError("native PVF directory identity is too large")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError("native PVF directory payload is too large")
    return b"".join(
        [
            _HEADER.pack(
                DIRECTORY_CACHE_MAGIC,
                DIRECTORY_CACHE_SCHEMA,
                len(identity_raw),
                len(payload),
                hashlib.sha256(identity_raw).digest(),
                hashlib.sha256(payload).digest(),
            ),
            identity_raw,
            payload,
        ]
    )


def _decode(raw: bytes, identity: DirectoryIdentity) -> bytes:
    if len(raw) < _HEADER.size:
        raise ValueError("native PVF directory cache header is truncated")
    (
        magic,
        schema,
        identity_size,
        payload_size,
        identity_sha256,
        payload_sha256,
    ) = _HEADER.unpack_from(raw)
    if magic != DIRECTORY_CACHE_MAGIC:
        raise ValueError("native PVF directory cache magic mismatch")
    if schema != DIRECTORY_CACHE_SCHEMA:
        raise ValueError("native PVF directory cache schema mismatch")
    if identity_size > _MAX_IDENTITY_BYTES or payload_size > _MAX_PAYLOAD_BYTES:
        raise ValueError("native PVF directory cache length exceeds limit")
    expected_size = _HEADER.size + identity_size + payload_size
    if expected_size != len(raw):
        raise ValueError("native PVF directory cache length mismatch")
    identity_raw = raw[_HEADER.size : _HEADER.size + identity_size]
    payload = raw[_HEADER.size + identity_size :]
    if hashlib.sha256(identity_raw).digest() != identity_sha256:
        raise ValueError("native PVF directory identity checksum mismatch")
    if hashlib.sha256(payload).digest() != payload_sha256:
        raise ValueError("native PVF directory payload checksum mismatch")
    cached_identity = json.loads(identity_raw.decode("utf-8"))
    if cached_identity != asdict(identity):
        raise ValueError("native PVF directory identity mismatch")
    return payload


def _remove_file(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_directory(
    identity: DirectoryIdentity,
    native: Any,
    group_sizes: list[int],
) -> tuple[Optional[Any], str, pathlib.Path]:
    path = cache_path(identity)
    if not _env_bool("USDOF_PVF_INDEX_CACHE", True):
        return None, "disabled", path
    if _env_bool("USDOF_PVF_INDEX_CACHE_CLEAR", False):
        _remove_file(path)
        return None, "rebuild", path
    if not path.is_file():
        return None, "miss", path
    try:
        raw = path.read_bytes()
        payload = _decode(raw, identity)
        directory = native.load_directory(payload)
        if len(directory) != identity.file_count:
            raise ValueError("native PVF directory file count mismatch")
        directory.validate(group_sizes)
        return directory, "hit", path
    except Exception:
        _remove_file(path)
        return None, "rebuild", path


def store_directory(
    identity: DirectoryIdentity,
    native: Any,
    directory: Any,
) -> pathlib.Path:
    path = cache_path(identity)
    if not _env_bool("USDOF_PVF_INDEX_CACHE", True):
        return path
    payload = native.dump_directory(directory)
    encoded = _encode(identity, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            verified = pathlib.Path(temp_name).read_bytes()
            if _decode(verified, identity) != payload:
                raise ValueError("native PVF directory cache publish verification failed")
            os.replace(temp_name, str(path))
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return path


__all__ = [
    "DIRECTORY_CACHE_SCHEMA",
    "DIRECTORY_SEMANTIC_VERSION",
    "DirectoryIdentity",
    "build_identity",
    "cache_path",
    "load_directory",
    "store_directory",
]
