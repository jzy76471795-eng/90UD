from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from config import PVF_PATH
from pvf.nkpi_reader import PvfReader


@dataclass(frozen=True)
class PvfPreloadStats:
    path: str
    file_size: int
    file_count: int
    group_count: int
    path_index_ready: bool
    groups_preloaded: bool
    decompressed_bytes: int
    elapsed_ms: int
    memory_backed: bool
    runtime_frozen: bool
    source_sha256: str = ""
    profile_name: str = ""
    guarded: bool = False
    bridge_required: bool = False
    group_residency: str = "full"
    resident_group_count: int = 0
    resident_decompressed_bytes: int = 0
    runtime_group_load_count: int = 0
    runtime_group_load_bytes: int = 0
    directory_cache_status: str = "uninitialized"


_LOCK = threading.RLock()
_READER: PvfReader | None = None
_READER_PATH: str | None = None
_LAST_STATS: PvfPreloadStats | None = None


def _norm_path(path: str | os.PathLike[str] | None) -> str:
    return str(Path(path if path is not None else PVF_PATH).resolve())


def last_preload_stats() -> PvfPreloadStats | None:
    with _LOCK:
        return _LAST_STATS


def source_sha256(path: str | os.PathLike[str] | None = None) -> str:
    """Return the SHA-256 verified while opening the shared PVF reader."""
    target = _norm_path(path)
    with _LOCK:
        if _READER is None or _READER_PATH != target or _LAST_STATS is None:
            raise RuntimeError("PVF SHA requested before shared reader open: %s" % target)
        value = str(_LAST_STATS.source_sha256 or "")
        if not value:
            raise RuntimeError("shared PVF preload has no source SHA-256")
        return value


def is_preloaded(path: str | os.PathLike[str] | None = None) -> bool:
    """Return whether the historical all-group runtime mapping is available."""
    target = _norm_path(path)
    with _LOCK:
        return (
            _READER is not None
            and _READER_PATH == target
            and _READER.groups_preloaded
            and _READER.is_memory_backed
            and _READER.path_index_ready
            and _READER.name_pools_ready
            and _READER.all_groups_resident
            and _READER.runtime_frozen
        )


def is_memory_prepared(path: str | os.PathLike[str] | None = None) -> bool:
    """Return whether startup has prepared the shared in-memory PVF structure.

    This is valid while boot domains are running, before a selective reader is
    frozen.  It deliberately requires no body group residency.
    """
    target = _norm_path(path)
    with _LOCK:
        return bool(
            _READER is not None
            and _READER_PATH == target
            and _LAST_STATS is not None
            and _READER.is_memory_backed
            and _READER.path_index_ready
            and _READER.name_pools_ready
        )


def get_shared_reader(path: str | os.PathLike[str] | None = None) -> PvfReader | None:
    target = _norm_path(path)
    with _LOCK:
        if _READER is not None and _READER_PATH == target:
            return _READER
        return None


def close_shared_reader() -> None:
    global _READER, _READER_PATH, _LAST_STATS
    with _LOCK:
        if _READER is not None:
            try:
                _READER.close()
            except Exception:
                pass
        _READER = None
        _READER_PATH = None
        _LAST_STATS = None


def _validate_expected_identity(
    *,
    target: str,
    profile_name: str,
    source_sha256: str,
) -> None:
    expected_profile = os.environ.get("USDOF_EXPECTED_PVF_PROFILE", "").strip()
    expected_sha256 = os.environ.get("USDOF_EXPECTED_PVF_SHA256", "").strip().lower()
    if expected_profile and profile_name != expected_profile:
        raise RuntimeError(
            "Script.pvf profile changed after launcher detection: "
            "expected=%s actual=%s path=%s"
            % (expected_profile, profile_name, target)
        )
    if expected_sha256 and source_sha256.lower() != expected_sha256:
        raise RuntimeError(
            "Script.pvf SHA-256 changed after launcher detection: "
            "expected=%s actual=%s path=%s"
            % (expected_sha256, source_sha256, target)
        )


def is_runtime_ready(path: str | os.PathLike[str] | None = None) -> bool:
    """Return whether a frozen memory-backed reader can serve gameplay.

    Selective boot deliberately leaves unused body groups compressed.  It is
    still runtime-ready because all structural metadata is resident and a
    missing group can only be materialized from the retained PVF bytes.
    """
    target = _norm_path(path)
    with _LOCK:
        return bool(
            _READER is not None
            and _READER_PATH == target
            and _LAST_STATS is not None
            and _READER.is_memory_backed
            and _READER.path_index_ready
            and _READER.name_pools_ready
            and _READER.runtime_frozen
        )


def open_script_pvf_on_demand(
    path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> PvfPreloadStats:
    """Open one verified shared reader without full group decompression.

    This is intentionally an explicit launcher-driven quick-start path for
    frequent Script.pvf iteration.  It still performs the strict profile and
    SHA-256 verification used by the launcher, but leaves body groups and
    domain indexes unloaded until a feature reads them through this shared
    reader. Normal server startup uses selective memory preparation plus hard
    domain warmup instead.
    """

    global _READER, _READER_PATH, _LAST_STATS

    target = _norm_path(path)
    started = time.perf_counter()
    with _LOCK:
        if (
            not force
            and _READER is not None
            and _READER_PATH == target
            and _LAST_STATS is not None
        ):
            return _LAST_STATS

        # Detection reads a stable snapshot and validates the NKPI profile
        # before the disk-backed reader is shared with Channel/Game.
        from pvf.nkpi_reader import detect_pvf_profile

        detected = detect_pvf_profile(target)
        _validate_expected_identity(
            target=target,
            profile_name=detected.profile_name,
            source_sha256=detected.source_sha256,
        )

        if _READER is not None:
            try:
                _READER.close()
            except Exception:
                pass
            _READER = None
            _READER_PATH = None
            _LAST_STATS = None

        reader = PvfReader.open(
            target,
            memory=False,
            preload_groups=False,
            group_cache_limit=16,
            source_sha256=detected.source_sha256,
        )
        try:
            if reader.profile_name != detected.profile_name:
                raise RuntimeError(
                    "Script.pvf profile changed after quick-start detection: "
                    "expected=%s actual=%s path=%s"
                    % (detected.profile_name, reader.profile_name, target)
                )
            stats = PvfPreloadStats(
                path=target,
                file_size=int(reader.file_size),
                file_count=int(reader.file_count),
                group_count=int(reader.group_count),
                path_index_ready=False,
                groups_preloaded=False,
                decompressed_bytes=0,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                memory_backed=False,
                runtime_frozen=False,
                source_sha256=detected.source_sha256,
                profile_name=reader.profile_name,
                guarded=reader.profile.guarded,
                bridge_required=reader.profile.bridge_required,
            )
        except Exception:
            reader.close()
            raise

        _READER = reader
        _READER_PATH = target
        _LAST_STATS = stats
        return stats


def prepare_script_pvf_selective_runtime(
    path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> PvfPreloadStats:
    """Open a verified memory reader without eagerly decompressing all groups.

    Startup callers must run their required domain warmups and then call
    :func:`freeze_script_pvf_selective_runtime`.  The raw PVF, path index,
    name pools, and group table are retained before any listener starts; only
    body groups actually read by those domains are decompressed.
    """
    global _READER, _READER_PATH, _LAST_STATS

    target = _norm_path(path)
    with _LOCK:
        if (
            not force
            and _READER is not None
            and _READER_PATH == target
            and _LAST_STATS is not None
            and _LAST_STATS.group_residency == "selective"
        ):
            return _LAST_STATS

        if _READER is not None:
            try:
                _READER.close()
            except Exception:
                pass
            _READER = None
            _READER_PATH = None
            _LAST_STATS = None

        started = time.perf_counter()
        reader = PvfReader.open(
            target,
            memory=True,
            preload_groups=False,
            # Selective runtime keeps every group it does materialize so a
            # later read never evicts a verified startup result.
            group_cache_limit=0,
        )
        try:
            reader._ensure_path_index()
            reader._ensure_groups()
            if getattr(reader, "_file_bytes", None) is None:
                raise RuntimeError("memory PVF preparation unexpectedly has no source bytes")
            source_digest = reader.source_sha256
            _validate_expected_identity(
                target=target,
                profile_name=reader.profile_name,
                source_sha256=source_digest,
            )
            stats = PvfPreloadStats(
                path=target,
                file_size=int(reader.file_size),
                file_count=int(reader.file_count),
                group_count=int(reader.group_count),
                path_index_ready=reader.path_index_ready,
                groups_preloaded=False,
                decompressed_bytes=0,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                memory_backed=True,
                runtime_frozen=False,
                source_sha256=source_digest,
                profile_name=reader.profile_name,
                guarded=reader.profile.guarded,
                bridge_required=reader.profile.bridge_required,
                group_residency="selective",
                resident_group_count=0,
                resident_decompressed_bytes=0,
                directory_cache_status=reader.directory_cache_status,
            )
        except Exception:
            reader.close()
            raise

        _READER = reader
        _READER_PATH = target
        _LAST_STATS = stats
        return stats


def freeze_script_pvf_selective_runtime(
    path: str | os.PathLike[str] | None = None,
) -> PvfPreloadStats:
    """Freeze a selectively warmed reader after all required boot domains pass."""
    global _LAST_STATS

    target = _norm_path(path)
    with _LOCK:
        if _READER is None or _READER_PATH != target or _LAST_STATS is None:
            raise RuntimeError("selective PVF runtime was not prepared: %s" % target)
        if _LAST_STATS.group_residency != "selective":
            raise RuntimeError("shared PVF reader is not in selective startup mode")
        _READER.freeze_runtime(allow_partial_groups=True)
        _LAST_STATS = replace(
            _LAST_STATS,
            runtime_frozen=True,
            groups_preloaded=bool(_READER.groups_preloaded),
            resident_group_count=_READER.resident_group_count,
            resident_decompressed_bytes=_READER.resident_decompressed_bytes,
            decompressed_bytes=_READER.resident_decompressed_bytes,
            runtime_group_load_count=_READER.runtime_group_load_count,
            runtime_group_load_bytes=_READER.runtime_group_load_bytes,
        )
        return _LAST_STATS


def runtime_group_residency(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, int | str]:
    """Expose current selective/full group residency without touching the PVF."""
    target = _norm_path(path)
    with _LOCK:
        if _READER is None or _READER_PATH != target or _LAST_STATS is None:
            raise RuntimeError("PVF reader unavailable: %s" % target)
        return {
            "mode": _LAST_STATS.group_residency,
            "group_count": int(_READER.group_count),
            "resident_group_count": int(_READER.resident_group_count),
            "resident_decompressed_bytes": int(_READER.resident_decompressed_bytes),
            "runtime_group_load_count": int(_READER.runtime_group_load_count),
            "runtime_group_load_bytes": int(_READER.runtime_group_load_bytes),
        }


def preload_script_pvf(
    path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> PvfPreloadStats:
    """Always load the whole Script.pvf into process memory.

    Steps:
    1. read the entire NKPI file into RAM
    2. build the full path index
    3. decompress every body group into RAM

    After this returns, read_text/read_raw only slice the in-memory groups.
    """

    global _READER, _READER_PATH, _LAST_STATS

    target = _norm_path(path)
    with _LOCK:
        if (
            not force
            and _READER is not None
            and _READER_PATH == target
            and _LAST_STATS is not None
            and _READER.groups_preloaded
            and _READER.is_memory_backed
        ):
            return _LAST_STATS

        if _READER is not None:
            try:
                _READER.close()
            except Exception:
                pass
            _READER = None
            _READER_PATH = None
            _LAST_STATS = None

        started = time.perf_counter()
        reader = PvfReader.open(
            target,
            memory=True,
            preload_groups=True,
            group_cache_limit=0,
        )
        # open(preload_groups=True) already decompressed groups; still force path index.
        reader._ensure_path_index()
        decompressed = sum(len(data) for data in reader._group_cache.values())
        if not reader.groups_preloaded:
            decompressed = reader.preload_all_groups()
        reader.freeze_runtime()

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # PvfReader.open(memory=True) retains the exact on-disk bytes. Hash that
        # buffer so index-cache identity never causes a second Script.pvf scan.
        if getattr(reader, "_file_bytes", None) is None:
            raise RuntimeError("memory PVF preload unexpectedly has no source bytes")
        stats = PvfPreloadStats(
            path=target,
            file_size=int(reader.file_size),
            file_count=int(reader.file_count),
            group_count=int(reader.group_count),
            path_index_ready=True,
            groups_preloaded=True,
            decompressed_bytes=int(decompressed),
            elapsed_ms=elapsed_ms,
            memory_backed=True,
            runtime_frozen=reader.runtime_frozen,
            source_sha256=reader.source_sha256,
            profile_name=reader.profile_name,
            guarded=reader.profile.guarded,
            bridge_required=reader.profile.bridge_required,
            group_residency="full",
            resident_group_count=int(reader.resident_group_count),
            resident_decompressed_bytes=int(reader.resident_decompressed_bytes),
            directory_cache_status=reader.directory_cache_status,
        )
        try:
            _validate_expected_identity(
                target=target,
                profile_name=stats.profile_name,
                source_sha256=stats.source_sha256,
            )
        except Exception:
            reader.close()
            raise
        _READER = reader
        _READER_PATH = target
        _LAST_STATS = stats
        return stats


def ensure_shared_reader(path: str | os.PathLike[str] | None = None) -> PvfReader:
    target = _norm_path(path)
    with _LOCK:
        if (
            _READER is not None
            and _READER_PATH == target
            and _READER.is_memory_backed
            and _READER.path_index_ready
            and _READER.name_pools_ready
        ):
            return _READER
    stats = preload_script_pvf(target)
    reader = get_shared_reader(target)
    if reader is None:
        raise RuntimeError(f"failed to preload PVF: {stats.path}")
    return reader


def get_or_open_shared_reader(path: str | os.PathLike[str] | None = None) -> PvfReader:
    """Return the current shared reader without escalating quick-start mode.

    Normal startup already has a frozen shared memory reader, with required
    groups resident and rare groups materialized only from retained PVF bytes.
    In explicit quick mode this returns the disk-backed reader so ordinary PVF
    reads stay on demand; callers that require a memory mapping use
    ``ensure_shared_reader``.
    """
    target = _norm_path(path)
    with _LOCK:
        if _READER is not None and _READER_PATH == target and _LAST_STATS is not None:
            return _READER
    return ensure_shared_reader(target)


def read_text(path: str, *, pvf_path: str | os.PathLike[str] | None = None) -> str:
    reader = get_or_open_shared_reader(pvf_path)
    return reader.read_text(path)


def read_raw(path: str, *, pvf_path: str | os.PathLike[str] | None = None) -> bytes:
    reader = get_or_open_shared_reader(pvf_path)
    return reader.read_raw(path)


def list_paths(prefix: str | None = None, *, pvf_path: str | os.PathLike[str] | None = None) -> list[str]:
    reader = get_or_open_shared_reader(pvf_path)
    return reader.list_paths(prefix)


def search_paths(
    query: str,
    *,
    prefix: str | None = None,
    limit: int = 0,
    pvf_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    reader = get_or_open_shared_reader(pvf_path)
    return reader.search_paths(query, prefix=prefix, limit=limit)
