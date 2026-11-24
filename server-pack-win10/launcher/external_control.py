# -*- coding: utf-8 -*-
"""Persistent configuration shared with the standalone GM application.

The game launcher consumes only the loopback port.  The standalone GM process
owns the external listener, its bearer token, and all GM service controls.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from launcher.paths import external_control_path, launcher_settings_path, runtime_temp_dir


CONFIG_FILENAME = "external-control.json"
CONFIG_VERSION = 2
LISTEN_HOST = "0.0.0.0"
DEFAULT_CONNECT_HOST = "127.0.0.1"
DEFAULT_PORT = 17601
DEFAULT_INTERNAL_PORT = 17600


@dataclass(frozen=True)
class ExternalControlConfig:
    listen_host: str
    connect_host: str
    internal_port: int
    port: int
    token: str
    api_version: int = 1


def config_path(root: Path) -> Path:
    return external_control_path(Path(root))


def _valid_port(value: object, *, field: str = "port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _legacy_launcher_values(root: Path) -> Dict[str, Any]:
    path = launcher_settings_path(Path(root))
    if not path.is_file():
        path = Path(root) / "launcher-settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_config(root: Path, config: ExternalControlConfig) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CONFIG_VERSION,
        "listen_host": LISTEN_HOST,
        "connect_host": config.connect_host,
        "internal_port": int(config.internal_port),
        "port": int(config.port),
        "token": config.token,
        "api_version": int(config.api_version),
    }
    temp_dir = runtime_temp_dir(root)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = temp_dir / (path.name + ".%s.tmp" % os.getpid())
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temp), str(path))
    return path


def _strip_legacy_launcher_values(root: Path, legacy: Dict[str, Any]) -> None:
    keys = {"gm_api_enabled", "gm_api_host", "gm_api_port", "gm_api_token"}
    if not keys.intersection(legacy):
        return
    cleaned = {key: value for key, value in legacy.items() if key not in keys}
    path = launcher_settings_path(Path(root))
    if not path.is_file():
        legacy_path = Path(root) / "launcher-settings.json"
        if legacy_path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(legacy_path.read_bytes())
    if not path.is_file():
        return
    temp_dir = runtime_temp_dir(root)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp = temp_dir / (path.name + ".%s.tmp" % os.getpid())
    temp.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temp), str(path))
    try:
        (Path(root) / "launcher-settings.json").unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def ensure_external_control_config(root: Path) -> ExternalControlConfig:
    """Load or create listener credentials and migrate legacy launcher keys."""
    root = Path(root)
    path = config_path(root)
    legacy = _legacy_launcher_values(root)
    legacy_config_path = root / CONFIG_FILENAME
    read_path = path if path.is_file() else legacy_config_path
    try:
        raw = json.loads(read_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    token = str(raw.get("token") or legacy.get("gm_api_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
    port = _valid_port(raw.get("port", legacy.get("gm_api_port", DEFAULT_PORT)))
    internal_port = _valid_port(
        raw.get("internal_port", legacy.get("control_port", DEFAULT_INTERNAL_PORT))
    )
    if internal_port == port:
        internal_port = DEFAULT_INTERNAL_PORT if port != DEFAULT_INTERNAL_PORT else 17602
    connect_host = str(raw.get("connect_host") or DEFAULT_CONNECT_HOST).strip()
    if not connect_host:
        connect_host = DEFAULT_CONNECT_HOST
    config = ExternalControlConfig(
        listen_host=LISTEN_HOST,
        connect_host=connect_host,
        internal_port=internal_port,
        port=port,
        token=token,
        api_version=1,
    )
    _write_config(root, config)
    _strip_legacy_launcher_values(root, legacy)
    if legacy_config_path != path:
        try:
            legacy_config_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return config


def regenerate_external_control_token(root: Path) -> ExternalControlConfig:
    current = ensure_external_control_config(root)
    updated = ExternalControlConfig(
        listen_host=LISTEN_HOST,
        connect_host=current.connect_host,
        internal_port=current.internal_port,
        port=current.port,
        token=secrets.token_urlsafe(32),
        api_version=current.api_version,
    )
    _write_config(Path(root), updated)
    return updated


def update_external_control_config(
    root: Path,
    *,
    port: int,
    internal_port: int = DEFAULT_INTERNAL_PORT,
    token: str,
    connect_host: str = DEFAULT_CONNECT_HOST,
) -> ExternalControlConfig:
    """Persist listener credentials edited by the independent control console."""
    port_value = _valid_port(port)
    internal_port_value = _valid_port(internal_port, field="internal_port")
    if int(port_value) != int(port):
        raise ValueError("port must be between 1 and 65535")
    if int(internal_port_value) != int(internal_port):
        raise ValueError("internal_port must be between 1 and 65535")
    if internal_port_value == port_value:
        raise ValueError("internal_port and port must be different")
    token_value = str(token or "").strip()
    if not token_value:
        raise ValueError("token is required")
    connect_value = str(connect_host or "").strip()
    if not connect_value or "://" in connect_value or "/" in connect_value:
        raise ValueError("connect_host must be a hostname or IP address")
    updated = ExternalControlConfig(
        listen_host=LISTEN_HOST,
        connect_host=connect_value,
        internal_port=internal_port_value,
        port=port_value,
        token=token_value,
        api_version=1,
    )
    _write_config(Path(root), updated)
    return updated


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_CONNECT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_INTERNAL_PORT",
    "ExternalControlConfig",
    "LISTEN_HOST",
    "config_path",
    "ensure_external_control_config",
    "regenerate_external_control_token",
    "update_external_control_config",
]
