# -*- coding: utf-8 -*-
"""Managed player DLLs stored beside the selected client.

The share launcher keeps the source selected in the panel separate from the
runtime copy that is injected.  This lets players move or replace the source
file without invalidating the next launch, while the client directory remains
the stable location used by the launcher.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence


EXTEND_DLL_DIR_NAME = "us_extend_dll"
ARCHIVE_DIR_NAME = "delete"


def external_dll_dir(client_root: Path) -> Path:
    return Path(client_root).expanduser().resolve() / EXTEND_DLL_DIR_NAME


def external_dll_archive_dir(client_root: Path) -> Path:
    return external_dll_dir(client_root) / ARCHIVE_DIR_NAME


def _dll_files(directory: Path) -> List[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        (
            path
            for path in entries
            if path.is_file() and path.suffix.casefold() == ".dll"
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _unique_path(path: Path, *, add_date_on_collision: bool = False) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    if add_date_on_collision:
        stamp = datetime.now().strftime("%Y%m%d")
        candidate = path.with_name("%s_%s%s" % (stem, stamp, suffix))
        if not candidate.exists():
            return candidate
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = path.with_name("%s_%s%s" % (stem, stamp, suffix))
        if not candidate.exists():
            return candidate
        index = 2
        while True:
            candidate = path.with_name("%s_%s_%d%s" % (stem, stamp, index, suffix))
            if not candidate.exists():
                return candidate
            index += 1
    index = 2
    while True:
        candidate = path.with_name("%s_%d%s" % (stem, index, suffix))
        if not candidate.exists():
            return candidate
        index += 1


def _active_by_name(client_root: Path) -> dict[str, Path]:
    return {
        path.name.casefold(): path
        for path in _dll_files(external_dll_dir(client_root))
    }


def install_external_dll(client_root: Path, source: Path) -> Path:
    """Copy ``source`` into ``us_extend_dll`` and return the injected copy."""
    source = Path(source).expanduser().resolve()
    if source.suffix.casefold() != ".dll":
        raise ValueError("外置启动组件必须是 .dll 文件：%s" % source)
    if not source.is_file():
        raise FileNotFoundError("外置启动组件不存在：%s" % source)

    active_dir = external_dll_dir(client_root)
    active_dir.mkdir(parents=True, exist_ok=True)
    existing = _active_by_name(client_root).get(source.name.casefold())
    if existing is not None:
        try:
            if existing.stat().st_size == source.stat().st_size:
                # Avoid needless copies when the selected item is already the
                # managed active copy or is byte-for-byte identical.
                if _same_path(existing, source) or existing.read_bytes() == source.read_bytes():
                    return existing
        except OSError:
            pass
        destination = _unique_path(active_dir / source.name, add_date_on_collision=True)
    else:
        destination = active_dir / source.name
    shutil.copy2(str(source), str(destination))
    return destination


def _archive_destination(client_root: Path, source: Path) -> Path:
    archive_dir = external_dll_archive_dir(client_root)
    archive_dir.mkdir(parents=True, exist_ok=True)
    return _unique_path(archive_dir / source.name, add_date_on_collision=True)


def archive_external_dll(client_root: Path, selected: Path) -> Optional[Path]:
    """Archive a managed DLL, or copy a legacy external DLL into ``delete``.

    Managed copies are moved so the active directory no longer loads them.
    Legacy settings may still point outside the client; those originals are
    left untouched and an archive copy is created for the removal record.
    """
    selected = Path(selected).expanduser().resolve()
    if selected.suffix.casefold() != ".dll":
        raise ValueError("外置启动组件必须是 .dll 文件：%s" % selected)
    active_dir = external_dll_dir(client_root)
    active = selected if selected.parent == active_dir else _active_by_name(client_root).get(
        selected.name.casefold(), selected
    )
    if not active.is_file():
        return None
    destination = _archive_destination(client_root, active)
    if _same_path(active, selected) or active.parent == active_dir:
        shutil.move(str(active), str(destination))
    else:
        shutil.copy2(str(active), str(destination))
    return destination


def preferred_external_dll_paths(
    client_root: Path,
    values: Sequence[object],
    *,
    base_root: Optional[Path] = None,
) -> List[str]:
    """Return settings paths with managed client copies taking precedence.

    Managed client copies are returned before legacy external paths.  The
    configured order is retained within each group, and DLLs manually placed
    in the managed folder become active without requiring a panel edit.
    """
    managed = _active_by_name(client_root)
    managed_result: List[str] = []
    legacy_result: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            continue
        raw = value.strip().strip('"')
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(raw))
        path = Path(expanded)
        if not path.is_absolute():
            path = Path(base_root or client_root).resolve() / path
        path = Path(os.path.abspath(str(path)))
        candidate = managed.get(path.name.casefold()) or path
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        target = managed_result if path.name.casefold() in managed else legacy_result
        target.append(str(candidate))
    for path in managed.values():
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            managed_result.append(str(path))
    return managed_result + legacy_result


def resolve_managed_external_dll_paths(client_root: Path) -> List[Path]:
    """Return all valid DLLs currently present in the managed folder."""
    return _dll_files(external_dll_dir(client_root))
