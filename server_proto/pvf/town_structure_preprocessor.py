from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

try:
    from pvf.nkpi_reader import (
        FILE_ENTRY_SIZE,
        GROUP_ENTRY_SIZE,
        HEADER_MAGIC,
        HEADER_SIZE,
        INC1,
        PvfReader,
        decrypt_lcg,
    )
except ImportError:
    # The frozen share launcher embeds this module under server_proto.pvf.
    from server_proto.pvf.nkpi_reader import (
        FILE_ENTRY_SIZE,
        GROUP_ENTRY_SIZE,
        HEADER_MAGIC,
        HEADER_SIZE,
        INC1,
        PvfReader,
        decrypt_lcg,
    )


_AREA_MARKERS = frozenset(("`[gate]`", "`[normal]`", "`[dungeon gate]`"))
_CACHE_LOCK = threading.RLock()
_RESULT_CACHE: Dict[str, Tuple[Tuple[int, int], "TownStructurePreprocessResult"]] = {}
_COPY_CHUNK_SIZE = 8 * 1024 * 1024
_CACHE_SCHEMA = 1


class TownStructurePreprocessError(RuntimeError):
    """Raised before startup when a town script has ambiguous structure."""


@dataclass(frozen=True)
class TownStructurePreprocessResult:
    path: Path
    source_sha256: str
    scanned_files: int
    changed_files: int
    moved_permission_closures: int
    changed_paths: Tuple[str, ...]
    wrote_archive: bool
    cached: bool
    elapsed_ms: int


@dataclass(frozen=True)
class _RepairRecord:
    area_number: int
    marker_token_index: int
    permission_end_token_index: int
    marker: str


@dataclass(frozen=True)
class _FilePlan:
    path: str
    chunk_index: int
    data_offset: int
    data_size: int
    original_data: bytes
    normalized_data: bytes
    repairs: Tuple[_RepairRecord, ...]


@dataclass(frozen=True)
class _RawEntryView:
    name_offset: int
    path_offset: int
    chunk_index: int
    data_offset: int
    data_size: int
    data_type: int


@dataclass(frozen=True)
class _Analysis:
    scanned_files: int
    plans: Tuple[_FilePlan, ...]


def _file_identity(path: Path) -> Tuple[int, int]:
    stat = path.stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


def _sha256_stable(path: Path) -> Tuple[str, Tuple[int, int]]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(_COPY_CHUNK_SIZE), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    before_identity = (int(before.st_size), int(before.st_mtime_ns))
    after_identity = (int(after.st_size), int(after.st_mtime_ns))
    if before_identity != after_identity or _file_identity(path) != after_identity:
        raise RuntimeError("Script.pvf changed while hashing town cache identity: %s" % path)
    return digest.hexdigest(), after_identity


def _persistent_cache_dir(cache_dir: str | os.PathLike[str] | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).resolve()
    configured = os.environ.get("USDOF_TOWN_PREPROCESS_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    server_home = os.environ.get("USDOF_SERVER_HOME", "").strip()
    if server_home:
        return Path(server_home).resolve() / "data" / "runtime_cache" / "town_structure"
    return Path(__file__).resolve().parents[1] / "data" / "runtime_cache" / "town_structure"


def _load_persistent_cache(
    cache_root: Path,
    source_sha256: str,
    file_size: int,
) -> int | None:
    stamp_path = cache_root / (source_sha256 + ".json")
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema") != _CACHE_SCHEMA
        or payload.get("sha256") != source_sha256
        or payload.get("file_size") != file_size
    ):
        return None
    scanned_files = payload.get("scanned_files")
    if not isinstance(scanned_files, int) or scanned_files < 0:
        return None
    return scanned_files


def _write_persistent_cache(
    cache_root: Path,
    source_sha256: str,
    file_size: int,
    scanned_files: int,
) -> None:
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        destination = cache_root / (source_sha256 + ".json")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % source_sha256,
            suffix=".tmp",
            dir=str(cache_root),
        )
        temporary_path = Path(temporary_name)
        try:
            payload = {
                "schema": _CACHE_SCHEMA,
                "sha256": source_sha256,
                "file_size": file_size,
                "scanned_files": scanned_files,
            }
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary_path), str(destination))
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        # Cache persistence is only an optimization; archive validation remains authoritative.
        return


def _reader_identity(reader: PvfReader) -> Tuple[int, int]:
    if reader._file is None:
        raise RuntimeError("town preprocessor requires a file-backed PVF reader")
    stat = os.fstat(reader._file.fileno())
    return int(stat.st_size), int(stat.st_mtime_ns)


def _require_stable_source(
    path: Path,
    reader: PvfReader,
    expected: Tuple[int, int],
) -> None:
    handle_identity = _reader_identity(reader)
    path_identity = _file_identity(path)
    if handle_identity != expected or path_identity != expected:
        raise RuntimeError("Script.pvf changed during town structure preprocessing: %s" % path)


def _structure_error(
    path: str,
    area_number: int,
    token_index: int,
    message: str,
) -> TownStructurePreprocessError:
    return TownStructurePreprocessError(
        "path=%s area=%d token=%d: %s"
        % (path, area_number, token_index, message)
    )


def _token_display_line(reader: PvfReader, chunk: bytes) -> str:
    token_type = chunk[0]
    value = struct.unpack_from("<i", chunk, 1)[0]
    if token_type == 3:
        name = reader.resolve_name(value)
        if name.startswith("[") and name.endswith("]"):
            return name
        return "[%s]" % name
    if token_type == 6:
        return "`%s`" % reader.resolve_name(value)
    return ""


def _find_area_start(lines: Sequence[str], marker_index: int) -> int:
    for index in range(marker_index - 1, -1, -1):
        if lines[index] == "[area]":
            return index
        if lines[index] == "[/area]":
            return -1
    return -1


def _find_area_end(lines: Sequence[str], area_start: int) -> int:
    for index in range(area_start + 1, len(lines)):
        if lines[index] == "[/area]":
            return index
        if lines[index] == "[area]":
            return -1
    return -1


def _count_areas_through(lines: Sequence[str], area_start: int) -> int:
    return sum(1 for line in lines[: area_start + 1] if line == "[area]")


def _find_open_permission(
    lines: Sequence[str],
    area_start: int,
    marker_index: int,
    path: str,
    area_number: int,
) -> int:
    permission_start = -1
    for index in range(area_start + 1, marker_index):
        if lines[index] == "[permission]":
            if permission_start >= 0:
                raise _structure_error(
                    path,
                    area_number,
                    marker_index,
                    "nested [permission] begins at token %d" % index,
                )
            permission_start = index
        elif lines[index] == "[/permission]":
            if permission_start < 0:
                raise _structure_error(
                    path,
                    area_number,
                    marker_index,
                    "orphan [/permission] appears at token %d" % index,
                )
            permission_start = -1
    return permission_start


def _find_permission_end(
    lines: Sequence[str],
    marker_index: int,
    area_end: int,
    path: str,
    area_number: int,
) -> int:
    for index in range(marker_index + 1, area_end):
        if lines[index] == "[/permission]":
            return index
        if lines[index] == "[permission]":
            raise _structure_error(
                path,
                area_number,
                marker_index,
                "another [permission] begins at token %d before the open block closes"
                % index,
            )
    return -1


def _normalize_file(reader: PvfReader, path: str, raw_entry) -> _FilePlan:
    group = reader._load_group(raw_entry.chunk_index)
    entry_end = raw_entry.data_offset + raw_entry.data_size
    if raw_entry.data_offset < 0 or raw_entry.data_size < 0 or entry_end > len(group):
        raise _structure_error(path, 0, 0, "entry slice is outside its body group")
    data = group[raw_entry.data_offset:entry_end]
    if raw_entry.data_type != 1:
        raise _structure_error(path, 0, 0, "town script is not token DataType=1")
    if len(data) % 5 != 0:
        raise _structure_error(
            path,
            0,
            len(data) // 5,
            "token stream size %d is not divisible by 5" % len(data),
        )

    chunks = [bytearray(data[offset : offset + 5]) for offset in range(0, len(data), 5)]
    try:
        lines = [_token_display_line(reader, bytes(chunk)) for chunk in chunks]
    except Exception as exc:
        raise _structure_error(
            path,
            0,
            0,
            "token name resolution failed: %s: %s" % (type(exc).__name__, exc),
        ) from exc

    repairs: List[_RepairRecord] = []
    index = 0
    while index < len(lines):
        if lines[index] not in _AREA_MARKERS:
            index += 1
            continue

        area_start = _find_area_start(lines, index)
        if area_start < 0:
            raise _structure_error(
                path,
                0,
                index,
                "area marker %s is outside an [area] block" % lines[index],
            )
        area_number = _count_areas_through(lines, area_start)
        area_end = _find_area_end(lines, area_start)
        if area_end < 0 or index >= area_end:
            raise _structure_error(
                path,
                area_number,
                index,
                "[area] block has no unambiguous [/area]",
            )

        permission_start = _find_open_permission(
            lines, area_start, index, path, area_number
        )
        if permission_start < 0:
            index += 1
            continue

        permission_end = _find_permission_end(
            lines, index, area_end, path, area_number
        )
        if permission_end < 0:
            raise _structure_error(
                path,
                area_number,
                index,
                "marker %s is inside [permission], but no matching "
                "[/permission] exists in this area" % lines[index],
            )

        repairs.append(
            _RepairRecord(
                area_number=area_number,
                marker_token_index=index,
                permission_end_token_index=permission_end,
                marker=lines[index],
            )
        )
        close_line = lines.pop(permission_end)
        close_chunk = chunks.pop(permission_end)
        lines.insert(index, close_line)
        chunks.insert(index, close_chunk)
        index += 2

    normalized = b"".join(bytes(chunk) for chunk in chunks)
    if len(normalized) != len(data):
        raise _structure_error(path, 0, 0, "normalization changed the token stream size")
    return _FilePlan(
        path=path,
        chunk_index=raw_entry.chunk_index,
        data_offset=raw_entry.data_offset,
        data_size=raw_entry.data_size,
        original_data=data,
        normalized_data=normalized,
        repairs=tuple(repairs),
    )


def _exact_name_offsets(reader: PvfReader, value: str) -> frozenset[int]:
    reader._ensure_name_pools()
    assert reader._utf8_pool is not None
    assert reader._utf16_pool is not None
    offsets = set()
    utf8_needle = value.encode("utf-8") + b"\x00"
    start = 0
    while True:
        start = reader._utf8_pool.find(utf8_needle, start)
        if start < 0:
            break
        magic = start * 2
        if reader.resolve_name(magic) == value:
            offsets.add(magic)
        start += 1

    utf16_needle = value.encode("utf-16le") + b"\x00\x00"
    start = 0
    while True:
        start = reader._utf16_pool.find(utf16_needle, start)
        if start < 0:
            break
        if start % 2 == 0:
            magic = start | 1
            if reader.resolve_name(magic) == value:
                offsets.add(magic)
        start += 1
    return frozenset(offsets)


def _direct_town_entries(reader: PvfReader):
    town_offsets = _exact_name_offsets(reader, "town")
    file_table = reader._read_at(
        HEADER_SIZE, reader.header.file_count * FILE_ENTRY_SIZE
    )
    for values in struct.iter_unpack("<iiiiii", file_table):
        raw_entry = _RawEntryView(*values)
        if raw_entry.path_offset not in town_offsets:
            continue
        name = reader.resolve_name(raw_entry.name_offset).replace("\\", "/")
        if "/" in name or not name.casefold().endswith(".twn"):
            continue
        yield "town/%s" % name, raw_entry


def _analyze(reader: PvfReader) -> _Analysis:
    scanned_files = 0
    plans: List[_FilePlan] = []
    failures: List[str] = []
    for path, raw_entry in _direct_town_entries(reader):
        scanned_files += 1
        try:
            plan = _normalize_file(reader, path, raw_entry)
            if plan.repairs:
                plans.append(plan)
        except TownStructurePreprocessError as exc:
            failures.append(str(exc))
        except Exception as exc:
            failures.append(
                "path=%s area=0 token=0: %s: %s"
                % (path, type(exc).__name__, exc)
            )
    if failures:
        raise TownStructurePreprocessError(
            "town script preprocessing found ambiguous structure: %s"
            % " | ".join(failures[:20])
        )
    return _Analysis(scanned_files=scanned_files, plans=tuple(plans))


def _apply_header_body_size(
    raw_header: bytes,
    reader: PvfReader,
    new_body_size: int,
) -> bytes:
    encrypted = bytearray(raw_header)
    if reader.profile.guarded:
        for index in range(24, 28):
            encrypted[index] ^= 0x55
    plain = bytearray(decrypt_lcg(bytes(encrypted), reader.profile.header_seed, INC1))
    if struct.unpack_from("<I", plain, 0)[0] != HEADER_MAGIC:
        raise ValueError("header signature changed before town rewrite")
    if struct.unpack_from("<i", plain, 32)[0] != reader.header.body_size:
        raise ValueError("header body_size differs from the detected town layout")
    struct.pack_into("<i", plain, 32, new_body_size)
    rewritten = bytearray(decrypt_lcg(bytes(plain), reader.profile.header_seed, INC1))
    if reader.profile.guarded:
        for index in range(24, 28):
            rewritten[index] ^= 0x55
    return bytes(rewritten)


def _copy_reader_range(reader: PvfReader, output, offset: int, size: int) -> None:
    remaining = size
    current = offset
    while remaining:
        chunk_size = min(remaining, _COPY_CHUNK_SIZE)
        output.write(reader._read_at(current, chunk_size))
        current += chunk_size
        remaining -= chunk_size


def _build_changed_groups(
    reader: PvfReader,
    plans: Sequence[_FilePlan],
) -> Dict[int, bytes]:
    group_plain: Dict[int, bytearray] = {}
    for plan in plans:
        if plan.chunk_index not in group_plain:
            group_plain[plan.chunk_index] = bytearray(reader._load_group(plan.chunk_index))
        data = group_plain[plan.chunk_index]
        end = plan.data_offset + plan.data_size
        if bytes(data[plan.data_offset:end]) != plan.original_data:
            raise ValueError("entry bytes changed before rewrite: %s" % plan.path)
        data[plan.data_offset:end] = plan.normalized_data

    changed: Dict[int, bytes] = {}
    for group_index, plain in group_plain.items():
        compressed = zlib.compress(bytes(plain), 9)
        changed[group_index] = decrypt_lcg(
            compressed, reader.profile.body_seed, INC1
        )
    return changed


def _rewritten_group_table(
    reader: PvfReader,
    changed_groups: Dict[int, bytes],
) -> Tuple[bytes, Tuple[Tuple[int, int], ...], int]:
    reader._ensure_groups()
    assert reader._groups is not None
    encrypted = reader._read_at(
        reader.layout.group_offset,
        reader.header.group_count * GROUP_ENTRY_SIZE,
    )
    plain = bytearray(decrypt_lcg(encrypted, reader.profile.group_seed, INC1))
    cumulative = 0
    expected_groups: List[Tuple[int, int]] = []
    previous = 0
    for index, (old_cumulative, original_size) in enumerate(reader._groups):
        old_compressed_size = old_cumulative - previous
        previous = old_cumulative
        compressed_size = len(changed_groups.get(index, b"")) or old_compressed_size
        cumulative += compressed_size
        if cumulative > 0x7FFFFFFF:
            raise ValueError("rewritten body_size is outside signed 32-bit range")
        struct.pack_into(
            "<ii", plain, index * GROUP_ENTRY_SIZE, cumulative, original_size
        )
        expected_groups.append((cumulative, original_size))
    return (
        decrypt_lcg(bytes(plain), reader.profile.group_seed, INC1),
        tuple(expected_groups),
        cumulative,
    )


def _verify_output(
    output_path: Path,
    source_reader: PvfReader,
    plans: Sequence[_FilePlan],
    expected_groups: Sequence[Tuple[int, int]],
    expected_size: int,
) -> None:
    if output_path.stat().st_size != expected_size:
        raise ValueError("rewritten PVF file size does not match its layout")
    with PvfReader.open(output_path) as output_reader:
        if output_reader.profile_name != source_reader.profile_name:
            raise ValueError("PVF profile changed during town rewrite")
        if output_reader.header.file_count != source_reader.header.file_count:
            raise ValueError("PVF file count changed during town rewrite")
        if output_reader.header.group_count != source_reader.header.group_count:
            raise ValueError("PVF group count changed during town rewrite")
        if output_reader.header.hash_table_size != source_reader.header.hash_table_size:
            raise ValueError("PVF hash section changed during town rewrite")
        if output_reader.header.name_table_size != source_reader.header.name_table_size:
            raise ValueError("PVF name section changed during town rewrite")
        output_reader._ensure_groups()
        assert output_reader._groups is not None
        if tuple(output_reader._groups) != tuple(expected_groups):
            raise ValueError("PVF group table changed unexpectedly during town rewrite")

        path_to_plan = {plan.path.casefold(): plan for plan in plans}
        verified_paths = set()
        for path, raw_entry in _direct_town_entries(output_reader):
            plan = path_to_plan.get(path.casefold())
            if plan is None:
                continue
            group = output_reader._load_group(raw_entry.chunk_index)
            end = raw_entry.data_offset + raw_entry.data_size
            if group[raw_entry.data_offset:end] != plan.normalized_data:
                raise ValueError("normalized town entry failed reopen verification: %s" % path)
            verified_paths.add(path.casefold())
        if verified_paths != set(path_to_plan):
            raise ValueError("not every normalized town entry was found after reopen")


def _write_normalized_archive(
    path: Path,
    reader: PvfReader,
    source_identity: Tuple[int, int],
    analysis: _Analysis,
) -> Path:
    changed_groups = _build_changed_groups(reader, analysis.plans)
    group_table, expected_groups, new_body_size = _rewritten_group_table(
        reader, changed_groups
    )
    raw_header = reader._read_at(0, HEADER_SIZE)
    rewritten_header = _apply_header_body_size(raw_header, reader, new_body_size)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % path.name,
        suffix=".town-preprocess.tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(rewritten_header)
            _copy_reader_range(
                reader,
                output,
                HEADER_SIZE,
                reader.layout.group_offset - HEADER_SIZE,
            )
            output.write(group_table)

            reader._ensure_groups()
            assert reader._groups is not None
            previous = 0
            for index, (cumulative, _original_size) in enumerate(reader._groups):
                replacement = changed_groups.get(index)
                if replacement is not None:
                    output.write(replacement)
                else:
                    compressed_size = cumulative - previous
                    _copy_reader_range(
                        reader,
                        output,
                        reader.layout.body_offset + previous,
                        compressed_size,
                    )
                previous = cumulative
            output.flush()
            os.fsync(output.fileno())

        expected_size = reader.layout.body_offset + new_body_size
        _verify_output(
            temporary_path,
            reader,
            analysis.plans,
            expected_groups,
            expected_size,
        )
        _require_stable_source(path, reader, source_identity)
        return temporary_path
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def preprocess_town_structure(
    pvf_path: str | os.PathLike[str],
    *,
    cache_dir: str | os.PathLike[str] | None = None,
) -> TownStructurePreprocessResult:
    """Normalize malformed direct ``town/*.twn`` files before PVF detection.

    The full archive is analyzed before any write. A normal archive remains
    byte-for-byte untouched. A repair rewrites only affected compressed body
    groups, verifies a same-directory temporary archive, then atomically
    replaces the source file.
    """

    started = time.monotonic()
    path = Path(pvf_path).resolve()
    initial_identity = _file_identity(path)
    cache_key = str(path).casefold()
    with _CACHE_LOCK:
        cached = _RESULT_CACHE.get(cache_key)
        if cached is not None and cached[0] == initial_identity:
            return replace(
                cached[1],
                changed_files=0,
                moved_permission_closures=0,
                changed_paths=(),
                wrote_archive=False,
                cached=True,
                elapsed_ms=0,
            )

    source_sha256, hashed_identity = _sha256_stable(path)
    if hashed_identity != initial_identity:
        raise RuntimeError("Script.pvf changed before town cache lookup: %s" % path)
    cache_root = _persistent_cache_dir(cache_dir)
    cached_scanned_files = _load_persistent_cache(
        cache_root, source_sha256, initial_identity[0]
    )
    if cached_scanned_files is not None:
        result = TownStructurePreprocessResult(
            path=path,
            source_sha256=source_sha256,
            scanned_files=cached_scanned_files,
            changed_files=0,
            moved_permission_closures=0,
            changed_paths=(),
            wrote_archive=False,
            cached=True,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        with _CACHE_LOCK:
            _RESULT_CACHE[cache_key] = (initial_identity, result)
        return result

    temporary_path = None
    source_identity = initial_identity
    with PvfReader.open(path) as reader:
        source_identity = _reader_identity(reader)
        if source_identity != initial_identity:
            raise RuntimeError("Script.pvf changed before town structure preprocessing: %s" % path)
        analysis = _analyze(reader)
        _require_stable_source(path, reader, source_identity)
        if analysis.plans:
            temporary_path = _write_normalized_archive(
                path, reader, source_identity, analysis
            )

    if temporary_path is not None:
        try:
            if _file_identity(path) != source_identity:
                raise RuntimeError(
                    "Script.pvf changed before atomic town replacement: %s" % path
                )
            os.replace(str(temporary_path), str(path))
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    final_identity = _file_identity(path)
    if analysis.plans:
        final_sha256, final_identity = _sha256_stable(path)
    else:
        final_sha256 = source_sha256
    result = TownStructurePreprocessResult(
        path=path,
        source_sha256=final_sha256,
        scanned_files=analysis.scanned_files,
        changed_files=len(analysis.plans),
        moved_permission_closures=sum(len(plan.repairs) for plan in analysis.plans),
        changed_paths=tuple(plan.path for plan in analysis.plans),
        wrote_archive=bool(analysis.plans),
        cached=False,
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
    )
    _write_persistent_cache(
        cache_root,
        final_sha256,
        final_identity[0],
        analysis.scanned_files,
    )
    with _CACHE_LOCK:
        _RESULT_CACHE[cache_key] = (final_identity, result)
    return result


def format_town_preprocess_result(
    result: TownStructurePreprocessResult,
    context: str,
) -> str:
    changed = ",".join(result.changed_paths) if result.changed_paths else "none"
    return (
        "[PVF] town preprocessor context=%s scanned=%d changed=%d moved=%d "
        "wrote=%d cached=%d elapsed_ms=%d paths=%s"
        % (
            context,
            result.scanned_files,
            result.changed_files,
            result.moved_permission_closures,
            int(result.wrote_archive),
            int(result.cached),
            result.elapsed_ms,
            changed,
        )
    )
