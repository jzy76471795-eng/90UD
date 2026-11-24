"""Dedicated process for building the GM-only PVF item catalog.

The game process supplies the verified PVF identity and cache directory.  The
child opens its own disk-backed NKPI reader, reads only the two item lists and
their referenced scripts, streams an identity-bound SQLite artifact, and then
exits so all path/group memory is released at the process boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


GM_PVF_WORKER_FLAG = "--usdof-gm-pvf-worker"
GM_PVF_WORKER_MODE_ENV = "USDOF_LAUNCH_MODE"
GM_PVF_WORKER_MODE = "gm-pvf-worker"
GM_PVF_WORKER_REQUEST_ARG = "--request-file"
GM_PVF_WORKER_PROTOCOL_VERSION = 1
DEFAULT_WORKER_TIMEOUT_SECONDS = 15 * 60.0

WorkerProgress = Callable[[float, str], None]
WorkerLog = Callable[[str], None]
WorkerProcessCallback = Callable[[Optional[subprocess.Popen]], None]


def _report(callback: Optional[WorkerProgress], progress: float, detail: str) -> None:
    if callback is None:
        return
    callback(max(0.0, min(1.0, float(progress))), str(detail))


def _emit_event(event: str, **payload: Any) -> None:
    document = {"event": str(event)}
    document.update(payload)
    print(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def _stable_sha256(
    path: Path,
    progress_callback: Optional[WorkerProgress],
) -> tuple[str, int, int]:
    try:
        before = path.stat()
        total = max(1, int(before.st_size))
        digest = hashlib.sha256()
        completed = 0
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
                completed += len(chunk)
                crossed_progress_boundary = (
                    completed % (64 * 1024 * 1024) < len(chunk)
                )
                if (
                    completed == len(chunk)
                    or completed == total
                    or crossed_progress_boundary
                ):
                    _report(
                        progress_callback,
                        0.04 + min(1.0, float(completed) / float(total)) * 0.12,
                        "Verifying GM worker PVF identity",
                    )
            closed = os.fstat(handle.fileno())
        after = path.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError("GM worker PVF is missing: %s" % path) from exc
    opened_id = (int(opened.st_size), int(opened.st_mtime_ns))
    closed_id = (int(closed.st_size), int(closed.st_mtime_ns))
    final_id = (int(after.st_size), int(after.st_mtime_ns))
    if (
        opened_id != closed_id
        or closed_id != final_id
        or int(before.st_size) != int(after.st_size)
    ):
        raise RuntimeError("Script.pvf changed while GM worker verified it: %s" % path)
    return digest.hexdigest(), int(after.st_size), int(after.st_mtime_ns)


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path.resolve())))


def _validated_identity(raw: Any):
    from pvf.index_cache import PvfIdentity

    if not isinstance(raw, dict):
        raise ValueError("GM worker expected identity must be an object")
    identity = PvfIdentity(
        path=str(raw.get("path") or ""),
        sha256=str(raw.get("sha256") or "").lower(),
        file_size=int(raw.get("file_size") or 0),
        file_count=int(raw.get("file_count") or 0),
        group_count=int(raw.get("group_count") or 0),
        python=str(raw.get("python") or ""),
    )
    if not identity.path or len(identity.sha256) != 64:
        raise ValueError("GM worker expected PVF identity is incomplete")
    try:
        int(identity.sha256, 16)
    except ValueError as exc:
        raise ValueError("GM worker expected PVF SHA-256 is invalid") from exc
    if min(identity.file_size, identity.file_count, identity.group_count) <= 0:
        raise ValueError("GM worker expected PVF dimensions are invalid")
    return identity


def _fallback_metadata(item_id: int, item_kind: str, source_path: str):
    from game.character.item_metadata import ItemMetadata

    if item_kind == "equipment":
        return ItemMetadata(
            item_id=int(item_id),
            item_kind="equipment",
            stack_limit=1,
            source_path=str(source_path),
            note="gm_worker_read_fail",
        )
    return ItemMetadata(
        item_id=int(item_id),
        item_kind="stackable",
        stackable_type="[waste]",
        stack_limit=0,
        source_path=str(source_path),
        note="gm_worker_read_fail",
    )


def _catalog_metadata(item_id: int, item_kind: str, source_path: str, text: str):
    from game.character.item_metadata import (
        ItemMetadata,
        _extract_equipment_type,
        _extract_int_field,
        _extract_minimum_level,
        _extract_name,
        _extract_rarity,
        _extract_stackable_type,
        _is_avatar_equipment_type,
        normalize_stack_limit,
    )

    if item_kind == "equipment":
        equipment_type = _extract_equipment_type(text)
        return ItemMetadata(
            item_id=int(item_id),
            item_kind="equipment",
            stack_limit=1,
            equipment_type=equipment_type,
            is_avatar=_is_avatar_equipment_type(equipment_type),
            minimum_level=_extract_minimum_level(text),
            name=_extract_name(text),
            source_path=str(source_path),
            note="ok:gm_catalog_worker",
            rarity=_extract_rarity(text),
        )

    stackable_type = _extract_stackable_type(text)
    explicit_limit = _extract_int_field(text, "stack limit")
    stack_limit = normalize_stack_limit(explicit_limit)
    return ItemMetadata(
        item_id=int(item_id),
        item_kind="stackable",
        stackable_type=stackable_type or "[waste]",
        stack_limit=int(stack_limit),
        minimum_level=_extract_minimum_level(text),
        name=_extract_name(text),
        source_path=str(source_path),
        note="ok:gm_catalog_worker",
        rarity=_extract_rarity(text),
    )


def _record_from_metadata(metadata: Any):
    from game.character.gm_item_catalog import GmItemRecord, _category_for_metadata

    return GmItemRecord(
        item_id=int(metadata.item_id),
        name=str(metadata.name or ""),
        category=_category_for_metadata(metadata),
        rarity=max(0, int(metadata.rarity or 0)),
        minimum_level=max(0, int(metadata.minimum_level or 0)),
        stack_limit=max(0, int(metadata.effective_stack_limit or 0)),
        source_path=str(metadata.source_path or ""),
    )


def iter_selective_catalog_records(
    reader: Any,
    *,
    pvf_sha256: str,
    progress_callback: Optional[WorkerProgress] = None,
):
    """Yield GM search records in group order without retaining the full catalog."""
    from game.character.gm_item_catalog import _parse_lst

    _report(progress_callback, 0.18, "Reading GM item lists")
    equipment = _parse_lst(
        reader.read_text("equipment/equipment.lst"),
        "equipment",
    )
    stackable = _parse_lst(
        reader.read_text("stackable/stackable.lst"),
        "stackable",
    )
    item_ids = sorted(set(equipment) | set(stackable))
    if not item_ids:
        raise RuntimeError("GM item lists are empty")

    _report(progress_callback, 0.22, "Resolving GM item script paths")
    ordered = []
    missing = []
    for position, item_id in enumerate(item_ids, 1):
        if item_id in equipment:
            item_kind = "equipment"
            source_path = equipment[item_id]
        else:
            item_kind = "stackable"
            source_path = stackable[item_id]
        try:
            entry = reader.get_entry(source_path)
        except (KeyError, ValueError):
            missing.append((int(item_id), item_kind, source_path))
        else:
            ordered.append(
                (
                    int(entry.chunk_index),
                    int(entry.data_offset),
                    int(item_id),
                    int(getattr(entry, "index", -1)),
                    item_kind,
                    source_path,
                )
            )
        if position == len(item_ids) or position % 8192 == 0:
            _report(
                progress_callback,
                0.22 + 0.14 * float(position) / float(len(item_ids)),
                "Resolving GM item script paths %d/%d" % (position, len(item_ids)),
            )

    ordered.sort(key=lambda value: (value[0], value[1], value[2]))
    relevant_groups = len({value[0] for value in ordered})
    total = len(ordered) + len(missing)
    if total <= 0:
        raise RuntimeError("GM item catalog is empty")

    def generate():
        completed = 0
        read_text_by_index = getattr(reader, "read_text_by_index", None)
        for (
            _chunk_index,
            _data_offset,
            item_id,
            entry_index,
            item_kind,
            source_path,
        ) in ordered:
            try:
                if callable(read_text_by_index) and entry_index >= 0:
                    text = read_text_by_index(entry_index)
                else:
                    text = reader.read_text(source_path)
            except Exception:
                metadata = _fallback_metadata(item_id, item_kind, source_path)
            else:
                metadata = _catalog_metadata(item_id, item_kind, source_path, text)
            yield _record_from_metadata(metadata)
            completed += 1
            if completed == 1 or completed == total or completed % 512 == 0:
                _report(
                    progress_callback,
                    0.36 + 0.54 * float(completed) / float(total),
                    "Building GM item catalog %d/%d" % (completed, total),
                )

        for item_id, item_kind, source_path in missing:
            yield _record_from_metadata(
                _fallback_metadata(item_id, item_kind, source_path)
            )
            completed += 1
            if completed == total or completed % 512 == 0:
                _report(
                    progress_callback,
                    0.36 + 0.54 * float(completed) / float(total),
                    "Building GM item catalog %d/%d" % (completed, total),
                )

    return generate(), {
        "listed_item_count": len(item_ids),
        "script_item_count": len(ordered),
        "missing_script_count": len(missing),
        "relevant_group_count": relevant_groups,
    }


def build_selective_catalog(
    reader: Any,
    *,
    pvf_sha256: str,
    progress_callback: Optional[WorkerProgress] = None,
):
    """Materialized reference helper retained for focused equivalence tests."""
    from game.character.gm_item_catalog import GmItemCatalog

    records, stats = iter_selective_catalog_records(
        reader,
        pvf_sha256=pvf_sha256,
        progress_callback=progress_callback,
    )
    return GmItemCatalog(records, pvf_sha256=str(pvf_sha256)), stats


def execute_worker_request(
    request: dict[str, Any],
    *,
    progress_callback: Optional[WorkerProgress] = None,
) -> dict[str, Any]:
    from game.character.gm_item_catalog import (
        CATALOG_CACHE_DOMAIN,
        CATALOG_CACHE_VERSION,
    )
    from game.character.gm_item_catalog_sqlite import (
        catalog_artifact_path,
        catalog_file_sha256,
        publish_catalog_database,
        validate_catalog_database,
    )
    from pvf.index_cache import PvfIndexCache
    from pvf.nkpi_reader import PvfReader

    started = time.perf_counter()
    if int(request.get("protocol_version") or -1) != GM_PVF_WORKER_PROTOCOL_VERSION:
        raise ValueError("GM worker protocol version mismatch")
    if int(request.get("semantic_version") or -1) != CATALOG_CACHE_VERSION:
        raise ValueError("GM worker catalog version mismatch")
    identity = _validated_identity(request.get("pvf_identity"))
    pvf_path = Path(str(request.get("pvf_path") or "")).expanduser().resolve()
    if _path_identity(pvf_path) != _path_identity(Path(identity.path)):
        raise ValueError("GM worker PVF path does not match the server identity")
    runtime_python = "%d.%d.%d" % sys.version_info[:3]
    if identity.python != runtime_python:
        raise RuntimeError(
            "GM worker Python identity mismatch: expected=%s actual=%s"
            % (identity.python, runtime_python)
        )
    cache_root = Path(str(request.get("cache_root") or "")).expanduser().resolve()
    cache = PvfIndexCache(
        identity,
        root=cache_root,
        enabled=bool(request.get("cache_enabled", True)),
        clear=False,
    )
    if not cache.enabled:
        raise RuntimeError("GM item catalog cache is disabled")

    _report(progress_callback, 0.01, "Checking GM item catalog cache")
    cache_file = catalog_artifact_path(cache)
    legacy_cache_file = cache.artifact_path(CATALOG_CACHE_DOMAIN)
    invalid_artifact = False
    database_stats = None
    if cache_file.is_file():
        try:
            database_stats = validate_catalog_database(
                cache_file,
                expected_identity=identity,
                verify_digest=True,
            )
        except Exception:
            invalid_artifact = True
            try:
                cache_file.unlink()
            except FileNotFoundError:
                pass
    if database_stats is not None:
        scan_stats = {
            "listed_item_count": int(database_stats["item_count"]),
            "script_item_count": 0,
            "missing_script_count": 0,
            "relevant_group_count": 0,
        }
        cache_status = "hit"
        _report(progress_callback, 1.0, "GM item catalog cache ready")
    else:
        actual_sha256, actual_size, source_mtime_ns = _stable_sha256(
            pvf_path,
            progress_callback,
        )
        if actual_sha256 != identity.sha256 or actual_size != identity.file_size:
            raise RuntimeError(
                "GM worker PVF identity changed: expected=%s/%d actual=%s/%d"
                % (
                    identity.sha256,
                    identity.file_size,
                    actual_sha256,
                    actual_size,
                )
            )
        _report(progress_callback, 0.17, "Opening dedicated GM PVF reader")
        # The worker processes scripts in chunk order.  An unbounded cache keeps
        # each relevant group decompressed once; process exit releases it all.
        with PvfReader.open(pvf_path, group_cache_limit=0) as reader:
            if int(reader.file_count) != identity.file_count:
                raise RuntimeError("GM worker PVF file count changed")
            if int(reader.group_count) != identity.group_count:
                raise RuntimeError("GM worker PVF group count changed")
            expected_profile = str(request.get("pvf_profile") or "")
            if expected_profile and reader.profile_name != expected_profile:
                raise RuntimeError(
                    "GM worker PVF profile changed: expected=%s actual=%s"
                    % (expected_profile, reader.profile_name)
                )
            records, scan_stats = iter_selective_catalog_records(
                reader,
                pvf_sha256=identity.sha256,
                progress_callback=progress_callback,
            )
            database_stats = publish_catalog_database(
                cache_file,
                identity=identity,
                records=records,
                expected_record_count=int(scan_stats["listed_item_count"]),
            )
        final = pvf_path.stat()
        if (
            int(final.st_size) != identity.file_size
            or int(final.st_mtime_ns) != source_mtime_ns
        ):
            try:
                cache_file.unlink()
            except FileNotFoundError:
                pass
            raise RuntimeError("Script.pvf changed while GM worker scanned it")
        _report(progress_callback, 0.96, "Validating GM item catalog database")
        cache_status = "rebuild" if (invalid_artifact or legacy_cache_file.is_file()) else "miss"
        _report(progress_callback, 1.0, "GM item catalog cache ready")

    cache_file = cache_file.resolve()
    if not cache_file.is_file():
        raise RuntimeError("GM worker did not publish the catalog artifact")
    try:
        legacy_cache_file.unlink()
    except FileNotFoundError:
        pass
    if not isinstance(database_stats, dict):
        raise RuntimeError("GM worker did not validate the catalog database")
    if not database_stats.get("artifact_sha256"):
        artifact_sha256, artifact_size = catalog_file_sha256(cache_file)
        database_stats["artifact_sha256"] = artifact_sha256
        database_stats["artifact_size"] = artifact_size
    counts = dict(database_stats["category_counts"])
    result = {
        "cache_file": str(cache_file),
        "cache": cache_status,
        "pvf_sha256": identity.sha256,
        "pvf_identity": asdict(identity),
        "artifact_sha256": str(database_stats["artifact_sha256"]),
        "artifact_size": int(database_stats["artifact_size"]),
        "item_count": int(database_stats["item_count"]),
        "equipment_count": counts["equipment"],
        "stackable_count": counts["stackable"],
        "avatar_count": counts["avatar"],
        "pet_count": counts["pet"],
        "guild_medal_count": counts["guild_medal"],
        "guardian_gem_count": counts["guardian_gem"],
        "worker_pid": os.getpid(),
        "elapsed_ms": int(round((time.perf_counter() - started) * 1000.0)),
    }
    result.update(scan_stats)
    return result


def create_worker_request(cache: Any) -> dict[str, Any]:
    from config import PVF_PATH
    from game.character.gm_item_catalog import CATALOG_CACHE_VERSION
    from pvf import pvf_runtime

    identity = getattr(cache, "identity", None)
    if identity is None:
        raise ValueError("GM worker cache context has no PVF identity")
    shared = pvf_runtime.get_shared_reader(PVF_PATH)
    if shared is None or not pvf_runtime.is_memory_prepared(PVF_PATH):
        raise RuntimeError("GM worker requires the server's verified PVF identity")
    if _path_identity(Path(identity.path)) != _path_identity(Path(PVF_PATH)):
        raise RuntimeError("GM worker cache identity path differs from the shared PVF")
    return {
        "protocol_version": GM_PVF_WORKER_PROTOCOL_VERSION,
        "semantic_version": CATALOG_CACHE_VERSION,
        "pvf_path": str(Path(identity.path).resolve()),
        "pvf_profile": str(getattr(shared, "profile_name", "") or ""),
        "pvf_identity": asdict(identity),
        "cache_root": str(Path(cache.root).resolve()),
        "cache_enabled": bool(getattr(cache, "enabled", False)),
    }


def worker_command(request_file: Path, *, frozen: Optional[bool] = None) -> list[str]:
    frozen_mode = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if frozen_mode:
        return [
            str(Path(sys.executable).resolve()),
            GM_PVF_WORKER_FLAG,
            GM_PVF_WORKER_REQUEST_ARG,
            str(Path(request_file).resolve()),
        ]
    return [
        str(Path(sys.executable).resolve()),
        "-u",
        "-c",
        "from control.gm_catalog_worker import main; raise SystemExit(main())",
        GM_PVF_WORKER_REQUEST_ARG,
        str(Path(request_file).resolve()),
    ]


def _write_request_file(cache: Any, request: dict[str, Any]) -> Path:
    directory = Path(getattr(cache, "directory", tempfile.gettempdir())).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".gm_catalog_worker_",
        suffix=".json",
        dir=str(directory),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                request,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def run_gm_catalog_worker(
    cache: Any,
    *,
    progress_callback: Optional[WorkerProgress] = None,
    log_callback: Optional[WorkerLog] = None,
    cancel_event: Optional[threading.Event] = None,
    process_callback: Optional[WorkerProcessCallback] = None,
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Run one source/frozen worker process and return its explicit cache source."""
    request = create_worker_request(cache)
    raw_timeout: Any = (
        timeout_seconds
        if timeout_seconds is not None
        else os.environ.get(
            "USDOF_GM_PVF_WORKER_TIMEOUT",
            str(DEFAULT_WORKER_TIMEOUT_SECONDS),
        )
    )
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("GM catalog worker timeout must be numeric") from exc
    if not math.isfinite(timeout):
        raise ValueError("GM catalog worker timeout must be finite")
    timeout = max(1.0, timeout)

    request_file = _write_request_file(cache, request)
    process: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None
    output_lines = []
    result = None
    child_error = ""
    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def read_output() -> None:
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                line_queue.put(line)
        finally:
            line_queue.put(None)

    try:
        command = worker_command(request_file)
        env = os.environ.copy()
        if bool(getattr(sys, "frozen", False)):
            env[GM_PVF_WORKER_MODE_ENV] = GM_PVF_WORKER_MODE
        else:
            env.pop(GM_PVF_WORKER_MODE_ENV, None)
        cwd = Path(
            str(
                os.environ.get("USDOF_SERVER_HOME")
                or Path(__file__).resolve().parents[1]
            )
        )
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            if os.name == "nt"
            else 0
        )
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("GM catalog worker was cancelled before launch")
        _report(progress_callback, 0.0, "Starting dedicated GM PVF worker")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        if process_callback is not None:
            process_callback(process)
        reader_thread = threading.Thread(
            target=read_output,
            name="usdof-gm-worker-output",
            daemon=True,
        )
        reader_thread.start()
        deadline = time.monotonic() + max(1.0, timeout)
        stream_closed = False
        while not stream_closed:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                raise RuntimeError("GM catalog worker was cancelled")
            if time.monotonic() >= deadline:
                process.terminate()
                raise TimeoutError("GM catalog worker timed out after %.1fs" % timeout)
            try:
                line = line_queue.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and not reader_thread.is_alive():
                    break
                continue
            if line is None:
                stream_closed = True
                continue
            text = line.strip()
            if not text:
                continue
            output_lines.append(text)
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                if log_callback is not None:
                    log_callback(text)
                continue
            if not isinstance(event, dict):
                continue
            event_name = str(event.get("event") or "")
            if event_name == "progress":
                _report(
                    progress_callback,
                    float(event.get("progress") or 0.0),
                    str(event.get("detail") or ""),
                )
            elif event_name == "result":
                value = event.get("result")
                if isinstance(value, dict):
                    result = dict(value)
            elif event_name == "error":
                child_error = str(event.get("error") or "")
        return_code = process.wait(timeout=5.0)
        if return_code != 0:
            detail = child_error or " | ".join(output_lines[-8:])
            raise RuntimeError(
                "GM catalog worker exited with code %s: %s" % (return_code, detail)
            )
        if not isinstance(result, dict):
            raise RuntimeError("GM catalog worker returned no result")
        expected_identity = request["pvf_identity"]
        if result.get("pvf_identity") != expected_identity:
            raise RuntimeError("GM catalog worker returned a different PVF identity")
        _report(progress_callback, 1.0, "GM item catalog cache ready")
        return result
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except Exception:
                    pass
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
        try:
            if process_callback is not None:
                process_callback(None)
        finally:
            request_file.unlink(missing_ok=True)


def _read_request(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("GM worker request must be a JSON object")
    return document


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(GM_PVF_WORKER_REQUEST_ARG, required=True, type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        request = _read_request(args.request_file.expanduser().resolve())

        def report_progress(progress: float, detail: str) -> None:
            _emit_event("progress", progress=round(float(progress), 6), detail=str(detail))

        result = execute_worker_request(
            request,
            progress_callback=report_progress,
        )
        _emit_event("result", result=result)
        return 0
    except Exception as exc:
        _emit_event("error", error="%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GM_PVF_WORKER_FLAG",
    "GM_PVF_WORKER_MODE",
    "GM_PVF_WORKER_MODE_ENV",
    "build_selective_catalog",
    "create_worker_request",
    "execute_worker_request",
    "iter_selective_catalog_records",
    "run_gm_catalog_worker",
    "worker_command",
]
